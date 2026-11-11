"""Bitcoin adapter: native segwit, signed in-process, broadcast through Esplora.

Like the EVM adapter, this needs no external signer: a 32-byte key inside `walletd`'s sealed payload,
signed locally with `embit`, then handed to a public Esplora endpoint. The key never reaches the
Hermes process, and the daemon is the only thing that can build a witness.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from decimal import Decimal
from typing import Any, Dict, List, Optional

from .base import ChainAdapter, ChainError, TxPlan

SATS = Decimal(10) ** 8
DUST = 546
# a p2wpkh spend: 10.5 vB overhead + 68 vB per input + 31 vB per output, rounded up
def _vsize(inputs: int, outputs: int) -> int:
    return 11 + 68 * inputs + 31 * outputs


ESPLORA = {
    "bitcoin-mainnet": "https://blockstream.info/api",
    "bitcoin-testnet": "https://blockstream.info/testnet/api",
}


class BtcAdapter(ChainAdapter):
    supports_accounts = False
    asset = "BTC"

    def __init__(self, kind: str, secret_hex: str, api_url: str = "", timeout: float = 30.0):
        if kind not in ESPLORA:
            raise ChainError("unknown_chain", f"{kind} is not a supported Bitcoin network")
        self.kind = kind
        self.network = "main" if kind.endswith("mainnet") else "test"
        self.secret_hex = secret_hex
        self.api = (api_url or ESPLORA[kind]).rstrip("/")
        if api_url:
            self._apis = [self.api]
        else:
            from .nodes import candidates   # registry order is the preference order
            urls = [c["url"].rstrip("/") for c in candidates(kind) if c.get("url")]
            self._apis = [self.api] + [u for u in urls if u != self.api]
        self.timeout = timeout
        self.explorer_tx = ("https://blockstream.info/tx/{tx}" if self.network == "main"
                            else "https://blockstream.info/testnet/tx/{tx}")
        self._key = None

    # ---------- plumbing ----------

    def _embit(self):
        try:
            from embit import ec, script
            from embit.networks import NETWORKS
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise ChainError("missing_dependency",
                             "embit is not installed (declared python_dependencies)") from exc
        return ec, script, NETWORKS[self.network]

    def _priv(self):
        if self._key is None:
            ec, _, _ = self._embit()
            raw = bytes.fromhex(self.secret_hex)
            if len(raw) != 32:
                raise ChainError("bad_key", "the stored Bitcoin key is not 32 bytes")
            self._key = ec.PrivateKey(raw)
        return self._key

    def _script(self):
        _, script, _ = self._embit()
        return script.p2wpkh(self._priv().get_public_key())

    def _http(self, path: str, method: str = "GET", body: Optional[bytes] = None,
              timeout: Optional[float] = None) -> bytes:
        """GET/POST with failover across the registry's Esplora endpoints.

        One host rate-limiting us (HTTP 429) used to take the whole chain offline; the node registry
        has always listed a second endpoint, it just was never used.
        """
        last: Optional[ChainError] = None
        for api in getattr(self, "_apis", [self.api]):
            request = urllib.request.Request(api + path, method=method, data=body,
                                             headers={"User-Agent": "agent-wallet/btc"})
            # a failover list multiplies the worst case: two hosts at 30s each holds the daemon's
            # mutation lock for a minute and every other call looks hung. Bound each attempt.
            per_attempt = min(float(timeout or self.timeout), 8.0)
            try:
                with urllib.request.urlopen(request, timeout=per_attempt) as response:
                    if api != self.api:
                        self.api = api      # stick with the host that works for later calls
                    return response.read()
            except urllib.error.HTTPError as exc:
                detail = exc.read()[:160].decode("utf-8", "replace")
                last = ChainError("rpc_error", f"{path}: HTTP {exc.code} {detail}")
                if exc.code not in (429, 500, 502, 503, 504):
                    raise last from exc       # a 4xx that is not load-related will not improve
                continue
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last = ChainError("rpc_unreachable", f"{api}: {type(exc).__name__}")
                continue
        raise last or ChainError("rpc_unreachable", "no Esplora endpoint answered")

    # ---------- reads ----------

    def available(self) -> bool:
        height = self._http("/blocks/tip/height", timeout=10).decode().strip()
        return height.isdigit()

    def status(self) -> Dict[str, Any]:
        info: Dict[str, Any] = {"kind": self.kind, "available": False, "provider": self.api}
        try:
            info["available"] = self.available()
            if info["available"]:
                info["address"] = self.address()
                info["height"] = int(self._http("/blocks/tip/height", timeout=10).decode())
        except ChainError as exc:
            info["error"] = exc.message
        return info

    def address(self) -> str:
        _, _, net = self._embit()
        return self._script().address(net)

    def balance(self) -> Decimal:
        stats = json.loads(self._http(f"/address/{self.address()}"))
        chain = stats.get("chain_stats", {})
        pool = stats.get("mempool_stats", {})
        sats = (chain.get("funded_txo_sum", 0) - chain.get("spent_txo_sum", 0)
                + pool.get("funded_txo_sum", 0) - pool.get("spent_txo_sum", 0))
        return Decimal(sats) / SATS

    def _utxos(self) -> List[Dict[str, Any]]:
        rows = json.loads(self._http(f"/address/{self.address()}/utxo"))
        return [row for row in rows if isinstance(row, dict) and row.get("value")]

    def _fee_rate(self, target: int = 6) -> Decimal:
        """sat/vB from the node's own estimate, with a floor so a quiet mempool still confirms."""
        try:
            estimates = json.loads(self._http("/fee-estimates", timeout=10))
            value = estimates.get(str(target)) or estimates.get("6") or estimates.get("1") or 2
            return max(Decimal(str(value)), Decimal("1"))
        except (ChainError, json.JSONDecodeError, ArithmeticError):
            return Decimal("2")

    # ---------- writing ----------

    def plan_transfer(self, amount: Decimal, payee: str, account_index: int = 0, asset: str = "") -> TxPlan:
        from embit.transaction import Transaction, TransactionInput, TransactionOutput

        _, script_mod, net = self._embit()
        sats = int(amount * SATS)
        if sats <= 0:
            raise ChainError("bad_amount", "amount rounds to zero satoshis")
        try:
            # embit infers the network from the address prefix and rejects a wrong-net address
            payee_script = script_mod.address_to_scriptpubkey(payee)
        except Exception as exc:  # noqa: BLE001 - embit raises a variety of parse errors
            raise ChainError("invalid_payee", f"not a {self.network}net address: {exc}") from exc

        own = self._script()
        utxos = self._utxos()
        if not utxos:
            raise ChainError("insufficient_funds", "this address has no spendable outputs")
        utxos.sort(key=lambda row: row["value"], reverse=True)

        rate = self._fee_rate()
        chosen: List[Dict[str, Any]] = []
        total = 0
        # two outputs when change is needed, one when it is not; check both
        for candidate in utxos:
            chosen.append(candidate)
            total += candidate["value"]
            fee = int(rate * _vsize(len(chosen), 2))
            if total >= sats + fee:
                break
        else:
            fee = int(rate * _vsize(len(chosen), 2))
            raise ChainError("insufficient_funds",
                             f"need {Decimal(sats + fee) / SATS:.8f} BTC including fee, "
                             f"have {Decimal(total) / SATS:.8f}")

        change = total - sats - fee
        outputs = [TransactionOutput(sats, payee_script)]
        if change >= DUST:
            outputs.append(TransactionOutput(change, own))
        else:
            fee += change          # dust change is not worth an output: give it to the miner
            change = 0

        tx = Transaction(
            version=2,
            vin=[TransactionInput(bytes.fromhex(row["txid"])[::-1], row["vout"], sequence=0xFFFFFFFD)
                 for row in chosen],
            vout=outputs,
            locktime=0)

        # Sign each p2wpkh input: BIP143 sighash from the PSBT's own implementation, then a
        # [sig || SIGHASH_ALL, pubkey] witness. Finalising by hand keeps one code path and no
        # dependence on PSBT finalisation quirks.
        from embit.psbt import PSBT
        from embit.transaction import Witness

        psbt = PSBT(tx)
        for index, row in enumerate(chosen):
            psbt.inputs[index].witness_utxo = TransactionOutput(int(row["value"]), own)
        priv = self._priv()
        for index in range(len(chosen)):
            digest = psbt.sighash(index)
            signature = priv.sign(digest).serialize() + b"\x01"
            tx.vin[index].witness = Witness([signature, priv.get_public_key().sec()])

        raw = tx.serialize().hex()
        return TxPlan(
            kind=self.kind, asset="BTC", amount=amount, payee=payee,
            fee=Decimal(fee) / SATS,
            raw={"raw_tx": raw, "inputs": len(chosen), "change": str(Decimal(change) / SATS),
                 "fee_rate": str(rate), "txid": tx.txid().hex()},
            summary=f"{amount} BTC -> {payee[:12]}… ({len(chosen)} input(s), {rate} sat/vB)")

    def payment_proof(self, txid: str, address: str) -> Dict[str, Any]:
        """Prove a Bitcoin payment: sum the outputs that pay this address in that transaction."""
        try:
            tx = json.loads(self._http(f"/tx/{txid}"))
            status = json.loads(self._http(f"/tx/{txid}/status"))
        except ChainError as exc:
            if exc.code == "rpc_error" and "404" in str(exc.message):
                return {"received": False, "amount": "0", "confirmations": 0,
                        "reason": "transaction not found"}
            raise
        sats = sum(int(out.get("value", 0)) for out in tx.get("vout", [])
                   if str(out.get("scriptpubkey_address", "")) == address)
        confirmations = int(status.get("confirmations") or 0)
        if not confirmations and status.get("block_height"):
            # Esplora's tx status carries the height, not always a count
            try:
                tip = int(self._http("/blocks/tip/height", timeout=10).decode())
                confirmations = max(0, tip - int(status["block_height"]) + 1)
            except (ChainError, ValueError):
                confirmations = 0
        return {"received": sats > 0 and bool(status.get("confirmed")),
                "amount": f"{Decimal(sats) / SATS:f}",
                "confirmations": confirmations,
                "confirmed": bool(status.get("confirmed"))}

    def broadcast(self, plan: TxPlan) -> Dict[str, Any]:
        raw = plan.raw.get("raw_tx")
        if not raw:
            raise ChainError("bad_plan", "no signed transaction in this plan")
        try:
            txid = self._http("/tx", method="POST", body=raw.encode(),
                              timeout=max(self.timeout, 60)).decode().strip()
        except ChainError as exc:
            # The failover list means a POST can reach a second host after the first one accepted
            # the transaction and then timed out. That host answers "already known", which used to
            # be reported as a failed payment for money that had in fact left.
            known = str(plan.raw.get("txid") or "")
            text = str(exc.message).lower()
            if len(known) == 64 and any(marker in text for marker in (
                    "already in block chain", "already known", "txn-already-in-mempool",
                    "txn-already-known", "transaction already exists")):
                txid = known
            else:
                raise
        if len(txid) != 64:
            raise ChainError("broadcast_rejected", f"unexpected response: {txid[:120]}")
        return {"tx_hash": txid, "explorer": self.explorer_tx.format(tx=txid),
                "fee": str(plan.fee), "signed": True}


def from_secret(kind: str, secret_hex: str, api_url: str = "") -> BtcAdapter:
    return BtcAdapter(kind, secret_hex, api_url=api_url)
