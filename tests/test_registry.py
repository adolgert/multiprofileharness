import json

import pytest

from multiagent.registry import apply_machine, load_config
from multiagent.types import ConfigError

BACKENDS = """
backends:
  home-ollama:
    type: ollama
    api_base: http://localhost:11434
    discovery: dynamic
  gemini:
    type: gemini
    credential: gemini-api-key
    models: [gemini-2.5-pro, gemini-2.5-flash]
"""

PROJECTS = """
projects:
  home:
    backends: [home-ollama, gemini]
    default_model: qwen2.5-14b
  paper-review:
    backends: [home-ollama]
    model_filter: [qwen2.5-14b]
"""

MODELS = """
models:
  qwen2.5-14b:
    match: ["qwen2.5:14b", "Qwen/Qwen2.5-14B*"]
    context: 131072
    tools: true
  gemini-2.5-pro:
    match: ["gemini-2.5-pro*"]
    catalog_key: gemini/gemini-2.5-pro

deployments:
  home-ollama:
    "qwen2.5:14b":
      max_output: 4096
"""


def write(tmp_path, backends=BACKENDS, projects=PROJECTS, models=MODELS, catalog=None):
    d = tmp_path / "config"
    d.mkdir(exist_ok=True)
    if backends is not None:
        (d / "backends.yaml").write_text(backends)
    if projects is not None:
        (d / "projects.yaml").write_text(projects)
    if models is not None:
        (d / "models.yaml").write_text(models)
    if catalog is not None:
        (d / "catalog.json").write_text(json.dumps(catalog))
    return d


def test_happy_path(tmp_path):
    cfg = load_config(write(tmp_path, catalog={"gemini/gemini-2.5-pro": {"max_input_tokens": 1}}))

    assert sorted(cfg.backends) == ["gemini", "home-ollama"]
    ollama = cfg.backends["home-ollama"]
    assert (ollama.name, ollama.type, ollama.discovery) == ("home-ollama", "ollama", "dynamic")
    assert ollama.api_base == "http://localhost:11434"
    assert ollama.credential is None and ollama.models == []

    gemini = cfg.backends["gemini"]
    assert gemini.credential == "gemini-api-key"
    assert gemini.models == ["gemini-2.5-pro", "gemini-2.5-flash"]
    assert gemini.discovery == "static"  # default

    home = cfg.projects["home"]
    assert home.backends == ["home-ollama", "gemini"]
    assert home.default_model == "qwen2.5-14b"
    assert home.model_filter is None
    assert cfg.projects["paper-review"].model_filter == ["qwen2.5-14b"]

    entry = cfg.knowledge.models["qwen2.5-14b"]
    assert entry.facts == {"context": 131072, "tools": True}
    assert entry.catalog_key is None
    assert cfg.knowledge.models["gemini-2.5-pro"].catalog_key == "gemini/gemini-2.5-pro"
    assert cfg.knowledge.deployments == {"home-ollama": {"qwen2.5:14b": {"max_output": 4096}}}
    assert cfg.catalog["gemini/gemini-2.5-pro"]["max_input_tokens"] == 1


def test_canonical_for_matches_patterns_from_loaded_file(tmp_path):
    knowledge = load_config(write(tmp_path)).knowledge
    assert knowledge.canonical_for("qwen2.5:14b") == "qwen2.5-14b"
    assert knowledge.canonical_for("Qwen/Qwen2.5-14B-Instruct") == "qwen2.5-14b"
    assert knowledge.canonical_for("gemini-2.5-pro-preview") == "gemini-2.5-pro"
    assert knowledge.canonical_for("qwen2.5:7b") is None


def test_a_specific_entry_below_a_glob_is_still_reachable(tmp_path):
    # The glob is listed first, as it would be after someone appends the
    # specific entry to the end of the file.
    models = (
        'models:\n'
        '  llava:\n    match: ["llava:*"]\n'
        '  llava-34b:\n    match: ["llava:34b"]\n'
    )
    unfiltered = "projects:\n  home:\n    backends: [home-ollama]\n"
    knowledge = load_config(write(tmp_path, projects=unfiltered, models=models)).knowledge
    assert knowledge.canonical_for("llava:13b") == "llava"
    assert knowledge.canonical_for("llava:34b") == "llava-34b"


def test_catalog_missing_is_empty(tmp_path):
    assert load_config(write(tmp_path)).catalog == {}


def test_models_yaml_optional(tmp_path):
    # No beliefs recorded yet, so nothing may filter on a canonical name either.
    unfiltered = "projects:\n  home:\n    backends: [home-ollama, gemini]\n"
    cfg = load_config(write(tmp_path, projects=unfiltered, models=None))
    assert cfg.knowledge.models == {} and cfg.knowledge.deployments == {}


def test_unknown_backend_key(tmp_path):
    bad = "backends:\n  b:\n    type: ollama\n    api_bass: http://x\n"
    with pytest.raises(ConfigError) as e:
        load_config(write(tmp_path, backends=bad, projects="projects: {}\n"))
    assert "api_bass" in str(e.value) and "'b'" in str(e.value)
    assert "backends.yaml" in str(e.value)


def test_unknown_project_key(tmp_path):
    bad = "projects:\n  p:\n    backends: [home-ollama]\n    defualt_model: x\n"
    with pytest.raises(ConfigError) as e:
        load_config(write(tmp_path, projects=bad))
    assert "defualt_model" in str(e.value) and "projects.yaml" in str(e.value)


def test_unknown_model_key(tmp_path):
    bad = 'models:\n  m:\n    match: ["m*"]\n    contxt: 100\n'
    with pytest.raises(ConfigError) as e:
        load_config(write(tmp_path, models=bad))
    assert "contxt" in str(e.value) and "models.yaml" in str(e.value)


def test_unknown_deployment_fact_key(tmp_path):
    bad = 'deployments:\n  home-ollama:\n    "q:14b":\n      max_ouput: 10\n'
    with pytest.raises(ConfigError) as e:
        load_config(write(tmp_path, models=bad))
    assert "max_ouput" in str(e.value) and "models.yaml" in str(e.value)


def test_dangling_project_backend_reference(tmp_path):
    bad = "projects:\n  home:\n    backends: [home-ollama, work-vllm]\n"
    with pytest.raises(ConfigError) as e:
        load_config(write(tmp_path, projects=bad))
    msg = str(e.value)
    assert "'home'" in msg and "work-vllm" in msg and "projects.yaml" in msg


def test_missing_backends_file(tmp_path):
    d = write(tmp_path, backends=None)
    with pytest.raises(ConfigError) as e:
        load_config(d)
    assert str(d / "backends.yaml") in str(e.value)


def test_missing_projects_file(tmp_path):
    d = write(tmp_path, projects=None)
    with pytest.raises(ConfigError) as e:
        load_config(d)
    assert str(d / "projects.yaml") in str(e.value)


def test_backend_missing_type(tmp_path):
    with pytest.raises(ConfigError) as e:
        load_config(write(tmp_path, backends="backends:\n  b:\n    api_base: http://x\n",
                          projects="projects: {}\n"))
    assert "type" in str(e.value)


def test_model_filter_names_an_unknown_model(tmp_path):
    bad = "projects:\n  p:\n    backends: [home-ollama]\n    model_filter: [qwen2.5-14bb]\n"
    with pytest.raises(ConfigError) as e:
        load_config(write(tmp_path, projects=bad))
    msg = str(e.value)
    assert "qwen2.5-14bb" in msg and "'p'" in msg
    assert "projects.yaml" in msg and "models.yaml" in msg
    assert "qwen2.5-14b" in msg  # the known names, so the typo is visible


def test_dynamic_discovery_on_a_type_that_cannot_be_probed(tmp_path):
    bad = "backends:\n  vendor:\n    type: anthropic\n    discovery: dynamic\n"
    with pytest.raises(ConfigError) as e:
        load_config(write(tmp_path, backends=bad, projects="projects: {}\n", models=None))
    msg = str(e.value)
    assert "vendor" in msg and "anthropic" in msg and "ollama" in msg


def test_backend_extra_is_carried_through(tmp_path):
    text = (
        "backends:\n  work:\n    type: openai-compat\n"
        "    api_base: https://mantle.example.com\n"
        "    extra:\n      endpoint_url: https://mantle.example.com/inference\n"
    )
    cfg = load_config(write(tmp_path, backends=text, projects="projects: {}\n", models=None))
    assert cfg.backends["work"].extra == {"endpoint_url": "https://mantle.example.com/inference"}
    assert load_config(write(tmp_path)).backends["gemini"].extra == {}


# --- credentials over cleartext -------------------------------------------


def credentialed(api_base):
    return f"backends:\n  work:\n    type: openai-compat\n    credential: work-key\n" + (
        f"    api_base: {api_base}\n" if api_base else ""
    )


@pytest.mark.parametrize(
    "api_base",
    [
        "https://vllm.example.com:8000",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://[::1]:8000",
        "http://10.1.2.3:8000",
        "http://192.168.1.7:8000",
        "http://host.docker.internal:8000",
        "http://gpu01:8000",  # a dotless name is a LAN name
        None,  # address deferred to machine.yaml
    ],
)
def test_credential_over_a_local_or_encrypted_address_is_fine(tmp_path, api_base):
    cfg = load_config(
        write(tmp_path, backends=credentialed(api_base), projects="projects: {}\n", models=None)
    )
    assert cfg.backends["work"].credential == "work-key"


def test_credential_over_cleartext_to_a_public_host_is_rejected(tmp_path):
    with pytest.raises(ConfigError) as e:
        load_config(
            write(
                tmp_path,
                backends=credentialed("http://vllm.example.com:8000"),
                projects="projects: {}\n",
                models=None,
            )
        )
    msg = str(e.value)
    assert "work" in msg and "work-key" in msg
    assert "cleartext" in msg and "https" in msg
    assert "backends.yaml" in msg


def test_backend_without_a_credential_may_use_plain_http(tmp_path):
    text = "backends:\n  peer:\n    type: openai-compat\n    api_base: http://peer.example.com\n"
    cfg = load_config(write(tmp_path, backends=text, projects="projects: {}\n", models=None))
    assert cfg.backends["peer"].api_base == "http://peer.example.com"


def test_machine_override_may_not_downgrade_a_credentialed_backend(tmp_path):
    cfg = load_config(
        write(
            tmp_path,
            backends=credentialed("https://vllm.example.com:8000"),
            projects="projects: {}\n",
            models=None,
        )
    )
    path = tmp_path / "machine.yaml"
    path.write_text("overrides:\n  work:\n    api_base: http://vllm.example.com:8000\n")
    with pytest.raises(ConfigError) as e:
        apply_machine(cfg, path)
    assert "cleartext" in str(e.value) and "machine.yaml" in str(e.value)


def test_machine_override_to_a_local_address_is_fine(tmp_path):
    cfg = load_config(
        write(
            tmp_path,
            backends=credentialed("https://vllm.example.com:8000"),
            projects="projects: {}\n",
            models=None,
        )
    )
    path = tmp_path / "machine.yaml"
    path.write_text("overrides:\n  work:\n    api_base: http://localhost:8000\n")
    assert apply_machine(cfg, path).backends["work"].api_base == "http://localhost:8000"


def test_machine_override_applied(tmp_path):
    cfg = load_config(write(tmp_path))
    path = tmp_path / "machine.yaml"
    path.write_text("overrides:\n  home-ollama:\n    api_base: http://nuc:11434\n")
    out = apply_machine(cfg, path)
    assert out.backends["home-ollama"].api_base == "http://nuc:11434"
    assert out.backends["gemini"].api_base is None


def test_machine_missing_is_noop(tmp_path):
    cfg = load_config(write(tmp_path))
    apply_machine(cfg, tmp_path / "nope.yaml")
    assert cfg.backends["home-ollama"].api_base == "http://localhost:11434"


def test_machine_unknown_backend(tmp_path):
    cfg = load_config(write(tmp_path))
    path = tmp_path / "machine.yaml"
    path.write_text("overrides:\n  work-vllm:\n    api_base: http://x\n")
    with pytest.raises(ConfigError) as e:
        apply_machine(cfg, path)
    assert "work-vllm" in str(e.value) and "machine.yaml" in str(e.value)


def test_machine_rejects_non_api_base(tmp_path):
    cfg = load_config(write(tmp_path))
    path = tmp_path / "machine.yaml"
    path.write_text("overrides:\n  gemini:\n    credential: other-key\n")
    with pytest.raises(ConfigError) as e:
        apply_machine(cfg, path)
    assert "credential" in str(e.value) and "api_base" in str(e.value)
