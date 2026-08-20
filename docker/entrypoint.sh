#!/bin/sh
# Start the proxy holding the provider secrets, wait for it, drop the secrets,
# then become the agent. The agent inherits exactly one credential: the
# per-launch master key, which is worth nothing outside this container.
set -eu

litellm --config /run/ma/config.yaml --host 127.0.0.1 --port 4000 &

# python3 rather than curl: the slim image has no curl and we are not adding one.
if ! python3 - <<'PY'
import sys, time, urllib.request

deadline = time.time() + 60
while time.time() < deadline:
    try:
        with urllib.request.urlopen(
            "http://127.0.0.1:4000/health/liveliness", timeout=2
        ) as response:
            if response.status < 400:
                sys.exit(0)
    except Exception:
        pass
    time.sleep(0.5)
sys.exit(1)
PY
then
    echo "multiagent: the proxy was not healthy after 60s; the agent did not start." >&2
    echo "multiagent: its output is above; the config it read is /run/ma/config.yaml." >&2
    exit 1
fi

# Every provider secret named by the launcher goes now. What the proxy already
# read stays in the proxy's own environment, not ours.
old_ifs="$IFS"
IFS=','
for name in ${MA_SCRUB:-}; do
    if [ -n "$name" ]; then
        unset "$name" || true
    fi
done
IFS="$old_ifs"
unset old_ifs
unset MA_SCRUB || true

# Agents speak OpenAI-compatible to the local proxy, under canonical model names.
OPENAI_BASE_URL="http://127.0.0.1:4000/v1"
OPENAI_API_BASE="$OPENAI_BASE_URL"
OPENAI_API_KEY="$LITELLM_MASTER_KEY"
export OPENAI_BASE_URL OPENAI_API_BASE OPENAI_API_KEY

if [ "$#" -eq 0 ]; then
    set -- bash
fi
exec "$@"
