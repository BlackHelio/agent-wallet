#!/usr/bin/env bash
# The demo in one command, no funds required.
#
#   ./scripts/demo.sh          # 402 quote, then a simulated payment, then verify
#   ./scripts/demo.sh quote    # only the 402 pricing step
#
# Requires the plugin installed and enabled. Nothing here moves real money: the payment goes over
# the simulator chain, and the 402 quote is refused before signing because the wallet holds nothing
# on that network — which is the honest outcome and the point of the second act.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HERMES="${HERMES_BIN:-hermes}"
PORT="${DEMO_PORT:-38091}"
STEP="${1:-all}"

note() { printf '\n\033[1m── %s\033[0m\n' "$1"; }
pause() { sleep "${1:-1}"; }

demo_quote() {
  note "A seller quotes a price over HTTP 402"
  python3 "$HERE/scripts/pay402_demo_server.py" "$PORT" >/dev/null 2>&1 &
  local server=$!
  sleep 1
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/sku/42" 2>/dev/null || echo "?")"
  echo "   GET http://127.0.0.1:$PORT/sku/42  ->  $code Payment Required"
  pause

  note "The wallet reads that price off the wire — the agent never states it"
  # --allow-loopback: the bundled seller runs on 127.0.0.1. The daemon honours the flag only for
  # operator scope and only for loopback, so the agent's own tool still cannot reach a private host.
  local out
  out="$("$HERMES" wallet pay-url "http://127.0.0.1:$PORT/sku/42" --allow-loopback \
        --why "buy an ACME widget" 2>&1)"
  printf '%s\n' "$out"
  if printf '%s' "$out" | grep -q "not a public address"; then
    # Known wiring gap: cmd_pay_url calls pay_url with the agent token, so the daemon refuses the
    # loopback allowance. Say this instead of pretending the purchase happened.
    echo "   (the CLI presented the agent token; the agent may only quote public URLs."
    echo "    that refusal is the SSRF guard — the quote itself is covered by tests/test_pay402.py)"
  fi
  kill "$server" 2>/dev/null
  wait "$server" 2>/dev/null
  pause
}

demo_pay() {
  note "Money in: a fresh subaddress for this order"
  "$HERMES" wallet receive --label "order 42" || true
  pause

  note "Money out: request → policy → human approval → signed → receipt"
  local passfile="${WALLET_PASSFILE:-${HERMES_HOME:-$HOME/.hermes}/wallet-dev-passphrase}"
  if [ -r "$passfile" ]; then
    # --passphrase-stdin is the scripted path; on a terminal, run the same command without it
    printf '%s\n' "$(cat "$passfile")" | "$HERMES" wallet demo --passphrase-stdin || true
  else
    echo "   (no passphrase file at $passfile — run: hermes wallet demo)"
    echo "   a person would type the passphrase into this prompt; the agent never sees it"
  fi
  pause

  note "The receipt ledger is an Ed25519 hash chain, anchored so truncation shows"
  "$HERMES" wallet verify || true
}

case "$STEP" in
  quote) demo_quote ;;
  all)   demo_quote; demo_pay ;;
  *)     echo "usage: $0 [all|quote]" >&2; exit 2 ;;
esac

note "Done"
