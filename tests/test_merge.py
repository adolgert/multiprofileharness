from multiagent.merge import catalog_facts, merge_backend, stale_overrides
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


def backend(name="home-ollama", kind="ollama"):
    return Backend(name=name, type=kind, api_base="http://localhost:11434")


def probe(served_id, facts=None):
    return ProbeResult(status="live", models=[ObservedModel(id=served_id, facts=facts or {})])


def one(
    knowledge,
    served_id,
    observed_facts=None,
    catalog=CATALOG,
    name="home-ollama",
    kind="ollama",
):
    merged = merge_backend(
        backend(name, kind), probe(served_id, observed_facts), knowledge, catalog
    )
    assert len(merged) == 1
    return merged[0]


# --- catalog_facts -------------------------------------------------------


def test_catalog_facts_maps_names_and_prices():
    assert catalog_facts(CATALOG, ["gemini/gemini-2.5-pro"]) == {
        "context": 1048576,
        "max_output": 65536,
        "tools": True,
        "vision": True,
        "mode": "chat",
        "input_per_mtok": 1.25,
        "output_per_mtok": 10.0,
    }


def test_catalog_facts_first_present_key_wins_and_none_skipped():
    keys = [None, "gemini/gemini-2.5-pro", "gemini-2.5-pro"]
    assert catalog_facts(CATALOG, keys)["context"] == 1048576
    assert catalog_facts(CATALOG, [None, "nope", "gemini-2.5-pro"])["context"] == 999


def test_catalog_facts_omits_absent_and_unknown_key():
    assert catalog_facts(CATALOG, ["qwen2.5:14b"]) == {"context": 131072}
    assert catalog_facts(CATALOG, ["nothing", None]) == {}


# --- precedence ----------------------------------------------------------


def test_entry_beats_catalog():
    k = ModelKnowledge(
        models={
            "q": ModelEntry(
                match=["qwen2.5:14b"], facts={"context": 32768}, catalog_key="qwen2.5:14b"
            )
        }
    )
    assert one(k, "qwen2.5:14b").facts["context"] == 32768


def test_deployment_beats_entry():
    k = ModelKnowledge(
        models={"q": ModelEntry(match=["qwen2.5:14b"], facts={"max_output": 8192})},
        deployments={"home-ollama": {"qwen2.5:14b": {"max_output": 4096}}},
    )
    assert one(k, "qwen2.5:14b").facts["max_output"] == 4096


def test_deployment_scoped_to_its_backend():
    k = ModelKnowledge(
        models={"q": ModelEntry(match=["qwen2.5:14b"], facts={"max_output": 8192})},
        deployments={"home-ollama": {"qwen2.5:14b": {"max_output": 4096}}},
    )
    assert one(k, "qwen2.5:14b", name="work-ollama").facts["max_output"] == 8192


def test_observation_beats_the_model_entry():
    k = ModelKnowledge(models={"q": ModelEntry(match=["qwen2.5:14b"], facts={"context": 131072})})
    assert one(k, "qwen2.5:14b", {"context": 32768}).facts["context"] == 32768


def test_deployment_override_beats_observation():
    # ollama reports the model file's build context, not the context the server
    # was started with, so a human's per-deployment note has to be able to win.
    k = ModelKnowledge(
        models={"q": ModelEntry(match=["qwen2.5:14b"], facts={"context": 131072})},
        deployments={"home-ollama": {"qwen2.5:14b": {"context": 16384}}},
    )
    assert one(k, "qwen2.5:14b", {"context": 32768}).facts["context"] == 16384


def test_layers_union_rather_than_replace():
    k = ModelKnowledge(
        models={
            "g": ModelEntry(
                match=["gemini-2.5-pro*"],
                facts={"tools": False},
                catalog_key="gemini/gemini-2.5-pro",
            )
        },
        deployments={"home-ollama": {"gemini-2.5-pro": {"max_output": 4096}}},
    )
    m = one(k, "gemini-2.5-pro", {"mode": "chat"})
    assert m.facts == {
        "context": 1048576,
        "max_output": 4096,
        "tools": False,
        "vision": True,
        "mode": "chat",
        "input_per_mtok": 1.25,
        "output_per_mtok": 10.0,
    }


# --- conflicts -----------------------------------------------------------


def reported(model):
    return [(c.fact, c.believed, c.observed, c.winner) for c in model.conflicts]


def test_entry_belief_conflicts_with_observation_and_observation_wins():
    k = ModelKnowledge(models={"q": ModelEntry(match=["qwen2.5:14b"], facts={"context": 131072})})
    m = one(k, "qwen2.5:14b", {"context": 32768}, catalog={})
    assert m.facts["context"] == 32768
    assert reported(m) == [("context", 131072, 32768, "observed")]


def test_override_conflicts_with_observation_and_the_override_wins():
    k = ModelKnowledge(deployments={"home-ollama": {"qwen2.5:14b": {"context": 16384}}})
    m = one(k, "qwen2.5:14b", {"context": 32768}, catalog={})
    assert m.facts["context"] == 16384
    assert reported(m) == [("context", 16384, 32768, "override")]


def test_override_is_the_only_conflict_when_it_covers_the_fact():
    k = ModelKnowledge(
        models={"q": ModelEntry(match=["qwen2.5:14b"], facts={"max_output": 8192})},
        deployments={"home-ollama": {"qwen2.5:14b": {"max_output": 4096}}},
    )
    m = one(k, "qwen2.5:14b", {"max_output": 2048})
    assert reported(m) == [("max_output", 4096, 2048, "override")]


def test_no_conflict_when_belief_equals_observation():
    k = ModelKnowledge(models={"q": ModelEntry(match=["qwen2.5:14b"], facts={"context": 32768})})
    assert one(k, "qwen2.5:14b", {"context": 32768}, catalog={}).conflicts == []


def test_no_conflict_when_the_override_agrees_with_the_observation():
    k = ModelKnowledge(deployments={"home-ollama": {"qwen2.5:14b": {"context": 32768}}})
    assert one(k, "qwen2.5:14b", {"context": 32768}, catalog={}).conflicts == []


def test_no_conflict_for_catalog_versus_observation():
    k = ModelKnowledge(models={"q": ModelEntry(match=["qwen2.5:14b"])})
    m = one(k, "qwen2.5:14b", {"context": 32768})
    assert m.conflicts == []
    assert m.facts["context"] == 32768


def test_belief_only_or_observation_only_facts_do_not_conflict():
    k = ModelKnowledge(models={"q": ModelEntry(match=["qwen2.5:14b"], facts={"tools": True})})
    m = one(k, "qwen2.5:14b", {"vision": False}, catalog={})
    assert m.conflicts == []
    assert m.facts == {"tools": True, "vision": False}


def test_conflicts_reported_in_facts_order():
    k = ModelKnowledge(
        models={"q": ModelEntry(match=["qwen2.5:14b"], facts={"tools": True, "context": 131072})}
    )
    m = one(k, "qwen2.5:14b", {"tools": False, "context": 32768}, catalog={})
    assert [c.fact for c in m.conflicts] == ["context", "tools"]


# --- canonical name ------------------------------------------------------


def test_longest_matching_pattern_wins_over_a_glob():
    k = ModelKnowledge(
        models={
            "llava": ModelEntry(match=["llava:*"], facts={"context": 4096}),
            "llava-34b": ModelEntry(match=["llava:34b"], facts={"context": 32768}),
        }
    )
    assert k.canonical_for("llava:13b") == "llava"
    assert k.canonical_for("llava:34b") == "llava-34b"
    assert one(k, "llava:34b", catalog={}).facts["context"] == 32768


def test_equally_specific_patterns_break_the_tie_by_file_order():
    k = ModelKnowledge(
        models={
            "first": ModelEntry(match=["llava:*"]),
            "second": ModelEntry(match=["llava:*"]),
        }
    )
    assert k.canonical_for("llava:13b") == "first"


def test_no_pattern_matches():
    k = ModelKnowledge(models={"q": ModelEntry(match=["qwen*"])})
    assert k.canonical_for("llama3") is None


# --- catalog gating by backend type --------------------------------------


def test_hosted_backend_inherits_catalog_by_served_id():
    m = one(ModelKnowledge(), "gemini-2.5-pro", name="gemini", kind="gemini")
    assert m.canonical is None
    assert m.facts == {"context": 999, "mode": "chat"}


def test_local_backend_does_not_inherit_catalog_by_served_id():
    # A local model whose served id collides with a catalog key must not be
    # given the hosted model's context or price.
    m = one(ModelKnowledge(), "qwen2.5:14b")
    assert m.canonical is None
    assert m.facts == {}


def test_local_backend_does_not_inherit_catalog_by_canonical_name_either():
    k = ModelKnowledge(models={"qwen2.5:14b": ModelEntry(match=["served-alias"])})
    m = one(k, "served-alias")
    assert m.canonical == "qwen2.5:14b"
    assert m.facts == {}


def test_hosted_backend_inherits_catalog_by_canonical_name():
    k = ModelKnowledge(models={"qwen2.5:14b": ModelEntry(match=["served-alias"])})
    assert one(k, "served-alias", name="cloud", kind="anthropic").facts == {"context": 131072}


def test_catalog_key_applies_whatever_the_backend_type():
    k = ModelKnowledge(models={"q": ModelEntry(match=["qwen*"], catalog_key="qwen2.5:14b")})
    assert one(k, "qwen2.5:14b").facts == {"context": 131072}


def test_catalog_key_preferred_over_served_id():
    k = ModelKnowledge(
        models={"g": ModelEntry(match=["gemini-2.5-pro*"], catalog_key="gemini/gemini-2.5-pro")}
    )
    assert one(k, "gemini-2.5-pro", name="gemini", kind="gemini").facts["context"] == 1048576


def test_unknown_served_id_with_no_catalog_entry():
    m = one(ModelKnowledge(), "mystery:7b", {"mode": "chat"})
    assert m.canonical is None
    assert m.facts == {"mode": "chat"}


# --- fact filtering ------------------------------------------------------


def test_non_fact_keys_dropped_from_every_belief_layer():
    k = ModelKnowledge(
        models={"q": ModelEntry(match=["qwen2.5:14b"], facts={"context": 8192, "family": "qwen"})},
        deployments={"home-ollama": {"qwen2.5:14b": {"quantization": "q4"}}},
    )
    m = one(k, "qwen2.5:14b", {"digest": "abc123", "tools": True}, catalog={})
    assert m.facts == {"context": 8192, "tools": True}


def test_non_fact_key_never_becomes_a_conflict():
    k = ModelKnowledge(models={"q": ModelEntry(match=["qwen2.5:14b"], facts={"family": "qwen"})})
    m = one(k, "qwen2.5:14b", {"family": "llama"}, catalog={})
    assert m.conflicts == []
    assert m.facts == {}


# --- stale deployment overrides ------------------------------------------


def test_stale_override_is_reported():
    k = ModelKnowledge(
        deployments={"home-ollama": {"qwen1.5:old": {"context": 4096}, "qwen2.5:14b": {}}}
    )
    assert stale_overrides(backend(), probe("qwen2.5:14b"), k) == [
        "deployment override for home-ollama/qwen1.5:old matches no served model (redeployed?)"
    ]


def test_no_stale_warning_when_every_override_matches():
    k = ModelKnowledge(deployments={"home-ollama": {"qwen2.5:14b": {"context": 4096}}})
    assert stale_overrides(backend(), probe("qwen2.5:14b"), k) == []


def test_a_down_or_static_backend_reports_nothing_stale():
    # Seeing no models is not evidence that an override went stale.
    k = ModelKnowledge(deployments={"home-ollama": {"qwen2.5:14b": {"context": 4096}}})
    for result in (ProbeResult("down", error="refused"), ProbeResult("static", [])):
        assert stale_overrides(backend(), result, k) == []


def test_overrides_for_other_backends_are_not_stale_here():
    k = ModelKnowledge(deployments={"work-vllm": {"qwen1.5:old": {}}})
    assert stale_overrides(backend(), probe("qwen2.5:14b"), k) == []


# --- shape ---------------------------------------------------------------


def test_merges_every_observed_model_and_carries_backend_name():
    k = ModelKnowledge(models={"q": ModelEntry(match=["qwen2.5:14b"])})
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
