"""Exercise the pane's backend router against a real daemon (no desktop app needed)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from walletd.rpc import ServerThread
from walletd.state import State
from walletd.tokens import TokenStore
import dashboard.plugin_api as api

PASS = "smoke-passphrase-1"
ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def xmr(offset: int) -> str:
    return "48" + ((ALPHABET[offset:] + ALPHABET[:offset]) * 3)[:93]


def main() -> int:
    tmp = tempfile.TemporaryDirectory()
    home = Path(tmp.name)
    os.environ["AGENT_WALLET_HOME"] = str(home)
    state = State(home)
    state.initialize(PASS)
    tokens = TokenStore(home / "tokens")
    tokens.mint_all()
    server = ServerThread(str(home / "walletd.sock"), state, tokens)
    server.__enter__()

    app = FastAPI()
    app.include_router(api.router, prefix="/api/plugins/agent-wallet")
    client = TestClient(app)
    base = "/api/plugins/agent-wallet"

    try:
        print("status     ->", json.dumps(client.get(f"{base}/status").json()["wallet"]["locked"]))
        r = client.post(f"{base}/unlock", json={"passphrase": PASS})
        print("unlock     ->", r.status_code, r.json())
        r = client.post(f"{base}/unlock", json={"passphrase": "wrong"})
        print("bad unlock ->", r.status_code, r.json()["detail"])

        # a request is created by the agent half; here we go through the daemon directly
        from wallet import client as wallet_client
        req = wallet_client.call("request_tx", {"kind": "monero-stagenet", "asset": "XMR",
                                                "amount": "0.01", "payee": xmr(3),
                                                "why": "pane smoke"}, scope="agent")
        pending = client.get(f"{base}/requests").json()["requests"]
        print("requests   ->", [(x["status"], x.get("approval_id", "")[:12]) for x in pending])
        approval_id = pending[0]["approval_id"]
        r = client.post(f"{base}/approve", json={"request_id": req["request"]["request_id"],
                                                 "approval_id": approval_id})
        print("approve    ->", r.status_code, r.json()["request"]["status"])

        r = client.post(f"{base}/verify")
        print("verify     ->", r.status_code, r.json()["ok"])
        r = client.get(f"{base}/config")
        print("config     ->", r.status_code, r.json()["settings"]["per_tx_cap"])
        r = client.post(f"{base}/human-gate", json={"enabled": False, "passphrase": PASS, "confirm": True})
        print("human-gate ->", r.status_code, r.json())
        r = client.post(f"{base}/human-gate", json={"enabled": False, "passphrase": "nope", "confirm": True})
        print("bad gate   ->", r.status_code, r.json()["detail"])
    finally:
        server.__exit__(None, None, None)
        tmp.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
