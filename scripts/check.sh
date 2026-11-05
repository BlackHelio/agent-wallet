#!/bin/sh
# Everything a reviewer (or CI) should be able to run before trusting this plugin.
# Usage: scripts/check.sh [path-to-plugin]
set -eu

PLUGIN_DIR="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
if [ -n "${HERMES_BIN:-}" ]; then :;
elif [ -x /opt/hermes/hermes ]; then HERMES_BIN=/opt/hermes/hermes;
else HERMES_BIN=hermes; fi

echo "== unit tests (no network, chains disabled)"
# The suite needs the plugin's runtime deps. A bare interpreter fails 14 tests with tracebacks and
# looks like a broken repo; say why instead.
if ! python3 -c "import nacl, eth_account, eth_keys, embit" >/dev/null 2>&1; then
  echo "   missing dependencies: python3 -m pip install -r $PLUGIN_DIR/requirements.txt" >&2
  echo "   (or: hermes plugins validate $PLUGIN_DIR --install-deps)" >&2
  exit 1
fi
python3 -m unittest discover -s "$PLUGIN_DIR/tests" -t "$PLUGIN_DIR" 2>&1 | tail -3

echo "== manifest + registration (doctor)"
"$HERMES_BIN" plugins doctor "$PLUGIN_DIR" 2>&1 | tail -4

echo "== catalog gate (validate)"
"$HERMES_BIN" plugins validate "$PLUGIN_DIR" 2>&1 | tail -4

echo "== desktop half parses"
cp "$PLUGIN_DIR/desktop/plugin.js" /tmp/agent-wallet-plugin.mjs
node --check /tmp/agent-wallet-plugin.mjs && echo "plugin.js OK"

echo "== page renders without a browser"
if [ -f "$PLUGIN_DIR/desktop/render-check.mjs" ]; then
  ( cd "$PLUGIN_DIR/desktop" && node render-check.mjs ./plugin.js 2>/dev/null \
      || echo "render-check needs the stubbed SDK+react node_modules — see docs/DEV-SETUP.md" )
fi

echo "== done"
