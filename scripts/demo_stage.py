#!/usr/bin/env python3
"""Stage a wallet for recording the demo, so the take is mechanical instead of improvised.

Creates a scratch wallet home, saves a contact, enables USDC on Base, files one old approved payment
(so the ledger is not empty on camera) and prints the recording sheet.

    python3 scripts/demo_stage.py [--home DIR]

Nothing here touches a real wallet: the home defaults to a fresh temp dir, and every payment it makes
is on the simulator. Fund the printed USDC address only if you want the x402 beat to settle on mainnet.
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
PASS = "demo-stage-passphrase"
PIN = "123456"
HERMES = os.environ.get("HERMES_BIN") or "hermes"

SELLER = "https://x402.tbs-digital.com/weather/current"     # 0.001 USDC on Base, verified live


def sh(home: Path, *args: str, stdin: str = "") -> subprocess.CompletedProcess:
    # the plugin lives in the real HERMES_HOME; only the *wallet* is scratch
    env = {**os.environ, "AGENT_WALLET_HOME": str(home)}
    env.pop("AGENT_WALLET_ALLOW_PRIVATE_402", None)
    return subprocess.run([HERMES, "wallet", *args], input=stdin, text=True,
                          capture_output=True, env=env, timeout=300)


def tool(home: Path, tool_name: str, **kwargs):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    os.environ["AGENT_WALLET_HOME"] = str(home)
    from wallet.tools import TOOL_SPECS

    handler = next(fn for registered, _d, fn, _s in TOOL_SPECS if registered == tool_name)
    raw = handler(**kwargs)
    return json.loads(raw) if isinstance(raw, str) else raw


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", help="stage into this wallet home instead of a fresh temp dir")
    args = parser.parse_args()

    home = Path(args.home).expanduser() if args.home else Path(tempfile.mkdtemp(prefix="wallet-demo-"))
    print(f"staging into {home}\n")

    print("1. wallet + simulator")
    demo = sh(home, "demo", "--no-install", "--amount", "0.001", "--passphrase-stdin",
              stdin=PASS + "\n")
    print("   " + (demo.stdout or demo.stderr).strip().splitlines()[-1][:100])

    print("2. USDC on Base, so the x402 beat is priced instead of refused")
    coins = sh(home, "coins", "--enable", "USDC", "--passphrase-stdin", stdin=PASS + "\n")
    print("   " + ("USDC enabled" if coins.returncode == 0 else coins.stderr.strip()[:100]))

    print("3. a contact, so \"send adrian 10 dollars\" resolves by name")
    address = tool(home, "wallet_receive_address", kind="sim-mainnet", label="adrian").get("address", "")
    tool(home, "wallet_contact_save", name="adrian", kind="sim-mainnet", address=address)
    print(f"   adrian -> {str(address)[:20]}…")

    print("4. one old receipt, so the ledger is not empty on camera")
    old = tool(home, "wallet_request_payment", amount="0.0005", payee="adrian", kind="sim-mainnet",
               why="splitting the pizza")
    req = (old.get("request") or {}).get("request_id", "")
    sh(home, "approve", req, "--pin-stdin", "--passphrase-stdin", "--yes",
       stdin=PIN + "\n" + PASS + "\n")

    print("5. the live seller's quote (files a pending request; nothing is paid without your PIN)")
    quote = sh(home, "pay-url", SELLER, "--why", "one weather call", "--json", "--passphrase-stdin",
               stdin=PASS + "\n")
    payload = {}
    if "{" in quote.stdout:
        try:
            payload = json.loads(quote.stdout[quote.stdout.index("{"):])
        except json.JSONDecodeError:
            payload = {}
    price = (payload.get("quote") or {})
    print(f"   {price.get('amount')} {price.get('asset')} on {price.get('kind')} "
          f"-> {(payload.get('request') or {}).get('status')}")

    balances = tool(home, "wallet_balances").get("accounts") or []
    usdc = next((row for row in balances if row.get("asset") == "USDC"
                 and row.get("kind") == "base-mainnet"), {})
    print("\nrecording sheet")
    print(f"  export AGENT_WALLET_HOME={home}")
    print(f"  passphrase   {PASS}      PIN {PIN}")
    print(f"  USDC (Base)  {usdc.get('address') or '(none)'}   balance {usdc.get('balance')}")
    print(f"  seller       {SELLER}   ($0.001 per call)")
    print("""
  beats
   1  send adrian 10 dollars          -> the card: address, fee, fiat, new payee
   2  PIN in the card                -> sent + receipt (simulated, say so)
   3  paste a hostile line, then Deny -> a receipt for the refusal
   4  pay-url the seller, PIN         -> real 402 purchase (~1 cent, mainnet)
   5  wallet verify / wallet spend    -> ok, fees, verified; tamper one line -> bad signature

  before recording: run `hermes wallet demo` twice off camera (the cold run has ~10 s of dead air)
  and fund the USDC address above with ~1 USDC on Base if beat 4 should settle on chain.""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
