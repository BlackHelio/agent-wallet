#!/usr/bin/env python3
"""End-to-end rehearsal on a throwaway wallet home: every money path + the injection battery.

    python3 scripts/rehearse.py [--keep]

It creates `<HERMES_HOME>/cache/scratch/rehearse-*`, bootstraps a wallet with a throwaway
passphrase, and drives the **installed** plugin the way the agent and the operator do — through the
real tokens and the real RPC, not by importing daemon internals. Everything spends on
`sim-mainnet`, which moves play money only.

Prints one PASS/FAIL line per check and exits non-zero if anything failed. `--keep` leaves the home
behind for inspection.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PLUGIN = Path(os.environ.get("WALLET_PLUGIN") or Path(__file__).resolve().parents[1])
HERMES = Path(os.environ.get("HERMES_BIN") or "hermes")
PASS = "rehearse-passphrase-123"
# `wallet demo` bootstraps a throwaway home with this PIN; every approval here is an operator one
PIN = os.environ.get("WALLETD_PIN", "123456")

results: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((bool(ok), name, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))
    return bool(ok)


def run(args: list[str], home: Path, stdin: str = "", timeout: int = 120) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["AGENT_WALLET_HOME"] = str(home)
    return subprocess.run([str(HERMES), "wallet", *args], env=env, input=stdin,
                          capture_output=True, text=True, timeout=timeout)


def jrun(args: list[str], home: Path, stdin: str = "") -> tuple[dict, int]:
    out = run([*args, "--json"], home, stdin)
    try:
        return json.loads(out.stdout), out.returncode
    except json.JSONDecodeError:
        return {"_stdout": out.stdout[-400:], "_stderr": out.stderr[-400:]}, out.returncode


def main() -> int:
    keep = "--keep" in sys.argv
    scratch = Path(os.environ.get("HERMES_HOME", str(Path.home()))) / "cache" / "scratch"
    # a fresh Hermes home has no cache/scratch yet, and mkdtemp does not create its parent
    scratch.mkdir(parents=True, exist_ok=True)
    home = Path(tempfile.mkdtemp(prefix="rehearse-", dir=scratch))

    print(f"home: {home}")
    print("\n== bootstrap")
    out = run(["demo", "--no-install", "--amount", "0.001", "--passphrase-stdin"], home, PASS + "\n",
              timeout=240)
    check("demo bootstraps a fresh home and pays on the simulator",
          "sent" in out.stdout and "receipt" in out.stdout, out.stdout.strip().splitlines()[-1][:80]
          if out.stdout else out.stderr[-100:])

    status, _ = jrun(["status"], home)
    wallet = status.get("status", status)
    check("status reports the wallet as initialised and unlocked",
          wallet.get("initialized") and not wallet.get("locked"),
          f"v{wallet.get('version')} coins={wallet.get('coins') or wallet.get('assets')}")

    print("\n== receiving")
    order, _ = jrun(["receive", "--kind", "sim-mainnet", "--label", "order 1",
                     "--expected", "0.001"], home)
    order_id = order.get("order_id", "")
    check("receive opens an order with an id", bool(order_id), order_id)
    orders, _ = jrun(["orders"], home)
    check("orders lists it with a status",
          any(o.get("order_id") == order_id for o in orders.get("orders", [])) or
          any(o.get("label") == "order 1" for o in orders.get("orders", [])),
          f"{len(orders.get('orders', []))} order(s)")
    closed, _ = jrun(["orders"], home)
    print("\n== paying: request, deny, approve")
    addr = (order.get("address") or "").strip()
    # `request` is not a CLI verb: filing a payment is the agent's job, through its tools
    sys.path.insert(0, str(PLUGIN))
    os.environ["AGENT_WALLET_HOME"] = str(home)
    from wallet import client, tools  # noqa: E402  (after AGENT_WALLET_HOME is pinned)

    filed = json.loads(tools.wallet_request_payment(kind="sim-mainnet", asset="XMR", amount="0.001",
                                                    payee=addr, why="rehearsal"))
    rid, aid = filed["request"]["request_id"], filed.get("approval_id", "")
    check("agent files a payment that waits for a human",
          filed["request"]["status"] == "pending" and bool(aid), filed["decision"]["code"])

    denied = client.call("deny", {"request_id": rid, "reason": "rehearsal"}, scope=client.OPERATOR)
    check("operator can deny with just the request id", denied["request"]["status"] == "denied")

    filed = json.loads(tools.wallet_request_payment(kind="sim-mainnet", asset="XMR", amount="0.001",
                                                    payee=addr, why="rehearsal"))
    approved = client.call("approve", {"request_id": filed["request"]["request_id"],
                                       "approval_id": filed["approval_id"],
                                       "confirm_amount": "0.001", "passphrase": PASS, "pin": PIN},
                           scope=client.OPERATOR)
    check("operator approves with the passphrase and it sends",
          approved["request"]["status"] == "sent" and bool(approved.get("tx_hash")),
          str(approved.get("tx_hash"))[:18])

    print("\n== sandbox: fund, spend, sweep")
    made = client.call("sandbox_create", {"kind": "sim-mainnet", "asset": "XMR", "budget": "0.004",
                                          "label": "rehearsal", "passphrase": PASS},
                       scope=client.OPERATOR)
    sbx = made["sandbox"]["sandbox_id"]
    funding = made["funding"]
    client.call("approve", {"request_id": funding["request"]["request_id"],
                            "approval_id": funding["approval_id"], "confirm_amount": "0.004",
                            "passphrase": PASS, "pin": PIN}, scope=client.OPERATOR)
    live = [s for s in client.call("sandboxes_list")["sandboxes"] if s["sandbox_id"] == sbx][0]
    check("a funded sandbox reports its live balance",
          abs(float(live.get("live_balance") or 0) - 0.004) < 1e-9,
          f"{live.get('status')} live={live.get('live_balance')}")

    spend = json.loads(tools.wallet_request_payment(kind="sim-mainnet", asset="XMR", amount="0.001",
                                                    payee=addr, sandbox_id=sbx,
                                                    why="rehearsal spend"))
    check("an unblessed destination inside a sandbox escalates to the human",
          not spend.get("auto") and spend["decision"]["code"] == "needs_operator",
          spend["decision"]["code"])

    client.call("sandbox_revoke", {"sandbox_id": sbx, "passphrase": PASS}, scope=client.OPERATOR)
    revoked = [s for s in client.call("sandboxes_list")["sandboxes"] if s["sandbox_id"] == sbx][0]
    check("revoke sweeps the remainder home and closes the budget",
          revoked["status"] == "revoked" and revoked.get("remaining") == "0",
          f"swept={revoked.get('was_destroyed_with') or ''} remaining={revoked.get('remaining')}")

    print("\n== receipts, backup, readiness")
    verify = client.call("ledger_verify")
    check("the receipt chain still verifies", verify.get("ok") is True, f"{verify.get('entries')} entries")

    archive = home / "backup.json"
    out = run(["backup", "--out", str(archive), "--passphrase-stdin"], home, PASS + "\n")
    check("backup writes an encrypted archive", archive.exists() and archive.stat().st_size > 0,
          f"{archive.stat().st_size if archive.exists() else 0} bytes")

    ready = json.loads(run(["ready", "--amount", "0.001", "--json"], home).stdout or "{}")
    check("ready answers GO/NO_GO with reasons", ready.get("verdict") in ("go", "no_go"),
          f"{ready.get('verdict')} blocking={ready.get('blocking')}")

    print("\n== injection battery (live daemon, agent token)")
    attacker = "48DbP8S4bAExJqCZ8vU97TPokB5FqedqLsSJtnmWXZMKiqDbP8S4bAExJqCZ8vU97TPokB5FqedqLsSJtnmWXZMKiqDbP8S"
    prop = json.loads(tools.wallet_request_sandbox(kind="sim-mainnet", asset="XMR", budget="0.002",
                                                   label="attack", allowed_payees=[attacker]))
    check("an agent cannot name the payees of a budget it asks for",
          prop["sandbox"].get("allowed_payees") == [],
          f"ignored={prop.get('ignored')}")

    tools.wallet_contact_save(name="Mum", kind="sim-mainnet", address=attacker)
    prop2 = json.loads(tools.wallet_request_sandbox(kind="sim-mainnet", asset="XMR", budget="0.002",
                                                    label="attack2"))
    sbx2 = prop2["sandbox"]["sandbox_id"]
    client.call("approve", {"request_id": prop2["funding"]["request"]["request_id"],
                            "approval_id": prop2["funding"]["approval_id"],
                            "confirm_amount": "0.002", "passphrase": PASS, "pin": PIN},
                           scope=client.OPERATOR)
    attack = json.loads(tools.wallet_request_payment(kind="sim-mainnet", asset="XMR",
                                                     amount="0.001", payee=attacker,
                                                     sandbox_id=sbx2, why="rent"))
    check("a contact saved by the agent is not permission to pay it",
          not attack.get("auto") and attack["decision"]["code"] == "needs_operator",
          attack["decision"]["code"])

    # With the PIN gate on, the PIN is the boundary — the long passphrase is not consulted at all,
    # so the honest attack here is a wrong PIN (and then confirming the request is still pending).
    vs = None
    try:
        client.call("approve", {"request_id": attack["request"]["request_id"],
                                "approval_id": attack.get("approval_id", ""),
                                "confirm_amount": "0.001", "passphrase": PASS, "pin": "999999"},
                    scope=client.OPERATOR)
    except Exception as exc:  # noqa: BLE001
        vs = str(exc)
    still_pending = next((row for row in client.call("requests_list").get("requests", [])
                          if row.get("request_id") == attack["request"]["request_id"]), {})
    check("a wrong PIN cannot approve anything, and the request stays pending",
          bool(vs) and "pin" in str(vs).lower() and still_pending.get("status") == "pending",
          f"{str(vs)[:50]} status={still_pending.get('status')}")

    try:
        client.call("sandbox_sweep", {"sandbox_id": sbx2}, scope=client.AGENT)
        swept_from_agent = "allowed"
    except Exception as exc:  # noqa: BLE001
        swept_from_agent = str(exc)
    check("the agent cannot sweep a budget home", "operator" in swept_from_agent.lower(),
          swept_from_agent[:60])

    print()
    failed = [r for r in results if not r[0]]
    print(f"rehearse: {len(results) - len(failed)}/{len(results)} passed")
    if keep:
        print(f"kept: {home}")
    else:
        client.call("sandbox_revoke", {"sandbox_id": sbx2, "passphrase": PASS}, scope=client.OPERATOR)
        run(["down"], home)
        shutil.rmtree(home, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
