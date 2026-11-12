"""`hermes wallet ...` — the full lifecycle from a terminal, no desktop required.

Operator actions here ask for the passphrase on the TTY. `--passphrase-stdin` exists for scripts
and tests; there is deliberately no `--passphrase VALUE` flag, so a passphrase never lands in a
shell history or a process list.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import secrets
import functools
import tempfile
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from . import art, client, daemon, service


def _walletd_modules():
    """Import the trusted-side modules whether this runs as a plugin or standalone."""
    try:
        from ..walletd import backup, keystore   # loaded as hermes_plugins.<id>.wallet.cli
        return backup, keystore
    except ImportError:
        import sys
        root = Path(__file__).resolve().parents[1]
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from walletd import backup, keystore
        return backup, keystore

PASS_ENV = "WALLETD_PASSPHRASE"


CHAIN_LABEL = {
    "monero-mainnet": "Monero", "monero-stagenet": "Monero stagenet", "monero-testnet": "Monero testnet",
    "bitcoin-mainnet": "Bitcoin", "bitcoin-testnet": "Bitcoin testnet",
    "xrp-mainnet": "XRP", "xrp-testnet": "XRP testnet",
    "base-mainnet": "Base", "base-sepolia": "Base sepolia",
    "polygon-mainnet": "Polygon", "arbitrum-mainnet": "Arbitrum", "sim-mainnet": "Simulator",
}

COIN_LABEL = {"XMR": "Monero", "BTC": "Bitcoin", "XRP": "XRP", "ETH": "Ether",
              "POL": "Polygon", "USDC": "USD Coin"}


def _rule(width: int = 78) -> str:
    return "\u2500" * width


def _short(value: str, head: int = 14, tail: int = 6) -> str:
    value = str(value or "")
    if len(value) <= head + tail + 1:
        return value
    return f"{value[:head]}\u2026{value[-tail:]}"


def _cell_width(value: str) -> int:
    """Printed cell width, counting a wide glyph (the coin emblems) as two columns.

    `len()` of "◆ ETH" is 5 but it prints 6 wide, so a padded table built with len() breaks every
    column after the first emblem.
    """
    import unicodedata
    width = 0
    for char in value:
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


def _pad(value: str, width: int, right: bool = False) -> str:
    fill = max(0, width - _cell_width(value))
    return (" " * fill + value) if right else (value + " " * fill)


def _balance_cell(row: Dict[str, Any]) -> str:
    """The amount cell: the balance, or a truthful reason there is no number."""
    amount = row.get("balance")
    if amount is None:
        return "\u2014"
    if row.get("state") == "locked" or (not row.get("available") and str(row.get("error")) == "locked"):
        # A locked wallet cannot read a balance. The daemon sends "0"; printing it says the wallet is
        # empty, which is a different claim from "we could not ask".
        return "locked"
    return _amount_text(amount)


def _row_note(row: Dict[str, Any]) -> str:
    """What to say in the address column when there is no address: the reason, once.

    A locked row carries `error: "locked"` and prints "locked" in the state column already; the
    address column then repeated it. The one thing worth adding is *how* to unstick it.
    """
    error = str(row.get("error") or "").strip()
    if row.get("state") == "locked" or error == "locked":
        return "unlock to read"
    if len(error) > 64:
        # A refusal is the most useful thing on the row (a signer note names the command that fixes
        # it), so it keeps its head and its tail rather than losing the fix to a column width.
        return f"{error[:40]}…{error[-20:]}"
    return error


def _balance_table(accounts: List[Dict[str, Any]]) -> str:
    """One line per coin per chain: network, coin, amount, state, address.

    Grouped by chain so the question the operator actually asks — "what is on Base?" — is one run
    of rows: ETH and USDC on Base are both there, not one coin per line with the network hidden
    underneath it. Addresses are shortened to one column; `balances --json` has them whole.
    """
    if not accounts:
        return "  no chains are reachable right now"
    def order(row: Dict[str, Any]) -> tuple:
        kind = str(row.get("kind", ""))
        assets = list((row.get("chain_assets") or []))
        asset = str(row.get("asset", ""))
        return (kind, assets.index(asset) if asset in assets else 99)
    rows = sorted(accounts, key=order)
    widths = {
        "kind": max([11] + [_cell_width(CHAIN_LABEL.get(r.get("kind", ""), r.get("kind", ""))) for r in rows]),
        "asset": max([5] + [_cell_width(f"{art.emblem(r.get('asset', ''))} {r.get('asset', '')}") for r in rows]),
        "label": max([10] + [_cell_width(COIN_LABEL.get(str(r.get("asset", "")), "")) for r in rows]),
        "amount": max([10] + [_cell_width(_balance_cell(r) + " " + str(r.get("asset", ""))) for r in rows]),
        "state": 8,
    }
    header = (f"  {_pad('chain', widths['kind'])}  {_pad('coin', widths['asset'])}  "
              f"{_pad('name', widths['label'])}  {_pad('balance', widths['amount'], True)}  {_pad('state', widths['state'])}  address")
    lines: List[str] = [header]
    last_kind = None
    for row in rows:
        kind = str(row.get("kind", ""))
        if last_kind is not None and kind != last_kind:
            lines.append("")
        last_kind = kind
        asset = str(row.get("asset", "?"))
        chain = CHAIN_LABEL.get(kind, kind)
        coin = f"{art.emblem(asset)} {asset}"
        state = ("live" if row.get("available") and row.get("state") != "locked"
                 else "locked" if row.get("state") == "locked" else (row.get("state") or "offline"))
        amount = f"{_balance_cell(row)} {asset}"
        address = _short(row.get("address", ""), 10, 6) if row.get("address") else _row_note(row)
        lines.append(f"  {_pad(chain, widths['kind'])}  {_pad(coin, widths['asset'])}  "
                     f"{_pad(COIN_LABEL.get(asset, ''), widths['label'])}  "
                     f"{_pad(amount, widths['amount'], True)}  {_pad(state, widths['state'])}  {address}")
    return "\n".join(lines)


def _receipt_table(entries: List[Dict[str, Any]]) -> str:
    if not entries:
        return "  no receipts yet"
    lines = []
    for row in entries:
        details = row.get("details") or {}
        what = ""
        if details.get("amount"):
            what = f"{details['amount']} {details.get('asset', '')}"
            if details.get("payee"):
                what += f" -> {_short(details['payee'], 12, 6)}"
        elif details.get("reason"):
            what = str(details["reason"])[:60]
        elif details.get("file"):
            what = str(details["file"])
        elif details.get("address"):
            what = _short(str(details["address"]), 16, 6)
        stamp = str(row.get("ts", ""))[11:19]
        lines.append(f"  #{row.get('seq', '?'):<4} {stamp:<9} {row.get('kind', ''):<13} "
                     f"{row.get('decision', ''):<13} {row.get('rule', ''):<16} {what}")
    return "\n".join(lines)


def _amount_text(value: Any) -> str:
    """A decimal string without the ledger's trailing zeros: 0.003000000000 -> 0.003.

    The daemon stores exact Decimal strings; printing them raw makes a table of money unreadable.
    This only trims zeros - it never rounds, so the number stays the number.
    """
    text = str(value if value is not None else "").strip()
    if not text:
        return ""
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError):
        return text
    if number == 0:
        return "0"
    return format(number.normalize(), "f")


def _destroy_facts(row: Dict[str, Any]) -> Dict[str, str]:
    """What a destroyed sandbox left behind: the amount swept home and when it died.

    Two sources, because the field arrived after the fact and older receipts predate it:
    `was_destroyed_with` on the sandbox (a dict of receipt fields, or a bare amount) and, failing
    that, the `sandbox/destroyed` receipt that cmd_sandbox joins in. A sandbox with neither still
    has `destroyed_at`, so the time is never invented and the amount is never guessed: an unknown
    sweep is reported as unknown rather than as a zero the operator might read as "all spent".
    """
    facts = {"swept": "", "at": str(row.get("destroyed_at") or "")}
    raw = row.get("was_destroyed_with")
    if isinstance(raw, dict):
        facts["swept"] = str(raw.get("swept") or raw.get("amount") or "")
        facts["at"] = str(raw.get("at") or raw.get("ts") or raw.get("destroyed_at") or facts["at"])
    elif raw is not None and str(raw).strip() and str(raw).strip() != "None":
        facts["swept"] = str(raw)
    if not facts["swept"]:
        facts["swept"] = str(row.get("swept") or "")
    return facts


def _destroy_receipts() -> Dict[str, Dict[str, Any]]:
    """The `sandbox/destroyed` receipts, keyed by sandbox id.

    Read from the ledger rather than from the sandbox row: the receipt is the signed record of what
    was swept, and it exists even for sandboxes destroyed before the row grew the field. A ledger
    that cannot be read is not fatal - the table just says the sweep is unknown.
    """
    try:
        payload = client.call("ledger_tail", {"limit": 200})
    except Exception:                                  # noqa: BLE001 - a report must still print
        return {}
    found: Dict[str, Dict[str, Any]] = {}
    for entry in (payload.get("entries") or []) if isinstance(payload, dict) else []:
        if entry.get("kind") != "sandbox" or entry.get("decision") != "destroyed":
            continue
        details = entry.get("details") or {}
        sandbox_id = str(details.get("sandbox_id") or "")
        if sandbox_id:
            found[sandbox_id] = {**details, "at": entry.get("ts", "")}
    return found


def _with_destroy_detail(row: Dict[str, Any], receipts: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Fill in `was_destroyed_with` from the receipt when the daemon did not send it.

    The shape is the daemon's; this only steps in where it is absent, so a backend that sends the
    field is always the one that is displayed.
    """
    if row.get("status") != "destroyed" or row.get("was_destroyed_with"):
        return row
    receipt = receipts.get(str(row.get("sandbox_id") or ""))
    return {**row, "was_destroyed_with": receipt} if receipt else row


def _sandbox_table(rows: List[Dict[str, Any]]) -> str:
    """One line per sandbox. A destroyed row is never painted as a live budget.

    It used to print `remaining` for every row, so a sandbox that had been destroyed and swept
    still read "0.003 left" - the most expensive line in the table, because it says money is
    somewhere it is not. A destroyed row now shows what was swept and when, and no remaining.
    """
    lines: List[str] = []
    for row in rows:
        asset = str(row.get("asset", ""))
        chain = CHAIN_LABEL.get(row.get("kind", ""), row.get("kind", ""))
        status = str(row.get("status", ""))
        mode = str(row.get("mode", "budget"))
        if status == "destroyed":
            facts = _destroy_facts(row)
            swept = _amount_text(facts["swept"])
            when = facts["at"][11:19] if len(facts["at"]) >= 19 else facts["at"]
            if not facts["swept"]:
                cell, note = f"{'?':>12}", "sweep unreported"
            elif not swept or swept == "0":
                cell, note = f"{'nothing':>12}", "nothing left to sweep"
            else:
                cell, note = f"{swept:>12}", f"swept {swept} {asset}".rstrip()
            detail = " · ".join(part for part in
                                (mode, f"destroyed {when}" if when else "destroyed", note) if part)
            lines.append(f"  {row.get('sandbox_id', ''):<16} {status:<10} {cell} {asset:<5} "
                         f"{chain:<16} {detail}")
            continue
        extra = mode
        if mode == "one_time":
            left = row.get("destroy_in")
            extra += f", self-destructs in {left}s" if left is not None else ", waiting for its payment"
        if row.get("tampered"):
            # the row's seal did not verify: nothing may spend from it, and the numbers on disk are
            # not trustworthy either, so do not print them as if they were a budget
            lines.append(f"  {row.get('sandbox_id', ''):<16} {'TAMPERED':<10} "
                         f"{'—':>12} / {'—':<12} {asset:<5} {chain:<16} "
                         f"record edited outside the wallet; revoke it (leftover goes home)")
            continue
        cell = f"{_amount_text(row.get('remaining')):>12} / {_amount_text(row.get('budget')):<12}"
        lines.append(f"  {row.get('sandbox_id', ''):<16} {status:<10} {cell} {asset:<5} "
                     f"{chain:<16} {extra}")
    return "\n".join(lines)


def _out(payload: Any, as_json: bool = False) -> None:
    """Pretty by default, raw with --json. A CLI that only dumps JSON is a debug tool, not a wallet.

    `--json` is registered twice (globally and per subparser) and arrives here as `args.as_json`;
    the handler is the only place that knows. Sniffing `sys.argv` instead made `hermes wallet --json
    status` print the human block — the argv this process sees is the `hermes` launcher's, so a flag
    the user typed was invisible — and `--json` is not a substring test anywhere else in the CLI.
    """
    if as_json or not isinstance(payload, (dict, list)):
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    if isinstance(payload, dict):
        if "accounts" in payload:
            print(_balance_table(payload["accounts"]))
            return
        if "status" in payload:      # cmd_status passes the wallet dict through
            print(_status_block(payload))
            return
        if isinstance(payload.get("entries"), list):
            print(_receipt_table(payload["entries"]))
            return
        if "pubkey" in payload and "entries" in payload:
            # ledger_verify: entries is a COUNT, not a list — treat it as the result it is
            print(f"  receipts   {payload.get('entries')} ({'ok' if payload.get('ok') else 'FAILED'})")
            print(f"  message    {payload.get('message', '')}")
            anchor = payload.get("anchor") or {}
            if anchor:
                print(f"  anchor     {'ok' if anchor.get('ok') else 'FAILED'}"
                      + (f" — {anchor.get('reason', '')}" if anchor.get("reason") else ""))
            return
        if "requests" in payload:
            rows = payload["requests"]
            if not rows:
                print("  no requests")
                return
            for row in rows:
                print(f"  {row.get('request_id', ''):<18} {row.get('status', ''):<9} "
                      f"{row.get('amount', ''):>22} {row.get('asset', ''):<5} "
                      f"{CHAIN_LABEL.get(row.get('kind', ''), row.get('kind', '')):<16} "
                      f"{_short(row.get('payee', ''), 16, 6)}")
            return
        if "sandboxes" in payload:
            rows = payload["sandboxes"]
            if not rows:
                print("  no sandboxes")
                return
            print(_sandbox_table(rows))
            return
        if "mandates" in payload:
            rows = payload["mandates"]
            if not rows:
                print("  no autopays")
                return
            for row in rows:
                usage = row.get("usage") or {}
                print(f"  {row.get('mandate_id', ''):<14} {row.get('status', ''):<9} "
                      f"{row.get('label') or '(no label)':<20} "
                      f"<= {row.get('per_tx_cap')} / {row.get('period_cap')} per {row.get('period_hours')}h"
                      + (f", {usage.get('period_left')} left this window" if usage else ""))
            return
    print(json.dumps(payload, indent=2, sort_keys=True))


def _banner(wallet: Dict[str, Any]) -> str:
    """A small banner: this is a wallet, it says so, and it says whether it is open."""
    unlocked = not wallet.get("locked", True)
    state = "UNLOCKED" if unlocked else "LOCKED  "
    coins = "/".join(wallet.get("assets", []))
    art = [
        "   \u2584\u2584\u2584\u2584\u2584\u2584\u2584\u2584\u2584\u2584\u2584\u2584\u2584\u2584\u2584\u2584\u2584\u2584\u2584\u2584\u2584\u2584\u2584\u2584\u2584\u2584\u2584",
        "  \u2588  \u250c\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2510  \u2588   agent-wallet",
        f"  \u2588  \u2502 \u2593\u2593\u2593\u2593\u2593\u2593\u2593\u2593\u2593 \u2502  \u2588   {state}",
        "  \u2588  \u2514\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2518  \u2588   keys live in walletd only",
        f"   \u2580\u2580\u2580\u2580\u2580\u2580\u2580\u2580\u2580\u2580\u2580\u2580\u2580\u2580\u2580\u2580\u2580\u2580\u2580\u2580\u2580\u2580\u2580\u2580\u2580\u2580\u2580   {coins}",
    ]
    return "\n".join(art)


def _status_block(payload: Dict[str, Any]) -> str:
    # cmd_status wraps the payload as {"status": {...}} so _out can tell it apart from balances;
    # the daemon's own shape nests it under "wallet" and older builds return it flat.
    wallet = payload.get("status") or payload.get("wallet") or payload
    lines = [_banner(wallet), ""]
    if wallet.get("home"):
        lines.append(f"  home       {wallet['home']}")
    signer = "ready" if wallet.get("signer_reachable") else ("installed" if wallet.get("signer_installed") else "not installed")
    ttl = wallet.get("unlock_expires_in")
    lock_text = "unlocked" + (f" (locks in {max(1, round(ttl / 60))}m)" if isinstance(ttl, int) else "") \
        if not wallet.get("locked") else "locked"
    lines.append(f"  wallet     {lock_text}")
    held = wallet.get("assets", [])
    lines.append("  coins      " + "  ".join(f"{art.emblem(a)} {a}" for a in held))
    lines.append(f"  chains     {', '.join(CHAIN_LABEL.get(c, c) for c in wallet.get('chains', []))}")
    lines.append(f"  signer     {signer}")
    lines.append(f"  gate       {'passphrase prompt on' if wallet.get('human_gate') else 'passphrase prompt OFF'}")
    caps = wallet.get("caps") or {}
    per_tx = caps.get("per_tx") or {}
    coins_held = set(wallet.get("assets", []))
    shown_caps = {a: v for a, v in per_tx.items() if not coins_held or a in coins_held}
    if shown_caps:
        lines.append("  caps       " + ", ".join(f"{a} {v}" for a, v in sorted(shown_caps.items())))
    lines.append(f"  pending    {wallet.get('pending_requests', 0)}   "
                 f"autopays {wallet.get('active_mandates', 0)}   "
                 f"{wallet.get('ledger_archives', 0)} archive(s)")
    if wallet.get("prefer_monero") and not wallet.get("monero_only"):
        lines.append("  preference   Monero preferred — an agent payment in another coin must say why")
    elif wallet.get("monero_only"):
        lines.append("  preference   Monero only — other coins are refused")
    if wallet.get("config_tampered"):
        lines.append("  WARNING    settings signature does not match - defaults were restored")
    return "\n".join(lines)


def _fail(message: str, code: int = 2) -> int:
    """A refusal is one line on stderr: `error: code: message`, the same shape as `_bound`.

    It used to be a pretty-printed JSON object, so an operator reading a terminal got four lines
    of braces for one sentence, and a script had to parse JSON from stderr to find the reason.
    `--json` callers get their payloads on stdout; failures are for humans here.
    """
    code_name, _, text = str(message).partition(": ")
    if not text:
        code_name, text = "error", str(message)
    print(f"error: {code_name}: {text}", file=sys.stderr)
    return code


def _warn_home_override() -> None:
    """Say loudly when AGENT_WALLET_HOME points somewhere other than this Hermes home.

    A subagent exported it once and every later "live" check in that shell silently talked to a
    scratch wallet: status looked healthy, balances looked empty, and a passphrase that was correct
    was rejected. One line on stderr turns that from a mystery into a message.
    """
    override = os.environ.get("AGENT_WALLET_HOME")
    if not override:
        return
    try:
        resolved = client.wallet_home()
    except Exception:  # noqa: BLE001 - a broken override is reported by the call itself
        resolved = Path(override)
    print(f"  wallet home {resolved}  (from AGENT_WALLET_HOME)", file=sys.stderr)


def _ensure_daemon() -> None:
    _warn_home_override()
    try:
        client.call("status", timeout=2)
    except client.WalletUnavailable:
        started = daemon.start()
        if not started.get("ok"):
            raise client.WalletUnavailable(
                f"walletd is not running and could not be started: {started.get('error') or started}")


def _ensure_initialized(passphrase: str, pin: str = "") -> bool:
    """Create the keystore when the home is empty, before anything asks a daemon for one.

    `wallet demo` on a fresh machine used to stop at `no_keystore`: the daemon cannot start without a
    keystore, and the bootstrap branch ran after the daemon call. Returns False when there is no
    passphrase to bootstrap with, so the caller can say what it needs.
    """
    home = client.wallet_home()
    if (home / "keystore.json").exists():
        return True
    if not passphrase:
        return False
    print(f"0. bootstrap  creating the keystore in {home} (keys stay in the daemon)")
    result = daemon.bootstrap(home, passphrase, pin=pin or os.environ.get("WALLETD_PIN", ""))
    if not result.get("ok"):
        print(f"   bootstrap failed: {result.get('message') or result}", file=sys.stderr)
        return False
    return True


def _passphrase(args: argparse.Namespace, prompt: str = "Wallet passphrase (or the 6-digit PIN): ") -> str:
    """The secret that opens the wallet: the passphrase, or the six-digit PIN.

    The daemon already falls back from a six-digit passphrase to the PIN, so this only has to hand
    over what was typed. It must not check the length or the digits here: rejecting a short value as
    "not a passphrase" is exactly what made the CLI refuse the secret the daemon accepts.
    """
    if getattr(args, "passphrase_stdin", False):
        line = sys.stdin.readline()
        if not line:
            raise SystemExit("no passphrase on stdin")
        return line.rstrip("\n")
    if not sys.stdin.isatty():
        raise SystemExit("refusing to read a passphrase without a TTY (use --passphrase-stdin for scripts)")
    return getpass.getpass(prompt)


def _unlock_if_needed(args: argparse.Namespace) -> None:
    status = client.call("status")
    if status.get("locked"):
        # `{"passphrase": …}` on purpose: the daemon routes a six-digit value to the PIN path itself,
        # so one code path opens the wallet with either secret.
        client.call("unlock", {"passphrase": _passphrase(args)}, scope=client.OPERATOR)


def _settings() -> Dict[str, Any]:
    try:
        return client.call("config_get").get("settings", {})
    except client.WalletUnavailable:
        return {}


def _confirm(prompt: str) -> bool:
    if not sys.stdin.isatty():
        return False
    return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")


# ---------- commands ----------

def cmd_ready(args: argparse.Namespace) -> int:
    """GO / NO-GO for one real payment: builds the transaction, sends nothing."""
    _ensure_daemon()
    params: Dict[str, Any] = {}
    for key in ("kind", "asset", "amount", "payee"):
        value = getattr(args, key, "")
        if value:
            params[key] = value
    result = client.call("readiness", params)
    if args.as_json:
        _out(result, True)
        return 0 if result.get("verdict") == "go" else 1
    print(f"  {result['kind']}  {result['asset']}"
          + (f"  {result['amount']}" if result.get("amount") else "")
          + f"   -> {result['verdict'].upper()}")
    for check in result["checks"]:
        mark = "ok  " if check["ok"] else ("skip" if check.get("skipped") else "FAIL")
        print(f"  {mark} {check['check']:<20} {check['detail']}")
        if not check["ok"] and check.get("remedy"):
            print(f"       -> {check['remedy']}")
    if result["verdict"] != "go":
        print("\n  not ready: fix the FAIL lines above before approving a real payment")
        return 1
    print("\n  ready: approve a request on this chain and it will sign and broadcast")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    _ensure_daemon()
    result = client.call("doctor")
    if args.as_json:
        _out(result, True)
        return 0
    for check in result["checks"]:
        detail = str(check.get("detail") or "").strip()
        skipped = bool(check.get("skipped"))
        mark = "ok  " if check["ok"] else ("skip" if skipped else "FAIL")
        print(f"  {mark}  {check['check']}" + (f"   {detail}" if detail else ""))
        if not check["ok"] and check.get("remedy"):
            print(f"        fix: {check['remedy']}")
    print(f"\n  chains     {', '.join(c['kind'] for c in result['chains'] if c.get('available')) or 'none live'}")
    print(f"  receipts   {result['receipts']}")
    print(f"  contacts   {result['contacts']}   sandboxes {result['sandboxes']}   pending {result['pending_requests']}")
    print(f"  signer     {result['rpc_url']}  (wallet file: {result['wallet_file']})")
    return 0 if result["ok"] else 1


def _read_secret(use_stdin: bool, prompt: str) -> str:
    """Read a secret: one line from stdin for scripts, otherwise a typed prompt on a TTY.

    Deliberately not a `--pin 123456` flag: a six-digit secret on the command line lands in the
    shell history and in every process listing on the box.
    """
    if use_stdin:
        line = sys.stdin.readline()
        return "" if not line else line.rstrip("\n")
    if not sys.stdin.isatty():
        raise SystemExit("refusing to read a PIN without a TTY (use --pin-stdin for scripts)")
    return getpass.getpass(prompt)


def cmd_pin(args: argparse.Namespace) -> int:
    """The six-digit secret typed before every payment. Not the passphrase, and not a session."""
    _ensure_daemon()
    if args.action == "status":
        payload = client.call("pin_status")
        if args.as_json:
            _out(payload, True)
            return 0
        mark = "set" if payload.get("set") else "NOT SET"
        print(f"  pin        {mark}")
        print(f"  required   {'every payment' if payload.get('required') else 'only when disabled'}")
        if payload.get("set"):
            print(f"  changed    {payload.get('updated_at') or payload.get('created_at')}")
        print(f"  attempts   {payload.get('attempts_left')} left")
        if payload.get("locked_until"):
            print(f"  locked     until {payload['locked_until']}")
        if not payload.get("set"):
            print("\n  set one:   hermes wallet pin set")
        return 0
    if args.action == "set":
        # with --pin-stdin the PIN is the FIRST line and the passphrase the second
        first = _read_secret(args.pin_stdin, "New PIN (six digits): ")
        if not first:
            return _fail("no PIN supplied")
        if args.pin_stdin:
            confirm = first
        else:
            confirm = _read_secret(False, "PIN again (six digits): ")
            if confirm != first:
                return _fail("the two PINs differ")
        payload = {"pin": first, "confirm": confirm, "passphrase": _passphrase(args)}
        result = client.call("set_pin", payload, scope=client.OPERATOR)
        if args.as_json:
            _out(result, True)
            return 0
        print("  pin set — every payment will ask for it")
        return 0
    return _fail(f"unknown pin action {args.action!r}")


def cmd_passphrase(args: argparse.Namespace) -> int:
    """Re-seal the keystore under a new passphrase. Nothing else in the wallet changes.

    The passphrase is the key to the file, not a setting: everything else — the master key, the ledger
    seed, the chain keys, the Monero wallet password — lives *inside* the sealed payload, so re-sealing
    it leaves all of them intact. Losing the new passphrase loses the wallet, which is why it is typed
    twice and why the seed is still the only real backup.
    """
    _ensure_daemon()
    if args.action != "change":
        return _fail(f"unknown passphrase action {args.action!r}")
    if args.passphrase_stdin:
        # three lines, in the order they are printed: current, new, confirm
        lines = [sys.stdin.readline().rstrip("\n") for _ in range(3)]
        current, new, confirm = (lines + ["", "", ""])[:3]
    else:
        current = _read_secret(False, "Current passphrase: ")
        new = _read_secret(False, "New passphrase (8+ characters): ")
        confirm = _read_secret(False, "New passphrase again: ")
    if not current:
        return _fail("no passphrase supplied")
    if len(new) < 8:
        return _fail("a passphrase is at least 8 characters")
    if confirm != new:
        return _fail("the two new passphrases differ")
    result = client.call("change_passphrase",
                         {"passphrase": current, "new_passphrase": new, "confirm": confirm},
                         scope=client.OPERATOR)
    if args.as_json:
        _out(result, True)
        return 0
    print("  passphrase changed — the old one no longer opens this wallet")
    return 0


def cmd_signer(args: argparse.Namespace) -> int:
    """Install, start, stop or inspect the local Monero signer (monero-wallet-rpc)."""
    _ensure_daemon()
    if args.action == "status":
        _out(client.call("signer_status"), args.as_json)
        return 0
    _unlock_if_needed(args)
    if args.action == "install":
        print("downloading the official release and verifying its sha256 — a minute or two…", flush=True)
        _out(client.call("signer_install", {}, scope=client.OPERATOR, timeout=900), args.as_json)
        return 0
    if args.action == "repair":
        print("checking the signer binary and replacing it if it cannot run…", flush=True)
        _out(client.call("signer_repair", {}, scope=client.OPERATOR, timeout=900), args.as_json)
        return 0
    if args.action == "start":
        _out(client.call("signer_start", {"port": args.port, "node": args.node or ""},
                         scope=client.OPERATOR, timeout=120), args.as_json)
        return 0
    _out(client.call("signer_stop", {}, scope=client.OPERATOR), args.as_json)
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    """Create the chain wallet if it does not exist — the 'install and it just works' step."""
    if not _ensure_initialized(_passphrase(args) if args.passphrase_stdin or sys.stdin.isatty() else ""):
        return _fail("this wallet home has no keystore yet; run it on a TTY or pass --passphrase-stdin")
    _ensure_daemon()
    # The wallet-file password lives in the keystore now, so unlocking is what provisions it.
    _unlock_if_needed(args)
    result = client.call("wallet_init", {"kind": args.kind}, scope=client.OPERATOR, timeout=900)
    _out(result, args.as_json)
    if not args.as_json:
        print("\nthe wallet-file password lives inside the encrypted keystore; "
              "back the whole wallet up with `hermes wallet backup`.")
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    """Restore a wallet from recovery words typed at the TTY (never echoed, never logged)."""
    # `_ensure_daemon` refuses without a keystore, so a fresh home could never run the one command
    # that exists to create one. Bootstrap first, exactly like demo/init.
    if not _ensure_initialized(_passphrase(args)):
        return _fail("this wallet home has no keystore yet; run it on a TTY or pass --passphrase-stdin")
    _ensure_daemon()
    _unlock_if_needed(args)
    if args.passphrase_stdin:
        seed = sys.stdin.readline().rstrip("\n")
    else:
        seed = getpass.getpass("Recovery words (25 words, hidden): ")
    if not seed.strip():
        return _fail("no recovery words supplied")
    result = client.call("wallet_import", {"kind": args.kind, "seed": seed,
                                           "restore_height": args.restore_height,
                                           # the daemon refuses an import without both: it swaps the
                                           # keys behind the address the pane shows, so the human who
                                           # funds that address has to be the one asking
                                           "passphrase": _passphrase(args),
                                           "confirm": True},
                         scope=client.OPERATOR)
    return _out({"kind": result["kind"], "restored": True, "address": result["address"]}, args.as_json) or 0


def cmd_merchants(args: argparse.Namespace) -> int:
    _ensure_daemon()
    _out(client.call("merchants", {"query": args.query or "", "category": args.category or ""}), args.as_json)
    return 0


def cmd_contacts(args: argparse.Namespace) -> int:
    _ensure_daemon()
    if args.action == "list" or not args.action:
        _out(client.call("contacts_list"), args.as_json)
        return 0
    if args.action == "save":
        if not (args.name and args.address):
            return _fail("contacts save needs --name and --address [--kind]")
        payload: Dict[str, Any] = {"name": args.name,
                                   "addresses": {args.kind: args.address},
                                   "notes": args.notes or ""}
        # Operator scope matters beyond permissions: a contact saved by the agent carries
        # `created_by: agent` and does NOT bless a destination, while a human's does. Saving from the
        # CLI is a human act, so it must say so — otherwise the only way to bless an address would be
        # a route no shipped surface offers.
        payload["passphrase"] = _passphrase(args)
        _out(client.call("contact_save", payload, scope=client.OPERATOR), args.as_json)
        return 0
    _unlock_if_needed(args)
    # Freeing a name is a policy change: the daemon wants the passphrase, and not sending one made
    # deletion impossible from the CLI even on a TTY.
    _out(client.call("contact_delete", {"name": args.name, "passphrase": _passphrase(args)},
                     scope=client.OPERATOR), args.as_json)
    return 0


def cmd_deposits(args: argparse.Namespace) -> int:
    """Deposit addresses: a receive account whose balance merges into the main wallet."""
    _ensure_daemon()
    action = args.action or "list"
    if action == "list":
        payload = client.call("deposits_list")
        rows = payload.get("deposits") or []
        if args.as_json:
            _out(payload, True)
            return 0
        if not rows:
            print("  no deposit addresses yet — `hermes wallet deposits create --label \"shop\"`")
            return 0
        for row in rows:
            live = row.get("unlocked")
            print(f"  {row['deposit_id']}  {row.get('label') or '(no label)'}  "
                  f"{row.get('balance') or '?'} XMR ({live} spendable)  "
                  f"{'auto' if row.get('auto_merge') else 'manual'}"
                  f"{'  merged ' + row['merged_total'] if row.get('merged_total') not in (None, '0') else ''}")
            print(f"      {row.get('address')}")
            if row.get("error"):
                print(f"      {row['error']}")
        return 0
    if action == "create":
        payload = {"kind": args.kind or "", "label": args.label or "", "auto_merge": not args.manual}
        if not args.manual:
            # arming the address to spend a fee on its own is a policy decision, so the daemon
            # wants the passphrase; without this a script could never create an auto-merging one
            payload["passphrase"] = _passphrase(args)
        result = client.call("deposit_create", payload, scope=client.OPERATOR)
        _out(result, args.as_json)
        if not args.as_json:
            dep = result.get("deposit") or {}
            print(f"  \n  send to: {dep.get('address')}")
            print(f"  it merges into the main account automatically once the funds unlock.")
        return 0
    if action == "merge":
        if not args.deposit_id:
            return _fail("merge needs --deposit-id (see `hermes wallet deposits list`)")
        result = client.call("deposit_merge", {"deposit_id": args.deposit_id}, scope=client.OPERATOR)
        _out(result, args.as_json)
        return 0
    if action == "merge-orders":
        result = client.call("orders_merge", {}, scope=client.OPERATOR)
        _out(result, args.as_json)
        return 0
    if action == "auto":
        if not args.deposit_id:
            return _fail("auto needs --deposit-id")
        result = client.call("deposit_set_auto", {"deposit_id": args.deposit_id,
                                                  "auto_merge": not args.off}, scope=client.OPERATOR)
        _out(result, args.as_json)
        return 0
    return _fail(f"unknown deposits action {action!r}")


def cmd_sandbox(args: argparse.Namespace) -> int:
    _ensure_daemon()
    if args.action == "list" or not args.action:
        payload = client.call("sandboxes_list")
        rows = payload.get("sandboxes") if isinstance(payload, dict) else None
        if not args.as_json and rows and any(r.get("status") == "destroyed" for r in rows):
            # The sweep amount lives in the destroy receipt, and a daemon that predates
            # `was_destroyed_with` does not repeat it on the row. Join it here so the table can
            # say what happened to the money rather than only that the sandbox is gone.
            receipts = _destroy_receipts()
            payload = {**payload, "sandboxes": [_with_destroy_detail(r, receipts) for r in rows]}
        _out(payload, args.as_json)
        return 0
    if args.action == "sweep":
        if not args.sandbox_id:
            return _fail("sandbox sweep needs --sandbox-id")
        _unlock_if_needed(args)
        _out(client.call("sandbox_sweep", {"sandbox_id": args.sandbox_id,
                                           "profile": client.current_profile()},
                         scope=client.OPERATOR), args.as_json)
        return 0
    if args.action == "arm":
        # a budget the agent asked for on a chain with nothing to fund (Bitcoin, EVM): there is no
        # funding request to approve, so this is the operator's yes
        if not args.sandbox_id:
            return _fail("sandbox arm needs --sandbox-id (see `hermes wallet sandbox list`)")
        _unlock_if_needed(args)
        _out(client.call("sandbox_arm", {"sandbox_id": args.sandbox_id,
                                         "passphrase": _passphrase(args)},
                         scope=client.OPERATOR), args.as_json)
        return 0
    if args.action == "unattended":
        if not args.sandbox_id:
            return _fail("sandbox unattended needs --sandbox-id")
        _unlock_if_needed(args)
        payload: Dict[str, Any] = {"sandbox_id": args.sandbox_id, "on": not args.off,
                                   "passphrase": _passphrase(args)}
        if not args.off:
            pin = _read_secret(getattr(args, "pin_stdin", False), "PIN (six digits): ")
            if not pin:
                return _fail("arming a budget needs the PIN: add --pin-stdin and pipe it")
            payload["pin"] = pin
        result = client.call("sandbox_set_unattended", payload, scope=client.OPERATOR)
        if args.as_json:
            _out(result, True)
            return 0
        sandbox = result.get("sandbox") or {}
        if result.get("warning"):
            print(f"  note     {result['warning']}")
        if result.get("unattended"):
            print(f"  {sandbox.get('sandbox_id')}  ARMED: the agent may pay any address from this "
                  f"budget without asking, up to {sandbox.get('remaining')} {sandbox.get('asset')}")
            print("  take it back: hermes wallet sandbox unattended --sandbox-id "
                  f"{sandbox.get('sandbox_id')} --off")
        else:
            print(f"  {sandbox.get('sandbox_id')}  disarmed: every payment from it needs your PIN again")
        return 0
    if args.action == "fund":
        if not args.sandbox_id or not args.amount:
            return _fail("sandbox fund needs --sandbox-id and --amount")
        _unlock_if_needed(args)
        _out(client.call("sandbox_topup", {"sandbox_id": args.sandbox_id, "amount": args.amount,
                                           "why": args.why}, scope=client.OPERATOR), args.as_json)
        return 0
    if args.action == "payees":
        if not args.sandbox_id:
            return _fail("sandbox payees needs --sandbox-id")
        if not getattr(args, "pay_to", None) and not getattr(args, "clear", False):
            payout: Dict[str, Any] = {"sandbox_id": args.sandbox_id,
                                      "allowed_payees": None}
            rows = client.call("sandboxes_list").get("sandboxes") or []
            row = next((r for r in rows if r.get("sandbox_id") == args.sandbox_id), None)
            if row is None:
                return _fail(f"no sandbox {args.sandbox_id!r}")
            allowed = row.get("allowed_payees") or []
            if args.as_json:
                _out({"sandbox_id": args.sandbox_id, "allowed_payees": allowed}, True)
                return 0
            print(f"  {args.sandbox_id}  may pay without asking: "
                  f"{', '.join(allowed) if allowed else 'saved contacts only'}")
            print("  change it: --pay-to ADDRESS (repeatable, needs the passphrase to widen) or --clear")
            return 0
        payload: Dict[str, Any] = {"sandbox_id": args.sandbox_id,
                                   "allowed_payees": [] if getattr(args, "clear", False)
                                   else list(args.pay_to or []),
                                   "passphrase": _passphrase(args)}
        result = client.call("sandbox_set_payees", payload, scope=client.OPERATOR)
        _out(result, args.as_json)
        return 0
    if args.action == "create":
        missing = [flag for flag, value in (("--kind", args.kind), ("--budget", args.budget)) if not value]
        if missing:
            # naming only the flag that *is* present sends the operator in a circle
            return _fail(f"sandbox create needs {' and '.join(missing)}")
        _unlock_if_needed(args)
        payload = {"kind": args.kind, "asset": args.asset, "budget": args.budget,
                   "lifetime_hours": args.hours, "label": args.label or "agent sandbox",
                   "allowed_payees": list(getattr(args, "pay_to", None) or []),
                   "mode": "one_time" if getattr(args, "one_time", False) else "budget"}
        if getattr(args, "unattended", False):
            payload["passphrase"] = _passphrase(args)
            # An unattended budget is the operator handing the agent a prepaid card: the daemon wants
            # both secrets for that, so read the PIN the same way `approve` does.
            payload["unattended"] = True
            pin = _read_secret(getattr(args, "pin_stdin", False), "PIN (six digits): ")
            if not pin:
                return _fail("--unattended needs the PIN: add --pin-stdin and pipe it")
            payload["pin"] = pin
        if getattr(args, "destruct_after", None):
            payload["self_destruct_seconds"] = args.destruct_after
        created = client.call("sandbox_create", payload, scope=client.OPERATOR)
        if args.as_json:
            _out(created, True)
            return 0
        sandbox = created.get("sandbox") or {}
        funding = (created.get("funding") or {}).get("request") or {}
        print(f"  sandbox   {sandbox.get('sandbox_id')}  {sandbox.get('budget')} {sandbox.get('asset')}"
              f"  {sandbox.get('mode')}")
        if sandbox.get("address"):
            print(f"  address   {sandbox.get('address')}")
        if sandbox.get("pooled") or sandbox.get("mode") == "pooled":
            print("  budget    enforced against the main account — no funds moved, so nothing to fund or sweep")
            # The reason is per chain: naming Bitcoin on a Base sandbox sent people looking for a
            # transfer that was never needed.
            print(f"            {sandbox.get('kind', 'this chain')} cannot hold a separate sandbox account,")
            print("            so the cap is enforced by policy instead of by custody")
            return 0
        if funding.get("status") == "pending":
            print("\n  next: approve the funding transfer, and the budget moves into that account:")
            # `--amount` matters: funding above the per-coin threshold asks for the amount typed, and
            # `--yes` is what makes the line work when it is pasted into a non-interactive shell
            print(f"        hermes wallet approve {funding.get('request_id')} "
                  f"--amount {funding.get('amount')} --yes")
            print(f"        hermes wallet sandbox list      (shows live_balance once it lands)")
        elif funding.get("status") == "denied":
            reason = (funding.get("decision") or {}).get("reason") or funding.get("status")
            print(f"\n  funding refused: {reason}")
            print("        top the wallet up, then re-run this command")
        return 0
    if args.action == "destroy":
        if not args.sandbox_id:
            return _fail("sandbox destroy needs --sandbox-id")
        _unlock_if_needed(args)
        params = {"sandbox_id": args.sandbox_id, "reason": "manual",
                  "force": getattr(args, "force", False)}
        _out(client.call("sandbox_destroy", params, scope=client.OPERATOR), args.as_json)
        return 0
    if args.action == "reap":
        _unlock_if_needed(args)
        _out(client.call("sandboxes_reap", {}, scope=client.OPERATOR), args.as_json)
        return 0
    _unlock_if_needed(args)
    params = {"sandbox_id": args.sandbox_id, "sweep": not getattr(args, "no_sweep", False)}
    _out(client.call("sandbox_revoke", params, scope=client.OPERATOR), args.as_json)
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    backup, keystore = _walletd_modules()

    out = Path(args.out).expanduser() if args.out else (
        client.wallet_home() / f"wallet-backup-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.json")
    passphrase = _passphrase(args)
    try:
        _out(backup.create(client.wallet_home(), out, passphrase), args.as_json)
    except (FileNotFoundError, keystore.KeystoreError) as exc:
        return _fail(str(exc))
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    backup, keystore = _walletd_modules()

    if not args.archive:
        return _fail("restore needs --archive <file>")
    passphrase = _passphrase(args)
    try:
        _out(backup.restore(Path(args.archive).expanduser(), client.wallet_home(),
                            passphrase, force=bool(args.force)), args.as_json)
    except (FileExistsError, FileNotFoundError, keystore.KeystoreError) as exc:
        return _fail(str(exc))
    return 0


def cmd_seed(args: argparse.Namespace) -> int:
    """Print the recovery seed to the terminal. Operator scope + passphrase + explicit confirm."""
    _ensure_daemon()
    _unlock_if_needed(args)
    if not args.confirm:
        return _fail("refusing without --confirm: this prints 25 words that own the wallet")
    passphrase = _passphrase(args)
    result = client.call("export_seed", {"passphrase": passphrase, "confirm": True,
                                         "kind": args.kind}, scope=client.OPERATOR)
    if args.as_json:
        print(json.dumps(result, indent=2))
        return 0
    print("Recovery seed — write it down offline, never paste it into a chat or a shell history:")
    for i in range(0, len(result["mnemonic"].split()), 5):
        chunk = result["mnemonic"].split()[i:i + 5]
        print("  " + "  ".join(f"{i + j + 1:>2}. {w}" for j, w in enumerate(chunk)))
    print(f"\n{result['warning']}")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    """Walk the whole flow end to end on the simulated chain: request → approve → receipt.

    No real funds move: `sim-mainnet` keeps its own play balances (funding, spending, sweeping and
    teardown all move that fake money), broadcasts nothing, and marks every receipt simulated. It exists so the flow can be shown and reviewed
    without money and without a funded wallet.
    """
    # A demo on the real home used to bootstrap a live wallet with the published PIN 123456 and print
    # it as if it were harmless. It runs on its own scratch home now: a demo that can arm your real
    # wallet is not a demo.
    if getattr(args, "home", None):
        os.environ["AGENT_WALLET_HOME"] = str(Path(args.home).expanduser())
    elif os.environ.get("AGENT_WALLET_HOME"):
        pass          # an explicit home (a script's scratch wallet) is honoured, not overridden
    else:
        scratch = Path(tempfile.mkdtemp(prefix="hermes-wallet-demo-"))
        os.environ["AGENT_WALLET_HOME"] = str(scratch)
        if not getattr(args, "as_json", False):
            print(f"   demo home  {scratch}  (a throwaway wallet; your real one is untouched)")
    passphrase = _passphrase(args)
    # The keystore has to exist before the daemon can start: bootstrap first, then talk to it.
    # a demo wallet gets a demo PIN, and says so: the point is to show the gate, not to hide it
    demo_pin = os.environ.get("WALLETD_PIN", "123456")
    if not _ensure_initialized(passphrase, pin=demo_pin):
        return _fail("this wallet home has no keystore yet; run it on a TTY or pass --passphrase-stdin")
    if not getattr(args, "as_json", False):
        print(f"   demo PIN   {demo_pin}  (this throwaway wallet only, never your real one)")
    _ensure_daemon()
    status = client.call("status")
    if status.get("locked"):
        client.call("unlock", {"passphrase": passphrase}, scope=client.OPERATOR)
        status = client.call("status")
    if not status.get("signer_installed") and not getattr(args, "no_install", False):
        print("0. bootstrap  installing the Monero signer (official binary, SHA256-pinned)")
        client.call("signer_install", {}, scope=client.OPERATOR)
    if not status.get("signer_reachable") and not getattr(args, "no_install", False):
        client.call("signer_start", {}, scope=client.OPERATOR)
        import time as _time
        for _ in range(20):
            _time.sleep(1)
            if client.call("status").get("signer_reachable"):
                break
    kind = args.kind or "sim-mainnet"
    if kind not in client.call("status").get("chains", []):
        chains = list(client.call("status").get("chains", [])) + [kind]
        client.call("config_set", {"chains_enabled": chains, "passphrase": passphrase},
                    scope=client.OPERATOR)
        import time as _time
        _time.sleep(6)
    amount = args.amount or "0.001"

    balances = client.call("balances").get("accounts", [])
    account = next((a for a in balances if a.get("kind") == kind), None)
    payee = args.payee or (account or {}).get("address", "")
    if not payee:
        return _fail(f"no address for {kind}; pass --payee")

    print(f"1. request   {amount} XMR on {kind} → {payee[:16]}…")
    requested = client.call("request_tx", {"kind": kind, "asset": "XMR", "amount": amount,
                                           "payee": payee, "why": "demo run",
                                           "profile": client.current_profile()})
    decision = requested["decision"]
    print(f"   policy    allow={decision['allow']} rule={decision['rule']} code={decision['code']}")
    if not decision["allow"]:
        _out(requested, args.as_json)
        return 1

    approval_id = requested.get("approval_id")
    request_id = requested["request"]["request_id"]
    if approval_id:
        print(f"2. approve   request {request_id} (operator)")
        approved = client.call("approve", {"request_id": request_id, "approval_id": approval_id,
                                           "confirm_amount": amount, "passphrase": passphrase,
                                           "pin": demo_pin},
                               scope=client.OPERATOR)
    else:
        approved = requested
    print(f"   status    {approved['request']['status']}  simulated={approved.get('simulated', False)}")
    if approved.get("tx_hash"):
        print(f"   tx        {approved['tx_hash'][:24]}…")

    verified = client.call("ledger_verify")
    tail = client.call("ledger_tail", {"limit": 3}).get("entries", [])
    print(f"3. receipt   chain ok={verified['ok']} entries={verified['entries']}")
    for entry in tail:
        details = entry.get("details", {})
        print(f"   #{entry['seq']:<3} {entry['kind']:<14} {entry['decision']:<9} "
              f"{(details.get('amount') or ''):>8} {details.get('asset') or ''}"
              + ("  [simulated]" if details.get("simulated") else ""))
    if getattr(args, "with_402", False):
        _demo_402(passphrase, args)
    return 0


def _demo_402(passphrase: str, args: argparse.Namespace) -> int:
    """Price a purchase from the bundled x402 seller, end to end.

    The seller quotes what real ones quote (USDC on Base), which is opt-in under the default coin
    set — so the demo turns it on with the operator's passphrase instead of dead-ending on
    `chain_not_enabled`. Nothing is broadcast here: with no USDC funded the request stays pending for
    the human, which is exactly what the screenshot should show.
    """
    script = Path(__file__).resolve().parent.parent / "scripts" / "pay402_demo_server.py"
    if not script.exists():
        print(f"4. x402      skipped: {script} is not installed beside the plugin")
        return 0
    status = client.call("status")
    if "USDC" not in (status.get("assets") or []):
        print("4. x402      enabling USDC on Base for the quote (opt-in under the default coin set)")
        client.call("config_set", {"assets_enabled": sorted(set(status.get("assets") or []) | {"USDC"}),
                                   "passphrase": passphrase}, scope=client.OPERATOR)
        time.sleep(1)
    import socket
    import subprocess
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    # The throwaway seller is a stdlib HTTP server: it needs a PATH and a HOME, not the agent's keys.
    seller = subprocess.Popen([sys.executable, str(script), str(port)],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                              env=daemon.child_env())
    try:
        banner = seller.stdout.readline().strip() if seller.stdout else ""
        print(f"   seller    {banner or f'listening on {port}'}")
        quoted = None
        for _attempt in range(15):
            try:
                quoted = client.call("pay_url", {"url": f"http://127.0.0.1:{port}/sku/42",
                                                 "allow_loopback": True, "_scope": client.OPERATOR,
                                                 "why": "demo: x402 purchase"},
                                     scope=client.OPERATOR)
                break
            except client.WalletUnavailable as exc:
                if "unreachable" not in str(exc) or _attempt == 14:
                    raise
                time.sleep(0.2)
        quote = quoted.get("quote", {})
        request = quoted.get("request", {})
        print(f"   quote     {quote.get('amount')} {quote.get('asset')} on {quote.get('kind')} "
              f"→ {str(quote.get('payee'))[:14]}…")
        print(f"   filed     request {request.get('request_id')} status={request.get('status')}")
        print("   next      approve it in the Wallet pane; with no USDC funded it will refuse at the "
              "chain, not before the quote")
        return 0
    except client.WalletUnavailable as exc:
        print(f"   quote     refused: {exc}")
        return 1
    finally:
        seller.terminate()
        try:
            seller.wait(timeout=5)
        except Exception:  # noqa: BLE001 - a wedged demo seller must not block the CLI
            seller.kill()


def cmd_request(args: argparse.Namespace) -> int:
    """File a payment yourself. The agent has a tool for this; a human at a terminal needs a verb.

    `balances` and `coin` print `hermes wallet request 0.001 XMR <payee>` as the next step, and for a
    while that command did not exist — following the tool's own advice gave "'request' is not a
    hermes wallet command". This is that command.
    """
    _ensure_daemon()
    params: Dict[str, Any] = {"kind": args.kind or "", "asset": args.asset,
                              "amount": args.amount, "payee": args.payee}
    if args.why:
        params["why"] = args.why
    if args.memo:
        params["memo"] = args.memo
    if not params["kind"]:
        params["kind"] = client.call("status").get("default_chain", {}).get(args.asset.upper(), "")
    result = client.call("request_tx", params, scope=client.OPERATOR)
    if args.as_json:
        _out(result, True)
        return 0
    decision = result.get("decision") or {}
    request = result.get("request") or {}
    print(f"  request   {request.get('request_id')}  {request.get('amount')} {request.get('asset')}"
          f"  {request.get('kind')}")
    print(f"  policy    allow={decision.get('allow')} rule={decision.get('rule')}"
          f" code={decision.get('code')}")
    if decision.get("allow") and result.get("approval_id"):
        print(f"  approve   hermes wallet approve {request.get('request_id')} "
              f"--amount {request.get('amount')} --yes")
    elif not decision.get("allow"):
        print(f"  refused   {decision.get('reason')}")
        reason = str(decision.get("reason", "")).lower()
        if "money" in reason or "balance" in reason or "fund" in reason:
            # "not enough money" with no balance and no way to fix it is a dead end, not an error
            try:
                held = (client.call("balances").get("balances") or [])
                row = next((r for r in held if str(r.get("asset", "")).upper()
                            == str(request.get("asset", "")).upper()), None)
                if row is not None:
                    print(f"  you hold  {row.get('balance') or '0'} {row.get('asset')}")
                print(f"  receive   hermes wallet receive --kind {request.get('kind')}   "
                      f"(incoming funds show up after they confirm)")
            except Exception:                            # noqa: BLE001 - the refusal still stands
                pass
        # A refusal is not a success: scripts and agents read the exit code, and this one used to say
        # 0 for over-cap, bad-payee and locked alike, so nothing was filed and nothing noticed.
        return 2
    return 0


def cmd_nodes(args: argparse.Namespace) -> int:
    _ensure_daemon()
    payload = client.call("nodes")
    if args.as_json:
        _out(payload, True)
        return 0
    rows = payload.get("nodes") or payload.get("chains") or []
    if not rows:
        _out(payload, True)      # unknown shape from an older daemon: show it rather than lie
        return 0
    for row in rows:
        kind = CHAIN_LABEL.get(row.get("kind", ""), row.get("kind", ""))
        height = row.get("height")
        healthy = "ok" if row.get("available") or height else "FAIL"
        print(f"  {kind:<16} {healthy:<5} height {str(height or '-'):>10}  "
              f"{row.get('provider') or row.get('url') or ''}")
        if row.get("error"):
            print(f"       {str(row['error'])[:100]}")
    return 0


def cmd_chains(args: argparse.Namespace) -> int:
    _ensure_daemon()
    payload = client.call("chain_status")
    if args.as_json:
        _out(payload, True)
        return 0
    for row in payload.get("chains") or []:
        kind = CHAIN_LABEL.get(row.get("kind", ""), row.get("kind", ""))
        state = ("ok" if row.get("available")
                 else row.get("state") or "offline")
        print(f"  {kind:<16} {state:<12} height {str(row.get('height') or '-'):>10}  "
              f"{row.get('provider') or ''}")
        if row.get("error"):
            print(f"       {str(row['error'])[:100]}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Lock state, rules, and a per-chain balance table — the one screen an operator opens first.

    `status` used to print the rules and nothing about where the money is, so two commands were
    needed to answer one question, and `balances` buried Base's ETH and USDC under a per-coin
    layout. The table comes from the same `balances` call the pane makes, so the two never disagree.
    """
    _ensure_daemon()
    wallet = client.call("status")
    if args.as_json:
        _out({"status": wallet}, True)
        return 0
    accounts: List[Dict[str, Any]] = []
    try:
        accounts = client.call("balances").get("accounts") or []
        for row in accounts:                # the table orders coins the way the chain knows them
            row["chain_assets"] = (wallet.get("chain_assets") or {}).get(row.get("kind")) or [row.get("asset")]
    except client.WalletUnavailable as exc:
        # status is the command you run when something is wrong: report the rules, say the table is
        # missing and why, and do not fail the whole screen over it.
        accounts = [{"kind": "wallet", "asset": "?", "balance": None, "available": False,
                     "state": "error", "error": str(exc)}]
    print(_status_block({"status": wallet}))
    print()
    print(_balance_table(accounts))
    return 0


def cmd_up(args: argparse.Namespace) -> int:
    _out(daemon.start(), args.as_json)
    return 0


def cmd_down(args: argparse.Namespace) -> int:
    result = daemon.stop()
    _out(result, args.as_json)
    return 0 if result.get("ok", True) else 2


def cmd_receive(args: argparse.Namespace) -> int:
    """A fresh address for one incoming payment."""
    _ensure_daemon()
    params: Dict[str, Any] = {"label": args.label or ""}
    if getattr(args, "expected", ""):
        params["expected"] = args.expected
    if getattr(args, "kind", ""):
        params["kind"] = args.kind
    result = client.call("receive_address", params)
    if args.as_json:
        _out(result, True)
        return 0
    print(f"  {result['kind']}  account {result['account_index']}  {result.get('label', '')}")
    print(f"  address  {result['address']}")
    if result.get("expected"):
        print(f"  expect   {result['expected']} (the incoming list tracks this order as paid/partial)")
    if result.get("reused"):
        print("  note     this chain has no subaddresses, so the wallet address is reused")
    return 0


def _incoming_table(rows: List[Dict[str, Any]], note: str = "") -> str:
    if not rows:
        return "  no incoming orders" + (f"\n  note: {note}" if note else "")
    lines: List[str] = []
    for row in rows:
        expected = str(row.get("expected") or "")
        chain = CHAIN_LABEL.get(row.get("kind", ""), row.get("kind", ""))
        # An order opened without a target amount has no denominator. Printing "/ -" made it look
        # like a broken order rather than one that was never given a price.
        amount = (f"{row.get('received', '0'):>14} / {expected:<14}" if expected
                  else f"{row.get('received', '0'):>14}{'':<18}")
        # the order id is how you close one (`wallet_close_order`); without it five "order 42" rows
        # are indistinguishable in the only view a human reads
        order_id = str(row.get("order_id") or "")
        lines.append(f"  {order_id:<14} {(row.get('label') or '(no label)')[:20]:<20} "
                     f"{row.get('status', ''):<8} {amount} {row.get('asset', ''):<4} "
                     f"acct {row.get('account_index', 0):<3} {chain}")
        if row.get("address"):
            lines.append(f"       {row['address']}")
        if row.get("last_tx"):
            lines.append(f"       tx {_short(row['last_tx'], 16, 8)}")
    if note:
        lines.append(f"  note: {note}")
    return "\n".join(lines)


def cmd_incoming(args: argparse.Namespace) -> int:
    """Orders opened by receive_address(), with what has actually arrived."""
    _ensure_daemon()
    result = client.call("incoming")
    if args.as_json:
        _out(result, True)
        return 0
    print(_incoming_table(result.get("orders") or [], str(result.get("note") or "")))
    return 0


def _spend_table(payload: Dict[str, Any]) -> str:
    assets = payload.get("totals") or {}
    by_asset = payload.get("assets") or {}
    payees = payload.get("payees") or []
    if not assets:
        return "  no approved payments in this window"
    lines = ["  by asset"]
    for asset, amount in sorted(assets.items()):
        count = (by_asset.get(asset) or {}).get("count")
        suffix = f"   ({count} payment(s))" if count is not None else ""
        lines.append(f"  {asset:<5} {amount:>20}{suffix}")
    if payees:
        lines.append("")
        lines.append("  by payee")
        for row in payees:
            lines.append(f"  {row.get('payee', ''):<24} {row.get('spent', '0'):>16} "
                         f"{row.get('asset', ''):<4} {row.get('count', 0)} payment(s)")
    # The per-asset number is the one policy enforces against the daily cap, so it gets an explicit
    # total at the bottom: a long payee list used to bury it, and "0.035 XMR" was left to be
    # summed by eye.
    lines.append("")
    split_known = "real_totals" in payload and "simulated_totals" in payload
    real_totals = payload.get("real_totals") or {}
    sim_totals = payload.get("simulated_totals") or {}
    for asset in sorted(assets):
        real = real_totals.get(asset, "0")
        simulated = sim_totals.get(asset, "0")
        if not split_known:
            # old daemon: no split available, say so rather than implying it is all real money
            lines.append(f"  total spent in window: {_amount_text(assets[asset])} {asset}")
            continue
        line = f"  total spent in window: {_amount_text(real)} {asset}"
        if _amount_text(simulated) not in ("0", "0.0"):
            line += f"  (+{_amount_text(simulated)} {asset} simulated, not real money)"
        lines.append(line)
    window = f"  window: {payload.get('since') or 'the beginning'} -> {payload.get('until') or 'now'}"
    return "\n".join([window, ""] + lines)


def cmd_spend(args: argparse.Namespace) -> int:
    """Spend totals per asset and per payee over a window (`--since 30d`)."""
    _ensure_daemon()
    result = client.call("spend_totals", {"since": args.since or "", "until": args.until or ""})
    if args.as_json:
        _out(result, True)
        return 0
    print(_spend_table(result))
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    """Withdraw a pending request before anyone acts on it."""
    _ensure_daemon()
    result = client.call("cancel_request", {"request_id": args.request_id, "reason": args.reason or ""})
    request = result.get("request", {})
    if args.as_json:
        _out(result, True)
        return 0
    print(f"  cancelled {request.get('request_id', args.request_id)}  "
          f"({request.get('amount', '?')} {request.get('asset', '?')} to "
          f"{_short(request.get('payee', ''), 16, 6)})")
    return 0


def cmd_proof(args: argparse.Namespace) -> int:
    """Verify a payment from the payer's tx key."""
    _ensure_daemon()
    params = {"txid": args.txid, "tx_key": args.tx_key, "address": args.address}
    if getattr(args, "kind", ""):
        params["kind"] = args.kind
    result = client.call("verify_payment", params)
    if args.as_json:
        _out(result, True)
        return 0 if result.get("received") else 1
    print(f"  received   {result['received']}")
    print(f"  amount     {result['amount']} XMR")
    print(f"  confirmations {result['confirmations']}{' (in pool)' if result.get('in_pool') else ''}")
    return 0 if result.get("received") else 1


def cmd_pay_url(args: argparse.Namespace) -> int:
    """Price a purchase off the site's own 402 demand and open a request the human approves."""
    _ensure_daemon()
    # --allow-loopback exists for the bundled demo seller only: the daemon honours it for operator
    # scope and for 127.0.0.1, so the agent's tool can never reach a private address.
    params: Dict[str, Any] = {"url": args.url, "why": args.why or ""}
    if getattr(args, "amount", None):
        # an invoice page that states an address but no amount needs one from the caller; the daemon's
        # own error message says "pass the amount" and the command had no way to do it
        params["amount"] = args.amount
    allow_loopback = bool(getattr(args, "allow_loopback", False))
    if allow_loopback:
        # the daemon only honours this at OPERATOR scope, and this call used the agent token - so the
        # demo's money shot failed with "not a public address". The CLI is the operator here.
        params["allow_loopback"] = True
    try:
        result = client.call("pay_url", params,
                             scope=client.OPERATOR if allow_loopback else client.AGENT)
    except client.WalletUnavailable as exc:
        # a refused quote is information, not a crash: "127.0.0.1 is not a public address" is the
        # answer here, and a traceback hides it
        return _fail(str(exc))
    quote = result.get("quote", {})
    if args.as_json:
        _out(result, True)
        return 0
    print(f"  quoted   {quote.get('amount')} {quote.get('asset')} on {quote.get('kind')}")
    print(f"  pay to   {quote.get('payee')}")
    print(f"  for      {quote.get('description') or quote.get('resource')}")
    request = result.get("request", {})
    decision = result.get("decision", {})
    if decision.get("allow") is False:
        print(f"  refused  {decision.get('code')}: {decision.get('reason')}")
        return 1
    print(f"  request  {request.get('request_id')}  (pending a human decision)")
    print(f"  approve  hermes wallet approve {request.get('request_id')}")
    return 0


def cmd_requests(args: argparse.Namespace) -> int:
    _ensure_daemon()
    rows = client.call("requests_list").get("requests", [])
    if args.as_json:
        _out(rows, True)
        return 0
    if not rows:
        print("no requests")
        return 0
    for row in rows:
        print(f"{row['request_id']:<20} {row['status']:<9} {row['amount']:>12} {row['asset']:<5} "
              f"{row['kind']:<18} {row['payee'][:16]}…  approval={row.get('approval_id') or '-'}")
    return 0


def cmd_unlock(args: argparse.Namespace) -> int:
    """Unlock with either secret. The PIN is the everyday one; the passphrase is the way in when there
    is no PIN yet, or when the PIN is locked out."""
    _ensure_daemon()
    # Only read a PIN when the caller said so: `_read_secret(False, …)` refuses without a TTY, which
    # would break every scripted `unlock --passphrase-stdin` in the fleet.
    pin = _read_secret(True, "PIN (six digits): ") if getattr(args, "pin_stdin", False) else ""
    if pin:
        # The format check belongs to the daemon (`bad_pin_format`). A six-digit value is not the
        # only thing this can be asked to open — nothing short is rejected here any more.
        result = client.call("unlock", {"passphrase": pin}, scope=client.OPERATOR)
        _out(result, args.as_json)
        return 0
    _out(client.call("unlock", {"passphrase": _passphrase(args)}, scope=client.OPERATOR), args.as_json)
    return 0


def cmd_lock(args: argparse.Namespace) -> int:
    _out(client.call("lock", {}, scope=client.OPERATOR), args.as_json)
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    """Approve one request, from a terminal, with the human watching."""
    _ensure_daemon()
    # Line order is PIN first, then the passphrase — the same as `pin set`. Two commands reading the
    # same pipe in different orders fed the passphrase into the PIN field, which is a lockout.
    pin = _read_secret(getattr(args, "pin_stdin", False), "PIN (six digits): ")
    if not pin:
        return _fail("no PIN supplied (--pin-stdin reads the first line of stdin)")
    status = client.call("status")
    wallet = status.get("wallet") or status
    pin_gate = (wallet.get("pin") or {}).get("required", True)
    passphrase = ""
    if wallet.get("locked"):
        # A locked wallet needs the long secret before anything can be read.
        if getattr(args, "pin_stdin", False) and not getattr(args, "passphrase_stdin", False):
            return _fail("this wallet is locked: add --passphrase-stdin and pipe the PIN on line 1, "
                         "the passphrase on line 2 (or run it on a TTY)")
        passphrase = _passphrase(args)
        client.call("unlock", {"passphrase": passphrase}, scope=client.OPERATOR)
    elif pin_gate is False:
        # With the per-payment gate off, the daemon authorises an approval with the passphrase — and
        # the CLI only sent one when the wallet was locked, so every approve died with
        # `passphrase_required` on a wallet that had the gate turned off on purpose.
        passphrase = _passphrase(args)
    rows = client.call("requests_list").get("requests", [])
    match: Optional[Dict[str, Any]] = None
    for row in rows:
        if args.request_id in (row.get("request_id"), row.get("approval_id")):
            match = row
            break
    if match is None:
        return _fail(f"no pending request matching {args.request_id!r}")
    if match["status"] != "pending" or not match.get("approval_id"):
        return _fail(f"request {match['request_id']} is {match['status']} and cannot be approved")

    print(f"  request   {match['request_id']}")
    print(f"  amount    {match['amount']} {match['asset']}")
    print(f"  chain     {match['kind']}")
    print(f"  payee     {match['payee']}")
    if match.get("memo"):
        print(f"  memo      {match['memo']}")
    if match.get("why"):
        print(f"  why       {match['why']}")

    if not getattr(args, "yes", False) and not _confirm("Approve this payment?"):
        return _fail("not approved (no confirmation) — no terminal to ask on: "
                     "add --yes to confirm non-interactively")

    # The daemon decides whether this amount must be typed (per asset, and never above that coin's
    # per-tx cap). Re-deriving it here from one global `approval_threshold` made the guard
    # unreachable for BTC and noise for XRP — and the two could disagree.
    confirm_amount = args.amount
    if match.get("confirm_amount_required") and not confirm_amount:
        if not sys.stdin.isatty():
            return _fail("this amount needs --amount <value> as confirmation")
        confirm_amount = input(f"Type {match['amount']} to confirm: ").strip()

    # The card, exactly as printed above: the daemon compares these against the request, so a
    # request that changed after the numbers were shown cannot be signed by this PIN.
    params = {"request_id": match["request_id"], "approval_id": match["approval_id"],
              "pin": pin, "expect_amount": match["amount"],
              "expect_payee": match["payee"]}
    if passphrase:
        params["passphrase"] = passphrase
    if confirm_amount:
        params["confirm_amount"] = confirm_amount
    result = client.call("approve", params, scope=client.OPERATOR)
    if args.as_json:
        _out(result, True)
    else:
        sent = result.get("request") or {}
        execution = {k: result.get(k) for k in ("tx_hash", "fee", "simulated") if k in result}
        print(f"  sent      {sent.get('amount')} {sent.get('asset')} to {sent.get('payee')}")
        if execution.get("fee"):
            print(f"  fee       {execution['fee']} {sent.get('asset')}")
        if execution.get("tx_hash"):
            print(f"  tx        {execution['tx_hash']}")
        if execution.get("simulated"):
            print("  note      simulated chain — no real funds moved")
        print(f"  receipt   written; `hermes wallet ledger` shows it")
    if result.get("x402_delivered") is False:
        # the money moved but the resource was never delivered: say so loudly and fail the command
        print(f"  delivery  FAILED — {result.get('x402_error') or 'the seller did not accept it'}",
              file=sys.stderr)
        print("  the transfer is already on chain; the receipt records the failed delivery",
              file=sys.stderr)
        return 1
    return 0


def cmd_deny(args: argparse.Namespace) -> int:
    _ensure_daemon()
    _unlock_if_needed(args)
    rows = client.call("requests_list").get("requests", [])
    match = next((r for r in rows if args.request_id in (r.get("request_id"), r.get("approval_id"))), None)
    if match is None or not match.get("approval_id"):
        return _fail(f"no pending request matching {args.request_id!r}")
    _out(client.call("deny", {"request_id": match["request_id"], "approval_id": match["approval_id"],
                              "reason": args.reason}, scope=client.OPERATOR), args.as_json)
    return 0


def cmd_mandates(args: argparse.Namespace) -> int:
    """Autopays end to end: create, edit, pause, resume, delete, list."""
    _ensure_daemon()
    action = args.action or "list"
    if action == "list":
        _out(client.call("mandates_list"), args.as_json)
        return 0

    if action == "create":
        if not (args.payee and args.per_tx and args.period):
            return _fail("create needs --payee, --per-tx and --period")
        _unlock_if_needed(args)
        payload: Dict[str, Any] = {
            "kind": args.kind or _first_monero_chain(),
            "asset": args.asset,
            "payee": args.payee,
            "per_tx_cap": args.per_tx,
            "period_cap": args.period,
            "absolute_cap": args.absolute or args.period,
            "interval": args.interval or "24h",
            "label": args.label or "",
            "activate": not args.no_activate,
            "passphrase": _passphrase(args),
        }
        if args.expires_at:
            payload["expires_at"] = args.expires_at
        _out(client.call("mandates_create", payload, scope=client.OPERATOR), args.as_json)
        return 0

    if not args.mandate_id:
        return _fail("--mandate-id is required")

    if action in ("pause", "resume", "activate"):
        _unlock_if_needed(args)
        payload = {"mandate_id": args.mandate_id, "enabled": action != "pause"}
        if action != "pause":
            payload["passphrase"] = _passphrase(args)
        _out(client.call("mandates_set_enabled", payload, scope=client.OPERATOR), args.as_json)
        return 0

    if action == "unattended":
        _unlock_if_needed(args)
        payload = {"mandate_id": args.mandate_id, "unattended": not args.off}
        if not args.off:
            # the one setting that removes the PIN in front of every payment: passphrase + PIN
            payload["passphrase"] = _passphrase(args)
            if args.pin_stdin:
                payload["pin"] = sys.stdin.readline().strip()
            else:
                payload["pin"] = getpass.getpass("PIN (six digits): ").strip()
        _out(client.call("mandates_set_unattended", payload, scope=client.OPERATOR), args.as_json)
        return 0

    if action == "revoke":
        _out(client.call("mandates_revoke", {"mandate_id": args.mandate_id,
                                             "reason": "revoked from CLI"}), args.as_json)
        return 0

    if action == "edit":
        _unlock_if_needed(args)
        payload = {"mandate_id": args.mandate_id, "passphrase": _passphrase(args)}
        for key, value in (("per_tx_cap", args.per_tx), ("period_cap", args.period),
                           ("absolute_cap", args.absolute), ("interval", args.interval),
                           ("expires_at", args.expires_at), ("label", args.label)):
            if value:
                payload[key] = value
        if len(payload) <= 2:
            return _fail("edit needs at least one of --per-tx/--period/--absolute/--interval/"
                         "--expires-at/--label")
        _out(client.call("mandates_update", payload, scope=client.OPERATOR), args.as_json)
        return 0

    if action == "delete":
        _unlock_if_needed(args)
        _out(client.call("mandates_delete", {"mandate_id": args.mandate_id,
                                             "passphrase": _passphrase(args)},
                         scope=client.OPERATOR), args.as_json)
        return 0

    return _fail(f"unknown action {action}")


def _first_monero_chain() -> str:
    try:
        chains = client.call("status").get("chains") or []
    except Exception:  # noqa: BLE001
        chains = []
    for kind in chains:
        if str(kind).startswith("monero"):
            return str(kind)
    return "monero-mainnet"


def cmd_autolock(args: argparse.Namespace) -> int:
    """How long the wallet stays unlocked without you: `5`, `30`, or `off`."""
    _ensure_daemon()
    current = client.call("status")
    if not args.value:
        mins = current.get("autolock_minutes")
        left = current.get("unlock_expires_in")
        print(f"  auto-lock   {'off — stays unlocked' if not mins else str(mins) + ' min'}")
        if left is not None:
            print(f"  locks in    {left // 60}m {left % 60}s")
        return 0
    raw = str(args.value).strip().lower()
    minutes = 0 if raw in ("off", "never", "0") else int(raw)
    _unlock_if_needed(args)
    result = client.call("config_set", {"autolock_minutes": minutes,
                                        "passphrase": _passphrase(args)}, scope=client.OPERATOR)
    print(f"  auto-lock set to {'off' if minutes == 0 else str(minutes) + ' min'}"
          f" ({result['settings']['autolock_minutes']})")
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    """Archive the receipt chain so the view starts clean; the old chain stays on disk."""
    _ensure_daemon()
    if args.action == "verify":
        _out(client.call("ledger_verify"), args.as_json)
        return 0
    _unlock_if_needed(args)
    _out(client.call("ledger_archive", {}, scope=client.OPERATOR), args.as_json)
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    _ensure_daemon()
    result = client.call("ledger_verify")
    _out(result, args.as_json)
    return 0 if result.get("ok") else 1


def cmd_ledger(args: argparse.Namespace) -> int:
    _ensure_daemon()
    if getattr(args, "csv", False):
        result = client.call("ledger_csv", {"since": getattr(args, "since", "") or "",
                                            "until": getattr(args, "until", "") or ""})
        # raw CSV, not JSON-wrapped: `hermes wallet ledger --csv > receipts.csv` is the point
        sys.stdout.write(result.get("csv", ""))
        return 0
    _out(client.call("ledger_tail", {"limit": args.limit}), args.as_json)
    return 0


def cmd_service(args: argparse.Namespace) -> int:
    if args.action == "env":
        pairs = {}
        for item in args.set or []:
            if "=" not in item:
                return _fail(f"--set expects KEY=VALUE, got {item!r}")
            key, value = item.split("=", 1)
            pairs[key] = value
        if not pairs:
            return _fail("nothing to write (use --set KEY=VALUE ...)")
        path = service.write_env(pairs)
        _out({"ok": True, "written": str(path), "keys": sorted(pairs)}, args.as_json)
        return 0
    if args.action == "install":
        result = service.install()
        _out(result, args.as_json)
        # "no user service manager here" is a refusal, and it exited 0 so a script could not tell.
        return 0 if result.get("ok") else 2
    if args.action == "uninstall":
        _out(service.uninstall(), args.as_json)
        return 0
    _out(service.status(), args.as_json)
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    # This command talks to the daemon directly rather than through `_ensure_daemon`, so a fresh
    # home (no keystore, no socket) used to raise out of it; the boundary turned that into a
    # sentence, but the sentence is better here, where it can name the fix.
    _ensure_daemon()
    if args.action == "show":
        _out(client.call("config_get"), args.as_json)
        return 0
    if not args.json_payload:
        return _fail("config set needs --json '<object>'")
    try:
        payload = json.loads(args.json_payload)
    except json.JSONDecodeError as exc:
        return _fail(f"--json is not valid JSON: {exc}")
    _unlock_if_needed(args)
    # Policy changes are authorised by the passphrase, not the operator token
    if "passphrase" not in payload:
        payload["passphrase"] = _passphrase(args)
    _out(client.call("config_set", payload, scope=client.OPERATOR), args.as_json)
    return 0


def _gas_num(value: str) -> str:
    """A gas figure short enough for a table but never rounded to a lie ('0' stays '0')."""
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return str(value)[:18]
    if amount == 0:
        return "0"
    text = f"{amount:.9f}".rstrip("0").rstrip(".")
    if len(text) > 18:
        text = f"{amount:.6f}"
    return text


def cmd_swap(args: argparse.Namespace) -> int:
    """Convert one coin into another through a trusted operator, or just look at the price.

    A swap is a payment to a contract: the router is pinned in code, the minimum out comes from the
    operator's slippage ceiling, and the PIN is what converts. `swap` on its own quotes.
    """
    _ensure_daemon()
    what = str(getattr(args, "what", "") or "quote")
    if what == "routers":
        rows = client.call("swap_routers", {"kind": args.kind}).get("operators") or []
        if args.as_json:
            _out({"operators": rows}, True)
            return 0
        if not rows:
            print("no trusted swap operator on this chain")
            return 0
        for row in rows:
            print(f"{row.get('operator',''):<16}{row.get('label',''):<16}{row.get('router','')}"
                  f"   fee {row.get('fee_bps', 0)} bps")
        return 0
    if what not in ("quote", "do"):
        return _fail("usage: hermes wallet swap [quote|do|routers] <amount> <from> <to>")
    if args.amount is None or not args.coin_from or not args.coin_to:
        return _fail("usage: hermes wallet swap [quote|do] <amount> <from> <to> [--operator NAME]")
    payload = {"kind": args.kind, "asset": args.coin_from, "asset_out": args.coin_to,
               "amount": args.amount, "operator": args.operator or ""}
    if what == "quote":
        quote = client.call("swap_quote", payload)
        if args.as_json:
            _out(quote, True)
            return 0
        print(f"{payload['amount']} {quote['asset_in']} → {quote['amount_out']} {quote['asset_out']}")
        print(f"  route      {quote['label']} ({quote['router']})")
        print(f"  minimum    {quote['min_out']} {quote['asset_out']}"
              f"  ({quote['slippage_bps']} bps of slippage allowed)")
        print(f"  operators  {', '.join(quote.get('operators') or [])}")
        print("  nothing was filed: add `do` to ask a human to approve it")
        return 0
    payload.update({"why": args.why or "", "memo": args.memo or ""})
    # The daemon requires the passphrase (or the PIN) to file a conversion: it is a payment to a
    # contract. Without this line every `swap ... do` failed client-side for want of a secret.
    payload["passphrase"] = _passphrase(args)
    filed = client.call("swap_request", payload, scope=client.OPERATOR)
    if args.as_json:
        _out(filed, True)
        return 0
    req = filed.get("request") or {}
    swap = req.get("swap") or {}
    print(f"filed {req.get('request_id')} — {req.get('amount')} {req.get('asset')} → "
          f"{swap.get('quoted_out')} {swap.get('asset_out')} (min {swap.get('min_out')})")
    print(f"  route   {swap.get('label')} · fee {filed.get('fee')} ETH")
    print(f"  approve with the PIN in the pane: approval {filed.get('approval_id')} "
          f"expires {filed.get('approval_expires_at')}")
    return 0


def cmd_gas(args: argparse.Namespace) -> int:
    """The gas coin on every chain that charges one: what is there, what must stay, how to refill.

    Monero and Bitcoin pay the fee in the coin being sent, so they have no separate gas balance and
    do not appear here. A 402 purchase is submitted by the seller, so it costs none either.
    """
    _ensure_daemon()
    rest = list(getattr(args, "rest", []) or [])
    payload = client.call("gas", scope=client.OPERATOR)
    rows = payload.get("chains") or []
    if rest and rest[0] == "reserve":
        if len(rest) != 3:
            return _fail("usage: hermes wallet gas reserve <chain> <address>")
        chain, address = rest[1], rest[2]
        row = next((r for r in rows if r.get("kind") == chain and "native" in r), None)
        if row is None:
            known = ", ".join(r.get("kind", "") for r in rows if "native" in r) or "none"
            return _fail(f"{chain} does not charge gas in a separate coin (gas chains: {known})")
        _unlock_if_needed(args)
        current = (client.call("config_get") or {}).get("settings") or {}
        reserves = dict(current.get("gas_reserve") or {})
        reserves[row["native"]] = address
        client.call("config_set", {"gas_reserve": reserves, "passphrase": _passphrase(args)},
                    scope=client.OPERATOR)
        print(f"refill address for {row['native']} set to {address}")
        return 0
    if args.as_json:
        _out(payload, True)
        return 0
    if payload.get("locked"):
        return _fail("unlock first: gas is read from the chain at the wallet's address")
    if not rows:
        print("no chain here charges gas in a separate coin (Monero and Bitcoin pay the fee in the "
              "coin being sent; a 402 purchase is paid by the seller)")
        return 0
    out = [f"{'chain':<16}{'gas':<5}{'held':>18}{'floor':>18}{'left':>6}  state"]
    for row in rows:
        if "error" in row:
            out.append(f"{row.get('kind', '?'):<16}{'—':<5}{'unreadable':>18}  {row['error'][:38]}")
            continue
        out.append(f"{row['kind']:<16}{row['native']:<5}{_gas_num(row['held']):>18}"
                   f"{_gas_num(row['floor']):>18}{row['transfers_left']:>6}  {row['state']}")
    print("\n".join(out))
    low = payload.get("low") or []
    if low:
        print()
        for row in low:
            print(f"low on {row['kind']}: floor is {row['floor'][:12]} {row['native']} "
                  f"({row['transfers_floor']} transfers), held {row['held'][:12]}")
            print(f"  send at least {row['shortfall'][:12]} {row['native']} to {row['address']}")
            if row.get("reserve"):
                print(f"  refill address on file: {row['reserve']}")
    return 0


def cmd_balances(args: argparse.Namespace) -> int:
    """Every coin with its balance and receive address. The CLI's answer to 'where is my money'."""
    _ensure_daemon()
    coin = str(getattr(args, "coin", "") or "").strip()
    if coin:
        args.asset = coin
        return cmd_coin(args)
    payload = client.call("balances", {"fresh": bool(getattr(args, "fresh", False))})
    if args.as_json:
        _out(payload, True)
        return 0
    try:
        # ETH and USDC live on Base (and USDC on Polygon too). Reading only the chains a fresh
        # wallet enables made the command answer a different question than it asks.
        assets = payload.get("asset_chains") or client.call("status").get("asset_chains") or {}
    except client.WalletUnavailable:
        assets = payload.get("asset_chains") or {}
    accounts = payload.get("accounts") or []
    for row in accounts:
        row["chain_assets"] = (assets.get(row.get("asset")) or [row.get("kind")])
    print(_balance_table(accounts))
    return 0


def cmd_coin(args: argparse.Namespace) -> int:
    """One coin, spelled out: the lettering, the balance, the address and the rules that apply.

    The multi-coin table answers "where is my money"; this answers "what is this coin, right now" —
    the same facts with room to read them. `--json` and a pipe get the plain payload, because art in
    a script's output is noise.
    """
    _ensure_daemon()
    asset = str(args.asset).upper()
    payload = client.call("balances", {"fresh": bool(getattr(args, "fresh", False))})
    accounts = payload.get("accounts") or []
    rows = [row for row in accounts if str(row.get("asset", "")).upper() == asset]
    status = client.call("status")
    wallet = status.get("status") or status or {}
    caps = wallet.get("caps") or {}
    if not rows:
        held = ", ".join(sorted({str(row.get("asset", "")).upper() for row in accounts})) or "nothing"
        return _fail(f"this wallet holds no {asset} (it holds {held})")
    if args.as_json:
        _out({"asset": asset, "accounts": rows,
              "caps": {k: (v or {}).get(asset) for k, v in caps.items()}}, True)
        return 0

    out: List[str] = []
    rendered = None if getattr(args, "no_art", False) else art.render_art(asset)
    if rendered:
        out.extend([rendered, ""])
    else:
        out.extend([f"  {art.emblem(asset)} {art.FULL_NAMES.get(asset, asset)} ({asset})", ""])
    for row in rows:
        simulated = str(row.get("kind", "")).startswith("sim")
        name = "SIMULATOR" if simulated else art.FULL_NAMES.get(asset, asset)
        amount = row.get("balance")
        state = ("live" if row.get("available")
                 else "locked" if row.get("state") == "locked" else "offline")
        shown = "\u2014" if amount is None else f"{amount} {asset}"
        out.append(f"  {name:<12} {shown:>26}   {state}")
        detail = " \u00b7 ".join(x for x in (CHAIN_LABEL.get(row.get("kind", ""), row.get("kind", "")),
                                             row.get("provider", "")) if x)
        if detail:
            out.append(f"  {'':<12} {detail}")
        if row.get("address"):
            out.append(f"  address      {row['address']}")
        elif row.get("error"):
            out.append(f"  error        {str(row['error'])[:100]}")
    limits = " \u00b7 ".join(x for x in (
        f"{caps.get('per_tx', {}).get(asset)} per tx" if (caps.get("per_tx") or {}).get(asset) else "",
        f"{caps.get('daily', {}).get(asset)} per day" if (caps.get("daily") or {}).get(asset) else "",
        f"fee cap {caps.get('fees', {}).get(asset)}" if (caps.get("fees") or {}).get(asset) else "") if x)
    if limits:
        out.append(f"  limits       {limits}")
    if not wallet.get("human_gate"):
        out.append("  WARNING      the passphrase prompt is OFF: holding the operator token is enough")
    if asset == "XRP":
        out.append("  note         receive and hold only — this wallet does not sign XRP payments")
    out.append("  receive      hermes wallet receive --label \"order 42\"")
    out.append(f"  send         hermes wallet request 0.001 {asset} <payee>")
    print("\n".join(out))
    return 0


def cmd_coins(args: argparse.Namespace) -> int:
    """List, add or remove coins - the terminal version of the pane's coin menu.

    `assets_enabled` is the source of truth and the daemon derives `chains_enabled` from it, so this
    adds or removes a chain, an address and a balance row together.
    """
    _ensure_daemon()
    status = client.call("status")
    enabled = list(status.get("assets", []))
    available = list(status.get("available_assets", enabled))
    chains = status.get("asset_chains", {})
    if args.enable or args.disable:
        wanted = enabled + [a.upper() for a in (args.enable or [])]
        for asset in [a.upper() for a in (args.disable or [])]:
            wanted = [a for a in wanted if a != asset]
        wanted = [a for i, a in enumerate(wanted) if a not in wanted[:i]]
        unknown = [a for a in wanted if a not in available]
        if unknown:
            return _fail(f"unknown coin(s): {', '.join(unknown)} (known: {', '.join(available)})")
        if not wanted:
            return _fail("keep at least one coin enabled")
        _unlock_if_needed(args)
        payload: Dict[str, Any] = {"assets_enabled": wanted}
        payload["passphrase"] = _passphrase(args)
        out = client.call("config_set", payload, scope=client.OPERATOR)
        if getattr(args, "as_json", False):
            _out(out, True)
            return 0
        status = client.call("status")
        enabled = list(status.get("assets", []))
    if getattr(args, "as_json", False):
        _out({"assets": enabled, "available": available, "asset_chains": chains}, True)
        return 0
    print(f"  {'coin':<6}{'networks':<28}{'state'}")
    for asset in available:
        mark = "on " if asset in enabled else "off"
        nets = ", ".join(CHAIN_LABEL.get(c, c) for c in chains.get(asset, []))
        print(f"  {asset:<6}{nets:<28}{mark}")
    print("\n  enable/disable with: hermes wallet coins --enable ETH --disable XRP")
    return 0


def cmd_gate(args: argparse.Namespace) -> int:
    _ensure_daemon()
    _unlock_if_needed(args)
    enabled = args.action == "on"
    _out(client.call("set_human_gate", {"enabled": enabled, "passphrase": _passphrase(args),
                                        "confirm": True}, scope=client.OPERATOR), args.as_json)
    return 0


def _install_error_boundary(actions: argparse._SubParsersAction) -> None:
    """Turn a wallet refusal into one line and exit 2, never a traceback.

    The first command in a fresh home (`hermes wallet status` with no keystore) and a wrong
    passphrase both used to end in a stack trace; a custody tool's first impression cannot be that.
    `client` already raises `WalletUnavailable("code: message")` for every daemon refusal, so the
    message is the contract and the code sat in it all along.
    """
    for child in actions._name_parser_map.values():     # every command, hidden ones included
        func = child.get_default("func")
        if func is None or getattr(func, "_wallet_bound", False):
            continue
        child.set_defaults(func=_bound(func))


def _bound(func):
    @functools.wraps(func)
    def run(args: argparse.Namespace) -> int:
        # The global `--json` lands in `global_json` (a subparser's own flag would overwrite
        # `as_json`); fold it in and make sure the attribute exists — handlers read it directly, and
        # its absence used to crash three commands (spend, receive, incoming).
        if not getattr(args, "as_json", False) and getattr(args, "global_json", False):
            args.as_json = True
        if not hasattr(args, "as_json"):
            args.as_json = False
        try:
            return func(args)
        except client.WalletUnavailable as exc:
            text = str(exc)
            code, _, message = text.partition(": ")
            if not message:
                code, message = "daemon_unavailable", text
            print(f"error: {code}: {message}", file=sys.stderr)
            return 2
        except SystemExit:
            raise                       # argparse and the secret readers already print and exit
        except KeyboardInterrupt:
            print("\naborted", file=sys.stderr)
            return 130
        except BrokenPipeError:
            # `hermes wallet ledger | head` — the reader left. Not an error, and not a traceback:
            # redirect stdout to devnull so the interpreter's final flush cannot raise either.
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
            return 0
        except Exception as exc:        # noqa: BLE001 - a custody CLI must never show a traceback
            # Last line of defence. Every daemon refusal is a WalletUnavailable above; what reaches
            # here is a bug in this file, and the operator gets one sentence plus how to report it
            # instead of a stack trace and a half-drawn table. The type name is the part that makes
            # it diagnosable without leaking any key material.
            print(f"error: internal_error: {type(exc).__name__}: {exc}".rstrip(), file=sys.stderr)
            print("  this is a CLI bug; WALLET_DEBUG=1 reruns it with the traceback", file=sys.stderr)
            if os.environ.get("WALLET_DEBUG"):
                import traceback
                traceback.print_exc()
            return 2
    run._wallet_bound = True  # type: ignore[attr-defined]
    return run


def setup(sub: argparse.ArgumentParser) -> None:
    # Its own destination, not `as_json`: a subparser declares `--json` too, and argparse writes a
    # subparser default over the global value, so `hermes wallet --json status` printed the human
    # block for anything parsing it. `_bound` / `run` merge the two before a handler reads them.
    sub.add_argument("--json", action="store_true", dest="global_json", default=argparse.SUPPRESS,
                     help="raw JSON output")
    actions = sub.add_subparsers(dest="action", required=True)

    for name, func, help_text in (
        ("status", cmd_status, "lock state, chains, caps, pending count"),
        ("balances", cmd_balances, "every coin: balance and receive address (or just one)"),
        ("gas", cmd_gas, "gas on each chain: held, the floor it keeps, how to refill"),
        ("swap", cmd_swap, "convert a coin through a trusted operator: quote, or file one"),
        ("coin", cmd_coin, "one coin, spelled out: art, balance, address, caps"),
        ("coins", cmd_coins, "list coins; --enable/--disable to add or remove one"),
        ("chains", cmd_chains, "chain availability and height"),
        ("nodes", cmd_nodes, "public node health per chain"),
        ("up", cmd_up, "start the signer daemon"),
        ("down", cmd_down, "stop a daemon this process started"),
        ("unlock", cmd_unlock, "unlock with the passphrase (operator)"),
        ("lock", cmd_lock, "lock and drop the key from memory"),
        ("requests", cmd_requests, "list requests with their approval ids"),
        ("request", cmd_request, "file a payment yourself: amount, asset, payee"),
        ("pin", cmd_pin, "the six-digit secret typed before every payment: status, set"),
        ("passphrase", cmd_passphrase, "re-seal the keystore under a new passphrase (operator)"),
        ("approve", cmd_approve, "approve one request"),
        ("deny", cmd_deny, "deny one request"),
        ("verify", cmd_verify, "verify the receipt hash chain"),
        ("ledger", cmd_ledger, "print recent receipts"),
    ):
        parser = actions.add_parser(name, help=help_text)
        parser.set_defaults(func=func)
        if name == "swap":
            # `swap ... do` files a conversion, which the daemon authorises with the passphrase.
            parser.add_argument("--passphrase-stdin", action="store_true",
                                help="read the passphrase (or the 6-digit PIN) from stdin (scripts only)")
        if name in ("unlock", "approve", "deny", "gate"):
            parser.add_argument("--passphrase-stdin", action="store_true",
                                help="read the passphrase (or the 6-digit PIN) from stdin (scripts/tests only)")
        if name == "request":
            # `request` asks the daemon for a payment and needs no secret, but every other command
            # here accepts the flag: a script that pipes one secret to the whole flow must not die
            # on `unrecognized arguments` for the one verb that ignores it.
            parser.add_argument("--passphrase-stdin", action="store_true",
                                help="accepted for symmetry; `request` files a payment and needs no secret")
        if name == "unlock":
            parser.add_argument("--pin-stdin", action="store_true",
                                help="read the six-digit PIN from stdin instead of the passphrase")
        if name == "approve":
            parser.add_argument("request_id")
            parser.add_argument("--amount", help="typed confirmation for amounts above the threshold")
            parser.add_argument("--yes", action="store_true",
                                help="skip the interactive y/N prompt (the passphrase is still required)")
            parser.add_argument("--pin-stdin", action="store_true",
                                help="read the PIN from the first stdin line (then the passphrase, if "
                                     "the wallet is locked); the pane asks in a popup instead")
        if name == "deny":
            parser.add_argument("request_id")
            parser.add_argument("--reason", default="")
        if name == "passphrase":
            parser.add_argument("action", nargs="?", choices=["change"], default="change")
            parser.add_argument("--passphrase-stdin", action="store_true",
                                help="read three stdin lines: current, new, confirm")
        if name == "pin":
            parser.add_argument("action", nargs="?", choices=["status", "set"], default="status")
            parser.add_argument("--pin-stdin", action="store_true",
                                help="read the new PIN from stdin (scripts only)")
            # `pin set` calls `_passphrase`, which reads this flag; without it a headless set was
            # impossible ("refusing to read a passphrase without a TTY").
            parser.add_argument("--passphrase-stdin", action="store_true",
                                help="with --pin-stdin: the PIN is line 1, the passphrase line 2")
        if name == "request":
            parser.add_argument("amount", help="how much, e.g. 0.001")
            parser.add_argument("asset", help="XMR, BTC, XRP, USDC…")
            parser.add_argument("payee", help="address, or a saved contact name")
            parser.add_argument("--kind", default="", help="chain id (default: the coin's chain)")
            parser.add_argument("--why", default="", help="why this payment (recorded in the receipt)")
            parser.add_argument("--memo", default="", help="note for the operator")
        if name == "balances":
            parser.add_argument("coin", nargs="?", default="",
                                help="just this coin, with its lettering and rules")
            parser.add_argument("--fresh", action="store_true", help="skip the 60s balance cache")
        if name == "swap":
            parser.add_argument("what", nargs="?", default="quote",
                                choices=["quote", "do", "routers"],
                                help="quote (default), do (file it for the PIN), routers")
            parser.add_argument("amount", nargs="?", default=None)
            parser.add_argument("coin_from", nargs="?", default="")
            parser.add_argument("coin_to", nargs="?", default="")
            parser.add_argument("--operator", default="", help="which trusted operator to use")
            parser.add_argument("--kind", default="", help="chain id (default: the coin's chain)")
            parser.add_argument("--why", default="", help="why this conversion (in the receipt)")
            parser.add_argument("--memo", default="", help="note for the operator")
        if name == "gas":
            parser.add_argument("rest", nargs="*", default=[],
                                help="reserve <chain> <address> — where a human sends the refill")
            parser.add_argument("--passphrase-stdin", action="store_true")
        if name == "coin":
            parser.add_argument("asset", help="XMR, BTC, XRP, ETH, USDC, POL…")
            parser.add_argument("--fresh", action="store_true", help="skip the 60s balance cache")
            parser.add_argument("--no-art", action="store_true", dest="no_art",
                                help="plain output, no lettering")
        if name == "coins":
            parser.add_argument("--enable", action="append", metavar="COIN", help="add a coin (repeatable)")
            parser.add_argument("--disable", action="append", metavar="COIN", help="remove a coin (repeatable)")
            parser.add_argument("--passphrase-stdin", action="store_true")
            parser.add_argument("--json", action="store_true", dest="as_json", default=argparse.SUPPRESS,
                                help="raw JSON (also accepted before the subcommand)")
        if name == "ledger":
            parser.add_argument("--limit", type=int, default=20)
            parser.add_argument("--csv", action="store_true",
                                help="every receipt as CSV (seq,ts,kind,decision,rule,asset,amount,payee,tx_hash)")
            parser.add_argument("--since", default="", help="CSV window start: 30d, 12h or an ISO timestamp")
            parser.add_argument("--until", default="", help="CSV window end")

    recv = actions.add_parser("receive", help="a fresh address for one incoming payment")
    recv.set_defaults(func=cmd_receive)
    recv.add_argument("--kind", default="", help="chain to receive on (default: the first Monero chain)")
    recv.add_argument("--label", default="")
    recv.add_argument("--expected", default="", help="order total, so `incoming` can say paid/partial")
    recv.add_argument("--json", action="store_true", dest="as_json", default=argparse.SUPPRESS)

    _add_hidden_parser(actions, "incoming", cmd_incoming, aliases=["orders"])

    spend = actions.add_parser("spend", help="spend totals per asset and per payee")
    spend.set_defaults(func=cmd_spend)
    spend.add_argument("--since", default="", help="window start: 30d, 12h, 2w or an ISO timestamp")
    spend.add_argument("--until", default="", help="window end")
    spend.add_argument("--json", action="store_true", dest="as_json", default=argparse.SUPPRESS)

    cancel = actions.add_parser("cancel", help="withdraw a pending request before it is approved")
    cancel.set_defaults(func=cmd_cancel)
    cancel.add_argument("request_id")
    cancel.add_argument("--reason", default="")
    cancel.add_argument("--json", action="store_true", dest="as_json")

    proof = actions.add_parser("proof", help="verify a payment with the payer's tx key (Monero)")
    proof.set_defaults(func=cmd_proof)
    proof.add_argument("txid")
    proof.add_argument("--tx-key", dest="tx_key", default="",
                       help="Monero only; BTC/EVM need just the txid")
    proof.add_argument("--address", required=True)
    proof.add_argument("--kind", default="", help="chain id (defaults to the first Monero chain)")
    proof.add_argument("--json", action="store_true", dest="as_json")

    pay = actions.add_parser("pay-url", help="pay an HTTP 402 endpoint: the price comes from the server")
    pay.set_defaults(func=cmd_pay_url)
    pay.add_argument("url")
    pay.add_argument("--why", default="")
    pay.add_argument("--amount", default="",
                     help="an amount to pay when the page states an address but no price (e.g. 10$)")
    pay.add_argument("--json", action="store_true", dest="as_json")
    pay.add_argument("--passphrase-stdin", action="store_true",
                     help="read the passphrase from stdin (scripts; no TTY needed)")
    pay.add_argument("--allow-loopback", action="store_true", dest="allow_loopback",
                     help="demo only: allow a 402 quote from 127.0.0.1 (the bundled seller)")

    mandates = actions.add_parser("mandates", help="autopays: list, create, edit, pause, resume, delete")
    mandates.set_defaults(func=cmd_mandates)
    mandates.add_argument("action", nargs="?",
                          choices=["list", "create", "edit", "pause", "resume", "activate",
                                   "revoke", "delete", "unattended"], default="list")
    mandates.add_argument("--mandate-id", help="which autopay (for edit/pause/resume/delete/revoke)")
    mandates.add_argument("--kind", help="chain id (default: the enabled Monero chain)")
    mandates.add_argument("--asset", default="XMR")
    mandates.add_argument("--payee", help="address, or a saved contact name")
    mandates.add_argument("--per-tx", help="most this autopay may send in one payment")
    mandates.add_argument("--period", help="most it may send per interval")
    mandates.add_argument("--absolute", help="lifetime ceiling (defaults to --period)")
    mandates.add_argument("--interval", default="", help="24h, 6h, 7d, weekly … (default 24h)")
    mandates.add_argument("--expires-at", dest="expires_at", help="YYYY-MM-DDTHH:MM:SSZ")
    mandates.add_argument("--label", default="")
    mandates.add_argument("--no-activate", action="store_true", help="create it paused")
    mandates.add_argument("--off", action="store_true",
                          help="with action=unattended: turn the PIN waiver back off")
    mandates.add_argument("--pin-stdin", action="store_true",
                          help="with action=unattended: read the PIN as the next stdin line")
    mandates.add_argument("--passphrase-stdin", action="store_true")
    mandates.add_argument("--json", action="store_true", dest="as_json")

    cfg = actions.add_parser("config", help="show or change signed settings (operator)")
    cfg.set_defaults(func=cmd_config)
    cfg.add_argument("action", nargs="?", choices=["show", "set"], default="show")
    cfg.add_argument("--json", dest="json_payload", help="settings object, e.g. '{\"chains_enabled\":[\"monero-mainnet\"]}'")
    cfg.add_argument("--passphrase-stdin", action="store_true")

    bk = actions.add_parser("backup", help="write an encrypted backup of the keystore, settings and ledger")
    bk.set_defaults(func=cmd_backup)
    bk.add_argument("--out", help="output file (default: <wallet home>/wallet-backup-<ts>.json)")
    bk.add_argument("--passphrase-stdin", action="store_true")

    rs = actions.add_parser("restore", help="restore a backup into this wallet home")
    rs.set_defaults(func=cmd_restore)
    rs.add_argument("--archive", help="backup file to restore")
    rs.add_argument("--force", action="store_true", help="overwrite existing files")
    rs.add_argument("--passphrase-stdin", action="store_true")

    seed = actions.add_parser("seed", help="print the recovery seed (operator, passphrase, --confirm)")
    seed.set_defaults(func=cmd_seed)
    seed.add_argument("--kind", default="monero-mainnet")
    seed.add_argument("--confirm", action="store_true")
    seed.add_argument("--passphrase-stdin", action="store_true")

    demo = actions.add_parser("demo", help="run the whole flow on the simulated chain (no funds move)")
    demo.set_defaults(func=cmd_demo)
    demo.add_argument("--kind", default="sim-mainnet")
    demo.add_argument("--amount", default="0.001")
    demo.add_argument("--payee", default="")
    demo.add_argument("--passphrase-stdin", action="store_true")
    demo.add_argument("--no-install", action="store_true",
                      help="do not download the signer; fail instead if it is missing")
    demo.add_argument("--with-402", action="store_true", dest="with_402",
                      help="also price a purchase from the bundled x402 seller "
                           "(enables USDC on Base, which is opt-in)")
    demo.add_argument("--home", help="run the demo against this wallet home instead of a scratch one")
    demo.add_argument("--json", action="store_true", dest="as_json")

    auto = actions.add_parser("autolock", help="how long the wallet stays unlocked: 5, 30, off")
    auto.set_defaults(func=cmd_autolock)
    auto.add_argument("value", nargs="?", help="minutes, or 'off'")
    auto.add_argument("--passphrase-stdin", action="store_true")

    hist = actions.add_parser("history", help="archive the receipt chain (the old chain is kept)")
    hist.set_defaults(func=cmd_history)
    hist.add_argument("action", nargs="?", choices=["clear", "verify"], default="clear")
    hist.add_argument("--passphrase-stdin", action="store_true")
    hist.add_argument("--json", action="store_true", dest="as_json")

    ready = actions.add_parser("ready", help="GO/NO-GO for one real payment (builds it, sends nothing)")
    ready.set_defaults(func=cmd_ready)
    ready.add_argument("--kind", default="", help="chain id, default: the first Monero chain")
    ready.add_argument("--asset", default="", help="asset, default: the chain's native coin")
    ready.add_argument("--amount", default="", help="the amount you intend to send")
    ready.add_argument("--payee", default="", help="destination (default: your own address, a self-test)")
    ready.add_argument("--json", action="store_true", dest="as_json")

    docs = actions.add_parser("doctor", help="health check: keystore, signer, chains, receipts")
    docs.set_defaults(func=cmd_doctor)
    docs.add_argument("--json", action="store_true", dest="as_json")

    sgn = actions.add_parser("signer", help="the Monero signer: install / start / stop / status")
    sgn.set_defaults(func=cmd_signer)
    sgn.add_argument("action", nargs="?", choices=["status", "install", "start", "stop", "repair"],
                     default="status")
    sgn.add_argument("--port", type=int, default=38084)
    sgn.add_argument("--node", default="", help="node URL (default: first healthy from the registry)")
    sgn.add_argument("--passphrase-stdin", action="store_true")
    sgn.add_argument("--json", action="store_true", dest="as_json")

    init = actions.add_parser("init", help="create the chain wallet if missing (generates keys)")
    init.set_defaults(func=cmd_init)
    init.add_argument("--kind", default="monero-mainnet")
    init.add_argument("--passphrase-stdin", action="store_true")
    init.add_argument("--json", action="store_true", dest="as_json")

    imp = actions.add_parser("import", help="restore a wallet from its recovery words")
    imp.set_defaults(func=cmd_import)
    imp.add_argument("--kind", default="monero-mainnet")
    imp.add_argument("--restore-height", type=int, default=0)
    imp.add_argument("--passphrase-stdin", action="store_true")

    book = actions.add_parser("contacts", help="address book: list, save, delete")
    book.set_defaults(func=cmd_contacts)
    book.add_argument("action", nargs="?", choices=["list", "save", "delete"], default="list")
    book.add_argument("--name", help="contact name")
    book.add_argument("--kind", default="monero-mainnet", help="chain id the address belongs to")
    book.add_argument("--address", help="address to save")
    book.add_argument("--notes", default="")
    book.add_argument("--passphrase-stdin", action="store_true")

    shop = actions.add_parser("merchants", help="where to buy things with crypto")
    shop.set_defaults(func=cmd_merchants)
    shop.add_argument("--query", default="")
    shop.add_argument("--category", default="")

    dep = actions.add_parser("deposits", help="receive accounts that merge into the main wallet")
    dep.set_defaults(func=cmd_deposits)
    dep.add_argument("action", nargs="?",
                     choices=["list", "create", "merge", "merge-orders", "auto"], default="list")
    dep.add_argument("--kind", help="chain id (default: the enabled Monero chain)")
    dep.add_argument("--label", default="", help="what this address is for")
    dep.add_argument("--manual", action="store_true", help="do not merge automatically")
    dep.add_argument("--off", action="store_true", help="with action=auto: turn auto-merge off")
    dep.add_argument("--deposit-id", help="with action=merge/auto")
    dep.add_argument("--passphrase-stdin", action="store_true",
                     help="read the passphrase from stdin (scripts; no TTY needed)")

    sbx = actions.add_parser("sandbox", help="capped sub-wallets: list, create, revoke, destroy")
    sbx.set_defaults(func=cmd_sandbox)
    sbx.add_argument("action", nargs="?",
                     choices=["list", "create", "arm", "unattended", "fund", "revoke", "sweep",
                              "destroy", "reap", "payees"],
                     default="list")
    sbx.add_argument("--kind", help="chain id, e.g. monero-mainnet")
    sbx.add_argument("--asset", default="XMR")
    sbx.add_argument("--budget", help="total budget to move into the sandbox")
    sbx.add_argument("--hours", type=int, default=24)
    sbx.add_argument("--label", default="")
    sbx.add_argument("--sandbox-id", help="with action=arm/revoke/sweep/destroy/payees")
    sbx.add_argument("--one-time", action="store_true",
                     help="create: one payment only, then the sandbox destroys itself")
    sbx.add_argument("--destruct-after", type=int, default=None,
                     help="create: seconds between the payment and teardown (60..86400, default 1800)")
    sbx.add_argument("--force", action="store_true",
                     help="destroy: allow closing a budget sandbox (funds are still swept first)")
    sbx.add_argument("--clear", action="store_true",
                     help="with action=payees: forget every allowed address (contacts still apply)")
    sbx.add_argument("--no-sweep", action="store_true",
                     help="with action=revoke: leave the leftover budget in the sub-account")
    sbx.add_argument("--pay-to", action="append", metavar="ADDRESS", default=[],
                     help="create: an address this budget may pay without asking (repeatable). "
                          "Without it, a sandbox only auto-pays saved contacts and everything else "
                          "needs your approval")
    sbx.add_argument("--passphrase-stdin", action="store_true")
    sbx.add_argument("--unattended", action="store_true",
                     help="create: hand this budget to the agent — it may pay any address from it "
                          "without a per-payment PIN, up to the budget. Costs the passphrase and the PIN")
    sbx.add_argument("--off", action="store_true",
                     help="with action=unattended: take the hand-over back (free, no secret)")
    sbx.add_argument("--amount", help="with action=fund: how much more to ask the operator for")
    sbx.add_argument("--why", default="", help="with action=fund: one line the operator reads")
    sbx.add_argument("--pin-stdin", action="store_true",
                     help="read the PIN from stdin (scripts; no TTY needed)")

    svc = actions.add_parser("service", help="run the signer as a background service (systemd --user)")
    svc.set_defaults(func=cmd_service)
    svc.add_argument("action", nargs="?", choices=["install", "status", "uninstall", "env"], default="status")
    svc.add_argument("--set", action="append", metavar="KEY=VALUE",
                     help="with action=env: write a signer environment value (e.g. AGENT_WALLET_MONERO_WALLET)")

    gate = actions.add_parser("gate", help="turn the passphrase prompt on or off (operator + passphrase)")
    gate.set_defaults(func=cmd_gate)
    gate.add_argument("action", choices=["on", "off"])
    gate.add_argument("--passphrase-stdin", action="store_true")

    add_json_flags(actions)
    _hide_from_help(actions)            # last: the choices view must see every command
    _install_error_boundary(actions)


#: subparsers action -> the command names it must still accept but never show
_HIDDEN: Dict[Any, set] = {}


def _hide_from_help(actions: argparse._SubParsersAction) -> None:
    """Swap the choices mapping argparse renders from for one that skips the hidden commands."""
    hidden = _HIDDEN.get(actions) or set()

    class _VisibleChoices(dict):
        def __iter__(self):
            return iter([key for key in dict.keys(self) if key not in hidden])

        def keys(self):                       # type: ignore[override]
            return list(self)

        def __repr__(self):
            return repr(list(self))

    actions.choices = _VisibleChoices(actions._name_parser_map)   # type: ignore[assignment]
    # The command list is built from these pseudo-actions, and this argparse renders one whose help
    # is SUPPRESS as the literal text "==SUPPRESS==": drop the rows outright.
    actions._choices_actions = [row for row in actions._choices_actions if row.dest not in hidden]


def _add_hidden_parser(actions: argparse._SubParsersAction, name: str, func,
                       aliases: Optional[List[str]] = None) -> argparse.ArgumentParser:
    """Register a command that still parses but appears nowhere in `--help`.

    `help=SUPPRESS` hides the line under the command list but *not* the `{a,b,c}` choice list in
    the usage line, and `add_parser` without `help` does the reverse: no line, but the name is still
    in every usage banner and in argparse's "invalid choice: choose from …" message. Both still
    advertise it. The choices mapping is what argparse renders the usage from, so `_hide_from_help`
    swaps in a view that skips the hidden names at the end of `setup()`; `_name_parser_map` itself
    keeps every command, so scripts that still call `incoming` keep working.
    """
    hidden = {name, *(aliases or [])}
    parser = actions.add_parser(name, aliases=aliases or [], help=argparse.SUPPRESS, add_help=False)
    # `actions.choices` IS `actions._name_parser_map` (same object), so the view must be installed
    # after every command exists — `_hide_from_help` runs last in setup(). A view installed here
    # would refuse to parse the hidden command, which is the one thing it must still do.
    _HIDDEN.setdefault(actions, set()).update(hidden)
    parser.set_defaults(func=func)
    parser.add_argument("--json", action="store_true", dest="as_json", default=argparse.SUPPRESS)
    return parser


def add_json_flags(actions: argparse._SubParsersAction) -> None:
    """`hermes wallet --json status` worked; `hermes wallet status --json` did not.

    The flag belongs on the leaf parser too: the help advertises it and a shell user types it after
    the command. Harmless on the interactive commands.
    """
    for parser in actions.choices.values():
        # `config set --json '<object>'` already owns the flag with a different destination
        if any("--json" in action.option_strings for action in parser._actions):
            continue
        parser.add_argument("--json", action="store_true", dest="as_json", help="raw JSON output")


def run(args: argparse.Namespace) -> int:
    # `--json` is declared with `default=argparse.SUPPRESS` on the shared parser (so a subparser's own
    # `--json` cannot clobber the global one), which means the attribute is simply absent when nobody
    # passed it. Handlers read `args.as_json` directly, so one missing default crashed three commands
    # (`spend`, `receive`, `incoming`). Normalise it once, here.
    if not getattr(args, "as_json", False) and getattr(args, "global_json", False):
        args.as_json = True
    if not hasattr(args, "as_json"):
        args.as_json = False
    try:
        return args.func(args)
    except client.WalletUnavailable as exc:
        return _fail(str(exc), 3)
    except KeyboardInterrupt:
        print("\naborted", file=sys.stderr)
        return 130
    except Exception as exc:            # noqa: BLE001 - same promise as _bound: one line, no stack
        print(f"error: internal_error: {type(exc).__name__}: {exc}".rstrip(), file=sys.stderr)
        if os.environ.get("WALLET_DEBUG"):
            import traceback
            traceback.print_exc()
        return 2
