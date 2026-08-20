import json
from pathlib import Path

import pytest

from multiagent.probe import probe_backend
from multiagent.types import Backend

FIXTURES = Path(__file__).parent / "fixtures"


def stub_fetch(responses):
    """responses maps url -> response dict, or -> callable(payload) when one url
    serves several models (ollama's /api/show)."""

    def fetch(url, payload=None, timeout=3.0):
        if url not in responses:
            raise RuntimeError(f"unexpected url {url}")
        value = responses[url]
        return value(payload) if callable(value) else value

    return fetch


def no_fetch(url, payload=None, timeout=3.0):
    raise AssertionError(f"probe made a network call to {url}")


OLLAMA_TAGS = {
    "models": [
        {"name": "qwen2.5:14b", "model": "qwen2.5:14b", "size": 9000000000, "details": {}},
        {"name": "llava:13b", "model": "llava:13b", "size": 8000000000, "details": {}},
        {"name": "nomic-embed-text:latest", "model": "nomic-embed-text:latest", "details": {}},
    ]
}

OLLAMA_SHOW = {
    "qwen2.5:14b": {
        "license": "Apache 2.0",
        "modelfile": "FROM qwen2.5:14b",
        "template": "{{ .Prompt }}",
        "details": {"family": "qwen2"},
        "model_info": {"general.architecture": "qwen2", "qwen2.context_length": 32768},
        "capabilities": ["completion", "tools"],
        "modified_at": "2025-08-01T12:00:00Z",
    },
    "llava:13b": {
        "details": {"family": "llama"},
        "model_info": {"general.architecture": "llama", "llama.context_length": 4096},
        "capabilities": ["completion", "vision"],
    },
    "nomic-embed-text:latest": {
        "details": {"family": "nomic-bert"},
        "model_info": {"nomic-bert.context_length": 2048},
        "capabilities": ["embedding"],
    },
}


def test_static_backend_makes_no_network_call():
    backend = Backend(
        name="anthropic",
        type="anthropic",
        discovery="static",
        models=["claude-opus-4", "claude-sonnet-4"],
    )
    result = probe_backend(backend, fetch=no_fetch)
    assert result.status == "static"
    assert [m.id for m in result.models] == ["claude-opus-4", "claude-sonnet-4"]
    assert all(m.facts == {} for m in result.models)


def test_ollama_happy_path_extracts_facts():
    fetch = stub_fetch(
        {
            "http://localhost:11434/api/tags": OLLAMA_TAGS,
            "http://localhost:11434/api/show": lambda payload: OLLAMA_SHOW[payload["model"]],
        }
    )
    backend = Backend(
        name="local", type="ollama", api_base="http://localhost:11434", discovery="dynamic"
    )
    result = probe_backend(backend, fetch=fetch)

    assert result.status == "live"
    assert result.error is None
    facts = {m.id: m.facts for m in result.models}
    assert facts["qwen2.5:14b"] == {"tools": True, "vision": False, "context": 32768}
    assert facts["llava:13b"] == {"tools": False, "vision": True, "context": 4096}
    assert facts["nomic-embed-text:latest"] == {
        "tools": False,
        "vision": False,
        "mode": "embedding",
        "context": 2048,
    }


def test_ollama_trailing_slash_api_base():
    fetch = stub_fetch(
        {
            "http://localhost:11434/api/tags": {"models": [{"name": "qwen2.5:14b"}]},
            "http://localhost:11434/api/show": lambda payload: OLLAMA_SHOW[payload["model"]],
        }
    )
    backend = Backend(
        name="local", type="ollama", api_base="http://localhost:11434/", discovery="dynamic"
    )
    assert [m.id for m in probe_backend(backend, fetch=fetch).models] == ["qwen2.5:14b"]


def test_ollama_show_failure_keeps_model_with_empty_facts():
    def show(payload):
        if payload["model"] == "llava:13b":
            raise RuntimeError("500 Internal Server Error")
        return OLLAMA_SHOW[payload["model"]]

    fetch = stub_fetch(
        {
            "http://localhost:11434/api/tags": OLLAMA_TAGS,
            "http://localhost:11434/api/show": show,
        }
    )
    backend = Backend(
        name="local", type="ollama", api_base="http://localhost:11434", discovery="dynamic"
    )
    result = probe_backend(backend, fetch=fetch)

    assert result.status == "live"
    facts = {m.id: m.facts for m in result.models}
    assert set(facts) == {"qwen2.5:14b", "llava:13b", "nomic-embed-text:latest"}
    assert facts["llava:13b"] == {}
    assert facts["qwen2.5:14b"]["context"] == 32768


def test_openai_compat_vllm_fixture_reports_context():
    listing = json.loads((FIXTURES / "vllm_models.json").read_text())
    fetch = stub_fetch({"http://gpu01:8000/v1/models": listing})
    backend = Backend(
        name="vllm", type="openai-compat", api_base="http://gpu01:8000", discovery="dynamic"
    )
    result = probe_backend(backend, fetch=fetch)

    assert result.status == "live"
    assert [m.id for m in result.models] == [
        "Qwen/Qwen2.5-14B-Instruct",
        "meta-llama/Llama-3.1-8B-Instruct",
    ]
    assert [m.facts for m in result.models] == [{"context": 16384}, {"context": 131072}]


def test_openai_compat_without_max_model_len_has_no_context_fact():
    listing = {
        "object": "list",
        "data": [
            {"id": "qwen2.5:14b", "object": "model", "owned_by": "library"},
            {"id": "llava:13b", "object": "model", "owned_by": "library"},
        ],
    }
    fetch = stub_fetch({"http://localhost:11434/v1/models": listing})
    backend = Backend(
        name="ollama-facade",
        type="openai-compat",
        api_base="http://localhost:11434",
        discovery="dynamic",
    )
    result = probe_backend(backend, fetch=fetch)

    assert result.status == "live"
    assert all(m.facts == {} for m in result.models)


@pytest.mark.parametrize(
    "backend",
    [
        Backend(name="local", type="ollama", api_base="http://down:11434", discovery="dynamic"),
        Backend(
            name="vllm", type="openai-compat", api_base="http://down:8000", discovery="dynamic"
        ),
    ],
)
def test_listing_failure_is_down_with_error(backend):
    result = probe_backend(backend, fetch=stub_fetch({}))
    assert result.status == "down"
    assert result.models == []
    assert result.error and "unexpected url" in result.error
