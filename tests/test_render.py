import pytest

from multiagent.render import render_config, to_yaml
from multiagent.types import Backend, ConfigError, MergedModel

OLLAMA = Backend(name="home-ollama", type="ollama", api_base="http://localhost:11434")
COMPAT = Backend(name="home-ollama-openai", type="openai-compat", api_base="http://localhost:11434")
GEMINI = Backend(name="gemini", type="gemini", credential="gemini-api-key")
ANTHROPIC = Backend(name="anthropic", type="anthropic", credential="anthropic-api-key")
BEDROCK = Backend(
    name="aws-gov", type="bedrock", credential="aws-gov-keys", region="us-gov-west-1"
)

KEY_ENV = {"gemini": "GEMINI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}


def merged(backend, served_id, canonical=None, **facts):
    return MergedModel(backend.name, served_id, canonical, facts)


def one(backend, model, key_env=KEY_ENV):
    config = render_config({backend.name: backend}, [model], key_env)
    assert len(config["model_list"]) == 1
    return config["model_list"][0]


# --- litellm_params per backend type -------------------------------------


def test_ollama_params():
    entry = one(OLLAMA, merged(OLLAMA, "qwen2.5:14b", "qwen2.5-14b"))
    assert entry["litellm_params"] == {
        "model": "ollama_chat/qwen2.5:14b",
        "api_base": "http://localhost:11434",
    }


def test_ollama_embedding_uses_plain_prefix():
    entry = one(OLLAMA, merged(OLLAMA, "nomic-embed-text", mode="embedding"))
    assert entry["litellm_params"]["model"] == "ollama/nomic-embed-text"


def test_openai_compat_params():
    entry = one(COMPAT, merged(COMPAT, "qwen2.5:14b"))
    assert entry["litellm_params"] == {
        "model": "openai/qwen2.5:14b",
        "api_base": "http://localhost:11434/v1",
        "api_key": "dummy",
    }


def test_openai_compat_v1_appended_exactly_once():
    for given in ("http://h:8000/v1", "http://h:8000/v1/", "http://h:8000", "http://h:8000/"):
        backend = Backend(name="vllm", type="openai-compat", api_base=given)
        entry = one(backend, merged(backend, "m"))
        assert entry["litellm_params"]["api_base"] == "http://h:8000/v1"


def test_openai_compat_with_credential_uses_env_reference():
    backend = Backend(
        name="work-vllm", type="openai-compat", api_base="http://h:8000", credential="work-key"
    )
    entry = one(backend, merged(backend, "m"), {"work-vllm": "WORK_VLLM_API_KEY"})
    assert entry["litellm_params"]["api_key"] == "os.environ/WORK_VLLM_API_KEY"


def test_gemini_params():
    entry = one(GEMINI, merged(GEMINI, "gemini-2.5-pro", "gemini-2.5-pro"))
    assert entry["litellm_params"] == {
        "model": "gemini/gemini-2.5-pro",
        "api_key": "os.environ/GEMINI_API_KEY",
    }


def test_anthropic_params():
    entry = one(ANTHROPIC, merged(ANTHROPIC, "claude-opus-5", "claude-opus-5"))
    assert entry["litellm_params"] == {
        "model": "anthropic/claude-opus-5",
        "api_key": "os.environ/ANTHROPIC_API_KEY",
    }


def test_bedrock_params():
    entry = one(BEDROCK, merged(BEDROCK, "us.anthropic.claude-sonnet-5-v1:0", "claude-sonnet-5"))
    assert entry["litellm_params"] == {
        "model": "bedrock/us.anthropic.claude-sonnet-5-v1:0",
        "aws_region_name": "us-gov-west-1",
    }


def test_bedrock_without_region_raises():
    backend = Backend(name="aws-com", type="bedrock", credential="aws-com")
    with pytest.raises(ConfigError) as exc:
        one(backend, merged(backend, "us.anthropic.claude-sonnet-5-v1:0"))
    assert "aws-com" in str(exc.value) and "region" in str(exc.value)


def test_bedrock_carries_no_api_key_and_ignores_key_env():
    # boto3 in the proxy reads AWS_* from the env file, so the credential name
    # never becomes a key reference — rendering works with no key_env at all.
    entry = one(BEDROCK, merged(BEDROCK, "us.anthropic.claude-sonnet-5-v1:0"), key_env={})
    assert "api_key" not in entry["litellm_params"]
    assert entry["litellm_params"]["aws_region_name"] == "us-gov-west-1"


def test_unknown_backend_type_raises():
    backend = Backend(name="mystery", type="carrier-pigeon")
    with pytest.raises(ConfigError) as exc:
        one(backend, merged(backend, "claude-sonnet-5"))
    assert "mystery" in str(exc.value) and "carrier-pigeon" in str(exc.value)


def test_missing_env_var_for_credentialed_backend_raises():
    with pytest.raises(ConfigError) as exc:
        one(GEMINI, merged(GEMINI, "gemini-2.5-pro"), key_env={})
    assert "gemini" in str(exc.value)


# --- model_name and model_info -------------------------------------------


def test_canonical_falls_back_to_served_id():
    assert one(OLLAMA, merged(OLLAMA, "qwen2.5:14b"))["model_name"] == "qwen2.5:14b"
    entry = one(OLLAMA, merged(OLLAMA, "qwen2.5:14b", "qwen2.5-14b"))
    assert entry["model_name"] == "qwen2.5-14b"


def test_model_info_full():
    model = merged(
        GEMINI,
        "gemini-2.5-pro",
        "gemini-2.5-pro",
        context=1048576,
        max_output=65536,
        tools=True,
        vision=True,
        mode="chat",
        input_per_mtok=1.25,
        output_per_mtok=10.0,
        tokenizer="sentencepiece",
    )
    assert one(GEMINI, model)["model_info"] == {
        "max_input_tokens": 1048576,
        "max_output_tokens": 65536,
        "supports_function_calling": True,
        "supports_vision": True,
        "mode": "chat",
        "input_cost_per_token": 1.25e-06,
        "output_cost_per_token": 1e-05,
    }


def test_model_info_omits_absent_facts():
    entry = one(OLLAMA, merged(OLLAMA, "qwen2.5:14b", context=131072))
    assert entry["model_info"] == {"max_input_tokens": 131072}


def test_duplicate_model_names_are_kept():
    backends = {OLLAMA.name: OLLAMA, GEMINI.name: GEMINI}
    models = [
        merged(OLLAMA, "qwen2.5:14b", "qwen2.5-14b"),
        merged(GEMINI, "gemini-2.5-pro", "qwen2.5-14b"),
    ]
    names = [e["model_name"] for e in render_config(backends, models, KEY_ENV)["model_list"]]
    assert names == ["qwen2.5-14b", "qwen2.5-14b"]


# --- global sections ------------------------------------------------------


def test_settings_sections():
    config = render_config({}, [], {})
    assert config["general_settings"] == {"master_key": "os.environ/LITELLM_MASTER_KEY"}
    assert config["litellm_settings"] == {
        "drop_params": True,
        "callbacks": "custom_callbacks.proxy_handler_instance",
    }


# --- YAML output ----------------------------------------------------------


def test_to_yaml_is_deterministic_and_parses():
    import yaml

    config = render_config({GEMINI.name: GEMINI}, [merged(GEMINI, "gemini-2.5-pro")], KEY_ENV)
    text = to_yaml(config)
    assert text == to_yaml(config)
    assert yaml.safe_load(text) == config


def test_no_secret_ever_reaches_the_rendered_yaml():
    secret = "sk-ant-DO-NOT-RENDER-0123456789"  # noqa: S105 - fake value, test fixture
    # What the launcher holds: names -> values. Only the names may cross over.
    resolved = {"GEMINI_API_KEY": secret, "ANTHROPIC_API_KEY": secret}
    key_env = {"gemini": "GEMINI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}
    assert all(var in resolved for var in key_env.values())

    backends = {b.name: b for b in (GEMINI, ANTHROPIC, COMPAT, BEDROCK)}
    models = [
        merged(GEMINI, "gemini-2.5-pro", "gemini-2.5-pro"),
        merged(ANTHROPIC, "claude-opus-5", "claude-opus-5"),
        merged(COMPAT, "qwen2.5:14b"),
        merged(BEDROCK, "us.anthropic.claude-sonnet-5-v1:0", "claude-sonnet-5"),
    ]
    text = to_yaml(render_config(backends, models, key_env))
    assert secret not in text
    assert "os.environ/GEMINI_API_KEY" in text
    assert "os.environ/ANTHROPIC_API_KEY" in text
    assert "os.environ/LITELLM_MASTER_KEY" in text
