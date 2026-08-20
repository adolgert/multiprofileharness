"""`ma keys`: the morning ritual, short-term AWS credentials into a file.

The AWS CLI is shelled out to rather than reimplemented: SSO logins, MFA
prompts, and role chains are already configured in the user's AWS profiles, and
`aws configure export-credentials` is the supported way to ask for the result.
Values live in memory and in one 0600 file; nothing here prints or logs them,
and no error message quotes the CLI's stdout, which is where the secret is.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .types import ConfigError, credentials_dir

HINT = "install awscli or check the profile"


def fetch(profile: str) -> dict:
    """Short-term credentials for an AWS profile, as the CLI reports them."""
    argv = [
        "aws", "configure", "export-credentials",
        "--profile", profile,
        "--format", "process",
    ]
    try:
        result = subprocess.run(argv, capture_output=True, text=True)
    except OSError as exc:  # no `aws` on PATH, or it is not executable
        raise ConfigError(f"cannot run 'aws' for profile {profile!r} ({exc}); {HINT}") from None

    if result.returncode != 0:
        detail = " ".join((result.stderr or "").split())[:300]
        raise ConfigError(
            f"aws export-credentials failed for profile {profile!r}"
            + (f": {detail}" if detail else "")
            + f"; {HINT}"
        )

    try:
        creds = json.loads(result.stdout)
    except ValueError:
        raise ConfigError(
            f"aws export-credentials returned no usable JSON for profile {profile!r}; {HINT}"
        ) from None

    missing = [f for f in ("AccessKeyId", "SecretAccessKey") if not creds.get(f)]
    if missing:
        raise ConfigError(
            f"aws export-credentials for profile {profile!r} omitted {', '.join(missing)}; {HINT}"
        )
    return creds


def write_credential(name: str, creds: dict, cred_dir: Path | None = None) -> Path:
    """Write `<cred_dir>/<name>.env`, mode 0600, and return its path."""
    directory = cred_dir or credentials_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.env"

    lines = []
    if creds.get("Expiration"):
        lines.append(f"# expires: {creds['Expiration']}")
    lines.append(f"AWS_ACCESS_KEY_ID={creds['AccessKeyId']}")
    lines.append(f"AWS_SECRET_ACCESS_KEY={creds['SecretAccessKey']}")
    if creds.get("SessionToken"):
        lines.append(f"AWS_SESSION_TOKEN={creds['SessionToken']}")

    # Private before it holds anything: an existing file may carry looser bits
    # from an earlier tool, so set the mode either way, then fill it.
    path.touch(mode=0o600)
    path.chmod(0o600)
    path.write_text("\n".join(lines) + "\n")
    return path
