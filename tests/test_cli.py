import json
from pathlib import Path

import pytest

from multiagent import cli, run
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
    model_filter: [qwen2.5-14b]
"""

MODELS = """
models:
  qwen2.5-14b:
    match: ["qwen2.5:14b"]
    context: 131072
    tools: true
  llava:
    match: ["llava:*"]
    tools: false
    vision: true
  gemini-2.5-pro:
    match: ["gemini-2.5-pro*"]
    catalog_key: gemini/gemini-2.5-pro
"""

CATALOG = {
    "gemini/gemini-2.5-pro": {
        "max_input_tokens": 1048576,
        "supports_function_calling": True,
        "input_cost_per_token": 1.25e-06,
        "output_cost_per_token": 1e-05,
    }
}


@pytest.fixture
def config_dir(tmp_path):
    d = tmp_path / "config"
    d.mkdir()
    (d / "backends.yaml").write_text(BACKENDS)
    (d / "projects.yaml").write_text(PROJECTS)
    (d / "models.yaml").write_text(MODELS)
    (d / "catalog.json").write_text(json.dumps(CATALOG))
    return d


def stub_probe(results, calls):
    """Stand in for probe_backend, recording which backends were asked."""

    def probe(backend, api_key=None):
        calls.append(backend.name)
        return results[backend.name]

    return probe


def models(monkeypatch, tmp_path, config_dir, results, *extra, cred=True, expires=None):
    calls = []
    monkeypatch.setattr(run, "probe_backend", stub_probe(results, calls))
    cred_dir = tmp_path / "creds"
    cred_dir.mkdir(exist_ok=True)
    if cred:
        comment = f"# expires: {expires}\n" if expires else ""
        (cred_dir / "cloud-key.env").write_text(comment + "GEMINI_API_KEY=x\n")
    argv = [
        "models",
        "--config", str(config_dir),
        "--machine", str(tmp_path / "machine.yaml"),
        "--state", str(tmp_path / "state.json"),
        "--cred-dir", str(cred_dir),
        *extra,
    ]
    return cli.main(argv), calls


def live(*models):
    return ProbeResult("live", [ObservedModel(i, f) for i, f in models])


STATIC_CLOUD = ProbeResult("static", [ObservedModel("gemini-2.5-pro", {})])


def test_lists_live_models_with_facts(monkeypatch, tmp_path, config_dir, capsys):
    results = {"box": live(("qwen2.5:14b", {})), "cloud": STATIC_CLOUD}
    code, calls = models(monkeypatch, tmp_path, config_dir, results)
    out = capsys.readouterr().out
    assert code == 0
    assert calls == ["box", "cloud"]
    header, *lines = out.splitlines()
    assert header.split() == list(cli.COLUMNS)
    qwen = next(line for line in lines if "qwen2.5-14b" in line)
    assert "box" in qwen and "(none)" in qwen and "live" in qwen
    assert "131072" in qwen and "yes" in qwen
    gemini = next(line for line in lines if "gemini-2.5-pro" in line)
    # `listed`, not `ok`: nobody checked whether that account works.
    assert "cloud-key" in gemini and " listed " in gemini
    assert "$1.25/$10 per Mtok" in gemini


def test_missing_credential_is_not_probed(monkeypatch, tmp_path, config_dir, capsys):
    results = {"box": live(("qwen2.5:14b", {}))}
    code, calls = models(monkeypatch, tmp_path, config_dir, results, cred=False)
    out = capsys.readouterr().out
    assert code == 0
    assert calls == ["box"]
    cloud = next(line for line in out.splitlines() if line.startswith("cloud"))
    assert "NO KEY" in cloud and cloud.count("—") == 4


def test_stale_credential_is_flagged_but_still_probed(monkeypatch, tmp_path, config_dir, capsys):
    results = {"box": live(("qwen2.5:14b", {})), "cloud": STATIC_CLOUD}
    code, calls = models(monkeypatch, tmp_path, config_dir, results, expires="2020-01-01T00:00:00Z")
    out = capsys.readouterr().out
    assert code == 0
    assert calls == ["box", "cloud"]  # an expired key still answers; probe it
    gemini = next(line for line in out.splitlines() if "gemini-2.5-pro" in line)
    assert "STALE" in gemini
    assert "1048576" in gemini  # and its models are still listed with facts


def test_conflict_reported_with_observation_winning(monkeypatch, tmp_path, config_dir, capsys):
    results = {"box": live(("qwen2.5:14b", {"context": 32768})), "cloud": STATIC_CLOUD}
    code, _ = models(monkeypatch, tmp_path, config_dir, results)
    out = capsys.readouterr().out
    assert code == 0
    qwen = next(line for line in out.splitlines() if "qwen2.5-14b" in line)
    assert "32768" in qwen and "*" in qwen
    assert (
        "conflict box/qwen2.5:14b context: believed 131072, observed 32768 (observed wins)"
        in out
    )


def test_model_filter_hides_other_canonicals(monkeypatch, tmp_path, config_dir, capsys):
    results = {"box": live(("qwen2.5:14b", {}), ("llava:13b", {}))}
    code, calls = models(monkeypatch, tmp_path, config_dir, results, "--project", "local")
    out = capsys.readouterr().out
    assert code == 0
    assert calls == ["box"]  # the project does not name the cloud backend
    assert "qwen2.5-14b" in out
    assert "llava" not in out


def test_unknown_model_shows_no_facts(monkeypatch, tmp_path, config_dir, capsys):
    results = {"box": live(("mystery:1b", {})), "cloud": STATIC_CLOUD}
    code, _ = models(monkeypatch, tmp_path, config_dir, results)
    out = capsys.readouterr().out
    assert code == 0
    row = next(line for line in out.splitlines() if "mystery:1b" in line)
    assert "no facts" in row


def test_snapshot_records_then_reports_change(monkeypatch, tmp_path, config_dir, capsys):
    first = {"box": live(("qwen2.5:14b", {}))}
    code, _ = models(monkeypatch, tmp_path, config_dir, first, "--project", "local")
    assert code == 0
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["box"]["models"] == ["qwen2.5:14b"]
    capsys.readouterr()

    second = {"box": live(("llava:13b", {}))}
    code, _ = models(monkeypatch, tmp_path, config_dir, second, "--project", "local")
    out = capsys.readouterr().out
    assert code == 0
    assert "new: llava:13b" in out
    assert "gone: qwen2.5:14b" in out


def test_unknown_project_lists_the_known_ones(monkeypatch, tmp_path, config_dir, capsys):
    code, calls = models(monkeypatch, tmp_path, config_dir, {}, "--project", "nope")
    err = capsys.readouterr().err
    assert code == 2
    assert calls == []
    assert "nope" in err and "all-in" in err and "local" in err


def test_down_backend_shows_the_error(monkeypatch, tmp_path, config_dir, capsys):
    results = {
        "box": ProbeResult("down", error="URLError: <urlopen error connection refused>"),
        "cloud": STATIC_CLOUD,
    }
    code, _ = models(monkeypatch, tmp_path, config_dir, results)
    out = capsys.readouterr().out
    assert code == 0
    row = next(line for line in out.splitlines() if line.startswith("box"))
    assert "down" in row and "URLError" in row


def test_stale_deployment_override_is_reported(monkeypatch, tmp_path, config_dir, capsys):
    config_dir.joinpath("models.yaml").write_text(
        MODELS + '\ndeployments:\n  box:\n    "qwen2.5:7b":\n      max_output: 512\n'
    )
    results = {"box": live(("qwen2.5:14b", {})), "cloud": STATIC_CLOUD}
    code, _ = models(monkeypatch, tmp_path, config_dir, results)
    out = capsys.readouterr().out
    assert code == 0
    assert "stale: deployment override for box/qwen2.5:7b matches no served model" in out


# --- finding the config directory ----------------------------------------


@pytest.fixture
def empty_env(monkeypatch, tmp_path):
    """No MA_CONFIG, and a config home and cwd that hold nothing."""
    monkeypatch.delenv("MA_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_explicit_config_wins_over_everything(empty_env, monkeypatch, tmp_path):
    monkeypatch.setenv("MA_CONFIG", str(tmp_path / "from-env"))
    assert cli.resolve_config(tmp_path / "explicit") == tmp_path / "explicit"


def test_ma_config_wins_over_the_config_home(empty_env, monkeypatch, tmp_path):
    (tmp_path / "xdg" / "multiagent" / "config").mkdir(parents=True)
    monkeypatch.setenv("MA_CONFIG", str(tmp_path / "from-env"))
    assert cli.resolve_config(None) == tmp_path / "from-env"


def test_config_home_wins_over_the_workspace(empty_env, tmp_path, capsys):
    home = tmp_path / "xdg" / "multiagent" / "config"
    home.mkdir(parents=True)
    (tmp_path / "config").mkdir()
    assert cli.resolve_config(None) == home
    assert capsys.readouterr().err == ""


def test_workspace_config_is_last_and_says_so(empty_env, tmp_path, capsys):
    (tmp_path / "config").mkdir()
    assert cli.resolve_config(None) == Path("config")
    err = capsys.readouterr().err
    assert "config" in err and "untrusted input" in err


def test_no_config_anywhere_lists_what_was_tried(empty_env, tmp_path, capsys):
    code = cli.main(["models", "--state", str(tmp_path / "state.json")])
    err = capsys.readouterr().err
    assert code == 2
    assert "--config" in err and "MA_CONFIG" in err
    assert str(tmp_path / "xdg" / "multiagent" / "config") in err
    assert "config" in err
