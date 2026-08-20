"""Merge what a probe observed with what we believe about a model.

Precedence, lowest to highest: vendored catalog < models.yaml entry <
deployments override < observation. Only the two middle layers are *our
belief*, so only they can disagree with the probe; the catalog is an external
source and its differences are silently overridden.
"""
from __future__ import annotations

from .types import FACTS, Backend, Conflict, MergedModel, ModelEntry, ModelKnowledge, ProbeResult

# LiteLLM catalog field -> our fact name.
_CATALOG_FIELDS = {
    "max_input_tokens": "context",
    "max_output_tokens": "max_output",
    "supports_function_calling": "tools",
    "supports_vision": "vision",
    "mode": "mode",
}
# Same, but per-token prices we restate per million tokens.
_CATALOG_PRICES = {
    "input_cost_per_token": "input_per_mtok",
    "output_cost_per_token": "output_per_mtok",
}


def catalog_facts(catalog: dict, keys: list[str]) -> dict:
    entry = next((catalog[k] for k in keys if k and k in catalog), None)
    if entry is None:
        return {}
    facts = {ours: entry[theirs] for theirs, ours in _CATALOG_FIELDS.items() if theirs in entry}
    for theirs, ours in _CATALOG_PRICES.items():
        if theirs in entry:
            facts[ours] = round(entry[theirs] * 1e6, 4)
    return facts


def _known(facts: dict) -> dict:
    return {k: v for k, v in facts.items() if k in FACTS}


def _entry_for(knowledge: ModelKnowledge, canonical: str | None) -> ModelEntry | None:
    if canonical is None:
        return None
    return next((e for e in knowledge.models.values() if e.name == canonical), None)


def merge_backend(
    backend: Backend, probe: ProbeResult, knowledge: ModelKnowledge, catalog: dict
) -> list[MergedModel]:
    overrides = knowledge.deployments.get(backend.name, {})
    merged = []
    for observed in probe.models:
        canonical = knowledge.canonical_for(observed.id)
        entry = _entry_for(knowledge, canonical)
        catalog_keys = [entry.catalog_key if entry else None, observed.id, canonical]

        seen = _known(observed.facts)
        believed = {
            **_known(entry.facts if entry else {}),
            **_known(overrides.get(observed.id, {})),
        }
        facts = catalog_facts(catalog, catalog_keys)
        facts.update(believed)
        facts.update(seen)

        conflicts = [
            Conflict(f, believed[f], seen[f])
            for f in FACTS
            if f in believed and f in seen and believed[f] != seen[f]
        ]
        merged.append(MergedModel(backend.name, observed.id, canonical, facts, conflicts))
    return merged
