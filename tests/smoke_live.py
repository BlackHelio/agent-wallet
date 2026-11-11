"""Live smoke test against the real daemon: agent requests, operator approves, ledger verifies."""
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from walletd.rpc import call

HOME = Path(os.environ.get("WALLETD_SMOKE_HOME",
                           str(Path.home() / ".cache" / "agent-wallet-smoke")))
SOCK = HOME / "walletd.sock"
TOKENS = HOME / "tokens"
PASS = "smoke-passphrase-1"
ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def xmr(offset: int) -> str:
    return "57" + ((ALPHABET[offset:] + ALPHABET[:offset]) * 3)[:93]


agent = (TOKENS / "agent.token").read_text().strip()
operator = (TOKENS / "operator.token").read_text().strip()
addr = xmr(3)

print("== 1. agent asks for status")
print(json.dumps(call(str(SOCK), "status", {}, agent), indent=2))

print("== 2. agent tries to approve (must be forbidden)")
try:
    call(str(SOCK), "approve", {"request_id": "x", "approval_id": "y"}, agent)
    raise SystemExit("FAIL: agent approved something")
except Exception as exc:
    print("   refused:", exc)

print("== 3. agent requests 0.01 XMR while the wallet is locked (must be denied)")
print(json.dumps(call(str(SOCK), "request_tx", {"kind": "monero-stagenet", "asset": "XMR",
                                                "amount": "0.01", "payee": addr,
                                                "why": "smoke test"}, agent)["decision"], indent=2))

print("== 4. operator unlocks")
print(json.dumps(call(str(SOCK), "unlock", {"passphrase": PASS}, operator), indent=2))

print("== 5. agent requests 0.01 XMR -> pending approval")
req = call(str(SOCK), "request_tx", {"kind": "monero-stagenet", "asset": "XMR",
                                    "amount": "0.01", "payee": addr,
                                    "memo": "api call", "why": "smoke test"}, agent)
print(json.dumps({k: req[k] for k in ("request", "decision", "approval_id")}, indent=2))

print("== 6. agent tries to approve its own request (must be forbidden)")
try:
    call(str(SOCK), "approve", {"request_id": req["request"]["request_id"],
                                "approval_id": req["approval_id"]}, agent)
    raise SystemExit("FAIL: agent self-approved")
except Exception as exc:
    print("   refused:", exc)

print("== 7. operator approves")
approved = call(str(SOCK), "approve", {"request_id": req["request"]["request_id"],
                                       "approval_id": req["approval_id"]}, operator)
print("   status:", approved["request"]["status"])

print("== 8. agent proposes a mandate; it must stay inert until the operator activates it")
proposal = call(str(SOCK), "mandates_propose", {"kind": "monero-stagenet", "asset": "XMR",
                                               "payee": addr, "per_tx_cap": "0.05",
                                               "period_cap": "0.1", "absolute_cap": "0.3",
                                               "period_hours": 24, "label": "smoke vendor"}, agent)
print("   mandate status:", proposal["mandate"]["status"])
auto = call(str(SOCK), "request_tx", {"kind": "monero-stagenet", "asset": "XMR",
                                     "amount": "0.02", "payee": addr}, agent)
print("   auto-paid?", auto.get("auto", False), "| needs approval?", "approval_id" in auto)

print("== 9. operator activates the mandate -> next request auto-approves")
call(str(SOCK), "mandates_activate", {"mandate_id": proposal["mandate"]["mandate_id"]}, operator)
auto = call(str(SOCK), "request_tx", {"kind": "monero-stagenet", "asset": "XMR",
                                      "amount": "0.02", "payee": addr}, agent)
print("   auto-paid?", auto.get("auto", False), "| status:", auto["request"]["status"])

print("== 10. agent revokes the mandate -> back to manual approval")
call(str(SOCK), "mandates_revoke", {"mandate_id": proposal["mandate"]["mandate_id"],
                                   "reason": "smoke done"}, agent)
after = call(str(SOCK), "request_tx", {"kind": "monero-stagenet", "asset": "XMR",
                                       "amount": "0.02", "payee": addr}, agent)
print("   auto-paid?", after.get("auto", False), "| needs approval?", "approval_id" in after)

print("== 11. ledger verify (in-process) and offline via CLI")
print("   ", call(str(SOCK), "ledger_verify", {}, agent))
out = subprocess.run([sys.executable, "-m", "walletd", "--home", str(HOME), "verify"],
                     capture_output=True, text=True, cwd=str(Path(__file__).resolve().parents[1]))
print("   ", out.stdout.strip() or out.stderr.strip())

print("== 12. tamper the ledger and verify again (must fail)")
ledger = HOME / "ledger.jsonl"
lines = ledger.read_text().splitlines()
entry = json.loads(lines[-1])
entry["details"]["amount"] = "999"
lines[-1] = json.dumps(entry, sort_keys=True, separators=(",", ":"))
ledger.write_text("\n".join(lines) + "\n")
out = subprocess.run([sys.executable, "-m", "walletd", "--home", str(HOME), "verify"],
                     capture_output=True, text=True, cwd=str(Path(__file__).resolve().parents[1]))
print("   exit:", out.returncode, "|", (out.stdout or out.stderr).strip()[:160])
