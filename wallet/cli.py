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
import sys
from datetime import datetime, timezone
from pathlib import Path
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from . import client, daemon, service


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


def _balance_table(accounts: List[Dict[str, Any]]) -> str:
    """Coin and amount on one line, the address underneath it, aligned."""
    if not accounts:
        return "  no chains are reachable right now"
    lines: List[str] = []
    for row in accounts:
        asset = str(row.get("asset", "?"))
        amount = row.get("balance")
        state = "live" if row.get("available") else ("locked" if row.get("state") == "locked" else "offline")
        shown = "\u2014" if amount is None else f"{amount} {asset}"
        label = COIN_LABEL.get(asset, asset)
        lines.append(f"  {asset:<4} {label:<14} {shown:>22}   {state}")
        detail = " \u00b7 ".join(x for x in (CHAIN_LABEL.get(row.get("kind", ""), row.get("kind", "")),
                                             row.get("provider", "")) if x)
        if detail:
            lines.append(f"       {detail}")
        if row.get("address"):
            lines.append(f"       {row['address']}")
        elif row.get("error"):
            lines.append(f"       {str(row['error'])[:100]}")
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


def _out(payload: Any, as_json: bool = False) -> None:
    """Pretty by default, raw with --json. A CLI that only dumps JSON is a debug tool, not a wallet."""
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
        if "entries" in payload:
            print(_receipt_table(payload["entries"]))
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
            for row in rows:
                extra = f"{row.get('mode', 'budget')}"
                if row.get("mode") == "one_time":
                    left = row.get("destroy_in")
                    extra += f", self-destructs in {left}s" if left is not None else ", waiting for its payment"
                print(f"  {row.get('sandbox_id', ''):<16} {row.get('status', ''):<10} "
                      f"{str(row.get('remaining')):>12} / {str(row.get('budget')):<12} {row.get('asset', ''):<5} "
                      f"{CHAIN_LABEL.get(row.get('kind', ''), row.get('kind', '')):<16} {extra}")
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
    signer = "ready" if wallet.get("signer_reachable") else ("installed" if wallet.get("signer_installed") else "not installed")
    ttl = wallet.get("unlock_expires_in")
    lock_text = "unlocked" + (f" (locks in {max(1, round(ttl / 60))}m)" if isinstance(ttl, int) else "") \
        if not wallet.get("locked") else "locked"
    lines.append(f"  wallet     {lock_text}")
    lines.append(f"  coins      {', '.join(wallet.get('assets', []))}")
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
                 f"receipts {wallet.get('ledger_archives', 0)} archive(s)")
    if wallet.get("config_tampered"):
        lines.append("  WARNING    settings signature does not match - defaults were restored")
    return "\n".join(lines)


def _fail(message: str, code: int = 2) -> int:
    print(json.dumps({"ok": False, "error": message}, indent=2), file=sys.stderr)
    return code


def _ensure_daemon() -> None:
    try:
        client.call("status", timeout=2)
    except client.WalletUnavailable:
        started = daemon.start()
        if not started.get("ok"):
            raise client.WalletUnavailable(
                f"walletd is not running and could not be started: {started.get('error') or started}")


def _passphrase(args: argparse.Namespace, prompt: str = "Wallet passphrase: ") -> str:
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

def cmd_doctor(args: argparse.Namespace) -> int:
    _ensure_daemon()
    result = client.call("doctor")
    if args.as_json:
        _out(result, True)
        return 0
    for check in result["checks"]:
        print(f"  {'ok  ' if check['ok'] else 'FAIL'}  {check['check']}")
    print(f"\n  chains     {', '.join(c['kind'] for c in result['chains'] if c.get('available')) or 'none live'}")
    print(f"  receipts   {result['receipts']}")
    print(f"  contacts   {result['contacts']}   sandboxes {result['sandboxes']}   pending {result['pending_requests']}")
    print(f"  signer     {result['rpc_url']}  (wallet file: {result['wallet_file']})")
    return 0 if result["ok"] else 1


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
    _ensure_daemon()
    _unlock_if_needed(args)
    if args.passphrase_stdin:
        seed = sys.stdin.readline().rstrip("\n")
    else:
        seed = getpass.getpass("Recovery words (25 words, hidden): ")
    if not seed.strip():
        return _fail("no recovery words supplied")
    result = client.call("wallet_import", {"kind": args.kind, "seed": seed,
                                           "restore_height": args.restore_height},
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
        _out(client.call("contact_save", {"name": args.name,
                                          "addresses": {args.kind: args.address},
                                          "notes": args.notes or ""}), args.as_json)
        return 0
    _unlock_if_needed(args)
    _out(client.call("contact_delete", {"name": args.name}, scope=client.OPERATOR), args.as_json)
    return 0


def cmd_sandbox(args: argparse.Namespace) -> int:
    _ensure_daemon()
    if args.action == "list" or not args.action:
        _out(client.call("sandboxes_list"), args.as_json)
        return 0
    if args.action == "sweep":
        if not args.sandbox_id:
            return _fail("sandbox sweep needs --sandbox-id")
        _unlock_if_needed(args)
        _out(client.call("sandbox_sweep", {"sandbox_id": args.sandbox_id,
                                           "profile": client.current_profile()},
                         scope=client.OPERATOR), args.as_json)
        return 0
    if args.action == "create":
        if not (args.budget and args.kind):
            return _fail("sandbox create needs --kind and --budget")
        _unlock_if_needed(args)
        payload = {"kind": args.kind, "asset": args.asset, "budget": args.budget,
                   "lifetime_hours": args.hours, "label": args.label or "agent sandbox",
                   "mode": "one_time" if getattr(args, "one_time", False) else "budget"}
        if getattr(args, "destruct_after", None):
            payload["self_destruct_seconds"] = args.destruct_after
        _out(client.call("sandbox_create", payload, scope=client.OPERATOR), args.as_json)
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
    passphrase = _passphrase(args, "Backup passphrase: ")
    try:
        _out(backup.create(client.wallet_home(), out, passphrase), args.as_json)
    except (FileNotFoundError, keystore.KeystoreError) as exc:
        return _fail(str(exc))
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    backup, keystore = _walletd_modules()

    if not args.archive:
        return _fail("restore needs --archive <file>")
    passphrase = _passphrase(args, "Backup passphrase: ")
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
    passphrase = _passphrase(args, "Wallet passphrase: ")
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

    No funds move: `sim-mainnet` builds and 'broadcasts' a plan with no transaction behind it, and
    every receipt it produces is marked simulated. It exists so the flow can be shown and reviewed
    without money and without a funded wallet.
    """
    _ensure_daemon()
    passphrase = _passphrase(args)
    status = client.call("status")
    # First run from a clean machine: make the demo actually be one command. Creating the keystore
    # and installing the signer are the two things that used to come before it.
    if not status.get("initialized"):
        print("0. bootstrap  creating the keystore (keys stay in the daemon)")
        client.call("create_keystore", {"passphrase": passphrase}, scope=client.OPERATOR)
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
                                           "confirm_amount": amount, "passphrase": passphrase},
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
    return 0


def cmd_nodes(args: argparse.Namespace) -> int:
    _ensure_daemon()
    _out(client.call("nodes"), args.as_json)
    return 0


def cmd_chains(args: argparse.Namespace) -> int:
    _ensure_daemon()
    _out(client.call("chain_status"), args.as_json)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    _ensure_daemon()
    _out({"status": client.call("status")}, args.as_json)
    return 0


def cmd_up(args: argparse.Namespace) -> int:
    _out(daemon.start(), args.as_json)
    return 0


def cmd_down(args: argparse.Namespace) -> int:
    _out(daemon.stop(), args.as_json)
    return 0


def cmd_receive(args: argparse.Namespace) -> int:
    """A fresh address for one incoming payment."""
    _ensure_daemon()
    params: Dict[str, Any] = {"label": args.label or ""}
    if getattr(args, "expected", ""):
        params["expected"] = args.expected
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
        expected = row.get("expected") or "-"
        chain = CHAIN_LABEL.get(row.get("kind", ""), row.get("kind", ""))
        lines.append(f"  {(row.get('label') or '(no label)'):<22} {row.get('status', ''):<8} "
                     f"{row.get('received', '0'):>14} / {expected:<14} {row.get('asset', ''):<4} "
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
    _ensure_daemon()
    _out(client.call("unlock", {"passphrase": _passphrase(args)}, scope=client.OPERATOR), args.as_json)
    return 0


def cmd_lock(args: argparse.Namespace) -> int:
    _out(client.call("lock", {}, scope=client.OPERATOR), args.as_json)
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    """Approve one request, from a terminal, with the human watching."""
    _ensure_daemon()
    passphrase = _passphrase(args)          # human gate: the same secret authorises the payment
    status = client.call("status")
    if status.get("locked"):
        client.call("unlock", {"passphrase": passphrase}, scope=client.OPERATOR)
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
        return _fail("not approved (no confirmation)")

    threshold = Decimal(str(_settings().get("approval_threshold", "0")))
    confirm_amount = args.amount
    try:
        over_threshold = Decimal(str(match["amount"])) > threshold
    except (InvalidOperation, ValueError):
        over_threshold = True
    if over_threshold and not confirm_amount:
        if not sys.stdin.isatty():
            return _fail("this amount needs --amount <value> as confirmation")
        confirm_amount = input(f"Type {match['amount']} to confirm: ").strip()

    params = {"request_id": match["request_id"], "approval_id": match["approval_id"],
              "passphrase": passphrase}
    if confirm_amount:
        params["confirm_amount"] = confirm_amount
    _out(client.call("approve", params, scope=client.OPERATOR), args.as_json)
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
        _out(service.install(), args.as_json)
        return 0
    if args.action == "uninstall":
        _out(service.uninstall(), args.as_json)
        return 0
    _out(service.status(), args.as_json)
    return 0


def cmd_config(args: argparse.Namespace) -> int:
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


def cmd_balances(args: argparse.Namespace) -> int:
    """Every coin with its balance and receive address. The CLI's answer to 'where is my money'."""
    _ensure_daemon()
    _out(client.call("balances"), args.as_json)
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
        if args.as_json:
            _out(out, True)
            return 0
        status = client.call("status")
        enabled = list(status.get("assets", []))
    if args.as_json:
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


def setup(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("--json", action="store_true", dest="as_json", help="raw JSON output")
    actions = sub.add_subparsers(dest="action", required=True)

    for name, func, help_text in (
        ("status", cmd_status, "lock state, chains, caps, pending count"),
        ("balances", cmd_balances, "every coin: balance and receive address"),
        ("coins", cmd_coins, "list coins; --enable/--disable to add or remove one"),
        ("chains", cmd_chains, "chain availability and height"),
        ("nodes", cmd_nodes, "public node health per chain"),
        ("up", cmd_up, "start the signer daemon"),
        ("down", cmd_down, "stop a daemon this process started"),
        ("unlock", cmd_unlock, "unlock with the passphrase (operator)"),
        ("lock", cmd_lock, "lock and drop the key from memory"),
        ("requests", cmd_requests, "list requests with their approval ids"),
        ("approve", cmd_approve, "approve one request"),
        ("deny", cmd_deny, "deny one request"),
        ("verify", cmd_verify, "verify the receipt hash chain"),
        ("ledger", cmd_ledger, "print recent receipts"),
    ):
        parser = actions.add_parser(name, help=help_text)
        parser.set_defaults(func=func)
        if name in ("unlock", "approve", "deny", "gate"):
            parser.add_argument("--passphrase-stdin", action="store_true",
                                help="read the passphrase from stdin (scripts/tests only)")
        if name == "approve":
            parser.add_argument("request_id")
            parser.add_argument("--amount", help="typed confirmation for amounts above the threshold")
            parser.add_argument("--yes", action="store_true",
                                help="skip the interactive y/N prompt (the passphrase is still required)")
        if name == "deny":
            parser.add_argument("request_id")
            parser.add_argument("--reason", default="")
        if name == "coins":
            parser.add_argument("--enable", action="append", metavar="COIN", help="add a coin (repeatable)")
            parser.add_argument("--disable", action="append", metavar="COIN", help="remove a coin (repeatable)")
            parser.add_argument("--passphrase-stdin", action="store_true")
            parser.add_argument("--json", action="store_true", dest="as_json",
                                help="raw JSON (also accepted before the subcommand)")
        if name == "ledger":
            parser.add_argument("--limit", type=int, default=20)
            parser.add_argument("--csv", action="store_true",
                                help="every receipt as CSV (seq,ts,kind,decision,rule,asset,amount,payee,tx_hash)")
            parser.add_argument("--since", default="", help="CSV window start: 30d, 12h or an ISO timestamp")
            parser.add_argument("--until", default="", help="CSV window end")

    recv = actions.add_parser("receive", help="a fresh address for one incoming payment")
    recv.set_defaults(func=cmd_receive)
    recv.add_argument("--label", default="")
    recv.add_argument("--expected", default="", help="order total, so `incoming` can say paid/partial")
    recv.add_argument("--json", action="store_true", dest="as_json")

    inc = actions.add_parser("incoming", help="incoming orders: what arrived for each subaddress")
    inc.set_defaults(func=cmd_incoming)
    inc.add_argument("--json", action="store_true", dest="as_json")

    spend = actions.add_parser("spend", help="spend totals per asset and per payee")
    spend.set_defaults(func=cmd_spend)
    spend.add_argument("--since", default="", help="window start: 30d, 12h, 2w or an ISO timestamp")
    spend.add_argument("--until", default="", help="window end")
    spend.add_argument("--json", action="store_true", dest="as_json")

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
    pay.add_argument("--json", action="store_true", dest="as_json")
    pay.add_argument("--allow-loopback", action="store_true", dest="allow_loopback",
                     help="demo only: allow a 402 quote from 127.0.0.1 (the bundled seller)")

    mandates = actions.add_parser("mandates", help="autopays: list, create, edit, pause, resume, delete")
    mandates.set_defaults(func=cmd_mandates)
    mandates.add_argument("action", nargs="?",
                          choices=["list", "create", "edit", "pause", "resume", "activate",
                                   "revoke", "delete"], default="list")
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

    sbx = actions.add_parser("sandbox", help="capped sub-wallets: list, create, revoke, destroy")
    sbx.set_defaults(func=cmd_sandbox)
    sbx.add_argument("action", nargs="?",
                     choices=["list", "create", "revoke", "sweep", "destroy", "reap"], default="list")
    sbx.add_argument("--kind", help="chain id, e.g. monero-mainnet")
    sbx.add_argument("--asset", default="XMR")
    sbx.add_argument("--budget", help="total budget to move into the sandbox")
    sbx.add_argument("--hours", type=int, default=24)
    sbx.add_argument("--label", default="")
    sbx.add_argument("--sandbox-id", help="with action=revoke/sweep")
    sbx.add_argument("--one-time", action="store_true",
                     help="create: one payment only, then the sandbox destroys itself")
    sbx.add_argument("--destruct-after", type=int, default=None,
                     help="create: seconds between the payment and teardown (60..86400, default 1800)")
    sbx.add_argument("--force", action="store_true",
                     help="destroy: allow closing a budget sandbox (funds are still swept first)")
    sbx.add_argument("--no-sweep", action="store_true",
                     help="with action=revoke: leave the leftover budget in the sub-account")
    sbx.add_argument("--passphrase-stdin", action="store_true")

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
    try:
        return args.func(args)
    except client.WalletUnavailable as exc:
        return _fail(str(exc), 3)
