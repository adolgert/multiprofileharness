"""`ma run`: the models pipeline, then a proxy and an agent.

The container topology is the one described below and the default. The two
weaker ones — proxy as a host process, or no proxy at all — live in
topology.py; everything up to the rendered config is shared with them.

Secrets travel exactly one way — credential file, per-launch env file under
`$XDG_RUNTIME_DIR`, engine `--env-file`, the proxy process inside the
container — and the entrypoint drops them before the agent starts. So: no value
ever reaches argv (which `ps` shows to every user on the box), the rendered
config carries only `os.environ/NAME` references, and the env file is never
mounted — only the rendered config is.

Every credential variable is renamed `MA_<BACKEND>_<VAR>` on the way in, so no
two backends can collide on a variable name. `AWS_*` is process-global: two
Bedrock accounts under those names means one silently signs for both.
"""
from __future__ import annotations

import argparse
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from . import credentials, registry, snapshot, topology
from .merge import merge_backend, stale_overrides
from .probe import probe_backend
from .render import render_config, to_yaml
from .topology import CA_VARS
from .types import (
    Backend,
    Config,
    ConfigError,
    Deployment,
    ProbeResult,
    state_home,
)

# Addresses that mean "this machine" on the host mean the container itself
# inside one, so they are rewritten to the gateway alias docker run publishes.
LOOPBACK = ("localhost", "127.0.0.1")
CONTAINER_HOST = "host.docker.internal"

CONFIG_IN_CONTAINER = "/run/ma/config.yaml"
CA_IN_CONTAINER = "/run/ma/ca.pem"
PROXY_VARS = (
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "no_proxy",
)


@dataclass
class Pipeline:
    """Everything both `ma models` and `ma run` learn before they diverge."""

    config: Config
    backends: list[Backend]
    cred_status: dict[str, str]  # backend name -> none | ok | stale | missing
    probes: dict[str, ProbeResult]  # only backends whose credential resolved
    changes: dict[str, list[str]]  # backend name -> notes since last look
    merged: dict[str, list[Deployment]]
    stale: list[str]  # deployment overrides matching nothing served now


def namespaced(backend: str, var: str) -> str:
    """`MA_<BACKEND>_<VAR>`: the name this credential variable travels under."""
    return f"MA_{re.sub(r'[^A-Za-z0-9]', '_', backend).upper()}_{var}"


def _probe_key(backend: Backend, cred_dir: Path | None) -> str | None:
    """The bearer token a dynamic probe should send, if the backend has one."""
    if backend.discovery != "dynamic" or not backend.credential:
        return None
    values = credentials.resolve(backend.credential, cred_dir)
    if not values:
        return None
    return values[credentials.key_var(values, source=f"credential {backend.credential!r}")]


def pipeline(args: argparse.Namespace, probe=None) -> Pipeline:
    """Load, select, resolve credentials, probe, diff the diary, merge facts.

    `probe` is a parameter only so callers own the one step that touches the
    network. Raises ConfigError for anything the user must fix in a file.
    """
    probe = probe_backend if probe is None else probe
    config = registry.apply_machine(registry.load_config(args.config), args.machine)

    if args.project is None:
        backends = list(config.backends.values())
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
        b.name: probe(b, api_key=_probe_key(b, args.cred_dir))
        for b in backends
        if cred_status[b.name] != "missing"
    }

    previous = snapshot.load(args.state)
    current = snapshot.observed_state(probes)
    changes = snapshot.diff(previous, current)
    snapshot.save(args.state, {**previous, **current})

    merged: dict[str, list[Deployment]] = {}
    stale: list[str] = []
    model_filter = None if args.project is None else project.model_filter
    for backend in backends:
        result = probes.get(backend.name)
        if result is None or result.status == "down":
            continue
        stale += stale_overrides(backend, result, config.knowledge)
        models = merge_backend(backend, result, config.knowledge, config.catalog)
        if model_filter is not None:
            models = [m for m in models if m.canonical in model_filter]
        merged[backend.name] = models

    return Pipeline(config, backends, cred_status, probes, changes, merged, stale)


def for_container(backend: Backend) -> Backend:
    """A copy of `backend` addressed from inside the container."""
    if not backend.api_base:
        return backend
    parts = urlsplit(backend.api_base)
    if parts.hostname not in LOOPBACK:
        return backend
    netloc = CONTAINER_HOST if parts.port is None else f"{CONTAINER_HOST}:{parts.port}"
    return replace(backend, api_base=urlunsplit(parts._replace(netloc=netloc)))


def _launch_root() -> Path | None:
    """Where per-launch directories go: tmpfs, per-user, cleared at logout.

    Falling back to the system tempdir keeps the tool working where the runtime
    dir is not set, at the cost of secrets that outlive a crash until reboot.
    """
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime:
        return None
    root = Path(runtime) / "multiagent"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root


def _write_launch_dir(config_yaml: str, env_vars: dict[str, str] | None) -> Path:
    """The per-launch directory: the config to mount, the env file docker reads.

    `env_vars` is None when no env file should exist: on a dry run, which prints
    a plan rather than running it, and in the process topology, where the
    secrets go straight into the proxy's process environment instead.
    """
    launch_dir = Path(tempfile.mkdtemp(prefix="ma-", dir=_launch_root()))
    (launch_dir / "config.yaml").write_text(config_yaml)
    if env_vars is None:
        return launch_dir

    env_path = launch_dir / "env"
    env_path.touch(mode=0o600)  # created empty and private, then filled
    lines = [f"{name}={value}" for name, value in sorted(env_vars.items())]
    lines.append(f"LITELLM_MASTER_KEY={secrets.token_urlsafe(32)}")
    env_path.write_text("\n".join(lines) + "\n")
    return launch_dir


def _docker_argv(
    args: argparse.Namespace,
    launch_dir: Path,
    scrub: list[str],
    default_model: str | None,
) -> list[str]:
    # Per project: one project's spend history is not another's business, and a
    # shared ledger mount is a cross-project channel for free.
    usage_dir = state_home() / "usage" / args.project
    usage_dir.mkdir(parents=True, exist_ok=True)
    argv = [
        args.engine, "run", "--rm", "-i",
        *(["-t"] if sys.stdin.isatty() else []),
        # A packaging boundary, not a root playground: workspace files it writes
        # belong to the user.
        "--user", f"{os.getuid()}:{os.getgid()}",
        "--cap-drop=ALL",
        "--add-host", f"{CONTAINER_HOST}:host-gateway",
        "--env-file", str(launch_dir / "env"),
        "-e", f"MA_SCRUB={','.join(scrub)}",
        "-e", f"MA_PROJECT={args.project}",
        # litellm caches under $HOME, and the image's /root is closed to our uid.
        "-e", "HOME=/tmp",
    ]
    if default_model:
        argv += ["-e", f"MA_DEFAULT_MODEL={default_model}"]
    for name in PROXY_VARS:
        value = os.environ.get(name)
        if value:
            argv += ["-e", f"{name}={value}"]
    if args.ca_bundle:
        argv += ["-v", f"{Path(args.ca_bundle).resolve()}:{CA_IN_CONTAINER}:ro"]
        argv += [a for name in CA_VARS for a in ("-e", f"{name}={CA_IN_CONTAINER}")]
    argv += [
        # ONLY the config. The launch directory also holds the env file, and
        # mounting it would let the agent read every secret in the project.
        "-v", f"{launch_dir / 'config.yaml'}:{CONFIG_IN_CONTAINER}:ro",
        "-v", f"{Path.cwd()}:/workspace",
        "-v", f"{usage_dir}:/var/ma-usage",
        "-w", "/workspace",
        args.image,
        *args.agent,
    ]
    return argv


def _report(state: Pipeline, args: argparse.Namespace) -> list[Backend]:
    """Say what the launch is dropping and what drifted; return what remains."""
    usable = []
    for backend in state.backends:
        status = state.cred_status[backend.name]
        if status == "missing":
            # The <dir>/<name>.env convention lives in credentials.py; borrow it
            # rather than restate it here.
            print(
                f"WARNING: backend {backend.name!r} needs credential "
                f"{backend.credential!r}, which this machine does not have; "
                f"dropping it from this launch. Create "
                f"{credentials.path(backend.credential, args.cred_dir)} to use it.",
                file=sys.stderr,
            )
            continue
        # Expired is not missing: the key may still answer, and a launch cut
        # short mid-session is worse than one that starts with a warning.
        if status == "stale":
            print(
                f"WARNING: credential {backend.credential!r} for backend "
                f"{backend.name!r} has expired; run `ma keys {backend.credential}` "
                f"to refresh it. Starting anyway.",
                file=sys.stderr,
            )
        # A down backend is not fatal — the rest of the project still works —
        # but the session is missing models, so say which and why.
        result = state.probes[backend.name]
        if result.status == "down":
            print(
                f"warning: backend {backend.name!r} is down, serving nothing "
                f"this session: {result.error}",
                file=sys.stderr,
            )
        usable.append(backend)

    for note in state.stale:
        print(f"warning: {note}", file=sys.stderr)
    for name, lines in state.changes.items():
        for line in lines:
            print(f"change {name}: {line}", file=sys.stderr)
    return usable


def launch(args: argparse.Namespace) -> int:
    try:
        state = pipeline(args)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2

    usable = _report(state, args)
    merged = [m for b in usable for m in state.merged.get(b.name, [])]
    if not merged:
        print(
            f"project {args.project!r} selects no models: every backend it names is "
            f"missing a credential, down, or filtered out. Nothing to launch.",
            file=sys.stderr,
        )
        return 2

    project = state.config.projects.get(args.project)
    default_model = project and project.default_model
    if args.topology == "none":
        return topology.run_direct(args, usable, default_model)

    in_container = args.topology == "container"
    env_vars: dict[str, str] = {}
    key_env: dict[str, dict[str, str]] = {}
    # Loopback stays loopback outside a container: nothing to reach across.
    backends = {b.name: for_container(b) if in_container else b for b in usable}
    try:
        for backend in usable:
            if not backend.credential:
                continue
            values = credentials.resolve(backend.credential, args.cred_dir) or {}
            names = {var: namespaced(backend.name, var) for var in values}
            env_vars.update({names[var]: value for var, value in values.items()})
            key_env[backend.name] = names
        config = render_config(backends, merged, key_env)
        if not in_container:
            # The accounting callback module and /var/ma-usage exist only in the
            # image; naming a module the proxy cannot import would fail its start.
            config.get("litellm_settings", {}).pop("callbacks", None)
        config_yaml = to_yaml(config)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2

    write_env_file = in_container and not args.dry_run
    launch_dir = _write_launch_dir(config_yaml, env_vars if write_env_file else None)
    try:
        if args.topology == "process":
            return topology.run_process(
                args, launch_dir, config_yaml, env_vars, key_env, default_model
            )
        argv = _docker_argv(args, launch_dir, sorted(env_vars), default_model)
        if args.dry_run:
            print(config_yaml)
            print(shlex.join(argv))
            print("(dry run: no env file was written, so this command cannot run as-is)")
            return 0
        return subprocess.run(argv).returncode
    finally:
        # The env file is the only copy of the master key; it dies with the run.
        shutil.rmtree(launch_dir, ignore_errors=True)
