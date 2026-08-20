"""Ask each backend what it is serving right now.

Only facts a probe actually observed land in ObservedModel.facts; a key that is
absent means unobserved, not false.
"""
from __future__ import annotations

import json
import urllib.request

from .types import Backend, ObservedModel, ProbeResult

TIMEOUT = 3.0


def ensure_v1(api_base: str) -> str:
    """The OpenAI-style root, whether or not the configured address names it."""
    base = api_base.rstrip("/")
    return base if base.endswith("/v1") else f"{base}/v1"


def _http_json(url: str, payload: dict | None = None, headers: dict | None = None) -> dict:
    data = None
    sent = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload).encode()
        sent["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=sent)
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.loads(response.read().decode())


def probe_backend(backend: Backend, fetch=None, api_key: str | None = None) -> ProbeResult:
    """What `backend` serves. `api_key`, when given, authenticates the probe.

    `fetch` is resolved here rather than as a default argument so that patching
    this module's `_http_json` actually takes effect.
    """
    fetch = _http_json if fetch is None else fetch
    if backend.discovery == "static":
        return ProbeResult("static", [ObservedModel(m) for m in backend.models])

    base = (backend.api_base or "").rstrip("/")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
    if backend.type == "ollama":
        listing_url = f"{base}/api/tags"
    elif backend.type == "openai-compat":
        listing_url = f"{ensure_v1(base)}/models"
    else:
        return ProbeResult("down", error=f"no dynamic discovery for type {backend.type!r}")

    try:
        listing = fetch(listing_url, None, headers)
    except Exception as exc:
        # urllib's errors quote the URL and the status line, never the request
        # headers, so the bearer token cannot reach this message.
        return ProbeResult("down", error=f"{type(exc).__name__}: {exc}")

    if backend.type == "ollama":
        return ProbeResult("live", _ollama_models(base, listing, fetch, headers))
    return ProbeResult("live", _openai_models(listing))


def _ollama_models(base: str, listing: dict, fetch, headers: dict | None) -> list[ObservedModel]:
    models = []
    for entry in listing.get("models") or []:
        name = entry.get("name") or entry.get("model")
        if not name:
            continue
        try:
            detail = fetch(f"{base}/api/show", {"model": name}, headers)
        except Exception:
            detail = None  # the model is served either way; we just saw no facts
        models.append(ObservedModel(name, _ollama_facts(detail) if detail else {}))
    return models


def _ollama_facts(detail: dict) -> dict:
    facts = {}
    caps = detail.get("capabilities")
    if caps is not None:
        facts["tools"] = "tools" in caps
        facts["vision"] = "vision" in caps
        if "embedding" in caps:
            facts["mode"] = "embedding"
    for key, value in (detail.get("model_info") or {}).items():
        # architecture-prefixed, e.g. "qwen2.context_length"
        if key.endswith(".context_length"):
            facts["context"] = value
            break
    return facts


def _openai_models(listing: dict) -> list[ObservedModel]:
    models = []
    for entry in listing.get("data") or []:
        served_id = entry.get("id")
        if not served_id:
            continue
        facts = {}
        if entry.get("max_model_len") is not None:
            facts["context"] = entry["max_model_len"]
        models.append(ObservedModel(served_id, facts))
    return models
