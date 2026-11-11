#!/usr/bin/env bash
# Is this plugin still intact after a Hermes update? Run it after upgrading the app or the gateway.
#
#   scripts/update-check.sh [--installed DIR] [--desktop-dir DIR]
#
# It answers the five questions an update can change the answer to:
#   1. does the plugin still validate against this Hermes version?
#   2. does the package's desktop half still match what the pane is loaded from?
#   3. would the app load that pane enabled (standalone copy, no package marker)?
#   4. is the wallet daemon healthy and is the receipt chain intact?
#   5. does the gateway still serve the plugin's routes?
#
# Read-only: it never unlocks the wallet and never writes to it.
set -uo pipefail

INSTALLED="${HERMES_HOME:-$HOME/.hermes}/plugins/agent-wallet"
DESKTOP_DIR="${HERMES_HOME:-$HOME/.hermes}/desktop-plugins/agent-wallet"
while [ $# -gt 0 ]; do
  case "$1" in
    --installed) INSTALLED="$2"; shift 2 ;;
    --desktop-dir) DESKTOP_DIR="$2"; shift 2 ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fails=0
ok()   { printf '  ok    %s\n' "$*"; }
bad()  { printf '  FAIL  %s\n' "$*"; fails=$((fails+1)); }
note() { printf '  ·     %s\n' "$*"; }

echo "== hermes"
version="$(hermes --version 2>/dev/null | head -1)"
[ -n "$version" ] && ok "$version" || bad "hermes not on PATH"

echo "== validates against this version"
if out="$(hermes plugins validate "$INSTALLED" 2>&1)"; then
  ok "$(printf '%s' "$out" | tail -1)"
  printf '%s\n' "$out" | grep -qi "caution\|warning" && note "warnings: $(printf '%s' "$out" | grep -i 'caution\|warning' | head -1)"
else
  bad "validation failed: $(printf '%s' "$out" | tail -1)"
fi

echo "== pane: the copy the app loads"
if [ ! -f "$DESKTOP_DIR/plugin.js" ]; then
  note "no desktop pane on this machine ($DESKTOP_DIR) — skipped"
else
  if [ -f "$DESKTOP_DIR/.hermes-package.json" ]; then
    bad "a .hermes-package.json marker exists: the app loads this pane with defaultEnabled=false"
  else
    ok "no package marker (standalone desktop plugin)"
  fi
  if head -1 "$DESKTOP_DIR/plugin.js" | grep -q "standalone desktop copy"; then
    ok "standalone header present"
  else
    bad "no standalone header: if it matches the package half the app re-adopts it as an inert package half"
  fi
  if [ -f "$INSTALLED/desktop/plugin.js" ]; then
    a="$(sha256sum "$INSTALLED/desktop/plugin.js" | cut -d' ' -f1)"
    b="$(sha256sum "$DESKTOP_DIR/plugin.js" | cut -d' ' -f1)"
    if [ "$a" = "$b" ]; then
      bad "loaded copy is byte-identical to the package half (${a:0:12}): the app will re-materialise it and disable it"
    else
      ok "differs from the package half (${a:0:12} vs ${b:0:12}) — it will not be re-adopted"
    fi
  fi
  if command -v node >/dev/null 2>&1; then
    node --check "$DESKTOP_DIR/plugin.js" && ok "parses" || bad "does not parse"
    if [ -f "$DESKTOP_DIR/render-check.mjs" ]; then
      if node "$DESKTOP_DIR/render-check.mjs" "$DESKTOP_DIR/plugin.js" 2>/dev/null | grep -q "render-check: OK"; then
        ok "renders in the Node harness"
      else
        bad "render-check failed — run it directly to see why"
      fi
    else
      note "render-check.mjs not deployed beside the pane"
    fi
  fi
fi

echo "== wallet daemon"
status_json="$(hermes wallet status --json 2>/dev/null)"
if printf '%s' "$status_json" | grep -q '"status"'; then
  tmp="$(mktemp)"; printf '%s' "$status_json" > "$tmp"
  summary="$(python3 - "$tmp" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1]))["status"]
locked = "locked" if d.get("locked") else "unlocked"
print(f'home={d.get("home")} v={d.get("version")} {locked} coins={d.get("assets")}')
sys.exit(0 if d.get("initialized") else 3)
PYEOF
)"
  rc=$?
  rm -f "$tmp"
  if [ "$rc" -eq 0 ]; then ok "daemon answers: $summary"; else bad "uninitialised wallet: $summary"; fi
else
  bad "daemon did not answer: $(printf '%s' "$status_json" | head -1)"
fi
if verify="$(hermes wallet verify 2>&1)"; then
  printf '%s\n' "$verify" | grep -q "FAILED" && bad "receipt chain FAILED" || ok "receipt chain verifies"
else
  bad "verify failed: $(printf '%s' "$verify" | head -1)"
fi

echo "== routes still import and answer under this version"
route_out="$(INSTALLED="$INSTALLED" python3 - <<'PYEOF' 2>&1
import importlib.util, os, sys
plugin_dir = os.environ["INSTALLED"]
home = os.path.dirname(plugin_dir)          # <HERMES_HOME>/plugins/agent-wallet
pylibs = os.path.join(os.path.dirname(home), "wallet", "pylibs")
for path in (plugin_dir, pylibs):
    if os.path.isdir(path) and path not in sys.path:
        sys.path.insert(0, path)
try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
except Exception as exc:                      # noqa: BLE001
    print(f"skip: fastapi not importable here ({type(exc).__name__})")
    raise SystemExit(2)
spec = importlib.util.spec_from_file_location(
    "wallet_plugin_api", os.path.join(plugin_dir, "dashboard", "plugin_api.py"))
module = importlib.util.module_from_spec(spec)
sys.modules["wallet_plugin_api"] = module
try:
    spec.loader.exec_module(module)
except Exception as exc:                      # noqa: BLE001 - an import error IS the finding
    print(f"import failed: {type(exc).__name__}: {exc}")
    raise SystemExit(1)
app = FastAPI()
app.include_router(module.router, prefix="/api/plugins/agent-wallet")
client = TestClient(app)
codes = {}
for path in ("/status", "/balances", "/sandboxes", "/incoming"):
    try:
        codes[path] = client.get("/api/plugins/agent-wallet" + path).status_code
    except Exception as exc:                  # noqa: BLE001
        codes[path] = f"{type(exc).__name__}"
bad = {k: v for k, v in codes.items() if v not in (200, 503)}
print("codes:", codes)
raise SystemExit(1 if bad else 0)
PYEOF
)"
route_rc=$?
case "$route_rc" in
  0) ok "$route_out" ;;
  2) note "$route_out" ;;
  *) bad "routes broke: $route_out" ;;
esac

# Optional: the live gateway, when credentials for it are in this shell
base="${HERMES_GATEWAY_URL:-http://127.0.0.1:9119}"
user="${HERMES_DASHBOARD_BASIC_AUTH_USERNAME:-}"
pass="${HERMES_DASHBOARD_BASIC_AUTH_PASSWORD:-}"
if [ -n "$user" ] && [ -n "$pass" ]; then
  code="$(curl -s -o /dev/null -w '%{http_code}' -u "$user:$pass" "$base/api/plugins/agent-wallet/status" 2>/dev/null)"
  case "$code" in
    200) ok "gateway serves /api/plugins/agent-wallet/status (200)" ;;
    401) note "gateway answered 401 — its auth differs from this shell's credentials" ;;
    404) bad "gateway 404: it did not import the plugin after its restart" ;;
    000) note "gateway not reachable at $base" ;;
    *)   bad "gateway -> $code" ;;
  esac
fi

echo
if [ "$fails" -eq 0 ]; then
  echo "update-check: OK ($version)"
  exit 0
fi
echo "update-check: $fails problem(s)"
exit 1
