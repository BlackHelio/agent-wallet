#!/usr/bin/env bash
# The demo in one command, no real money.
#
#   ./scripts/demo.sh          # simulated payment + x402 quote, then orders, spend and verify
#   ./scripts/demo.sh quote    # only the 402 pricing step
#
# Requires the plugin installed and enabled, with a wallet created (`hermes wallet demo` once first).
# The payment runs on the simulator chain, which keeps
# its own play balances (1 XMR seeded on first use) and broadcasts nothing. The 402 quote is a real
# HTTP call to a throwaway seller on 127.0.0.1, and it is filed as a pending request: nothing is
# paid, and the seller is not given funds.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HERMES="${HERMES_BIN:-hermes}"
PORT="${DEMO_PORT:-38091}"
STEP="${1:-all}"

note() { printf '\n\033[1m── %s\033[0m\n' "$1"; }
pause() { sleep "${1:-1}"; }

# The passphrase file is the scripted path. On a terminal, run the same command without
# --passphrase-stdin and a person types it: the agent never sees it.
passphrase() {
  local file="${WALLET_PASSFILE:-${HERMES_HOME:-$HOME/.hermes}/wallet-dev-passphrase}"
  if [ ! -r "$file" ]; then
    echo "   no passphrase file at $file." >&2
    echo "   create one (a demo wallet's passphrase, not yours):" >&2
    echo "       printf '%s\\n' demo-passphrase > '$file' && chmod 600 '$file'" >&2
    return 1
  fi
  cat "$file"
}

demo_quote() {
  note "A seller quotes a price over HTTP 402"
  local pass
  pass="$(passphrase)" || return 1
  # The seller quotes USDC on Base, which is opt-in under the default coin set; --with-402 enables
  # it first, so the quote does not dead-end on chain_not_enabled.
  printf '%s\n' "$pass" | "$HERMES" wallet coins --enable USDC --passphrase-stdin || return 1
  python3 "$HERE/scripts/pay402_demo_server.py" "$PORT" &
  local server=$!
  sleep 1
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/sku/42" 2>/dev/null || echo "?")"
  echo "   GET http://127.0.0.1:$PORT/sku/42  ->  $code Payment Required"
  pause

  note "The wallet reads that price off the wire — the agent never states it"
  # --allow-loopback: the bundled seller runs on 127.0.0.1. The daemon honours the flag only for
  # operator scope and only for loopback, so the agent's own tool still cannot reach a private host.
  "$HERMES" wallet pay-url "http://127.0.0.1:$PORT/sku/42" --allow-loopback \
      --why "buy an ACME widget" || true
  kill "$server" 2>/dev/null
  wait "$server" 2>/dev/null
  pause
}

demo_pay() {
  local pass
  pass="$(passphrase)" || return 1
  # Unlock first so the receive below is not refused on a locked wallet; `demo` would unlock later.
  printf '%s\n' "$pass" | "$HERMES" wallet unlock --passphrase-stdin || true

  note "Money in: a fresh subaddress and an order id for this order"
  "$HERMES" wallet receive --label "order 42" || true
  pause

  note "Money out: request → policy → human approval → signed → receipt (simulator, play money)"
  printf '%s\n' "$pass" | "$HERMES" wallet demo --with-402 --passphrase-stdin || true
  pause

  note "Incoming orders carry ids, ages and a received-vs-expected status"
  "$HERMES" wallet incoming || true
  pause

  note "Simulated money is labelled, never counted as real"
  "$HERMES" wallet spend --since 30d || true
  pause

  note "The receipt ledger is an Ed25519 hash chain, anchored so truncation shows"
  "$HERMES" wallet verify || true
}

rc=0
case "$STEP" in
  quote) demo_quote || rc=1 ;;
  all)   demo_pay || rc=1 ;;
  *)     echo "usage: $0 [all|quote]" >&2; exit 2 ;;
esac

# A demo that cannot run must not print "Done" and exit 0: that is how it was silently no-op'd.
if [ "$rc" -ne 0 ]; then
  echo "" >&2
  echo "demo stopped before finishing — the message above says why" >&2
  exit 1
fi
note "Done"
