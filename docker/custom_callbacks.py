"""Usage accounting for one launch, written by the proxy.

Best effort by design: an accounting failure must never fail the request it was
describing, so every path here swallows its errors. Nothing written names a
prompt, a completion, or an environment value — the file is a ledger, not a log.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from litellm.integrations.custom_logger import CustomLogger

USAGE_PATH = "/var/ma-usage/usage.jsonl"


def _tokens(response_obj):
    usage = response_obj.get("usage") if isinstance(response_obj, dict) else None
    if usage is None:
        usage = getattr(response_obj, "usage", None)
    if usage is None:
        return None, None
    if isinstance(usage, dict):
        return usage.get("prompt_tokens"), usage.get("completion_tokens")
    return getattr(usage, "prompt_tokens", None), getattr(usage, "completion_tokens", None)


def _record(kwargs, response_obj) -> None:
    try:
        prompt_tokens, completion_tokens = _tokens(response_obj)
        entry = {
            "at": datetime.now(timezone.utc).isoformat(),
            "project": os.environ.get("MA_PROJECT"),
            "model": kwargs.get("model"),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cost": kwargs.get("response_cost"),
        }
        with open(USAGE_PATH, "a") as ledger:
            ledger.write(json.dumps(entry) + "\n")
    except Exception:
        pass


class UsageLogger(CustomLogger):
    def log_success_event(self, kwargs, response_obj, start_time, end_time):
        _record(kwargs, response_obj)

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        _record(kwargs, response_obj)


# The rendered config names this object by module path; keep both names.
proxy_handler_instance = UsageLogger()
