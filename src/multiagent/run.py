"""`ma run`: the models pipeline, then a proxy and an agent in a container.

Secrets travel exactly one way — credential file, per-launch env file, docker
`--env-file`, the proxy process inside the container — and the entrypoint drops
them before the agent starts. So: no value ever reaches argv (which `ps` shows
to every user on the box), the rendered config carries only `os.environ/NAME`
references, and the launch directory lives outside the mounted workspace, which
is the agent's readable surface.
"""
from __future__ import annotations

import argparse
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from . import credentials, registry, snapshot
from .merge import merge_backend
from .probe import probe_backend
from .render import render_config, to_yaml
from .types import (
    Backend,
    Config,
    ConfigError,
    MergedModel,
    ProbeResult,
    Project,
    state_home,
)

# Addresses that mean "this machine" on the host mean the container itself
# inside one, so they are rewritten to the gateway alias docker run publishes.
LOOPBACK = ("localhost", "127.0.0.1")
CONTAINER_HOST = "host.docker.internal"


@dataclass
class Pipeline:
    """Everything both `ma models` and `ma run` learn before they diverge."""

    config: Config
    project: Project | None
    backends: list[Backend]
    cred_status: dict[str, str]  # backend name -> none | ok | missing
    probes: dict[str, ProbeResult]  # only backends whose credential resolved
    changes: dict[str, list[str]]  # backend name -> notes since last look
    merged: dict[str, list[MergedModel]] = field(default_factory=dict)


def pipeline(args: argparse.Namespace, probe=None) -> Pipeline:
    """Load, select, resolve credentials, probe, diff the diary, merge facts.

    `probe` is a parameter only so callers own the one step that touches the
    network. Raises ConfigError for anything the user must fix in a file.
    """
    probe = probe_backend if probe is None else probe
    config = registry.apply_machine(registry.load_config(args.config), args.machine)

    if args.project is None:
        project, backends = None, list(config.backends.values())
    else:
        project = config.projects.get(args.project)
        if project is None:
            raise ConfigError(
                f"unknown project {args.project!r}; "
                f"known projects: {sorted(config.projects)}"
            )
        backends = [config.backends[name] for name in project.backends]

    cred_status = {b.name: credentials.status(b, args.cred_dir) for b in backends}
    probes = {
        b.name: probe(b) for b in backends if cred_status[b.name] != "missing"
    }

    previous = snapshot.load(args.state)
    current = snapshot.observed_state(probes)
    changes = snapshot.diff(previous, current)
    snapshot.save(args.state, {**previous, **current})

    merged: dict[str, list[MergedModel]] = {}
    for backend in backends:
        result = probes.get(backend.name)
        if result is None or result.status == "down":
            continue
        models = merge_backend(backend, result, config.knowledge, config.catalog)
        if project is not None and project.model_filter is not None:
            models = [m for m in models if m.canonical in project.model_filter]
        merged[backend.name] = models

    return Pipeline(config, project, backends, cred_status, probes, changes, merged)


def key_var(values: dict[str, str]) -> str | None:
    """The variable in a credential file that holds the key the proxy sends."""
    names = sorted(values)
    return next((n for n in names if n.endswith("_API_KEY")), names[0] if names else None)


def for_container(backend: Backend) -> Backend:
    """A copy of `backend` addressed from inside the container."""
    if not backend.api_base:
        return backend
    parts = urlsplit(backend.api_base)
    if parts.hostname not in LOOPBACK:
        return backend
    netloc = CONTAINER_HOST if parts.port is None else f"{CONTAINER_HOST}:{parts.port}"
    return replace(backend, api_base=urlunsplit(parts._replace(netloc=netloc)))


def _write_launch_dir(config_yaml: str, env_vars: dict[str, str]) -> Path:
    """The per-launch directory: the config to mount, the env file docker reads."""
    launch_dir = Path(tempfile.mkdtemp(prefix="ma-"))
    (launch_dir / "config.yaml").write_text(config_yaml)

    env_path = launch_dir / "env"
    env_path.touch(mode=0o600)  # created empty and private, then filled
    lines = [f"{name}={value}" for name, value in sorted(env_vars.items())]
    lines.append(f"LITELLM_MASTER_KEY={secrets.token_urlsafe(32)}")
    env_path.write_text("\n".join(lines) + "\n")
    return launch_dir


def _docker_argv(args: argparse.Namespace, launch_dir: Path, scrub: list[str]) -> list[str]:
    usage_dir = state_home() / "usage"
    usage_dir.mkdir(parents=True, exist_ok=True)
    return [
        "docker", "run", "--rm", "-i",
        *(["-t"] if sys.stdin.isatty() else []),
        "--add-host", f"{CONTAINER_HOST}:host-gateway",
        "--env-file", str(launch_dir / "env"),
        "-e", f"MA_SCRUB={','.join(scrub)}",
        "-e", f"MA_PROJECT={args.project}",
        "-v", f"{launch_dir}:/run/ma:ro",
        "-v", f"{Path.cwd()}:/workspace",
        "-v", f"{usage_dir}:/var/ma-usage",
        "-w", "/workspace",
        args.image,
        *args.agent,
    ]


def launch(args: argparse.Namespace) -> int:
    try:
        state = pipeline(args)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2

    # A launch that quietly drops a backend spends the wrong money later; say so
    # now and stop.
    missing = [b for b in state.backends if state.cred_status[b.name] == "missing"]
    if missing:
        for backend in missing:
            # The <dir>/<name>.env convention lives in credentials.py; borrow it
            # rather than restate it here.
            path = credentials._path(backend.credential, args.cred_dir)
            print(
                f"backend {backend.name!r} needs credential "
                f"{backend.credential!r}: create {path}",
                file=sys.stderr,
            )
        return 2

    # Expired is not missing: the key may still answer, and a launch cut short
    # mid-session is worse than one that starts with a warning it can act on.
    for backend in state.backends:
        if state.cred_status[backend.name] == "stale":
            print(
                f"WARNING: credential {backend.credential!r} for backend "
                f"{backend.name!r} has expired; run `ma keys {backend.credential}` "
                f"to refresh it. Starting anyway.",
                file=sys.stderr,
            )

    # A down backend is not fatal — the rest of the project still works — but the
    # session is missing models, so say which and why.
    for backend in state.backends:
        result = state.probes[backend.name]
        if result.status == "down":
            print(
                f"warning: backend {backend.name!r} is down, serving nothing "
                f"this session: {result.error}",
                file=sys.stderr,
            )

    env_vars: dict[str, str] = {}
    key_env: dict[str, str] = {}
    for backend in state.backends:
        if not backend.credential:
            continue
        values = credentials.resolve(backend.credential, args.cred_dir) or {}
        env_vars.update(values)
        var = key_var(values)
        if var:
            key_env[backend.name] = var

    backends = {b.name: for_container(b) for b in state.backends}
    merged = [m for b in state.backends for m in state.merged.get(b.name, [])]
    try:
        config_yaml = to_yaml(render_config(backends, merged, key_env))
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2

    launch_dir = _write_launch_dir(config_yaml, env_vars)
    try:
        argv = _docker_argv(args, launch_dir, sorted(env_vars))
        if args.dry_run:
            print(config_yaml)
            print(shlex.join(argv))
            return 0
        return subprocess.run(argv).returncode
    finally:
        # The env file is the only copy of the master key; it dies with the run.
        shutil.rmtree(launch_dir, ignore_errors=True)
