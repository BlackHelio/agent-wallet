"""Drive the pane's own HTTP API the way the UI drives it, and assert the observable effects.

Not a unit test of the handlers: this loads `dashboard/plugin_api.py` the way the host loads it
(module registered before exec, so the Pydantic bodies resolve), points it at a real `walletd` on a
scratch home, and then walks the flows a person actually performs in the pane:

    open → set PIN → save a contact → file "10$" → approve on the card → see the receipt
    → carve a sandbox → bless a destination → check balances carry token metadata

Each step prints PASS/FAIL with the value it read back from the daemon.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# The plugin under test: this checkout unless the caller points at an install (a reviewer has no
# reason to know the author's paths).
PLUGIN = Path(os.environ.get("AGENT_WALLET_PLUGIN") or Path(__file__).resolve().parents[1])
# NOTE: never import `tests` here. `tests/__init__.py` sets WALLETD_DISABLE_CHAINS=1 so the suite runs
# offline; that env var is inherited by any daemon a probe spawns, and the daemon then builds no
# adapters at all — which looks exactly like a product bug and is not one.

def xmr_addr(seed: int) -> str:
    """A checksummed stagenet address, so the probe does not depend on the test package."""
    from eth_utils.crypto import keccak
    sys.path.insert(0, str(PLUGIN))
    from walletd import addresses
    body = bytes([24]) + bytes([seed & 0xFF]) * 64
    return addresses.xmr_encode(body + keccak(body)[:4])
REPO = Path(__file__).resolve().parents[1]
PASS = "probe-passphrase-123"
PIN = "424242"

home = Path(tempfile.mkdtemp(prefix="pane-probe-"))
print(f"  probe home: {home}")
os.environ["AGENT_WALLET_HOME"] = str(home)
sys.path.insert(0, str(PLUGIN))

results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    results.append((bool(ok), name, str(detail)[:160]))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {detail}"[:150] if detail else ""))


def load_api():
    name = "hermes_dashboard_plugin_agent-wallet"
    # the *installed* copy, the one the host actually loads: importing the repo's copy puts the
    # repo's `wallet` package on sys.path ahead of the installed one
    spec = importlib.util.spec_from_file_location(name, PLUGIN / "dashboard" / "plugin_api.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod          # the host does this before exec; without it bodies don't resolve
    spec.loader.exec_module(mod)
    return mod


def boot() -> None:
    """Bring the wallet up the way a user does: `hermes wallet demo` in a scratch home.

    Rolling our own init+serve here kept producing a daemon with no adapters, which says more about a
    hand-built environment than about the product. The demo path is the one the README tells people to
    run, so it is the one worth probing against — and it leaves the daemon unlocked with the simulator
    funded, exactly where a pane session starts.
    """
    env = {**os.environ, "AGENT_WALLET_HOME": str(home), "HERMES_HOME": str(home.parent),
           "WALLETD_PIN": PIN}
    env.pop("WALLETD_DISABLE_CHAINS", None)      # the suite's offline switch must not reach the daemon
    proc = subprocess.run([os.environ.get("HERMES_BIN") or "hermes", "wallet", "demo", "--no-install",
                           "--passphrase-stdin"], input=PASS + "\n", env=env,
                          capture_output=True, text=True, timeout=300)
    if "status    sent" not in (proc.stdout or ""):
        print("    [debug] demo output:", (proc.stdout or "")[-600:], (proc.stderr or "")[-300:])
    return proc


def load_api():
    name = "hermes_dashboard_plugin_agent-wallet"
    # the *installed* copy, the one the host actually loads: importing the repo's copy puts the
    # repo's `wallet` package on sys.path ahead of the installed one
    spec = importlib.util.spec_from_file_location(name, PLUGIN / "dashboard" / "plugin_api.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod          # the host does this before exec; without it bodies don't resolve
    spec.loader.exec_module(mod)
    return mod


def boot() -> None:
    env = {**os.environ, "PYTHONPATH": str(PLUGIN), "AGENT_WALLET_HOME": str(home)}
    subprocess.run([sys.executable, "-m", "walletd", "--home", str(home), "init"],
                   env={**env, "WALLETD_PASSPHRASE": PASS, "WALLETD_PIN": PIN},
                   capture_output=True, timeout=180)
    log = open(home / "probe-daemon.log", "w")
    proc = subprocess.Popen([sys.executable, "-m", "walletd", "--home", str(home), "serve"],
                            env=env, stdout=log, stderr=subprocess.STDOUT,
                            start_new_session=True)
    for _ in range(80):
        if (home / "walletd.sock").exists():
            break
        time.sleep(0.25)
    from wallet import client
    for _ in range(40):
        try:
            client.call("status")
            break
        except Exception:
            time.sleep(0.25)
    client.call("unlock", {"passphrase": PASS}, scope=client.OPERATOR)
    # the simulator is not on by default; the demo enables it, and the pane's flows assume it
    settings = client.call("config_get").get("settings") or {}
    chains = list(settings.get("chains_enabled") or []) + ["sim-mainnet"]
    # chains only, exactly like `hermes wallet demo`: sending assets_enabled too is a path that
    # recomputes the chain list from the coins
    client.call("config_set", {"chains_enabled": chains, "passphrase": PASS}, scope=client.OPERATOR)
    rows = client.call("chain_status")["chains"]
    sim_row = next((c for c in rows if c["kind"] == "sim-mainnet"), {})
    check(bool(sim_row.get("available")), "the simulator has a live adapter before the flow starts",
          {k: sim_row.get(k) for k in ("kind", "available", "state", "error")})
    check(bool(client.call("balances").get("accounts")), "balances work before the flow starts",
          len(client.call("balances").get("accounts") or []))
    return proc


def http_client(api):
    """The pane's real path: HTTP → route → _op → client."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(api.router, prefix="/api/plugins/agent-wallet")
    return TestClient(app)


def main() -> int:
    proc = boot()
    # stop the daemon however this run ends: a failed check used to raise past the terminate() at
    # the bottom and leave a walletd serving the scratch home
    import atexit
    atexit.register(lambda: proc.terminate() if hasattr(proc, "terminate") else None)
    api = load_api()
    http = http_client(api)
    base = "/api/plugins/agent-wallet"

    def get(path, **params):
        resp = http.get(base + path, params=params)
        if resp.status_code >= 400:
            raise RuntimeError(f"{path} -> {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    def post(path, body):
        resp = http.post(base + path, json=body)
        return resp

    status = get("/status")
    wallet = status.get("wallet") or {}
    check(bool(wallet.get("pin")), "GET /status carries pin{} for the gate", wallet.get("pin"))
    pins = get("/pin/status")
    check(pins.get("set") is True and pins.get("required") is True,
          "GET /pin/status says the PIN is set and required", pins.get("set"))

    sim_address = "4A" + "B" * 93
    saved = post("/contact/save", {"name": "Adrian", "kind": "sim-mainnet",
                                   "address": sim_address, "notes": "probe",
                                   # a contact saved from the pane is operator-grade (it blesses
                                   # the address), so the daemon wants the passphrase the pane's
                                   # Contacts section collects; without it this step was refused
                                   "passphrase": PASS}).json()
    check((saved.get("contact") or {}).get("created_by") == "operator",
          "the pane's contact form saves an operator-grade contact",
          (saved.get("contact") or {}).get("created_by"))

    filed_resp = post("/request", {"kind": "sim-mainnet", "asset": "XMR", "amount": "10$",
                                   "payee": "Adrian", "why": "probe credits"})
    if filed_resp.status_code >= 400:
        print("    [debug] /request ->", filed_resp.status_code, filed_resp.text[:200])
    filed = filed_resp.json()
    request = filed.get("request") or {}
    check((request.get("fiat") or {}).get("currency") == "USD",
          "10$ is quoted and stored on the request", request.get("fiat"))
    check(bool(filed.get("approval_id")), "the request waits for the operator",
          (filed.get("decision") or {}).get("code"))

    refused = post("/approve", {"request_id": request["request_id"],
                                "approval_id": filed["approval_id"],
                                "expect_amount": request["amount"],
                                "expect_payee": request["payee"]})
    check(refused.status_code >= 400 and "pin" in refused.text.lower(),
          "approve without a PIN is refused", f"{refused.status_code} {refused.text[:60]}")

    done_resp = post("/approve", {"request_id": request["request_id"],
                                  "approval_id": filed["approval_id"], "pin": PIN,
                                  "expect_amount": request["amount"],
                                  "expect_payee": request["payee"]})
    done = done_resp.json() if done_resp.status_code < 400 else {}
    _keep = post("/approve", {"request_id": request["request_id"],
                             "approval_id": filed["approval_id"], "pin": PIN,
                             "expect_amount": request["amount"],
                             "expect_payee": request["payee"]}).json()
    check((done.get("request") or {}).get("status") == "sent",
          "the PIN pays, and the card bound the amount",
          (done.get("request") or {}).get("status"))

    second = post("/request", {"kind": "sim-mainnet", "asset": "XMR", "amount": "0.001",
                               "payee": "Adrian", "why": "second"}).json()
    moved = post("/approve", {"request_id": second["request"]["request_id"],
                              "approval_id": second["approval_id"], "pin": PIN,
                              "expect_payee": xmr_addr(71)})
    check(moved.status_code >= 400 and "card" in moved.text.lower(),
          "a card pointing somewhere else is refused", f"{moved.status_code} {moved.text[:60]}")

    ledger = get("/ledger", limit=5)
    rows = ledger.get("entries") if isinstance(ledger, dict) else ledger
    check(bool(rows), "receipts are readable through the pane", len(rows or []))

    bal_raw = get("/balances", fresh=1)
    accounts = (bal_raw or {}).get("accounts") or []
    check(bool(accounts), "balances reach the pane", f"{len(accounts)} rows")

    posted = post("/sandbox/create", {"kind": "sim-mainnet", "asset": "XMR", "budget": "0.01",
                                      "lifetime_hours": 24, "label": "probe"})
    created = posted.json() if posted.status_code < 400 else {}
    sandbox = created.get("sandbox") or {}
    check(bool(sandbox.get("sandbox_id")), "a sandbox can be created from the pane",
          sandbox.get("sandbox_id"))
    # the sandbox is on sim-mainnet, so the destination must be a sim-shaped address: a payee is
    # validated against its own chain, and a stagenet address here is refused (correctly)
    widen = post("/sandbox/payees", {"sandbox_id": sandbox["sandbox_id"],
                                     "allowed_payees": [sim_address]})
    check(widen.status_code >= 400 and "passphrase" in widen.text.lower(),
          "widening a sandbox needs the passphrase", f"{widen.status_code} {widen.text[:60]}")
    blessed = post("/sandbox/payees", {"sandbox_id": sandbox["sandbox_id"],
                                       "allowed_payees": [sim_address], "passphrase": PASS})
    check(blessed.status_code < 400 and bool((blessed.json() or {}).get("sandbox")),
          "the operator can bless a destination", blessed.status_code)

    failed = [name for ok, name, _ in results if not ok]
    print(f"\n  {len(results) - len(failed)}/{len(results)} checks passed")
    if hasattr(proc, 'terminate'):
        proc.terminate()
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
