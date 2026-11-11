#!/usr/bin/env bash
# Build the tree that goes public, from a single commit.
#
# An internal working history can carry machine-specific paths, private network addresses and the
# location of local key files, inside docs that were deleted later. None of that is a credential —
# `scripts/secret_scan.py --history --env-file` finds no secret *value* — but a public mirror would
# publish it, and a deleted file is still in the packfile.
#
# So the mirror is a snapshot, not this history: one commit, the current tree, nothing behind it.
# That also means the catalog `sha:` must point at the snapshot commit, not at a commit in here.
set -euo pipefail

src="$(cd "$(dirname "$0")/.." && pwd)"
out="${1:-$src/../agent-wallet-public}"
branch="${2:-public}"

cd "$src"
if [ -n "$(git status --porcelain)" ]; then
  echo "working tree is dirty — commit or stash first, the snapshot must match a real commit" >&2
  exit 1
fi

rm -rf "$out"
mkdir -p "$out"
git archive --format=tar HEAD | tar -x -C "$out"

cd "$out"
git init -q -b main
git add -A
git -c user.name="$(git -C "$src" config user.name || echo agent-wallet)" \
    -c user.email="$(git -C "$src" config user.email || echo agent-wallet@localhost)" \
    commit -q -m "agent-wallet $(grep -oP '(?<=^__version__ = ")[^"]+' walletd/__init__.py 2>/dev/null || echo '')"

# The snapshot has to pass the same gate as the tree it came from, and its history is one commit.
python3 scripts/secret_scan.py --history || {
  echo "secret scan failed on the snapshot — do not publish" >&2
  exit 1
}
ENV_FILE="${ENV_FILE:-$HOME/.env}"   # live credentials to compare against, if you keep one
if [ -f "$ENV_FILE" ]; then
  python3 scripts/secret_scan.py --history --env-file "$ENV_FILE" || {
    echo "a live credential value appears in the snapshot — do not publish" >&2
    exit 1
  }
fi

echo
echo "snapshot: $out"
echo "sha:      $(git rev-parse HEAD)   <- this is the value for plugin-catalog/agent-wallet.yaml"
echo "push it to the public remote, then run: hermes plugins validate <dir> --install-deps"
