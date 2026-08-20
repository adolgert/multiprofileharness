import pytest

from multiagent.render import render_config, to_yaml
from multiagent.types import Backend, ConfigError, Deployment

OLLAMA = Backend(name="home-ollama", type="ollama", api_base="http://localhost:11434")
COMPAT = Backend(name="home-ollama-openai", type="openai-compat", api_base="http://localhost:11434")
GEMINI = Backend(name="gemini", type="gemini", credential="gemini-api-key")
ANTHROPIC = Backend(name="anthropic", type="anthropic", credential="anthropic-api-key")
BEDROCK = Backend(
    name="aws-gov", type="bedrock", credential="aws-gov-keys", region="us-gov-west-1"
)

# backend -> {the credential file's variable: the name it travels under}.
KEY_ENV = {
    "gemini": {"GEMINI_API_KEY": "MA_GEMINI_GEMINI_API_KEY"},
    "anthropic": {"ANTHROPIC_API_KEY": "MA_ANTHROPIC_ANTHROPIC_API_KEY"},
}
AWS_GOV_ENV = {
    "aws-gov": {
        "AWS_ACCESS_KEY_ID": "MA_AWS_GOV_AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY": "MA_AWS_GOV_AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN": "MA_AWS_GOV_AWS_SESSION_TOKEN",
    }
}


def merged(backend, served_id, canonical=None, **facts):
    return Deployment(backend.name, served_id, canonical, facts)


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


def test_openai_compat_with_credential_uses_namespaced_env_reference():
    backend = Backend(
        name="work-vllm", type="openai-compat", api_base="http://h:8000", credential="work-key"
    )
    key_env = {"work-vllm": {"WORK_API_KEY": "MA_WORK_VLLM_WORK_API_KEY"}}
    entry = one(backend, merged(backend, "m"), key_env)
    assert entry["litellm_params"]["api_key"] == "os.environ/MA_WORK_VLLM_WORK_API_KEY"


def test_ambiguous_credential_names_are_an_error_not_a_guess():
    backend = Backend(name="work-vllm", type="openai-compat", api_base="http://h:8000",
                      credential="work-key")
    key_env = {"work-vllm": {"A_API_KEY": "MA_WORK_VLLM_A_API_KEY",
                             "B_API_KEY": "MA_WORK_VLLM_B_API_KEY"}}
    with pytest.raises(ConfigError) as exc:
        one(backend, merged(backend, "m"), key_env)
    assert "work-key" in str(exc.value)


def test_gemini_params():
    entry = one(GEMINI, merged(GEMINI, "gemini-2.5-pro", "gemini-2.5-pro"))
    assert entry["litellm_params"] == {
        "model": "gemini/gemini-2.5-pro",
        "api_key": "os.environ/MA_GEMINI_GEMINI_API_KEY",
    }


def test_anthropic_params():
    entry = one(ANTHROPIC, merged(ANTHROPIC, "claude-opus-5", "claude-opus-5"))
    assert entry["litellm_params"] == {
        "model": "anthropic/claude-opus-5",
        "api_key": "os.environ/MA_ANTHROPIC_ANTHROPIC_API_KEY",
    }


def test_bedrock_params_name_their_own_credential_variables():
    entry = one(
        BEDROCK, merged(BEDROCK, "us.anthropic.claude-sonnet-5-v1:0", "claude-sonnet-5"),
        AWS_GOV_ENV,
    )
    assert entry["litellm_params"] == {
        "model": "bedrock/us.anthropic.claude-sonnet-5-v1:0",
        "aws_region_name": "us-gov-west-1",
        "aws_access_key_id": "os.environ/MA_AWS_GOV_AWS_ACCESS_KEY_ID",
        "aws_secret_access_key": "os.environ/MA_AWS_GOV_AWS_SECRET_ACCESS_KEY",
        "aws_session_token": "os.environ/MA_AWS_GOV_AWS_SESSION_TOKEN",
    }


def test_bedrock_long_term_keys_omit_the_session_token():
    key_env = {"aws-gov": {k: v for k, v in AWS_GOV_ENV["aws-gov"].items()
                           if k != "AWS_SESSION_TOKEN"}}
    entry = one(BEDROCK, merged(BEDROCK, "us.amazon.nova-lite-v1:0"), key_env)
    assert "aws_session_token" not in entry["litellm_params"]
    assert entry["litellm_params"]["aws_access_key_id"] == "os.environ/MA_AWS_GOV_AWS_ACCESS_KEY_ID"


def test_two_bedrock_accounts_never_share_a_variable():
    # The whole point: AWS_* is process-global, so without per-backend names one
    # account would silently sign the other's requests.
    com = Backend(name="aws-com", type="bedrock", credential="aws-com-keys", region="us-east-1")
    key_env = {
        **AWS_GOV_ENV,
        "aws-com": {
            "AWS_ACCESS_KEY_ID": "MA_AWS_COM_AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY": "MA_AWS_COM_AWS_SECRET_ACCESS_KEY",
        },
    }
    models = [
        merged(BEDROCK, "us-gov.anthropic.claude-sonnet-5", "claude-sonnet-5-gov"),
        merged(com, "us.anthropic.claude-sonnet-5", "claude-sonnet-5"),
    ]
    entries = render_config(
        {BEDROCK.name: BEDROCK, com.name: com}, models, key_env
    )["model_list"]
    gov, commercial = (e["litellm_params"] for e in entries)
    assert gov["aws_access_key_id"] != commercial["aws_access_key_id"]
    assert gov["aws_secret_access_key"] != commercial["aws_secret_access_key"]
    assert gov["aws_region_name"] == "us-gov-west-1"
    assert commercial["aws_region_name"] == "us-east-1"


def test_bedrock_without_region_raises():
    backend = Backend(name="aws-com", type="bedrock", credential="aws-com")
    with pytest.raises(ConfigError) as exc:
        one(backend, merged(backend, "us.anthropic.claude-sonnet-5-v1:0"), {})
    assert "aws-com" in str(exc.value) and "region" in str(exc.value)


def test_bedrock_without_aws_keys_raises_naming_the_credential():
    key_env = {"aws-gov": {"AWS_ACCESS_KEY_ID": "MA_AWS_GOV_AWS_ACCESS_KEY_ID"}}
    with pytest.raises(ConfigError) as exc:
        one(BEDROCK, merged(BEDROCK, "us.amazon.nova-lite-v1:0"), key_env)
    assert "aws-gov-keys" in str(exc.value) and "AWS_SECRET_ACCESS_KEY" in str(exc.value)


def test_unknown_backend_type_raises():
    backend = Backend(name="mystery", type="carrier-pigeon")
    with pytest.raises(ConfigError) as exc:
        one(backend, merged(backend, "claude-sonnet-5"))
    assert "mystery" in str(exc.value) and "carrier-pigeon" in str(exc.value)


def test_missing_env_var_for_credentialed_backend_raises():
    with pytest.raises(ConfigError) as exc:
        one(GEMINI, merged(GEMINI, "gemini-2.5-pro"), key_env={})
    assert "gemini" in str(exc.value)


# --- extra passthrough ----------------------------------------------------


def test_extra_is_merged_last_and_verbatim():
    backend = Backend(
        name="mantle", type="bedrock", credential="mantle-keys", region="us-east-1",
        extra={"endpoint_url": "https://mantle.internal", "aws_region_name": "us-west-2"},
    )
    key_env = {"mantle": {
        "AWS_ACCESS_KEY_ID": "MA_MANTLE_AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY": "MA_MANTLE_AWS_SECRET_ACCESS_KEY",
    }}
    params = one(backend, merged(backend, "us.amazon.nova-lite-v1:0"), key_env)["litellm_params"]
    assert params["endpoint_url"] == "https://mantle.internal"
    assert params["aws_region_name"] == "us-west-2"  # the escape hatch outranks the adapter


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


def test_duplicate_model_names_are_a_render_error():
    # LiteLLM would read these as a load-balancing group and shuffle requests
    # between two backends — the exact failure this tool exists to prevent.
    backends = {OLLAMA.name: OLLAMA, GEMINI.name: GEMINI}
    models = [
        merged(OLLAMA, "qwen2.5:14b", "qwen2.5-14b"),
        merged(GEMINI, "gemini-2.5-pro", "qwen2.5-14b"),
    ]
    with pytest.raises(ConfigError) as exc:
        render_config(backends, models, KEY_ENV)
    message = str(exc.value)
    assert "qwen2.5-14b" in message
    assert "(home-ollama, qwen2.5:14b)" in message
    assert "(gemini, gemini-2.5-pro)" in message


def test_distinct_names_from_one_backend_are_fine():
    models = [
        merged(OLLAMA, "llava:latest", "llava"),
        merged(OLLAMA, "llava:34b", "llava-34b"),
    ]
    names = [e["model_name"] for e in render_config({OLLAMA.name: OLLAMA}, models, {})["model_list"]]
    assert names == ["llava", "llava-34b"]


# --- global sections ------------------------------------------------------


def test_settings_sections():
    config = render_config({}, [], {})
    assert config["general_settings"] == {"master_key": "os.environ/LITELLM_MASTER_KEY"}
    assert config["litellm_settings"] == {
        "drop_params": True,
        "callbacks": "custom_callbacks.proxy_handler_instance",
        # No unannounced outbound connections from a box holding cloud keys.
        "telemetry": False,
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
    resolved = {
        "MA_GEMINI_GEMINI_API_KEY": secret,
        "MA_ANTHROPIC_ANTHROPIC_API_KEY": secret,
        "MA_AWS_GOV_AWS_ACCESS_KEY_ID": secret,
        "MA_AWS_GOV_AWS_SECRET_ACCESS_KEY": secret,
        "MA_AWS_GOV_AWS_SESSION_TOKEN": secret,
    }
    key_env = {**KEY_ENV, **AWS_GOV_ENV}
    assert all(v in resolved for names in key_env.values() for v in names.values())

    backends = {b.name: b for b in (GEMINI, ANTHROPIC, COMPAT, BEDROCK)}
    models = [
        merged(GEMINI, "gemini-2.5-pro", "gemini-2.5-pro"),
        merged(ANTHROPIC, "claude-opus-5", "claude-opus-5"),
        merged(COMPAT, "qwen2.5:14b"),
        merged(BEDROCK, "us.anthropic.claude-sonnet-5-v1:0", "claude-sonnet-5"),
    ]
    text = to_yaml(render_config(backends, models, key_env))
    assert secret not in text
    assert "os.environ/MA_GEMINI_GEMINI_API_KEY" in text
    assert "os.environ/MA_ANTHROPIC_ANTHROPIC_API_KEY" in text
    assert "os.environ/MA_AWS_GOV_AWS_SECRET_ACCESS_KEY" in text
    assert "os.environ/LITELLM_MASTER_KEY" in text
