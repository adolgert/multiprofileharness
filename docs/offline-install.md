# Installing multiagent where there is no PyPI, Docker Hub, or GitHub

This is the work-side procedure. Everything you need arrives in one directory,
`dist/offline/`, built at home by `scripts/build-offline-bundle.sh`. Nothing
below touches the network.

## What you carry

Sizes are what the bundle actually measured on the home box on 2026-08-20,
image `sha256:f4a1dbbf0dc5…`, not estimates:

| part | size | what it is |
| --- | --- | --- |
| `wheels/` | 813 KiB (3 files) | `multiagent-0.1.0-py3-none-any.whl`, `pyyaml-6.0.3-…whl`, `requirements.txt` |
| `multiagent-image.tar.zst` | 176 MiB | the agent image: 758 MB on disk, 744 MiB as an uncompressed tar, 4.2× down under `zstd -12` |
| `wheels-proxy/` | 156 MiB (107 wheels) | only with `--with-proxy`: LiteLLM and its dependencies, for machines with no container engine |
| **total** | **333 MiB** | 177 MiB without the proxy wheel set |

The whole thing fits on any USB stick and inside most mail-size limits after
splitting. `MANIFEST.txt` lists every file with its size and sha256, plus the
image digest, the git commit, and the build date — so "is this the bundle I
built?" is a checkable question, not a hopeful one.

Build it at home with:

```sh
scripts/build-offline-bundle.sh              # launcher + image, ~8 s
scripts/build-offline-bundle.sh --with-proxy # ... plus LiteLLM, ~21 s
```

## 0. Check what arrived

```sh
cd dist/offline
awk '/^[0-9]+ /{print $2"  "$3}' MANIFEST.txt | sha256sum -c -
```

112 lines of `OK` for the full bundle. A copy that crossed an air gap on a
stick is exactly the kind of copy that gets truncated silently.

## 1. Install the launcher

The launcher is stdlib plus PyYAML on purpose, so this is two wheels and no
build step:

```sh
python3 -m venv ~/.local/venv/multiagent
~/.local/venv/multiagent/bin/pip install --no-index --find-links wheels/ multiagent
ln -s ~/.local/venv/multiagent/bin/ma ~/.local/bin/ma
```

The uv equivalent, if uv made it onto the work box:

```sh
uv venv ~/.local/venv/multiagent
VIRTUAL_ENV=~/.local/venv/multiagent \
  uv pip install --offline --no-index --find-links wheels/ multiagent
```

`--offline` is the belt to `--no-index`'s braces: it makes uv fail loudly
rather than quietly reaching for an index that will time out.

Check: `ma --help` prints the three subcommands (`models`, `run`, `keys`).

`wheels/requirements.txt` is the exported, hash-pinned dependency set from
`uv.lock`. You do not need it to install — `--find-links` is enough — but it is
the record of what the bundle was supposed to contain.

## 2. Load the agent image

```sh
zstd -dc multiagent-image.tar.zst | docker load
```

Podman reads the same archive:

```sh
zstd -dc multiagent-image.tar.zst | podman load
```

and then `ma run --engine podman`, or `export MA_CONTAINER_ENGINE=podman` once
in your shell profile. Rootless podman is the likelier engine on a locked-down
box, which is why the engine is a flag rather than a hardcoded string.

If `zstd` is not installed at work and cannot be, rebuild the bundle at home
without it — the script falls back to `gzip -9` and names the file
`multiagent-image.tar.gz`, which `gunzip -c … | docker load` reads. The gzip
tarball is bigger; measure it before you rely on a size budget.

Check: `docker image ls multiagent` shows the digest recorded in
`MANIFEST.txt`.

### Where the tarball is allowed to travel

The image contains no secrets — it is LiteLLM, a startup script, and a
callback module — so it is a normal third-party software artifact, and it
travels wherever your policy lets third-party software travel; that is a
question for whoever owns that policy, and the manifest's digests are what they
will want to see. What must *never* travel this way is a credential `.env`
file. Those are written on the machine that
uses them by `ma keys`, and they are the reason `ma models` never prints a
secret value.

## 3. Tell `ma` where the config lives

The shared config directory holds `backends.yaml`, `projects.yaml`,
`models.yaml`, `catalog.json`, and `catalog_version.txt`. Resolution order,
first hit wins:

1. `--config <dir>`
2. `$MA_CONFIG`
3. `~/.config/multiagent/config`
4. `./config` — last resort, and it prints a warning naming the directory

The order is deliberate. `backends.yaml` binds credential *names* to
destination URLs, so whoever can edit it can point `bedrock-projA-gov` at a
host they control and be handed that credential on the first request. A repo
you cloned an hour ago must not win over your own config, so the working
directory is last and noisy when it wins.

The normal work setup is to clone the config repo and either symlink it:

```sh
git clone <internal-mirror>/multiagent-config ~/src/multiagent-config
ln -s ~/src/multiagent-config/config ~/.config/multiagent/config
```

or set `MA_CONFIG` in your shell profile. Pick one and stop thinking about it.

## 4. First `ma keys`

Credentials are per machine, never in git, one file per credential name at
`~/.config/multiagent/credentials/<name>.env`, mode 0600 in a 0700 directory.
`ma keys` writes them from the AWS CLI, so the AWS CLI has to be on the work
box already — it is not in this bundle:

```sh
ma keys bedrock-projA-com            # profile defaults to the credential name
ma keys bedrock-projA-gov --profile projA-gov
```

It prints the file path and the expiry, nothing else. Short-term credentials
expire, so this is a morning ritual, not a one-time setup. Long-term keys and
non-AWS backends are just files with the right variable names in them; write
those by hand once.

## 5. First `ma models`

```sh
ma models --project <project>
```

This is the whole launch pipeline stopped just before the proxy would start:
it selects the project's backends, resolves credential names to files, probes
each endpoint, and merges what it found with `models.yaml` and the catalog.
Read the **status** column as your install checklist. Every backend the project
names should be listed and reachable; a backend that is missing a credential
file, or whose endpoint does not answer, says so on its own line and is dropped
from the launch rather than failing later at the first request.

Expect the first work run to show real problems: an endpoint on an internal
name that only resolves on the VPN, a corporate TLS interception that needs
`--ca-bundle`, a Gov endpoint whose model ids differ from the commercial ones.
That is what the column is for.

## 6. First run

```sh
ma run --project <project> -- <your agent command>
```

Add `--dry-run` first: it prints the rendered proxy config and the exact engine
command line and stops. Read the config once — it should contain
`os.environ/MA_<BACKEND>_<VAR>` references and no secret values anywhere.

## When there is no container engine

If docker is not permitted and podman is not installed, the container topology
is unavailable and the proxy runs as a plain host process:

```sh
ma run --project <project> --topology process -- <agent command>
```

This needs `litellm` on PATH. That is what `wheels-proxy/` is for — 156 MiB
against the launcher's 813 KiB, which is the whole reason it is a separate,
optional part of the bundle:

```sh
uv tool install --offline --find-links wheels-proxy 'litellm[proxy]'
```

or with pip, into a venv of its own:

```sh
python3 -m venv ~/.local/venv/litellm
~/.local/venv/litellm/bin/pip install --no-index \
  --find-links wheels-proxy -r wheels-proxy/requirements.txt
ln -s ~/.local/venv/litellm/bin/litellm ~/.local/bin/litellm
```

`wheels-proxy/requirements.txt` is the resolved, fully pinned set that the
bundle actually contains — 107 packages behind three direct pins. Installing
from it, rather than from `litellm[proxy]==1.97.0` alone, is what makes the
work-side install identical to the home-side one.

Those three pins are `litellm[proxy]==1.97.0`, `fastapi==0.140.0`, and
`boto3==1.43.75`. They live at the top of `scripts/build-offline-bundle.sh` and
in `docker/Dockerfile`, and the script warns if the two drift apart. The fastapi
pin is not cosmetic: litellm 1.97's proxy imports fastapi internals that 0.141
removed, while its own constraint still admits 0.141. An unpinned install
resolves to a proxy that fails at import.

Be clear about what you lose. `--topology process` runs the proxy under your
own uid, so the provider keys are readable out of `/proc/<pid>/environ` by
anything running as you — a raised bar, not the container's wall. There is also
no usage ledger: the accounting callback lives in the image. `ma run` says so
at every launch.

Below that is `--topology none`: no proxy, provider keys exported straight into
the agent, no canonical model names, no logging. It exists so the degraded case
is explicit and loud rather than improvised at 5pm. Do not use it with Gov
credentials.

## What was not validated at home

Honest list. All of the above was run for real on the home box; none of it was
run on work hardware.

**Binary wheels are built for the machine that downloaded them.** This is the
most likely thing to break. The bundle was built by Python 3.13 on x86-64
Linux, so it contains `pyyaml-6.0.3-cp313-cp313-manylinux2014_x86_64…whl`. On a
work box running Python 3.11, pip will find no compatible wheel and stop —
correctly, but at the worst moment. The proxy set is worse: it carries about
twenty cp313-specific wheels (`pydantic-core`, `tokenizers`, `tiktoken`,
`aiohttp`, `cryptography`, `uvloop`, …), plus `manylinux_2_28` and
`manylinux_2_34` tags that need a glibc at least that new. RHEL 8 is glibc
2.28; anything older will refuse `cryptography-50.0.0-cp311-abi3-manylinux_2_34`.

Two fixes, in order of preference:

1. Build the bundle on a machine matching work's Python and glibc — a container
   with the right base image is enough, since only the download platform
   matters.
2. Re-run the build with explicit target flags:

   ```sh
   MA_PIP_TARGET='--python-version 3.11 --abi cp311 --platform manylinux_2_28_x86_64' \
     scripts/build-offline-bundle.sh --with-proxy
   ```

   The script passes `$MA_PIP_TARGET` straight to `pip download` and records it
   in `MANIFEST.txt`, so a bundle built for another platform says so on its
   face. That exact line was exercised at home for the launcher set — it
   fetches the cp311 PyYAML wheel, hashes still checked. It was *not* exercised
   for the 107-wheel proxy set, and cross-platform downloads are best-effort
   there: pip cannot evaluate the target's environment markers, so a dependency
   that exists only on some platforms can come out wrong.

**The launcher itself is fine either way.** `multiagent-0.1.0-py3-none-any.whl`
is pure Python, and PyYAML has an sdist that builds without libyaml if a
compiler is present. If only PyYAML is missing, that is a small problem.

**Not tested:** podman. The `podman load` line is the documented equivalent, not
something that ran here — there is no podman on the home box. The same goes for
rootless podman's uid mapping against `ma run`'s `--user` handling.

**Not tested:** any of it behind corporate TLS interception. `--ca-bundle`
exists for it, and it has never met a real interception certificate.

**Not tested:** GovCloud endpoints, model ids, or prices. That waits for work
hardware regardless of how the software got there — see M3.

**Reproducibility is same-day, not eternal.** The launcher wheels come from
`uv.lock` with hashes, so those are reproducible for as long as the lock says
so. The proxy set is resolved at build time by `uv pip compile` and the
resolution is written into `wheels-proxy/requirements.txt`; two runs a month
apart can pick up newer transitive versions. If you need the bundle to be
bit-identical later, keep `wheels-proxy/requirements.txt` from the build that
mattered — that file is the record.
