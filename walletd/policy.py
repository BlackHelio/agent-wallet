"""Spend requests, mandates, and the policy engine.

Policy lives here — inside the trusted process — not in the plugin, so a prompt-injected agent
cannot argue with it. Every denial carries a machine code and a human sentence, because both go
into the receipt.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from .config import Settings

ISO = "%Y-%m-%dT%H:%M:%SZ"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime(ISO)


def parse_amount(value: Any, asset: str) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"amount for {asset} is not a number") from exc
    if not amount.is_finite():
        # NaN made the comparison below raise out of the request path, and Infinity passed it: an
        # autopay could be proposed with unbounded caps
        raise ValueError(f"amount for {asset} is not a number")
    if amount <= 0:
        raise ValueError("amount must be positive")
    exponent = amount.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -12:
        raise ValueError("amount has more than 12 decimal places")
    return amount


# Monero address prefixes differ per network: mainnet 4/8, stagenet 5/7, testnet 9/A.
_XMR_RE = {
    "monero-mainnet": re.compile(r"^[48][0-9AB][1-9A-HJ-NP-Za-km-z]{93}$"),
    "monero-stagenet": re.compile(r"^[57][0-9AB][1-9A-HJ-NP-Za-km-z]{93}$"),
    "monero-testnet": re.compile(r"^[9A][0-9AB][1-9A-HJ-NP-Za-km-z]{93}$"),
    "sim-mainnet": re.compile(r"^[48][0-9AB][1-9A-HJ-NP-Za-km-z]{93}$"),
}
_EVM_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
# Bitcoin addresses are network-specific: mainnet uses bc1/1/3, testnet uses tb1/m/n/2, regtest uses
# bcrt1. Accepting only mainnet shapes meant a testnet wallet could not pay anyone at all.
_BTC_MAIN_RE = re.compile(r"^(bc1[ac-hj-np-z02-9]{11,71}|[13][a-km-zA-HJ-NP-Z1-9]{25,34})$")
_BTC_TEST_RE = re.compile(r"^(tb1[ac-hj-np-z02-9]{11,71}|bcrt1[ac-hj-np-z02-9]{11,71}|[mn2][a-km-zA-HJ-NP-Z1-9]{25,34})$")
# XRPL classic address: 'r' + base58 (ripple alphabet) with a double-SHA256 checksum
_XRP_RE = re.compile(r"^r[1-9A-HJ-NP-Za-km-z]{24,34}$")


def payee_key(kind: str, address: str) -> str:
    """Normalise a payee for comparison. EVM/bech32 are case-insensitive; base58 is not."""
    address = (address or "").strip()
    if not address:
        raise ValueError("payee address is required")
    # every EVM chain Hermes knows about, including the "-mainnet"/"-sepolia" ids the node registry
    # uses — a chain missing from this tuple rejects a perfectly good address as "unknown chain"
    if kind in ("evm", "base", "base-mainnet", "base-sepolia", "bsc", "polygon",
                "polygon-mainnet", "polygon-amoy", "arbitrum", "arbitrum-mainnet"):
        if not _EVM_RE.match(address):
            raise ValueError("not a valid EVM address")
        return address.lower()
    if kind.startswith("xrp"):
        from .chains.xrp import is_address as _xrp_is_address   # checksum, not just the shape
        if not _xrp_is_address(address):
            raise ValueError("not a valid XRP address")
        return address
    if kind.startswith("monero") or kind.startswith("sim-"):
        pattern = _XMR_RE.get(kind)
        if pattern is None:
            raise ValueError(f"unknown Monero network {kind!r}")
        if not pattern.match(address):
            raise ValueError(f"not a valid {kind} address")
        if kind == "monero-mainnet":
            # Shape is not an address: base58 + Keccak. One wrong character is a burned payment, or
            # someone else's wallet, and the regex cannot tell the difference.
            from . import addresses as _addresses
            if not _addresses.xmr_valid(address, kind):
                raise ValueError("Monero address checksum does not match — check every character")
        return address
    if kind.startswith("bitcoin"):
        pattern = _BTC_MAIN_RE if kind.endswith("mainnet") else _BTC_TEST_RE
        if not pattern.match(address):
            network = "mainnet" if kind.endswith("mainnet") else "testnet"
            raise ValueError(f"not a valid Bitcoin {network} address")
        if kind == "bitcoin-mainnet":
            from . import addresses as _addresses
            # bech32/bech32m for segwit and taproot, Base58Check for 1…/3…. Running the bech32
            # check over every address refused all legacy and script addresses with a message about
            # a checksum they do not have.
            if not _addresses.btc_valid(address, "main"):
                raise ValueError("Bitcoin address checksum does not match — check every character")
        # bech32 is case-insensitive, base58 is not: lower-case only the bech32 forms
        return address.lower() if address.lower().startswith(("bc1", "tb1", "bcrt1")) else address
    raise ValueError(f"unknown chain kind {kind!r}")


@dataclass
class SpendRequest:
    request_id: str
    kind: str                    # monero-stagenet | bitcoin-testnet | base-sepolia | ...
    asset: str                   # XMR | BTC | ETH | USDC
    amount: Decimal
    payee: str
    memo: str = ""
    why: str = ""
    sandbox_id: str = ""          # spend from this sandbox (no per-payment approval)
    profile: str = ""             # Hermes profile that asked (sandbox ACL)
    funding_for: str = ""         # this transfer funds that sandbox
    sweep_of: str = ""            # this transfer returns a sandbox's leftovers
    challenge_sha256: str = ""    # digest of a 402 demand this request was priced from
    challenge: Dict[str, Any] = field(default_factory=dict)   # that demand, public terms only
    idempotency_key: str = ""     # caller's retry key: the same key never pays twice
    created_at: str = field(default_factory=now_iso)
    status: str = "pending"      # pending | approved | failed | denied | expired
    tx_hash: str = ""
    # What the network charged for this payment, recorded when it is broadcast. Without it a crash
    # between "sent" and "the budget was debited" is unreconcilable: the receipts only say the
    # broadcast started, so a restart cannot tell how much of a budget actually left.
    fee_paid: str = ""
    #: What the network quoted when the request was planned. The card shows it before the PIN, so the
    #: human approves a total, not just the amount.
    fee_estimate: str = ""
    #: The fiat the human actually asked for (`10 USD`) and the rate that turned it into `amount`, so
    #: the card can show both and a receipt can prove which number the human meant.
    fiat: Dict[str, str] = field(default_factory=dict)
    #: Where the payee came from when it was not typed by a human: the page the wallet read, its host
    #: and title, and how the address was found (402 / link / text). This is what the card shows so a
    #: person can judge an address instead of trusting that the agent found it somewhere real.
    provenance: Dict[str, str] = field(default_factory=dict)
    #: Set when this transfer moves money out of a deposit address into the main account. A merge is
    #: not a payment: the payee is this wallet's own account 0, computed by the daemon, never a caller.
    deposit_id: str = ""
    # Set when the approval is issued: this amount must be typed by the operator. Decided in one
    # place (the daemon, per asset) so the CLI and the pane cannot disagree about it.
    confirm_amount_required: bool = False
    #: How this payment was authorised: "passphrase" (a human typed it), "mandate" / "sandbox"
    #: (standing authority the human created with the passphrase), "" (nothing yet - never sent).
    authorised_by: str = ""
    #: What this payment does to the chain's gas balance: the native coin, what would be left after
    #: it, and the floor the wallet keeps. On the card, so a human sees the payment cannot strand
    #: the wallet; empty for a chain that charges the fee in the coin being sent, and for a 402.
    gas: Dict[str, str] = field(default_factory=dict)
    #: Set when this request is a conversion rather than a payment: asset_out, min_out, the operator
    #: name, the router, the quoted out and the path. The payee is then the pinned router, which the
    #: caller never chose, so the allowlist and the blessing checks do not apply to it.
    swap: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "request_id": self.request_id,
            "kind": self.kind,
            "asset": self.asset,
            "amount": str(self.amount),
            "payee": self.payee,
            "memo": self.memo,
            "why": self.why,
            "sandbox_id": self.sandbox_id,
            "profile": self.profile,
            "funding_for": self.funding_for,
            "sweep_of": self.sweep_of,
            "challenge_sha256": self.challenge_sha256,
            "challenge": dict(self.challenge),
            "idempotency_key": self.idempotency_key,
            "created_at": self.created_at,
            "status": self.status,
            "tx_hash": self.tx_hash,
            "authorised_by": self.authorised_by,
            "confirm_amount_required": self.confirm_amount_required,
            "fee_paid": self.fee_paid,
            "fee_estimate": self.fee_estimate,
            "fiat": dict(self.fiat),
            "provenance": dict(self.provenance),
            "deposit_id": self.deposit_id,
            "gas": dict(self.gas),
            "swap": dict(self.swap),
        }


INTERVALS = {"hourly": 1, "daily": 24, "weekly": 168, "monthly": 720}


def parse_interval(text: Any) -> int:
    """`1h`, `6h`, `2d`, `weekly`, or a bare number of hours -> hours."""
    if text in (None, ""):
        return 24
    if isinstance(text, (int, float)):
        hours = int(text)
    else:
        raw = str(text).strip().lower()
        if raw in INTERVALS:
            hours = INTERVALS[raw]
        elif raw.endswith("h") and raw[:-1].isdigit():
            hours = int(raw[:-1])
        elif raw.endswith("d") and raw[:-1].isdigit():
            hours = int(raw[:-1]) * 24
        elif raw.endswith("w") and raw[:-1].isdigit():
            hours = int(raw[:-1]) * 24 * 7
        elif raw.isdigit():
            hours = int(raw)
        else:
            raise ValueError(f"cannot read interval {text!r} (use 6h, 2d, weekly, or hours)")
    if hours < 1:
        raise ValueError("interval must be at least one hour")
    return hours


@dataclass
class Mandate:
    mandate_id: str
    kind: str
    asset: str
    payee: str
    per_tx_cap: Decimal
    period_cap: Decimal
    period_hours: int
    absolute_cap: Decimal
    expires_at: str
    enabled: bool = True
    created_by: str = "operator"
    label: str = ""
    # An autopay's caps apply to *its own* spending. Without a creation floor the period/lifetime
    # totals count every earlier payment to the same payee, so a brand new autopay looks already
    # exhausted (0 of 0.01 left) and denies the very payment it was created for.
    created_at: str = ""
    activated_at: str = ""         # set when the operator turns it on: proposed vs paused
    revoked_at: str = ""           # terminal: revoked is not the same state as paused
    # The one way a payment can leave without the PIN. Only an operator can set it (it costs the
    # passphrase *and* the PIN), and the pane shows it in red. Everything else asks every time.
    unattended: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mandate_id": self.mandate_id,
            "kind": self.kind,
            "asset": self.asset,
            "payee": self.payee,
            "per_tx_cap": str(self.per_tx_cap),
            "period_cap": str(self.period_cap),
            "period_hours": self.period_hours,
            "absolute_cap": str(self.absolute_cap),
            "expires_at": self.expires_at,
            "enabled": self.enabled,
            "created_by": self.created_by,
            "label": self.label,
            "created_at": self.created_at,
            "activated_at": self.activated_at,
            "revoked_at": self.revoked_at,
            "unattended": self.unattended,
            "status": self.status(),
        }

    def expired(self, at: Optional[datetime] = None) -> bool:
        at = at or datetime.now(timezone.utc)
        try:
            return at >= datetime.strptime(self.expires_at, ISO).replace(tzinfo=timezone.utc)
        except ValueError:
            return True   # unparseable expiry fails closed

    def status(self) -> str:
        if not self.enabled:
            # never activated = the agent's proposal; switched off after use = paused (revoked is a
            # different, terminal thing now: `revoked_at` is set by `mandates_revoke`)
            if self.revoked_at:
                return "revoked"
            return "paused" if self.activated_at else "proposed"
        return "expired" if self.expired() else "active"

    @classmethod
    def parse(cls, raw: Dict[str, Any], settings: Settings) -> "Mandate":
        kind = str(raw.get("kind", ""))
        asset = str(raw.get("asset", "")).upper()
        if kind not in settings.chains_enabled:
            raise ValueError(f"chain {kind!r} is not enabled")
        if asset not in settings.assets_enabled:
            raise ValueError(f"asset {asset!r} is not enabled")
        payee = payee_key(kind, str(raw.get("payee", "")))
        per_tx = parse_amount(raw.get("per_tx_cap"), asset)
        period_cap = parse_amount(raw.get("period_cap"), asset)
        absolute = parse_amount(raw.get("absolute_cap"), asset)
        if absolute < period_cap:
            raise ValueError("absolute_cap must be >= period_cap")
        hours = parse_interval(raw.get("interval") or raw.get("period_hours") or 24)
        if hours < 1 or hours > settings.max_mandate_lifetime_hours:
            raise ValueError(f"period_hours must be 1..{settings.max_mandate_lifetime_hours}")
        expires_raw = raw.get("expires_at")
        if expires_raw:
            expires = str(expires_raw)
            try:
                parsed = datetime.strptime(expires, ISO).replace(tzinfo=timezone.utc)
            except ValueError as exc:
                raise ValueError("expires_at must be YYYY-MM-DDTHH:MM:SSZ") from exc
        else:
            # default lifetime is the maximum (a week), not one interval: an autopay that expires
            # after its first period is not an autopay
            parsed = datetime.now(timezone.utc) + timedelta(hours=settings.max_mandate_lifetime_hours)
            expires = parsed.strftime(ISO)
        horizon = datetime.now(timezone.utc) + timedelta(hours=settings.max_mandate_lifetime_hours)
        if parsed > horizon:
            raise ValueError("expires_at exceeds max_mandate_lifetime_hours")
        return cls(
            # random, not a timestamp: two proposals in one second shared an id and the second
            # silently replaced the first
            mandate_id=str(raw.get("mandate_id") or f"mnd_{secrets.token_hex(6)}"),
            kind=kind,
            asset=asset,
            payee=payee,
            per_tx_cap=per_tx,
            period_cap=period_cap,
            period_hours=hours,
            absolute_cap=absolute,
            expires_at=expires,
            enabled=bool(raw.get("enabled", True)),
            created_by=str(raw.get("created_by", "operator")),
            label=str(raw.get("label", ""))[:80],
            # Never from an agent proposal: `mandates_create` refuses `unattended` unless the caller
            # is the operator and passes the passphrase AND the PIN (see `mandates_set_unattended`).
            unattended=bool(raw.get("unattended", False)),
            revoked_at=str(raw.get("revoked_at", "")),
        )


@dataclass
class SpendContext:
    """Everything policy needs about the past. Supplied by the ledger, never by the agent."""
    unattended: bool = False
    # True when the payee is this wallet's own address (a sweep returning a sandbox balance home).
    # Per-tx/daily caps and the payee allowlist exist to limit what leaves the wallet; moving money
    # between our own accounts is not that, and treating it as a spend let a tightened cap wedge a
    # sandbox teardown forever.
    self_transfer: bool = False
    period_spent: Decimal = Decimal("0")
    daily_spent: Decimal = Decimal("0")
    lifetime_spent: Decimal = Decimal("0")
    # False when the daemon is locked and has no ledger to append to: a refusal then leaves no
    # receipt, and saying "recorded" would be a lie the user only discovers when they audit it.
    ledger_available: bool = True
    # True when the request came in on the *agent* token. Rules that exist to contain a compromised
    # agent (the Monero preference) apply there and not to a human typing the same command.
    agent_initiated: bool = False


@dataclass
class Decision:
    allow: bool
    rule: str
    code: str
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {"allow": self.allow, "rule": self.rule, "code": self.code, "reason": self.reason}


def _deny(rule: str, code: str, reason: str) -> Decision:
    return Decision(False, rule, code, reason)


def _allowlisted(kind: str, payee: str, allowlist: List[str]) -> bool:
    """Is this (already normalised) payee on the operator's allowlist?

    Each entry is normalised the same way the payee was — per chain. The list used to be lower-cased
    wholesale and compared with a payee that keeps its case on base58 chains, so an allowlisted Monero,
    XRP or legacy Bitcoin address never matched and every payment to it was refused.
    """
    for entry in allowlist:
        try:
            if payee_key(kind, str(entry)) == payee:
                return True
        except ValueError:
            continue                    # an entry for another chain: not this payee
    return False


def evaluate(req: SpendRequest, settings: Settings, ctx: SpendContext,
             mandate: Optional[Mandate] = None) -> Decision:
    # 1. chain / asset allowlists
    if req.kind not in settings.chains_enabled:
        # Name the fix: the default coin set is XMR/BTC/XRP, so a USDC-on-Base quote (what every real
        # x402 seller asks for) is refused until that coin is switched on - say so instead of leaving
        # the agent to guess why a documented flow does not work.
        if req.asset in settings.assets_enabled:
            # the coin is on, the chain is not: telling the user to "add XMR" when XMR is already
            # enabled sends them in a circle
            return _deny("chain_allowlist", "chain_not_enabled",
                         f"chain {req.kind} is not enabled (the {req.asset} coin is); turn that chain "
                         f"on in `hermes wallet chains` or the Wallet pane's coin menu")
        return _deny("chain_allowlist", "chain_not_enabled",
                     f"chain {req.kind} is not enabled; "
                     f"add {req.asset} with `hermes wallet coins --enable {req.asset}` "
                     f"or in the coin menu of the Wallet pane")
    if req.asset not in settings.assets_enabled:
        return _deny("asset_allowlist", "asset_not_enabled",
                     f"asset {req.asset} is not enabled; "
                     f"add it with `hermes wallet coins --enable {req.asset}` "
                     f"or in the coin menu of the Wallet pane")
    if settings.monero_only and req.asset != "XMR":
        return _deny("monero_only", "monero_only",
                     "this wallet is set to Monero only; many merchants do not accept XMR, "
                     "so switch the setting if a shop needs another coin")
    # Prefer Monero: the default. An agent paying in another coin has to say why, and the reason is
    # what a human reads later - "this shop only takes USDC" is an exception, not a habit. A sweep or
    # a sandbox funding transfer is our own money moving between our own addresses, so it is exempt.
    if (ctx.agent_initiated and settings.prefer_monero and req.asset != "XMR"
            and not req.sweep_of and not req.funding_for
            and not str(req.why or "").strip()):
        preferred = (settings.prefer_asset or "XMR").upper()
        return _deny("prefer_monero", "prefer_monero_needs_reason",
                     f"this wallet prefers {preferred}: pay in {preferred}, or say why this payee "
                     f"needs {req.asset} (pass why=\"why this merchant needs {req.asset}\") and the "
                     f"reason is recorded with the payment")

    # 2. payee normalisation + optional allowlist
    try:
        payee = payee_key(req.kind, req.payee)
    except ValueError as exc:
        return _deny("payee_format", "invalid_payee", str(exc))
    if ctx.self_transfer:
        return Decision(True, "sweep", "sweep_ok",
                        "a transfer to this wallet's own address; caps do not apply")
    if settings.payee_allowlist and not _allowlisted(req.kind, payee, settings.payee_allowlist):
        return _deny("payee_allowlist", "payee_not_allowed", "payee is not in the operator allowlist")

    # 3. mandate path
    if mandate is not None:
        if not mandate.enabled:
            return _deny("mandate", "mandate_revoked", "mandate was revoked")
        if mandate.expired():
            return _deny("mandate", "mandate_expired", "mandate has expired")
        if (mandate.kind, mandate.asset) != (req.kind, req.asset):
            return _deny("mandate", "mandate_scope", "mandate covers a different chain or asset")
        if payee != mandate.payee:
            return _deny("mandate", "payee_mismatch", "payee does not match the mandate exactly")
        if req.amount > mandate.per_tx_cap:
            return _deny("mandate", "per_tx_cap", f"amount {req.amount} exceeds mandate per-tx cap {mandate.per_tx_cap} {req.asset}")
        if ctx.period_spent + req.amount > mandate.period_cap:
            return _deny("mandate", "period_cap",
                         f"period spend {ctx.period_spent + req.amount} would exceed {mandate.period_cap} {req.asset} per {mandate.period_hours}h")
        if ctx.lifetime_spent + req.amount > mandate.absolute_cap:
            return _deny("mandate", "absolute_cap",
                         f"lifetime spend would exceed the {mandate.absolute_cap} {req.asset} mandate ceiling")
        if ctx.unattended and settings.unattended_policy != "mandate-only":
            return _deny("unattended", "unattended_denied", "unattended runs may not spend")
        return Decision(True, "mandate", "mandate_ok", "inside an active mandate")

    # 4. no mandate: a human must approve, so an unattended run cannot proceed
    if ctx.unattended:
        # A locked daemon has no ledger to write to, so this denial leaves no receipt — say so, or the
        # refusal claims an audit trail the user will not find when it matters.
        return _deny("unattended", "unattended_no_operator",
                     "no operator available to approve: nothing was signed"
                     + ("" if ctx.ledger_available else " and no receipt could be written while locked"))

    # 5. caps for the manual path
    if req.amount > settings.cap(settings.per_tx_cap, req.asset):
        return _deny("per_tx_cap", "per_tx_cap",
                     f"amount {req.amount} exceeds per-tx cap {settings.cap(settings.per_tx_cap, req.asset)} {req.asset}")
    if ctx.daily_spent + req.amount > settings.cap(settings.daily_cap, req.asset):
        return _deny("daily_cap", "daily_cap",
                     f"daily spend {ctx.daily_spent + req.amount} would exceed {settings.cap(settings.daily_cap, req.asset)} {req.asset}")
    return Decision(True, "manual_approval", "needs_operator", "awaiting operator approval")
