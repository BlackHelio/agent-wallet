#!/usr/bin/env python3
"""Prove the two sentences this product exists for, on a throwaway wallet.

    "send adrian 10 dollars"
    "deposit 10 dollars into my openrouter"

`rehearse.py` walks the API; this walks the *conversation*: it calls the same 27 agent tools the model
calls, in the order a model would, and asserts the things a human would notice on the card — the coin
amount, the fee, the fiat value, whether the payee is new, and that the money only moves after the PIN.

Run it from the repo root:

    python3 scripts/spec_check.py

It creates its own wallet home under a fresh temp dir, starts a daemon there, and never touches
`$HERMES_HOME/wallet`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PASS = "spec-check-passphrase"
PIN = "123456"
CHECKS: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((bool(ok), name, str(detail)[:110]))
    mark = "PASS" if ok else "FAIL"
    print(f"  {mark}  {name}" + (f"   {detail}" if detail else ""))


def hermes(*args: str, stdin: str = "", home: Path) -> subprocess.CompletedProcess:
    # NOTE: no AGENT_WALLET_ALLOW_PRIVATE_402 here — that env var switches the SSRF guard off for
    # *every* caller, so setting it would hide the check that the agent cannot reach a private page.
    # The bundled seller is reached the supported way: the operator's `--allow-loopback`.
    env = {**os.environ, "AGENT_WALLET_HOME": str(home)}
    env.pop("AGENT_WALLET_ALLOW_PRIVATE_402", None)
    env.pop("WALLETD_DISABLE_CHAINS", None)
    return subprocess.run([os.environ.get("HERMES_BIN") or "hermes", "wallet", *args],
                          input=stdin, text=True,
                          capture_output=True, env=env, timeout=300)


def _json_from(text: str) -> dict:
    """The CLI prints its banner before the JSON; take the object, not the noise."""
    start = text.find("{")
    if start < 0:
        return {}
    try:
        return json.loads(text[start:])
    except json.JSONDecodeError:
        return {}


def tool(tool_name: str, **kwargs):
    """Call an agent tool the way the model does: JSON in, JSON out."""
    from wallet.tools import TOOL_SPECS

    handler = next(fn for registered, _d, fn, _s in TOOL_SPECS if registered == tool_name)
    raw = handler(**kwargs)
    return json.loads(raw) if isinstance(raw, str) else raw


def main() -> int:
    home = Path(tempfile.mkdtemp(prefix="spec-check-"))
    print(f"wallet: {home}\n")
    os.environ["AGENT_WALLET_HOME"] = str(home)

    # ---------- 0. a fresh install has a wallet, a balance and a coin to spend ------------------
    print("== the wallet a stranger gets")
    demo = hermes("demo", "--no-install", "--amount", "0.001", "--passphrase-stdin",
                  stdin=PASS + "\n", home=home)
    check("demo bootstraps a wallet and pays once on the simulator", demo.returncode == 0,
          (demo.stdout or demo.stderr).strip().splitlines()[-1][:90] if (demo.stdout or demo.stderr) else "")
    status = (tool("wallet_status").get("wallet") or {})
    check("status answers without a passphrase (agent scope)", bool(status.get("coins")),
          f"coins={status.get('coins')}")
    rows = tool("wallet_balances").get("accounts") or []
    check("balances list a chain with an address", bool(rows), f"{len(rows)} row(s)")

    # ---------- 1. "send adrian 10 dollars" -----------------------------------------------------
    print("\n== \"send adrian 10 dollars\"")
    saved = tool("wallet_contact_save", name="adrian", kind="sim-mainnet",
                 address=tool("wallet_receive_address", kind="sim-mainnet", label="adrian")["address"])
    check("the agent can save a contact", bool(saved), str(saved)[:70])
    contacts = tool("wallet_contacts")
    named = [c for c in (contacts.get("contacts") or []) if (c.get("name") or "").lower() == "adrian"]
    check("the contact comes back by name, with its chain", bool(named),
          f"{named[0].get('name')} on {', '.join(sorted((named[0].get('addresses') or {}).keys()))}" if named else "")

    filed = tool("wallet_request_payment", payee="adrian", amount="10$", kind="sim-mainnet",
                 why="the price adrian quoted")
    request = filed.get("request") or {}
    decision = filed.get("decision") or {}
    check("a fiat amount is converted to a coin amount by the wallet, not by the agent",
          bool(request.get("amount")) and str(request.get("amount")) != "10",
          f"{request.get('amount')} {request.get('asset')} for 10 USD")
    check("the request waits for a human", decision.get("code") in ("needs_operator", "needs_pin"),
          f"{decision.get('rule')}/{decision.get('code')}")
    check("the card carries the fee that will be charged", bool(request.get("fee_estimate") or filed.get("fee")),
          f"fee={request.get('fee_estimate') or filed.get('fee')}")
    check("nothing was sent before the PIN", request.get("status") in ("pending", None),
          f"status={request.get('status')}")

    approved = hermes("approve", request["request_id"], "--pin-stdin", "--passphrase-stdin", "--yes",
                      stdin=PIN + "\n" + PASS + "\n", home=home)
    after = tool("wallet_status")
    sent_rows = [r for r in (tool("wallet_ledger").get("entries") or [])
                 if r.get("decision") == "approved"]
    check("the PIN sends it", approved.returncode == 0 and bool(sent_rows),
          (approved.stdout or approved.stderr).strip().splitlines()[-1][:90] if (approved.stdout or approved.stderr) else "")

    # ---------- 2. "deposit 10 dollars into my openrouter" --------------------------------------
    # A metered API is an HTTP 402 seller: the page quotes, the wallet prices it, the human approves.
    # The bundled seller asks for USDC on Base, which this wallet ships switched OFF — so the honest
    # demo is: priced, refused with the exact command that enables the coin, then pending once it is.
    print("\n== \"deposit 10 dollars into my openrouter\" (an HTTP 402 seller)")
    seller = subprocess.Popen([sys.executable, str(ROOT / "scripts" / "pay402_demo_server.py"), "38097"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(0.5)
        # wait for the seller to answer, or the first call measures python startup instead of policy
        import urllib.error
        import urllib.request

        for _ in range(40):
            try:
                urllib.request.urlopen("http://127.0.0.1:38097/sku/42", timeout=1)
                break
            except urllib.error.HTTPError:
                break                       # 402 is the answer we want
            except Exception:               # noqa: BLE001 - not listening yet
                time.sleep(0.25)
        refused = tool("wallet_pay_url", url="http://127.0.0.1:38097/sku/42")
        check("the agent cannot price a private address (SSRF guard)",
              refused.get("error") == "bad_url" or "bad_url" in json.dumps(refused),
              json.dumps(refused)[:90])

        first = hermes("pay-url", "http://127.0.0.1:38097/sku/42", "--allow-loopback",
                       "--passphrase-stdin", "--json", stdin=PASS + "\n", home=home)
        payload = _json_from(first.stdout)
        quote, req = payload.get("quote") or {}, payload.get("request") or {}
        check("the price comes from the page, not from the agent", bool(quote.get("amount")),
              f"{quote.get('amount')} {quote.get('asset')} for \"{quote.get('description') or quote.get('resource')}\"")
        check("the payee is the page's, and it is marked verified by the wallet",
              bool(req.get("payee")) and str((req.get("provenance") or {}).get("verified")) == "true",
              f"payee={str(req.get('payee'))[:44]}")
        check("a coin this wallet ships off is refused with the command that enables it",
              (payload.get("decision") or {}).get("code") == "chain_not_enabled"
              and "coins --enable USDC" in str((payload.get("decision") or {}).get("reason")),
              str((payload.get("decision") or {}).get("reason"))[:88])

        enabled = hermes("coins", "--enable", "USDC", "--passphrase-stdin", stdin=PASS + "\n", home=home)
        check("the operator can enable that coin with the passphrase", enabled.returncode == 0,
              (enabled.stdout or enabled.stderr).strip()[:70])
        second = hermes("pay-url", "http://127.0.0.1:38097/sku/42", "--allow-loopback",
                        "--passphrase-stdin", "--json", stdin=PASS + "\n", home=home)
        payload2 = _json_from(second.stdout)
        req2 = payload2.get("request") or {}
        check("with the coin on, the purchase is filed and waits for the PIN",
              req2.get("status") == "pending" and bool(payload2.get("approval_id")),
              f"{req2.get('request_id')} {req2.get('amount')} {req2.get('asset')} status={req2.get('status')}")
    finally:
        seller.terminate()
        try:
            seller.wait(timeout=10)
        except subprocess.TimeoutExpired:
            seller.kill()

    # ---------- 3. the budget handoff ----------------------------------------------------------
    print("\n== a budget for one task")
    sandbox = tool("wallet_request_sandbox", kind="sim-mainnet", asset="XMR", budget="0.01",
                   label="one task", hours=2)
    box = sandbox.get("sandbox") or {}
    check("the agent can ask for a budget", bool(box.get("sandbox_id")), box.get("sandbox_id", ""))
    check("…and it is unfunded until a human approves the transfer",
          (sandbox.get("funding") or {}).get("status") in ("pending", None, "denied"),
          str((sandbox.get("funding") or {}).get("status")))

    # ---------- 4. the receipts reconcile ------------------------------------------------------
    print("\n== the receipts")
    verify = tool("wallet_ledger", verify=True).get("verification") or {}
    check("the chain verifies offline", bool(verify.get("ok")), str(verify.get("entries", "")) + " entries")
    spend = tool("wallet_spend")
    assets = spend.get("assets") or {}
    fees = (spend.get("fees") or {})
    check("spend reports fees as well as amounts", bool(fees),
          f"spent={ {k: v['spent'] for k, v in assets.items()} } fees={fees}")
    check("the report says whether it verified what it summed", spend.get("verified") is True,
          str(spend.get("verified")))

    failed = [c for c in CHECKS if not c[0]]
    print(f"\nspec-check: {len(CHECKS) - len(failed)}/{len(CHECKS)} passed")
    daemon = hermes("down", home=home)
    if failed:
        print("failed:", *(c[1] for c in failed), sep="\n  - ")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
