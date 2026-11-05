"""Signed settings.

`human_gate` (the passphrase prompt) can only be switched off by an operator who knows the
passphrase — the RPC layer enforces that. This module makes a *file edit* useless on top of it:
settings are HMAC-signed with a key derived from the keystore master secret, which the agent
cannot read. A tampered or unsigned file is not "trusted defaults"; it is detected, and the
human gate is forced back ON.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List

SIG_LABEL = b"agent-wallet/config/v1"


def _signing_key(master: bytes) -> bytes:
    return hmac.new(master, SIG_LABEL, hashlib.sha256).digest()


def _canonical(payload: Dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


@dataclass
class Settings:
    human_gate: bool = True              # passphrase prompt before unlock / approval
    approval_threshold: Decimal = Decimal("0.02")   # above this, operator types the amount
    auto_provision_wallets: bool = True   # create the chain wallets on first unlock
    autolock_minutes: int = 15        # 0 = stay unlocked until locked by hand
    unlock_ttl_seconds: int = 900      # kept in sync with autolock_minutes
    http_timeout_seconds: int = 30    # quoting and settling an HTTP 402
    approval_ttl_seconds: int = 600   # a human needs longer than two minutes to check a purchase
    sandbox_destruct_seconds: int = 1800   # default window between a one-time payment and teardown
    # Bumped when new chains appear, so an existing install picks them up exactly once. Without it,
    # defaults only ever apply to a fresh home and a wallet created before XRP existed would never
    # show an XRP address no matter what the code shipped.
    schema: int = 3
    # Three coins by default: the wallet should be small enough to understand at a glance, and every
    # extra coin is another adapter, another address and another thing to sweep. Everything else is
    # one toggle away in the pane's coin menu, which writes these two lists.
    chains_enabled: List[str] = field(default_factory=lambda: ["monero-mainnet", "bitcoin-mainnet",
                                                              "xrp-mainnet"])
    simulated: bool = False    # the simulator is opt-in: it never moves money, and it is not a coin
    prefer_asset: str = "XMR"  # default asset when the agent does not name one
    monero_only: bool = False  # refuse every non-XMR payment outright
    assets_enabled: List[str] = field(default_factory=lambda: ["XMR", "BTC", "XRP"])
    payee_allowlist: List[str] = field(default_factory=list)   # empty = any payee (still needs approval)
    # what a network fee may cost, in the fee's own coin; a compromised RPC or a busy mempool must
    # not be able to turn a 1-unit payment into a 100-unit one
    # A coin with no entry here has a cap of zero, which denies every payment in that coin. New
    # coins must ship with caps or they look "broken" the first time someone tries to use them.
    fee_cap: Dict[str, str] = field(default_factory=lambda: {"XMR": "0.0005", "BTC": "0.0001",
                                                             "XRP": "0.05",
                                                             "ETH": "0.0008", "POL": "0.05",
                                                             "USDC": "0.0008"})
    per_tx_cap: Dict[str, str] = field(default_factory=lambda: {"XMR": "0.5", "BTC": "0.001",
                                                               "XRP": "50",
                                                               "ETH": "0.05", "POL": "10", "USDC": "25"})
    daily_cap: Dict[str, str] = field(default_factory=lambda: {"XMR": "1.0", "BTC": "0.002",
                                                              "XRP": "200",
                                                              "ETH": "0.1", "POL": "25", "USDC": "50"})
    max_mandate_lifetime_hours: int = 168
    monero_rpc_url: str = "http://127.0.0.1:38084/json_rpc"   # the only port the signer is spawned on
    monero_provider: str = "cakewallet"   # which public node the wallet daemon talks to
    monero_wallet_file: str = "agent-wallet"   # wallet file name inside the RPC's --wallet-dir
    unattended_policy: str = "deny"      # deny | mandate-only

    def cap(self, table: Dict[str, str], asset: str) -> Decimal:
        try:
            return Decimal(str(table.get(asset, "0")))
        except (InvalidOperation, ValueError):
            return Decimal("0")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "human_gate": self.human_gate,
            "approval_threshold": str(self.approval_threshold),
            "auto_provision_wallets": self.auto_provision_wallets,
            "autolock_minutes": self.autolock_minutes,
            "unlock_ttl_seconds": self.unlock_ttl_seconds,
            "approval_ttl_seconds": self.approval_ttl_seconds,
            "sandbox_destruct_seconds": self.sandbox_destruct_seconds,
            "schema": self.schema,
            "chains_enabled": list(self.chains_enabled),
            "simulated": self.simulated,
            "prefer_asset": self.prefer_asset,
            "monero_only": self.monero_only,
            "assets_enabled": list(self.assets_enabled),
            "payee_allowlist": list(self.payee_allowlist),
            # fee_cap and http_timeout_seconds used to be missing here, so `config set fee_cap=...`
            # wrote a signed file without them, reported success, and the next unlock restored the
            # shipped default - the operator's tightened cap silently loosened.
            "fee_cap": dict(self.fee_cap),
            "http_timeout_seconds": self.http_timeout_seconds,
            "per_tx_cap": dict(self.per_tx_cap),
            "daily_cap": dict(self.daily_cap),
            "max_mandate_lifetime_hours": self.max_mandate_lifetime_hours,
            "monero_rpc_url": self.monero_rpc_url,
            "monero_provider": self.monero_provider,
            "monero_wallet_file": self.monero_wallet_file,
            "unattended_policy": self.unattended_policy,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Settings":
        base = cls()
        known = {k: v for k, v in data.items() if hasattr(base, k)}
        # A document written before the schema key existed is version 1, whatever the dataclass
        # default says: `replace(base, **known)` would otherwise inherit today's version and every
        # migration would look already-applied.
        known.setdefault("schema", 1 if "schema" not in data else int(data.get("schema") or 1))
        if "approval_threshold" in known:
            known["approval_threshold"] = Decimal(str(known["approval_threshold"]))
        return replace(base, **known)


@dataclass
class LoadResult:
    settings: Settings
    signed: bool
    tampered: bool
    reason: str = ""


def save(path: Path, settings: Settings, master: bytes) -> None:
    payload = {"settings": settings.to_dict()}
    sig = hmac.new(_signing_key(master), _canonical(payload), hashlib.sha256).hexdigest()
    envelope = {**payload, "sig": sig}
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(envelope, handle, indent=2, sort_keys=True)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def load(path: Path, master: bytes | None) -> LoadResult:
    path = Path(path)
    if not path.exists():
        return LoadResult(Settings(), signed=False, tampered=False, reason="defaults")
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return LoadResult(Settings(), signed=False, tampered=True, reason=f"unreadable: {type(exc).__name__}")

    if not isinstance(envelope, dict) or "settings" not in envelope:
        return LoadResult(Settings(), signed=False, tampered=True, reason="malformed")

    raw_settings = envelope["settings"]
    if not isinstance(raw_settings, dict):
        return LoadResult(Settings(), signed=False, tampered=True, reason="malformed settings")
    settings = Settings.from_dict(raw_settings)
    sig = envelope.get("sig")
    if master is None:
        # Locked process cannot verify the signature. Fail closed: defaults, human gate on.
        return LoadResult(Settings(), signed=False, tampered=False,
                          reason="keystore locked; settings unverified")
    # Sign the STORED dict, not a re-serialization of the parsed one: adding a setting field must
    # not invalidate every existing install's signature (and must not be confused with tampering).
    expected = hmac.new(_signing_key(master), _canonical({"settings": raw_settings}), hashlib.sha256).hexdigest()
    if not isinstance(sig, str) or not hmac.compare_digest(sig, expected):
        # Never trust unsigned values: a tampered caps table must not be honoured at all.
        return LoadResult(Settings(), signed=False, tampered=True,
                          reason="signature mismatch; defaults restored, human gate forced on")
    return LoadResult(settings, signed=True, tampered=False)
