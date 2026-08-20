from multiagent.merge import catalog_facts, merge_backend
from multiagent.types import (
    Backend,
    ModelEntry,
    ModelKnowledge,
    ObservedModel,
    ProbeResult,
)

CATALOG = {
    "gemini/gemini-2.5-pro": {
        "max_input_tokens": 1048576,
        "max_output_tokens": 65536,
        "supports_function_calling": True,
        "supports_vision": True,
        "mode": "chat",
        "input_cost_per_token": 1.25e-06,
        "output_cost_per_token": 1e-05,
        "litellm_provider": "gemini",
    },
    "gemini-2.5-pro": {"max_input_tokens": 999, "mode": "chat"},
    "qwen2.5:14b": {"max_input_tokens": 131072},
}


def backend(name="home-ollama"):
    return Backend(name=name, type="ollama", api_base="http://localhost:11434")


def probe(served_id, facts=None):
    return ProbeResult(status="live", models=[ObservedModel(id=served_id, facts=facts or {})])


def one(knowledge, served_id, observed_facts=None, catalog=CATALOG, name="home-ollama"):
    merged = merge_backend(backend(name), probe(served_id, observed_facts), knowledge, catalog)
    assert len(merged) == 1
    return merged[0]


# --- catalog_facts -------------------------------------------------------


def test_catalog_facts_maps_names_and_prices():
    facts = catalog_facts(CATALOG, ["gemini/gemini-2.5-pro"])
    assert facts == {
        "context": 1048576,
        "max_output": 65536,
        "tools": True,
        "vision": True,
        "mode": "chat",
        "input_per_mtok": 1.25,
        "output_per_mtok": 10.0,
    }


def test_catalog_facts_first_present_key_wins_and_none_skipped():
    assert catalog_facts(CATALOG, [None, "gemini/gemini-2.5-pro", "gemini-2.5-pro"])["context"] == 1048576
    assert catalog_facts(CATALOG, [None, "nope", "gemini-2.5-pro"])["context"] == 999


def test_catalog_facts_omits_absent_and_unknown_key():
    assert catalog_facts(CATALOG, ["qwen2.5:14b"]) == {"context": 131072}
    assert catalog_facts(CATALOG, ["nothing", None]) == {}


# --- precedence ----------------------------------------------------------


def test_entry_beats_catalog():
    k = ModelKnowledge(
        models={"q": ModelEntry(name="q", match=["qwen2.5:14b"], facts={"context": 32768})}
    )
    assert one(k, "qwen2.5:14b").facts["context"] == 32768


def test_deployment_beats_entry():
    k = ModelKnowledge(
        models={"q": ModelEntry(name="q", match=["qwen2.5:14b"], facts={"max_output": 8192})},
        deployments={"home-ollama": {"qwen2.5:14b": {"max_output": 4096}}},
    )
    assert one(k, "qwen2.5:14b").facts["max_output"] == 4096


def test_deployment_scoped_to_its_backend():
    k = ModelKnowledge(
        models={"q": ModelEntry(name="q", match=["qwen2.5:14b"], facts={"max_output": 8192})},
        deployments={"home-ollama": {"qwen2.5:14b": {"max_output": 4096}}},
    )
    assert one(k, "qwen2.5:14b", name="work-ollama").facts["max_output"] == 8192


def test_observed_beats_deployment():
    k = ModelKnowledge(
        models={"q": ModelEntry(name="q", match=["qwen2.5:14b"], facts={"max_output": 8192})},
        deployments={"home-ollama": {"qwen2.5:14b": {"max_output": 4096}}},
    )
    m = one(k, "qwen2.5:14b", {"max_output": 2048})
    assert m.facts["max_output"] == 2048


def test_layers_union_rather_than_replace():
    k = ModelKnowledge(
        models={
            "g": ModelEntry(
                name="g",
                match=["gemini-2.5-pro*"],
                facts={"tokenizer": "gemini"},
                catalog_key="gemini/gemini-2.5-pro",
            )
        },
        deployments={"home-ollama": {"gemini-2.5-pro": {"max_output": 4096}}},
    )
    m = one(k, "gemini-2.5-pro", {"mode": "chat"})
    assert m.facts == {
        "context": 1048576,
        "max_output": 4096,
        "tools": True,
        "vision": True,
        "mode": "chat",
        "tokenizer": "gemini",
        "input_per_mtok": 1.25,
        "output_per_mtok": 10.0,
    }


# --- conflicts -----------------------------------------------------------


def test_entry_belief_conflicts_with_observation_and_observation_wins():
    k = ModelKnowledge(
        models={"q": ModelEntry(name="q", match=["qwen2.5:14b"], facts={"context": 131072})}
    )
    m = one(k, "qwen2.5:14b", {"context": 32768}, catalog={})
    assert m.facts["context"] == 32768
    assert [(c.fact, c.believed, c.observed) for c in m.conflicts] == [("context", 131072, 32768)]


def test_deployment_is_the_believed_side_when_both_define_it():
    k = ModelKnowledge(
        models={"q": ModelEntry(name="q", match=["qwen2.5:14b"], facts={"max_output": 8192})},
        deployments={"home-ollama": {"qwen2.5:14b": {"max_output": 4096}}},
    )
    m = one(k, "qwen2.5:14b", {"max_output": 2048})
    assert [(c.fact, c.believed, c.observed) for c in m.conflicts] == [("max_output", 4096, 2048)]


def test_no_conflict_when_belief_equals_observation():
    k = ModelKnowledge(
        models={"q": ModelEntry(name="q", match=["qwen2.5:14b"], facts={"context": 32768})}
    )
    m = one(k, "qwen2.5:14b", {"context": 32768}, catalog={})
    assert m.conflicts == []


def test_no_conflict_for_catalog_versus_observation():
    k = ModelKnowledge(models={"q": ModelEntry(name="q", match=["qwen2.5:14b"])})
    m = one(k, "qwen2.5:14b", {"context": 32768})
    assert m.conflicts == []
    assert m.facts["context"] == 32768


def test_belief_only_or_observation_only_facts_do_not_conflict():
    k = ModelKnowledge(
        models={"q": ModelEntry(name="q", match=["qwen2.5:14b"], facts={"tools": True})}
    )
    m = one(k, "qwen2.5:14b", {"vision": False}, catalog={})
    assert m.conflicts == []
    assert m.facts == {"tools": True, "vision": False}


def test_conflicts_reported_in_facts_order():
    k = ModelKnowledge(
        models={
            "q": ModelEntry(
                name="q",
                match=["qwen2.5:14b"],
                facts={"tools": True, "context": 131072},
            )
        }
    )
    m = one(k, "qwen2.5:14b", {"tools": False, "context": 32768}, catalog={})
    assert [c.fact for c in m.conflicts] == ["context", "tools"]


# --- canonical / catalog key ---------------------------------------------


def test_unknown_served_id_still_gets_catalog_by_served_id():
    m = one(ModelKnowledge(), "qwen2.5:14b")
    assert m.canonical is None
    assert m.facts == {"context": 131072}
    assert m.conflicts == []


def test_unknown_served_id_with_no_catalog_entry():
    m = one(ModelKnowledge(), "mystery:7b", {"mode": "chat"})
    assert m.canonical is None
    assert m.facts == {"mode": "chat"}


def test_catalog_key_preferred_over_served_id():
    k = ModelKnowledge(
        models={
            "g": ModelEntry(
                name="g", match=["gemini-2.5-pro*"], catalog_key="gemini/gemini-2.5-pro"
            )
        }
    )
    assert one(k, "gemini-2.5-pro").facts["context"] == 1048576


def test_canonical_name_used_as_last_catalog_key():
    k = ModelKnowledge(models={"c": ModelEntry(name="qwen2.5:14b", match=["served-alias"])})
    m = one(k, "served-alias")
    assert m.canonical == "qwen2.5:14b"
    assert m.facts == {"context": 131072}


# --- fact filtering ------------------------------------------------------


def test_non_fact_keys_dropped_from_every_belief_layer():
    k = ModelKnowledge(
        models={
            "q": ModelEntry(
                name="q", match=["qwen2.5:14b"], facts={"context": 8192, "family": "qwen"}
            )
        },
        deployments={"home-ollama": {"qwen2.5:14b": {"quantization": "q4"}}},
    )
    m = one(k, "qwen2.5:14b", {"digest": "abc123", "tools": True}, catalog={})
    assert m.facts == {"context": 8192, "tools": True}


def test_non_fact_key_never_becomes_a_conflict():
    k = ModelKnowledge(
        models={"q": ModelEntry(name="q", match=["qwen2.5:14b"], facts={"family": "qwen"})}
    )
    m = one(k, "qwen2.5:14b", {"family": "llama"}, catalog={})
    assert m.conflicts == []
    assert m.facts == {}


# --- shape ---------------------------------------------------------------


def test_merges_every_observed_model_and_carries_backend_name():
    k = ModelKnowledge(models={"q": ModelEntry(name="q", match=["qwen2.5:14b"])})
    result = merge_backend(
        backend("home-ollama"),
        ProbeResult(
            status="live",
            models=[ObservedModel(id="qwen2.5:14b"), ObservedModel(id="llava:13b")],
        ),
        k,
        CATALOG,
    )
    assert [(m.backend, m.served_id, m.canonical) for m in result] == [
        ("home-ollama", "qwen2.5:14b", "q"),
        ("home-ollama", "llava:13b", None),
    ]


def test_empty_probe_merges_to_nothing():
    assert merge_backend(backend(), ProbeResult(status="down"), ModelKnowledge(), CATALOG) == []
