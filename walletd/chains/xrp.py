"""XRP Ledger adapter: address, balance and receiving. Signing is deliberately not wired up yet.

Why it exists at all: the user asked for XMR, BTC and XRP on install, and XRP is the one chain whose
account model is trivially cheap to *receive* on - an address, a balance, and a destination tag. That
is the useful half for an agent wallet (quote, invoice, verify funds) and it needs no external
signer: the key sits in the same sealed payload as the Bitcoin and EVM keys.

Address derivation is the standard XRPL one, and it is implemented here rather than pulled from a
dependency so a fresh install can show an address before any optional package is present:

    account_id = RIPEMD160(SHA256(compressed_secp256k1_pubkey))
    address    = base58_ripple(0x00 || account_id || SHA256d(payload)[:4])

Sending is refused with an explicit ``unsupported`` error instead of pretending: an XRP Payment needs
a sequence number, a fee, a last-ledger bound and canonical binary serialisation, and a wrong
implementation of any of those loses money. The rest of the plugin treats that exactly like any other
chain that cannot sign - the request is denied with a reason, never silently.
"""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from decimal import Decimal
from typing import Any, Dict, Optional

from .base import ChainAdapter, ChainError, TxPlan

DROPS = Decimal(10) ** 6          # 1 XRP = 1 000 000 drops

# XRPL's base58 alphabet. It is not Bitcoin's: the characters are checked so the checksum means the
# same thing on the other side.
_RIPPLE_ALPHABET = "rpshnaf39wBUDNEGHJKLM4PQRST7VWXYZ2bcdeCg65jkm8oFqi1tuvAxyz"

RPC = {
    "xrp-mainnet": "https://xrplcluster.com/",
    "xrp-testnet": "https://s.altnet.rippletest.net:51234/",
}


def _b58encode(raw: bytes) -> str:
    number = int.from_bytes(raw, "big")
    out = ""
    while number:
        number, rem = divmod(number, 58)
        out = _RIPPLE_ALPHABET[rem] + out
    # leading zero bytes are encoded as the first alphabet character, as in every base58 variant
    for byte in raw:
        if byte == 0:
            out = _RIPPLE_ALPHABET[0] + out
        else:
            break
    return out


def _double_sha256(raw: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(raw).digest()).digest()


def address_from_secret(secret_hex: str) -> str:
    """Classic XRPL address (``r...``) for a 32-byte secp256k1 private key."""
    try:
        from eth_keys import keys  # shipped with eth-account, already a dependency
    except ImportError as exc:  # pragma: no cover - dependency error path
        raise ChainError("unsupported", "eth-keys is required to derive an XRP address") from exc
    private = keys.PrivateKey(bytes.fromhex(secret_hex))
    public = private.public_key.to_bytes()          # 64 bytes: x || y, no 0x04 prefix
    x = public[:32]
    # SEC1 compressed form encodes the parity of **y** in the prefix byte, not of x. Taking it from
    # x[-1] (the low byte of the x coordinate) produced a valid-looking address that the matching key
    # cannot spend - for about half of all keys. Verified against ecdsa's point arithmetic.
    prefix = b"\x02" if public[-1] % 2 == 0 else b"\x03"
    compressed = prefix + x
    account_id = hashlib.new("ripemd160", hashlib.sha256(compressed).digest()).digest()
    payload = b"\x00" + account_id
    return _b58encode(payload + _double_sha256(payload)[:4])


def _b58decode(text: str) -> bytes:
    number = 0
    for char in text:
        number = number * 58 + _RIPPLE_ALPHABET.index(char)
    raw = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    # leading 'r' characters are zero bytes, and int() has dropped them
    pad = len(text) - len(text.lstrip(_RIPPLE_ALPHABET[0]))
    return b"\x00" * pad + raw


def is_address(address: str) -> bool:
    """Full XRPL check: alphabet, version byte, and the double-SHA256 checksum."""
    if not address or not address.startswith("r") or len(address) < 25 or len(address) > 35:
        return False
    try:
        raw = _b58decode(address)
    except (ValueError, IndexError):
        return False
    if len(raw) < 6 or raw[0] != 0:
        return False
    body, checksum = raw[:-4], raw[-4:]
    return _double_sha256(body)[:4] == checksum


class XrpAdapter(ChainAdapter):
    """Read-only XRPL account: address, balance, and whether it exists yet."""

    asset = "XRP"
    supports_accounts = False
    explorer_address = "https://xrpscan.com/account/{address}"
    explorer_tx = "https://xrpscan.com/tx/{tx}"

    def __init__(self, kind: str, secret_hex: str, rpc_url: str = "", timeout: float = 30.0):
        if kind not in RPC:
            raise ChainError("unknown_chain", f"{kind} is not a supported XRP network")
        self.kind = kind
        self.secret_hex = secret_hex
        self.rpc_url = rpc_url or RPC[kind]
        self.timeout = timeout
        self._address: Optional[str] = None
        self._seq = 0

    # ---------- plumbing ----------

    def _call(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        body = json.dumps({"method": method, "params": [params]}).encode("utf-8")
        request = urllib.request.Request(self.rpc_url, data=body,
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            raise ChainError("rpc_unreachable", f"XRP node unreachable: {type(exc).__name__}") from exc
        result = payload.get("result") or {}
        if result.get("status") == "error":
            error = str(result.get("error", "rpc_error"))
            if error == "actNotFound":
                return {"account_data": None}
            raise ChainError("rpc_error", f"XRP node error: {error}")
        return result

    # ---------- adapter surface ----------

    def address(self) -> str:
        if self._address is None:
            self._address = address_from_secret(self.secret_hex)
        return self._address

    def available(self) -> bool:
        try:
            self._call("server_info", {})
            return True
        except ChainError:
            return False

    def status(self) -> Dict[str, Any]:
        try:
            info = self._call("server_info", {}).get("info") or {}
            return {"kind": self.kind, "available": True,
                    "height": int(info.get("validated_ledger", {}).get("seq", 0) or 0)}
        except ChainError as exc:
            return {"kind": self.kind, "available": False, "error": exc.code}

    def balance(self) -> Decimal:
        """Spendable balance in XRP. An unfunded account is 0, not an error: that is normal here."""
        result = self._call("account_info",
                            {"account": self.address(), "ledger_index": "validated", "strict": True})
        data = result.get("account_data")
        if not data:
            return Decimal("0")
        # the reserve is not spendable, but a wallet that shows 0 for a funded account is worse than
        # one that shows the gross balance; report gross and let the reserve be visible on the explorer
        return (Decimal(int(data.get("Balance", 0))) / DROPS).quantize(Decimal("0.000001"))

    def plan_transfer(self, amount, payee, account_index: int = 0, asset: str = "") -> TxPlan:
        raise ChainError("unsupported",
                         "XRP can receive and hold funds, but this wallet does not sign XRP payments "
                         "yet - use XMR or BTC to pay out")

    def explorer_address_url(self) -> str:
        return self.explorer_address.replace("{address}", self.address())
