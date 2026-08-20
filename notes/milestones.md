# Milestones

Each milestone extends the launch pipeline one stage further and states what
can be exercised at home versus what waits for work. M1 and M2 are done; M3 is
done as far as home hardware allows.

## M1 — `ma models` (done)

Parse the four shared files, resolve credentials by name, probe live
endpoints, diff against the last-seen snapshot, merge facts with precedence
and conflict reporting, print the table. 76 tests; verified live against
ollama through both its native API and its OpenAI-compatible facade.

## M2 — `ma run`: render and launch (done)

The pipeline's remaining stages, in the home topology (proxy inside the agent
container):

- Render a LiteLLM `config.yaml` from the merged Deployments: routes under
  canonical model names, secrets as `os.environ/MA_<BACKEND>_<VAR>`
  references (never values), merged prices and `model_info` so spend logging
  prices each request, duplicate canonical names rejected at render time.
- Entrypoint: start LiteLLM with provider env vars, generate a per-launch
  master key, scrub provider vars from the environment, `exec` the agent.
- `ma run --project X -- <agent command>`: one engine invocation composing
  env-file(secrets, not mounted) + config(ro at `/run/ma/config.yaml`) +
  workspace mount + per-project ledger mount, as the invoking uid/gid with
  capabilities dropped, launch dir under `$XDG_RUNTIME_DIR`.
- Usage JSONL per project/machine (tokens, model, cost — never prompts).
- Verified with a real agent talking through the proxy to ollama and one
  hosted API, and the gov-slip guard checked: from `paper-review`, a request
  naming a Gemini model fails as unreachable.

Tests: render as a pure function (config in → YAML out, asserted no secret
values in output); entrypoint scrub asserted by reading the agent's
environment; an end-to-end smoke against ollama.

## M3 — Bedrock adapter and `ma keys` (home scope done; work scope open)

Done:

- `type: bedrock` adapter: region and partition rendered into
  `litellm_params`, with explicit per-backend
  `aws_access_key_id`/`aws_secret_access_key`/`aws_session_token`
  `os.environ` references so two accounts × two partitions cannot sign each
  other's requests. Mantle needs no adapter work — its `endpoint_url` rides
  the backend entry's `extra:` passthrough. Four registry entries and four
  credential names for the work accounts (projA/projB × gov/com).
- `ma keys`: the morning ritual — write short-term AWS credentials into the
  credential files; staleness warning when a short-term file is older than N
  hours.
- Home validation: rendered-config unit tests, plus live-fire of the
  commercial path with the home Bedrock account on a morning keys were
  fetched.

Remaining, and both need work hardware:

- Sidecar-proxy topology (proxy in its own container on a per-launch
  network), so the agent container never holds provider secrets — gov
  credentials get the wall, not the raised bar.
- GovCloud validation: endpoints, model ids, and prices confirm only at work.

## M4 — Portability and work install (simulatable at home)

- Plain-process proxy fallback for machines without a container engine, and
  the loud "no proxy, env vars exported directly" degraded mode.
- **Image transport into the work network.** The agent image cannot be pulled
  there, so build it at home, `docker save | zstd` it to a tarball, measure
  the size on home hardware, and document the `docker load` (and `podman
  load`) path end to end, including where the tarball is allowed to travel.
  This is a first-class deliverable, not a footnote: without it the whole
  container topology is unavailable at work and M4's gate is unmet.
- Vendored wheels; verify `uv`-based offline install with networking off.
- Refresh story for the vendored price catalog (deliberate, dated).
- A short coworker-onboarding doc: clone shared config, create credential
  files, read the `ma models` status column as the checklist.

Gate: this is the milestone to finish before the first work import.

## M5 — Multi-machine and coworkers (needs the NUC/Mac as stand-ins)

- Credential-file sync across machines (syncthing/scp/age — pick when it
  hurts), scoped to the one credentials directory.
- machine.yaml in anger: the Mac reaching the home box by tailnet name.
- Decide whether projects.yaml (and sensitive deployment names) split into a
  private overlay repo.

## Later, when a real need appears

- Long-lived per-machine proxy; remote proxies as peers; issuing virtual
  keys to coworkers from your own proxy.
- Background polling for change monitoring (launch-time diff may be the
  permanent answer).
- Usage reconciliation reporting across ledgers (yours vs. a shared
  endpoint's).
