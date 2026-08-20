"""Per-machine secrets: `<credentials_dir>/<name>.env`, referenced by name only.

Nothing here is ever written back to the shared config, and no value is ever
put in an error message.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .types import Backend, ConfigError, credentials_dir

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


def path(name: str, cred_dir: Path | None = None) -> Path:
    """Where a credential of this name lives. Public: error messages need it."""
    return (cred_dir or credentials_dir()) / f"{name}.env"


def resolve(name: str, cred_dir: Path | None = None) -> dict[str, str] | None:
    """Read a named credential file, or None if this machine doesn't have it."""
    file = path(name, cred_dir)
    if not file.exists():
        return None
    return parse_env_file(file.read_text())


def key_var(values: dict[str, str], source: object = "the credential file") -> str:
    """The variable in a credential file that holds the key the proxy sends.

    Never guessed: guessing wrong means sending one third party's secret to
    another, so an ambiguous file is an error for the human to settle. Only
    variable NAMES reach the message.
    """
    names = sorted(values)
    api_keys = [n for n in names if n.endswith("_API_KEY")]
    if len(api_keys) == 1:
        return api_keys[0]
    if not api_keys and len(names) == 1:
        return names[0]
    raise ConfigError(
        f"cannot tell which variable in {source} holds the key to send: it defines "
        f"{names or 'nothing'}. Keep exactly one '*_API_KEY' variable per credential file."
    )


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
    file = path(backend.credential, cred_dir)
    if not file.exists():
        return "missing"
    expires = expires_at(file.read_text())
    if expires is not None and expires <= datetime.now(timezone.utc):
        return "stale"
    return "ok"
