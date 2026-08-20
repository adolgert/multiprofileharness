# Living with this tool day to day

This doc is for the developer-operator: how the pieces in this repo become the
thing you actually run, where every file ends up at runtime, and what a normal
day looks like. It assumes you currently have something like an `agent-run pi`
script in `~/bin` that pulls a Docker image of agent harnesses, checks config,
and runs the agent. That model survives almost intact; here is the mapping.

## The mental-model bridge

| Your current setup | This tool | What changed and why |
|---|---|---|
| `agent-run pi` | `ma run --project X -- pi` | The new argument is the point: `--project` picks which backends — and so which credentials, which money — the session can reach. Everything else is unreachable, not just unconfigured. |
| Script copied to `~/bin` on every account | `ma` installed once per machine: `uv tool install .` from a checkout (or from the offline wheel bundle at work) | Same ergonomics — a command on PATH, runnable from any directory — but versioned by git instead of by which copy of the script a machine happens to have. |
| One image with agent harnesses baked in | `multiagent` image (proxy only) + a harness layer you build on top | See "Images" below. The base image holds LiteLLM and the entrypoint that guards your keys; your harnesses go in a derived image, exactly like your current build script. |
| Script ensures config files exist | Config resolution: `--config` flag → `$MA_CONFIG` → `~/.config/multiagent/config` → `./config` (warned) | The script's "ensure config" step becomes a one-time placement of the shared config where the tool looks for it. |
| (no equivalent) | `ma keys`, `ma models`, the usage ledger | The parts your script never had: morning credentials, "what is actually being served right now", and per-project spend. |

## What lives where

**In this repo (shared, no secrets):**

| Path | Role at runtime |
|---|---|
| `src/multiagent/` | The tool itself. Installed once per machine; you don't touch it daily. |
| `config/backends.yaml` | What endpoints exist and which credential *name* each needs. |
| `config/projects.yaml` | Which backends each project may use — the money policy. |
| `config/models.yaml` | Facts endpoints won't tell you (context, tools, prices), including per-deployment overrides. The file coworkers improve together. |
| `config/catalog.json` + `catalog_version.txt` | Vendored hosted-API price table; refresh deliberately with `scripts/refresh-catalog.sh`. |
| `docker/` | The proxy image definition. Rebuild only when pins change. |
| `scripts/` | Offline bundle build, catalog refresh. Run occasionally, never daily. |
| `notes/` | The design argument. Read when changing structure, not when using it. |

**Per machine, outside any repo (private):**

| Path | Role |
|---|---|
| `~/.config/multiagent/credentials/<name>.env` | One secret per file, 0600. The *names* come from backends.yaml; the *values* are yours. |
| `~/.config/multiagent/machine.yaml` | This machine's quirks (an `api_base` override, nothing more). |
| `~/.config/multiagent/config/` | Where the shared `config/` lands so `ma` finds it from any directory (see setup). |
| `~/.local/state/multiagent/` | The change diary (`last-seen.json`) and per-project usage ledgers (`usage/<project>/usage.jsonl`). |

The rule underneath: everything in git is safe to publish by construction;
everything secret lives under `~/.config/multiagent/` and nothing else ever
holds it.

## One-time setup on a new machine

This replaces "copy the script to ~/bin":

```sh
git clone https://github.com/adolgert/multiprofileharness
cd multiprofileharness
uv tool install .                  # puts `ma` on PATH
ln -s "$PWD/config" ~/.config/multiagent/config   # or copy, or set MA_CONFIG
docker build -t multiagent docker/ # or: load the offline tarball (docs/offline-install.md)
ma keys bedrock-personal --profile personal       # per credential you hold
ma models --project gemini-work    # sanity: live / listed / NO KEY rows
```

The symlink-vs-copy choice is the "how do config updates reach me" choice: a
symlink means `git pull` updates your registry; a copy means you update
deliberately. Either works; the tool doesn't care.

## The daily loop

```sh
# Morning, only on days you want AWS (long-term keys skip this):
ma keys bedrock-projA-gov --profile work-projA-gov     # one per account you'll use

# Any time, from any directory:
ma models --project bedrock-work    # what's being served NOW, what changed since
                                    # last look, what's in conflict with beliefs

# Work:
cd ~/dev/some-project
ma run --project bedrock-work -- pi         # pi, aider, codex, bash — your call
```

`ma run` probes the live backends, renders a proxy config under your project's
policy, starts the container with your working directory mounted at
`/workspace`, and execs the agent with exactly one secret in its environment:
a per-launch proxy key. The agent sees stable model names (`qwen2.5-14b`,
`claude-haiku-4-5`) regardless of which backend serves them. Spend lands in
`~/.local/state/multiagent/usage/<project>/usage.jsonl`.

On a machine without Docker: `ma run --topology process` (proxy as a host
process) or `--topology none` (no proxy, loud warning, last resort).

## Images

Two layers, mirroring your current build script:

1. **`multiagent` (this repo, `docker/`)** — python-slim + pinned LiteLLM +
   the entrypoint that starts the proxy, scrubs provider secrets, and execs
   your command. It deliberately contains no agents.
2. **Your harness image** — the analog of your current image, built FROM the
   base so the secret-handling entrypoint is inherited:

   ```dockerfile
   FROM multiagent
   RUN pip install --no-cache-dir aider-chat && npm install -g @anthropic/pi-cli  # whatever you use
   ```

   Build it as `multiagent-harness`, run with
   `ma run --project X --image multiagent-harness -- pi`.

Today `python3` and `bash` are the only things in the base image, which is why
the smoke tests use them; baking your harnesses into a derived image is the
next concrete step to make `ma run -- pi` real.

**Storing images.** Right now: built locally on each machine. Two ways to
distribute instead: push to a registry (`docker tag` + push to ghcr; then
`ma run --image ghcr.io/adolgert/multiagent:v1` pulls automatically when
absent — your script's pull-if-missing behavior comes free from docker), or
for the offline work network, `scripts/build-offline-bundle.sh` produces a
`docker load`-able tarball (see docs/offline-install.md).

## Which command exercises which stage

The architecture note's pipeline, mapped to what you type:

- `ma models` — everything up to (not including) render: select → resolve →
  probe → diff-vs-diary → merge → print. Read-only, safe always.
- `ma run` — the same pipeline continued: render → env-file → container.
- `ma keys` — writes one credential file; touches nothing else.
- `scripts/refresh-catalog.sh` — replaces the vendored price table, dated.

## Not yet true (so you don't go looking for it)

- No harness image exists yet — the stanza above is the template, unbuilt.
- The sidecar-proxy topology (gov-credential isolation wall) is designed but
  not implemented; in-container is what runs today.
- The shared config lives in this repo; if project names get sensitive,
  projects.yaml splits into a private overlay — decided when it hurts.
