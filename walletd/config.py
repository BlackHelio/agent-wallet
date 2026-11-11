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
    human_gate: bool = True              # passphrase prompt before unlock
    # Every payment asks for the six-digit PIN, in the pane, on a card that names the alias, the
    # destination, the amount and the fee. Turning this off is a passphrase-level change and shows a
    # warning in the pane; the only other exception is an autopay an operator marked `unattended`.
    pin_every_payment: bool = True
    approval_threshold: Decimal = Decimal("0.02")   # default: above this, operator types the amount
    # One scalar for six coins is wrong in both directions: a BTC payment can never exceed 0.02
    # (per-tx cap 0.001), so the typed confirmation is unreachable, while almost every XRP or USDC
    # payment trips it. Per-asset overrides fix that without changing the old key.
    approval_thresholds: Dict[str, str] = field(default_factory=dict)
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
    # Off by default: the wallet pays in whatever coin the operator enabled, and the agent is not
    # second-guessed about it. Turning it on is for an XMR-first operator — then the agent is told to
    # pay in Monero, and a payment in another coin has to say *why* ("this shop only takes USDC"). The
    # reason lands in the request and the receipt, so a human sees the exception rather than
    # discovering it later. Default off because a wallet that refuses the coin you just funded is a
    # bug, not a feature, to everyone who is not already a Monero user.
    prefer_monero: bool = False
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
    # Keep enough native coin to keep paying for gas. A wallet that spends its last ETH on a
    # transfer cannot send anything again on that chain until a human funds it, and an agent should
    # not be able to walk the wallet into that corner — not by mistake, and not because a web page
    # told it to. Measured in *transfers*, not in a fixed amount, so the floor tunes itself to the
    # chain instead of stranding money on a cheap one and being useless on an expensive one.
    gas_floor_transfers: int = 4
    gas_floor: Dict[str, str] = field(default_factory=dict)   # override, in the native coin
    # A separate wallet that holds the gas money. Only an address: this wallet never spends from it,
    # so an agent that goes rogue cannot reach the reserve, and refilling is a human act. `wallet
    # gas` names the shortfall and this address; nothing here grants any authority.
    gas_reserve: Dict[str, str] = field(default_factory=dict)
    # Swaps: a conversion is a payment to a contract, so it is a policy, not a convenience. Off
    # means the daemon refuses every swap outright; `suggest_swaps` is the narrower switch — the
    # operator can still convert from the CLI, but the agent may not propose one and the brief never
    # mentions it. The slippage ceiling is clamped to 5% in the daemon whatever is written here.
    swaps_enabled: bool = True
    suggest_swaps: bool = True
    swap_slippage_bps: int = 50
    # Empty = every operator pinned in code. A list narrows it; it can never add one.
    swap_routers: List[str] = field(default_factory=list)
    per_tx_cap: Dict[str, str] = field(default_factory=lambda: {"XMR": "0.5", "BTC": "0.001",
                                                               "XRP": "50",
                                                               "ETH": "0.05", "POL": "10", "USDC": "25"})
    daily_cap: Dict[str, str] = field(default_factory=lambda: {"XMR": "1.0", "BTC": "0.002",
                                                              "XRP": "200",
                                                              "ETH": "0.1", "POL": "25", "USDC": "50"})
    # A weekly autopay created with the old 168-hour ceiling expired exactly when payment two was
    # due. A year is the default; an operator can still shorten it per mandate.
    max_mandate_lifetime_hours: int = 8760
    monero_rpc_url: str = "http://127.0.0.1:38084/json_rpc"   # the only port the signer is spawned on
    monero_provider: str = "cakewallet"   # which public node the wallet daemon talks to
    monero_wallet_file: str = "agent-wallet"   # wallet file name inside the RPC's --wallet-dir
    unattended_policy: str = "deny"      # deny | mandate-only

    def cap(self, table: Dict[str, str], asset: str) -> Decimal:
        try:
            return Decimal(str(table.get(asset, "0")))
        except (InvalidOperation, ValueError):
            return Decimal("0")

    def threshold(self, asset: str) -> Decimal:
        """Amount above which the operator has to type it, for *this* coin.

        Falls back to the flat `approval_threshold`, but never above the coin's per-tx cap: a
        threshold the cap makes unreachable is an approval guard that silently does not exist.
        """
        asset = str(asset or "").upper()
        explicit = (self.approval_thresholds or {}).get(asset)
        if explicit not in (None, ""):
            try:
                return Decimal(str(explicit))
            except (InvalidOperation, ValueError):
                pass
        cap = self.cap(self.per_tx_cap, asset) if asset else Decimal("0")
        base = self.approval_threshold
        if cap > 0 and base > cap:
            return cap
        return base

    def to_dict(self) -> Dict[str, Any]:
        return {
            "human_gate": self.human_gate,
            "pin_every_payment": self.pin_every_payment,
            "approval_threshold": str(self.approval_threshold),
            "approval_thresholds": dict(self.approval_thresholds),
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
            "prefer_monero": self.prefer_monero,
            "assets_enabled": list(self.assets_enabled),
            "payee_allowlist": list(self.payee_allowlist),
            # fee_cap and http_timeout_seconds used to be missing here, so `config set fee_cap=...`
            # wrote a signed file without them, reported success, and the next unlock restored the
            # shipped default - the operator's tightened cap silently loosened.
            "fee_cap": dict(self.fee_cap),
            # Same trap as fee_cap: a key that to_dict drops is a key `config set` writes, reports
            # success for, and then loses on the next unlock.
            "gas_floor_transfers": self.gas_floor_transfers,
            "gas_floor": dict(self.gas_floor),
            "gas_reserve": dict(self.gas_reserve),
            "swaps_enabled": self.swaps_enabled,
            "suggest_swaps": self.suggest_swaps,
            "swap_slippage_bps": self.swap_slippage_bps,
            "swap_routers": list(self.swap_routers),
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
        # Lists are sets here: a duplicated coin or chain is a rendering bug waiting to happen, and
        # older builds did persist duplicates. Healing on load makes an existing file self-correct.
        for key in ("assets_enabled", "chains_enabled", "payee_allowlist"):
            if isinstance(known.get(key), list):
                seen = []
                for item in known[key]:
                    text = str(item).strip()
                    text = text.upper() if key == "assets_enabled" else text
                    if text and text not in seen:
                        seen.append(text)
                known[key] = seen
        if "approval_threshold" in known:
            known["approval_threshold"] = Decimal(str(known["approval_threshold"]))
        for boolean in ("prefer_monero", "pin_every_payment"):
            if boolean not in known:
                continue
            value = known[boolean]
            if isinstance(value, str):
                known[boolean] = value.strip().lower() not in ("0", "false", "no", "off", "")
            else:
                known[boolean] = bool(value)
        if "prefer_monero" in known:
            value = known["prefer_monero"]
            # a hand-edited "false"/"0" must not become truthy by accident
            known["prefer_monero"] = value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on")
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
