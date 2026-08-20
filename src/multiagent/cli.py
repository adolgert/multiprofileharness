"""The `ma` command line.

`ma models` is the launch pipeline stopped just before the proxy would start:
load config, pick the project's backends, resolve credentials, probe, diff
against the diary, merge facts, print. Nothing here decides anything; it only
shows what a launch would be working with. `ma run` continues from the same
point into a container (see run.py).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import keys, run
from .probe import probe_backend
from .types import ConfigError, config_home, state_home

DASH = "—"
COLUMNS = ("backend", "credential", "status", "model", "ctx", "tools", "price", "note")
STATUS = {"live": "live", "down": "down", "static": "ok"}


def format_table(headers, rows) -> str:
    """Left-aligned columns, two spaces apart, no trailing whitespace."""
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    lines = []
    for row in (headers, *rows):
        lines.append("  ".join(c.ljust(widths[i]) for i, c in enumerate(row)).rstrip())
    return "\n".join(lines)


def _number(value) -> str:
    # %g turns large context windows into scientific notation; keep integers whole.
    if isinstance(value, float) and value == int(value):
        value = int(value)
    return str(value)


def _ctx(facts: dict) -> str:
    value = facts.get("context")
    return DASH if value is None else _number(value)


def _tools(facts: dict) -> str:
    value = facts.get("tools")
    if value is None:
        return DASH
    return "yes" if value else "no"


def _price(facts: dict) -> str:
    into, out = facts.get("input_per_mtok"), facts.get("output_per_mtok")
    if into is None or out is None:
        return DASH
    if not into and not out:
        return "free"
    return f"${_number(into)}/${_number(out)} per Mtok"


def _short(error: str | None) -> str:
    if not error:
        return ""
    return error.splitlines()[0][:60]


def _join(*parts: str) -> str:
    return "; ".join(p for p in parts if p)


def _models(args: argparse.Namespace) -> int:
    try:
        # Passed explicitly: probing is the one step that touches the network,
        # so it is the one a caller may want to stand in for.
        state = run.pipeline(args, probe_backend)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2

    changes = state.changes
    rows = []
    conflicts = []
    for backend in state.backends:
        name = backend.name
        credential = backend.credential or "(none)"
        change = changes.get(name, [])
        # One note reads fine in the table; several go below it instead.
        change_cell = ""
        if len(change) == 1:
            change_cell = change[0]
        elif change:
            change_cell = f"changed ({len(change)})"

        if state.cred_status[name] == "missing":
            rows.append((name, credential, "NO KEY", DASH, DASH, DASH, DASH, change_cell))
            continue

        result = state.probes[name]
        if result.status == "down":
            rows.append(
                (name, credential, "down", DASH, DASH, DASH, DASH,
                 _join(_short(result.error), change_cell))
            )
            continue

        merged = state.merged[name]
        # An expired key still answers for a while; the loud cell is the warning,
        # so the backend is probed and rendered exactly as usual.
        status = (
            "STALE" if state.cred_status[name] == "stale"
            else STATUS.get(result.status, result.status)
        )
        if not merged:
            rows.append((name, credential, status, DASH, DASH, DASH, DASH, change_cell))
            continue

        for model in merged:
            note = _join(
                "no facts" if model.canonical is None else "",
                "*" if model.conflicts else "",
                change_cell,
            )
            change_cell = ""  # change notes belong to the backend, not each model
            rows.append(
                (
                    name,
                    credential,
                    status,
                    model.canonical or model.served_id,
                    _ctx(model.facts),
                    _tools(model.facts),
                    _price(model.facts),
                    note,
                )
            )
            conflicts += [(model, c) for c in model.conflicts]

    print(format_table(COLUMNS, rows))

    details = [
        f"conflict {m.backend}/{m.served_id} {c.fact}: "
        f"believed {c.believed}, observed {c.observed} (observed wins)"
        for m, c in conflicts
    ]
    for name, lines in changes.items():
        if len(lines) > 1:  # the single-note case is already in the table
            details += [f"change {name}: {line}" for line in lines]
    if details:
        print()
        for line in details:
            print(line)
    return 0


def _keys(args: argparse.Namespace) -> int:
    profile = args.profile or args.name
    try:
        creds = keys.fetch(profile)
        path = keys.write_credential(args.name, creds, args.cred_dir)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    expiry = creds.get("Expiration")
    print(f"{path}: {f'expires {expiry}' if expiry else 'long-term keys (no expiry)'}")
    return 0


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, default=Path("config"), help="shared config dir")
    parser.add_argument("--machine", type=Path, default=config_home() / "machine.yaml")
    parser.add_argument("--state", type=Path, default=state_home() / "last-seen.json")
    parser.add_argument("--cred-dir", type=Path, default=None, help="credential .env dir")


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Everything after `--` is the agent's command line, not ours; argparse must
    # never see it, or a `-p` meant for the agent becomes our option.
    agent = []
    if "--" in argv:
        cut = argv.index("--")
        argv, agent = argv[:cut], argv[cut + 1:]

    parser = argparse.ArgumentParser(prog="ma", description="Start AI agents.")
    sub = parser.add_subparsers(dest="command", required=True)

    models = sub.add_parser("models", help="show backends and the models they serve")
    models.add_argument("--project", help="project policy to apply (default: all backends)")
    _common(models)
    models.set_defaults(func=_models)

    runner = sub.add_parser(
        "run",
        help="start an agent against a project's models",
        description="Start the proxy and the agent in a container. "
        "Put the agent's command after `--`.",
    )
    # Required: a run without a policy is a run that can spend any credential
    # this machine holds.
    runner.add_argument("--project", required=True, help="project policy to apply")
    _common(runner)
    runner.add_argument("--image", default="multiagent", help="agent container image")
    runner.add_argument(
        "--dry-run",
        action="store_true",
        help="print the proxy config and the docker command, then stop",
    )
    runner.set_defaults(func=run.launch)

    keyer = sub.add_parser(
        "keys",
        help="write short-term AWS credentials into a credential file",
        description="Fetch credentials with the AWS CLI and store them as "
        "<cred-dir>/<name>.env.",
    )
    keyer.add_argument("name", help="credential name, i.e. the .env file's stem")
    keyer.add_argument("--profile", help="AWS profile to export (default: the credential name)")
    keyer.add_argument("--cred-dir", type=Path, default=None, help="credential .env dir")
    keyer.set_defaults(func=_keys)

    args = parser.parse_args(argv)
    args.agent = agent or ["bash"]
    return args.func(args)
