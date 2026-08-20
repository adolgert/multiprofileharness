"""`ma run` where there is no container engine: proxy as a host process, or none.

The container is a packaging choice, not the architecture, so the rendered
config has to be applicable two weaker ways — and both are weaker in ways the
user is told about at every launch rather than in a README:

* **process** — the proxy runs beside the agent under the same uid, so
  `/proc/<pid>/environ` is readable and the secret isolation is a raised bar,
  not a wall. The agent still gets exactly one credential of its own: the
  per-launch master key.
* **none** — no proxy at all, provider keys exported straight into the agent.
  Last resort, never silent.

Nothing here reaches into the launcher's own environment: every child gets an
explicit `env=`, built from a copy.
"""
from __future__ import annotations

import argparse
import os
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib import request

from . import credentials
from .types import Backend, ConfigError

# Every library that might do TLS reads a different one of these.
CA_VARS = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "AWS_CA_BUNDLE")

# The container entrypoint's budget, kept identical: the same proxy takes the
# same time to answer whichever side of a container boundary it starts on.
LIVENESS_TIMEOUT = 60.0
POLL = 0.5
STOP_GRACE = 5.0

RULE = "=" * 72

PROCESS_CAVEAT = """\
multiagent: --topology process runs the proxy as a host process under your own
uid, so a determined agent can read the provider keys out of its environment.
That is a raised bar, not the container's wall. No usage ledger either: the
accounting callback and /var/ma-usage live in the agent image."""


def free_port() -> int:
    """A port nothing is listening on right now.

    Racy by construction — the port is released before litellm binds it — but
    the alternative is passing a socket into a program that wants a number.
    """
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _litellm_binary() -> str:
    found = shutil.which("litellm")
    if found is None:
        raise ConfigError(
            "no `litellm` on PATH, and --topology process runs the proxy as a "
            "host process. Install it with `uv tool install 'litellm[proxy]'`, "
            "or from the vendored wheels where this machine has no index "
            "(`uv tool install --offline --find-links <wheels> 'litellm[proxy]'`)."
        )
    return found


def _healthy(url: str) -> bool:
    try:
        with request.urlopen(url, timeout=2) as response:
            return response.status < 400
    except Exception:
        return False


def _wait_healthy(proxy, url: str) -> str | None:
    """None once the proxy answers; otherwise why it never did."""
    deadline = time.monotonic() + LIVENESS_TIMEOUT
    while time.monotonic() < deadline:
        if _healthy(url):
            return None
        if proxy.poll() is not None:
            return f"the proxy exited with status {proxy.returncode} before answering"
        time.sleep(POLL)
    return f"the proxy was not healthy after {LIVENESS_TIMEOUT:.0f}s"


def _stop(proxy) -> None:
    """Ask, then insist. A proxy outliving its launch dir holds a dead config."""
    if proxy.poll() is not None:
        return
    proxy.terminate()
    try:
        proxy.wait(timeout=STOP_GRACE)
    except subprocess.TimeoutExpired:
        proxy.kill()
        proxy.wait()


def _proxy_env(
    args: argparse.Namespace, env_vars: dict[str, str], master_key: str
) -> dict[str, str]:
    """The proxy's environment: a copy of ours plus the namespaced secrets.

    Proxy variables (HTTP_PROXY and friends) need no forwarding here — unlike
    the container, this child inherits them by copy.
    """
    env = {
        **os.environ,
        **env_vars,
        "LITELLM_MASTER_KEY": master_key,
        # Without this litellm fetches its price map from GitHub at import; the
        # vendored catalog is the price source anyway (see docker/Dockerfile).
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
    }
    if args.ca_bundle:
        bundle = str(Path(args.ca_bundle).resolve())
        env.update({name: bundle for name in CA_VARS})
    return env


def _agent_env(
    args: argparse.Namespace,
    env_vars: dict[str, str],
    key_env: dict[str, dict[str, str]],
    base_url: str,
    master_key: str,
    default_model: str | None,
) -> dict[str, str]:
    """What the agent gets: one credential, worthless outside this launch.

    Our own environment may already hold the very provider variables this
    launch is isolating — a sourced .env, an exported key — and there is no
    entrypoint to scrub them here, so they are dropped by name on the way in,
    under both the original and the namespaced spelling.
    """
    provider = set(env_vars) | {name for names in key_env.values() for name in names}
    env = {k: v for k, v in os.environ.items() if k not in provider}
    env.pop("MA_SCRUB", None)  # nothing to scrub, and a stale list misleads
    env["OPENAI_BASE_URL"] = env["OPENAI_API_BASE"] = base_url
    env["OPENAI_API_KEY"] = master_key
    env["MA_PROJECT"] = args.project
    if default_model:
        env["MA_DEFAULT_MODEL"] = default_model
    return env


def run_process(
    args: argparse.Namespace,
    launch_dir: Path,
    config_yaml: str,
    env_vars: dict[str, str],
    key_env: dict[str, dict[str, str]],
    default_model: str | None,
) -> int:
    """Start litellm on a free loopback port, wait for it, run the agent."""
    try:
        binary = _litellm_binary()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2

    port = free_port()
    base_url = f"http://127.0.0.1:{port}/v1"
    argv = [
        binary, "--config", str(launch_dir / "config.yaml"),
        "--host", "127.0.0.1", "--port", str(port),
    ]
    master_key = secrets.token_urlsafe(32)

    if args.dry_run:
        print(config_yaml)
        print(shlex.join(argv))
        print(shlex.join(args.agent))
        print("(dry run: no proxy was started and no secret was written anywhere)")
        return 0

    print(PROCESS_CAVEAT, file=sys.stderr)
    proxy = subprocess.Popen(argv, env=_proxy_env(args, env_vars, master_key))
    reason = _wait_healthy(proxy, f"http://127.0.0.1:{port}/health/liveliness")
    if reason is not None:
        _stop(proxy)
        print(f"multiagent: {reason}; the agent did not start.", file=sys.stderr)
        print(
            "multiagent: its output is above; the config it read was "
            f"{launch_dir / 'config.yaml'} (deleted with this launch).",
            file=sys.stderr,
        )
        return 2

    agent_env = _agent_env(args, env_vars, key_env, base_url, master_key, default_model)
    try:
        return subprocess.run(args.agent, env=agent_env).returncode
    finally:
        _stop(proxy)


def _direct_values(
    args: argparse.Namespace, usable: list[Backend]
) -> tuple[dict[str, str], list[str]]:
    """Credential values under their ORIGINAL names, and collision warnings.

    Namespacing is what keeps two accounts apart, and this mode has none of it:
    `AWS_*` is process-global, so two Bedrock backends in one project means one
    of them signs for both.
    """
    values: dict[str, str] = {}
    owners: dict[str, list[str]] = {}
    for backend in usable:
        if not backend.credential:
            continue
        for var, value in (credentials.resolve(backend.credential, args.cred_dir) or {}).items():
            values[var] = value
            owners.setdefault(var, []).append(backend.name)
    warnings = [
        f"{var} is defined by backends {', '.join(who)}; with no proxy to keep "
        f"them apart, whichever value was read last wins for all of them"
        for var, who in sorted(owners.items())
        if len(who) > 1
    ]
    return values, warnings


def _direct_banner(project: str, names: list[str]) -> str:
    listed = ", ".join(names) or "nothing (no backend here needs a credential)"
    return "\n".join(
        [
            RULE,
            "multiagent: --topology none — NO PROXY. The agent holds the keys.",
            "This launch gives up, exactly:",
            "  * secret isolation: provider variables go into the agent's own",
            "    environment, where anything it runs can read and send them.",
            f"    This launch exports: {listed}.",
            "  * canonical model names: the agent must name each provider's own",
            "    model ids; the names in models.yaml route nowhere this session.",
            f"  * policy: project {project!r} now decides only WHICH credentials",
            "    are exported. Nothing stops the agent from reaching any model",
            "    those credentials can reach.",
            "  * the usage ledger: no tokens, no cost, no record of this session.",
            "Prefer --topology process, or container, wherever either one runs.",
            RULE,
        ]
    )


def run_direct(
    args: argparse.Namespace, usable: list[Backend], default_model: str | None
) -> int:
    """The last resort: provider variables exported straight into the agent."""
    try:
        values, warnings = _direct_values(args, usable)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2

    print(_direct_banner(args.project, sorted(values)), file=sys.stderr)
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)

    if args.dry_run:
        print(shlex.join(args.agent))
        print(f"(dry run: would export {', '.join(sorted(values)) or 'nothing'})")
        return 0

    env = {**os.environ, **values, "MA_PROJECT": args.project}
    if default_model:
        env["MA_DEFAULT_MODEL"] = default_model
    return subprocess.run(args.agent, env=env).returncode
