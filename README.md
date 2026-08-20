# multiagent

`ma` starts an AI agent behind a LiteLLM proxy rendered fresh for each launch
from a *project policy* naming which backends — and therefore which credentials,
and therefore whose money — that session may reach. Anything the project does not
name has no route in the rendered config, and the agent never receives a provider
key, only a per-launch key worth nothing outside `localhost:4000`. The shared
configuration lives in git and holds no secrets, so a coworker clones it, adds
their own files under `~/.config/multiagent/credentials/`, and gets the same
behavior with their own access.

## The shape

| Component | Question it answers | Lifetime | Where it lives | Shareable? |
|---|---|---|---|---|
| **Backend registry** (`config/backends.yaml`) | What endpoints exist, and how do you talk to them? | Months | git | Yes — no secrets |
| **Credential store** | What secrets do *I* hold, on *this* machine, *today*? | Hours–months | `~/.config/multiagent/credentials/` | Never |
| **Project policy** (`config/projects.yaml`) | Which backends may *this project* use? | Weeks | git | With collaborators |
| **Model knowledge** (`config/models.yaml`) | What do we believe about model X that no endpoint will tell us? | Weeks | git | Yes — the most valuable thing coworkers share |
| **Price catalog** (`config/catalog.json`) | What does a token cost on a hosted API? | Vendored, refreshed deliberately | git | Yes |
| **Machine config** | How does *this* machine reach things? | Stable | `~/.config/multiagent/machine.yaml` | No |
| **Last-seen snapshot** | What did probes observe last launch? | Per launch | `~/.local/state/multiagent/` | No |

A launch composes them in one direction: select → resolve credentials by name →
probe → diff against the snapshot → merge facts → render the proxy config → start
the proxy → start the agent. Why it is cut this way, and why availability is
derived rather than stored, is argued in [notes/multiagent_arch.md](notes/multiagent_arch.md).

## Quickstart, on a machine with Docker

```sh
git clone https://github.com/adolgert/multiprofileharness.git
cd multiprofileharness
uv sync
mkdir -p ~/.config/multiagent
ln -s "$PWD/config" ~/.config/multiagent/config    # where the resolution order looks
```

That last line matters: `ma` reads `--config <dir>`, then `$MA_CONFIG`, then
`~/.config/multiagent/config`, and only last `./config`, which warns when it wins
— a repo you cloned an hour ago is untrusted input.

Credentials never live in the repo. One file per credential *name* used in
`backends.yaml`, `KEY=value` lines, mode 0600. Key-based backends want exactly
one `*_API_KEY` per file, since `ma` will not guess which of two keys to send
where; AWS files hold the `AWS_*` set and `ma keys` writes them for you.

```sh
mkdir -p -m 700 ~/.config/multiagent/credentials
( umask 077; printf 'GEMINI_API_KEY=%s\n' "$GEMINI_KEY" \
    > ~/.config/multiagent/credentials/gemini-api-key.env )
```

Then see what a project can reach — `uv run ma models --project gemini-work`:

```
backend      credential      status  model             ctx      tools  price               note
home-ollama  (none)          live    qwen2.5-14b       32768    yes    $1/$1 per Mtok      *
gemini       gemini-api-key  listed  gemini-2.5-flash  1048576  yes    $0.3/$2.5 per Mtok

conflict home-ollama/qwen2.5:14b context: believed 131072, observed 32768 (observed wins)
```

Build the image once, then launch. With no command after `--` you get a shell in
the container, which is the quickest way to see that the proxy is up:

```sh
docker build -t multiagent docker/
uv run ma run --project gemini-work                    # bash, with the proxy running
uv run ma run --project gemini-work -- your-agent      # or your agent
```

Whatever runs there starts in the current directory with `OPENAI_BASE_URL`
pointed at the per-launch proxy and models under the canonical names `ma models`
printed. This image carries the proxy, not agent harnesses — build those `FROM
multiagent` and pass `--image`. `--dry-run` prints the rendered config and the
engine command without running anything, `--engine podman` swaps engines, and
`--topology` moves the proxy out of the container; `ma run --help` is
authoritative there.

## Onboarding, as a checklist

The status column of `ma models --project <yours>` *is* the checklist; every
backend the project names gets a row.

1. **NO KEY** — the credential file is missing, so the backend is dropped from
   launches. The credential column names the file to write; `ma run` prints the
   full path it wanted.
2. **STALE** — the file's recorded expiry has passed. Refresh it (below);
   launches proceed anyway, loudly, since an expired key often still answers.
3. **down** — the endpoint did not answer; the reason is in the note column.
   Usually a server that is off, or an address wrong for this machine, which is
   a `machine.yaml` fix rather than a shared-registry one.
4. **live** means we probed and it answered; **listed** means the models came
   from a curated list and nobody checked anything — not a claim that the
   account works.
5. A `*` marks a fact where belief and observation disagree, detailed under the
   table; fixing it in `models.yaml` fixes it for everyone.
6. Nothing needs to be green. A backend you cannot reach is dark for you and the
   rest still runs; `ma run` fails only when a project has zero models left.

## The morning ritual

Short-term AWS keys, one credential file per account and partition:

```sh
uv run ma keys bedrock-personal --profile my-sso-profile
```

`ma keys` shells out to `aws configure export-credentials`, so SSO logins and MFA
prompts work exactly as they already do, and writes `<name>.env` at mode 0600 with
the expiry recorded; `--profile` defaults to the credential name. Yesterday's file
reads **STALE** until you re-run this.

## Sharing the registry

Shared config, private credentials: [docs/registry.md](docs/registry.md) covers
how a coworker adopts this repo and where `projects.yaml` splits off if names get
sensitive. One point belongs on the front page — `backends.yaml` binds a
credential *name* to a destination *URL*, so whoever edits it can point a
credential at a host they control and receive it on the first request. A pull
request to that file is a request for other people's keys; review it accordingly.

## Also here

- [docs/offline-install.md](docs/offline-install.md) — installing where PyPI and
  container registries are unreachable: vendored wheels, image as a tarball.
- [docs/daily-workflow.md](docs/daily-workflow.md) — the day-to-day loop.
- [`scripts/refresh-catalog.sh`](scripts/refresh-catalog.sh) — re-fetch the
  vendored price catalog: only when you run it, recording source, date, and
  sha256, and reporting which keys we depend on moved. Use `--dry-run` first.
- [notes/](notes/) — requirements, the architecture argument, milestones.

MIT licensed; see [LICENSE](LICENSE).
