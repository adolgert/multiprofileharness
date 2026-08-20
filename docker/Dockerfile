# The agent image: a LiteLLM proxy that holds the provider secrets and an agent
# that never sees them. Build from this directory:  docker build -t multiagent .
FROM python:3.12-slim

# Pinned pair: litellm 1.97's proxy imports fastapi internals that fastapi
# 0.141 removed (its own constraint still admits 0.141). Pins also make the
# image reproducible on a restricted network.
RUN pip install --no-cache-dir 'litellm[proxy]==1.97.0' 'fastapi==0.140.0' 'boto3==1.43.75'

COPY entrypoint.sh custom_callbacks.py /app/
RUN chmod +x /app/entrypoint.sh

# litellm resolves the callback by module name, from whatever directory the
# agent happens to run in.
ENV PYTHONPATH=/app

ENTRYPOINT ["/app/entrypoint.sh"]
