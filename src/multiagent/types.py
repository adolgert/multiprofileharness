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
    "tokenizer",
    "input_per_mtok",
    "output_per_mtok",
)


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


@dataclass
class Project:
    name: str
    backends: list[str]
    model_filter: list[str] | None = None  # canonical names
    default_model: str | None = None


@dataclass
class ModelEntry:
    name: str  # canonical name
    match: list[str] = field(default_factory=list)  # fnmatch patterns over served ids
    facts: dict = field(default_factory=dict)
    catalog_key: str | None = None  # explicit key into the vendored catalog


@dataclass
class ModelKnowledge:
    models: dict[str, ModelEntry] = field(default_factory=dict)
    # backend name -> served id -> facts
    deployments: dict[str, dict[str, dict]] = field(default_factory=dict)

    def canonical_for(self, served_id: str) -> str | None:
        for entry in self.models.values():
            if any(fnmatch(served_id, pat) for pat in entry.match):
                return entry.name
        return None


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


@dataclass
class MergedModel:
    backend: str
    served_id: str
    canonical: str | None  # None: probed but models.yaml has no entry
    facts: dict = field(default_factory=dict)
    conflicts: list[Conflict] = field(default_factory=list)
