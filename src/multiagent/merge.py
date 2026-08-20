"""Merge what a probe observed with what we believe about a model.

Precedence, lowest to highest: vendored catalog < models.yaml entry <
observation < deployments override. Observation beats our general belief about
a model, but a deployment override — a human writing down what THIS backend
does with THIS served id — beats the observation, because a probe can answer
the wrong question: ollama reports the model file's build context, not the
context the server was started with. Both disagreements are reported as
conflicts; the catalog is an external source and is overridden silently.
"""
from __future__ import annotations

from .types import (
    FACTS,
    LITELLM_FIELDS,
    LITELLM_PRICES,
    Backend,
    Conflict,
    Deployment,
    ModelKnowledge,
    ProbeResult,
)

# Backend types whose served ids are the catalog's own keys. Elsewhere a served
# id is a local name that may collide with a hosted one, and inheriting hosted
# prices for a model running on your own GPU is worse than knowing no price.
HOSTED = {"gemini", "anthropic", "bedrock"}


def catalog_facts(catalog: dict, keys: list[str]) -> dict:
    entry = next((catalog[k] for k in keys if k and k in catalog), None)
    if entry is None:
        return {}
    facts = {ours: entry[theirs] for ours, theirs in LITELLM_FIELDS.items() if theirs in entry}
    for ours, theirs in LITELLM_PRICES.items():
        if theirs in entry:
            facts[ours] = round(entry[theirs] * 1e6, 4)
    return facts


def _known(facts: dict) -> dict:
    return {k: v for k, v in facts.items() if k in FACTS}


def merge_backend(
    backend: Backend, probe: ProbeResult, knowledge: ModelKnowledge, catalog: dict
) -> list[Deployment]:
    overrides = knowledge.deployments.get(backend.name, {})
    merged = []
    for observed in probe.models:
        canonical = knowledge.canonical_for(observed.id)
        entry = knowledge.models.get(canonical) if canonical else None
        catalog_keys = [entry.catalog_key if entry else None]
        if backend.type in HOSTED:
            catalog_keys += [observed.id, canonical]

        seen = _known(observed.facts)
        believed = _known(entry.facts if entry else {})
        override = _known(overrides.get(observed.id, {}))

        facts = catalog_facts(catalog, catalog_keys)
        facts.update(believed)
        facts.update(seen)
        facts.update(override)

        conflicts = []
        for fact in FACTS:
            if fact not in seen:
                continue
            if fact in override and override[fact] != seen[fact]:
                conflicts.append(Conflict(fact, override[fact], seen[fact], "override"))
            elif fact not in override and fact in believed and believed[fact] != seen[fact]:
                conflicts.append(Conflict(fact, believed[fact], seen[fact], "observed"))
        merged.append(Deployment(backend.name, observed.id, canonical, facts, conflicts))
    return merged


def stale_overrides(
    backend: Backend, probe: ProbeResult, knowledge: ModelKnowledge
) -> list[str]:
    """Deployment overrides that matched nothing the backend is serving now.

    Only for a live probe: a static list or a down endpoint is no evidence
    about what the deployment holds.
    """
    if probe.status != "live":
        return []
    served = {model.id for model in probe.models}
    return [
        f"deployment override for {backend.name}/{served_id} "
        f"matches no served model (redeployed?)"
        for served_id in knowledge.deployments.get(backend.name, {})
        if served_id not in served
    ]
