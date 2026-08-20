"""Render the LiteLLM proxy config.

Clean by construction: a secret never enters this module. Callers pass
`key_env`, a backend name -> environment variable NAME map, and the only thing
rendered is the string `os.environ/VARNAME`, which LiteLLM resolves in the
proxy process. The output is therefore always safe to print, diff, and attach
to a bug report.
"""
from __future__ import annotations

import yaml

from .types import Backend, ConfigError, MergedModel

# Our fact name -> LiteLLM model_info field. Prices are handled separately.
_INFO_FIELDS = {
    "context": "max_input_tokens",
    "max_output": "max_output_tokens",
    "tools": "supports_function_calling",
    "vision": "supports_vision",
    "mode": "mode",
}
_INFO_PRICES = {
    "input_per_mtok": "input_cost_per_token",
    "output_per_mtok": "output_cost_per_token",
}


def _key_ref(backend: Backend, key_env: dict[str, str]) -> str:
    var = key_env.get(backend.name)
    if not var:
        raise ConfigError(f"backend {backend.name!r} needs a key but no env var was given for it")
    return f"os.environ/{var}"


def _v1(api_base: str | None) -> str | None:
    if api_base is None:
        return None
    base = api_base.rstrip("/")
    return base if base.endswith("/v1") else f"{base}/v1"


def _params(backend: Backend, model: MergedModel, key_env: dict[str, str]) -> dict:
    served = model.served_id
    if backend.type == "ollama":
        # ollama_chat is chat-only; embeddings must go through the plain route.
        prefix = "ollama" if model.facts.get("mode") == "embedding" else "ollama_chat"
        return {"model": f"{prefix}/{served}", "api_base": backend.api_base}
    if backend.type == "openai-compat":
        return {
            "model": f"openai/{served}",
            "api_base": _v1(backend.api_base),
            # The OpenAI client refuses to send without a key, even to a local
            # server that ignores it.
            "api_key": _key_ref(backend, key_env) if backend.credential else "dummy",
        }
    if backend.type == "gemini":
        return {"model": f"gemini/{served}", "api_key": _key_ref(backend, key_env)}
    if backend.type == "anthropic":
        return {"model": f"anthropic/{served}", "api_key": _key_ref(backend, key_env)}
    if backend.type == "bedrock":
        # No api_key: the proxy's boto3 signs with the AWS_* variables the
        # launcher's env file delivers, so there is no key name to reference.
        if not backend.region:
            raise ConfigError(f"backend {backend.name!r} is bedrock and needs a region")
        return {"model": f"bedrock/{served}", "aws_region_name": backend.region}
    raise ConfigError(f"backend {backend.name!r} has unsupported type {backend.type!r}")


def _model_info(facts: dict) -> dict:
    info = {theirs: facts[ours] for ours, theirs in _INFO_FIELDS.items() if ours in facts}
    for ours, theirs in _INFO_PRICES.items():
        if ours in facts:
            info[theirs] = facts[ours] / 1e6
    return info


def render_config(
    backends: dict[str, Backend], merged: list[MergedModel], key_env: dict[str, str]
) -> dict:
    """Build the proxy config dict; `key_env` maps backend name -> env var NAME."""
    model_list = []
    for model in merged:
        backend = backends[model.backend]
        model_list.append(
            {
                "model_name": model.canonical or model.served_id,
                "litellm_params": _params(backend, model, key_env),
                "model_info": _model_info(model.facts),
            }
        )
    return {
        "model_list": model_list,
        "general_settings": {"master_key": "os.environ/LITELLM_MASTER_KEY"},
        "litellm_settings": {
            "drop_params": True,
            # The callback module ships in the proxy image; this string is the contract.
            "callbacks": "custom_callbacks.proxy_handler_instance",
        },
    }


def to_yaml(config: dict) -> str:
    return yaml.safe_dump(config, sort_keys=False, default_flow_style=False)
