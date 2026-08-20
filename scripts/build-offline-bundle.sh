#!/bin/sh
# Build dist/offline/ : everything needed to install multiagent on a machine
# that can reach neither PyPI, Docker Hub, nor GitHub.  Run it at home, carry
# dist/offline/ to work, follow docs/offline-install.md there.
#
#   scripts/build-offline-bundle.sh              launcher wheels + image tarball
#   scripts/build-offline-bundle.sh --with-proxy  ... plus the LiteLLM wheel set
#                                                 for the no-container topology
#
# Deliberately boring: POSIX sh, no temp state, everything pinned or
# lock-derived so two runs on the same day produce the same bundle.
set -eu

# --- the pins, in one visible place ---------------------------------------
# These three must match docker/Dockerfile: litellm 1.97's proxy imports
# fastapi internals that 0.141 removed.  If you change them there, change them
# here (and the other way round); the script checks and complains if they drift.
PROXY_PINS='litellm[proxy]==1.97.0 fastapi==0.140.0 boto3==1.43.75'
IMAGE=multiagent

# Extra flags for the wheel downloads.  The default is "wheels for the machine
# running this script".  To target a work box with a different Python or a
# different glibc, e.g.:
#   MA_PIP_TARGET='--python-version 3.11 --abi cp311 --platform manylinux_2_28_x86_64'
PIP_TARGET=${MA_PIP_TARGET:-}

ROOT=$(cd "$(dirname "$0")/.." && pwd)
OUT=$ROOT/dist/offline

usage() {
	echo "usage: $0 [--with-proxy]" >&2
	exit 2
}

die() {
	echo "$0: $*" >&2
	exit 1
}

# uv builds the project wheel and owns the lock the launcher deps come from.
command -v uv >/dev/null 2>&1 || die "uv not found; it builds the wheel and reads uv.lock"
command -v docker >/dev/null 2>&1 || die "docker not found; needed for docker save"

with_proxy=0
while [ $# -gt 0 ]; do
	case $1 in
	--with-proxy) with_proxy=1 ;;
	-h | --help) usage ;;
	*) usage ;;
	esac
	shift
done

cd "$ROOT"

# pip itself is not a project dependency; borrow one for the download.
run_pip() {
	uv run --quiet --with pip python -m pip "$@"
}

rm -rf "$OUT"
mkdir -p "$OUT/wheels"

# --- wheels: the launcher and its one dependency --------------------------
echo "==> building the multiagent wheel"
uv build --quiet --wheel --out-dir "$OUT/wheels"
rm -f "$OUT/wheels/.gitignore" # uv drops one in any output directory

echo "==> downloading launcher dependencies"
# uv.lock is the pin: exported with hashes, so pip verifies what it fetches
# and a second run on a moved index still produces the same bytes.
uv export --quiet --no-dev --no-emit-project --format requirements-txt \
	-o "$OUT/wheels/requirements.txt"
# shellcheck disable=SC2086  # PIP_TARGET is a flag list and must word-split
run_pip download -r "$OUT/wheels/requirements.txt" \
	--only-binary :all: -d "$OUT/wheels" $PIP_TARGET

# --- wheels-proxy: LiteLLM, for machines with no container engine ------
if [ "$with_proxy" = 1 ]; then
	for pin in $PROXY_PINS; do
		grep -qF -- "$pin" "$ROOT/docker/Dockerfile" ||
			echo "warning: $pin is not in docker/Dockerfile; pins have drifted" >&2
	done
	mkdir -p "$OUT/wheels-proxy"
	echo "==> resolving the proxy wheel set"
	# The pins above name three packages; the hundred-odd transitive
	# dependencies are resolved here and written down, so the bundle records
	# exactly what it contains and the work side installs from that file
	# rather than from a fresh resolution.
	for pin in $PROXY_PINS; do echo "$pin"; done |
		uv pip compile --quiet - -o "$OUT/wheels-proxy/requirements.txt"
	echo "==> downloading the proxy wheel set (this is the big one)"
	# shellcheck disable=SC2086
	run_pip download -r "$OUT/wheels-proxy/requirements.txt" --no-deps \
		--only-binary :all: -d "$OUT/wheels-proxy" $PIP_TARGET
fi

# --- the agent image --------------------------------------------------
echo "==> saving the $IMAGE image"
image_id=$(docker image inspect "$IMAGE" --format '{{.Id}}' 2>/dev/null) ||
	die "no local image '$IMAGE'; build it with: docker build -t $IMAGE docker/"
if command -v zstd >/dev/null 2>&1; then
	tarball=$OUT/$IMAGE-image.tar.zst
	docker save "$IMAGE" | zstd -q -T0 -12 -o "$tarball"
else
	echo "    zstd not found, falling back to gzip"
	tarball=$OUT/$IMAGE-image.tar.gz
	docker save "$IMAGE" | gzip -9 >"$tarball"
fi

# --- the manifest -----------------------------------------------------
echo "==> writing MANIFEST.txt"
commit=$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || echo unknown)
if [ -n "$(git -C "$ROOT" status --porcelain 2>/dev/null || true)" ]; then
	commit="$commit (working tree dirty)"
fi

{
	echo "multiagent offline bundle"
	echo "date:      $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
	echo "git:       $commit"
	echo "image:     $IMAGE $image_id"
	echo "built on:  $(uname -srm), $(uv run --quiet python -V)"
	echo "pip flags: ${PIP_TARGET:-none (wheels match the builder's platform)}"
	echo
	echo 'verify:    awk '"'"'/^[0-9]+ /{print $2"  "$3}'"'"' MANIFEST.txt | sha256sum -c -'
	echo
	echo "size        sha256                                                            file"
	find "$OUT" -type f ! -name MANIFEST.txt | LC_ALL=C sort | while read -r f; do
		printf '%-11s %s  %s\n' \
			"$(wc -c <"$f" | tr -d ' ')" \
			"$(sha256sum "$f" | cut -d' ' -f1)" \
			"${f#"$OUT"/}"
	done
} >"$OUT/MANIFEST.txt"

# --- size summary ---------------------------------------------------------
echo
echo "bundle: $OUT"
du -sh "$OUT/wheels" | sed 's/^/  wheels        /'
if [ "$with_proxy" = 1 ]; then
	du -sh "$OUT/wheels-proxy" | sed 's/^/  wheels-proxy  /'
fi
du -sh "$tarball" | sed 's/^/  image         /'
du -sh "$OUT" | sed 's/^/  total         /'
