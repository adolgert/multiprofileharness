#!/bin/sh
# Refresh the vendored LiteLLM price catalog — only when someone runs this.
#
# config/catalog.json is a pinned copy so that everyone computes the same costs
# and so that a machine with no route to the internet still has prices. Nothing
# updates it automatically; this script is the deliberate, dated act. It writes
# config/catalog_version.txt with the source URL, today's date, and the sha256
# of what it installed, so `git log` and that file agree on what we are using.
#
#   ./scripts/refresh-catalog.sh --dry-run   fetch, compare, report, change nothing
#   ./scripts/refresh-catalog.sh             the same, then install the new copy
#
# The report names the catalog keys this repo actually depends on — the
# catalog_key: lines in config/models.yaml and the hosted served ids listed in
# config/backends.yaml — and says which of them changed price or disappeared.
# Read that before committing: a vanished key means a model silently loses its
# facts, and a price change moves every number in the usage ledger.
set -eu

URL=https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json
DRY_RUN=0

usage() {
    cat <<'EOF'
usage: refresh-catalog.sh [--dry-run] [--url URL]

  --dry-run   fetch to a temporary file and report the diff; leave config/ alone
  --url URL   fetch from somewhere else (an internal mirror, or a file:// copy)
EOF
}

die() {
    echo "refresh-catalog: $1" >&2
    exit 1
}

while [ $# -gt 0 ]; do
    case $1 in
        --dry-run) DRY_RUN=1 ;;
        --url) shift; [ $# -gt 0 ] || die "--url needs a value"; URL=$1 ;;
        --url=*) URL=${1#--url=} ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; die "unknown option: $1" ;;
    esac
    shift
done

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
CONFIG=$ROOT/config
CATALOG=$CONFIG/catalog.json
VERSION=$CONFIG/catalog_version.txt

[ -d "$CONFIG" ] || die "no config directory at $CONFIG"
command -v python3 >/dev/null 2>&1 || die "python3 is required"

TMP=$(mktemp -d "${TMPDIR:-/tmp}/ma-catalog.XXXXXX")
trap 'rm -rf "$TMP"' EXIT
trap 'rm -rf "$TMP"; exit 130' INT HUP TERM

NEW=$TMP/catalog.json
echo "fetching $URL"
if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$URL" -o "$NEW" || die "fetch failed"
elif command -v wget >/dev/null 2>&1; then
    wget -q -O "$NEW" "$URL" || die "fetch failed"
else
    die "neither curl nor wget is available"
fi

# The keys this repo depends on. Two sources, both greppable on purpose: an
# explicit catalog_key: in models.yaml, and the dotted served ids of hosted
# backends (bedrock's us.anthropic.…, us.amazon.nova-…). Gemini and Anthropic
# ids reach the catalog through catalog_key:, so the first grep covers them.
{
    grep -h 'catalog_key:' "$CONFIG/models.yaml" 2>/dev/null |
        sed -e 's/#.*//' -e 's/.*catalog_key:[[:space:]]*//' || true
    grep -hE '^[[:space:]]*-[[:space:]]*[A-Za-z0-9_-]+\.[A-Za-z0-9_.:-]+[[:space:]]*$' \
        "$CONFIG/backends.yaml" 2>/dev/null |
        sed -e 's/^[[:space:]]*-[[:space:]]*//' || true
} | sed -e 's/[[:space:]]*$//' -e '/^$/d' | sort -u >"$TMP/keys"

python3 - "$CATALOG" "$NEW" "$TMP/keys" <<'PY' || die "the fetched file is not a usable catalog; config/ untouched"
import json, sys

old_path, new_path, keys_path = sys.argv[1:4]


def load(path):
    try:
        with open(path) as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {}
    except ValueError as exc:
        print(f"{path}: invalid JSON: {exc}", file=sys.stderr)
        sys.exit(1)
    if not isinstance(data, dict):
        print(f"{path}: expected a JSON object of model entries", file=sys.stderr)
        sys.exit(1)
    return data


old, new = load(old_path), load(new_path)
if len(new) < 100:
    print(f"{new_path}: only {len(new)} entries; that is not the catalog", file=sys.stderr)
    sys.exit(1)

print(f"entries: {len(old)} -> {len(new)} ({len(new) - len(old):+d})")

keys = [line.strip() for line in open(keys_path) if line.strip()]
print(f"keys referenced by config/: {len(keys)}")


def price(entry):
    return (entry.get("input_cost_per_token"), entry.get("output_cost_per_token"))


def mtok(value):
    return "?" if value is None else f"{value * 1e6:g}"


notes = []
for key in keys:
    before, after = old.get(key), new.get(key)
    if after is None:
        notes.append(f"  VANISHED  {key}" + ("" if before is None else " (was present)"))
    elif before is None:
        notes.append(f"  appeared  {key}  ${mtok(price(after)[0])}/${mtok(price(after)[1])} per Mtok")
    elif price(before) != price(after):
        notes.append(
            f"  price     {key}  "
            f"${mtok(price(before)[0])}/${mtok(price(before)[1])} -> "
            f"${mtok(price(after)[0])}/${mtok(price(after)[1])} per Mtok"
        )

if notes:
    print("\n".join(notes))
else:
    print("  no referenced key changed price or vanished")
PY

if [ "$DRY_RUN" -eq 1 ]; then
    echo "dry run: config/catalog.json and config/catalog_version.txt are untouched"
    exit 0
fi

SHA=$(python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$NEW")
cp "$NEW" "$CATALOG"
cat >"$VERSION" <<EOF
source: $URL
fetched: $(date -u +%Y-%m-%d)
sha256: $SHA
EOF
echo "wrote $CATALOG"
echo "wrote $VERSION (sha256 $SHA)"
echo "commit both together; the diff summary above belongs in the commit message"
