"""Mimic the dashboard's own plugin-API mount, exactly as web_server_dashboard does it.

If this passes, the route the Desktop pane calls exists in the real server after a restart.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

PLUGIN = Path(os.environ.get("AGENT_WALLET_PLUGIN") or Path(__file__).resolve().parents[1])
API = PLUGIN / "dashboard" / "plugin_api.py"

module_name = f"hermes_dashboard_plugin_{PLUGIN.name}"
spec = importlib.util.spec_from_file_location(module_name, API)
mod = importlib.util.module_from_spec(spec)
sys.modules[module_name] = mod
spec.loader.exec_module(mod)

app = FastAPI()
app.include_router(mod.router, prefix=f"/api/plugins/{PLUGIN.name}")
client = TestClient(app)

r = client.get(f"/api/plugins/{PLUGIN.name}/status")
print("status", r.status_code, r.json())
r = client.get(f"/api/plugins/{PLUGIN.name}/config")
print("config", r.status_code, r.json()["settings"]["per_tx_cap"])
r = client.get(f"/api/plugins/{PLUGIN.name}/balances")
print("balances", r.status_code, [(a["asset"], a["balance"], a["available"]) for a in r.json()["accounts"]])

# The human-approval loop through the route the pane calls. Before ApproveBody declared
# `passphrase`, Pydantic silently dropped it and every GUI approval returned 400
# passphrase_required — the flagship flow only worked from a terminal.
import os
print("--- approve with passphrase (the GUI path) ---")
PASS_FILE = Path(os.environ.get("WALLET_PASSFILE") or Path.home() / ".hermes" / "wallet-dev-passphrase")
passphrase = PASS_FILE.read_text().strip() if PASS_FILE.exists() else ""
sys.path.insert(0, str(PLUGIN))
from wallet import client as wallet_client  # noqa: E402

created = wallet_client.call("request_tx", {
    "kind": "sim-mainnet", "asset": "XMR", "amount": "0.001",
    "payee": "44AFFq5kSiGBoZ4NMDwYtN18obc8AemS33DBLWs3H7otXft3XjrpDtQGv7SqSsaBYBb98uNbr2VBBEt7f2wfn3RVGQBEP3A",
    "why": "smoke: approve through the route"})
request_id = created["request"]["request_id"]
approval_id = created["approval_id"]

r = client.post(f"/api/plugins/{PLUGIN.name}/approve",
                json={"request_id": request_id, "approval_id": approval_id,
                      "confirm_amount": "0.001", "passphrase": passphrase})
print("approve", r.status_code, str(r.json())[:160])
assert r.status_code == 200, f"GUI approve failed: {r.status_code} {r.text[:200]}"

print("--- request through the route (sandbox field must survive Pydantic) ---")
import json as _json
sandboxes = client.get(f"/api/plugins/{PLUGIN.name}/sandboxes").json().get("sandboxes", [])
if sandboxes:
    sbox = next((s for s in sandboxes if s.get("enabled")), sandboxes[0])
    r = client.post(f"/api/plugins/{PLUGIN.name}/request", json={
        "kind": sbox["kind"], "asset": sbox["asset"], "amount": "0.001",
        "payee": sbox["address"], "memo": "smoke: from sandbox", "sandbox_id": sbox["sandbox_id"]})
    print("request", r.status_code, str(r.json())[:120])
    assert r.status_code == 200, r.text[:200]
    made = r.json().get("request", {})
    assert made.get("sandbox_id") == sbox["sandbox_id"], f"sandbox_id was dropped: {made}"

print("--- passphrase prompt toggles through the route the pane calls ---")
passphrase = PASS_FILE.read_text().strip() if PASS_FILE.exists() else ""
off = client.post(f"/api/plugins/{PLUGIN.name}/human-gate",
                  json={"enabled": False, "passphrase": passphrase, "confirm": True})
print("off", off.status_code, str(off.json())[:80])
assert off.status_code == 200, off.text[:200]
on = client.post(f"/api/plugins/{PLUGIN.name}/human-gate", json={"enabled": True, "confirm": True})
print("on ", on.status_code, str(on.json())[:80])
assert on.status_code == 200, on.text[:200]
no_pw = client.post(f"/api/plugins/{PLUGIN.name}/human-gate", json={"enabled": False, "confirm": True})
print("off without passphrase", no_pw.status_code, "(must be 400)")
assert no_pw.status_code == 400, no_pw.text[:200]
