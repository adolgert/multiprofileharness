"""Per-machine secrets: `<credentials_dir>/<name>.env`, referenced by name only.

Nothing here is ever written back to the shared config, and no value is ever
put in an error message.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .types import Backend, credentials_dir

_EXPIRES = "# expires:"


def parse_env_file(text: str) -> dict[str, str]:
    """KEY=value lines. Blank lines and '#' comments ignored; values may contain '='."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep:
            continue
        out[key.strip()] = value.strip()
    return out


def _path(name: str, cred_dir: Path | None) -> Path:
    return (cred_dir or credentials_dir()) / f"{name}.env"


def resolve(name: str, cred_dir: Path | None = None) -> dict[str, str] | None:
    """Read a named credential file, or None if this machine doesn't have it."""
    path = _path(name, cred_dir)
    if not path.exists():
        return None
    return parse_env_file(path.read_text())


def expires_at(text: str) -> datetime | None:
    """The `# expires:` timestamp a short-term credential file carries, if any.

    Always returned timezone-aware; a stamp without an offset is read as UTC,
    which is what every AWS credential process emits.
    """
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith(_EXPIRES):
            continue
        stamp = line[len(_EXPIRES):].strip()
        if stamp.endswith(("Z", "z")):
            stamp = stamp[:-1] + "+00:00"
        try:
            moment = datetime.fromisoformat(stamp)
        except ValueError:
            return None  # an unreadable stamp is not evidence of expiry
        return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
    return None


def status(backend: Backend, cred_dir: Path | None = None) -> str:
    """'none' (backend needs no credential), 'ok', 'stale', or 'missing'."""
    if backend.credential is None:
        return "none"
    path = _path(backend.credential, cred_dir)
    if not path.exists():
        return "missing"
    expires = expires_at(path.read_text())
    if expires is not None and expires <= datetime.now(timezone.utc):
        return "stale"
    return "ok"
