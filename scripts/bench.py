"""Where does the wallet's time actually go? Numbers first, then optimise.

Run from the repo with the daemon up and the wallet unlocked:

    PYTHONPATH=<wallet home>/pylibs HERMES_HOME=<hermes home> \
      python3 scripts/bench.py

It only reads: no payment is filed except through the in-memory test fixture.
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from decimal import Decimal  # noqa: E402
from pathlib import Path  # noqa: E402


def timed(label, fn, n=5, warmup=1):
    for _ in range(warmup):
        fn()
    samples = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1000)
    print(f"  {label:<44} median {statistics.median(samples):8.2f} ms   "
          f"min {min(samples):7.2f}  max {max(samples):7.2f}")
    return statistics.median(samples)


print("== RPC round trips (live daemon)")
from wallet import client  # noqa: E402
timed("status", lambda: client.call("status"), n=20)
timed("balances (10s cache warm)", lambda: client.call("balances"), n=10)
timed("chain_status", lambda: client.call("chain_status"), n=10)
timed("requests_list", lambda: client.call("requests_list", scope=client.AGENT), n=10)
timed("ledger_tail(40)", lambda: client.call("ledger_tail", {"limit": 40}), n=10)
timed("spend_totals(30d)", lambda: client.call("spend_totals", {"since": "30d"}), n=5)

print("\n== keystore KDF")
from walletd import keystore  # noqa: E402
home = Path(__import__("os").environ.get("AGENT_WALLET_HOME") or
            (Path(__import__("os").environ.get("HERMES_HOME", "~")) / "wallet")).expanduser()
pw_file = Path(__import__("os").environ.get("WALLET_BENCH_PASSPHRASE_FILE",
                                          str(home.parent / "wallet-dev-passphrase")))
env = keystore.load(home / "keystore.json")
pw = pw_file.read_text().strip()
timed("scrypt open_sealed (n=2^15)", lambda: keystore.open_sealed(env, pw), n=3)

print("\n== ledger arithmetic at scale")
from walletd.ledger import Ledger  # noqa: E402
import tempfile  # noqa: E402
for n in (1_000, 10_000, 40_000):
    tmp = Path(tempfile.mkdtemp())
    # write the file directly: building 40k receipts through the signed ledger is a one-off
    # cost that has nothing to do with the read path being measured
    lines = []
    prev, seq = "0" * 64, 0
    for i in range(n):
        seq += 1
        lines.append(json.dumps({"seq": seq, "prev": prev, "ts": "2026-11-06T00:00:00Z",
                                 "kind": "spend_request", "decision": "approved",
                                 "rule": "manual_approval",
                                 "details": {"asset": "XMR", "kind": "monero-mainnet",
                                             "amount": "0.001", "payee": "49" + "x" * 90},
                                 "sig": "0" * 64}))
    (tmp / "ledger.jsonl").write_text("\n".join(lines) + "\n")
    led = Ledger(tmp / "ledger.jsonl", b"\0" * 32)
    ms = timed(f"totals() over {n:,} receipts", lambda: led.totals("XMR"), n=3)

print("\n== signing (the pure-python dependency path)")
try:
    import embit  # noqa: F401
    from embit import bip32, script  # noqa: E402
    from embit.transaction import Transaction, TransactionInput, TransactionOutput  # noqa: E402
    seed = bytes(range(32))
    root = bip32.HDKey.from_seed(seed)
    key = root.derive("m/84h/0h/0h/0/0")
    tx = Transaction(version=2, inputs=[TransactionInput(b"\x11" * 32, 0)],
                     outputs=[TransactionOutput(50_000, script.p2wpkh(key.derive([0, 0]).key))])
    timed("embit sign_input (BIP84)", lambda: tx.sign_with(key, sighash=1), n=10)
except Exception as exc:  # noqa: BLE001
    print(f"  embit unavailable: {type(exc).__name__}: {exc}")

try:
    from eth_account import Account  # noqa: E402
    acct = Account.from_key(bytes.fromhex("11" * 32))
    tx = {"to": "0x" + "22" * 20, "value": 10**15, "gas": 21000,
          "maxFeePerGas": 10**9, "maxPriorityFeePerGas": 10**8, "nonce": 0, "chainId": 8453,
          "type": 2}
    timed("eth-account sign_transaction (EIP-1559)", lambda: acct.sign_transaction(tx), n=10)
except Exception as exc:  # noqa: BLE001
    print(f"  eth-account unavailable: {type(exc).__name__}: {exc}")

print("\n== pane routes (in-process, what the app polls)")
import importlib.util  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
spec = importlib.util.spec_from_file_location(
    "wallet_plugin_api", str(HERE / "dashboard" / "plugin_api.py"))
mod = importlib.util.module_from_spec(spec)
sys.modules["wallet_plugin_api"] = mod
spec.loader.exec_module(mod)
app = FastAPI()
app.include_router(mod.router, prefix="/api/plugins/agent-wallet")
cli = TestClient(app)
for path in ("/status", "/balances", "/requests", "/sandboxes", "/incoming"):
    timed(f"GET {path}", lambda p=path: cli.get("/api/plugins/agent-wallet" + p), n=10)
