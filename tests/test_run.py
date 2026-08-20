"""What `ma run` hands the container, and what it must never hand it.

render.py is stubbed here on purpose: these tests are about the launch — the
env file, the argv, the mounts — not about proxy config content.
"""
import json
import shlex
from pathlib import Path

import pytest

from multiagent import run
from multiagent.types import Backend, ObservedModel, ProbeResult

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
  cloud-only:
    backends: [cloud]
  fussy:
    backends: [box]
    default_model: qwen2.5-14b
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
NAMESPACED_KEY = "MA_CLOUD_GEMINI_API_KEY"


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
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    for name in run.PROXY_VARS:
        monkeypatch.delenv(name, raising=False)
    probes = {
        "box": ProbeResult("live", [ObservedModel("qwen2.5:14b", {})]),
        "cloud": ProbeResult("static", [ObservedModel("gemini-2.5-pro", {})]),
    }
    seen = {}

    def fake_probe(backend, api_key=None):
        seen.setdefault("api_keys", {})[backend.name] = api_key
        return probes[backend.name]

    monkeypatch.setattr(run, "probe_backend", fake_probe)

    def fake_render(backends, merged, key_env):
        seen["backends"] = backends
        seen["merged"] = merged
        seen["key_env"] = key_env
        return {"model_list": []}

    monkeypatch.setattr(run, "render_config", fake_render)
    monkeypatch.setattr(run, "to_yaml", lambda config: RENDERED)

    class Launch:
        code = None
        argv = None
        env_text = None
        env_mode = None
        config_text = None
        called = False
        leaks = None
        rendered = seen

    def fake_docker(argv, *args, **kwargs):
        Launch.called = True
        Launch.argv = list(argv)
        env_path = _env_path(argv)
        Launch.env_text = env_path.read_text()
        Launch.env_mode = env_path.stat().st_mode & 0o777
        Launch.config_text = (env_path.parent / "config.yaml").read_text()
        # Evaluated now: the launch directory is gone by the time a test looks.
        Launch.leaks = [
            mount for mount in _flag(argv, "-v")
            if _reaches(Path(mount.split(":")[0]), env_path)
        ]
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
    return Path(argv[argv.index("--env-file") + 1])


def _flag(argv, flag):
    """Every value given to a repeated flag, e.g. every -v."""
    return [argv[i + 1] for i, a in enumerate(argv) if a == flag]


def _reaches(source: Path, target: Path) -> bool:
    """Would mounting `source` put `target` inside the container?"""
    return source == target or (source.is_dir() and target.is_relative_to(source))


# --- for_container, as a pure function ------------------------------------


@pytest.mark.parametrize(
    "given,expected",
    [
        ("http://localhost:11434", "http://host.docker.internal:11434"),
        ("http://127.0.0.1:8000/v1", "http://host.docker.internal:8000/v1"),
        ("http://localhost", "http://host.docker.internal"),
        ("http://vllm.internal:8000", "http://vllm.internal:8000"),
        (None, None),
    ],
)
def test_for_container_rewrites_only_loopback(given, expected):
    backend = Backend(name="b", type="openai-compat", api_base=given)
    assert run.for_container(backend).api_base == expected


def test_for_container_copies_rather_than_mutates():
    backend = Backend(name="b", type="ollama", api_base="http://localhost:11434")
    rewritten = run.for_container(backend)
    assert rewritten is not backend
    assert backend.api_base == "http://localhost:11434"


def test_namespaced_folds_punctuation_out_of_the_backend_name():
    assert run.namespaced("bedrock-projA.gov", "AWS_ACCESS_KEY_ID") == (
        "MA_BEDROCK_PROJA_GOV_AWS_ACCESS_KEY_ID"
    )


# --- the env file ---------------------------------------------------------


def test_env_file_carries_namespaced_credentials_and_a_master_key(launch):
    result = launch()
    assert result.code == 7  # docker's exit status is the launch's
    assert result.env_mode == 0o600
    lines = result.env_text.splitlines()
    assert f"{NAMESPACED_KEY}={SECRET}" in lines
    assert "MA_CLOUD_GEMINI_PROJECT=demo" in lines
    assert not any(line.startswith("GEMINI_") for line in lines)  # never the bare name
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
    assert f"{NAMESPACED_KEY}=" not in joined  # the name may appear, never a value


def test_scrub_names_exactly_the_namespaced_vars(launch):
    result = launch()
    scrub = next(a for a in result.argv if a.startswith("MA_SCRUB="))
    assert scrub == f"MA_SCRUB={NAMESPACED_KEY},MA_CLOUD_GEMINI_PROJECT"
    assert "MA_PROJECT=all-in" in result.argv


def test_key_env_maps_original_names_to_namespaced_ones(launch):
    result = launch()
    assert result.rendered["key_env"] == {
        "cloud": {
            "GEMINI_API_KEY": NAMESPACED_KEY,
            "GEMINI_PROJECT": "MA_CLOUD_GEMINI_PROJECT",
        }
    }


def test_backends_are_rewritten_for_the_container(launch):
    result = launch()
    rendered = result.rendered["backends"]
    assert rendered["box"].api_base == "http://host.docker.internal:11434"
    assert rendered["cloud"].api_base is None


# --- credentials ----------------------------------------------------------


def test_missing_credential_warns_and_drops_the_backend(launch, capsys, tmp_path):
    result = launch(cred=False)
    err = capsys.readouterr().err
    assert result.code == 7  # box still serves models, so the launch proceeds
    assert "WARNING" in err and "cloud" in err
    assert str(tmp_path / "creds" / "cloud-key.env") in err
    assert result.rendered["key_env"] == {}
    assert "cloud" not in result.rendered["backends"]
    assert result.env_text.splitlines() == [
        next(line for line in result.env_text.splitlines()
             if line.startswith("LITELLM_MASTER_KEY="))
    ]


def test_no_models_left_is_a_hard_failure(launch, capsys):
    result = launch(project="cloud-only", cred=False)
    err = capsys.readouterr().err
    assert result.code == 2
    assert result.called is False
    assert "cloud-only" in err and "no models" in err


def test_stale_credential_warns_but_launches(launch, capsys):
    result = launch(expires="2020-01-01T00:00:00Z")
    err = capsys.readouterr().err
    assert result.code == 7
    assert "cloud-key" in err and "ma keys cloud-key" in err
    # Warned, not withheld: the expired values still reach the proxy.
    assert f"{NAMESPACED_KEY}={SECRET}" in result.env_text.splitlines()


def test_down_backend_warns_but_launches(launch, monkeypatch, capsys):
    down = ProbeResult("down", error="URLError: connection refused")
    monkeypatch.setattr(
        run, "probe_backend",
        lambda backend, api_key=None: (
            down if backend.name == "box"
            else ProbeResult("static", [ObservedModel("gemini-2.5-pro", {})])
        ),
    )
    result = launch()
    err = capsys.readouterr().err
    assert result.code == 7
    assert "box" in err and "connection refused" in err


def test_probe_receives_the_credential_as_a_bearer_token(launch, monkeypatch, config_dir):
    # box is uncredentialed; give a dynamic backend a credential and check.
    config_dir.joinpath("backends.yaml").write_text(
        BACKENDS.replace(
            "    discovery: dynamic\n  cloud:",
            "    discovery: dynamic\n    credential: cloud-key\n  cloud:",
        )
    )
    result = launch()
    assert result.rendered["api_keys"] == {"box": SECRET, "cloud": None}


def test_uncredentialed_backend_probes_anonymously(launch):
    result = launch(project="local")
    assert result.rendered["api_keys"] == {"box": None}


# --- the docker command line ---------------------------------------------


def test_dry_run_prints_the_plan_and_writes_no_env_file(launch, capsys):
    result = launch("--dry-run")
    out = capsys.readouterr().out
    assert result.code == 0
    assert result.called is False
    assert RENDERED.strip() in out
    argv_line = next(line for line in out.splitlines() if line.startswith("docker run"))
    argv = shlex.split(argv_line)
    env_path = _env_path(argv)
    assert env_path.name == "env"
    assert not env_path.exists()  # named, never written
    assert env_path.parent.joinpath("config.yaml").exists() is False  # cleaned up after
    assert "dry run" in out
    assert SECRET not in out


def test_docker_argv_structure(launch, tmp_path):
    result = launch("--image", "agents:latest", "--", "aider", "--model", "gemini-2.5-pro")
    argv = result.argv
    assert argv[:4] == ["docker", "run", "--rm", "-i"]
    assert argv[argv.index("--add-host") + 1] == "host.docker.internal:host-gateway"

    mounts = _flag(argv, "-v")
    launch_dir = _env_path(argv).parent
    assert f"{launch_dir / 'config.yaml'}:/run/ma/config.yaml:ro" in mounts
    assert f"{Path.cwd()}:/workspace" in mounts
    assert result.config_text == RENDERED

    assert argv[argv.index("-w") + 1] == "/workspace"
    image = argv.index("agents:latest")
    assert argv[image - 1] == "/workspace"  # the image ends the docker options
    assert argv[image + 1:] == ["aider", "--model", "gemini-2.5-pro"]


def test_the_env_file_is_never_inside_a_mount(launch):
    # `cat /run/ma/env` must find nothing: mounting the launch directory would
    # hand the agent every secret in the project.
    result = launch()
    launch_dir = _env_path(result.argv).parent
    assert result.leaks == []
    # …while the config from the same directory is mounted, file by name.
    assert f"{launch_dir / 'config.yaml'}:/run/ma/config.yaml:ro" in _flag(result.argv, "-v")


def test_container_is_unprivileged_and_owns_nothing(launch):
    import os

    argv = launch().argv
    assert argv[argv.index("--user") + 1] == f"{os.getuid()}:{os.getgid()}"
    assert "--cap-drop=ALL" in argv
    assert "HOME=/tmp" in argv  # litellm caches there; /root is closed to our uid


def test_usage_ledger_is_per_project(launch, tmp_path):
    result = launch(project="local")
    usage = tmp_path / "state-home" / "multiagent" / "usage" / "local"
    assert f"{usage}:/var/ma-usage" in _flag(result.argv, "-v")
    assert usage.is_dir()


def test_engine_is_selectable(launch, monkeypatch):
    assert launch("--engine", "podman").argv[0] == "podman"
    monkeypatch.setenv("MA_CONTAINER_ENGINE", "podman")
    assert launch().argv[0] == "podman"


def test_proxy_variables_are_forwarded(launch, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.corp:3128")
    monkeypatch.setenv("no_proxy", "localhost,.corp")
    argv = launch().argv
    assert "HTTPS_PROXY=http://proxy.corp:3128" in argv
    assert "no_proxy=localhost,.corp" in argv
    assert not any(a.startswith("HTTP_PROXY=") for a in argv)  # unset stays unset


def test_ca_bundle_is_mounted_and_named_to_every_client(launch, tmp_path):
    bundle = tmp_path / "corp-ca.pem"
    bundle.write_text("-----BEGIN CERTIFICATE-----\n")
    argv = launch("--ca-bundle", str(bundle)).argv
    assert f"{bundle}:/run/ma/ca.pem:ro" in _flag(argv, "-v")
    for name in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "AWS_CA_BUNDLE"):
        assert f"{name}=/run/ma/ca.pem" in argv


def test_default_model_reaches_the_agent(launch):
    assert "MA_DEFAULT_MODEL=qwen2.5-14b" in launch(project="fussy").argv
    assert not any(a.startswith("MA_DEFAULT_MODEL=") for a in launch(project="local").argv)


def test_launch_dir_lives_under_the_runtime_dir_when_there_is_one(launch, monkeypatch, tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    result = launch()
    root = runtime / "multiagent"
    assert _env_path(result.argv).parent.parent == root
    assert root.stat().st_mode & 0o777 == 0o700


def test_change_notes_are_printed_before_the_launch(launch, capsys):
    launch(project="local")
    assert "change box: first observation" in capsys.readouterr().err


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
