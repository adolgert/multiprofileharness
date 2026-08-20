# Sharing the registry

This repo is meant to be consumed as *shared configuration*. You clone it, keep
your own secrets on your own machine, and contribute back the part that is
genuinely collective — what we know about the models.

## What a coworker does

1. Clone the repo and point the stable config location at it, so `ma` finds the
   shared files from any working directory:

   ```sh
   mkdir -p ~/.config/multiagent
   ln -s "$PWD/config" ~/.config/multiagent/config
   ```

   A symlink means `git pull` is the whole update mechanism. If you would rather
   keep the clone unlinked, `export MA_CONFIG=/path/to/clone/config` does the
   same job; running `ma` from inside the clone also works but prints a warning,
   because a workspace directory is the last and least trusted place it looks.

2. Create credential files for the backends you can actually reach, named after
   the credential *names* the registry uses — `backends.yaml` says a backend
   needs a credential called `bedrock-projA-gov`, and
   `~/.config/multiagent/credentials/bedrock-projA-gov.env` maps that name to
   your key. This indirection by name is the whole trick: the same shared file
   resolves to your credentials on your machine and to mine on mine, and nothing
   shared ever contains a secret.

3. Run `ma models --project <yours>` and read the status column as a checklist.
   Backends you hold no key for show `NO KEY` and are dropped from launches with
   a warning naming the path they wanted; everything else still works. A shared
   registry that only functioned for whoever holds every key in it would defeat
   the purpose.

4. Local quirks go in `~/.config/multiagent/machine.yaml` — an `api_base` your
   laptop needs but the desktop does not, a proxy port, a usage-log directory.
   If policy starts appearing in that file, something is in the wrong place.

## What flows back

`models.yaml` is the file worth contributing to. An endpoint will tell you a
model id and sometimes a context length; it will not tell you whether tool
calling works, which tokenizer it uses, or what the administrator actually set
`--max-model-len` to. Someone has to ask, and the answer should be written down
once for everyone, with a dated comment saying where it came from. Pull requests
to `models.yaml` are ordinary pull requests.

`projects.yaml` is policy. Adding a project, or adding a backend to one, widens
what a session may spend against — small diffs, worth reading, but not alarming.

## Why `backends.yaml` gets credential-level review

`backends.yaml` is the file that binds a credential *name* to a destination
*URL*. Whoever can edit it can point `bedrock-projA-gov` at a host they control
and receive that credential on the first request. It is a capability-granting
input, not documentation: a pull request to it is a request for other people's
keys, and reviewers should read it with the scrutiny they would give a change to
a credential file. The file is small and its diffs are short, so this is cheap to
actually do. The same reasoning is why `./config` is last in the resolution order
and warns when it wins — see
[notes/multiagent_arch.md](../notes/multiagent_arch.md#finding-the-config) for
the full argument, including why a credentialed backend with a cleartext
`http://` address off the local network is treated as a config error.

## When names become sensitive

Nothing here is secret, but names can still leak: internal hostnames, a project
called after a customer or a contract, deployment entries that describe a
particular team's hardware. The schema does not care which directory each file
comes from, so the split is mechanical when you need it:

- `projects.yaml` (and, if it comes to it, the `deployments:` section of
  `models.yaml`) moves to a private overlay repo, and `MA_CONFIG` or the symlink
  points at a directory that assembles both.
- Individual backends can omit `api_base` entirely and defer the address to each
  machine's `machine.yaml`. The shared registry then documents *that* a backend
  exists and what credential it needs, not *where* it is.

Both are cheap because backends, policy, and beliefs are already separate files
with separate owners. Deciding whether the overlay repo is worth it is open work
— see M5 in [notes/milestones.md](../notes/milestones.md).
