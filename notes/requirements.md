This project solves problems I have using AI agents. It is developed at home and
carried into work, so the home setup doubles as a test analog of the work setup.

## The two environments

### Work (the primary target)

- Multiple vLLM instances on the internal network. Other people administer them
  and change which models they serve; the set of offerings churns.
- Amazon Bedrock reached through **two different project accounts**, and each
  account has **both Gov-cloud and non-Gov (commercial) access keys** — four
  distinct credential sets in total.
- Choosing the wrong account or partition spends the wrong project's money (and
  may cross a data boundary), so selecting a project must select the credentials.
- Coworkers should be able to adopt the tool: shared configuration, their own
  credentials.
- The work network may not reach the public internet; assume packages and images
  cannot be pulled freely there.

### Home (development environment and daily use)

- A local vLLM instance on a Linux computer; the model it runs changes.
- An Anthropic Max account; sometimes an OpenAI account; a Gemini Ultra account.
- An Amazon Bedrock account with two projects: one gov-cloud-only, one with
  either gov or regular cloud; each project can also reach a Bedrock Mantle
  endpoint.
- Machines agents run on: main Linux computer, an Intel NUC, a Mac.
- Agent frameworks in use: `pi`, `opencode`, `aider`, `claude`, `codex`.

The structural rhyme is deliberate: home vLLM ↔ work vLLM instances, home
Bedrock projects ↔ work Bedrock accounts. If the tool works at home, only
addresses and credential files should change at work.

## Core requirements

1. **Record available resources.** A registry of endpoints/backends that exists
   independently of any one machine, lives in git, and contains no secrets.
2. **Monitor changes in what resources offer.** vLLM instances are updated by
   others; the tool must show what each backend offers *now* and surface what
   changed since last looked (model swapped, endpoint down, new instance).
3. **Select subsets by project — use the right money.** A project names the
   backends and credentials it may use. Everything else is unreachable from that
   project, not merely discouraged. This is the guard against "I forgot which
   account I was on."
4. **Shareable with coworkers.** A coworker clones the shared configuration,
   supplies their own credential files, and gets the same behavior. Nothing
   shared ever contains a secret.
5. **Small and portable.** Little code, few dependencies, installable on a
   restricted network. Complexity budget goes to the interactions (registry ∩
   credentials ∩ liveness ∩ policy), not to infrastructure.

## Workflows

### Daily

1. In the morning get short-term access keys, if I think I want them that day
   (AWS at home; the work equivalent for the two Bedrock accounts).
2. Share those among my machines somehow.
3. To start a project, open a CLI and navigate to a directory.
4. Start an agent specifying the project I'm using.
5. See all models that should be available for that project.

There should be some time to find what models are currently available. Maybe it
happens in the morning when I get keys for the day. Maybe it happens when I
start using a project. Maybe it's a separate step. I have in my head the set of
all possible models to run, the set currently available, and — new with the
work use case — a notion of *what changed since yesterday*.

### Onboarding a coworker

1. They clone the shared registry/project configuration.
2. They create their own credential files for the backends they can access.
3. `ma models --project X` shows them which backends are live, which are
   configured-but-not-credentialed, and which are down.

## Open questions (work environment)

- Is Docker available and permitted on work machines? The current architecture
  runs a LiteLLM proxy in or beside the agent container; if Docker is out, the
  proxy must run as a plain process, or the tool must be able to skip the proxy
  and export env vars directly.
- Can Python packages be installed at work (internal mirror? vendored wheels?),
  or should the tool restrict itself to the standard library?
- Does "monitor" mean on-demand ("what changed since I last ran `ma models`?")
  or a background poller that records a history? On-demand with a stored
  last-seen snapshot is the minimal version.
- How do coworkers receive updates to the shared registry — a git repo on an
  internal host, or copied files?
