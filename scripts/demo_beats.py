#!/usr/bin/env python3
"""Rehearse the demo's three beats for real, and print what a viewer would see.

    python3 scripts/demo_beats.py            # fresh scratch wallet
    python3 scripts/demo_beats.py --home DIR # reuse a staged wallet

Beat 1 — the agent sends money, a human types the PIN, the receipt lands.
Beat 2 — a prompt-injected payment to an address the human never used: the card shows it, the human
         denies it, and the refusal is a receipt.
Beat 3 — a sandbox: the budget is real money moved to its own account, the agent spends inside it, an
         unblessed destination escalates to the human, and revoking sweeps the remainder home.

Everything runs on `sim-mainnet` (labelled as simulated) or on the staged wallet's own chain. Nothing
here can touch a real wallet: the home is scratch and `wallet demo` writes its own.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PASS = "demo-beats-passphrase"
PIN = "123456"
HERMES = os.environ.get("HERMES_BIN") or "hermes"
KIND = "sim-mainnet"
ASSET = "XMR"


def screen(title: str) -> None:
    print(f"\n\033[1m▸ {title}\033[0m")


def show(*lines: str) -> None:
    for line in lines:
        print(f"   {line}")


def hermes(home: Path, *args: str, stdin: str = "") -> subprocess.CompletedProcess:
    env = {**os.environ, "AGENT_WALLET_HOME": str(home)}
    env.pop("AGENT_WALLET_ALLOW_PRIVATE_402", None)
    return subprocess.run([HERMES, "wallet", *args], input=stdin, text=True,
                          capture_output=True, env=env, timeout=300)


def tool(home: Path, tool_name: str, **kwargs):
    os.environ["AGENT_WALLET_HOME"] = str(home)
    from wallet.tools import TOOL_SPECS

    handler = next(fn for registered, _d, fn, _s in TOOL_SPECS if registered == tool_name)
    raw = handler(**kwargs)
    return json.loads(raw) if isinstance(raw, str) else raw


def card(filed: dict) -> None:
    """Print the four things the approval card must show — the demo's whole argument."""
    req = filed.get("request") or {}
    decision = filed.get("decision") or {}
    show(f"card      {req.get('amount')} {req.get('asset')} on {req.get('kind')}",
         f"pay to    {req.get('payee')}",
         f"fee       {req.get('fee_estimate') or '(quoted at approval)'}",
         f"why       {req.get('why') or '(none)'}",
         f"verdict   {decision.get('rule')}/{decision.get('code')} "
         f"· needs a human: {bool(filed.get('approval_id'))}"
         f" · provenance: {(req.get('provenance') or {}).get('method', 'wallet')}")


def approve(home: Path, request_id: str, amount: str = "") -> subprocess.CompletedProcess:
    args = ["approve", request_id, "--pin-stdin", "--passphrase-stdin", "--yes"]
    if amount:
        args += ["--amount", amount]
    return hermes(home, *args, stdin=PIN + "\n" + PASS + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home")
    args = parser.parse_args()
    home = Path(args.home).expanduser() if args.home else Path(tempfile.mkdtemp(prefix="demo-beats-"))

    screen("stage: a scratch wallet (the simulator keeps its own play money)")
    demo = hermes(home, "demo", "--no-install", "--amount", "0.001", "--passphrase-stdin",
                  stdin=PASS + "\n")
    show((demo.stdout or demo.stderr).strip().splitlines()[-1][:100])

    screen("0: the pane: balances and the coin set")
    accounts = tool(home, "wallet_balances").get("accounts") or []
    for row in accounts[:4]:
        show(f"{row.get('asset'):<5} {str(row.get('balance')):<12} {row.get('kind')}"
             f"{'  (simulated)' if row.get('simulated') else ''}")

    # ------------------------------------------------------------------ beat 1: send money
    screen("1: \"send adrian 10 dollars\" — the agent files, the human approves")
    address = tool(home, "wallet_receive_address", kind=KIND, label="adrian").get("address", "")
    hermes(home, "contacts", "save", "--name", "adrian", "--kind", KIND, "--address", address,
           "--passphrase-stdin", stdin=PASS + "\n")
    filed = tool(home, "wallet_request_payment", amount="10$", payee="adrian", kind=KIND,
                 why="the pizza he spotted me")
    card(filed)
    sent = approve(home, (filed.get("request") or {}).get("request_id", ""),
                   (filed.get("request") or {}).get("amount", ""))
    show("approve   " + ((sent.stdout or sent.stderr).strip().splitlines()[-1][:100] if (sent.stdout or sent.stderr) else ""))

    # ------------------------------------------------------------------ beat 2: injection denied
    screen("2: an injected payment — the card shows an address the human never used")
    attacker = "48" + "Att4ck3rAddre55" * 3 + "X" * (95 - 2 - 45)
    filed_attack = tool(home, "wallet_request_payment", amount="0.02", payee=attacker, kind=KIND,
                        why="as instructed by the page I just read",
                        source_url="https://blog.example/how-to-tip")
    card(filed_attack)
    req = filed_attack.get("request") or {}
    show(f"human     denies {req.get('request_id')}")
    denied = hermes(home, "deny", req.get("request_id", ""), "--reason", "I did not ask for this",
                    "--passphrase-stdin", stdin=PASS + "\n")
    show("result    " + ("denied" if denied.returncode == 0 else (denied.stderr or "").strip()[:80]))
    ledger = tool(home, "wallet_ledger", limit=5).get("entries") or []
    refusal = next((row for row in reversed(ledger) if row.get("decision") == "denied"), {})
    show(f"receipt   {refusal.get('seq')}  {refusal.get('kind')}/{refusal.get('decision')}"
         f"  rule={refusal.get('rule')}")

    # ------------------------------------------------------------------ beat 3: the sandbox
    screen("3: a sandbox — real money in its own account, the agent spends inside it")
    created = hermes(home, "sandbox", "create", "--kind", KIND, "--asset", ASSET, "--budget", "0.02",
                     "--label", "one task", "--hours", "2", "--passphrase-stdin", "--json",
                     stdin=PASS + "\n")
    try:
        box = json.loads(created.stdout[created.stdout.index("{"):])
    except (ValueError, json.JSONDecodeError):
        box = {}
    sandbox = box.get("sandbox") or {}
    funding = (box.get("funding") or {}).get("request") or {}
    sid = sandbox.get("sandbox_id", "")
    show(f"sandbox   {sid} {sandbox.get('budget')} {sandbox.get('asset')} {sandbox.get('mode')}",
         f"account   0/{sandbox.get('address') or '(pooled)'}",
         f"funding   {funding.get('request_id')} {funding.get('amount')} {funding.get('status')}")
    if funding.get("request_id"):
        fund = approve(home, funding["request_id"], funding.get("amount", ""))
        show("funded    " + ("yes" if fund.returncode == 0 else (fund.stderr or "").strip()[:80]))
    rows = tool(home, "wallet_sandboxes").get("sandboxes") or []
    mine = next((row for row in rows if row.get("sandbox_id") == sid), {})
    show(f"live      {mine.get('live_balance')} {ASSET} in the budget"
         f" ({mine.get('remaining')} of {mine.get('budget')} unspent)")

    blessed = tool(home, "wallet_request_payment", amount="0.002", payee=address, kind=KIND,
                   sandbox_id=sid, why="a small purchase inside the budget")
    card(blessed)
    paid = approve(home, (blessed.get("request") or {}).get("request_id", ""),
                   (blessed.get("request") or {}).get("amount", ""))
    show("spent     " + ("inside the budget" if paid.returncode == 0 else (paid.stderr or "").strip()[:90]))

    screen("3b: the same budget, an address nobody blessed — it escalates to a human")
    stranger = tool(home, "wallet_request_payment", amount="0.002",
                    payee=tool(home, "wallet_receive_address", kind=KIND, label="stranger")["address"],
                    kind=KIND, sandbox_id=sid, why="a different shop")
    decision = stranger.get("decision") or {}
    show(f"verdict   {decision.get('rule')}/{decision.get('code')} — "
         f"{'human card' if stranger.get('approval_id') else 'no card'}",
         f"reason    {decision.get('reason')}")

    screen("3d: a budget handed over — the agent spends on its own, and asks for more")
    hand = hermes(home, "sandbox", "create", "--kind", KIND, "--asset", ASSET, "--budget", "0.02",
                  "--label", "shopping run", "--hours", "2", "--unattended",
                  "--passphrase-stdin", "--pin-stdin", "--json",
                  stdin=PASS + "\n" + PIN + "\n")
    try:
        armed = json.loads(hand.stdout[hand.stdout.index("{"):])
    except (ValueError, json.JSONDecodeError):
        armed = {}
    hbox = armed.get("sandbox") or {}
    hsid = hbox.get("sandbox_id", "")
    hfund = (armed.get("funding") or {}).get("request") or {}
    show(f"armed     {hsid}  unattended={hbox.get('unattended')}  "
         f"(the passphrase and the PIN bought this once)",
         f"funding   {hfund.get('request_id')} {hfund.get('amount')} {hfund.get('status')}")
    if hfund.get("request_id"):
        hf = approve(home, hfund["request_id"], hfund.get("amount", ""))
        show("funded    " + ("yes" if hf.returncode == 0 else (hf.stderr or "").strip()[:80]))
    shop = tool(home, "wallet_request_payment", amount="0.004",
                payee=tool(home, "wallet_receive_address", kind=KIND, label="shop")["address"],
                kind=KIND, sandbox_id=hsid, why="a shop the human never saw")
    sreq = shop.get("request") or {}
    show(f"spend     {sreq.get('status')} by {sreq.get('authorised_by')} — no PIN, "
         f"{sreq.get('amount')} {ASSET} to an address nobody blessed")
    more = tool(home, "wallet_request_sandbox", sandbox_id=hsid, budget="0.01",
                label="another order")
    mreq = (more.get("funding") or {}).get("request") or {}
    after = next((row for row in (tool(home, "wallet_sandboxes").get("sandboxes") or [])
                  if row.get("sandbox_id") == hsid), {})
    show(f"ask more  {mreq.get('request_id')} {mreq.get('status')} — the ceiling did not move: "
         f"{after.get('budget')} {ASSET} (needs the human's PIN)")

    screen("3c: revoking returns the rest of the budget home, with a receipt")
    revoke = hermes(home, "sandbox", "revoke", "--sandbox-id", sid, "--passphrase-stdin", "--json",
                    stdin=PASS + "\n")
    try:
        result = json.loads(revoke.stdout[revoke.stdout.index("{"):])
    except (ValueError, json.JSONDecodeError):
        result = {}
    sweep = result.get("sweep") or {}
    detail = sweep.get("result") or {}
    show(f"swept     {sweep.get('swept') or '0'} {ASSET} back to the main account"
         f" · fee {detail.get('fee') or detail.get('fee_reserved') or '0'}"
         f" · to {str(detail.get('to'))[:18]}…")
    hermes(home, "sandbox", "revoke", "--sandbox-id", hsid, "--passphrase-stdin", stdin=PASS + "\n")
    closed = next((row for row in (tool(home, "wallet_sandboxes").get("sandboxes") or [])
                   if row.get("sandbox_id") == hsid), {})
    show(f"revoke    {hsid} {closed.get('status')} · unattended={closed.get('unattended')} "
         f"— the hand-over ends with the budget")

    screen("4: the ledger proves all of it — and notices a forged line")
    verify = tool(home, "wallet_ledger", verify=True).get("verification") or {}
    show(f"verify    ok={verify.get('ok')} entries={verify.get('entries')}")
    spend = tool(home, "wallet_spend")
    assets = spend.get("assets") or {}
    show("spend     " + ", ".join(f"{asset} {row.get('spent')} (+{row.get('fees')} fees)"
                                  for asset, row in assets.items()))

    print("\nrecording sheet")
    print(f"  export AGENT_WALLET_HOME={home}")
    print(f"  passphrase {PASS}   PIN {PIN}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
