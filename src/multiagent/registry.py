"""Load the shared, secret-free config: backends, projects, model knowledge.

Every error raised here names the file to edit, because the person reading it
is usually looking at a typo in YAML someone else committed.
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml

from .types import FACTS, Backend, Config, ConfigError, ModelEntry, ModelKnowledge, Project

BACKEND_KEYS = {"type", "api_base", "discovery", "credential", "models", "region"}
PROJECT_KEYS = {"backends", "model_filter", "default_model"}
MODEL_KEYS = {"match", "catalog_key"} | set(FACTS)


def _load_yaml(path: Path, *, required: bool) -> dict:
    if not path.exists():
        if required:
            raise ConfigError(f"missing config file: {path} (create it)")
        return {}
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a mapping at the top level")
    return data


def _section(data: dict, name: str, path: Path, allowed_top: set[str]) -> dict:
    unknown = set(data) - allowed_top
    if unknown:
        raise ConfigError(
            f"{path}: unknown top-level key {sorted(unknown)[0]!r}; "
            f"expected one of {sorted(allowed_top)}. Fix {path}."
        )
    section = data.get(name) or {}
    if not isinstance(section, dict):
        raise ConfigError(f"{path}: '{name}:' must be a mapping of name to entry")
    return section


def _entry(raw: object, path: Path, name: str) -> dict:
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: entry {name!r} must be a mapping, got {type(raw).__name__}")
    return raw


def _check_keys(raw: dict, allowed: set[str], path: Path, name: str) -> None:
    for key in raw:
        if key not in allowed:
            raise ConfigError(
                f"{path}: entry {name!r} has unknown key {key!r}; "
                f"allowed keys are {sorted(allowed)}. Fix {path}."
            )


def _str_list(value: object, path: Path, name: str, key: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{path}: entry {name!r} key {key!r} must be a list of strings")
    return list(value)


def load_config(config_dir: Path) -> Config:
    """Read the shared config directory into a Config. No secrets are touched."""
    backends_path = config_dir / "backends.yaml"
    projects_path = config_dir / "projects.yaml"
    models_path = config_dir / "models.yaml"
    catalog_path = config_dir / "catalog.json"

    backends: dict[str, Backend] = {}
    raw_backends = _section(
        _load_yaml(backends_path, required=True), "backends", backends_path, {"backends"}
    )
    for name, raw in raw_backends.items():
        raw = _entry(raw, backends_path, name)
        _check_keys(raw, BACKEND_KEYS, backends_path, name)
        if not raw.get("type"):
            raise ConfigError(f"{backends_path}: backend {name!r} is missing required key 'type'")
        backends[name] = Backend(
            name=name,
            type=raw["type"],
            api_base=raw.get("api_base"),
            discovery=raw.get("discovery", "static"),
            credential=raw.get("credential"),
            models=_str_list(raw.get("models", []), backends_path, name, "models"),
            region=raw.get("region"),
        )

    projects: dict[str, Project] = {}
    raw_projects = _section(
        _load_yaml(projects_path, required=True), "projects", projects_path, {"projects"}
    )
    for name, raw in raw_projects.items():
        raw = _entry(raw, projects_path, name)
        _check_keys(raw, PROJECT_KEYS, projects_path, name)
        if "backends" not in raw:
            raise ConfigError(
                f"{projects_path}: project {name!r} is missing required key 'backends'"
            )
        project_backends = _str_list(raw["backends"], projects_path, name, "backends")
        for backend_name in project_backends:
            if backend_name not in backends:
                raise ConfigError(
                    f"{projects_path}: project {name!r} names backend {backend_name!r}, "
                    f"which is not defined in {backends_path}. "
                    f"Known backends: {sorted(backends)}."
                )
        model_filter = raw.get("model_filter")
        if model_filter is not None:
            model_filter = _str_list(model_filter, projects_path, name, "model_filter")
        projects[name] = Project(
            name=name,
            backends=project_backends,
            model_filter=model_filter,
            default_model=raw.get("default_model"),
        )

    models_data = _load_yaml(models_path, required=False)
    knowledge = ModelKnowledge()
    for name, raw in _section(
        models_data, "models", models_path, {"models", "deployments"}
    ).items():
        raw = _entry(raw, models_path, name)
        _check_keys(raw, MODEL_KEYS, models_path, name)
        knowledge.models[name] = ModelEntry(
            name=name,
            match=_str_list(raw.get("match", []), models_path, name, "match"),
            facts={k: v for k, v in raw.items() if k in FACTS},
            catalog_key=raw.get("catalog_key"),
        )
    for backend_name, served in _section(
        models_data, "deployments", models_path, {"models", "deployments"}
    ).items():
        served = _entry(served, models_path, backend_name)
        if backend_name not in backends:
            raise ConfigError(
                f"{models_path}: deployments names backend {backend_name!r}, "
                f"which is not defined in {backends_path}."
            )
        knowledge.deployments[backend_name] = {}
        for served_id, facts in served.items():
            label = f"{backend_name}/{served_id}"
            facts = _entry(facts, models_path, label)
            _check_keys(facts, set(FACTS), models_path, label)
            knowledge.deployments[backend_name][served_id] = dict(facts)

    catalog: dict = {}
    if catalog_path.exists():
        try:
            catalog = json.loads(catalog_path.read_text())
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{catalog_path}: invalid JSON: {exc}") from exc

    return Config(backends=backends, projects=projects, knowledge=knowledge, catalog=catalog)


def apply_machine(config: Config, machine_path: Path) -> Config:
    """Apply per-machine address overrides in place and return the config.

    Only api_base is overridable: machine.yaml is per-machine plumbing, and
    policy (which backends, which credentials) must stay in the shared files.
    """
    data = _load_yaml(machine_path, required=False)
    overrides = _section(data, "overrides", machine_path, {"overrides"})
    for name, raw in overrides.items():
        raw = _entry(raw, machine_path, name)
        if name not in config.backends:
            raise ConfigError(
                f"{machine_path}: override for unknown backend {name!r}. "
                f"Known backends: {sorted(config.backends)}."
            )
        for key in raw:
            if key != "api_base":
                raise ConfigError(
                    f"{machine_path}: override for {name!r} sets {key!r}; "
                    f"only 'api_base' may be overridden per machine. Fix {machine_path}."
                )
        if "api_base" in raw:
            config.backends[name].api_base = raw["api_base"]
    return config
