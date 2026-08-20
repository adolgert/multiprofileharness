# Milestones

Each milestone extends the launch pipeline one stage further and states what
can be exercised at home versus what waits for work. Milestone 1 is done.

## M1 — `ma models` (done)

Parse the four shared files, resolve credentials by name, probe live
endpoints, diff against the last-seen snapshot, merge facts with precedence
and conflict reporting, print the table. 76 tests; verified live against
ollama through both its native API and its OpenAI-compatible facade.

## M2 — `ma run`: render and launch (fully exercisable at home)

The pipeline's remaining stages, in the home topology (proxy inside the agent
container):

- Render a LiteLLM `config.yaml` from the merged table: routes under
  canonical model names, secrets as `os.environ/VAR` references (never
  values), merged prices and `model_info` so spend logging prices each
  request.
- Entrypoint: start LiteLLM with provider env vars, generate a per-launch
  master key, scrub provider vars from the environment, `exec` the agent.
- `ma run --project X -- <agent command>`: one `docker run` composing
  env-file(secrets) + config(ro) + workspace mount.
- Usage JSONL per project/machine (tokens, model, cost — never prompts).
- Verify with a real agent (`aider` or `claude`) talking through the proxy to
  ollama and one hosted API; verify the gov-slip guard: from `paper-review`,
  a request naming a Gemini model must fail as unreachable.

Tests: render as a pure function (config in → YAML out, asserted no secret
values in output); entrypoint scrub asserted by reading the agent's
environment; an end-to-end smoke against ollama.

## M3 — Bedrock adapter and `ma keys` (partially exercisable at home)

- `type: bedrock` adapter: region, partition, Mantle endpoint rendered into
  `litellm_params`; four registry entries and four credential names for the
  work accounts (projA/projB × gov/com).
- `ma keys`: the morning ritual — write short-term AWS credentials into the
  credential files; staleness warning when a short-term file is older than N
  hours.
- Sidecar-proxy topology (proxy in its own container on a per-launch
  network), so the agent container never holds provider secrets — gov
  credentials get the wall, not the raised bar.
- Home validation: rendered-config unit tests now; live-fire the commercial
  path with the home Bedrock account on a morning keys are fetched. GovCloud
  specifics confirm only at work.

## M4 — Portability and work install (simulatable at home)

- Plain-process proxy fallback for machines without Docker, and the loud
  "no proxy, env vars exported directly" degraded mode.
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
