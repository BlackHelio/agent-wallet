"""Monero adapter over `monero-wallet-rpc`.

The wallet daemon holds the spend key; we hold the wallet's *password* in the keystore and talk to
`monero-wallet-rpc` on loopback. Two-step by design: `transfer` with `do_not_relay` builds and signs
a transaction without publishing it, an operator approves, then `relay_tx` publishes it. The tx key
comes back with the plan and goes into the receipt, so the recipient can verify the payment against
the txid without either side sharing a view key.
"""

from __future__ import annotations

import hashlib
import time
from decimal import Decimal, InvalidOperation
from typing import Any, Dict

from typing import Any, Dict, List

from .base import ChainAdapter, ChainError, JsonRpc, TxPlan

ATOMIC = Decimal(10) ** 12          # 1 XMR = 1e12 atomic units
MAX_FEE_WARN = Decimal("0.001")     # ~1e9 atomic; anything above this is shown to the operator


def to_atomic(amount: Decimal) -> int:
    try:
        atomic = (Decimal(amount) * ATOMIC).to_integral_value()
    except (InvalidOperation, ValueError) as exc:
        raise ChainError("bad_amount", "amount is not a number") from exc
    if atomic <= 0:
        raise ChainError("bad_amount", "amount rounds to zero")
    return int(atomic)


def from_atomic(atomic: Any) -> Decimal:
    return (Decimal(int(atomic)) / ATOMIC).quantize(Decimal("0.000000000001"))


class XmrAdapter(ChainAdapter):
    def __init__(self, kind: str, rpc_url: str, wallet_filename: str, wallet_password: str,
                 account_index: int = 0, timeout: float = 60.0, provider: str = "",
                 simulate: bool = False, rpc_user: str = "", rpc_password: str = ""):
        self.kind = kind
        self.asset = "XMR"
        self.rpc = JsonRpc(rpc_url, timeout=timeout, user=rpc_user, password=rpc_password)
        self.wallet_filename = wallet_filename
        self.wallet_password = wallet_password
        self.account_index = account_index
        self.provider = provider
        self.simulate = simulate

    # ---------- lifecycle ----------

    def _open(self) -> None:
        """Open the wallet if this RPC instance has none open yet."""
        try:
            self.rpc.call("get_version")
        except ChainError:
            raise
        try:
            self.rpc.call("get_address", {"account_index": self.account_index})
            return
        except ChainError:
            pass
        self.rpc.call("open_wallet", {"filename": self.wallet_filename,
                                      "password": self.wallet_password})

    def wallet_exists(self) -> bool:
        """True when this RPC already has a wallet open, or can open the configured file.

        monero-wallet-rpc keeps one wallet open at a time and answers `open_wallet` with an error
        when one is already open, so "a wallet is open" has to count as "ours exists" — otherwise a
        second, empty wallet file gets created next to the real one.
        """
        try:
            self.rpc.call("get_version")
        except ChainError:
            return False
        try:
            self.rpc.call("get_address", {"account_index": self.account_index})
            return True                     # a wallet is already open
        except ChainError:
            pass
        try:
            self.rpc.call("open_wallet", {"filename": self.wallet_filename,
                                          "password": self.wallet_password})
            return True
        except ChainError:
            return False

    def close_wallet(self) -> None:
        try:
            self.rpc.call("close_wallet")
        except ChainError:
            pass

    def create_wallet(self) -> Dict[str, Any]:
        self.rpc.call("create_wallet", {"filename": self.wallet_filename,
                                        "password": self.wallet_password,
                                        "language": "English"})
        # create_wallet does not return the address; read it back from the freshly created wallet.
        self.rpc.call("open_wallet", {"filename": self.wallet_filename,
                                      "password": self.wallet_password})
        return {"created": True, "address": self.address()}

    def restore_wallet(self, seed: str, restore_height: int = 0) -> Dict[str, Any]:
        if not seed.strip():
            raise ChainError("bad_seed", "no recovery words supplied")
        self.rpc.call("restore_wallet", {"filename": self.wallet_filename,
                                         "password": self.wallet_password,
                                         "seed": seed.strip(),
                                         "restore_height": int(restore_height),
                                         "language": "English"})
        self.rpc.call("open_wallet", {"filename": self.wallet_filename,
                                      "password": self.wallet_password})
        return {"restored": True, "address": self.address()}

    def available(self) -> bool:
        try:
            self._open()
            return True
        except ChainError:
            return False

    def status(self) -> Dict[str, Any]:
        from . import nodes as node_registry

        def _label(url: str) -> str:
            for node in node_registry.candidates(self.kind):
                if node["url"].rstrip("/") in url or url.rstrip("/").endswith(node["url"].rstrip("/")):
                    return node["label"]
            return "custom"

        try:
            info = self.rpc.call("get_height")
            return {"kind": self.kind, "available": True, "height": info.get("height"),
                    "rpc": self.rpc.url,
                    "provider": self.provider or _label(self.rpc.url)}
        except ChainError as exc:
            return {"kind": self.kind, "available": False, "error": exc.message}

    # ---------- reads ----------

    def address(self) -> str:
        self._open()
        return str(self.rpc.call("get_address", {"account_index": self.account_index}).get("address", ""))

    def balance(self) -> Decimal:
        self._open()
        result = self.rpc.call("get_balance", {"account_index": self.account_index})
        return from_atomic(result.get("unlocked_balance", result.get("balance", 0)))

    # ---------- transfer ----------

    def plan_transfer(self, amount: Decimal, payee: str, account_index: int = 0,
                      asset: str = "") -> TxPlan:
        self._open()
        if self.simulate:
            # Same shape as a real plan, but nothing is built or signed: the demo path.
            digest = hashlib.sha256(f"{payee}|{amount}|{account_index}|{time.time()}".encode()).hexdigest()
            return TxPlan(kind=self.kind, asset="XMR", amount=amount, payee=payee,
                          fee=Decimal("0.00003"),
                          summary=f"[SIMULATED] {amount} XMR → {payee[:12]}…",
                          raw={"simulated": True, "tx_hash": "sim" + digest[:61],
                               "tx_key": digest[:64], "atomic": to_atomic(amount)})
        atomic = to_atomic(amount)
        result = self.rpc.call("transfer", {
            "destinations": [{"amount": atomic, "address": payee}],
            "account_index": account_index,
            "priority": 1,
            "do_not_relay": True,
            "get_tx_key": True,
            "ring_size": 16,
        })
        tx_blob = result.get("tx_blob") or ""
        if not tx_blob:
            raise ChainError("plan_failed", "wallet returned no transaction blob")
        fee = from_atomic(result.get("fee", 0))
        tx_hash = str(result.get("tx_hash", ""))
        tx_key = str(result.get("tx_key", ""))
        return TxPlan(
            kind=self.kind, asset="XMR", amount=amount, payee=payee, fee=fee,
            summary=f"{amount} XMR → {payee[:12]}… (fee {fee} XMR)",
            raw={"tx_blob": tx_blob, "tx_hash": tx_hash, "tx_key": tx_key, "atomic": atomic},
        )

    def broadcast(self, plan: TxPlan) -> Dict[str, Any]:
        self._open()
        if plan.raw.get("simulated"):
            return {"tx_hash": plan.raw.get("tx_hash", ""), "tx_key": plan.raw.get("tx_key", ""),
                    "fee": str(plan.fee), "confirmations": 0, "simulated": True}
        blob = plan.raw.get("tx_blob", "")
        if not blob:
            raise ChainError("plan_failed", "no transaction to broadcast")
        self.rpc.call("relay_tx", {"hex": blob})
        tx_hash = plan.raw.get("tx_hash", "")
        # Read the transfer back from the wallet: never trust a broadcast claim.
        confirmed = {}
        try:
            confirmed = self.rpc.call("get_transfer_by_txid", {"txid": tx_hash})
        except ChainError:
            confirmed = {}
        transfer = (confirmed.get("transfer") or {}) if isinstance(confirmed, dict) else {}
        return {
            "tx_hash": tx_hash or transfer.get("txid", ""),
            "tx_key": plan.raw.get("tx_key", ""),
            "fee": str(plan.fee),
            "height": transfer.get("height"),
            "confirmations": transfer.get("confirmations", 0),
        }

    # ---------- sub-accounts (sandboxes) ----------

    supports_accounts = True
    explorer_tx = ""   # Monero has no public explorer that would not deanonymise spenders

    def create_account(self, label: str) -> Dict[str, Any]:
        self._open()
        result = self.rpc.call("create_account", {"label": label[:40]})
        return {"account_index": int(result.get("account_index", 0)),
                "address": str(result.get("address", ""))}

    def payment_proof(self, txid: str, tx_key: str, address: str) -> Dict[str, Any]:
        """Prove a payment with its tx key — the only way to verify XMR the recipient can also see.

        Monero hides amounts and parties from everyone else; the sender's tx key is what makes a
        payment checkable by the party who was paid.
        """
        self._open()
        result = self.rpc.call("check_tx_key", {"txid": txid, "tx_key": tx_key, "address": address})
        return {"received": bool(result.get("received")),
                "amount": str(from_atomic(result.get("amount", 0))),
                "confirmations": int(result.get("confirmations", 0)),
                "in_pool": bool(result.get("in_pool", False))}

    def accounts(self) -> List[Dict[str, Any]]:
        self._open()
        result = self.rpc.call("get_accounts")
        rows = []
        for account in result.get("subaddress_accounts", []) or []:
            rows.append({
                "account_index": int(account.get("account_index", 0)),
                "address": account.get("base_address", ""),
                "balance": str(from_atomic(account.get("balance", 0))),
                "unlocked": str(from_atomic(account.get("unlocked_balance", 0))),
                "label": account.get("label", ""),
            })
        return rows

    def total_balance_at(self, account_index: int) -> Decimal:
        """Balance including outputs still confirming.

        `balance_at` reports the unlocked balance, which is the right number to *spend* and the wrong
        one to decide whether an account is empty: change from a payment can be confirming while the
        unlocked balance reads zero.
        """
        self._open()
        result = self.rpc.call("get_balance", {"account_index": account_index})
        return from_atomic(result.get("balance", 0))

    def balance_at(self, account_index: int) -> Decimal:
        self._open()
        result = self.rpc.call("get_balance", {"account_index": account_index})
        return from_atomic(result.get("unlocked_balance", result.get("balance", 0)))

    def mnemonic(self) -> str:
        """The wallet's 25-word recovery seed. Only an operator route may call this."""
        self._open()
        result = self.rpc.call("query_key", {"key_type": "mnemonic"})
        return str(result.get("key", ""))

    def verify_payment(self, txid: str, tx_key: str, address: str) -> bool:
        """Third-party proof: no view key needed when the payer supplies the tx key."""
        try:
            result = self.rpc.call("check_tx_key", {"txid": txid, "tx_key": tx_key, "address": address})
        except ChainError:
            return False
        return bool(result.get("received"))
