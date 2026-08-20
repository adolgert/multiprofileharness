"""The observation diary.

What the probe saw last time, so a launch can say what changed since: an admin
swapped a model out, an endpoint went down. Nothing here feeds routing or
selection; it exists only to report drift back to the user.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .types import ProbeResult

_STAMP = "recorded_at"


def observed_state(results: dict[str, ProbeResult]) -> dict:
    """Reduce probe results to the part worth remembering."""
    return {
        name: {"status": result.status, "models": sorted(m.id for m in result.models)}
        for name, result in results.items()
    }


def load(path: Path) -> dict:
    """The last recorded state, or {} if there is nothing readable to load."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if k != _STAMP}


def save(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    stamped = dict(state)
    stamped[_STAMP] = datetime.now(timezone.utc).isoformat()
    text = json.dumps(stamped, indent=2, sort_keys=True) + "\n"
    # Two `ma run`s can start at once. A half-written diary parses as nothing,
    # and nothing reads as "first observation" for every backend, so the file
    # is replaced whole: same directory, so os.replace stays atomic.
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(handle, "w") as out:
            out.write(text)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def diff(prev: dict, current: dict) -> dict[str, list[str]]:
    """Human-readable change notes per backend, omitting the unchanged."""
    notes: dict[str, list[str]] = {}
    for name, now in current.items():
        if now.get("status") == "static":
            continue  # a static model list is config, not observation
        if name not in prev:
            notes[name] = ["first observation"]
            continue
        before = prev[name]
        was, is_now = before.get("status"), now.get("status")
        old_models, new_models = set(before.get("models", [])), set(now.get("models", []))
        lines = [f"new: {m}" for m in sorted(new_models - old_models)]
        lines += [f"gone: {m}" for m in sorted(old_models - new_models)]
        if was == "down" and is_now != "down":
            lines.append(f"was down, now {is_now}")
        elif was != "down" and is_now == "down":
            lines.append(f"went down (was {was})")
        if lines:
            notes[name] = lines
    return notes
