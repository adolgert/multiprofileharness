"""Render the LiteLLM proxy config.

Clean by construction: a secret never enters this module. Callers pass
`key_env`, a backend name -> {original variable NAME: namespaced variable
NAME} map, and the only thing rendered is the string `os.environ/VARNAME`,
which LiteLLM resolves in the proxy process. The output is therefore always
safe to print, diff, and attach to a bug report.

Every route names the variables it uses, per backend. That is load-bearing for
Bedrock: `AWS_*` is process-global, so two accounts in one proxy would
otherwise sign every request with whichever set won, silently.
"""
from __future__ import annotations

import yaml

from . import credentials
from .probe import ensure_v1
from .types import LITELLM_FIELDS, LITELLM_PRICES, Backend, ConfigError, Deployment

# litellm_params name -> the credential-file variable that fills it.
AWS_PARAMS = {
    "aws_access_key_id": "AWS_ACCESS_KEY_ID",
    "aws_secret_access_key": "AWS_SECRET_ACCESS_KEY",
    "aws_session_token": "AWS_SESSION_TOKEN",  # short-term keys only
}


def _names(backend: Backend, key_env: dict[str, dict[str, str]]) -> dict[str, str]:
    names = key_env.get(backend.name)
    if not names:
        raise ConfigError(
            f"backend {backend.name!r} needs a key but no env var was given for it"
        )
    return names


def _key_ref(backend: Backend, key_env: dict[str, dict[str, str]]) -> str:
    names = _names(backend, key_env)
    var = credentials.key_var(names, source=f"credential {backend.credential!r}")
    return f"os.environ/{names[var]}"


def _aws_params(backend: Backend, key_env: dict[str, dict[str, str]]) -> dict:
    names = _names(backend, key_env)
    missing = [
        var for param, var in AWS_PARAMS.items()
        if var not in names and param != "aws_session_token"
    ]
    if missing:
        raise ConfigError(
            f"backend {backend.name!r} is bedrock but credential "
            f"{backend.credential!r} defines no {' or '.join(missing)}"
        )
    return {
        param: f"os.environ/{names[var]}"
        for param, var in AWS_PARAMS.items()
        if var in names
    }


def _params(backend: Backend, model: Deployment, key_env: dict[str, dict[str, str]]) -> dict:
    served = model.served_id
    if backend.type == "ollama":
        # ollama_chat is chat-only; embeddings must go through the plain route.
        prefix = "ollama" if model.facts.get("mode") == "embedding" else "ollama_chat"
        return {"model": f"{prefix}/{served}", "api_base": backend.api_base}
    if backend.type == "openai-compat":
        return {
            "model": f"openai/{served}",
            "api_base": ensure_v1(backend.api_base) if backend.api_base else None,
            # The OpenAI client refuses to send without a key, even to a local
            # server that ignores it.
            "api_key": _key_ref(backend, key_env) if backend.credential else "dummy",
        }
    if backend.type == "gemini":
        return {"model": f"gemini/{served}", "api_key": _key_ref(backend, key_env)}
    if backend.type == "anthropic":
        return {"model": f"anthropic/{served}", "api_key": _key_ref(backend, key_env)}
    if backend.type == "bedrock":
        if not backend.region:
            raise ConfigError(f"backend {backend.name!r} is bedrock and needs a region")
        return {
            "model": f"bedrock/{served}",
            "aws_region_name": backend.region,
            **_aws_params(backend, key_env),
        }
    raise ConfigError(f"backend {backend.name!r} has unsupported type {backend.type!r}")


def _model_info(facts: dict) -> dict:
    info = {theirs: facts[ours] for ours, theirs in LITELLM_FIELDS.items() if ours in facts}
    for ours, theirs in LITELLM_PRICES.items():
        if ours in facts:
            info[theirs] = facts[ours] / 1e6
    return info


def _check_unique(model_list: list[dict], merged: list[Deployment]) -> None:
    """LiteLLM load-balances same-name entries; a collision must not be silent."""
    owners: dict[str, list[str]] = {}
    for entry, model in zip(model_list, merged):
        owners.setdefault(entry["model_name"], []).append(
            f"({model.backend}, {model.served_id})"
        )
    duplicates = {name: who for name, who in owners.items() if len(who) > 1}
    if duplicates:
        detail = "; ".join(
            f"{name}: {', '.join(who)}" for name, who in sorted(duplicates.items())
        )
        raise ConfigError(
            f"two deployments claim the same model name, which LiteLLM would read as a "
            f"load-balancing group and spread requests across: {detail}. "
            f"Give them different canonical names in models.yaml."
        )


def render_config(
    backends: dict[str, Backend],
    merged: list[Deployment],
    key_env: dict[str, dict[str, str]],
) -> dict:
    """Build the proxy config dict.

    `key_env` maps a backend name to {credential variable NAME: the namespaced
    NAME it carries into the container}.
    """
    model_list = []
    for model in merged:
        backend = backends[model.backend]
        params = _params(backend, model, key_env)
        # Last, and verbatim: the escape hatch outranks what the adapter guessed.
        params.update(backend.extra)
        model_list.append(
            {
                "model_name": model.canonical or model.served_id,
                "litellm_params": params,
                "model_info": _model_info(model.facts),
            }
        )
    _check_unique(model_list, merged)
    return {
        "model_list": model_list,
        "general_settings": {"master_key": "os.environ/LITELLM_MASTER_KEY"},
        "litellm_settings": {
            "drop_params": True,
            # The callback module ships in the proxy image; this string is the contract.
            "callbacks": "custom_callbacks.proxy_handler_instance",
            "telemetry": False,
        },
    }


def to_yaml(config: dict) -> str:
    return yaml.safe_dump(config, sort_keys=False, default_flow_style=False)
