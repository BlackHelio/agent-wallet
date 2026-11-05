"""Chain adapter interface.

One contract for every chain: read state, plan a transfer, broadcast it after approval, and hand
back whatever the receipt needs to prove the payment. Adapters never touch the keystore.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from .. import httpdigest
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, Optional


class ChainError(Exception):
    """A chain refused or failed. `code` is stable and goes into the receipt."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class TxPlan:
    """Everything needed to broadcast later. Never leaves the signing process."""
    kind: str
    asset: str
    amount: Decimal
    payee: str
    fee: Decimal = Decimal("0")
    raw: Dict[str, Any] = field(default_factory=dict)
    summary: str = ""

    def public(self) -> Dict[str, Any]:
        """The part an operator may see (no broadcast material)."""
        return {"kind": self.kind, "asset": self.asset, "amount": str(self.amount),
                "payee": self.payee, "fee": str(self.fee), "summary": self.summary}


class ChainAdapter:
    kind = "none"
    asset = "?"
    supports_accounts = False          # true when the chain can hold isolated sub-accounts
    explorer_tx = ""                   # "{tx}" template; empty = no public explorer by design

    def available(self) -> bool:
        return False

    def status(self) -> Dict[str, Any]:
        return {"kind": self.kind, "available": False}

    def address(self) -> str:
        raise ChainError("unsupported", f"{self.kind} has no address")

    def balance(self) -> Decimal:
        raise ChainError("unsupported", f"{self.kind} has no balance")

    def plan_transfer(self, amount: Decimal, payee: str, account_index: int = 0) -> TxPlan:
        raise ChainError("unsupported", f"{self.kind} cannot transfer")

    def create_account(self, label: str) -> Dict[str, Any]:
        raise ChainError("unsupported", f"{self.kind} cannot create sub-accounts")

    def accounts(self) -> list:
        raise ChainError("unsupported", f"{self.kind} has no accounts")

    def broadcast(self, plan: TxPlan) -> Dict[str, Any]:
        raise ChainError("unsupported", f"{self.kind} cannot broadcast")


class JsonRpc:
    """Minimal JSON-RPC client (stdlib only, bounded timeout)."""

    def __init__(self, url: str, timeout: float = 30.0, user: str = "", password: str = ""):
        self.url = url
        self.timeout = timeout
        self.user = user or ""
        self.password = password or ""

    def call(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = json.dumps({"jsonrpc": "2.0", "id": "0", "method": method,
                              "params": params or {}}).encode("utf-8")
        try:
            status, raw = httpdigest.post(self.url, payload, self.user, self.password,
                                          timeout=self.timeout)
        except OSError as exc:
            raise ChainError("rpc_unreachable", f"{self.url}: {exc}") from exc
        if status == 401:
            raise ChainError("rpc_unauthorized", f"{self.url}: the signer rejected our credentials")
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ChainError("rpc_error", f"{self.url}: HTTP {status}, not JSON") from exc
        if "error" in body:
            error = body["error"]
            message = str(error.get("message") or error)
            lowered = message.lower()
            if "not enough money" in lowered or "not enough unlocked" in lowered:
                raise ChainError("insufficient_funds", message[:200])
            if "wrong address" in lowered or "invalid address" in lowered:
                raise ChainError("invalid_payee", message[:200])
            if "no connection to daemon" in lowered or "daemon is busy" in lowered:
                raise ChainError("daemon_unavailable", message[:200])
            raise ChainError("rpc_error", message[:200])
        return body.get("result") or {}
