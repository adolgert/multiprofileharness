"""Shared data model.

Vocabulary, fixed from the first commit: a *backend* is a reachable thing you
send tokens to, a *credential* is the named secret it requires, a *project* is
the policy that joins them, a *model* is the abstract thing facts attach to,
and a *deployment* is one backend's serving of one model.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path


def config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "multiagent"


def credentials_dir() -> Path:
    return config_home() / "credentials"


def state_home() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "multiagent"


# Fact keys carried through the merge, in table display order.
FACTS = (
    "context",
    "max_output",
    "tools",
    "vision",
    "mode",
    "input_per_mtok",
    "output_per_mtok",
)

# Our fact name -> LiteLLM's spelling for it, in one place because the catalog
# we read and the proxy config we write use the same vocabulary in opposite
# directions: merge.py reads this right-to-left, render.py left-to-right.
LITELLM_FIELDS = {
    "context": "max_input_tokens",
    "max_output": "max_output_tokens",
    "tools": "supports_function_calling",
    "vision": "supports_vision",
    "mode": "mode",
}
# Same, except LiteLLM states prices per token and we state them per million;
# the 1e6 belongs with whoever converts, not with the table.
LITELLM_PRICES = {
    "input_per_mtok": "input_cost_per_token",
    "output_per_mtok": "output_cost_per_token",
}


class ConfigError(Exception):
    """A shared config file is malformed or inconsistent."""


@dataclass
class Backend:
    name: str
    type: str  # ollama | openai-compat | gemini | anthropic | bedrock
    api_base: str | None = None
    discovery: str = "static"  # static | dynamic
    credential: str | None = None  # credential NAME, resolved locally; never a value
    models: list[str] = field(default_factory=list)  # static served ids
    region: str | None = None
    extra: dict = field(default_factory=dict)  # passed through into litellm_params


@dataclass
class Project:
    name: str
    backends: list[str]
    model_filter: list[str] | None = None  # canonical names
    default_model: str | None = None


@dataclass
class ModelEntry:
    match: list[str] = field(default_factory=list)  # fnmatch patterns over served ids
    facts: dict = field(default_factory=dict)
    catalog_key: str | None = None  # explicit key into the vendored catalog


@dataclass
class ModelKnowledge:
    models: dict[str, ModelEntry] = field(default_factory=dict)  # canonical name -> entry
    # backend name -> served id -> facts
    deployments: dict[str, dict[str, dict]] = field(default_factory=dict)

    def canonical_for(self, served_id: str) -> str | None:
        """The entry whose matching pattern is most specific, longest pattern first.

        Specificity is length, so `llava:34b` beats the `llava:*` that would
        otherwise shadow it whatever order the file lists them in.
        """
        best, best_length = None, -1
        for name, entry in self.models.items():
            for pattern in entry.match:
                if fnmatch(served_id, pattern) and len(pattern) > best_length:
                    best, best_length = name, len(pattern)
        return best


@dataclass
class Config:
    backends: dict[str, Backend]
    projects: dict[str, Project]
    knowledge: ModelKnowledge
    catalog: dict  # vendored LiteLLM price/context table, possibly empty


@dataclass
class ObservedModel:
    id: str
    facts: dict = field(default_factory=dict)  # only facts the probe actually saw


@dataclass
class ProbeResult:
    status: str  # live | down | static
    models: list[ObservedModel] = field(default_factory=list)
    error: str | None = None


@dataclass
class Conflict:
    fact: str
    believed: object
    observed: object
    winner: str  # "observed" or "override": which value the merge actually used


@dataclass
class Deployment:
    backend: str
    served_id: str
    canonical: str | None  # None: probed but models.yaml has no entry
    facts: dict = field(default_factory=dict)
    conflicts: list[Conflict] = field(default_factory=list)
