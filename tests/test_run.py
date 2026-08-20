"""What `ma run` hands the container, and what it must never hand it.

render.py is stubbed here on purpose: these tests are about the launch — the
env file, the argv, the mounts — not about proxy config content.
"""
import json
import shlex

import pytest

from multiagent import run
from multiagent.types import ObservedModel, ProbeResult

BACKENDS = """
backends:
  box:
    type: ollama
    api_base: http://localhost:11434
    discovery: dynamic
  cloud:
    type: gemini
    credential: cloud-key
    models: [gemini-2.5-pro]
"""

PROJECTS = """
projects:
  all-in:
    backends: [box, cloud]
  local:
    backends: [box]
"""

MODELS = """
models:
  qwen2.5-14b:
    match: ["qwen2.5:14b"]
    context: 131072
  gemini-2.5-pro:
    match: ["gemini-2.5-pro*"]
"""

RENDERED = "model_list: [stand-in for the rendered proxy config]\n"
SECRET = "s3cret-gemini-value"
CREDENTIAL_FILE = f"GEMINI_API_KEY={SECRET}\nGEMINI_PROJECT=demo\n"


@pytest.fixture
def config_dir(tmp_path):
    d = tmp_path / "config"
    d.mkdir()
    (d / "backends.yaml").write_text(BACKENDS)
    (d / "projects.yaml").write_text(PROJECTS)
    (d / "models.yaml").write_text(MODELS)
    (d / "catalog.json").write_text(json.dumps({}))
    return d


@pytest.fixture
def launch(monkeypatch, tmp_path, config_dir):
    """Run `ma run` with the network, docker, and render.py stood in for.

    Returns a Launch: the exit code, the docker argv, and the env file as it
    looked while docker was being called (the launch dir is deleted after).
    """
    from multiagent import cli

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    probes = {
        "box": ProbeResult("live", [ObservedModel("qwen2.5:14b", {})]),
        "cloud": ProbeResult("static", [ObservedModel("gemini-2.5-pro", {})]),
    }
    monkeypatch.setattr(run, "probe_backend", lambda backend: probes[backend.name])

    seen = {}

    def fake_render(backends, merged, key_env):
        seen["backends"] = backends
        seen["merged"] = merged
        seen["key_env"] = key_env
        return {"model_list": []}

    monkeypatch.setattr(run, "render_config", fake_render)
    monkeypatch.setattr(run, "to_yaml", lambda config: RENDERED)

    real_pipeline = run.pipeline

    def spy_pipeline(args, probe=None):
        seen["pipeline"] = real_pipeline(args, probe)
        return seen["pipeline"]

    monkeypatch.setattr(run, "pipeline", spy_pipeline)

    class Launch:
        code = None
        argv = None
        env_text = None
        env_mode = None
        config_text = None
        called = False
        rendered = seen

    def fake_docker(argv, *args, **kwargs):
        Launch.called = True
        Launch.argv = list(argv)
        env_path = _env_path(argv)
        Launch.env_text = env_path.read_text()
        Launch.env_mode = env_path.stat().st_mode & 0o777
        Launch.config_text = (env_path.parent / "config.yaml").read_text()
        return _Completed(7)

    monkeypatch.setattr(run.subprocess, "run", fake_docker)

    cred_dir = tmp_path / "creds"
    cred_dir.mkdir()

    def go(*extra, project="all-in", cred=True, expires=None):
        if cred:
            comment = f"# expires: {expires}\n" if expires else ""
            (cred_dir / "cloud-key.env").write_text(comment + CREDENTIAL_FILE)
        argv = [
            "run",
            "--project", project,
            "--config", str(config_dir),
            "--machine", str(tmp_path / "machine.yaml"),
            "--state", str(tmp_path / "state.json"),
            "--cred-dir", str(cred_dir),
            *extra,
        ]
        Launch.code = cli.main(argv)
        return Launch

    go.cred_dir = cred_dir
    return go


class _Completed:
    def __init__(self, returncode):
        self.returncode = returncode


def _env_path(argv):
    from pathlib import Path

    return Path(argv[argv.index("--env-file") + 1])


def _flag(argv, flag):
    """Every value given to a repeated flag, e.g. every -v."""
    return [argv[i + 1] for i, a in enumerate(argv) if a == flag]


def test_env_file_carries_the_credentials_and_a_master_key(launch):
    result = launch()
    assert result.code == 7  # docker's exit status is the launch's
    assert result.env_mode == 0o600
    lines = result.env_text.splitlines()
    assert f"GEMINI_API_KEY={SECRET}" in lines
    assert "GEMINI_PROJECT=demo" in lines
    master = next(line for line in lines if line.startswith("LITELLM_MASTER_KEY="))
    assert len(master.split("=", 1)[1]) >= 32


def test_no_secret_reaches_the_argv(launch):
    result = launch()
    master = next(
        line for line in result.env_text.splitlines()
        if line.startswith("LITELLM_MASTER_KEY=")
    ).split("=", 1)[1]
    joined = " ".join(result.argv)
    assert SECRET not in joined
    assert master not in joined
    assert "GEMINI_API_KEY=" not in joined  # the name may appear, never a value


def test_scrub_names_exactly_the_credential_vars(launch):
    result = launch()
    scrub = next(a for a in result.argv if a.startswith("MA_SCRUB="))
    assert scrub == "MA_SCRUB=GEMINI_API_KEY,GEMINI_PROJECT"
    assert "MA_PROJECT=all-in" in result.argv


def test_key_env_picks_the_api_key_variable(launch):
    result = launch()
    assert result.rendered["key_env"] == {"cloud": "GEMINI_API_KEY"}


def test_loopback_is_rewritten_for_the_container_only(launch):
    result = launch()
    rendered = result.rendered["backends"]
    assert rendered["box"].api_base == "http://host.docker.internal:11434"
    assert rendered["cloud"].api_base is None
    # The rewrite is on copies: what the host loaded still names the host.
    loaded = result.rendered["pipeline"].config.backends
    assert loaded["box"].api_base == "http://localhost:11434"
    assert rendered["box"] is not loaded["box"]


def test_missing_credential_is_a_hard_failure(launch, capsys, tmp_path):
    result = launch(cred=False)
    err = capsys.readouterr().err
    assert result.code == 2
    assert result.called is False
    assert str(tmp_path / "creds" / "cloud-key.env") in err
    assert "cloud" in err


def test_stale_credential_warns_but_launches(launch, capsys):
    result = launch(expires="2020-01-01T00:00:00Z")
    err = capsys.readouterr().err
    assert result.code == 7
    assert "cloud-key" in err and "ma keys cloud-key" in err
    # Warned, not withheld: the expired values still reach the proxy.
    assert f"GEMINI_API_KEY={SECRET}" in result.env_text.splitlines()


def test_down_backend_warns_but_launches(launch, monkeypatch, capsys):
    down = ProbeResult("down", error="URLError: connection refused")
    monkeypatch.setattr(
        run, "probe_backend",
        lambda backend: down if backend.name == "box" else ProbeResult("static", []),
    )
    result = launch()
    err = capsys.readouterr().err
    assert result.code == 7
    assert "box" in err and "connection refused" in err


def test_dry_run_prints_the_plan_and_starts_nothing(launch, capsys):
    result = launch("--dry-run")
    out = capsys.readouterr().out
    assert result.code == 0
    assert result.called is False
    assert RENDERED.strip() in out
    argv_line = next(line for line in out.splitlines() if line.startswith("docker run"))
    argv = shlex.split(argv_line)
    assert "--env-file" in argv and _env_path(argv).name == "env"
    assert SECRET not in out
    assert "GEMINI_API_KEY=" not in out  # the scrub list names it, unvalued


def test_docker_argv_structure(launch, tmp_path):
    from pathlib import Path

    result = launch("--image", "agents:latest", "--", "aider", "--model", "gemini-2.5-pro")
    argv = result.argv
    assert argv[:4] == ["docker", "run", "--rm", "-i"]
    assert argv[argv.index("--add-host") + 1] == "host.docker.internal:host-gateway"

    mounts = _flag(argv, "-v")
    launch_dir = _env_path(argv).parent
    assert f"{launch_dir}:/run/ma:ro" in mounts
    assert f"{Path.cwd()}:/workspace" in mounts
    assert f"{tmp_path / 'state-home' / 'multiagent' / 'usage'}:/var/ma-usage" in mounts
    assert result.config_text == RENDERED  # mounted read-only from the launch dir

    assert argv[argv.index("-w") + 1] == "/workspace"
    image = argv.index("agents:latest")
    assert argv[image - 1] == "/workspace"  # the image ends the docker options
    assert argv[image + 1:] == ["aider", "--model", "gemini-2.5-pro"]


def test_agent_command_defaults_to_bash(launch):
    result = launch()
    assert result.argv[-2:] == ["multiagent", "bash"]


def test_project_without_credentials_still_launches(launch):
    result = launch(project="local")
    assert result.code == 7
    assert result.env_text.splitlines()[0].startswith("LITELLM_MASTER_KEY=")
    assert len(result.env_text.splitlines()) == 1
    assert "MA_SCRUB=" in result.argv
    assert result.rendered["key_env"] == {}
