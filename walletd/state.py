"""The trusted process state and its method surface.

Every method declares a scope. `agent` may ask, `operator` may decide. There is no method through
which key material leaves this process.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import io
import json
import re
import secrets
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import os
import threading
import time
import urllib.parse
import urllib.request

from . import compat, config, keystore
from . import fx
from . import invoice as invoice_mod
from . import merchants as merchant_book
from . import pay402
from . import signer as signer_mod
from .chains import nodes as node_registry
from .chains.base import ChainAdapter, ChainError
from .chains.bitcoin import BtcAdapter
from .chains.xrp import XrpAdapter
from .chains.evm import EvmAdapter
from .chains.monero import XmrAdapter
from .ledger import Ledger, _now, network_class, verify_file
from .policy import Decision, Mandate, SpendContext, SpendRequest, evaluate, now_iso, parse_amount, payee_key
from .tokens import AGENT, OPERATOR

# One source of truth: plugin.yaml, walletd/__init__.py and the daemon must agree, or a stale daemon
# serves a stale protocol while the pane and CLI happily talk to it (no surface noticed 0.2.0 vs 0.3.0).
from . import __version__ as _PACKAGE_VERSION  # noqa: E402  (circular-safe: __init__ imports state last)

VERSION = _PACKAGE_VERSION

#: Wrong PINs allowed before a lockout. Six digits with no throttle is a 10^6 guess space, so the
#: counter (not the hash cost) is the real defence.
PIN_MAX_ATTEMPTS = 5

#: Lockouts after which the PIN path closes until someone proves the passphrase. Five guesses, then
#: 30 s, 2 min, 10 min, 1 h — without a hard stop a patient attacker still gets a million guesses,
#: just slowly. Three lockouts is 15 wrong PINs and then the door is shut on the six digits.
PIN_UNLOCK_LOCKOUTS = 3

#: One lock for the whole verify-and-count sequence. The daemon is a ThreadingUnixStreamServer, and
#: the counter was a read-modify-write: 12 simultaneous wrong PINs recorded `fails: 1`, so the lockout
#: never fired and 1e6 guesses were a few hours of parallel requests.
PIN_LOCK = threading.Lock()


def _pin_lockout_seconds(fails: int) -> int:
    """0 for the first four guesses, then 30 s, 2 min, 8 min, 32 min, 1 h — capped at an hour.

    Note the shape: the ladder keys off the *total* fail count, so every wrong PIN after the fourth is
    a new lockout rather than one per five. Combined with `PIN_UNLOCK_LOCKOUTS` that closes the PIN
    path on the **seventh** guess, not the fifteenth — `tests/test_credentials.py` pins that number."""
    if fails < PIN_MAX_ATTEMPTS:
        return 0
    steps = fails - PIN_MAX_ATTEMPTS
    return min(3600, 30 * (4 ** min(steps, 6)))


class JsonRpcProbe:
    """One-question RPC probe: is the signer answering, and what height does it see?"""

    def __init__(self, url: str, user: str = "", password: str = ""):
        self.url = url
        self.user = user or ""
        self.password = password or ""

    def _call(self, method: str) -> Dict[str, Any]:
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": {}}).encode()
        from . import httpdigest
        status, raw = httpdigest.post(self.url, payload, self.user, self.password, timeout=3)
        if status != 200:
            raise RuntimeError(f"HTTP {status}")
        return json.loads(raw.decode("utf-8")).get("result") or {}

    def ok(self) -> bool:
        try:
            self._call("get_version")
            return True
        except Exception:  # noqa: BLE001
            return False

    def height(self):
        try:
            return self._call("get_height").get("height")
        except Exception:  # noqa: BLE001
            return None


class WalletError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


_WINDOW_UNITS = {"m": 60, "h": 3600, "d": 86400, "w": 604800}
_WINDOW_FORMATS = ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d")


def parse_window(value: Any) -> str:
    """`30d`, `12h`, `90m`, `2w` or an ISO timestamp -> an ISO instant.

    Reporting windows come from a terminal and from an agent, so both a relative age ("the last
    30 days") and an absolute stamp have to work. Empty stays empty (no bound). Nonsense raises
    instead of silently exporting all of history, which reads like an empty range.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    unit = lowered[-1]
    if unit in _WINDOW_UNITS and lowered[:-1].isdigit():
        seconds = int(lowered[:-1]) * _WINDOW_UNITS[unit]
        return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for fmt in _WINDOW_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")
    raise WalletError("bad_window", f"expected 30d / 12h / 2w or an ISO timestamp, got {text[:40]!r}")


@dataclass
class Contact:
    """A saved counterparty: a name the agent can resolve instead of pasting addresses."""
    contact_id: str
    name: str                       # display name as the user wrote it
    key: str                        # normalised lookup key (lowercase, single spaces)
    addresses: Dict[str, str] = field(default_factory=dict)   # chain kind -> address
    notes: str = ""
    created_at: str = ""
    updated_at: str = ""
    # "operator" when a human typed it, "agent" when the agent saved it itself. Only an operator
    # contact counts as permission to pay an address without asking — otherwise a prompt-injected
    # agent could save the attacker's address as "Aiden" and then auto-pay it from a sandbox.
    created_by: str = "operator"

    def to_dict(self) -> Dict[str, Any]:
        return {"contact_id": self.contact_id, "name": self.name, "addresses": dict(self.addresses),
                "notes": self.notes, "created_at": self.created_at, "updated_at": self.updated_at,
                "created_by": self.created_by}


@dataclass
class DepositAddress:
    """A Monero account whose incoming payments are merged into the main balance.

    An account rather than a subaddress, because a subaddress *is* the main balance: there would be
    nothing to merge and nothing to show per deposit. A separate account can hold money, be read on its
    own, and be swept home with a receipt — which is what "it lands in my wallet" should look like.
    """

    deposit_id: str
    kind: str
    account_index: int
    address: str
    label: str = ""
    created_at: str = ""
    auto_merge: bool = True
    merged_total: str = "0"
    last_merge_tx: str = ""
    last_merge_at: str = ""
    last_error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"deposit_id": self.deposit_id, "kind": self.kind,
                "account_index": self.account_index, "address": self.address,
                "label": self.label, "created_at": self.created_at,
                "auto_merge": self.auto_merge, "merged_total": self.merged_total,
                "last_merge_tx": self.last_merge_tx, "last_merge_at": self.last_merge_at,
                "last_error": self.last_error}


@dataclass
class Sandbox:
    """A capped sub-wallet the agent may spend from without per-payment approval.

    The budget is moved on-chain into a dedicated wallet account once, by the operator. Inside it
    the agent is free up to the remaining budget and the expiry; outside it changes nothing.
    """
    sandbox_id: str
    kind: str
    asset: str
    account_index: int
    address: str
    budget: Decimal
    spent: Decimal
    created_at: str
    expires_at: str
    label: str = ""
    allowed_profiles: List[str] = field(default_factory=list)   # empty = every profile
    # Addresses this budget may pay *without asking a human*. Empty does not mean "anyone": with
    # nothing here a sandbox may only auto-pay a saved contact, and any other destination is filed
    # for approval. Prompt injection picks destinations, so the destination needs a human blessing.
    allowed_payees: List[str] = field(default_factory=list)
    enabled: bool = False          # becomes true when the funding transfer is broadcast
    # A one-time sandbox exists for exactly one agent payment and then destroys itself. The clock
    # starts when that payment settles, never at creation, so a slow funding approval cannot eat
    # the window.
    # budget | one_time | pooled. `pooled` is a budget that never leaves the main account: the money
    # is not moved anywhere, the daemon just refuses to exceed it. That is the only sane sandbox for a
    # chain where funding means an on-chain transfer of its own (Bitcoin: ~10-60 min to confirm, and a
    # fee that can be a large share of a small budget). Same policy, no wait, no fee, nothing stranded.
    mode: str = "budget"
    self_destruct_seconds: int = 1800
    spent_at: str = ""
    destroy_at: str = ""
    destroyed_at: str = ""
    destroy_blocked: str = ""      # why the destruction is waiting (never destroyed while it holds funds)
    last_balance: str = ""
    revoked_at: str = ""           # closed by the operator: not "unfunded", it held money once
    # An unattended budget is a prepaid card: inside it the agent pays *anyone* without a per-payment
    # PIN, because the operator already decided the ceiling and paid to fill it. The money is the
    # bound; the destination stops being one — unless the operator narrowed `allowed_payees`, which
    # still wins. Turning this on costs the passphrase AND the PIN (see sandbox_set_unattended);
    # turning it off is free, and revoking the budget clears it.
    unattended: bool = False
    armed_at: str = ""             # when the operator handed the budget over
    #: Set when the row's seal does not verify: something edited `sandboxes.json` by hand. A tampered
    #: row is refused for every money path until the operator revokes it, because the fields in that
    #: file (address, arm, allowlist) are exactly the ones a forged "confirmation" would change.
    tampered: bool = False

    def remaining(self) -> Decimal:
        return max(Decimal("0"), self.budget - self.spent)

    def expired(self) -> bool:
        try:
            return datetime.now(timezone.utc) >= datetime.strptime(self.expires_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            return True

    def status(self) -> str:
        if self.destroyed_at:
            return "destroyed"
        if self.revoked_at:
            # A revoked sandbox often still holds a remainder (the sweep can be refused while the
            # chain is down); calling that "unfunded" hid the money from every surface.
            return "revoked"
        if not self.enabled:
            return "unfunded"
        return "expired" if self.expired() else "active"

    def destroy_in(self) -> Optional[int]:
        """Seconds until a one-time sandbox destroys itself, or None when it never will."""
        if not self.destroy_at or self.destroyed_at:
            return None
        try:
            due = datetime.strptime(self.destroy_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
        return max(0, int((due - datetime.now(timezone.utc)).total_seconds()))

    def to_dict(self) -> Dict[str, Any]:
        # A destroyed sandbox no longer exists, so "remaining" must not keep reporting the budget
        # arithmetic: 0.1 XMR shown against a sandbox that is gone reads as money the agent can
        # still spend. What it held at the moment of destruction is kept separately, as history.
        destroyed = bool(self.destroyed_at)
        remaining = Decimal("0") if destroyed else self.remaining()
        return {"sandbox_id": self.sandbox_id, "kind": self.kind, "asset": self.asset,
                "account_index": self.account_index, "address": self.address,
                "budget": str(self.budget), "spent": str(self.spent),
                "remaining": str(remaining),
                "was_destroyed_with": (self.last_balance or "0") if destroyed else "",
                "created_at": self.created_at,
                "expires_at": self.expires_at, "label": self.label,
                "allowed_profiles": list(self.allowed_profiles),
                "allowed_payees": list(self.allowed_payees),
                "enabled": self.enabled, "status": self.status(),
                # true when the budget has no account of its own and is enforced against the main
                # balance (every chain without sub-accounts); independent of `mode`, so a one-time
                # budget on Bitcoin is both
                "pooled": self.account_index < 0,
                "mode": self.mode, "self_destruct_seconds": self.self_destruct_seconds,
                "spent_at": self.spent_at, "destroy_at": self.destroy_at,
                "destroyed_at": self.destroyed_at, "destroy_blocked": self.destroy_blocked,
                "last_balance": self.last_balance, "destroy_in": self.destroy_in(),
                "revoked_at": self.revoked_at,
                "unattended": self.unattended, "armed_at": self.armed_at,
                "tampered": self.tampered}


@dataclass
class Approval:
    approval_id: str
    request_id: str
    digest: str
    expires_at: datetime
    used: bool = False


class State:
    def __init__(self, home: Path):
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.keystore_path = self.home / "keystore.json"
        self.config_path = self.home / "config.json"
        self.ledger_path = self.home / "ledger.jsonl"
        self.sandboxes_path = self.home / "sandboxes.json"
        self.deposits_path = self.home / "deposits.json"
        # outside the keystore on purpose: the attempt counter must survive a restart, or the
        # lockout is trivial to bypass by restarting the daemon
        self.pin_state_path = self.home / "pin-attempts.json"
        # Unlock attempts get their own counter: an agent that burns payment attempts must not be
        # able to lock the operator out of their own wallet, and vice versa.
        self.pin_unlock_state_path = self.home / "pin-unlock-attempts.json"
        self.contacts_path = self.home / "contacts.json"
        self.mandates_path = self.home / "mandates.json"
        self.requests_path = self.home / "requests.json"
        self.idempotency_path = self.home / "idempotency.json"
        # key -> {request_id, status, created_at}. Money movement is the one place where "did my
        # retry already pay?" has to be answerable across a crash, so this is persisted, not in-memory.
        self.idempotency: Dict[str, Any] = {}
        self.contacts: Dict[str, Contact] = {}
        # Seals for the three files an agent can edit with its own uid *and* that grant spending
        # authority. `requests.json` carries no authority (an approval id and the PIN do), but these
        # do: a forged `unattended`, a forged `created_by: operator` or a forged `enabled` autopay is
        # a way to spend without a human, so each row is MAC'd with the master key and checked on
        # unlock. A file an agent can write must not be able to grant authority.
        self.seals: Dict[str, Dict[str, str]] = {"sandbox": {}, "contact": {}, "mandate": {}}
        self.seal_format: Dict[str, int] = {"sandbox": 0, "contact": 0, "mandate": 0}
        #: Digest of each file's seal map, and the last one the *ledger* recorded for that file.
        #: A seal proves a row was written by someone holding the passphrase; it cannot prove the row
        #: is the newest one, so an attacker who copied `sandboxes.json` while a budget was armed can
        #: put it back after the operator took the arm away and the seal still verifies. The ledger is
        #: append-only (and anchored), so it remembers which file was the last one written; a file that
        #: does not match its ledger record is stale, and stale authority is dropped.
        self.file_digest: Dict[str, str] = {"sandbox": "", "contact": "", "mandate": ""}
        self.ledger_rewound = ""
        #: False until `_verify_authority_seals` has run in this process. Guarding `_sealed_file` on it
        #: is what stops a save that happens *during* a load from blessing a file nobody checked.
        self.authority_checked = False
        self._signer_proc = None
        self.mutex = threading.RLock()     # every dispatch holds it: one request at a time
        self.payload: Optional[Dict[str, Any]] = None      # only while unlocked
        self.master: Optional[bytes] = None
        self.ledger: Optional[Ledger] = None
        self.settings: config.Settings = config.Settings()
        self.config_tampered = False
        self.requests: Dict[str, SpendRequest] = {}
        self.mandates: Dict[str, Mandate] = {}
        self.approvals: Dict[str, Approval] = {}
        self.sandboxes: Dict[str, Sandbox] = {}
        self.deposits: Dict[str, DepositAddress] = {}
        self.unlocked_at: Optional[datetime] = None
        # The keystore's data key: 32 bytes that decrypt the payload. Held only while unlocked and
        # required to re-seal it, so rotating a PIN or a passphrase never re-derives anything.
        self.data_key: Optional[bytes] = None
        self.unlock_ttl = 900
        self.adapters: Dict[str, ChainAdapter] = {}
        self._last_heal = 0.0        # last attempt to restart a dead signer
        # what the agent is waiting for the human to do, surfaced in the pane
        self.prompt: Optional[Dict[str, Any]] = None
        self.plans: Dict[str, Any] = {}
        # Balances cost a network round-trip per chain (and a failover retry when a node is rate
        # limiting us). The pane polls, so without a short cache a slow node held the mutation lock
        # and made every other call look hung.
        self._balances_cache: Optional[tuple] = None
        # Live sandbox balances are a network read per row, and the pane polls this list. Cached
        # briefly so a slow node cannot make the sandbox view look hung; keyed by sandbox id.
        self._sandbox_balances_cache: Dict[str, tuple] = {}
        # Load the public state at startup, not only on unlock. Sandboxes, contacts, mandates and
        # pending requests are plain JSON on disk (no key material), and a locked wallet that reports
        # "no contacts, no sandboxes, no pending requests" reads as "everything was deleted".
        self._load_sandboxes()
        self._load_deposits()
        self._load_contacts()
        self._load_mandates()
        self._load_requests()
        self._load_idempotency()

    # ---------- lifecycle ----------

    def exists(self) -> bool:
        return self.keystore_path.exists()

    def create_keystore(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """First run from the pane: the keystore did not exist, so nothing could be unlocked."""
        passphrase = str(params.get("passphrase") or "")
        if self.exists():
            raise WalletError("already_initialized", "a keystore already exists")
        if len(passphrase) < 8:
            raise WalletError("weak_passphrase", "use at least 8 characters")
        self.initialize(passphrase)
        return {"ok": True, "created": True}

    def initialize(self, passphrase: str, pin: Optional[str] = None) -> Dict[str, Any]:
        """Create the keystore. `pin` is the six-digit secret that authorises payments; without it the
        wallet can hold funds but cannot approve anything (the pane asks for one on first open)."""
        if self.exists():
            raise WalletError("already_initialized", "keystore already exists")
        if not passphrase or len(passphrase) < 8:
            raise WalletError("weak_passphrase", "passphrase must be at least 8 characters")
        if pin is not None and not re.fullmatch(r"\d{6}", str(pin)):
            raise WalletError("bad_pin_format", "the PIN is exactly six digits")
        payload, data_key = keystore.create_full(self.keystore_path, passphrase)
        self.data_key = data_key
        if pin is not None:
            payload["pin"] = keystore.make_pin(str(pin))
            envelope = keystore.reseal(keystore.load(self.keystore_path), payload, data_key)
            keystore.write_atomic(self.keystore_path,
                                  keystore.with_pin(envelope, str(pin), data_key,
                                                   pepper=self._device_key(create=True)))
        self.payload = payload
        self.master = base64.b64decode(payload["master"])
        self.ledger = Ledger(self.ledger_path, base64.b64decode(payload["ledger_seed"]))
        self._attach_anchor()
        self.settings = config.Settings()
        self.settings.assets_enabled = list(self.DEFAULT_ASSETS)
        self.settings.chains_enabled = [c for chain in self.DEFAULT_ASSETS for c in self.ASSET_CHAINS[chain]]
        config.save(self.config_path, self.settings, self.master)
        self.unlocked_at = datetime.now(timezone.utc)
        self.prompt = None      # the reason the pane was opened is satisfied
        self.ledger.append("wallet", "created", "operator",
                           {"human_gate": True, "pin": bool(pin)})
        # a wallet this process just made has nothing to verify; its rows are its own
        self.authority_checked = True
        return {"ok": True, "pubkey": self.ledger.pubkey_hex}

    def unlock(self, passphrase: str) -> Dict[str, Any]:
        upgraded = False
        try:
            envelope = keystore.load(self.keystore_path)
            if keystore.is_legacy(envelope):
                # v2 had one key straight over the payload, so there is no data key and no PIN wrapper
                # to carry over. Re-seal into v3 here, with the passphrase we just proved, and say so
                # in the receipt: the PIN has to be set once more because a PIN cannot be derived from
                # the hash inside the payload.
                payload = keystore.open_legacy(envelope, passphrase)
                envelope, data_key = keystore.seal_and_key(payload, passphrase)
                keystore.write_atomic(self.keystore_path, envelope)
                upgraded = True
            else:
                payload, data_key = keystore.open_full(envelope, passphrase)
        except keystore.InvalidPassphrase as exc:
            # A six-digit secret can only be a PIN: the passphrase has a minimum of eight
            # characters, so it can never match one. Older desktop bundles put whatever the
            # operator typed into the passphrase field, and without this the six digits came back
            # as "passphrase does not match the keystore" and the wallet could never be opened
            # from the pane at all.
            secret = str(passphrase or "")
            if len(secret) == 6 and secret.isdigit():
                try:
                    if keystore.has_pin(keystore.load(self.keystore_path)):
                        return self.unlock_pin(secret)
                except (OSError, ValueError, keystore.CorruptKeystore):
                    pass
            raise WalletError("bad_passphrase", "passphrase does not match the keystore") from exc
        except keystore.NewerKeystore as exc:
            raise WalletError("keystore_too_new", str(exc)) from exc
        except keystore.CorruptKeystore as exc:
            raise WalletError("corrupt_keystore", "keystore is missing or unreadable") from exc
        # Proving the passphrase re-arms the PIN path and clears the ladder.
        self._save_pin_unlock_state({"fails": 0, "lockouts": 0, "locked_until": "", "disabled": False})
        result = self._adopt(payload, data_key, how="passphrase")
        if upgraded:
            self._record("wallet", "keystore_upgraded", "operator",
                         {"from": keystore.LEGACY_VERSION, "to": keystore.VERSION,
                          "pin_slot": False,
                          "pin_was_set": bool((payload.get("pin") or {}).get("hash"))})
        return result

    def unlock_pin(self, pin: str) -> Dict[str, Any]:
        """Unlock with the six-digit PIN — the same secret that authorises a payment.

        The PIN opens its own wrapper around the keystore's data key, so it is a real second way into
        the file and not a shortcut that keeps the passphrase in memory. What it costs is stated where
        it belongs: six digits behind ~1 s of 128 MB scrypt per guess, plus a counter that slows a live
        attacker. The passphrase stays the stronger key and the only way to change the PIN.
        """
        row = self._pin_unlock_state()
        if row.get("disabled"):
            raise WalletError("pin_disabled",
                              "the PIN is disabled after repeated lockouts; unlock with the passphrase")
        locked_until = str(row.get("locked_until") or "")
        if locked_until and locked_until > now_iso():
            raise WalletError("pin_locked", f"too many wrong PINs; this is locked until {locked_until}")
        try:
            envelope = keystore.load(self.keystore_path)
            payload, data_key = keystore.open_with_pin(envelope, str(pin or "").strip(),
                                                       pepper=self._device_key())
        except keystore.NoDeviceKey as exc:
            raise WalletError("pin_needs_device",
                              "this PIN is bound to the machine that set it and that key is not here; "
                              "unlock with the passphrase") from exc
        except keystore.NoPinSlot as exc:
            raise WalletError("pin_not_set",
                              "this wallet has no PIN yet; unlock it with the passphrase once") from exc
        except keystore.InvalidPin as exc:
            fails = int(row.get("fails") or 0) + 1
            lockouts = int(row.get("lockouts") or 0)
            seconds = _pin_lockout_seconds(fails)
            locked = ""
            if seconds:
                locked = (datetime.now(timezone.utc) + timedelta(seconds=seconds)) \
                    .strftime("%Y-%m-%dT%H:%M:%SZ")
                lockouts += 1
            # Three lockouts and the PIN path closes until someone proves the passphrase. A million
            # guesses spread over an hour of retries is still a million guesses; this is the only
            # control that makes the online path finite.
            disabled = lockouts >= PIN_UNLOCK_LOCKOUTS
            self._save_pin_unlock_state({"fails": fails, "lockouts": lockouts,
                                         "locked_until": locked, "disabled": disabled})
            if disabled:
                raise WalletError("pin_disabled",
                                  "the PIN is disabled after repeated lockouts; unlock with the passphrase") from exc
            if locked:
                raise WalletError("pin_locked", f"wrong PIN; locked for {seconds}s") from exc
            left = max(0, PIN_MAX_ATTEMPTS - fails)
            raise WalletError("bad_pin",
                              f"wrong PIN ({left} attempt{'s' if left != 1 else ''} left)") from exc
        except keystore.NewerKeystore as exc:
            raise WalletError("keystore_too_new", str(exc)) from exc
        except keystore.CorruptKeystore as exc:
            raise WalletError("corrupt_keystore", "keystore is missing or unreadable") from exc
        if int(row.get("fails") or 0):
            # The full row, not just the two fields being cleared: a partial write used to rely on the
            # reader's defaults for `lockouts`/`disabled`, which is how a counter silently resets.
            self._save_pin_unlock_state({"fails": 0, "lockouts": 0, "locked_until": "",
                                         "disabled": False})
        return self._adopt(payload, data_key, how="pin")

    def _device_key_path(self) -> Path:
        """Beside the wallet directory, never inside it: a copy of the wallet must not carry the key
        that makes its PIN guessable. `<home>/.<name>-device.key`, 0600, 32 random bytes."""
        return self.home.parent / f".{self.home.name}-device.key"

    def _device_key(self, create: bool = False) -> Optional[bytes]:
        path = self._device_key_path()
        try:
            raw = bytes.fromhex(path.read_text(encoding="utf-8").strip())
            if len(raw) == 32:
                return raw
        except (OSError, ValueError):
            pass
        if not create:
            return None
        key = secrets.token_bytes(32)
        compat.write_private_text(path, key.hex())
        return key

    def _pin_unlock_state(self) -> Dict[str, Any]:
        """The unlock counter is plain JSON, and that is deliberate: it is read while locked, when
        there is no master key to MAC it with. It only slows a live attacker — the wall is the scrypt
        cost on the PIN wrapper, and anyone holding the file brute-forces it offline regardless."""
        try:
            row = json.loads(self.pin_unlock_state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"fails": 0, "lockouts": 0, "locked_until": "", "disabled": False}
        return {"fails": int(row.get("fails") or 0),
                "lockouts": int(row.get("lockouts") or 0),
                "locked_until": str(row.get("locked_until") or ""),
                "disabled": bool(row.get("disabled"))}

    def _save_pin_unlock_state(self, row: Dict[str, Any]) -> None:
        self._write_private_json(self.pin_unlock_state_path, row)

    def _adopt(self, payload: Dict[str, Any], data_key: bytes, how: str = "passphrase") -> Dict[str, Any]:
        """Everything an unlock does once the payload is decrypted, whichever secret opened it."""
        self.data_key = data_key
        self.payload = self._ensure_chain_keys(payload, data_key)
        self.master = base64.b64decode(payload["master"])
        self.ledger = Ledger(self.ledger_path, base64.b64decode(payload["ledger_seed"]))
        # Before the anchor is rewritten: `_write_anchor` records the newest receipt, so checking
        # after it would compare the chain with itself and a truncated chain would pass. This is the
        # only place that can still see the anchor the previous process left behind.
        self._record_anchor_verdict()
        self._attach_anchor()
        result = config.load(self.config_path, self.master)
        self.settings = result.settings
        self.config_tampered = result.tampered
        if self._migrate_settings():
            try:
                config.save(self.config_path, self.settings, self.master)
                self.ledger and self.ledger.append(
                    "wallet", "settings_migrated", "policy",
                    {"schema": self.settings.schema, "chains": list(self.settings.chains_enabled),
                     "assets": list(self.settings.assets_enabled)})
            except Exception:  # noqa: BLE001 - a migration that cannot be saved is retried next unlock
                pass
        self.unlock_ttl = self._autolock_seconds()
        self.unlocked_at = datetime.now(timezone.utc)
        self._load_sandboxes()
        self._load_deposits()
        self._load_contacts()
        self._load_mandates()
        # Authority is checked before anything else touches these files. `_load_requests` reconciles
        # in-flight sandbox spends, which *saves* `sandboxes.json` — sealing whatever it read. Doing
        # that first re-sealed a restored (stale, armed) file and made it look newest.
        self._verify_authority_seals()
        self._load_idempotency()
        self._load_requests()
        # A pending request for a chain that is no longer enabled can never be signed: clear it on
        # every unlock, not only in the migration that removed the chain.
        orphans = sorted({r.kind for r in self.requests.values() if r.status == "pending"}
                         - set(self.settings.chains_enabled))
        self._deny_requests_on(orphans)
        self._expire_unapprovable_requests()
        for req in getattr(self, "_pending_expiry_receipts", []):
            if self.ledger is not None:
                self.ledger.append("spend_request", "expired", "policy",
                                   {"request_id": req.request_id, "kind": req.kind,
                                    "asset": req.asset, "amount": str(req.amount),
                                    "reason": "the approval token did not survive; file the request again"})
        self._pending_expiry_receipts = []
        self._autostart_signer()      # background: unlocking must not wait for a signer download/start
        self._build_adapters()
        # refusals raised while locked could not be written; they go in now, marked deferred, before
        # the unlocked marker so the order still reads "refusal, then unlock"
        self._flush_deferred_receipts()
        self.ledger.append("wallet", "unlocked", "operator",
                           {"config_signed": result.signed, "config_tampered": result.tampered,
                            "how": how})
        return {"ok": True, "human_gate": self.settings.human_gate, "config_tampered": result.tampered}

    #: The fields of each authority row that a seal covers. Everything a forged value could spend:
    #: the destination, the ceiling, the arm, the blessing, the caps. `spent` is included because on a
    #: pooled budget the row *is* the accounting.
    _SEALED_FIELDS = {
        "sandbox": ("sandbox_id", "kind", "asset", "account_index", "address", "budget", "spent",
                    "enabled", "mode", "expires_at", "revoked_at", "destroyed_at", "allowed_payees",
                    "allowed_profiles", "unattended", "armed_at"),
        "contact": ("contact_id", "name", "key", "addresses", "created_by"),
        "mandate": ("mandate_id", "kind", "asset", "payee", "per_tx_cap", "period_cap",
                    "period_hours", "absolute_cap", "expires_at", "enabled", "created_by",
                    "revoked_at", "unattended", "activated_at"),
    }
    #: The field that identifies a row inside its seal map. A contact is keyed by `contact_id`, not by
    #: its name: `Contact.to_dict()` does not carry `key`, so the seal map collapsed every contact onto
    #: the same empty key and one of them silently lost its seal (caught on the live wallet: 2 rows,
    #: 1 seal).
    _SEAL_ID = {"sandbox": "sandbox_id", "contact": "contact_id", "mandate": "mandate_id"}

    def _seal_body(self, kind: str, row: Dict[str, Any]) -> bytes:
        body = {name: row.get(name) for name in self._SEALED_FIELDS[kind]}
        return json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")

    def _seal_for(self, kind: str, row: Dict[str, Any]) -> str:
        """HMAC of the authority fields, keyed by the master key — which needs the passphrase."""
        master = getattr(self, "master", None)
        if not master:
            return ""
        return hmac.new(master, kind.encode() + b"|" + self._seal_body(kind, row),
                        hashlib.sha256).hexdigest()

    def _seal_ok(self, kind: str, row: Dict[str, Any], stored: str) -> bool:
        mine = self._seal_for(kind, row)
        return bool(mine) and bool(stored) and hmac.compare_digest(mine, stored)

    def _sealed_file(self, kind: str, key: str, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        """The payload for an authority file: the rows, the format marker and a seal per row."""
        seals: Dict[str, str] = {}
        if not self.authority_checked:
            # Nothing has verified these rows yet in this process, so the values in memory may be a
            # file's word rather than the wallet's. Writing them back with fresh seals would turn an
            # unverified row into a trusted one; keep the seals the file came with, and let the
            # verification decide.
            seals = dict(self.seals[kind])
        for row in (rows if self.authority_checked else []):
            seal = self._seal_for(kind, row)
            if seal:
                seals[str(row.get(self._SEAL_ID[kind], ""))] = seal
        if seals:
            self.seals[kind] = seals
            self.seal_format[kind] = self.SEAL_VERSION
        payload = {key: rows, "seals": dict(self.seals[kind]),
                   "seal_format": self.seal_format[kind]}
        digest = self._seal_digest(kind, payload["seals"])
        payload["digest"] = digest
        self.file_digest[kind] = digest
        self._record_authority_state(kind, digest)
        return payload

    def _load_seals(self, kind: str, raw: Dict[str, Any]) -> None:
        seals = raw.get("seals")
        self.seals[kind] = {str(k): str(v) for k, v in seals.items()} if isinstance(seals, dict) else {}
        try:
            self.seal_format[kind] = int(raw.get("seal_format", 0) or 0)
        except (TypeError, ValueError):
            self.seal_format[kind] = 0
        self.file_digest[kind] = str(raw.get("digest") or "")

    #: Bumped when the file carries something a 0.7.1 reader would ignore. Format 1 = per-row seals;
    #: format 2 adds the digest the ledger records, which is what makes a *stale* file detectable.
    SEAL_VERSION = 2

    @staticmethod
    def _seal_digest(kind: str, seals: Dict[str, str]) -> str:
        body = json.dumps({"kind": kind, "seals": seals}, sort_keys=True,
                          separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(body).hexdigest()

    def _record_authority_state(self, kind: str, digest: str) -> None:
        """Put the file's identity in the hash chain, so a later unlock can tell old from new."""
        if self.ledger is None or not digest:
            return
        self.ledger.append("wallet", "authority_state", "policy",
                           {"what": kind, "digest": digest, "seals": len(self.seals[kind])})

    def _ledger_digest(self, kind: str) -> str:
        """The digest of the file the ledger last saw written, or '' if it never saw one."""
        last = ""
        for entry in self.ledger.entries() if self.ledger is not None else ():
            if entry.get("kind") == "wallet" and entry.get("decision") == "authority_state":
                details = entry.get("details") or {}
                if details.get("what") == kind and details.get("digest"):
                    last = str(details["digest"])
        return last

    def _verify_authority_seals(self) -> None:
        """Drop every authority a file cannot prove, and say so in the ledger.

        Runs on unlock because the master key is only available then. Three cases, all fail closed:

        * **A stale file** — its seal map is not the one the ledger last recorded for that file. The
          seals are genuine (they were written by someone with the passphrase), which is exactly why
          this matters: an attacker copies `sandboxes.json` while a budget is armed, the operator takes
          the arm back, and the copy is put back. Every row still verifies. The ledger is append-only
          and anchored, so it knows which file was written last; a budget that does not match it is
          closed, not merely un-armed.
        * **An unsealed file** (written before this release). Its rows are re-sealed *after* their
          authority is removed — a contact stops blessing, a budget stops being armed, an autopay
          stops running — because the file was unsigned when it was written and there is no way to
          tell a human's row from an agent's. The operator re-blesses / re-arms / re-activates with
          the passphrase, which is one deliberate act each.
        * **A sealed file whose row does not match** (`seal_format` present, seal missing or wrong):
          a hand-edit. A sandbox is marked `tampered` and refused for money until it is revoked; a
          contact drops to `agent`; a mandate stops.
        """
        stale: Dict[str, str] = {}
        for kind in ("sandbox", "contact", "mandate"):
            if self.seal_format[kind] == 0:
                continue                    # pre-seal file: the per-row path below decides
            if self.ledger_rewound:
                stale[kind] = "ledger_rewound"
                continue
            recorded = self._ledger_digest(kind)
            # `recorded` is empty only until this wallet has written its first format-2 file, so an
            # upgrade adopts what it finds; after that, a file whose digest is missing or different
            # is not the one the ledger last saw — including a 0.7.1-era copy replayed afterwards.
            if recorded and recorded != self.file_digest[kind]:
                stale[kind] = "stale_copy"
        dropped: List[Dict[str, Any]] = []
        for sid, sandbox in list(self.sandboxes.items()):
            row = sandbox.to_dict()
            stored = self.seals["sandbox"].get(str(row.get(self._SEAL_ID["sandbox"])), "")
            if "sandbox" not in stale and self._seal_ok("sandbox", row, stored):
                continue
            legacy = self.seal_format["sandbox"] == 0
            reason = stale.get("sandbox", "unsealed" if legacy else "seal_mismatch")
            sandbox.unattended = False
            sandbox.armed_at = ""
            sandbox.allowed_payees = []
            sandbox.allowed_profiles = []
            if reason in ("stale_copy", "ledger_rewound"):
                # the row is authentic but not the newest one, so the record of what the operator
                # last did with it is lost. Closing it is the only safe reading; the leftover is
                # still sweepable home because that destination comes from the seed, not the row.
                sandbox.revoked_at = sandbox.revoked_at or now_iso()
                sandbox.tampered = False
                dropped.append({"what": "sandbox", "id": sid, "kind": sandbox.kind, "reason": reason,
                                "now": "closed; the leftover can be swept home"})
                continue
            sandbox.tampered = not legacy
            dropped.append({"what": "sandbox", "id": sid, "kind": sandbox.kind, "reason": reason,
                            "now": "arm and allowlist cleared" + ("" if legacy else "; refused for money")})
        for key, contact in list(self.contacts.items()):
            row = contact.to_dict()
            # the seal map is keyed by the row's own id (`contact_id`), not by the lookup key the
            # in-memory dict uses: reading it by name made every contact look unsealed
            if "contact" not in stale and self._seal_ok(
                    "contact", row, self.seals["contact"].get(str(row.get(self._SEAL_ID["contact"])), "")):
                continue
            legacy = self.seal_format["contact"] == 0
            contact.created_by = "agent"
            dropped.append({"what": "contact", "id": key, "kind": "",
                            "reason": stale.get("contact", "unsealed" if legacy else "seal_mismatch"),
                            "now": "no longer blesses a destination"})
        for mid, mandate in list(self.mandates.items()):
            row = mandate.to_dict()
            if "mandate" not in stale and self._seal_ok(
                    "mandate", row, self.seals["mandate"].get(str(row.get(self._SEAL_ID["mandate"])), "")):
                continue
            legacy = self.seal_format["mandate"] == 0
            mandate.enabled = False
            mandate.unattended = False
            dropped.append({"what": "mandate", "id": mid, "kind": mandate.kind,
                            "reason": stale.get("mandate", "unsealed" if legacy else "seal_mismatch"),
                            "now": "stopped"})
        self.authority_checked = True
        if not dropped:
            # An upgrade from 0.7.1: every row verified, so the seals are the wallet's own — what is
            # missing is only the age memory. Adopt it and record it, rather than demoting rows that
            # are provably the operator's (a format-1 file replayed *after* this write is stale, and
            # its absent digest is what gives it away).
            self._adopt_format_one()
            return
        for entry in dropped:
            if self.ledger is not None:
                self.ledger.append("wallet", "authority_dropped", "policy", entry)
        # write back: the stripped rows are now the truth, and they get seals
        if any(d["what"] == "sandbox" for d in dropped):
            self._save_sandboxes()
        if any(d["what"] == "contact" for d in dropped):
            self._save_contacts()
        if any(d["what"] == "mandate" for d in dropped):
            self._save_mandates()
        # ...and a file that had nothing wrong with it still gets its digest, so one bad row in
        # `sandboxes.json` does not leave `contacts.json` un-upgraded (and un-checkable) forever
        self._adopt_format_one()

    def _adopt_format_one(self) -> None:
        """Give a 0.7.1-era file the digest it predates, once its rows have verified."""
        if self.seal_format["sandbox"] == 1:
            self._save_sandboxes()
        if self.seal_format["contact"] == 1:
            self._save_contacts()
        if self.seal_format["mandate"] == 1:
            self._save_mandates()

    def _load_contacts(self) -> None:
        self.contacts = {}
        try:
            raw = json.loads(self.contacts_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        self._load_seals("contact", raw)
        for row in raw.get("contacts", []):
            try:
                name = str(row["name"])
                key = str(row.get("key") or self._contact_key(name))
                self.contacts[key] = Contact(
                    contact_id=row.get("contact_id") or "con_" + secrets.token_hex(4),
                    name=name, key=key,
                    addresses={str(k): str(v) for k, v in (row.get("addresses") or {}).items()},
                    notes=row.get("notes", ""), created_at=row.get("created_at", ""),
                    updated_at=row.get("updated_at", ""),
                    # legacy contacts predate the field: a human wrote them, so they keep blessing
                    created_by=str(row.get("created_by", "operator")),
                )
            except (KeyError, TypeError):
                continue

    def _save_contacts(self) -> None:
        rows = [c.to_dict() for c in self.contacts.values()]
        self._write_private_json(self.contacts_path, self._sealed_file("contact", "contacts", rows))

    @staticmethod
    def _contact_key(name: str) -> str:
        return " ".join(str(name or "").strip().lower().split())

    def _load_deposits(self) -> None:
        self.deposits = {}
        try:
            raw = json.loads(self.deposits_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for row in raw.get("deposits", []) if isinstance(raw, dict) else raw:
            try:
                deposit = DepositAddress(
                    deposit_id=str(row["deposit_id"]), kind=str(row["kind"]),
                    account_index=int(row.get("account_index", 0)), address=str(row.get("address", "")),
                    label=str(row.get("label", "")), created_at=str(row.get("created_at", "")),
                    auto_merge=bool(row.get("auto_merge", True)),
                    merged_total=str(row.get("merged_total", "0")),
                    last_merge_tx=str(row.get("last_merge_tx", "")),
                    last_merge_at=str(row.get("last_merge_at", "")),
                    last_error=str(row.get("last_error", "")))
            except (KeyError, TypeError, ValueError):
                continue
            self.deposits[deposit.deposit_id] = deposit

    def _save_deposits(self) -> None:
        # atomic: this file is the only record of which accounts hold deposited money
        self._write_private_json(self.deposits_path,
                                 {"deposits": [d.to_dict() for d in self.deposits.values()]})

    def _load_sandboxes(self) -> None:
        """Sandboxes are real on-chain accounts: survive a restart or they are lost money."""
        self.sandboxes = {}
        try:
            raw = json.loads(self.sandboxes_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        self._load_seals("sandbox", raw)
        for row in raw.get("sandboxes", []):
            try:
                self.sandboxes[row["sandbox_id"]] = Sandbox(
                    sandbox_id=row["sandbox_id"], kind=row["kind"], asset=row["asset"],
                    account_index=int(row["account_index"]), address=row["address"],
                    budget=Decimal(str(row["budget"])), spent=Decimal(str(row.get("spent", "0"))),
                    created_at=row.get("created_at", now_iso()),
                    expires_at=row.get("expires_at", now_iso()),
                    label=row.get("label", ""),
                    allowed_profiles=list(row.get("allowed_profiles") or []),
                    allowed_payees=list(row.get("allowed_payees") or []),
                    enabled=bool(row.get("enabled", False)),
                    mode=str(row.get("mode", "budget")),
                    self_destruct_seconds=int(row.get("self_destruct_seconds", 1800) or 1800),
                    spent_at=str(row.get("spent_at", "")),
                    destroy_at=str(row.get("destroy_at", "")),
                    destroyed_at=str(row.get("destroyed_at", "")),
                    destroy_blocked=str(row.get("destroy_blocked", "")),
                    last_balance=str(row.get("last_balance", "")),
                    revoked_at=str(row.get("revoked_at", "")),
                    unattended=bool(row.get("unattended", False)),
                    armed_at=str(row.get("armed_at", "")),
                    tampered=bool(row.get("tampered", False)),
                )
            except (KeyError, ValueError):
                continue

    def _save_sandboxes(self) -> None:
        rows = [s.to_dict() for s in self.sandboxes.values()]
        self._write_private_json(self.sandboxes_path, self._sealed_file("sandbox", "sandboxes", rows))

    def _build_adapters(self) -> None:
        self._chain_status_cache = None        # the adapter set is what chain_status describes
        """Construct a live adapter per enabled chain. No adapter may read the keystore."""
        self._invalidate_balances()
        self.adapters = {}
        self.chain_notes = {}
        self._chain_status_cache = None
        self._ensure_chain_deps()
        if os.environ.get("WALLETD_DISABLE_CHAINS") == "1":
            return
        wallet = (os.environ.get("AGENT_WALLET_MONERO_WALLET")
                  or self.settings.monero_wallet_file or "agent-wallet")
        # One source of truth: the password written next to the keystore. Reading only the
        # environment meant an ad-hoc start (no systemd EnvironmentFile) opened the wallet with an
        # empty password — "invalid password", every chain offline, silently.
        password = self._signer_password()
        for kind in self.settings.chains_enabled:
            if kind == "sim-mainnet":
                creds = signer_mod.credentials(self.home)
                adapter = XmrAdapter("sim-mainnet", self.settings.monero_rpc_url, wallet, password,
                                     provider="simulated", simulate=True,
                                     rpc_user=creds["user"], rpc_password=creds["password"],
                                     home=self.home)
                try:
                    usable = adapter.available()
                    note = "wallet RPC unreachable" if usable is False else ""
                except Exception as exc:  # noqa: BLE001 - report why, never swallow the reason
                    usable, note = False, f"{type(exc).__name__}: {str(exc)[:120]}"
                if usable:
                    self.adapters[kind] = adapter
                else:
                    self.chain_notes[kind] = note
                continue
            if kind.startswith(("base", "polygon")):
                try:
                    adapter = EvmAdapter(kind, self.chain_secret("evm"),
                                         rpc_url=os.environ.get(f"AGENT_WALLET_RPC_{kind.upper().replace('-', '_')}", ""))
                    usable, note = adapter.available(), ""
                except Exception as exc:  # noqa: BLE001
                    usable, note = False, f"{type(exc).__name__}: {str(exc)[:120]}"
                if usable:
                    self.adapters[kind] = adapter
                else:
                    self.chain_notes[kind] = note or "EVM RPC unreachable"
                continue
            if kind.startswith("bitcoin"):
                try:
                    adapter = BtcAdapter(kind, self.chain_secret("btc"),
                                         api_url=os.environ.get(f"AGENT_WALLET_ESPLORA_{kind.upper().replace('-', '_')}", ""))
                    usable, note = adapter.available(), ""
                except Exception as exc:  # noqa: BLE001
                    usable, note = False, f"{type(exc).__name__}: {str(exc)[:120]}"
                if usable:
                    self.adapters[kind] = adapter
                else:
                    self.chain_notes[kind] = note or "Esplora unreachable"
                continue
            if kind.startswith("xrp"):
                try:
                    adapter = XrpAdapter(kind, self.chain_secret("xrp"),
                                         rpc_url=os.environ.get(f"AGENT_WALLET_XRP_{kind.upper().replace('-', '_')}", ""))
                    usable, note = adapter.available(), ""
                except Exception as exc:  # noqa: BLE001
                    usable, note = False, f"{type(exc).__name__}: {str(exc)[:120]}"
                if usable:
                    self.adapters[kind] = adapter
                else:
                    self.chain_notes[kind] = note or "XRPL node unreachable"
                continue
            if kind.startswith("monero"):
                creds = signer_mod.credentials(self.home)
                adapter = XmrAdapter(kind, self.settings.monero_rpc_url, wallet, password,
                                     provider=self.settings.monero_provider,
                                     rpc_user=creds["user"], rpc_password=creds["password"],
                                     home=self.home)
                try:
                    usable = adapter.available()
                    note = ""
                except Exception as exc:  # noqa: BLE001
                    usable, note = False, f"{type(exc).__name__}: {str(exc)[:120]}"
                if usable:
                    self.adapters[kind] = adapter
                else:
                    creds2 = signer_mod.credentials(self.home, create=False)
                    rpc_up = JsonRpcProbe(self.settings.monero_rpc_url,
                                          creds2["user"], creds2["password"]).ok()
                    self.chain_notes[kind] = note or (
                        "the signer is not answering — `hermes wallet signer start`" if not rpc_up
                        else "the signer is up but cannot open the wallet file (wrong password, or "
                             "the wallet was created elsewhere)")
        if self.adapters:
            try:
                self._cache_addresses()
            except Exception:  # noqa: BLE001
                pass

    def nodes(self) -> Dict[str, Any]:
        """Public-node health for every enabled chain (no local chain data anywhere)."""
        return {"nodes": node_registry.health(list(self.settings.chains_enabled))}

    def _heal_signer(self) -> None:
        """One rate-limited attempt to bring the signer back when it died under a live daemon.

        Nothing watched the child: if `monero-wallet-rpc` crashed (or was killed), the daemon kept
        serving and every XMR chain reported "connection refused" until the operator happened to
        unlock again. A read that needs the signer now tries once to restart it.
        """
        if os.environ.get("WALLETD_DISABLE_CHAINS") == "1" or not self.is_unlocked():
            return
        monero_kinds = [k for k in self.settings.chains_enabled if k.startswith(("monero", "sim-"))]
        if not monero_kinds:
            return
        if not signer_mod.binary_path(self.home).exists():
            return
        import time as _time

        creds = signer_mod.credentials(self.home)
        answering = JsonRpcProbe(self.settings.monero_rpc_url, creds["user"], creds["password"]).ok()
        if answering:
            # Healthy (possibly restarted already): rebuild only if some chain has no adapter.
            if not all(kind in self.adapters for kind in monero_kinds):
                with self.mutex:
                    self._build_adapters()
            return
        # Not answering: the adapters still in the map are stale. Drop them so the rows tell the
        # truth, then try one rate-limited restart.
        for kind in monero_kinds:
            self.adapters.pop(kind, None)
        now = _time.time()
        if now - self._last_heal < 20:      # do not hammer a signer that is genuinely broken
            return
        self._last_heal = now
        try:
            self.ensure_signer({})
        except Exception:  # noqa: BLE001 - a failed heal is reported by the chain rows, not raised
            return
        with self.mutex:
            self._build_adapters()

    def _invalidate_balances(self) -> None:
        self._balances_cache = None
        # adapters are rebuilt on unlock and dropped on lock: a cached sandbox balance from before
        # either event describes a chain connection that no longer exists
        self._sandbox_balances_cache = {}

    BALANCE_TTL_SECONDS = 60.0      # a poll must not pay the per-chain network cost every few seconds

    def balances(self, ttl_seconds: Optional[float] = None, fresh: bool = False) -> Dict[str, Any]:
        """Per-chain address + balance, from the live adapter only (never from a cached number).

        When the wallet is locked there is nothing to read, but saying "no chain is reachable" is a
        lie: the chains are fine, the signer just cannot decrypt yet. Each row carries a `state` so
        the UI can say the true thing.

        Every *enabled* chain gets a row, always — a locked wallet, a chain with no signer adapter
        and a chain whose read raised all appear, with `available: false` and the reason. The caller
        asked for that chain; dropping its row turns "we could not ask" into "you hold nothing". A
        disabled chain is not in `chains_enabled` and so gets no row: it is not shown as zero either.
        """
        ttl = self.BALANCE_TTL_SECONDS if ttl_seconds is None else ttl_seconds
        cached_row = self._balances_cache
        if not fresh and cached_row is not None \
                and (datetime.now(timezone.utc) - cached_row[0]).total_seconds() < ttl:
            enabled = list(self.settings.chains_enabled)
            accounts = [row for row in cached_row[1].get("accounts") or []
                        if str(row.get("kind", "")) in enabled]
            # Chains enabled since the cache was filled have no row in it; fill them and keep the
            # cached numbers. Serving a payload that lacks an enabled chain's row is the lie this
            # method exists to avoid — and so is keeping a row for a chain that was just disabled.
            note = self._missing_chain_rows(accounts)
            if not note and len(accounts) == len(cached_row[1].get("accounts") or []):
                return dict(cached_row[1])
            payload = {**cached_row[1], "accounts": self._ordered_rows(accounts + note)}
            return payload
        if not self.is_unlocked():
            cached = self._cached_addresses()
            rows = []
            for kind in self.settings.chains_enabled:
                for asset in self._chain_assets(kind):
                    row = {"kind": kind, "asset": asset, "balance": None,
                           "available": False, "state": "locked", "error": "locked",
                           "simulated": kind.startswith("sim-")}
                    if kind in cached:
                        row["address"] = cached[kind]  # public data: funding works while locked
                    rows.append(row)
            payload = {"accounts": rows, "state": "locked", "addresses": cached}
            self._balances_cache = (datetime.now(timezone.utc), payload)
            return payload

        self._heal_signer()

        def read_chain(kind: str, adapter: Any) -> List[Dict[str, Any]]:
            """One chain's rows. Runs in a thread: the chains are independent HTTP calls."""
            base: Dict[str, Any] = {"kind": kind, "simulated": kind.startswith("sim-")}
            # Token metadata for the pane's deposit QR: without the contract address a USDC row can
            # only encode `ethereum:<wallet>`, which is a *native* transfer — the wrong asset.
            from .chains.evm import CHAINS as _EVM_CHAINS
            evm_row = _EVM_CHAINS.get(kind) or {}
            tokens = evm_row.get("tokens") or {}
            if tokens:
                base["tokens"] = {asset: {"address": address, "decimals": decimals}
                                  for asset, (address, decimals) in tokens.items()}
                base["chain_id"] = evm_row.get("chain_id")
            out: List[Dict[str, Any]] = []
            try:
                provider = adapter.status().get("provider")
                if provider:
                    base["provider"] = provider
                base["address"] = adapter.address()
                base["available"] = True
                balance_all = getattr(adapter, "balance_all", None)
                if balance_all is not None:
                    # An EVM chain holds every token it knows; only the enabled ones are this
                    # wallet's coins, or adding ETH silently adds USDC to the page as well.
                    # The amount is copied out digit for digit: no rounding, no formatting.
                    # (Name the coin `symbol`: `asset` is the row key built for the QR and the
                    # failure path, and reusing it as the loop variable is how an earlier version
                    # came to filter on the amount instead of the coin.)
                    for symbol, amount in balance_all().items():
                        if symbol not in self.settings.assets_enabled:
                            continue
                        out.append({**base, "asset": symbol, "balance": f"{amount:f}"})
                else:
                    base["asset"] = adapter.asset
                    base["balance"] = f"{adapter.balance():f}"
                    out.append(base)
            except Exception as exc:                      # noqa: BLE001 - a dead chain must not break the page
                read = [{**row} for row in out if row.get("balance") is not None]
                # No balance at all: 0.0000 reads as "this wallet is empty", which is a different
                # (and wrong) statement from "we could not ask".
                base["available"] = False
                base["balance"] = None
                base["asset"] = getattr(adapter, "asset", "") or self._chain_assets(kind)[0]
                base["error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
                # An EVM read answers per coin: whatever was already read is a real number, and it
                # is kept beside the failed row rather than thrown away with it.
                return read + [base]
            # Nothing read: every coin on this chain is disabled. Return [] — the caller fills one
            # "not read" row per enabled coin so the chain itself never vanishes from the page.
            return out

        rows: List[Dict[str, Any]] = []
        adapters = list(self.adapters.items())
        if len(adapters) > 1:
            # wall clock is the slowest chain, not the sum: four live chains measured 3.26 s serially
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=min(8, len(adapters))) as pool:
                for chunk in pool.map(lambda item: read_chain(*item), adapters):
                    rows.extend(chunk)
        else:
            for item in adapters:
                rows.extend(read_chain(*item))
        # Whatever the adapter map could not answer still gets a row. Before this, a chain the
        # signer had not started yet (no adapter, no note) vanished from the page entirely, and the
        # panes that read `accounts` showed a wallet with fewer coins than the operator enabled.
        rows.extend(self._missing_chain_rows(rows, locked=False))
        payload = {"accounts": self._ordered_rows(rows)}
        self._balances_cache = (datetime.now(timezone.utc), payload)
        return payload

    #: A note row used to say "signer" whatever the reason, and it carried `balance: "0"` — so a
    #: chain nobody could ask about was printed as an empty wallet. "signer" is kept only for the
    #: signer itself being down; the other reasons get their own word.
    @staticmethod
    def _note_state(kind: str, note: str) -> str:
        """The `state` a recorded reason implies."""
        text = str(note or "").lower()
        if kind.startswith(("monero", "sim-")) and "signer" in text:
            return "signer"
        if "not supported" in text or "unknown_chain" in text:
            return "unsupported"
        return "error"

    def _chain_known(self, kind: str) -> bool:
        """Does this build have an adapter for `kind`? Checked without touching the network."""
        if kind.startswith("sim-"):
            return True
        if kind in self.NATIVE_ASSET:
            return True
        return any(kind in chains for chains in self.ASSET_CHAINS.values())

    def _missing_chain_rows(self, rows: List[Dict[str, Any]], locked: bool = False) -> List[Dict[str, Any]]:
        """Rows for enabled chains with no row in `rows`, and for enabled coins missing from a chain
        that did produce rows. A chain the caller was told is enabled must never simply vanish.

        Every row here says "not read" in one way or another: `balance` is None and `state` says why
        (locked, an unsupported network, a recorded reason, or a missing signer). A zero would be a
        different claim — "this wallet is empty" — and nothing here read a zero off a chain.
        """
        seen_chains = {str(row.get("kind", "")) for row in rows}
        seen_pairs = {(str(row.get("kind", "")), str(row.get("asset", "")).upper()) for row in rows}
        notes = getattr(self, "chain_notes", {}) or {}
        # Addresses are public: a row that could not be read (locked, signer down) still can be
        # funded, so carry the last known address when there is one.
        cached = self._cached_addresses() if locked is not None else {}
        out: List[Dict[str, Any]] = []
        for kind in self.settings.chains_enabled:
            if kind in seen_chains:
                # The chain produced at least one row; only its other coins may be missing.
                for asset in self._chain_assets(kind):
                    if (kind, asset.upper()) in seen_pairs:
                        continue
                    # Pressing on would show a zero for a number nobody read.
                    out.append({"kind": kind, "asset": asset, "available": False, "balance": None,
                                "state": "error", "simulated": kind.startswith("sim-"),
                                "error": f"{asset} was not read from {kind}"})
                    seen_pairs.add((kind, asset.upper()))
                continue
            if not self._chain_known(kind):
                # Enabled by hand, in a build that has no adapter for it: say so, once.
                out.append({"kind": kind, "asset": kind.split("-", 1)[0].upper(),
                            "available": False, "balance": None, "state": "unsupported",
                            "error": f"{kind} is not supported by this build",
                            "simulated": False})
                continue
            note = notes.get(kind)
            for asset in self._chain_assets(kind):
                if note:
                    state, error = self._note_state(kind, note), note
                elif locked:
                    state, error = "locked", "locked"
                else:
                    state, error = "error", "no signer adapter for this chain"
                    if kind.startswith(("monero", "sim-")):
                        error = "the signer is not answering — `hermes wallet signer start`"
                row = {"kind": kind, "asset": asset, "available": False, "balance": None,
                       "state": state, "error": error, "simulated": kind.startswith("sim-")}
                address = cached.get(kind)
                if address:
                    row["address"] = address      # public data: funding works while locked
                out.append(row)
                seen_pairs.add((kind, asset.upper()))
        return out

    def _ordered_rows(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Rows in the order the operator enabled the chains, one per (chain, coin)."""
        order = {kind: i for i, kind in enumerate(self.settings.chains_enabled)}
        return sorted(rows, key=lambda row: (order.get(str(row.get("kind", "")), len(order)),
                                             str(row.get("asset", ""))))

    def _ensure_chain_deps(self) -> None:
        """Make in-process signing libraries findable without Hermes having to install them.

        They are declared in `plugin.yaml` so a catalog install resolves them, but a hand-installed
        tree (or a service unit on a different interpreter) may not have them. Looking beside the
        wallet home keeps this self-contained: drop the wheels in `<wallet home>/pylibs`.
        """
        import sys

        libs = self.home / "pylibs"
        if libs.is_dir() and str(libs) not in sys.path:
            sys.path.insert(0, str(libs))

    def _chain_assets(self, kind: str) -> list:
        """Assets a chain can hold: native + tokens for EVM, one for the account chains.

        Sniffing the chain id here duplicated NATIVE_ASSET and had already drifted (arbitrum-mainnet
        reported XMR). The native coin comes from NATIVE_ASSET; EVM tokens come from the EVM table.
        """
        if kind.startswith(("base", "polygon", "arbitrum")):
            from .chains.evm import CHAINS as EVM_CHAINS

            spec = EVM_CHAINS.get(kind, {})
            return [self.NATIVE_ASSET.get(kind, spec.get("native", "ETH")), *spec.get("tokens", {})]
        return [self.NATIVE_ASSET.get(kind, "XMR")]

    def chain_status(self, ttl_seconds: float = 20.0) -> Dict[str, Any]:
        """Every configured chain, with why it cannot be reached right now.

        Returning only the adapters that happen to exist made a locked wallet report `chains: []`,
        which reads as "this wallet has no coins" — the opposite of the truth.
        """
        # Each adapter answers by asking a node for its height: ~1.9 s for the four enabled chains in
        # the benchmark, all of it inside the daemon lock. A 20 s cache turns the pane/CLI poll into
        # a memory read and keeps the lock free for payments.
        cached = getattr(self, "_chain_status_cache", None)
        if cached is not None and (time.monotonic() - cached[0]) < ttl_seconds:
            return dict(cached[1])
        self._heal_signer()
        chains = [a.status() for a in self.adapters.values()]
        seen = {str(row.get("kind", "")) for row in chains}
        notes = getattr(self, "chain_notes", {}) or {}
        locked = not self.is_unlocked()
        for kind in self.settings.chains_enabled:
            if kind in seen:
                continue
            chains.append({
                "kind": kind,
                "available": False,
                "state": "locked" if locked else "unavailable",
                "error": notes.get(kind) or (
                    "the wallet is locked; unlock it to reach this chain" if locked
                    else "no signer adapter for this chain"),
            })
        payload = {"chains": chains}
        self._chain_status_cache = (time.monotonic(), dict(payload))
        return payload

    def lock(self) -> Dict[str, Any]:
        """Locking must actually take the keys away, not just forget the keystore.

        The XMR spend key lives inside `monero-wallet-rpc`, and the BTC/EVM keys sit in the adapters
        we built while unlocked. Clearing `master` alone left all three usable, while the CLI said
        "drop the key from memory".
        """
        self._invalidate_balances()      # a cached balance must not outlive the key
        self.data_key = None             # and neither may the key that re-seals the keystore
        stopped = "not_running"
        proc = self._signer_proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=8)
                stopped = "stopped"
            except Exception:  # noqa: BLE001 - a wedged signer must not block locking
                try:
                    proc.kill()
                    stopped = "killed"
                except Exception:  # noqa: BLE001
                    stopped = "unreachable"
            self._signer_proc = None
        self.adapters = {}
        self.chain_notes = {}
        self.plans = {}
        if self.ledger is not None:
            self.ledger.append("wallet", "locked", "operator", {"signer": stopped})
        self.payload = None
        self.master = None
        self.ledger = None
        self.unlocked_at = None
        return {"ok": True, "signer": stopped}

    def _autolock_seconds(self) -> int:
        """Auto-lock window in seconds; 0 disables it."""
        minutes = int(getattr(self.settings, "autolock_minutes", 0) or 0)
        return max(0, minutes * 60)

    def unlock_expires_in(self) -> Optional[int]:
        """Seconds until the wallet locks itself, or None when unlocked indefinitely."""
        ttl = self._autolock_seconds()
        if not ttl or self.unlocked_at is None or self.master is None:
            return None
        elapsed = (datetime.now(timezone.utc) - self.unlocked_at).total_seconds()
        return max(0, int(ttl - elapsed))

    def is_unlocked(self) -> bool:
        if self.master is None or self.unlocked_at is None:
            return False
        ttl = self._autolock_seconds()
        if ttl and (datetime.now(timezone.utc) - self.unlocked_at).total_seconds() > ttl:
            self.lock()
            return False
        return True

    # ---------- the PIN: one payment, one card, one secret ----------
    def _pin_state(self) -> Dict[str, Any]:
        """The failure counter, MAC'd with the master key.

        A plain JSON counter can be reset by anything running as this uid — which is the same attacker
        the PIN exists to stop, because they can also read the operator token. A MAC that only the
        unlocked daemon can compute makes a reset detection, and detection means locked out.
        """
        try:
            row = json.loads(self.pin_state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"fails": 0, "locked_until": ""}
        mac = str(row.pop("mac", ""))
        expected = self._pin_mac(row)
        if not mac or not expected or not hmac.compare_digest(mac, expected):
            # tampered (or written by an older build): treat it as a lockout, not a reset
            return {"fails": PIN_MAX_ATTEMPTS,
                    "locked_until": (datetime.now(timezone.utc) + timedelta(seconds=3600))
                    .strftime("%Y-%m-%dT%H:%M:%SZ")}
        return row

    def _pin_mac(self, row: Dict[str, Any]) -> str:
        if not self.master:
            return ""
        payload = json.dumps(row, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hmac.new(self.master, payload, hashlib.sha256).hexdigest()

    def _write_private_json(self, path: Path, payload: Dict[str, Any]) -> None:
        """temp + fsync + replace, 0600. A half-written PIN counter read back as `fails: 0`."""
        compat.write_json_atomic(path, payload)

    def _save_pin_state(self, row: Dict[str, Any]) -> None:
        # atomic: a crash mid-write used to leave a truncated file, which reads back as `fails: 0` —
        # i.e. a power cut during a lockout could hand the attacker a fresh five guesses
        self._write_private_json(self.pin_state_path, {**row, "mac": self._pin_mac(row)})

    def pin_status(self) -> Dict[str, Any]:
        stored = ((self.payload or {}).get("pin") or {})
        if self.master is None:
            # The counter is MAC'd with the master key, so a locked daemon cannot read it — and
            # `_pin_state` treats "cannot verify" as a lockout. That is right for a PIN attempt and
            # wrong for a status line: the pane showed "locked for an hour" on every locked wallet
            # that had ever seen one wrong PIN.
            # `set` used to be None here, which made the pane offer the passphrase field on every
            # locked wallet — the one moment the PIN exists for. Whether a PIN *slot* is present is
            # public (it is a key in the file, not a secret), so answer it from the envelope. What
            # still cannot be answered while locked is the attempt counter, which is MAC'd.
            has_slot = False
            try:
                has_slot = keystore.has_pin(keystore.load(self.keystore_path))
            except keystore.KeystoreError:
                has_slot = False
            return {"set": has_slot, "unlocks": has_slot,
                    "required": bool(self.settings.pin_every_payment),
                    "created_at": None, "updated_at": None, "attempts_left": None,
                    "locked_until": "", "lockout_seconds": 0, "wallet_locked": True}
        row = self._pin_state()
        fails = int(row.get("fails") or 0)
        try:
            unlocks = keystore.has_pin(keystore.load(self.keystore_path))
        except keystore.KeystoreError:
            unlocks = False
        return {"set": bool(stored.get("hash")),
                # `set` is "a PIN exists"; `unlocks` is "that PIN opens the file". They differ for one
                # release after a v2 upgrade, until the operator sets the PIN again.
                "unlocks": unlocks,
                "required": bool(self.settings.pin_every_payment),
                "created_at": stored.get("created_at"),
                "updated_at": stored.get("updated_at"),
                "attempts_left": max(0, PIN_MAX_ATTEMPTS - fails),
                "locked_until": row.get("locked_until") or "",
                "lockout_seconds": _pin_lockout_seconds(fails)}

    def set_pin(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Set or replace the six-digit PIN. The passphrase is required: the PIN must not be
        changeable by anything holding only a token."""
        self._require_unlocked()
        pin = str(params.get("pin") or "").strip()
        if not re.fullmatch(r"\d{6}", pin):
            raise WalletError("bad_pin_format", "the PIN is exactly six digits")
        confirm = params.get("confirm")
        if confirm is not None and str(confirm).strip() != pin:
            raise WalletError("pin_mismatch", "the two PINs differ")
        # Either secret proves the operator is the operator. The passphrase is the way in for a wallet
        # with no PIN yet; once a PIN exists it is a full credential (it opens the wallet), so demanding
        # the passphrase to change it would just trap whoever only remembers the six digits. A wrong
        # current PIN is counted like any other wrong PIN.
        passphrase = str(params.get("passphrase") or "")
        current_pin = str(params.get("current_pin") or "")
        previous = bool(((self.payload or {}).get("pin") or {}).get("hash"))
        if passphrase:
            try:
                keystore.open_full(keystore.load(self.keystore_path), passphrase)
            except keystore.InvalidPassphrase as exc:
                raise WalletError("bad_passphrase", "passphrase does not match the keystore") from exc
        elif current_pin:
            if not previous:
                raise WalletError("pin_not_set", "no PIN is set yet; use the passphrase")
            self._verify_pin({"pin": current_pin})
        elif previous:
            raise WalletError("passphrase_required",
                              "changing the PIN needs the wallet passphrase or the current PIN")
        else:
            raise WalletError("passphrase_required",
                              "the first PIN needs the wallet passphrase — it is the higher secret")
        payload = dict(self.payload or {})
        data_key = self.data_key
        if data_key is None:
            raise WalletError("locked", "wallet is locked; an operator must unlock it")
        envelope = keystore.load(self.keystore_path)
        payload["pin"] = keystore.make_pin(pin)
        # The PIN is a way *into* the file, not only a signature on a payment: wrap the data key with it
        # so the operator can unlock with the six digits they already type, and keep the passphrase
        # wrapper as the stronger, portable key. Both open the same payload.
        envelope = keystore.reseal(envelope, payload, data_key)
        keystore.write_atomic(self.keystore_path,
                              keystore.with_pin(envelope, pin, data_key,
                                               pepper=self._device_key(create=True)))
        self.data_key = data_key
        self.payload = payload
        self._save_pin_state({"fails": 0, "locked_until": ""})
        self._record("wallet", "pin_changed", "operator",
                     {"replaced": previous, "digits": keystore.PIN_DIGITS,
                      "authorised_by": "passphrase" if passphrase else "pin"})
        return {"pin": self.pin_status()}

    def change_passphrase(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Re-seal the keystore under a new passphrase. The wallet does not change; the key that opens it does.

        Nothing else is re-derived: the master key, the ledger seed, the chain keys and the Monero wallet
        password all live *inside* the sealed payload, so re-sealing the same payload with a new
        passphrase leaves every one of them intact. That is why this is a small method and not a
        migration — and why it must never be reachable without the current passphrase.
        """
        self._require_unlocked()
        current = str(params.get("passphrase") or "")
        new = str(params.get("new_passphrase") or "")
        confirm = params.get("confirm")
        if not current:
            raise WalletError("passphrase_required", "the current passphrase is required")
        if len(new) < 8:
            raise WalletError("bad_passphrase_format",
                              "a passphrase is at least 8 characters — it is the key to this wallet")
        if confirm is not None and str(confirm) != new:
            raise WalletError("passphrase_mismatch", "the two new passphrases differ")
        if new == current:
            raise WalletError("passphrase_unchanged", "that is already the passphrase")
        envelope = keystore.load(self.keystore_path)
        try:
            payload, data_key = keystore.open_full(envelope, current)
        except keystore.InvalidPassphrase as exc:
            raise WalletError("bad_passphrase", "passphrase does not match the keystore") from exc
        # Only the passphrase wrapper is replaced. The PIN wrapper holds the same data key, so the
        # operator does not lose PIN unlock by changing the passphrase — and does not need the old PIN
        # to do it.
        keystore.write_atomic(self.keystore_path,
                              keystore.with_passphrase(keystore.reseal(envelope, payload, data_key),
                                                       new, data_key))
        self.data_key = data_key
        self.payload = payload
        # The secret itself never goes in the ledger: a receipt is readable while locked.
        self._record("wallet", "passphrase_changed", "operator", {"length": len(new)})
        return {"changed": True}

    def _verify_pin(self, params: Dict[str, Any]) -> None:
        """Refuse unless the human typed the right PIN — every payment, no exceptions.

        The PIN is checked here, in the daemon, against a scrypt hash inside the sealed payload. The
        agent cannot read the hash, cannot type the PIN, and cannot reach this method on its token.
        Wrong entries are counted and locked out, because six digits are trivially brute-forceable
        without a rate limit.
        """
        with PIN_LOCK:
            self._verify_pin_locked(params)

    def _verify_pin_locked(self, params: Dict[str, Any]) -> None:
        stored = ((self.payload or {}).get("pin") or {})
        if not stored.get("hash"):
            raise WalletError("pin_not_set",
                              "no PIN is set yet: set one in the Wallet pane (or `hermes wallet pin set`)")
        row = self._pin_state()
        locked_until = str(row.get("locked_until") or "")
        if locked_until and locked_until > now_iso():
            raise WalletError("pin_locked",
                              f"too many wrong PINs; this is locked until {locked_until}")
        pin = str(params.get("pin") or "").strip()
        if not pin:
            # A pane built before the card grew a PIN field posts the six digits as `passphrase`.
            # A passphrase is at least eight characters, so a six-digit value cannot be one: read it
            # as the PIN rather than telling the operator their PIN "does not work". Same secret,
            # same counter, same lockout — nothing here is weaker than the `pin` field.
            legacy = str(params.get("passphrase") or "").strip()
            if re.fullmatch(r"\d{6}", legacy):
                pin = legacy
        if not pin:
            raise WalletError("pin_required", "type the PIN to authorise this payment")
        if not keystore.check_pin(stored, pin):
            fails = int(row.get("fails") or 0) + 1
            locked = ""
            seconds = _pin_lockout_seconds(fails)
            if seconds:
                locked = (datetime.now(timezone.utc) + timedelta(seconds=seconds)) \
                    .strftime("%Y-%m-%dT%H:%M:%SZ")
            self._save_pin_state({"fails": fails, "locked_until": locked})
            self._record("wallet", "pin_refused", "operator",
                         {"fails": fails, "locked_until": locked})
            if locked:
                raise WalletError("pin_locked", f"wrong PIN; locked for {seconds}s")
            left = max(0, PIN_MAX_ATTEMPTS - fails)
            raise WalletError("bad_pin", f"wrong PIN ({left} attempt{'s' if left != 1 else ''} left)")
        if int(row.get("fails") or 0):
            self._save_pin_state({"fails": 0, "locked_until": ""})

    def _require_passphrase(self, params: Dict[str, Any], why: str = "") -> None:
        """Re-verify the operator's passphrase against the keystore.

        An operator *token* is not a human: tokens sit on disk at 0600 and anything running as this
        uid can read them. Every action that changes what the wallet will do without asking again
        (caps, allowlists, mandates, contact addresses) has to be authorised by the secret itself.
        """
        raw = params.get("passphrase")
        passphrase = "" if raw is None else str(raw)
        if not passphrase:
            raise WalletError("passphrase_required",
                              f"this change needs the wallet passphrase{(' — ' + why) if why else ''}")
        try:
            keystore.open_sealed(keystore.load(self.keystore_path), passphrase)
        except keystore.InvalidPassphrase as exc:
            raise WalletError("bad_passphrase", "passphrase does not match the keystore") from exc
        except keystore.NewerKeystore as exc:
            raise WalletError("keystore_too_new", str(exc)) from exc
        except keystore.CorruptKeystore as exc:
            raise WalletError("corrupt_keystore", "keystore is unreadable") from exc

    def _require_unlocked(self) -> None:
        if not self.is_unlocked():
            raise WalletError("locked", "wallet is locked; an operator must unlock it")

    # ---------- views ----------

    def _pin_set(self) -> Optional[bool]:
        """Whether a PIN slot exists, answerable while locked.

        The keystore names its slots in the clear — only the wrapped key inside them is secret —
        so this is not a disclosure. Answering None while locked made the pane send a six-digit PIN
        out as a passphrase, which is why the PIN "stopped working" after every auto-lock.
        """
        if self.is_unlocked():
            return bool(((self.payload or {}).get("pin") or {}).get("hash"))
        try:
            return bool(keystore.has_pin(keystore.load(self.keystore_path)))
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def status(self) -> Dict[str, Any]:
        return {
            "version": VERSION,
            "locked": not self.is_unlocked(),
            "initialized": self.exists(),
            "home": str(self.home),
            "human_gate": self.settings.human_gate,
            "config_signed": not self.config_tampered,
            "config_tampered": self.config_tampered,
            "chains": list(self.settings.chains_enabled),
            "assets": list(self.settings.assets_enabled),
            "available_assets": list(self.ASSET_CHAINS),      # everything the pane may offer
            "asset_chains": {k: list(v) for k, v in self.ASSET_CHAINS.items()},
            "native_asset": dict(self.NATIVE_ASSET),
            "default_chain": self.DEFAULT_CHAIN,          # coin -> its default network
            "chain_assets": {kind: self._chain_assets(kind) for kind in self.settings.chains_enabled},
            # the rules the agent is playing by, readable so it can plan instead of guessing
            "caps": {"per_tx": dict(self.settings.per_tx_cap), "daily": dict(self.settings.daily_cap),
                     "fees": dict(self.settings.fee_cap)},
            "monero_only": bool(self.settings.monero_only),
            "prefer_monero": bool(self.settings.prefer_monero),
            "preferred_asset": str(self.settings.prefer_asset or "").upper(),
            "payee_allowlist": list(self.settings.payee_allowlist),
            "unattended_policy": self.settings.unattended_policy,
            "approval_ttl_seconds": self.settings.approval_ttl_seconds,
            "autolock_minutes": int(getattr(self.settings, "autolock_minutes", 0) or 0),
            "unlock_expires_in": self.unlock_expires_in(),
            "signer_installed": signer_mod.binary_path(self.home).exists(),
            "signer_reachable": self._signer_answered(),
            # `set` is answerable while locked: the keystore names its slots in the clear, and whether
            # a PIN exists is not a secret. It used to be None until unlock, and the pane reads None
            # as "no PIN" — so after every auto-lock the six digits went out as a *passphrase* and
            # the PIN looked broken. `unlocks` is the field the pane checks first.
            "pin": {"required": bool(self.settings.pin_every_payment),
                    "set": self._pin_set(),
                    "unlocks": self._pin_set()},
            "ledger_archives": len(list(self.home.glob("ledger-*.jsonl"))),
            # only when it is true: a status that always carries the key costs tokens on every turn
            **({"ledger_rewound": self.ledger_rewound} if self.ledger_rewound else {}),
            "ledger_pubkey": self.ledger.pubkey_hex if self.ledger else None,
            "pending_requests": sum(1 for r in self.requests.values() if r.status == "pending"),
            "prompt": dict(self.prompt) if self.prompt else None,
            "active_mandates": sum(1 for m in self.mandates.values() if m.status() == "active"),
            # The profile list rides on /status on purpose: the dashboard imports a plugin's routes
            # once at startup, so a pane talking to an older backend cannot reach a newly added
            # route at all - but /status has existed since the first version.
            "profiles": self.profiles_list()["profiles"],
            # Which chains already have a wallet. The pane used to offer "create a wallet" on every
            # visit, even with a funded one sitting in the next card. Cached addresses survive a lock,
            # so this answers without the signer.
            "wallets": {kind: bool(self._cached_addresses().get(kind))
                        for kind in self.settings.chains_enabled},
        }

    def request_operator_prompt(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Ask the human, through the Wallet pane, for the one thing an agent must never handle.

        The passphrase cannot travel through chat: it would be in the transcript, in the model's
        context and in any logs. So the agent raises a prompt, the pane shows it with the passphrase
        field focused, and the secret never leaves that window.
        """
        request_id = str(params.get("request_id", "")).strip()
        req = self.requests.get(request_id) if request_id else None
        if request_id and req is None:
            raise WalletError("unknown_request", "request id is not recognised")
        if req is not None and req.status != "pending":
            return {"prompt": None, "note": f"that request is already {req.status}"}
        message = str(params.get("message", "")).strip()[:200]
        # Without a request id there is nothing to approve: a message-only prompt is a notification,
        # not an unlock. Labelling it "unlock" showed the human an Unlock button for a payment the
        # agent had described in prose - the one thing they could not act on.
        if req is not None:
            kind = "approval"
        elif str(params.get("kind", "")) in ("unlock", "note"):
            kind = str(params.get("kind"))
        else:
            kind = "note" if message else "unlock"
        self.prompt = {
            "kind": kind,
            "request_id": request_id,
            "message": message or (f"Approve {req.amount} {req.asset} to {req.payee[:16]}…" if req else
                                   "Unlock the wallet"),
            "created_at": now_iso(),
        }
        return {"prompt": dict(self.prompt),
                "note": "The Wallet pane is showing this prompt; the passphrase is typed there and "
                        "never reaches you."}

    def clear_operator_prompt(self) -> Dict[str, Any]:
        self.prompt = None
        return {"prompt": None}

    def config_get(self) -> Dict[str, Any]:
        return {"settings": self.settings.to_dict(), "tampered": self.config_tampered}

    def accounts(self) -> Dict[str, Any]:
        accounts = (self.payload or {}).get("accounts", {})
        return {"accounts": [{"kind": k, **v} for k, v in accounts.items()]}

    # ---------- requests ----------

    def _context(self, req: SpendRequest, mandate: Optional[Mandate],
                 agent_initiated: bool = False) -> SpendContext:
        if self.ledger is None:
            return SpendContext(unattended=True, ledger_available=False)
        now = datetime.now(timezone.utc)
        day_start = now.strftime("%Y-%m-%dT00:00:00Z")
        if mandate is not None:
            window_start = (now - timedelta(hours=mandate.period_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
            # An autopay's caps are for its own spending: floor both windows at its creation, or
            # every earlier payment to the same payee counts against a budget that was granted later
            # (a fresh autopay showed 0 of 0.01 left and denied its first payment).
            floor = mandate.created_at if mandate.created_at else None
            if floor and floor > window_start:
                window_start = floor
            period = self.ledger.totals(req.asset, kind=req.kind, since=window_start,
                                        payee=req.payee, mandate_id=mandate.mandate_id)
            lifetime = self.ledger.totals(req.asset, kind=req.kind, since=floor,
                                          payee=req.payee, mandate_id=mandate.mandate_id)
        else:
            period = self.ledger.totals(req.asset, kind=req.kind)
            lifetime = period
        # Per asset, across every chain that carries it: the cap table is keyed by coin, and summing
        # per chain let USDC on Base and USDC on Polygon each spend a full day's cap.
        daily = self.ledger.totals(req.asset, since=day_start, network=network_class(req.kind))
        return SpendContext(unattended=False, period_spent=period, daily_spent=daily,
                            lifetime_spent=lifetime, agent_initiated=agent_initiated)

    def request_tx(self, params: Dict[str, Any], unattended: bool = False,
                   verified_provenance: bool = False,
                   from_pay_url: bool = False) -> Dict[str, Any]:
        key = str(params.get("idempotency_key") or "").strip()[:64]
        if key:
            record = self.idempotency.get(key) or {}
            recorded_id = record.get("request_id", "") if isinstance(record, dict) else str(record)
            existing = self.requests.get(recorded_id)
            if existing is None and recorded_id:
                # the request was pruned, but this key already produced one: never create a second
                return {
                    "request": {"request_id": recorded_id,
                                "status": record.get("status", "unknown"),
                                "amount": "", "asset": "", "payee": ""},
                    "status": record.get("status", "unknown"),
                    "approval": None,
                    # `allow: true` on a duplicate made an agent believe it had just filed a payment.
                    # A duplicate is not permission: nothing was created by this call.
                    "decision": {"allow": False, "rule": "idempotent", "code": "duplicate",
                                 "reason": "this idempotency key already produced a request"},
                    "duplicate": True, "idempotency_key": key,
                    "note": "nothing was filed by this call: the request already existed. The "
                            "original is no longer held in memory; its receipt is in the ledger. "
                            "Ask for it again only if you intend a new payment.",
                }
            if existing is not None:
                # A retry after a timeout or a crash must not become a second payment. Return the
                # request that already exists, with its live approval if it is still pending.
                row = existing.to_dict()
                approval = self._live_approval(existing.request_id)
                payload: Dict[str, Any] = {
                    "request": row,
                    "status": existing.status,
                    "decision": {"allow": False, "rule": "idempotent",
                                 "code": "duplicate",
                                 "reason": f"request already exists ({existing.status})"},
                    "duplicate": True,
                    "idempotency_key": key,
                    "approval": None,
                    "note": "nothing was filed by this call: the request above already existed.",
                }
                if approval is not None:
                    payload["approval_id"] = approval.approval_id
                    payload["approval_expires_at"] = approval.expires_at.strftime("%Y-%m-%dT%H:%M:%SZ")
                    payload["approval"] = {"approval_id": approval.approval_id,
                                           "expires_at": payload["approval_expires_at"]}
                    payload["needs"] = "operator approval"
                return payload

        kind = str(params.get("kind", ""))
        asset = str(params.get("asset", "")).upper()
        if not kind and asset and asset != "AUTO":
            kind = self.DEFAULT_CHAIN.get(asset, "")
            if not kind:
                raise WalletError("bad_chain", f"{asset} is not a coin this wallet knows")
        if not kind:
            raise WalletError("bad_chain", "a chain or a coin is required")
        if not asset or asset == "AUTO":
            # the chain decides: xrp-mainnet means XRP, bitcoin-mainnet means BTC
            asset = self.NATIVE_ASSET.get(kind, (self.settings.prefer_asset or "XMR")).upper()
        if asset not in self._chain_assets(kind):
            raise WalletError("asset_chain_mismatch",
                              f"{asset} is not a coin on {kind} (it holds "
                              f"{', '.join(self._chain_assets(kind))})")
        # Fiat first: "send adrian 10$" is the sentence this wallet exists for. The daemon quotes, the
        # card shows both numbers, and the *coin* amount is what gets stored, capped and signed.
        fiat_row: Dict[str, Any] = {}
        raw_amount = params.get("amount")
        if isinstance(raw_amount, str) and asset:
            # "0.01 XMR" names the coin the request is already in; only the number is the amount
            stripped = raw_amount.strip()
            if stripped.upper().endswith(asset) and stripped[:-len(asset)].strip()[-1:].isdigit():
                raw_amount = stripped[:-len(asset)].strip()
        fiat_hint = params.get("fiat_amount")
        try:
            if fiat_hint not in (None, ""):
                # an explicit fiat_amount carries its currency separately (default USD)
                currency = fx.normalise_currency(str(params.get("fiat_currency") or "USD"))
                amount_text = Decimal(str(fiat_hint).replace(",", "").strip())
                if not amount_text.is_finite() or amount_text <= 0:
                    raise fx.FxError("bad_fiat", "a fiat amount must be positive")
                fiat = (amount_text, currency)
            else:
                fiat = fx.split_fiat(raw_amount)
        except (fx.FxError, InvalidOperation, ValueError) as exc:
            code = getattr(exc, "code", "bad_fiat")
            raise WalletError(code, getattr(exc, "message", str(exc))) from exc
        if fiat is not None:
            fiat_value, fiat_currency = fiat
            if params.get("fiat_amount") not in (None, "") and params.get("amount") not in (None, ""):
                raise WalletError("bad_amount", "pass either an amount or a fiat_amount, not both")
            try:
                priced = fx.quote(asset, fiat_currency, home=self.home, secret=self.master)
                amount = fx.to_coin(fiat_value, Decimal(str(priced["rate"])), asset)
            except fx.FxError as exc:
                raise WalletError(exc.code, exc.message) from exc
            except ArithmeticError as exc:
                raise WalletError("bad_fiat", f"{raw_amount!r} is not a fiat amount") from exc
            fiat_row = {"currency": fiat_currency, "amount": format(fiat_value, "f"),
                        "rate": str(priced["rate"]), "source": priced["source"],
                        "quoted_at": priced["quoted_at"], "coin_amount": format(amount, "f")}
            if priced.get("stale"):
                # a fallback quote is disclosed, not hidden: the card dates it
                fiat_row["stale"] = "true"
                fiat_row["age_seconds"] = str(priced.get("age_seconds", 0))
        else:
            try:
                amount = parse_amount(raw_amount, asset)
            except ValueError as exc:
                raise WalletError("bad_amount", str(exc)) from exc
        # Quantise to what the chain can actually express, so the amount approved is the amount
        # signed. A 12-dp XMR-style amount on USDC used to sign 6 dp while the receipt kept 12.
        quantum = Decimal(1).scaleb(-self.ASSET_DECIMALS.get(asset, 12))
        try:
            amount = amount.quantize(quantum)
        except ArithmeticError as exc:
            raise WalletError("bad_amount", f"{asset} cannot express {amount}") from exc
        if amount <= 0:
            raise WalletError("bad_amount", f"{params.get('amount')} rounds to zero {asset}")
        provenance: Dict[str, str] = {}
        raw_prov = params.get("provenance")
        if isinstance(raw_prov, dict):
            provenance = {key: str(raw_prov.get(key) or "")[:300]
                          for key in ("url", "host", "title", "method", "extracted_at")
                          if raw_prov.get(key)}
        # `verified_provenance` is a Python keyword the RPC cannot pass: only `pay_url` (which fetched
        # the page itself) sets it. Without this an injected agent writes "address from openrouter.ai"
        # onto its own request and the card repeats it as fact.
        if provenance:
            provenance["verified"] = "true" if verified_provenance else "false"
        funding_for = str(params.get("funding_for", "") or "").strip()
        if funding_for:
            # Funding a sandbox arms it for no-approval spending, so it has to be exactly that
            # sandbox's address and budget — checked here, before approval and before broadcast.
            # Checking after the broadcast let a payment to anyone labelled "funding" complete and
            # only refuse the arming.
            target = self.sandboxes.get(funding_for)
            if target is None:
                raise WalletError("unknown_sandbox", f"no sandbox {funding_for!r} to fund")
            if target.tampered:
                raise WalletError("sandbox_tampered",
                                  "that budget's record was edited outside the wallet; revoke it")
            if target.revoked_at or target.destroyed_at:
                raise WalletError("sandbox_closed",
                                  "that sandbox was revoked or destroyed; its budget cannot be funded")
            if target.expired():
                # funding a budget whose clock already ran out just parks money in an account that
                # will never be spent from
                raise WalletError("sandbox_expired",
                                  "that sandbox has expired; create a new one instead of funding it")
            if (kind, asset) != (target.kind, target.asset):
                raise WalletError("funding_mismatch",
                                  f"this sandbox holds {target.asset} on {target.kind}")
            # A first funding must be the whole budget (the sandbox is armed by that one transfer);
            # a later one is a top-up and may be any amount, because it adds to a ceiling that is
            # already in force. `_broadcast` double-checks both cases before arming anything.
            if not target.enabled and amount != target.budget:
                raise WalletError("funding_mismatch",
                                  f"funding must be the whole budget ({target.budget} {target.asset}), "
                                  f"not {amount}")
        resolved, contact_name = self.resolve_payee(kind, str(params.get("payee", "")))
        try:
            payee = payee_key(kind, resolved)
        except ValueError:
            # Keep the raw string so policy can deny it *and* the ledger records the attempt.
            payee = str(params.get("payee", "")).strip()[:200]
            contact_name = ""
        if funding_for:
            # the destination has to be the sandbox's own address
            try:
                expected_payee = payee_key(kind, self.sandboxes[funding_for].address)
            except (KeyError, ValueError):
                expected_payee = ""
            if not expected_payee or payee != expected_payee:
                raise WalletError("funding_mismatch",
                                  "that transfer does not go to the sandbox address; it is not "
                                  "funding and will not arm anything")
        # set below once the payee is known to be ours
        req = SpendRequest(
            request_id="req_" + secrets.token_hex(6),
            kind=kind,
            asset=asset,
            amount=amount,
            payee=payee,
            memo=(str(params.get("memo", "")) or (f"to {contact_name}" if contact_name else ""))[:200],
            why=str(params.get("why", ""))[:400],
            sandbox_id=str(params.get("sandbox_id", "")),
            funding_for=str(params.get("funding_for", "")),
            sweep_of=str(params.get("sweep_of", "")),
            challenge_sha256=str(params.get("challenge_sha256", ""))[:64],
            # `from_pay_url` is a Python kwarg only `pay_url` passes; the RPC cannot reach it, so an
            # agent can no longer hand itself a challenge (which approve later POSTs a signed
            # authorisation to, with the response body coming back).
            challenge=dict(params.get("challenge") or {}) if from_pay_url else {},
            idempotency_key=key,
            profile=str(params.get("profile", "")),
            fiat=dict(fiat_row),
            provenance=provenance,
        )

        if req.sweep_of and str(params.get("_scope", "")) != OPERATOR:
            # Closing a budget is the operator's call: an agent that can sweep can end its own
            # spending room at will (and, before the payee check, aim it anywhere).
            raise WalletError("sweep_operator_only",
                              "only an operator can return a sandbox's balance to the wallet")
        if req.sweep_of:
            # A sweeps-only bypass of the budget must not become "send a sandbox's balance anywhere":
            # the destination has to be this wallet's own address for that chain.
            sandbox = self.sandboxes.get(req.sweep_of)
            adapter_for_sweep = self.adapters.get(req.kind)
            own_address = ""
            if sandbox is not None and adapter_for_sweep is not None:
                try:
                    own_address = payee_key(req.kind, adapter_for_sweep.address())
                except Exception:  # noqa: BLE001 - an unreadable address is a refusal, not a pass
                    own_address = ""
            if sandbox is None or not own_address or req.payee != own_address:
                req.status = "denied"
                self._save_requests()
                if self.ledger is not None:
                    self.ledger.append("spend_request", "denied", "policy",
                                       {"request_id": req.request_id, "code": "sweep_scope",
                                        "reason": "a sweep may only pay this wallet's own address"})
                return {"request": req.to_dict(),
                        "decision": {"allow": False, "rule": "policy", "code": "sweep_scope",
                                     "reason": "a sweep may only pay this wallet's own address"}}

        # A sweep is an operator action that returns money home; it still needs the sandbox path to
        # auto-broadcast, but it is policed like any other spend (the AND below).
        sandbox_decision = self._sandbox_decision(req) if req.sandbox_id else None
        mandate = None if sandbox_decision is not None else self._match_mandate(req)
        ctx = self._context(req, mandate, agent_initiated=str(params.get("_scope", "")) == AGENT)
        ctx.self_transfer = bool(req.sweep_of)      # the payee was validated as our own above
        # A locked wallet has no operator: it can never take the manual path.
        ctx.unattended = unattended or not self.is_unlocked()
        if sandbox_decision is not None:
            # A sandbox budget is an *additional* permission, never a substitute: the allowlist,
            # monero_only, enabled assets and the daily cap still apply to sandbox spending.
            policy_decision = evaluate(req, self.settings, ctx, mandate)
            decision = policy_decision if not policy_decision.allow else sandbox_decision
        else:
            decision = evaluate(req, self.settings, ctx, mandate)

        self.requests[req.request_id] = req
        self._save_requests()
        if key:
            self._remember_idempotency(key, req.request_id, req.status)
        details = {"request_id": req.request_id, "kind": req.kind, "asset": req.asset,
                   "amount": str(req.amount), "payee": req.payee, "mandate": bool(mandate),
                   "sandbox_id": req.sandbox_id,
                   # our own money moving between our own accounts: not spending (see spend_totals)
                   **({"funding_for": req.funding_for} if req.funding_for else {}),
                   **({"sweep_of": req.sweep_of} if req.sweep_of else {}),
                   "mandate_id": mandate.mandate_id if mandate is not None else "",
                   "idempotency_key": req.idempotency_key,
                   # the human's stated reason for a payment travels with the receipt, so an
                   # exception to "prefer Monero" is auditable instead of invisible
                   "why": req.why[:200],
                   **({"fiat": req.fiat} if req.fiat else {}),
                   **({"provenance": req.provenance} if req.provenance else {})}
        if not decision.allow:
            req.status = "denied"
            self._save_requests()
            self._record("spend_request", "denied", decision.rule,
                         {**details, "code": decision.code, "reason": decision.reason})
            return {"request": req.to_dict(), "decision": decision.to_dict()}

        # Chain step: build (but never publish) the transaction now, so an unfunded wallet or a
        # bad payee fails here — with a receipt — instead of after a human approved it.
        adapter = self.adapters.get(req.kind)
        plan = None
        if adapter is not None:
            try:
                account = 0
                if req.sandbox_id and req.sandbox_id in self.sandboxes:
                    # -1 marks a pooled budget: it has no account of its own, so it spends from main
                    account = max(0, self.sandboxes[req.sandbox_id].account_index)
                # A priced 402 purchase settles differently: we sign an off-chain authorization
                # that the facilitator submits (and pays the gas for), instead of broadcasting a
                # transfer ourselves.
                if req.challenge and hasattr(adapter, "plan_authorization") and req.challenge.get("scheme") == "exact":
                    plan = self._plan_authorization(adapter, req)
                else:
                    plan = adapter.plan_transfer(req.amount, req.payee, account_index=account,
                                                 asset=req.asset)
                cap = Decimal(str(self.settings.fee_cap.get(req.asset, "0") or "0"))
                if cap > 0 and plan.fee > cap and not plan.raw.get("x402"):
                    raise ChainError("fee_too_high",
                                     f"network fee {plan.fee} {req.asset} exceeds the {cap} cap")
                # Spending the last of the gas coin bricks the chain until a human notices: the
                # next payment cannot be signed at all. Refuse at planning, so the refusal is on
                # the receipt rather than on the human's approval card.
                self._check_gas_floor(req, plan)
                self.plans[req.request_id] = plan
                details["fee"] = str(plan.fee)
                req.fee_estimate = str(plan.fee)
                # What the transfer does to the gas balance, on the same card as the amount: a
                # human approving a payment should see that it leaves the chain able to pay again.
                gas = self._gas_view(req.kind)
                if gas and "error" not in gas and not plan.raw.get("x402"):
                    after = (Decimal(gas["held"]) - Decimal(str(plan.fee))
                             - (Decimal(str(plan.amount))
                                if str(req.asset).upper() == gas["native"] else Decimal("0")))
                    details["gas"] = {"native": gas["native"], "floor": gas["floor"],
                                      "after": str(after)}
                    # The card reads the request, not the receipt: without this the human approves
                    # a payment and never sees what it does to the gas balance.
                    req.gas = dict(details["gas"])
            except ChainError as exc:
                # `insufficient_funds` is not a refusal: it is "there is no money yet", and the same
                # request is payable after a top-up. Marking it denied made an unfunded attempt look
                # like a human said no.
                retryable = exc.code in ("rpc_unreachable", "daemon_unavailable", "rpc_error",
                                         "signer_unreachable", "insufficient_funds",
                                         "insufficient_gas", "gas_floor")
                # A dead endpoint is not a refusal: leave the request retryable instead of writing a
                # terminal "denied" that reads like the policy said no.
                req.status = "retry" if retryable else "denied"
                self._save_requests()
                if self.ledger is not None:
                    self.ledger.append("spend_request", "deferred" if retryable else "denied", "chain",
                                       {**details, "code": exc.code, "reason": exc.message})
                return {"request": req.to_dict(),
                        "decision": {"allow": False, "rule": "chain", "code": exc.code,
                                     "reason": exc.message, "retryable": retryable}}

        # The PIN is the gate on *every* payment, so a budget or an autopay no longer skips the human.
        # Unless an operator explicitly marked that autopay `unattended` (which costs the passphrase
        # and the PIN, and shows red in the pane), an otherwise-allowed auto payment becomes a pending
        # approval: the pane draws the card and asks for the PIN.
        armed_sandbox = (self.sandboxes.get(req.sandbox_id)
                         if req.sandbox_id and sandbox_decision is not None and decision.allow else None)
        pin_waiting = (self.settings.pin_every_payment
                       and not (mandate is not None and mandate.unattended)
                       and not (armed_sandbox is not None and armed_sandbox.unattended))
        if pin_waiting and (mandate is not None or (sandbox_decision is not None and decision.allow)):
            decision = Decision(True, "pin", "needs_pin",
                                "every payment needs the PIN; waiting for the operator")
            mandate = None
            sandbox_decision = None

        if (mandate is not None or (sandbox_decision is not None and decision.allow)) and adapter is None:
            req.status = "denied"
            if self.ledger is not None:
                self.ledger.append("spend_request", "denied", "chain",
                                   {**details, "code": "chain_unavailable",
                                    "reason": f"no signer adapter for {req.kind}; nothing was sent"})
            return {"request": req.to_dict(),
                    "decision": {"allow": False, "rule": "chain", "code": "chain_unavailable",
                                 "reason": f"no signer adapter for {req.kind}"}}

        if mandate is not None or (sandbox_decision is not None and decision.allow):
            req.authorised_by = "mandate" if mandate is not None else "sandbox"
            req.status = "approved"
            self._save_requests()
            # No human in the loop (mandate or sandbox budget) — publish now. Both exist only because
            # a human created them with the passphrase, and both require an unlocked wallet below. The intent is written
            # first: if the process dies mid-broadcast, the ledger shows an attempt instead of
            # silently losing the fact that money may have moved.
            if self.ledger is not None:
                self.ledger.append("spend_request", "broadcasting", decision.rule, details)
            try:
                execution = self._broadcast(req, plan, adapter)
            except WalletError as exc:
                self._fail_request(req, exc.code, exc.message)
                raise
            self._deliver_x402(req, plan, execution)
            self._settle_x402_status(req, execution)
            if self.ledger is not None:
                self.ledger.append("spend_request", "approved", req.authorised_by or decision.rule,
                                   {**details, "code": decision.code, "reason": decision.reason,
                                    **execution})
            return {"request": req.to_dict(), "decision": decision.to_dict(), "auto": True,
                    **execution}

        if self.ledger is not None:
            self.ledger.append("spend_request", "pending", decision.rule,
                               {**details, "code": decision.code, "reason": decision.reason,
                                "planned": plan is not None})
        approval = self._issue_approval(req)
        return {
            "request": req.to_dict(),
            "decision": decision.to_dict(),
            "approval_id": approval.approval_id,
            "approval_expires_at": approval.expires_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "confirm_amount_required": req.confirm_amount_required,
            "needs": "operator approval",
        }

    @staticmethod
    def _accepts(function: Any, name: str) -> bool:
        """Does this callable take a keyword called `name`? Asked instead of calling and catching
        TypeError, which cannot tell "does not take it" from "took it and failed inside"."""
        import inspect

        try:
            parameters = inspect.signature(function).parameters
        except (TypeError, ValueError):
            return False
        return name in parameters or any(p.kind is inspect.Parameter.VAR_KEYWORD
                                         for p in parameters.values())

    def _plan_authorization(self, adapter: Any, req: SpendRequest):
        """Sign the x402 authorisation for a priced purchase, naming the network the way the seller
        did: a facilitator matches the payload's `network` against its own offer, and "base-mainnet"
        is this wallet's name for a chain the seller called "base" or "eip155:8453"."""
        kwargs: Dict[str, Any] = {"resource": req.challenge.get("resource", "")}
        if self._accepts(adapter.plan_authorization, "network"):
            kwargs["network"] = str(req.challenge.get("network") or "")
        return adapter.plan_authorization(req.amount, req.payee, req.asset, **kwargs)

    def _fail_request(self, req: SpendRequest, code: str, message: str) -> None:
        """Mark a request that never left the wallet, and write the receipt that says why."""
        if self.ledger is not None:
            last = self.ledger.last() or {}
            if last.get("decision") == "denied" \
                    and (last.get("details") or {}).get("request_id") == req.request_id:
                # the chain-failure path (or a policy refusal) already wrote this exact refusal
                return
        req.status = "pending" if code == "locked" else "failed"
        self._save_requests()
        self._record("spend_request", "denied", "broadcast",
                     {"request_id": req.request_id, "kind": req.kind, "asset": req.asset,
                      "amount": str(req.amount), "payee": req.payee,
                      "code": code, "reason": str(message)[:200]})

    def _record(self, kind: str, decision: str, rule: str, details: Dict[str, Any]) -> None:
        """Append a receipt, or queue it when the ledger is not open (a locked wallet).

        A locked daemon cannot read the ledger, so a refusal raised then used to leave no trace at
        all — `approve` would report `locked` and the request list later showed a request nobody could
        account for. Queued entries are written the moment the operator unlocks, marked `deferred`.
        """
        if self.ledger is not None:
            self.ledger.append(kind, decision, rule, details)
            return
        queue = getattr(self, "_deferred_receipts", None)
        if queue is None:
            self._deferred_receipts = queue = []
        if len(queue) < 200:                     # a bounded queue: never grow without limit
            queue.append((kind, decision, rule, dict(details)))

    def _flush_deferred_receipts(self) -> int:
        queue = getattr(self, "_deferred_receipts", None) or []
        if self.ledger is None or not queue:
            return 0
        written = 0
        for kind, decision, rule, details in queue:
            self.ledger.append(kind, decision, rule, {**details, "deferred": True})
            written += 1
        self._deferred_receipts = []
        return written

    def _broadcast(self, req: SpendRequest, plan, adapter) -> Dict[str, Any]:
        """Publish an approved plan. Returns receipt fields; raises WalletError on chain failure.

        This is the only function in the daemon that can put a transaction on a network, so it is
        where the promise "the agent cannot send without the passphrase" is physically enforced:

        * no unlocked keystore, no signing key in memory - a locked daemon cannot sign at all;
        * the request must carry an authorisation that a human produced: the passphrase typed at
          approval time, or a mandate/sandbox that the human created with the passphrase. An empty
          `authorised_by` means nobody authorised this, and it is refused rather than sent.
        """
        if not self.is_unlocked():
            raise WalletError("locked",
                              "the wallet is locked, so there is no key in memory to sign with; "
                              "nothing was sent")
        if not req.authorised_by:
            raise WalletError("not_authorised",
                              "this request has no human authorisation; refusing to sign it")
        if adapter is None or plan is None:
            # Approval without a signed transaction would put "approved" in the receipt for money
            # that never moved. Fail instead, and say which piece is missing.
            raise WalletError("not_planned",
                              "this request has no signed transaction (the chain adapter was missing "
                              "when it was created); deny it and create a new request")
        # The cap is checked when a request is created; this is the second, later check, because a
        # plan can be rebuilt after a restart or an auto-lock and the chain can reprice between the
        # human's approval and this call. A fee above the operator's cap must not be sent at all.
        cap = Decimal(str(self.settings.fee_cap.get(req.asset, "0") or "0"))
        if cap > 0 and getattr(plan, "fee", None) is not None and plan.fee > cap \
                and not (plan.raw or {}).get("x402"):
            raise WalletError("fee_too_high",
                              f"network fee {plan.fee} {req.asset} is above the {cap} cap; nothing was sent")
        try:
            # Decided by signature, not by catching TypeError: a TypeError raised *inside* a
            # broadcast that takes the cap used to fall through to a second broadcast call.
            if self._accepts(adapter.broadcast, "fee_cap"):
                result = adapter.broadcast(plan, fee_cap=cap if cap > 0 else None)
            else:
                result = adapter.broadcast(plan)
        except ChainError as exc:
            # "approved" here told every reader that money moved when it had not, and the approval
            # was already consumed, so the payment could never be retried. "failed" is the honest
            # state: the human approves again if they still want it.
            req.status = "failed"
            self._save_requests()
            if self.ledger is not None:
                self.ledger.append("spend_request", "denied", "broadcast",
                                   {"request_id": req.request_id, "kind": req.kind, "asset": req.asset,
                                    "amount": str(req.amount), "payee": req.payee,
                                    "code": exc.code, "reason": exc.message})
            raise WalletError("broadcast_failed", exc.message) from exc
        self._invalidate_balances()
        req.tx_hash = result.get("tx_hash", "")
        req.status = "sent"
        self._save_requests()
        self.plans.pop(req.request_id, None)
        if req.funding_for and req.funding_for in self.sandboxes:
            sandbox = self.sandboxes[req.funding_for]
            expected = (sandbox.address or "").strip()
            top_up = bool(sandbox.enabled) and req.amount > 0
            if not expected or req.payee != expected or (not top_up and req.amount != sandbox.budget):
                # A transfer to somebody else is not funding: arming the sandbox on one would let the
                # agent open a no-approval budget with an unrelated (or attacker) payment.
                if self.ledger is not None:
                    self.ledger.append("sandbox", "funding_ignored", "policy",
                                       {"sandbox_id": sandbox.sandbox_id, "payee": req.payee,
                                        "expected_payee": expected, "amount": str(req.amount),
                                        "expected_amount": str(sandbox.budget)})
                sandbox = None          # not funding: keep going (a sweep can still apply)
            if sandbox is not None and (sandbox.revoked_at or sandbox.destroyed_at or sandbox.expired()):
                # A funding approval can arrive after the operator revoked the budget. Arming it
                # again would resurrect a closed sandbox with no-approval spending.
                if self.ledger is not None:
                    self.ledger.append("sandbox", "funding_ignored", "policy",
                                       {"sandbox_id": sandbox.sandbox_id, "reason": "sandbox_closed",
                                        "tx_hash": req.tx_hash})
                sandbox = None
            if sandbox is not None:
                was_open = sandbox.enabled
                if was_open:
                    # An approved transfer into a budget that is already armed raises the ceiling
                    # instead of contradicting it: that is what "it asks for more and a human says
                    # yes" means, and it must not require recreating the sandbox (a new account,
                    # a new address, and a sweep of the old one).
                    sandbox.budget = sandbox.budget + req.amount
                sandbox.enabled = True
                self._save_sandboxes()
                if self.ledger is not None:
                    self.ledger.append("sandbox", "topped_up" if was_open else "funded", "operator",
                                       {"sandbox_id": sandbox.sandbox_id, "tx_hash": req.tx_hash,
                                        "added": str(req.amount), "budget": str(sandbox.budget),
                                        "asset": sandbox.asset})
        if req.sweep_of and req.sweep_of in self.sandboxes:
            swept = self.sandboxes[req.sweep_of]
            reserve = self._fee_reserve(swept.kind, swept.asset)
            reader = getattr(adapter, "balance_at", None)
            live = None
            if reader is not None:
                try:
                    live = Decimal(str(reader(swept.account_index)))
                except Exception:  # noqa: BLE001 - unreadable is not "empty"
                    live = None
            if live is not None and live <= reserve:
                swept.spent = swept.budget
                swept.enabled = False
                if self.ledger is not None:
                    self.ledger.append("sandbox", "swept", "operator",
                                       {"sandbox_id": swept.sandbox_id, "amount": str(req.amount),
                                        "tx_hash": req.tx_hash})
            else:
                # Either the account still holds money (a partial self-payment, or change that has
                # not confirmed) or the balance could not be read at all. Both mean "do not close":
                # treating an unreadable balance as empty marked funded sandboxes as swept.
                if self.ledger is not None:
                    self.ledger.append("sandbox", "swept_partial", "operator",
                                       {"sandbox_id": swept.sandbox_id, "amount": str(req.amount),
                                        "left": str(live), "tx_hash": req.tx_hash})
            self._save_sandboxes()
        elif req.sandbox_id and req.sandbox_id in self.sandboxes:
            sandbox = self.sandboxes[req.sandbox_id]
            # The network fee comes out of the same account, so it comes out of the same budget. Not
            # charging it made `remaining` over-report by one fee per payment, and the last payment
            # then looked affordable when it was not.
            fee_paid = Decimal("0")
            try:
                fee_paid = Decimal(str(getattr(plan, "fee", 0) or 0))
            except (InvalidOperation, ValueError, TypeError):
                fee_paid = Decimal("0")
            req.fee_paid = str(max(Decimal("0"), fee_paid))
            sandbox.spent += req.amount + max(Decimal("0"), fee_paid)
            self._save_sandboxes()
            self._save_requests()
            self._arm_self_destruct(sandbox)
        explorer_tx = getattr(adapter, "explorer_tx", "") if adapter is not None else ""
        execution = {"signed": True, **result}
        if explorer_tx and result.get("tx_hash"):
            execution["explorer"] = explorer_tx.replace("{tx}", result["tx_hash"])
        return execution

    # Fields that describe the payment versus fields that describe what happened to it. The digest a
    # human approves is over the first kind only: adding "authorised_by" once (a legitimate state
    # change) must not make the approval look like a different payment.
    # What the human authorised, and nothing else. This used to be a deny-list ("status", "tx_hash",
    # "authorised_by"), which meant every *derived* field the daemon writes after the card is drawn —
    # the re-quoted `fee_estimate`, the `gas` projection, `confirm_amount_required`, the loader's
    # `provenance.verified` — silently invalidated a live approval and the human got
    # `approval_digest_mismatch` for a payment nobody had touched. An allow-list cannot rot that way:
    # a field added later is outside the seal until someone decides it belongs inside it.
    _DIGEST_FIELDS = ("request_id", "kind", "asset", "amount", "payee", "memo", "why",
                      "sandbox_id", "profile", "funding_for", "sweep_of", "challenge_sha256",
                      "challenge", "idempotency_key", "created_at", "fiat", "deposit_id", "swap")

    def _digest(self, req: SpendRequest) -> str:
        body = req.to_dict()
        sealed = {key: body.get(key) for key in self._DIGEST_FIELDS}
        # Where the payee came from is what the card asks a human to judge, so it is sealed too — but
        # without `verified`, which the loader demotes on every restart (the file is not signed).
        sealed["provenance"] = {k: v for k, v in dict(body.get("provenance") or {}).items()
                                if k != "verified"}
        raw = json.dumps(sealed, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def _issue_approval(self, req: SpendRequest) -> Approval:
        req.confirm_amount_required = bool(
            self.settings.human_gate and req.amount > self.settings.threshold(req.asset))
        approval = Approval(
            approval_id="apr_" + secrets.token_hex(8),
            request_id=req.request_id,
            digest=self._digest(req),
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=self.settings.approval_ttl_seconds),
        )
        self.approvals[approval.approval_id] = approval
        return approval

    def _consume_approval(self, approval_id: str, request_id: str) -> SpendRequest:
        # deny only needs an *unused* approval: refusing a payment must never be blocked by expiry,
        # or a dead request sits in the pane with a Decline button that always errors
        approval, req = self._peek_approval(approval_id, request_id, ignore_expiry=True)
        approval.used = True
        return req

    def _live_approval(self, request_id: str) -> Optional[Approval]:
        for approval in self.approvals.values():
            if approval.request_id == request_id and not approval.used:
                return approval
        return None

    def _peek_approval(self, approval_id: str, request_id: str,
                       ignore_expiry: bool = False) -> tuple[Approval, SpendRequest]:
        """Validate an approval without burning it, so a mistyped amount can be retried.

        `ignore_expiry` is for deny(): refusing a payment must work even after the approval window
        closed, otherwise the request is stuck in front of the human forever.
        """
        approval = self.approvals.get(approval_id)
        if approval is None:
            raise WalletError("unknown_approval", "approval id is not recognised")
        if approval.used:
            raise WalletError("approval_replayed", "approval id was already used")
        if approval.request_id != request_id:
            raise WalletError("approval_mismatch", "approval belongs to a different request")
        if not ignore_expiry and datetime.now(timezone.utc) > approval.expires_at:
            # a lapsed approval is terminal for the request: it can still be declined, never approved
            req_for_expiry = self.requests.get(request_id)
            if req_for_expiry is not None and req_for_expiry.status == "pending":
                req_for_expiry.status = "expired"
                self._save_requests()
            raise WalletError("approval_expired", "approval expired")
        req = self.requests.get(request_id)
        if req is None:
            raise WalletError("unknown_request", "request id is not recognised")
        if req.status != "pending":
            raise WalletError("request_not_pending", f"request is {req.status}")
        if self._digest(req) != approval.digest:
            raise WalletError("approval_digest_mismatch", "request changed after approval was issued")
        return approval, req

    def approve(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self.is_unlocked():
            self._record("spend_request", "denied", "locked",
                         {"request_id": str(params.get("request_id", "")),
                          "code": "locked",
                          "reason": "an approval was attempted while the wallet was locked; nothing was signed"})
            self._require_unlocked()
        approval_id = str(params.get("approval_id", ""))
        request_id = str(params.get("request_id", ""))
        approval, req = self._peek_approval(approval_id, request_id)

        # The PIN first: an approval call is the moment a human authorises money, and this is the only
        # place in the daemon where that secret is checked.
        if self.settings.pin_every_payment:
            self._verify_pin(params)

        # What the pane showed must be what the daemon is about to sign. A card that says one thing and
        # a request that says another is the whole attack, so the numbers are compared, not trusted.
        shown_payee = str(params.get("expect_payee") or "").strip()
        if shown_payee:
            try:
                same = payee_key(req.kind, shown_payee) == payee_key(req.kind, req.payee)
            except ValueError:
                same = False
            if not same:
                raise WalletError("card_changed",
                                  "the destination on the card is not the destination in the request")
        shown_amount = params.get("expect_amount")
        if shown_amount not in (None, ""):
            try:
                displayed = Decimal(str(shown_amount).strip())
            except (InvalidOperation, ArithmeticError, ValueError) as exc:
                raise WalletError("bad_expect_amount", "the card's amount is not a number") from exc
            if displayed != req.amount:
                raise WalletError("card_changed",
                                  f"the card says {displayed}, the request says {req.amount}")

        # Re-evaluate at approval time: caps are checked when a request is *made*, so an agent that
        # queues several pendings would otherwise multiply the daily cap (3 × 0.4 with a 1.0 cap).
        # A swap is exempt from the payee rules (its destination is the router pinned in code, not an
        # address anyone typed) but not from the caps: the amount sold is re-checked here.
        if req.swap:
            fresh = Decision(True, "swap", "swap_ok", "trusted swap operator")
        else:
            fresh = evaluate(req, self.settings, self._context(req, None), None)
        if not fresh.allow:
            approval.used = True
            req.status = "denied"
            self._save_requests()
            self._record("spend_request", "denied", fresh.rule,
                         {"request_id": req.request_id, "kind": req.kind, "asset": req.asset,
                          "amount": str(req.amount), "payee": req.payee,
                          "code": fresh.code, "reason": fresh.reason})
            raise WalletError(fresh.code, fresh.reason)

        if req.sandbox_id and not req.sweep_of:
            # The budget is re-checked at approval too. A request is measured against the budget when
            # it is filed; five of them filed against the same untouched budget each fit, and on a
            # pooled budget (which spends from the main account) nothing physical stopped all five.
            # The same goes for a budget that expired or was revoked while the card was on screen.
            budget = self.sandboxes.get(req.sandbox_id)
            limit = (Decision(False, "sandbox", "unknown_sandbox",
                              f"no sandbox '{req.sandbox_id[:24]}' — nothing was signed")
                     if budget is None else self._sandbox_limits(req, budget))
            if limit is not None:
                approval.used = True
                req.status = "denied"
                self._save_requests()
                self._record("spend_request", "denied", limit.rule,
                             {"request_id": req.request_id, "kind": req.kind, "asset": req.asset,
                              "amount": str(req.amount), "payee": req.payee,
                              "sandbox_id": req.sandbox_id,
                              "code": limit.code, "reason": limit.reason})
                raise WalletError(limit.code, limit.reason)

        if self.settings.human_gate and not self.settings.pin_every_payment:
            # With the PIN gate on, `_verify_pin` above is the boundary and asking for the long
            # passphrase too would be two secrets for one payment. With it off, fall back to the
            # passphrase: a manual payment is authorised by the human, never by an unlocked process
            # (the agent can read the operator token off disk).
            try:
                keystore.open_sealed(keystore.load(self.keystore_path), str(params.get("passphrase", "")))
            except keystore.KeystoreError as exc:
                raise WalletError("passphrase_required",
                                  "approving a payment needs the wallet passphrase") from exc
        if req.funding_for:
            target = self.sandboxes.get(req.funding_for)
            if target is not None and (target.expired() or target.revoked_at or target.destroyed_at):
                code = "sandbox_expired" if target.expired() else "sandbox_closed"
                approval.used = True
                req.status = "cancelled"
                self._save_requests()
                self._record("spend_request", "cancelled", "policy",
                             {"request_id": req.request_id, "code": code,
                              "reason": "the sandbox stopped being fundable before this approval"})
                raise WalletError(code, "that sandbox is no longer open; nothing was sent")
        confirm_amount = params.get("confirm_amount")
        typed: Optional[Decimal] = None
        if confirm_amount not in (None, ""):
            try:
                typed = Decimal(str(confirm_amount).strip())
            except (InvalidOperation, ArithmeticError, ValueError) as exc:
                raise WalletError("bad_confirm_amount",
                                  f"{confirm_amount!r} is not an amount") from exc
        needs_typing = self.settings.human_gate and req.amount > self.settings.threshold(req.asset)
        if needs_typing and typed is None:
            raise WalletError("confirm_amount_required",
                              f"type the amount ({req.amount}) to approve this payment")
        if typed is not None and typed != req.amount:
            # below the threshold the typed amount is optional, but a human who types one and sees it
            # accepted for a different amount has been lied to. Compare it always.
            raise WalletError("confirm_amount_mismatch",
                              f"you typed {typed}, this request is {req.amount}")
        # every check passed: record that a human authorised this specific request. `_broadcast`
        # refuses to sign anything without it, and nothing else in the daemon can set it.
        req.authorised_by = "pin" if self.settings.pin_every_payment else "passphrase"
        approval.used = True
        self.prompt = None
        if req.idempotency_key:
            self._remember_idempotency(req.idempotency_key, req.request_id, "approved")
        if self.ledger is not None:
            self.ledger.append("spend_request", "broadcasting", "manual_approval",
                               {"request_id": req.request_id, "kind": req.kind, "asset": req.asset,
                                "amount": str(req.amount), "payee": req.payee})
        # `_adapter` rebuilds once when an enabled chain is missing from the map: a node hiccup at
        # unlock used to turn the human's PIN into a permanent "denied" for a payable request.
        adapter = self._adapter(req.kind)
        if adapter is None:
            # Approving would otherwise record "approved" while no transaction exists — the receipt
            # would claim a payment the chain never saw.
            req.status = "denied"
            self._save_requests()      # otherwise the file still says pending after the refusal
            if self.ledger is not None:
                self.ledger.append("spend_request", "denied", "chain",
                                   {"request_id": req.request_id, "kind": req.kind,
                                    "asset": req.asset, "amount": str(req.amount),
                                    "payee": req.payee, "code": "chain_unavailable",
                                    "reason": f"no signer adapter for {req.kind}; nothing was sent"})
            raise WalletError("chain_unavailable",
                              f"no signer adapter for {req.kind} — nothing was sent, and the request "
                              f"is marked denied")
        req.status = "approved"
        self._save_requests()
        plan = self.plans.get(req.request_id)
        if plan is None and adapter is not None:
            # plans are never written to disk; re-plan now, which also refreshes nonce and fee
            try:
                account = 0
                if req.sandbox_id and req.sandbox_id in self.sandboxes:
                    # -1 marks a pooled budget: it has no account of its own, so it spends from main
                    account = max(0, self.sandboxes[req.sandbox_id].account_index)
                if req.swap:
                    # Re-quote before signing: the price can move while the card is on screen, and
                    # the minimum out the human approved is the line that has to hold. The on-chain
                    # minimum would revert anyway; this refuses without spending the gas.
                    quote_swap = getattr(adapter, "quote_swap", None)
                    plan_swap = getattr(adapter, "plan_swap", None)
                    if not callable(quote_swap) or not callable(plan_swap):
                        raise ChainError("no_swap_route", f"{req.kind} cannot swap")
                    fresh_quote = dict(quote_swap(req.swap["asset_in"], req.swap["asset_out"],
                                                  req.amount,
                                                  operator=str(req.swap.get("operator") or "")) or {})
                    if Decimal(str(fresh_quote["amount_out"])) < Decimal(str(req.swap["min_out"])):
                        raise ChainError("swap_price_moved",
                                         f"the route now returns {fresh_quote['amount_out']} "
                                         f"{req.swap['asset_out']}, below the {req.swap['min_out']} "
                                         f"minimum this card approved; nothing was signed")
                    plan = plan_swap(req.swap["asset_in"], req.amount,
                                     req.swap["asset_out"], Decimal(str(req.swap["min_out"])),
                                     operator=str(req.swap.get("operator") or ""))
                elif req.challenge and hasattr(adapter, "plan_authorization") \
                        and req.challenge.get("scheme") == "exact":
                    plan = self._plan_authorization(adapter, req)
                else:
                    plan = adapter.plan_transfer(req.amount, req.payee, account_index=account,
                                                 asset=req.asset)
                # The fee was re-read above; so must the floor, or a gas spike between the card and
                # the send would be the one case that gets through.
                self._check_gas_floor(req, plan)
                self.plans[req.request_id] = plan
            except ChainError as exc:
                req.status = "denied"
                self._save_requests()
                if self.ledger is not None:
                    self.ledger.append("spend_request", "denied", "chain",
                                       {"request_id": req.request_id, "kind": req.kind,
                                        "asset": req.asset, "amount": str(req.amount),
                                        "payee": req.payee, "code": exc.code,
                                        "reason": exc.message})
                raise WalletError(exc.code, exc.message) from exc
        try:
            execution = self._broadcast(req, plan, adapter)     # raises WalletError on chain failure
        except WalletError as exc:
            # `req.status` was already "approved"; without this the request list would show a payment
            # the receipt chain has no record of (locked, no plan, fee above the cap).
            self._fail_request(req, exc.code, exc.message)
            raise
        self._deliver_x402(req, plan, execution)
        self._settle_x402_status(req, execution)
        if self.ledger is not None:
            details = {"request_id": req.request_id, "kind": req.kind, "asset": req.asset,
                       "amount": str(req.amount), "payee": req.payee,
                       "sandbox_id": req.sandbox_id,
                       **(req.swap or {}),
                       **({"funding_for": req.funding_for} if req.funding_for else {}),
                       **({"sweep_of": req.sweep_of} if req.sweep_of else {}),
                       "code": "approved_by_operator", "reason": "operator approved",
                       **execution}
            # A conversion is not a payment: it must not show up in `spend` as money given away,
            # but it must be as provable as one — same chain, same signature, both hashes.
            self.ledger.append("swap" if req.swap else "spend_request", "approved",
                               "manual_approval", details)
        return {"request": req.to_dict(), **execution}

    def _settle_x402_status(self, req: "SpendRequest", execution: Dict[str, Any]) -> None:
        """`sent` means money moved. An x402 authorisation has moved nothing until the seller takes it.

        Approving one used to leave `status=sent` with no transaction hash while the balance never
        changed, and `wallet spend` counted it as real money — five payments that never happened.
        """
        if execution.get("settlement") != "x402":
            return
        if execution.get("x402_delivered") is True:
            req.status = "sent"
        elif execution.get("x402_delivered") is False:
            req.status = "authorised"          # signed, refused by the seller: nothing settled
        else:
            req.status = "authorised"          # signed, no delivery attempted
        self._save_requests()

    def _deliver_x402(self, req: "SpendRequest", plan: Any, execution: Dict[str, Any]) -> None:
        """Hand the signed authorisation to the seller, after the money has already moved.

        Shared by the operator path and the unattended (mandate/sandbox) path: only `approve` used to
        do this, so an auto-approved x402 payment recorded `settlement: x402` with no delivery and the
        seller never received anything, while the receipt said approved.
        """
        if execution.get("settlement") == "x402":
            target = str((req.challenge or {}).get("url") or plan.raw.get("resource") or "")
            try:
                # checked again here: the URL is about to be POSTed with a signed authorisation, and
                # a challenge that did not come from `pay_url` never gets that far (see request_tx)
                pay402._public_host(target, allow_loopback=bool((req.challenge or {}).get("_allow_loopback")))
            except pay402.QuoteError as exc:
                execution["x402_delivered"] = False
                execution["x402_error"] = f"{exc.code}: {exc.message}"
                target = ""
            if target:
                # The money has already left at this point: a delivery failure is a fact to report,
                # not an exception that hides a real payment from the operator.
                try:
                    execution["x402_response"] = pay402.submit(
                        target, plan.raw["x402"],
                        timeout=float(self.settings.http_timeout_seconds),
                        # the quote came from this URL with this allowance; the settlement goes back
                        # to the same place, or the bundled loopback seller can never settle
                        allow_loopback=bool((req.challenge or {}).get("_allow_loopback")))
                    execution["x402_delivered"] = True
                except Exception as exc:      # noqa: BLE001 - any submit failure is reported, never swallowed
                    execution["x402_delivered"] = False
                    execution["x402_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"


    def deny(self, params: Dict[str, Any]) -> Dict[str, Any]:
        # No unlock requirement: refusing a payment removes authority, and auto-lock can fire while
        # the operator is reading the approval card.
        approval_id = str(params.get("approval_id", ""))
        request_id = str(params.get("request_id", ""))
        if approval_id:
            req = self._consume_approval(approval_id, request_id)
        else:
            # Denying is a refusal, not an authorisation: it must not need the approval token the
            # operator may never have seen (a request listed in the CLI or the pane has an id, and
            # `deny <request-id>` used to fail with `unknown_approval`).
            req = self.requests.get(request_id)
            if req is None:
                raise WalletError("unknown_request", "request id is not recognised")
            if req.status != "pending":
                return {"request": req.to_dict(), "note": f"that request is already {req.status}"}
            live = self._live_approval(req.request_id)
            if live is not None:
                live.used = True   # a card still on screen must not be able to approve this now
        req.status = "denied"
        self.prompt = None
        if req.idempotency_key:
            self._remember_idempotency(req.idempotency_key, req.request_id, "denied")
        self._save_requests()
        if self.ledger is not None:
            self.ledger.append("spend_request", "denied", "operator",
                               {"request_id": req.request_id, "kind": req.kind, "asset": req.asset,
                                "amount": str(req.amount), "payee": req.payee,
                                "code": "denied_by_operator", "reason": str(params.get("reason", ""))[:200]})
        return {"request": req.to_dict()}

    def cancel_request(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Withdraw a request the agent filed, before anyone acts on it.

        Only a pending request can be cancelled: approved/denied/expired are history, and a request
        already being broadcast has left the building. Cancelling also burns every live approval
        token for it, so a card the human is still looking at cannot approve a payment the agent has
        withdrawn.
        """
        request_id = str(params.get("request_id") or "").strip()
        if not request_id:
            raise WalletError("missing_request_id", "which request should be cancelled?")
        req = self.requests.get(request_id)
        if req is None:
            raise WalletError("unknown_request", f"no request {request_id[:40]}")
        if req.status != "pending":
            raise WalletError("not_cancellable", f"request is already {req.status}")
        reason = str(params.get("reason") or "")[:200]
        req.status = "cancelled"
        for approval in list(self.approvals.values()):
            if approval.request_id == request_id and not approval.used:
                approval.used = True
        self._save_requests()
        self._remember_idempotency(req.idempotency_key, req.request_id, "cancelled")
        if self.ledger is not None:
            self.ledger.append("spend_request", "cancelled", "agent",
                               {"request_id": req.request_id, "kind": req.kind, "asset": req.asset,
                                "amount": str(req.amount), "payee": req.payee,
                                "code": "cancelled_by_agent", "reason": reason})
        return {"request": req.to_dict()}

    def requests_list(self) -> Dict[str, Any]:
        """Pending requests plus the approval id the UI needs to act on them."""
        now = datetime.now(timezone.utc)
        live: Dict[str, Approval] = {}
        for approval in self.approvals.values():
            if not approval.used and approval.expires_at > now:
                live.setdefault(approval.request_id, approval)
        rows = []
        for req in self.requests.values():
            row = req.to_dict()
            if req.status == "pending":
                # The approval window is measured from the request, not from the token: re-issuing on
                # every poll kept a dead request approvable forever.
                try:
                    age = (now - datetime.strptime(req.created_at, "%Y-%m-%dT%H:%M:%SZ")
                           .replace(tzinfo=timezone.utc)).total_seconds()
                except ValueError:
                    age = 0.0
                if age > self.settings.approval_ttl_seconds:
                    req.status = "expired"
                    row["status"] = "expired"
                    live.pop(req.request_id, None)
                    self._save_requests()
                elif req.request_id not in live:
                    live[req.request_id] = self._issue_approval(req)
            plan = self.plans.get(req.request_id)
            if plan is not None:
                row["fee"] = str(plan.fee)
                row["settles"] = "x402" if plan.raw.get("x402") else "onchain"
            approval = live.get(req.request_id)
            if approval is not None:
                row["approval_id"] = approval.approval_id
                row["approval_expires_at"] = approval.expires_at.strftime("%Y-%m-%dT%H:%M:%SZ")
            rows.append(row)
        return {"requests": rows}

    # ---------- paying an HTTP 402 ----------

    def pay_url(self, params: Dict[str, Any], unattended: bool = False) -> Dict[str, Any]:
        """Price a purchase from the server's own 402 demand, then open a normal spend request.

        The amount, asset and payee come from the response, never from the caller: the agent can ask
        to buy something, it cannot tell us what it costs or who to pay. A digest of the demand is
        bound into the request so the human approves these exact terms.
        """
        url = str(params.get("url", "")).strip()
        if not url:
            raise WalletError("bad_url", "a url is required")
        try:
            # the caller may ask for a shorter timeout but never a longer one: this fetch runs inside
            # the daemon's mutation lock, and a slow URL must not hold every other call hostage
            try:
                asked = float(params.get("timeout") or 15.0)
            except (TypeError, ValueError):
                asked = 15.0
            # `allow_loopback` is set by the operator CLI's demo path; the agent token cannot ask for
            # it, which is why the scope is checked here rather than trusted from the params
            loopback_ok = (bool(params.get("allow_loopback"))
                           and str(params.get("_scope", "")) == OPERATOR)
            quote = pay402.fetch(url, timeout=max(1.0, min(asked, 15.0)),
                                 allow_loopback=loopback_ok)
        except pay402.QuoteError as exc:
            # Not every checkout speaks 402. The agent's job for "buy this" is to find the checkout;
            # the wallet's job is to read the invoice that page states — and to refuse when it cannot.
            if exc.code == "not_payment":
                return self._pay_invoice_page(url, params, loopback_ok,
                                              timeout=max(1.0, min(asked, 15.0)),
                                              unattended=unattended)
            if self.ledger is not None:
                self.ledger.append("pay_url", "denied", "quote",
                                   {"url": url[:300], "code": exc.code, "reason": exc.message[:200]})
            raise WalletError(exc.code, exc.message) from exc

        request = self.request_tx({
            "kind": quote.kind,
            "asset": quote.asset,
            "amount": str(quote.amount),
            "payee": quote.payee,
            "memo": (quote.description or quote.resource or url)[:120],
            # No fabricated reason: "prefer Monero" asks the agent why it needs this coin, and a
            # system-generated string would answer for it. A 402 priced in USDC therefore needs the
            # caller to say why (the tool schema asks for it), or the request is refused like any other.
            "why": str(params.get("why") or "")[:300],
            "sandbox_id": str(params.get("sandbox_id", "")),
            "profile": str(params.get("profile", "")),
            # the caller's scope has to survive this hop: without it the policy engine reads a
            # 402 purchase as operator-initiated and skips the Monero preference and the sandbox
            # destination check applies to a different request than the one being priced
            "_scope": str(params.get("_scope") or ""),
            "provenance": {"url": url, "host": (urllib.parse.urlparse(url).hostname or ""),
                           "title": quote.description or quote.resource or "",
                           "method": "402", "extracted_at": now_iso(), "verified": "true"},
            "challenge_sha256": quote.digest,
            # only `pay_url` may attach a challenge: it is what the approval later POSTs to, so an
            # agent-supplied one would be an SSRF after the signature exists

            "challenge": {**quote.raw, "resource": quote.resource, "network": quote.network,
                          "url": url, "scheme": quote.scheme,
                          "_allow_loopback": loopback_ok},
            "idempotency_key": str(params.get("idempotency_key") or "")[:64],
        }, unattended=unattended, verified_provenance=True, from_pay_url=True)
        return {"quote": quote.to_dict(), **request}

    def _pay_invoice_page(self, url: str, params: Dict[str, Any], loopback_ok: bool,
                          timeout: float, unattended: bool = False) -> Dict[str, Any]:
        """Pay an invoice stated on a page that is not a 402 endpoint.

        "buy this" is not a directory of shops: the agent finds the checkout with its own web tools and
        hands the URL here. Three rules keep that honest — the address must be on the page, an
        ambiguous page is a question for the human, and the provenance travels with the request so the
        card can show *where* the address came from.
        """
        try:
            page = invoice_mod.fetch(url, timeout=timeout, allow_loopback=loopback_ok)
            target = invoice_mod.one(page)
        except pay402.QuoteError as exc:
            if self.ledger is not None:
                self.ledger.append("pay_url", "denied", "invoice",
                                   {"url": url[:300], "code": exc.code,
                                    "reason": exc.message[:200]})
            raise WalletError(exc.code, exc.message) from exc

        amount = params.get("amount")
        if target.amount is not None and amount in (None, ""):
            amount = format(target.amount, "f")
        if amount in (None, ""):
            raise WalletError(
                "amount_required",
                f"{url} names an address ({target.payee[:12]}…) but no amount — pass the amount, "
                f"e.g. 10$ or 0.001 {target.asset}")
        # the page may be a stagenet/testnet invoice; the address's checksum says which network it is
        kind = getattr(target, "kind", "") or self.DEFAULT_CHAIN.get(target.asset, "")
        if not kind:
            raise WalletError("bad_chain", f"this wallet has no chain for {target.asset}")
        host = urllib.parse.urlparse(url).hostname or ""
        return self.request_tx({
            "kind": kind,
            "asset": target.asset,
            "amount": amount,
            "payee": target.payee,
            "memo": (page.title or url)[:120],
            "why": str(params.get("why") or "")[:300],
            "sandbox_id": str(params.get("sandbox_id", "")),
            "profile": str(params.get("profile", "")),
            "_scope": str(params.get("_scope") or ""),
            "provenance": {"url": url, "host": host, "title": page.title,
                           "method": f"invoice-{target.source}",
                           "extracted_at": now_iso()},
            "idempotency_key": str(params.get("idempotency_key") or "")[:64],
        }, unattended=unattended, verified_provenance=True)

    # ---------- receiving: a fresh address per purchase ----------

    def receive_address(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Give out an address for one incoming payment, on its own subaddress.

        Reusing one address for every purchase links them together on-chain for anyone who sees two
        of the payments — the buyer's shop, a shipping label, a forum post. A fresh subaddress per
        purchase costs nothing and breaks that link, and it lets the wallet afterwards say which
        payment arrived for which order.
        """
        self._require_unlocked()
        kind = str(params.get("kind") or "").strip()
        if not kind:
            monero = [k for k in self.settings.chains_enabled if k.startswith("monero")]
            if not monero:
                raise WalletError("chain_not_enabled", "no Monero chain is enabled")
            kind = monero[0]
        adapter = self.adapters.get(kind)
        if adapter is None:
            raise WalletError("chain_unavailable", f"no signer for {kind}")
        if not getattr(adapter, "supports_accounts", False):
            # This used to hand back an `ord_…` that it never wrote anywhere: `wallet_orders` showed
            # nothing, and the agent told the user the order existed. A client then pays an address
            # with no record behind it — worse than a refusal, because it is a receipt that is not.
            raise WalletError(
                "unsupported",
                f"{kind} cannot issue a per-order address (no subaddresses or accounts). Use a Monero "
                f"chain for invoicing, or pass the wallet's own address explicitly and reconcile the "
                f"payment by hand.")

        label = str(params.get("label") or params.get("memo") or "payment")[:40]
        order_id = str(params.get("order_id") or "").strip()[:64] or f"ord_{os.urandom(4).hex()}"
        expected = str(params.get("expected") or params.get("amount") or "").strip()
        if expected:
            try:
                if not Decimal(expected).is_finite() or Decimal(expected) <= 0:
                    raise InvalidOperation
            except (InvalidOperation, ArithmeticError):
                raise WalletError("bad_amount", f"expected {expected!r} is not a positive amount") from None
            expected = f"{Decimal(expected):f}"
        created = adapter.create_account(label)
        if self.ledger is not None:
            self.ledger.append("receive_address", "created", "agent",
                               {"kind": kind, "account_index": created["account_index"],
                                "address": created["address"], "label": label,
                                "expected": expected, "order_id": order_id})
        return {"kind": kind, "address": created["address"],
                "account_index": created["account_index"], "label": label,
                "expected": expected, "order_id": order_id, "reused": False}

    # ---------- deposit addresses: receive here, merge home ----------

    def deposit_create(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """A named receive account whose incoming payments are merged into the main balance.

        Monero can already hand out a subaddress, but a subaddress *is* account 0: the money is already
        in the main balance and there is nothing to merge, nothing to show per deposit. A separate
        account gives the address its own balance and a real merge step with a receipt.
        """
        self._require_unlocked()
        kind = str(params.get("kind") or "").strip()
        if not kind:
            monero = [k for k in self.settings.chains_enabled if k.startswith("monero")]
            if not monero:
                raise WalletError("chain_not_enabled", "no Monero chain is enabled")
            kind = monero[0]
        adapter = self._adapter(kind)
        if adapter is None:
            raise WalletError("chain_unavailable", f"chain {kind} is not available")
        if not getattr(adapter, "supports_accounts", False):
            raise WalletError("unsupported", f"{kind} cannot hold separate receive accounts")
        # Opening a deposit address is free, but arming it to merge automatically is a policy
        # decision: it makes the daemon spend a fee on its own, on a clock, without asking again.
        if bool(params.get("auto_merge", True)):
            self._require_passphrase(params, "arming a deposit address to merge by itself")
        label = str(params.get("label") or "")[:40]
        try:
            created = adapter.create_account(label or "deposit")
        except ChainError as exc:
            raise WalletError(exc.code, exc.message) from exc
        index = int(created.get("account_index", 0))
        address = str(created.get("address") or adapter.address_for(index))
        deposit = DepositAddress(deposit_id="dep_" + secrets.token_hex(4), kind=kind,
                                 account_index=index, address=address, label=label,
                                 created_at=now_iso(),
                                 auto_merge=bool(params.get("auto_merge", True)))
        self.deposits[deposit.deposit_id] = deposit
        self._save_deposits()
        self._record("deposit", "created", "operator",
                     {"deposit_id": deposit.deposit_id, "kind": kind, "account_index": index,
                      "label": label, "auto_merge": deposit.auto_merge})
        return {"deposit": self._deposit_row(deposit)}

    def _adapter(self, kind: str):
        """The adapter for a chain, rebuilding once if it is enabled but missing.

        `_build_adapters` only registers a chain that answers *at that moment*, so a transient failure
        (a node hiccup, the signer starting) can leave an enabled chain unregistered until the next
        unlock. A read path that needs it — a deposit balance, a merge — should not report "chain
        unavailable" for a chain the operator has enabled and can see working.
        """
        adapter = self.adapters.get(kind)
        if adapter is None and kind in self.settings.chains_enabled and self.is_unlocked():
            self._build_adapters()
            adapter = self.adapters.get(kind)
        return adapter

    def _deposit_row(self, deposit: DepositAddress) -> Dict[str, Any]:
        row = deposit.to_dict()
        adapter = self._adapter(deposit.kind)
        reader = getattr(adapter, "account_balance", None) if adapter is not None else None
        if reader is not None:
            try:
                live = reader(deposit.account_index)
                row["balance"] = str(live.get("balance", "0"))
                row["unlocked"] = str(live.get("unlocked", "0"))
                row["last_error"] = deposit.last_error
            except Exception as exc:  # noqa: BLE001 - an unreadable balance is a state, not a crash
                row["balance"] = None
                row["error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
        else:
            row["balance"] = None
            row["error"] = f"chain {deposit.kind} is not available"
        return row

    def deposits_list(self) -> Dict[str, Any]:
        rows = [self._deposit_row(d) for d in self.deposits.values()]
        return {"deposits": rows, "count": len(rows),
                "auto_merge": sum(1 for d in self.deposits.values() if d.auto_merge)}

    def deposit_set_auto(self, params: Dict[str, Any]) -> Dict[str, Any]:
        deposit = self.deposits.get(str(params.get("deposit_id", "")))
        if deposit is None:
            raise WalletError("unknown_deposit", "deposit id is not recognised")
        if "auto_merge" in params:
            if bool(params["auto_merge"]) and not deposit.auto_merge:
                self._require_passphrase(params, "arming a deposit address to merge by itself")
            deposit.auto_merge = bool(params["auto_merge"])
            self._save_deposits()
            self._record("deposit", "auto_changed", "operator",
                         {"deposit_id": deposit.deposit_id, "auto_merge": deposit.auto_merge})
        return {"deposit": self._deposit_row(deposit)}

    def _merge_account(self, kind: str, account_index: int, label: str,
                       ref: Optional[Dict[str, Any]] = None,
                       receipt_kind: str = "deposit") -> Dict[str, Any]:
        """Sweep one of our own accounts into account 0. Shared by deposits and paid orders.

        The destination is read from the adapter (`address_for(0)`), never from a parameter, and the
        transfer spends *from* the account — so this is a self-transfer: no caps, no allowlist, no PIN,
        and always a receipt saying exactly what moved.
        """
        ref = dict(ref or {})
        self._require_unlocked()
        # Account 0 IS the main balance: "merging" it pays a fee to move our money to ourselves,
        # which a forged deposits.json could make the reaper do every five minutes for ever.
        if int(account_index) <= 0:
            raise WalletError("bad_deposit", "a merge must come from a sub-account, not from account 0")
        # …and a sandbox is also one of our accounts: sweeping it here would empty a budget the agent
        # is meant to spend from, and close it, without the operator or a receipt saying so.
        for sandbox in self.sandboxes.values():
            if sandbox.account_index == int(account_index) and sandbox.kind == kind:
                raise WalletError("sandbox_account",
                                  "that account is a sandbox budget; use `sandbox sweep` for it")
        adapter = self._adapter(kind)
        if adapter is None:
            raise WalletError("chain_unavailable", f"chain {kind} is not available")
        reader = getattr(adapter, "account_balance", None)
        if reader is None:
            raise WalletError("unsupported", f"{kind} cannot report an account balance")
        live = reader(account_index)
        unlocked = Decimal(str(live.get("unlocked", "0")))
        if unlocked <= 0:
            return {"merged": "0", "sent": False,
                    "note": "nothing spendable yet — incoming Monero unlocks ten blocks after it confirms"}
        try:
            dest = adapter.address_for(0)
        except (ChainError, AttributeError) as exc:
            raise WalletError("chain_unavailable", str(exc)[:160]) from exc
        asset = self._chain_assets(kind)[0]
        reserve = self._fee_reserve(kind, asset)
        amount = unlocked - reserve
        if amount <= 0:
            # Said once per balance, not once per tick: the reaper calls this every five minutes,
            # and a dust account used to add a signed receipt to the chain each time, for ever.
            seen = getattr(self, "_merge_skips", None)
            if seen is None:
                self._merge_skips = seen = {}
            if seen.get((kind, int(account_index))) != str(unlocked):
                seen[(kind, int(account_index))] = str(unlocked)
                self._record("deposit", "merge_skipped", "operator",
                             {**ref, "left": str(unlocked), "reserve": str(reserve),
                              "reason": "below a transfer fee"})
            return {"merged": "0", "sent": False,
                    "note": f"{unlocked} {asset} does not cover a transfer fee"}
        try:
            plan = adapter.plan_transfer(amount, dest, account_index=account_index, asset=asset)
        except ChainError as exc:
            self._record("deposit", "merge_failed", "chain",
                         {**ref, "code": exc.code, "reason": exc.message[:200]})
            raise WalletError(exc.code, exc.message) from exc
        fee = Decimal(str(getattr(plan, "fee", 0) or 0))
        if fee > 0 and fee != reserve:
            amount = unlocked - fee
            if amount <= 0:
                return {"merged": "0", "sent": False, "note": "the fee eats the balance"}
            plan = adapter.plan_transfer(amount, dest, account_index=account_index, asset=asset)

        req = SpendRequest(request_id="req_" + secrets.token_hex(6), kind=kind, asset=asset,
                           amount=amount, payee=dest, memo=f"merge {label}"[:200],
                           why="our own money, moved into the main account",
                           created_at=now_iso(), **{k: v for k, v in ref.items() if k == "deposit_id"})
        req.authorised_by = "operator"
        req.status = "approved"
        req.fee_estimate = str(fee)
        self.requests[req.request_id] = req
        self._save_requests()
        try:
            execution = self._broadcast(req, plan, adapter)
        except WalletError as exc:
            req.status = "failed"
            self._save_requests()
            self._record("deposit", "merge_failed", "broadcast",
                         {**ref, "code": exc.code, "reason": exc.message[:200]})
            raise
        tx = str(execution.get("tx_hash") or "")
        self._record(receipt_kind, "merged", "operator",
                     {**ref, "kind": kind, "account_index": account_index,
                      "amount": format(amount, "f"), "fee": str(fee), "tx_hash": tx, "to": dest,
                      **({"simulated": True} if kind.startswith("sim") else {})})
        return {"merged": format(amount, "f"), "sent": True, "fee": str(fee), "tx_hash": tx}

    def deposit_merge(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Operator: merge one deposit account into the main account now."""
        deposit = self.deposits.get(str(params.get("deposit_id", "")))
        if deposit is None:
            raise WalletError("unknown_deposit", "deposit id is not recognised")
        result = self._merge_account(deposit.kind, deposit.account_index,
                                     deposit.label or deposit.deposit_id,
                                     {"deposit_id": deposit.deposit_id})
        if result.get("sent"):
            deposit.merged_total = format(Decimal(deposit.merged_total or "0")
                                          + Decimal(result["merged"]), "f")
            deposit.last_merge_tx = result.get("tx_hash", "")
            deposit.last_merge_at = now_iso()
            deposit.last_error = ""
            self._save_deposits()
        return {**result, "deposit": self._deposit_row(deposit)}

    #: order accounts looked at per merge pass (one balance read each)
    ORDERS_PER_PASS = 40

    def orders_merge(self, limit: int = 4) -> List[Dict[str, Any]]:
        """Sweep paid *order* accounts into the main balance.

        `receive_address` hands out a fresh account per order, and money paid into it used to sit
        there for ever: `balances()` reads account 0, so a paid order was invisible in the balance and
        unspendable. Each settled account is merged once, with an `order/merged` receipt, and the join
        to the order id comes from the `receive_address` receipt that opened it.
        """
        if not self.is_unlocked() or self.ledger is None:
            return []
        # The ledger is the source of "which accounts are ours" here, so it has to be the *verified*
        # ledger: `tail()` parses whatever is on disk, and a forged `receive_address` line used to be
        # enough to make this sweep an arbitrary account into the main balance.
        try:
            ok, message, _count = self.ledger.verify()
        except Exception as exc:  # noqa: BLE001 - an unreadable chain is not a licence to act
            ok, message = False, str(exc)
        if not ok:
            self._record("order", "merge_refused", "policy",
                         {"reason": f"the receipt chain does not verify: {message}"[:200]})
            return []
        # The whole live chain, not its last 400 entries: an order paid late (after 400 other
        # receipts) fell out of that window and its money stayed in an account nothing reads.
        entries = list(self.ledger.entries())
        merged_ids = {(e.get("details") or {}).get("order_id")
                      for e in entries
                      if e.get("kind") == "order" and e.get("decision") == "merged"}
        candidates: List[tuple] = []
        for entry in entries:
            if entry.get("kind") != "receive_address" or entry.get("decision") != "created":
                continue
            details = entry.get("details") or {}
            order_id = str(details.get("order_id") or "")
            kind = str(details.get("kind") or "")
            try:
                account = int(details.get("account_index", 0) or 0)
            except (TypeError, ValueError):
                continue
            if not order_id or order_id in merged_ids or account <= 0:
                continue
            candidates.append((order_id, kind, account))
        # `limit` bounds the transfers per call, not the orders looked at. It used to count every
        # order examined, so four unpaid orders at the head of the list were re-checked on every tick
        # and no order behind them was ever merged. A rotating cursor keeps one call cheap (a balance
        # read per order) without letting any order wait for ever.
        results: List[Dict[str, Any]] = []
        sent = 0
        start = getattr(self, "_orders_cursor", 0) % len(candidates) if candidates else 0
        ordered = candidates[start:] + candidates[:start]
        examined = 0
        for order_id, kind, account in ordered:
            if sent >= limit or examined >= self.ORDERS_PER_PASS:
                break
            examined += 1
            try:
                # `receipt_kind="order"` means the merge writes ONE order/merged receipt (with its
                # fee); writing a deposit/merged one too made a single transfer look like two payments
                # when anything summed the ledger.
                result = self._merge_account(kind, account, f"order {order_id}",
                                             {"order_id": order_id}, receipt_kind="order")
            except WalletError as exc:
                results.append({"order_id": order_id, "sent": False,
                                "error": f"{exc.code}: {exc.message}"[:200]})
                continue
            if result.get("sent"):
                sent += 1
            results.append({"order_id": order_id, **result})
        self._orders_cursor = start + examined
        return results

    def deposits_auto_merge(self, limit: int = 4) -> List[Dict[str, Any]]:
        """Merge every deposit address that asked for it. Safe to call on a timer.

        One failure must not stop the others: a chain that is down for one deposit is recorded on that
        deposit and the loop moves on.
        """
        merged: List[Dict[str, Any]] = []
        for deposit in list(self.deposits.values()):
            if not deposit.auto_merge or len(merged) >= limit:
                continue
            adapter = self._adapter(deposit.kind)
            reader = getattr(adapter, "account_balance", None) if adapter is not None else None
            if reader is None:
                continue
            try:
                if Decimal(str(reader(deposit.account_index).get("unlocked", "0"))) <= 0:
                    continue
            except Exception:  # noqa: BLE001 - an unreadable account is skipped, not fatal
                continue
            try:
                merged.append({"deposit_id": deposit.deposit_id, **self.deposit_merge(
                    {"deposit_id": deposit.deposit_id})})
            except WalletError as exc:
                merged.append({"deposit_id": deposit.deposit_id, "sent": False,
                               "error": f"{exc.code}: {exc.message}"[:200]})
        return merged

    def incoming(self) -> Dict[str, Any]:
        """Incoming orders: every labelled subaddress, and what has landed in it.

        `receive_address` gives each purchase its own subaddress and writes a receipt for it, while
        the chain reports the balance sitting on each subaddress. Neither side alone answers "is
        this order paid?": the receipt knows what the address was opened for, the chain knows the
        money. This joins them on (kind, account_index).
        """
        self._require_unlocked()
        opens: List[tuple] = []
        last_tx: Dict[str, str] = {}
        for entry in self._receipt_entries():
            details = entry.get("details") or {}
            tx_hash = str(details.get("tx_hash") or "")
            if tx_hash:
                for key in (str(details.get("address") or ""), str(details.get("payee") or "")):
                    if key:
                        last_tx[key] = tx_hash
            if entry.get("kind") == "receive_address" and entry.get("decision") == "created":
                opens.append((str(entry.get("ts", "")), details))

        closed_at_by_order: Dict[str, str] = {}
        for entry in self._receipt_entries():
            details = entry.get("details") or {}
            if entry.get("kind") == "order" and entry.get("decision") == "closed":
                order_id = str(details.get("order_id") or "")
                if order_id:
                    closed_at_by_order[order_id] = str(entry.get("ts") or "")

        notes: List[str] = []
        orders: List[Dict[str, Any]] = []
        accounts_by_kind: Dict[str, Dict[int, Dict[str, Any]]] = {}
        unsupported: set = set()
        unreachable: set = set()
        now = datetime.now(timezone.utc)
        for _ts, details in sorted(opens, key=lambda row: row[0]):
            kind = str(details.get("kind") or "")
            address = str(details.get("address") or "")
            try:
                index = int(details.get("account_index") or 0)
            except (TypeError, ValueError):
                index = 0
            adapter = self.adapters.get(kind)
            if adapter is None:
                # Not the same thing as "this chain has no subaddresses": a chain that is simply not
                # reachable (locked, signer down, restored home) used to be reported as if Monero
                # reused one address for every payment.
                unreachable.add(kind or "?")
                continue
            if not getattr(adapter, "supports_accounts", False):
                unsupported.add(kind or "?")
                continue
            if kind not in accounts_by_kind:
                try:
                    accounts_by_kind[kind] = {int(row.get("account_index", 0)): row
                                              for row in adapter.accounts()}
                except Exception as exc:          # noqa: BLE001 - one dead chain must not hide the rest
                    accounts_by_kind[kind] = {}
                    notes.append(f"{kind}: could not read subaddress balances "
                                 f"({type(exc).__name__}: {str(exc)[:80]})")
            account = accounts_by_kind[kind].get(index) or {}
            received = str(account.get("balance", "0") or "0")
            expected = str(details.get("expected") or "")
            try:
                held = Decimal(received)
                want = Decimal(expected) if expected else None
            except (InvalidOperation, ArithmeticError):
                held, want = Decimal("0"), None
            if held <= 0:
                status = "waiting"
            elif want is None or held >= want:
                status = "paid"
            else:
                status = "partial"
            order_id = str(details.get("order_id") or "") or f"ord_legacy_{kind}_{index}"
            closed = closed_at_by_order.get(order_id, "")
            if closed:
                status = "closed"
            opened = str(_ts or "")
            try:
                age = int((now - datetime.strptime(opened, "%Y-%m-%dT%H:%M:%SZ")
                           .replace(tzinfo=timezone.utc)).total_seconds())
            except ValueError:
                age = None
            orders.append({
                "order_id": order_id,
                "label": str(details.get("label") or ""),
                "address": address or str(account.get("address", "")),
                "account_index": index,
                "kind": kind,
                "asset": self.NATIVE_ASSET.get(kind) or getattr(adapter, "asset", ""),
                "expected": expected,
                "received": f"{held:f}",
                "status": status,
                "opened_at": opened,
                "age_seconds": age,
                "closed_at": closed,
                "last_tx": last_tx.get(address, ""),
            })

        if unreachable:
            notes.append("could not reach " + ", ".join(sorted(unreachable))
                         + " right now, so its orders are missing from this list "
                           "(unlock the wallet and start the signer)")
        if unsupported:
            notes.append("no subaddresses on " + ", ".join(sorted(unsupported))
                         + "; that chain reuses the wallet address")
        if not orders and not unsupported and not unreachable and not any(
                getattr(a, "supports_accounts", False) for a in self.adapters.values()):
            notes.append("no enabled chain has subaddresses; the wallet address is reused")
        # Orders the operator has forgotten. `receive_address` writes a receipt per address and the
        # receipt chain is archived, not deleted, so without this a wallet used for testing keeps
        # listing phantom orders forever.
        purged = self._purged_orders()
        if purged:
            orders = [row for row in orders if str(row.get("order_id")) not in purged]
        counts = {name: 0 for name in ("waiting", "partial", "paid", "closed")}
        for row in orders:
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        return {"orders": orders, "counts": counts, "note": "; ".join(notes)}

    def _purged_orders(self) -> set:
        """Order ids the operator has forgotten, read from disk (never secret, never signed)."""
        try:
            raw = json.loads((self.home / "orders-purged.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return set()
        return {str(x) for x in raw} if isinstance(raw, list) else set()

    def orders_purge(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Operator: forget incoming orders that never received anything.

        Archiving the ledger does not forget them — `receive_address` wrote a receipt per address and
        the chain keeps it — so a wallet used for testing lists phantom orders forever. Only rows
        with nothing received and no transaction are dropped; an order paid even in part stays, and
        the money is untouched either way.
        """
        self._require_unlocked()
        dropped: List[str] = []
        kept: List[str] = []
        for order in self.incoming().get("orders", []):
            received = str(order.get("received") or "0")
            try:
                empty = Decimal(received) <= 0
            except (InvalidOperation, ArithmeticError):
                empty = False
            # Nothing arrived, so there is nothing to lose by forgetting the row: the money sits in
            # the account, not in this list, and a legacy row can carry a *simulated* tx hash.
            if empty:
                dropped.append(str(order.get("order_id") or ""))
            else:
                kept.append(str(order.get("order_id") or ""))
        purged = self._purged_orders()
        purged.update(name for name in dropped if name)
        try:
            (self.home / "orders-purged.json").write_text(
                json.dumps(sorted(purged), indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            raise WalletError("write_failed", f"could not record the purge: {exc}") from exc
        return {"purged": dropped, "kept": kept}

    def orders_close(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Close an incoming order so the list stops calling it outstanding.

        Bookkeeping only: money already received stays where it is, the order keeps its address and
        history, and the close itself is a receipt (an order that vanished without one would be a
        payment nobody could account for).
        """
        self._require_unlocked()
        order_id = str(params.get("order_id") or "").strip()
        if not order_id:
            raise WalletError("bad_order", "an order_id is required (see `incoming`)")
        known = {str(row.get("order_id")): row for row in self.incoming()["orders"]}
        if order_id not in known:
            raise WalletError("unknown_order", f"no order {order_id!r} is open")
        if known[order_id].get("status") == "closed":
            return {"order_id": order_id, "status": "closed", "already": True}
        if self.ledger is not None:
            self.ledger.append("order", "closed", "agent",
                               {"order_id": order_id, "received": known[order_id].get("received", ""),
                                "expected": known[order_id].get("expected", ""),
                                "reason": str(params.get("reason") or "")[:200]})
        return {"order_id": order_id, "status": "closed", "already": False}

    def verify_payment(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Prove a payment arrived. XMR needs the payer's tx key; BTC/EVM are public.

        On Monero, amounts and parties are hidden, so the tx key is the only proof a recipient can
        check. On Bitcoin and the EVM chains the transaction itself says who was paid and how much,
        so the txid is enough — and no key has to travel.
        """
        self._require_unlocked()
        txid = str(params.get("txid", "")).strip()
        tx_key = str(params.get("tx_key", "")).strip()
        address = str(params.get("address", "")).strip()
        if not (txid and address):
            raise WalletError("bad_proof", "txid and address are required")

        kind = str(params.get("kind") or "").strip()
        if not kind:
            monero = [k for k in self.settings.chains_enabled if k.startswith(("monero", "sim-"))]
            kind = monero[0] if monero else ""
        adapter = self.adapters.get(kind)
        if adapter is None or not hasattr(adapter, "payment_proof"):
            raise WalletError("chain_unavailable",
                              f"no adapter for {kind or 'that chain'}; start the signer or name a kind")
        is_monero = str(getattr(adapter, "kind", kind)).startswith(("monero", "sim-"))
        if is_monero and not tx_key:
            raise WalletError("bad_proof", "a Monero payment proof needs the payer's tx_key")

        try:
            if is_monero:
                proof = adapter.payment_proof(txid, tx_key, address)
            else:
                try:
                    proof = adapter.payment_proof(txid, address, asset=str(params.get("asset", "")))
                except TypeError:
                    # Bitcoin's proof has no asset: the outputs are the amount
                    proof = adapter.payment_proof(txid, address)
        except ChainError as exc:
            # "transaction not found" or a wrong key is an answer, not a failure; only a dead endpoint
            # is something the caller has to act on.
            if exc.code in ("rpc_unreachable", "signer_unreachable"):
                raise WalletError("signer_unreachable", exc.message) from exc
            proof = {"received": False, "amount": "0", "confirmations": 0,
                     "reason": exc.message[:160]}

        if self.ledger is not None:
            self.ledger.append("payment_proof", "checked", "agent",
                               {"txid": txid[:80], "address": address[:120],
                                "kind": getattr(adapter, "kind", kind), **proof})
        return {"kind": getattr(adapter, "kind", kind), **proof}

    # ---------- mandates ----------

    def _match_mandate(self, req: SpendRequest) -> Optional[Mandate]:
        for mandate in self.mandates.values():
            if mandate.status() != "active":
                continue
            if (mandate.kind, mandate.asset, mandate.payee) == (req.kind, req.asset, req.payee):
                return mandate
        return None

    def _reconcile_sandbox_spend(self) -> None:
        """A crash between "broadcast" and "debit the budget" must not re-open that budget.

        `spent` is bookkeeping; the requests are the record. Any sandbox whose own sent requests add
        up to more than the stored `spent` had a crash in the gap, so the difference is charged here.
        Only ever upward: a swept sandbox legitimately sits at `spent == budget` with fewer sends.
        """
        totals: Dict[str, Any] = {}
        for req in self.requests.values():
            if req.status != "sent" or not req.sandbox_id:
                continue
            try:
                charged = req.amount + Decimal(str(req.fee_paid or "0"))
            except (InvalidOperation, ValueError):
                charged = req.amount
            totals[req.sandbox_id] = totals.get(req.sandbox_id, Decimal("0")) + charged
        changed = False
        for sandbox_id, charged in totals.items():
            sandbox = self.sandboxes.get(sandbox_id)
            if sandbox is None or charged <= sandbox.spent:
                continue
            gap = charged - sandbox.spent
            sandbox.spent = charged
            changed = True
            self._record("sandbox", "spend_reconciled", "policy",
                         {"sandbox_id": sandbox_id, "charged": str(charged),
                          "recovered": str(gap),
                          "note": "a restart found sent payments the budget had not been charged for"})
        if changed:
            self._save_sandboxes()

    def _load_requests(self) -> None:
        """Pending requests survive a restart; their plans do not.

        A plan holds a signed transaction — key-equivalent material — so it is deliberately not
        written to disk. The request is re-planned at approval time instead, which also picks up a
        fresh nonce and fee. Losing the request entirely (the old behaviour) meant a human approving
        a payment that the daemon no longer knew about.
        """
        self.requests = {}
        try:
            raw = json.loads(self.requests_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        for row in raw.get("requests", []):
            try:
                self.requests[str(row["request_id"])] = SpendRequest(
                    request_id=str(row["request_id"]), kind=str(row["kind"]),
                    asset=str(row["asset"]), amount=Decimal(str(row["amount"])),
                    payee=str(row["payee"]), memo=str(row.get("memo", "")),
                    why=str(row.get("why", "")), sandbox_id=str(row.get("sandbox_id", "")),
                    profile=str(row.get("profile", "")), funding_for=str(row.get("funding_for", "")),
                    sweep_of=str(row.get("sweep_of", "")),
                    challenge_sha256=str(row.get("challenge_sha256", "")),
                    challenge=dict(row.get("challenge") or {}),
                    idempotency_key=str(row.get("idempotency_key", "")),
                    created_at=str(row.get("created_at", now_iso())),
                    status=str(row.get("status", "pending")),
                    tx_hash=str(row.get("tx_hash", "")),
                    authorised_by=str(row.get("authorised_by", "")),
                    fee_paid=str(row.get("fee_paid", "")),
                    fee_estimate=str(row.get("fee_estimate", "")),
                    fiat=dict(row.get("fiat") or {}),
                    # The file is plain JSON under a directory anything running as this uid can write,
                    # so a stored "verified: true" would be a claim nobody signed. Provenance read from
                    # disk is always marked unverified; the signed receipt keeps the truth.
                    deposit_id=str(row.get("deposit_id", "")),
                    gas=dict(row.get("gas") or {}),
                    swap=dict(row.get("swap") or {}),
                    provenance={**dict(row.get("provenance") or {}),
                                **({"verified": "false"}
                                   if row.get("provenance") else {})})
            except (KeyError, ArithmeticError, ValueError):
                continue
        self._reconcile_sandbox_spend()
        # A request that says "approved" but never got a tx hash died between the approval and the
        # broadcast (or the ledger write). It cannot be trusted as either: mark it failed so nothing
        # retries it silently, and say so when the ledger is open.
        reconciled = False
        for req in self.requests.values():
            if req.status == "approved" and not req.tx_hash:
                req.status = "failed"
                self._reconciled_approved = getattr(self, "_reconciled_approved", []) + [req.request_id]
                reconciled = True
        if reconciled:
            # without this the file keeps saying "approved" and every later load re-decides it
            self._save_requests()
        # Approval tokens are in memory only, so a request loaded without one can never be approved.
        # Doing this at load time (not only in unlock) means no path can resurrect a dead request.
        self._expire_unapprovable_requests()
        # keep the file bounded: 200 most recent, and drop anything older than a week
        if len(self.requests) > 200:
            ordered = sorted(self.requests.values(), key=lambda r: r.created_at)
            for stale in ordered[:-200]:
                self.requests.pop(stale.request_id, None)

    def _save_requests(self) -> None:
        # One serializer. This used to be a hand-written copy of the fields, and it silently dropped
        # anything added to `to_dict` later (`gas`, then `swap`) — so a card that survived a restart
        # lost the part that said what it was, and a swap came back as a plain transfer to a router.
        rows = []
        for r in self.requests.values():
            row = r.to_dict()
            # persisted so a restart keeps the card honest — and demoted to `verified: false`
            # on load, because this file is not signed
            row["provenance"] = {k: v for k, v in dict(r.provenance).items() if k != "verified"}
            rows.append(row)
        self._write_private_json(self.requests_path, {"requests": rows})

    def _load_idempotency(self) -> None:
        """key -> {request_id, status, created_at}.

        The record has to outlive the request object: `requests.json` is pruned to the newest 200, so
        a key whose request was pruned must still be answerable, or a retry becomes a second payment.
        """
        try:
            raw = json.loads(self.idempotency_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            self.idempotency = {}
            return
        rows: Dict[str, Dict[str, Any]] = {}
        for key, value in (raw.get("keys") or {}).items():
            if isinstance(value, dict):
                rows[str(key)] = value
            else:                              # older format: key -> request_id
                rows[str(key)] = {"request_id": str(value), "status": "unknown", "created_at": ""}
        self.idempotency = rows

    def _save_idempotency(self) -> None:
        # keep the file bounded by AGE, not by insertion order of a sort_keys dump
        if len(self.idempotency) > 500:
            newest = sorted(self.idempotency.items(),
                            key=lambda kv: kv[1].get("created_at", "") if isinstance(kv[1], dict) else "")
            self.idempotency = dict(newest[-500:])
        self._write_private_json(self.idempotency_path, {"keys": self.idempotency})

    def _remember_idempotency(self, key: str, request_id: str, status: str = "pending") -> None:
        if not key:
            return
        self.idempotency[key] = {"request_id": request_id, "status": status,
                                 "created_at": now_iso()}
        self._save_idempotency()

    def _load_mandates(self) -> None:
        """Mandates are operator-approved standing authority: they must outlive a restart.

        Losing them silently turns a recurring payment off without telling anyone, and losing them
        *mid-flight* would leave an agent believing it holds authority it does not have.
        """
        self.mandates = {}
        try:
            raw = json.loads(self.mandates_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        self._load_seals("mandate", raw)
        for row in raw.get("mandates", []):
            try:
                self.mandates[str(row["mandate_id"])] = Mandate(
                    mandate_id=str(row["mandate_id"]), kind=str(row["kind"]),
                    asset=str(row["asset"]), payee=str(row["payee"]),
                    per_tx_cap=Decimal(str(row["per_tx_cap"])),
                    period_cap=Decimal(str(row["period_cap"])),
                    period_hours=int(row["period_hours"]),
                    absolute_cap=Decimal(str(row["absolute_cap"])),
                    expires_at=str(row["expires_at"]), enabled=bool(row.get("enabled", False)),
                    created_by=str(row.get("created_by", "operator")),
                    label=str(row.get("label", "")),
                    created_at=str(row.get("created_at", "")),
                    activated_at=str(row.get("activated_at", "")),
                    # Both are written by to_dict() and both used to be dropped here: after a restart
                    # a revoked autopay came back as merely "paused" (and could be resumed), and an
                    # unattended one silently started asking for the PIN again.
                    revoked_at=str(row.get("revoked_at", "") or ""),
                    unattended=bool(row.get("unattended", False)) and not row.get("revoked_at"))
            except (KeyError, ValueError, ArithmeticError):
                continue   # a malformed row must not take the rest of the file with it

    def _save_mandates(self) -> None:
        rows = [m.to_dict() for m in self.mandates.values()]
        self._write_private_json(self.mandates_path, self._sealed_file("mandate", "mandates", rows))

    def mandates_propose(self, params: Dict[str, Any]) -> Dict[str, Any]:
        # The agent may propose, never replace: a caller-supplied mandate_id used to overwrite an
        # active autopay's record (its caps and history) with an inert proposal - a delete that
        # `mandates_delete` guards with the passphrase.
        existing_id = str(params.get("mandate_id") or "")
        if existing_id and existing_id in self.mandates:
            raise WalletError("mandate_exists",
                              "that autopay already exists; propose a new one with a different id")
        params = {**params, "mandate_id": ""}
        # `payee` is documented as "an address, or a saved contact name", and `request_tx` resolves
        # names — this path did not, so "set up an autopay to adrian" failed with `bad_mandate:
        # not a valid monero-mainnet address` while the identical payment worked.
        try:
            kind = str(params.get("kind") or "").strip()
            asset = str(params.get("asset") or "").strip().upper()
            if not kind and asset:
                kind = self.DEFAULT_CHAIN.get(asset, "")
            payee = str(params.get("payee") or "").strip()
            if kind and payee:
                address, _contact = self.resolve_payee(kind, payee)
                params["payee"] = address
        except WalletError:
            raise
        except Exception:  # noqa: BLE001 - an unresolvable name falls through to the address parser
            pass
        try:
            mandate = Mandate.parse(params, self.settings)
        except ValueError as exc:
            raise WalletError("bad_mandate", str(exc)) from exc
        mandate.enabled = False                     # proposals are inert until an operator activates
        mandate.created_by = "agent"
        mandate.created_at = now_iso()
        self.mandates[mandate.mandate_id] = mandate
        self._save_mandates()
        if self.ledger is not None:
            self.ledger.append("mandate", "proposed", "agent", mandate.to_dict())
        return {"mandate": mandate.to_dict(), "needs": "operator activation"}

    def mandates_create(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """An operator creates an autopay directly (agent proposals stay inert until activated)."""
        self._require_unlocked()
        self._require_passphrase(params, "an autopay spends without asking you each time")
        try:
            mandate = Mandate.parse({**params, "unattended": False}, self.settings)
        except ValueError as exc:
            raise WalletError("bad_mandate", str(exc)) from exc
        if params.get("unattended"):
            # forcing it false keeps the waiver in exactly one place (`mandates_set_unattended`),
            # which is also the only place that asks for the PIN
            mandate.unattended = False
        mandate.enabled = bool(params.get("activate", True))
        if mandate.enabled and mandate.expired():
            raise WalletError("mandate_expired", "that expiry is already in the past")
        mandate.created_by = "operator"
        mandate.created_at = now_iso()
        if mandate.enabled:
            mandate.activated_at = mandate.created_at
        self.mandates[mandate.mandate_id] = mandate
        self._save_mandates()
        if self.ledger is not None:
            self.ledger.append("mandate", "created", "operator", mandate.to_dict())
        return {"mandate": mandate.to_dict()}

    def mandates_update(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Change the caps, interval, expiry or label. Payee/chain/asset stay fixed by design:
        pointing an existing autopay at a new payee is how a hijacked mandate keeps its history."""
        self._require_unlocked()
        self._require_passphrase(params, "changing an autopay changes what it may spend")
        mandate_id = str(params.get("mandate_id", ""))
        mandate = self.mandates.get(mandate_id)
        if mandate is None:
            raise WalletError("unknown_mandate", "mandate id is not recognised")
        merged = {**mandate.to_dict(),
                  **{k: v for k, v in params.items()
                     if k in ("per_tx_cap", "period_cap", "absolute_cap", "interval",
                              "period_hours", "expires_at", "label", "activate")}}
        merged.pop("status", None)
        merged["kind"], merged["asset"], merged["payee"] = mandate.kind, mandate.asset, mandate.payee
        try:
            candidate = Mandate.parse(merged, self.settings)
        except ValueError as exc:
            raise WalletError("bad_mandate", str(exc)) from exc
        candidate.mandate_id = mandate.mandate_id
        candidate.created_by = "operator"
        candidate.created_at = mandate.created_at or now_iso()
        candidate.activated_at = mandate.activated_at
        candidate.enabled = (bool(params["activate"]) if "activate" in params else mandate.enabled)
        candidate.revoked_at = mandate.revoked_at
        if candidate.enabled:
            self._refuse_revoked(mandate)
        if candidate.enabled and candidate.expired():
            raise WalletError("mandate_expired", "that expiry is already in the past")
        if candidate.enabled and not candidate.activated_at:
            candidate.activated_at = now_iso()
        self.mandates[mandate_id] = candidate
        self._save_mandates()
        if self.ledger is not None:
            self.ledger.append("mandate", "updated", "operator",
                               {"before": mandate.to_dict(), "after": candidate.to_dict()})
        return {"mandate": candidate.to_dict()}

    @staticmethod
    def _refuse_revoked(mandate: Mandate) -> None:
        """Revoked is terminal. It was a label only: `enabled = True` on a revoked autopay made it
        "active" again with `revoked_at` still set, so the one state the docs call final was not."""
        if mandate.revoked_at:
            raise WalletError("mandate_revoked",
                              "this autopay was revoked and cannot be switched back on; "
                              "create a new one for the same payee")

    def mandates_set_enabled(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Pause (reversible) or resume an autopay. Resuming needs the passphrase; pausing an
        autopay only removes authority, so it does not."""
        self._require_unlocked()
        mandate_id = str(params.get("mandate_id", ""))
        mandate = self.mandates.get(mandate_id)
        if mandate is None:
            raise WalletError("unknown_mandate", "mandate id is not recognised")
        enable = bool(params.get("enabled", params.get("activate", True)))
        if enable:
            self._refuse_revoked(mandate)
            self._require_passphrase(params, "resuming an autopay lets it spend again")
            if mandate.expired():
                raise WalletError("mandate_expired",
                                  "this autopay has expired — extend its expiry before resuming")
        mandate.enabled = enable
        self._save_mandates()
        if self.ledger is not None:
            self.ledger.append("mandate", "resumed" if enable else "paused", "operator",
                               {"mandate_id": mandate_id})
        return {"mandate": mandate.to_dict()}

    def mandates_set_unattended(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Operator: let ONE autopay spend without the PIN. The only way past the gate.

        Costs the passphrase (what a human decided) *and* the PIN (that it is the human who holds it
        right now), because this is the one setting that removes the check in front of every payment.
        Turning it back on is free: removing authority never needs authority.
        """
        self._require_unlocked()
        mandate_id = str(params.get("mandate_id", ""))
        mandate = self.mandates.get(mandate_id)
        if mandate is None:
            raise WalletError("unknown_mandate", "mandate id is not recognised")
        enable = bool(params.get("unattended", True))
        if enable:
            self._refuse_revoked(mandate)
            self._require_passphrase(params, "an unattended autopay spends without asking you each time")
            self._verify_pin(params)
            if mandate.expired():
                raise WalletError("mandate_expired", "this autopay has expired — extend it first")
            if not (mandate.enabled and mandate.activated_at):
                raise WalletError("mandate_inactive",
                                  "activate the autopay before letting it run unattended")
        before = mandate.unattended
        mandate.unattended = enable
        self._save_mandates()
        if self.ledger is not None:
            self.ledger.append("mandate", "unattended_on" if enable else "unattended_off", "operator",
                               {"mandate_id": mandate_id, "before": before, "after": enable,
                                "label": mandate.label, "payee": mandate.payee})
        return {"mandate": mandate.to_dict()}

    def mandates_delete(self, params: Dict[str, Any]) -> Dict[str, Any]:
        self._require_unlocked()
        self._require_passphrase(params, "deleting an autopay removes its record of what it was")
        mandate_id = str(params.get("mandate_id", ""))
        mandate = self.mandates.pop(mandate_id, None)
        if mandate is None:
            raise WalletError("unknown_mandate", "mandate id is not recognised")
        self._save_mandates()
        if self.ledger is not None:
            self.ledger.append("mandate", "deleted", "operator", mandate.to_dict())
        return {"deleted": mandate_id, "mandate": mandate.to_dict()}

    def mandates_activate(self, params: Dict[str, Any]) -> Dict[str, Any]:
        self._require_unlocked()
        # A mandate is standing authority to spend without asking again — the passphrase, not the
        # operator token, is what makes it the human's decision.
        self._require_passphrase(params, "activating a mandate authorises payments without approval")
        mandate_id = str(params.get("mandate_id", ""))
        mandate = self.mandates.get(mandate_id)
        if mandate is None:
            raise WalletError("unknown_mandate", "mandate id is not recognised")
        self._refuse_revoked(mandate)
        if mandate.expired():
            raise WalletError("mandate_expired", "mandate has expired")
        mandate.enabled = True
        mandate.created_by = "operator"
        mandate.activated_at = now_iso()
        self._save_mandates()
        if self.ledger is not None:
            self.ledger.append("mandate", "activated", "operator", mandate.to_dict())
        return {"mandate": mandate.to_dict()}

    def mandates_revoke(self, params: Dict[str, Any]) -> Dict[str, Any]:
        mandate_id = str(params.get("mandate_id", ""))
        mandate = self.mandates.get(mandate_id)
        if mandate is None:
            raise WalletError("unknown_mandate", "mandate id is not recognised")
        mandate.enabled = False
        mandate.revoked_at = now_iso()
        mandate.unattended = False          # revoking removes the PIN waiver too
        self._save_mandates()
        if self.ledger is not None:
            self.ledger.append("mandate", "revoked", "agent_or_operator",
                               {"mandate_id": mandate.mandate_id, "reason": str(params.get("reason", ""))[:200]})
        return {"mandate": mandate.to_dict()}

    def mandates_list(self) -> Dict[str, Any]:
        """Mandates plus what each one has actually spent, so the caps are not just numbers.

        The period is a rolling window ending now, exactly what the policy checks, so "left this
        period" is the same figure the next request will be measured against.
        """
        out = []
        for m in self.mandates.values():
            usage: Dict[str, Any] = {}
            if self.ledger is not None:
                now = datetime.now(timezone.utc)
                since = (now - timedelta(hours=m.period_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
                # the same floor and the same mandate scoping `_context` uses, or "left this period"
                # is not the number the next request is measured against
                floor = m.created_at or None
                if floor and floor > since:
                    since = floor
                try:
                    period = self.ledger.totals(m.asset, kind=m.kind, since=since, payee=m.payee,
                                                mandate_id=m.mandate_id)
                    lifetime = self.ledger.totals(m.asset, kind=m.kind, since=floor, payee=m.payee,
                                                  mandate_id=m.mandate_id)
                    usage = {"period_spent": str(period),
                             "period_left": str(max(Decimal("0"), m.period_cap - period)),
                             "lifetime_spent": str(lifetime),
                             "lifetime_left": str(max(Decimal("0"), m.absolute_cap - lifetime))}
                except Exception:  # noqa: BLE001 - usage is decoration, never break the list
                    usage = {}
            out.append({**m.to_dict(), "usage": usage})
        return {"mandates": out}

    # ---------- the signer itself ----------

    def _autostart_signer(self) -> None:
        """Bring the signer up on unlock if it is installed but not answering.

        Only when the binary already exists: installing is a deliberate, ~120 MB step the operator
        triggers, never something a page load does behind their back.
        """
        if self.adapters:
            return
        if not any(k.startswith(("monero", "sim-")) for k in self.settings.chains_enabled):
            return
        if not signer_mod.binary_path(self.home).exists():
            return
        # A stale signed setting (an older default port) would make every probe look at a port we
        # never spawn on. The plugin always spawns the signer on DEFAULT_PORT, so adopt it.
        expected = f"127.0.0.1:{signer_mod.DEFAULT_PORT}/"
        if expected not in self.settings.monero_rpc_url:
            self.settings.monero_rpc_url = f"http://{expected}json_rpc"
            try:
                config.save(self.config_path, self.settings, self.master or b"")
            except Exception:  # noqa: BLE001
                pass

        def bring_up() -> None:
            try:
                self.ensure_signer({})
            except Exception:  # noqa: BLE001 - a signer that will not start must not block unlocking
                return
            with self.mutex:
                self._build_adapters()
                self._provision_wallets()

        # Off the request path: a first run may download ~120 MB and a cold signer takes seconds.
        # The pane shows "signer starting" and the next refresh finds live balances.
        threading.Thread(target=bring_up, name="walletd-signer", daemon=True).start()

    def _configured_chains(self) -> List[str]:
        """The chain list a diagnostic should report, readable while locked.

        While unlocked the signed settings are authoritative. While locked the signature cannot be
        checked, so this is the *stored* list: the point is that a healthy configured wallet must not
        print "chains: []" just because the key is away. Nothing here is trusted for policy.
        """
        if self.is_unlocked():
            return list(self.settings.chains_enabled)
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
            stored = (raw.get("settings") or {}).get("chains_enabled") or []
            return [str(kind) for kind in stored if str(kind).strip()]
        except (OSError, json.JSONDecodeError, TypeError, AttributeError):
            return []

    def readiness(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Can this wallet actually send this payment right now? One GO/NO-GO, no funds touched.

        A live test is the one moment where every piece has to line up at once: keystore open, signer
        running, the monero wallet file readable, the node answering, the fee inside the cap, the gate
        on, the caps big enough, and the receipt chain intact. `doctor` answers "is anything broken";
        this answers "if I approve this now, will it work" - and it builds the very transaction it
        would send, without broadcasting it.
        """
        kind = str(params.get("kind") or "").strip()
        asset = str(params.get("asset") or "").upper().strip()
        if not kind:
            kind = next((k for k in self.settings.chains_enabled if k.startswith("monero")), "") or                    (self.settings.chains_enabled[0] if self.settings.chains_enabled else "")
        if not asset:
            asset = self.NATIVE_ASSET.get(kind, "XMR")
        raw_amount = str(params.get("amount") or "").strip()
        checks: List[Dict[str, Any]] = []

        def add(name: str, ok: bool, detail: str, remedy: str = "", skipped: bool = False) -> None:
            checks.append({"check": name, "ok": bool(ok), "detail": detail,
                           "remedy": "" if ok else remedy, "skipped": skipped})

        unlocked = self.is_unlocked()
        add("keystore unlocked", unlocked,
            "the signing key is in memory" if unlocked else "the wallet is locked",
            "hermes wallet unlock")
        add("human gate", bool(self.settings.human_gate),
            "a human must type the passphrase to approve" if self.settings.human_gate
            else "the passphrase prompt is OFF: holding the operator token is enough to send",
            "hermes wallet gate on")

        adapter = self.adapters.get(kind)
        add("chain enabled", kind in self.settings.chains_enabled, f"{kind} is configured",
            f"hermes wallet coins --enable {asset}", skipped=kind in self.settings.chains_enabled)
        add("signer adapter", adapter is not None,
            f"{kind} has a signer" if adapter is not None else f"no adapter for {kind}",
            "hermes wallet signer start" if kind.startswith("monero") else "unlock the wallet")

        if adapter is not None:
            try:
                chain = adapter.status() or {}
                height = chain.get("height")
                if kind.startswith("sim"):
                    # there is no node to reach: the simulator is local, so this check does not apply
                    add("node reachable", True, "the simulator needs no node", "", skipped=True)
                else:
                    add("node reachable", bool(height),
                        f"height {height}" if height else "no height reported",
                        "hermes wallet nodes  (then pick a healthy one in the settings)")
            except Exception as exc:                  # noqa: BLE001 - a dead node is a check, not a crash
                add("node reachable", False, f"{type(exc).__name__}: {str(exc)[:100]}", "hermes wallet nodes")

            try:
                balance = Decimal(str(adapter.balance()))
                add("balance readable", True, f"{balance:f} {asset}")
                if raw_amount:
                    try:
                        wanted = Decimal(raw_amount)
                        add("enough money", balance >= wanted, f"{balance:f} available, {wanted:f} wanted",
                            "send coins to a receive address (hermes wallet receive)")
                    except (InvalidOperation, ValueError):
                        add("enough money", False, f"{raw_amount!r} is not an amount", "pass --amount 0.001")
            except Exception as exc:                  # noqa: BLE001
                add("balance readable", False, f"{type(exc).__name__}: {str(exc)[:120]}",
                    "hermes wallet signer start, or check the wallet file password")

            if raw_amount:
                try:
                    amount = Decimal(raw_amount)
                    payee = str(params.get("payee") or "").strip() or adapter.address()
                    plan = adapter.plan_transfer(amount, payee)
                    cap = Decimal(str(self.settings.fee_cap.get(asset, "0") or "0"))
                    fee = Decimal(str(getattr(plan, "fee", "0") or "0"))
                    add("fee inside the cap", (cap <= 0 or fee <= cap),
                        f"network fee {fee} {asset} (cap {cap})",
                        "hermes wallet config set fee_cap... or wait for a quieter chain")
                    add("transaction builds", True, "the exact transaction planned, nothing sent")
                    per_tx = self.settings.cap(self.settings.per_tx_cap, asset)
                    add("inside the per-tx cap", (per_tx <= 0 or amount <= per_tx),
                        f"{amount} vs cap {per_tx} {asset}",
                        "approve it as a one-off, or raise the cap deliberately")
                except Exception as exc:              # noqa: BLE001
                    add("transaction builds", False, f"{type(exc).__name__}: {str(exc)[:120]}",
                        "read the message: an unfunded account, a rejected payee or a dead node")

        try:
            verified = self.ledger_verify() if self.ledger is not None else None
            add("receipt chain", bool(verified and verified.get("ok")),
                f"{verified.get('entries')} receipts verify" if verified else "not readable while locked",
                "hermes wallet verify")
        except Exception as exc:                      # noqa: BLE001
            add("receipt chain", False, f"{type(exc).__name__}: {str(exc)[:100]}", "hermes wallet verify")
        try:
            add("settings signed", not self.config_tampered, "the settings signature matches",
                "hermes wallet doctor  (a tampered config must be re-signed)")
        except Exception:                             # noqa: BLE001
            add("settings signed", True, "could not check (reported by doctor)", "", skipped=True)

        blocking = [c for c in checks if not c["ok"] and not c["skipped"]]
        return {"verdict": "go" if not blocking else "no_go", "kind": kind, "asset": asset,
                "amount": raw_amount, "checks": checks, "blocking": [c["check"] for c in blocking]}

    def _pin_binding_state(self) -> tuple[bool, str, str]:
        """(ok, note, status) about whether the PIN is bound to this machine. Returns a tuple rather
        than a finished row because doctor's rows are built by its own `check()` helper — hand-built
        dicts missed `note` and crashed the renderer with a KeyError."""
        try:
            envelope = keystore.load(self.keystore_path)
        except keystore.KeystoreError:
            return True, "no keystore", "skipped"
        if not keystore.has_pin(envelope):
            return True, "no PIN set", "skipped"
        if not keystore.pin_is_bound(envelope):
            return (False, "the PIN opens this file with six digits alone — set it again to bind it to "
                           "this machine", "")
        if self._device_key() is None:
            return (False, "the PIN is bound to another machine; unlock with the passphrase and set the "
                           "PIN again here", "")
        return True, "the PIN needs this machine's key as well as the six digits", ""

    def doctor(self) -> Dict[str, Any]:
        """One call that answers 'is this wallet healthy?' without touching funds.

        Being locked is a state, not a fault. Checks that genuinely need the master key (the settings
        signature) are reported as ``skipped`` with a note, and the receipt chain is verified through
        the public-key path that works locked - otherwise doctor printed three red FAILs for a wallet
        that was simply closed, and showed ``chains: [] / receipts: 0`` to anyone reading --json.
        """
        from . import config as config_mod

        locked = not self.is_unlocked()
        signer = signer_mod.status(self.home)
        loaded = config_mod.load(self.config_path, self.master)
        from .home import stray_wallets
        strays = stray_wallets(self.home)
        try:
            verify = self.ledger_verify()          # public key only: works while locked
        except WalletError as exc:
            verify = {"ok": False, "entries": 0, "message": exc.message,
                      "anchor": {"anchored": False, "ok": False, "reason": exc.message}}

        def check(name: str, ok: bool, note: str = "", status: str = "") -> Dict[str, Any]:
            row: Dict[str, Any] = {"check": name, "ok": bool(ok),
                                   "status": status or ("ok" if ok else "failed")}
            if note:
                row["note"] = note
            return row

        checks = [
            check("keystore", self.exists()),
            # a second wallet on this machine, in a directory this daemon does not serve: the classic
            # way a user funds an address they cannot spend from
            check("one wallet home", not strays,
                  ("other home(s) with a keystore: " + ", ".join(str(s) for s in strays[:3]))
                  if strays else ""),
        ]
        if locked:
            checks.append(check("unlocked", True,
                                "wallet is locked: that is a state, not a fault. Unlock to sign.",
                                status="skipped"))
            checks.append(check("settings signed", True,
                                "the config signature needs the master key; unverified while locked",
                                status="skipped"))
        else:
            checks.append(check("unlocked", True, f"unlocked, {self.unlock_expires_in()}s of autolock left"))
            checks.append(check("settings signed", bool(loaded.signed) and not loaded.tampered,
                                "" if loaded.signed and not loaded.tampered else loaded.reason))
        checks.extend([
            check("signer installed", bool(signer["installed"])),
            check("signer runs", bool(signer.get("runs")), signer.get("reason", "")),
            check("signer reachable",
                  bool(JsonRpcProbe(self.settings.monero_rpc_url,
                                    *((lambda c: (c["user"], c["password"]))(
                                        signer_mod.credentials(self.home, create=False)))).ok())),
            check("receipt chain", bool(verify.get("ok")),
                  str(verify.get("message") or "") if not verify.get("ok") else ""),
            check("receipt anchor", bool((verify.get("anchor") or {}).get("ok")),
                  str((verify.get("anchor") or {}).get("reason") or "")),
            check("contacts file", self.contacts_path.exists() or not self.contacts),
            check("sandboxes file", self.sandboxes_path.exists() or not self.sandboxes),
            # A row whose seal did not verify is refused for money; that is not a fault of the
            # wallet but it *is* something the operator must act on, so it gets its own line.
            check("authority seals",
                  not self.ledger_rewound and not any(
                      sb.tampered for sb in self.sandboxes.values()
                      if not sb.destroyed_at and not sb.revoked_at),
                  self.ledger_rewound or ", ".join(
                      sb.sandbox_id for sb in self.sandboxes.values()
                      if sb.tampered and not sb.destroyed_at and not sb.revoked_at)[:120]),
            # A PIN that is not bound to this machine is still a six-digit secret in a file; say so
            # rather than letting the operator believe the stronger thing.
            check("pin binding", *self._pin_binding_state()),
        ])

        # Gas is the one balance whose exhaustion stops every other payment on the chain, so it
        # gets a line of its own. Skipped while locked: the address needs the key.
        if locked:
            checks.append(check("gas floor", True, "unlocked", status="skipped"))
        else:
            gas = self.gas_report()
            low = gas["low"]
            checks.append(check(
                "gas floor", not low,
                "; ".join(f"{r['kind']} holds {r['held'][:9]} {r['native']}, floor "
                          f"{r['floor'][:9]} (short {r['shortfall'][:9]})" for r in low)[:150]))

        if locked:
            chains = [{"kind": kind, "available": None, "state": "locked", "configured": True}
                      for kind in self._configured_chains()]
        else:
            chains = self.chain_status()["chains"]

        # A FAIL that does not say what to do is a diagnostic that wastes the reader's time: `chains`,
        # `balances` and `ready` all print a remedy, so `doctor` doing less made it the least
        # actionable of the four.
        remedies = {
            "keystore": "hermes wallet init   (or restore a backup)",
            "one wallet home": "fund only the address this daemon serves: remove the stray keystore, "
                               "or point AGENT_WALLET_HOME at the home you want",
            "settings signed": "hermes wallet config show   (then re-save the setting that was altered)",
            "signer installed": "hermes wallet signer install",
            "signer runs": "hermes wallet signer repair",
            "signer reachable": "hermes wallet signer start",
            "receipt chain": "hermes wallet verify --json   (do not delete the ledger: report it)",
            "receipt anchor": "hermes wallet verify --json",
            "contacts file": "hermes wallet contacts list",
            "sandboxes file": "hermes wallet sandbox list",
            "pin binding": "hermes wallet pin set   (set the PIN again on this machine)",
            "authority seals": "hermes wallet sandbox revoke --sandbox-id <id>   (its record was "
                               "edited outside the wallet; revoking sweeps the leftover home). If it "
                               "reads 'ledger…', receipts were deleted: report it, do not delete the "
                               "ledger",
            "gas floor": "hermes wallet gas   (send the shortfall to the address it prints; set the "
                         "refill address once with: hermes wallet gas reserve <chain> <address>)",
        }
        for row in checks:
            if not row["ok"] and not row.get("skipped") and row["check"] in remedies:
                row["remedy"] = remedies[row["check"]]

        failed = [c["check"] for c in checks if not c["ok"]]
        return {
            "ok": not failed,
            "locked": locked,
            "checks": checks,
            "failed": failed,
            "skipped": [c["check"] for c in checks if c["status"] == "skipped"],
            "chains": chains,
            "contacts": len(self.contacts),
            "sandboxes": len(self.sandboxes),
            "pending_requests": sum(1 for r in self.requests.values() if r.status == "pending"),
            "wallet_file": self.settings.monero_wallet_file,
            "rpc_url": self.settings.monero_rpc_url,
            "receipts": int(verify.get("entries") or 0),
            "receipts_verified": bool(verify.get("ok")),
        }

    def ensure_signer(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Make the signer exist and answer — install and start it if needed.

        This is what "self-contained" means here: the plugin owns the binary (downloaded from the
        vendor, hash-verified, inside its own wallet home), owns the process, and talks to a public
        node. Nothing else has to be running on the machine.
        """
        creds = signer_mod.credentials(self.home)
        probe = JsonRpcProbe(self.settings.monero_rpc_url, creds["user"], creds["password"])
        if probe.ok():
            return {"reachable": True, "started": False, "rpc_url": self.settings.monero_rpc_url}
        binary = signer_mod.binary_path(self.home)
        installed = False
        if not binary.exists():
            try:
                signer_mod.install(self.home)
                installed = True
            except signer_mod.SignerError as exc:
                raise WalletError(exc.code, exc.message) from exc
        started = self.signer_start(params)
        return {"reachable": True, "installed": installed, **started}

    def _signer_answered(self) -> bool:
        """Did the signer actually answer? `status` used to report "ready" whenever a Monero adapter
        existed, which on a two-wallet host meant the shallow port probe found *another* wallet's
        monero-wallet-rpc while this wallet's own signer was down. The chain status is the honest
        answer and it is already memoised for 20 s."""
        now = time.time()
        cached = getattr(self, "_signer_probe_cache", None)
        if cached and now - cached[0] < 5.0:
            return cached[1]
        ok = False
        try:
            creds = signer_mod.credentials(self.home, create=False)
            probe = JsonRpcProbe(self.settings.monero_rpc_url, creds["user"], creds["password"])
            answer: list = []
            worker = threading.Thread(target=lambda: answer.append(probe.ok()), daemon=True)
            worker.start()
            worker.join(1.5)          # bounded: a wedged signer must not make `status` hang
            ok = bool(answer and answer[0])
        except Exception:  # noqa: BLE001 - status must always render
            ok = False
        self._signer_probe_cache = (now, ok)
        return ok

    def signer_status(self) -> Dict[str, Any]:
        status = signer_mod.status(self.home)
        status["owned"] = bool(self._signer_proc is not None and self._signer_proc.poll() is None)
        status["rpc_url"] = self.settings.monero_rpc_url
        try:
            creds = signer_mod.credentials(self.home, create=False)
            probe = JsonRpcProbe(self.settings.monero_rpc_url, creds["user"], creds["password"])
            status["reachable"] = probe.ok()
            status["height"] = probe.height()
        except Exception:  # noqa: BLE001
            status["reachable"] = False
        return status

    def signer_repair(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Replace a signer binary that exists but does not run, then start it.

        Wallet files, keys and receipts are untouched: only the executable is swapped.
        """
        self._require_unlocked()
        try:
            result = signer_mod.repair(self.home)
        except signer_mod.SignerError as exc:
            raise WalletError(exc.code, exc.message) from exc
        if self.ledger is not None:
            self.ledger.append("signer", "repaired", "operator",
                               {"version": result.get("version", ""), "replaced": result.get("replaced", False)})
        if result.get("runs"):
            try:
                self.ensure_signer({})
            except WalletError:
                pass                       # repaired but the node may be down; that is not a repair failure
            self._build_adapters()
        return result

    def signer_install(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Download the official monero-wallet-rpc, verify its sha256, extract just that binary."""
        self._require_unlocked()
        try:
            result = signer_mod.install(self.home)
        except signer_mod.SignerError as exc:
            raise WalletError(exc.code, exc.message) from exc
        if self.ledger is not None:
            self.ledger.append("signer", "installed", "operator",
                               {"version": result.get("version", ""), "path": result.get("path", "")})
        return result

    def signer_start(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Start the signer on loopback against a node from the registry, then point the wallet at it."""
        self._require_unlocked()
        if self._signer_proc is not None and self._signer_proc.poll() is None:
            return {"started": False, "already": True, "rpc_url": self.settings.monero_rpc_url}
        monero_kinds = [k for k in self.settings.chains_enabled if k.startswith("monero")]
        node_kind = monero_kinds[0] if monero_kinds else "monero-mainnet"
        node = node_registry.pick(node_kind) or {"url": "http://xmr-node.cakewallet.com:18081",
                                                "label": "cakewallet"}
        port = int(params.get("port", signer_mod.DEFAULT_PORT))
        if not self._port_free(port):
            # Two wallets on one machine (two profiles) both default to 38084; the second one has to
            # take the next free port or it dies with "Failed to bind IPv4" and looks like a broken
            # signer.
            port = self._free_port(signer_mod.DEFAULT_PORT)
        try:
            result = signer_mod.spawn(self.home, str(params.get("node") or node["url"]), port=port)
        except signer_mod.SignerError as exc:
            raise WalletError(exc.code, exc.message) from exc
        self._signer_proc = result.pop("_proc", None)
        self.settings.monero_rpc_url = result["url"]
        config.save(self.config_path, self.settings, self.master or b"")
        self._build_adapters()
        if self.ledger is not None:
            self.ledger.append("signer", "started", "operator",
                               {"port": port, "node": node.get("label", "")})
        return {**result, "node": node.get("label", ""), "provider": node.get("label", "")}

    def signer_stop(self, params: Dict[str, Any]) -> Dict[str, Any]:
        self._require_unlocked()
        proc = self._signer_proc
        if proc is None or proc.poll() is not None:
            return {"stopped": False, "note": "no signer owned by this process"}
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:  # noqa: BLE001
            proc.kill()
        self._signer_proc = None
        return {"stopped": True}

    # ---------- wallet provisioning ----------

    # Chains that shipped after the first release, with the schema version that introduced them.
    # Adding them to an existing install is a migration, not a default: defaults would never apply.
    _CHAIN_MIGRATIONS = [(2, "xrp-mainnet", "XRP")]
    # Everything the wallet *can* hold, and the chain each asset lives on. The pane's coin menu is
    # built from this, so adding a coin is a config write and nothing else.
    ASSET_CHAINS: Dict[str, List[str]] = {
        "XMR": ["monero-mainnet"],
        "BTC": ["bitcoin-mainnet"],
        "XRP": ["xrp-mainnet"],
        "ETH": ["base-mainnet"],
        "USDC": ["base-mainnet", "polygon-mainnet"],
        "POL": ["polygon-mainnet"],
    }
    DEFAULT_ASSETS = ("XMR", "BTC", "XRP")
    # The network a coin uses unless the caller names another one. Order in ASSET_CHAINS is the
    # preference order, so there is one table rather than two that can disagree.
    @property
    def DEFAULT_CHAIN(self) -> Dict[str, str]:
        return {asset: chains[0] for asset, chains in self.ASSET_CHAINS.items() if chains}
    # The coin a chain exists for. Naming a chain and omitting the asset used to fall back to
    # prefer_asset, so "pay this XRP address" quietly became an XMR payment and failed on a
    # mismatch the caller never asked for.
    NATIVE_ASSET: Dict[str, str] = {
        "monero-mainnet": "XMR", "monero-stagenet": "XMR", "monero-testnet": "XMR", "sim-mainnet": "XMR",
        "bitcoin-mainnet": "BTC", "bitcoin-testnet": "BTC",
        "xrp-mainnet": "XRP", "xrp-testnet": "XRP",
        "base-mainnet": "ETH", "base-sepolia": "ETH", "arbitrum-mainnet": "ETH",
        "polygon-mainnet": "POL",
    }

    def _expire_unapprovable_requests(self) -> int:
        """Pending requests whose approval token is gone can never be approved.

        Approval tokens live in memory only (they are key-adjacent), so after a daemon restart every
        pending request is unapprovable. Leaving them "pending" put dead cards in front of the human
        and made the agent think a payment was still in flight.
        """
        now = datetime.now(timezone.utc)
        live = {a.request_id for a in self.approvals.values() if not a.used and a.expires_at > now}
        expired = 0
        for req in list(self.requests.values()):
            if req.status != "pending" or req.request_id in live:
                continue
            req.status = "expired"
            expired += 1
            if self.ledger is not None:
                self.ledger.append("spend_request", "expired", "policy",
                                   {"request_id": req.request_id, "kind": req.kind,
                                    "asset": req.asset, "amount": str(req.amount),
                                    "reason": "the approval token did not survive; file the request again"})
        if expired:
            self._save_requests()
        # __init__ has no ledger (the key is not loaded yet), so the receipts for an expiry that
        # happens at load time are written on the next unlock instead of being lost.
        if expired and self.ledger is None:
            self._pending_expiry_receipts = getattr(self, "_pending_expiry_receipts", []) + [
                r for r in self.requests.values() if r.status == "expired" and r.request_id not in
                getattr(self, "_receipted_expiry", set())]
        return expired

    def _deny_requests_on(self, chains: List[str]) -> int:
        """Deny pending requests whose chain is gone: no approval could ever sign them."""
        if not chains:
            return 0
        denied = 0
        for req in list(self.requests.values()):
            if req.status == "pending" and req.kind in chains:
                req.status = "denied"
                denied += 1
                if self.ledger is not None:
                    self.ledger.append("spend_request", "denied", "settings",
                                       {"request_id": req.request_id, "kind": req.kind,
                                        "asset": req.asset, "amount": str(req.amount),
                                        "code": "chain_disabled",
                                        "reason": f"{req.kind} is not enabled any more"})
        if denied:
            self._save_requests()
        return denied

    def _migrate_settings(self) -> bool:
        """Bring an older settings document up to date, once, without undoing the user's choices."""
        changed = False
        current = int(getattr(self.settings, "schema", 1) or 1)
        for schema, chain, asset in self._CHAIN_MIGRATIONS:
            if current >= schema:
                continue
            if chain not in self.settings.chains_enabled:
                self.settings.chains_enabled = list(self.settings.chains_enabled) + [chain]
            if asset not in self.settings.assets_enabled:
                self.settings.assets_enabled = list(self.settings.assets_enabled) + [asset]
            self.settings.schema = schema
            changed = True

        # Every enabled coin needs caps, or it is denied by a zero cap that nobody set. Backfilled on
        # every unlock (idempotent), so a coin added later - by the pane or by a future release -
        # arrives with sane limits instead of looking broken.
        fresh = config.Settings()
        for table in ("per_tx_cap", "daily_cap", "fee_cap"):
            current_table = dict(getattr(self.settings, table) or {})
            default_table = getattr(fresh, table)
            added = False
            for asset in self.settings.assets_enabled:
                if asset not in current_table and asset in default_table:
                    current_table[asset] = default_table[asset]
                    added = True
            if added:
                setattr(self.settings, table, current_table)
                changed = True

        # schema 3: the wallet shrank to XMR/BTC/XRP on purpose - six coins made every balance row,
        # approval and sweep list noisy, and the rest are one toggle away. This runs once.
        if int(getattr(self.settings, "schema", 1) or 1) < 3:
            before = list(self.settings.chains_enabled)
            self.settings.assets_enabled = list(self.DEFAULT_ASSETS)
            self.settings.chains_enabled = [c for chain in self.DEFAULT_ASSETS
                                            for c in self.ASSET_CHAINS[chain]]
            self.settings.schema = 3
            changed = True
            self._deny_requests_on([c for c in before if c not in self.settings.chains_enabled])
        return changed

    def _ensure_chain_keys(self, payload: Dict[str, Any], data_key: bytes) -> Dict[str, Any]:
        """Add EVM/Bitcoin signing keys to an existing keystore, once, on unlock.

        Keystores created before those chains existed have no `keys` block. Generating them here and
        re-sealing under the data key we just obtained keeps one unlock fast, leaves the file with
        exactly the same protection as before, and works for a PIN unlock too — which is why this
        takes the data key rather than the passphrase.
        """
        keys = dict(payload.get("keys") or {})
        missing = [name for name in ("evm", "btc", "xrp") if not keys.get(name)]
        had_password = bool(payload.get("monero_wallet_password"))
        password = payload.get("monero_wallet_password") or ""
        if not password:
            # migrate an older wallet: reuse the password it already has, never replace it, or the
            # existing Monero wallet file could no longer be opened
            password = os.environ.get("AGENT_WALLET_MONERO_PASSWORD") or self._legacy_env_password()
            if not password:
                password = secrets.token_urlsafe(32)
        if not missing and had_password:
            return payload
        payload = {**payload, "keys": {**keys, **{n: secrets.token_hex(32) for n in missing}},
                   "monero_wallet_password": password}
        keystore.write_atomic(
            self.keystore_path,
            keystore.reseal(keystore.load(self.keystore_path), payload, data_key))
        if not had_password:
            self._strip_env_password()
        if self.ledger is not None:
            self.ledger.append("wallet", "secrets_updated", "operator",
                               {"keys_added": missing, "wallet_password_encrypted": not had_password})
        return payload

    def chain_secret(self, name: str) -> str:
        """A chain signing key. Callable only inside the daemon, never exposed over RPC."""
        value = (self.payload or {}).get("keys", {}).get(name)
        if not value:
            raise WalletError("locked", "unlock the wallet to use in-process signing")
        return value

    @staticmethod
    def _port_free(port: int) -> bool:
        import socket

        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False
        finally:
            probe.close()

    def _free_port(self, start: int) -> int:
        for candidate in range(start, start + 40):
            if self._port_free(candidate):
                return candidate
        import socket as _socket

        probe = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        try:
            probe.bind(("127.0.0.1", 0))
            return int(probe.getsockname()[1])
        finally:
            probe.close()

    def _provision_wallets(self) -> None:
        """Create the missing chain wallets as soon as the wallet is usable.

        An install that then shows no address is a dead end for the user, so the first unlock
        provisions each enabled Monero chain. BTC and EVM need nothing: their keys are already in
        the keystore and the address is derived from them. Failures are recorded, never raised —
        a signer that is still booting must not break the unlock.
        """
        if not getattr(self.settings, "auto_provision_wallets", True):
            return
        # Chains that sign in-process have their wallet in the keystore already: the address is a pure
        # function of the key, so "provisioning" is recording the address once, in the encrypted
        # payload, and saying so in the ledger. That way a user who installs the plugin and looks at
        # the wallet sees XMR, BTC and XRP addresses without doing anything.
        derived: Dict[str, str] = {}
        for kind in self.settings.chains_enabled:
            if not kind.startswith(("bitcoin", "xrp", "base", "polygon")):
                continue
            adapter = self.adapters.get(kind)
            if adapter is None:
                continue
            try:
                address = adapter.address()
            except Exception:  # noqa: BLE001 - provisioning is best-effort
                continue
            derived[kind] = address
        for kind, address in derived.items():
            # Only a receipt: the address is a pure function of the sealed key, so it never needs to
            # be written down, and not writing it means the keystore passphrase is never kept in
            # memory after unlock just to re-seal a derived string.
            if self.ledger is not None and not self._address_receipted(kind, address):
                self.ledger.append("wallet", "address_ready", "operator",
                                   {"kind": kind, "address": address})

        for kind in self.settings.chains_enabled:
            if not kind.startswith("monero"):
                continue
            # A fresh home has no adapter yet: "cannot open the wallet" is exactly the state we are
            # here to fix, so build one directly instead of waiting for the adapter map.
            try:
                adapter = self._fresh_monero_adapter(kind)
                if adapter.wallet_exists():
                    continue
                created = adapter.create_wallet()
                if self.ledger is not None:
                    self.ledger.append("wallet", "created", "operator",
                                       {"kind": kind, "address": created.get("address", "")[:120],
                                        "reason": "first unlock"})
                self._build_adapters()
            except Exception as exc:  # noqa: BLE001 - provisioning is best-effort
                if self.ledger is not None:
                    self.ledger.append("wallet", "provision_failed", "operator",
                                       {"kind": kind, "reason": f"{type(exc).__name__}: {str(exc)[:120]}"})

    def _address_receipted(self, kind: str, address: str) -> bool:
        """Whether this address was already announced, so every unlock does not add another receipt."""
        if self.ledger is None:
            return True
        for entry in self.ledger.entries():
            details = entry.get("details", {})
            if entry.get("decision") == "address_ready" and details.get("kind") == kind \
                    and details.get("address") == address:
                return True
        return False

    def _cached_addresses(self) -> Dict[str, str]:
        """Addresses are public: cache them so the page can show a funding address while locked.

        Held in memory after the first read: `/status` reports them on every poll, and a file read per
        poll is wasted work (and it showed up in the status benchmark).
        """
        cached = getattr(self, "_addresses_cache", None)
        if cached is not None:
            return cached
        try:
            rows = json.loads((self.home / "addresses.json").read_text(encoding="utf-8")).get("addresses", {})
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            rows = {}
        self._addresses_cache = rows
        return rows

    def _cache_addresses(self) -> None:
        self._addresses_cache = None          # re-read after a write
        rows = {}
        for kind, adapter in self.adapters.items():
            try:
                rows[kind] = adapter.address()
            except Exception:  # noqa: BLE001
                continue
        if not rows:
            return
        path = self.home / "addresses.json"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"addresses": rows}, handle, indent=2, sort_keys=True)

    def _signer_password(self) -> str:
        """The Monero wallet-file password, from the sealed payload.

        It used to live in `wallet.env` in plaintext; now it is part of the encrypted keystore, so at
        rest the only copy is inside the envelope and a locked wallet cannot produce it. The env
        fallbacks remain only to migrate an older wallet (see `_ensure_payload_secrets`).
        """
        value = (self.payload or {}).get("monero_wallet_password")
        if value:
            return str(value)
        return os.environ.get("AGENT_WALLET_MONERO_PASSWORD", "") or self._legacy_env_password()

    def _legacy_env_password(self) -> str:
        env_path = self.home / "wallet.env"
        try:
            for line in env_path.read_text(encoding="utf-8").splitlines():
                if line.startswith("AGENT_WALLET_MONERO_PASSWORD="):
                    return line.split("=", 1)[1].strip()
        except OSError:
            pass
        return ""

    def _strip_env_password(self) -> None:
        """Remove the plaintext copy now that the keystore holds it."""
        env_path = self.home / "wallet.env"
        try:
            lines = env_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        kept = [line for line in lines if not line.startswith("AGENT_WALLET_MONERO_PASSWORD=")]
        if len(kept) == len(lines):
            return
        fd = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(kept) + ("\n" if kept else ""))

    def _fresh_monero_adapter(self, kind: str):
        wallet = (os.environ.get("AGENT_WALLET_MONERO_WALLET")
                  or self.settings.monero_wallet_file or "agent-wallet")
        password = self._signer_password()
        creds = signer_mod.credentials(self.home)
        return XmrAdapter(kind, self.settings.monero_rpc_url, wallet, password,
                          provider=self.settings.monero_provider,
                          rpc_user=creds["user"], rpc_password=creds["password"],
                          home=self.home)

    def wallet_init(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Make sure a wallet exists for every enabled chain, creating one if needed.

        The keys are generated *inside* monero-wallet-rpc, which stores them in its own encrypted
        file; walletd only ever holds that wallet's password, which came from the keystore's env.
        Nothing here ever prints or returns key material — only the resulting address.
        """
        self._require_unlocked()
        kind = str(params.get("kind", "")) or "monero-mainnet"
        if not kind.startswith("monero"):
            raise WalletError("unsupported", f"{kind}: only Monero wallets can be created right now")
        provisioned = self.ensure_signer(params)
        adapter = self._fresh_monero_adapter(kind)
        try:
            adapter.close_wallet()          # one wallet per RPC: make the check deterministic
            if adapter.wallet_exists():
                address = adapter.address()
                self._build_adapters()      # the signer may have just come up: refresh the map
                self._cache_addresses()      # "a wallet exists here" is answered from this cache
                return {"kind": kind, "created": False, "address": address,
                        "signer": provisioned}
            created = adapter.create_wallet()
            address = adapter.address()
        except ChainError as exc:
            raise WalletError(exc.code, exc.message) from exc
        self._build_adapters()
        self._cache_addresses()
        if self.ledger is not None:
            self.ledger.append("wallet", "created", "operator",
                               {"kind": kind, "address": address})
        return {"kind": kind, "created": True, "address": address, **created,
                "signer": provisioned}

    def wallet_import(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Restore an existing wallet from its recovery words (never echoed back, never logged)."""
        self._require_unlocked()
        # Importing swaps the keys behind the address the pane shows, so the human who funds that
        # address has to be the one asking. Operator scope is not enough: the token is readable by
        # anything running as this uid, which includes an agent with a shell.
        self._require_passphrase(params, "importing a wallet")
        if not params.get("confirm"):
            raise WalletError("confirm_required",
                              "set confirm=true and pass the passphrase: importing replaces this wallet's keys")
        kind = str(params.get("kind", "")) or "monero-mainnet"
        if not kind.startswith("monero"):
            raise WalletError("unsupported", f"{kind}: only Monero wallets can be imported right now")
        seed = str(params.get("seed", ""))
        if not seed.strip():
            raise WalletError("bad_seed", "no recovery words supplied")
        try:
            live = self.adapters[kind].balance() if kind in self.adapters else Decimal("0")
        except Exception:  # noqa: BLE001 - an unreadable balance must block the import, not allow it
            raise WalletError("chain_unavailable",
                              "cannot read the current balance; refusing to replace a wallet that may hold funds")
        if live > 0:
            raise WalletError("wallet_not_empty",
                              f"this wallet still holds {live}; sweep it out before importing another")
        adapter = self._fresh_monero_adapter(kind)
        try:
            restored = adapter.restore_wallet(seed, int(params.get("restore_height", 0) or 0))
            address = adapter.address()
        except ChainError as exc:
            raise WalletError(exc.code, exc.message) from exc
        self._build_adapters()
        if self.ledger is not None:
            self.ledger.append("wallet", "imported", "operator",
                               {"kind": kind, "address": address,
                                "restore_height": int(params.get("restore_height", 0) or 0)})
        return {"kind": kind, "restored": True, "address": address, **restored}

    # ---------- contacts ----------

    def merchants(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Where to buy things with this wallet, and how to pay at each shop."""
        return merchant_book.search(str(params.get("query", "")), str(params.get("category", "")))

    def contacts_list(self) -> Dict[str, Any]:
        return {"contacts": sorted((c.to_dict() for c in self.contacts.values()),
                                   key=lambda c: c["name"].lower())}

    def contact_save(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Create or update a contact. The agent may do this — it is its own address book.

        Any address supplied is validated against its chain before it is stored, so a bad paste is
        rejected at save time instead of at payment time.
        """
        name = " ".join(str(params.get("name", "")).split())[:60]
        if not name:
            raise WalletError("bad_contact", "a contact needs a name")
        key = self._contact_key(name)
        addresses = params.get("addresses") or {}
        if not isinstance(addresses, dict) or not addresses:
            raise WalletError("bad_contact", "a contact needs at least one chain -> address")
        checked: Dict[str, str] = {}
        for kind, address in addresses.items():
            try:
                checked[str(kind)] = payee_key(str(kind), str(address))
            except ValueError as exc:
                raise WalletError("bad_contact", f"{kind}: {exc}") from exc
        existing = self.contacts.get(key)
        scope = str(params.get("_scope") or "")
        if scope == "operator" and (existing is None or existing.created_by != "operator"):
            # A new operator contact IS a destination blessing: `_payee_is_blessed` trusts it for
            # sandbox spending. Requiring the passphrase here is what stops a same-uid attacker (or a
            # prompt-injected agent holding the operator token) from minting one. The same goes for
            # *promoting* a contact the agent saved: re-saving it unchanged on the operator token
            # used to flip `created_by` to "operator" with no secret at all.
            self._require_passphrase(params, "saving a contact as the operator blesses that address")
        now = now_iso()
        contact = Contact(
            contact_id=existing.contact_id if existing else "con_" + secrets.token_hex(4),
            name=name, key=key,
            addresses={**(existing.addresses if existing else {}), **checked},
            notes=str(params.get("notes", existing.notes if existing else ""))[:200],
            created_at=existing.created_at if existing else now, updated_at=now,
            # Never demote a blessing: an agent adding a chain to a contact a human saved used to
            # reset `created_by` to "agent", silently turning off no-approval spending that relied
            # on that contact. Only a human whose entry this is can take the blessing away.
            created_by=("operator" if existing is not None and existing.created_by == "operator"
                        else str(scope or (existing.created_by if existing else "agent"))),
        )
        # Changing an address an existing contact already has is how a swapped payee steals a
        # payment. Adding a *new chain* to an existing contact is the same attack with one more step:
        # "send Aiden 5 USDC" resolves to an address the human has never seen for that chain. Both
        # need the passphrase; creating a contact, or adding a chain to a brand-new one, does not.
        changed = {kind: (existing.addresses.get(kind, ""), address)
                   for kind, address in checked.items()
                   if existing is not None and existing.addresses.get(kind, "") not in ("", address)}
        added_chains = {kind: address for kind, address in checked.items()
                        if existing is not None and kind not in existing.addresses}
        if added_chains and not changed:
            changed = {kind: ("", address) for kind, address in added_chains.items()}
        if changed:
            self._require_passphrase(params, "changing an address an existing contact already has")
        self.contacts[key] = contact
        self._save_contacts()
        if self.ledger is not None:
            self.ledger.append("contact", "saved", "operator" if changed else "agent_or_operator",
                               {"contact_id": contact.contact_id, "name": contact.name,
                                "chains": sorted(checked),
                                # old -> new, so the receipt can be compared with what was approved
                                "changed": {k: {"from": old, "to": new}
                                            for k, (old, new) in changed.items()}})
        return {"contact": contact.to_dict(), "created": existing is None}

    def contact_delete(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Deleting a contact lets the same name be re-added with a different address — which is the
        contact-substitution attack with one extra step, so it needs the passphrase too."""
        self._require_passphrase(params, "deleting a contact frees its name for a different address")
        key = self._contact_key(str(params.get("name", "")))
        contact = self.contacts.pop(key, None)
        if contact is None:
            raise WalletError("unknown_contact", "no contact with that name")
        self._save_contacts()
        if self.ledger is not None:
            self.ledger.append("contact", "deleted", "operator",
                               {"contact_id": contact.contact_id, "name": contact.name})
        return {"deleted": contact.to_dict()}

    def resolve_payee(self, kind: str, payee: str) -> tuple:
        """Accept either an address or a saved contact name. Returns (address, contact_name)."""
        candidate = str(payee or "").strip()
        if not candidate:
            raise WalletError("invalid_payee", "payee is required")
        # An address is longer and never contains spaces; a name is the other way round.
        if " " in candidate or len(candidate) < 40:
            contact = self.contacts.get(self._contact_key(candidate))
            if contact is not None:
                address = contact.addresses.get(kind)
                if not address:
                    raise WalletError("contact_chain_missing",
                                      f"{contact.name} has no {kind} address saved")
                return address, contact.name
            # Not a saved name: let it fall through to policy so a malformed payee is denied
            # *with a receipt* instead of raising out of the request path.
        return candidate, ""

    # ---------- sandboxes (capped sub-wallets) ----------

    def profiles_list(self) -> Dict[str, Any]:
        """Hermes profiles that exist next to this wallet, for the sandbox access list.

        This lives in the daemon rather than only in the dashboard route because the dashboard
        imports a plugin's routes once at startup: a route added later 404s until the backend is
        restarted, and then the pane silently gets an empty list and shows nothing to tick. The
        daemon is restarted with the wallet, so it can always answer.
        """
        names = {"default"}
        from . import home as home_mod

        home = Path(self.home)
        default = home_mod._default_hermes_home()       # ~/.hermes, or %LOCALAPPDATA%\hermes
        bases = [home.parent / "profiles"] + ([default / "profiles"] if default is not None else [])
        for base in bases:
            try:
                if base.is_dir():
                    names.update(child.name for child in base.iterdir()
                                 if child.is_dir() and not child.name.startswith("."))
            except OSError:
                continue
        current = os.environ.get("HERMES_PROFILE") or "default"
        names.add(current)
        return {"profiles": sorted(names), "current": current}

    # ---------- gas ----------

    def _gas_floor(self, native: str, cost: Decimal) -> Decimal:
        """The native balance a chain must keep: an explicit override, else N transfers' worth."""
        explicit = (self.settings.gas_floor or {}).get(native)
        if explicit not in (None, ""):
            try:
                return max(Decimal("0"), Decimal(str(explicit)))
            except (InvalidOperation, ValueError):
                pass
        transfers = max(0, int(self.settings.gas_floor_transfers or 0))
        return cost * transfers

    def _gas_view(self, kind: str) -> Optional[Dict[str, Any]]:
        """Native balance, floor and headroom for one chain.

        None for a chain that does not pay gas out of a separate coin: Monero and Bitcoin charge
        the fee in the coin being sent (already inside the amount check), and a 402 purchase is
        submitted — and paid for — by the seller.
        """
        adapter = self.adapters.get(kind)
        symbol_fn = getattr(adapter, "native_symbol", None)
        cost_fn = getattr(adapter, "transfer_cost", None)
        held_fn = getattr(adapter, "native_balance", None)
        if not callable(symbol_fn) or not callable(cost_fn) or not callable(held_fn):
            return None
        try:
            native = str(symbol_fn())
            cost = Decimal(str(cost_fn()))
            held = Decimal(str(held_fn()))
        except Exception as exc:      # noqa: BLE001 - a gas hint must never break a payment
            return {"kind": kind, "error": f"{type(exc).__name__}: {exc}"}
        floor = self._gas_floor(native, cost)
        reserve = str((self.settings.gas_reserve or {}).get(native, "") or "")
        # A chain that holds nothing at all is not "low", it is unused: a permanent red line for a
        # chain nobody funds is how a real warning gets ignored. A chain that holds a token but no
        # gas is the case this exists for, and it is reported.
        else_held = Decimal("0")
        balance_all = getattr(adapter, "balance_all", None)
        if callable(balance_all):
            try:
                mapping = balance_all() or {}
                if isinstance(mapping, dict):
                    else_held = sum((Decimal(str(v or 0)) for k, v in mapping.items()
                                     if str(k).upper() != native.upper()), Decimal("0"))
            except Exception:      # noqa: BLE001 - the gas hint is not worth failing a payment for
                else_held = Decimal("0")
        if held <= 0 and else_held <= 0:
            state = "unused"
        else:
            state = "ok" if held >= floor else "low"
        return {"kind": kind, "native": native, "held": str(held), "cost": str(cost),
                "floor": str(floor), "headroom": str(max(Decimal("0"), held - floor)),
                "address": str(getattr(adapter, "address", lambda: "")() or ""),
                "transfers_left": int(held // cost) if cost > 0 else 0,
                "transfers_floor": int(self.settings.gas_floor_transfers or 0),
                "reserve": reserve,
                "state": state,
                "shortfall": str(max(Decimal("0"), floor - held))}

    def _check_gas_floor(self, req: SpendRequest, plan: Any) -> None:
        """Refuse a transfer that would leave the chain unable to pay for the next one.

        The floor is the difference between "the agent spent some money" and "the agent bricked
        the wallet on this chain until a human notices". Enforced at planning *and* at broadcast,
        because the fee is re-read between the human's approval and the send.
        """
        if plan is None or getattr(plan, "raw", {}).get("x402"):
            return
        view = self._gas_view(req.kind)
        if not view or view.get("error"):
            return
        native = view["native"]
        held = Decimal(view["held"])
        cost = Decimal(view["cost"])
        floor = self._gas_floor(native, cost)
        spend = Decimal(str(getattr(plan, "fee", 0) or 0))
        if str(req.asset).upper() == native:
            spend += Decimal(str(getattr(plan, "amount", 0) or 0))
        after = held - spend
        if after < floor:
            keep = int(floor // cost) if cost > 0 else 0
            hint = f" — top up {view['reserve']}" if view.get("reserve") else ""
            raise ChainError("gas_floor",
                             f"this would leave {after:.9f} {native}, below the {floor:.9f} {native} "
                             f"floor (about {keep} more transfer(s)); nothing was sent{hint}")

    def gas_report(self) -> Dict[str, Any]:
        """What every gas-paying chain holds, what it must keep, and how a human refills it."""
        if not self.is_unlocked():
            # The address comes from the keystore, so there is nothing honest to report while locked.
            return {"chains": [], "low": [], "locked": True,
                    "floor_transfers": int(self.settings.gas_floor_transfers or 0)}
        rows = []
        for kind in list(self.settings.chains_enabled):
            view = self._gas_view(kind)
            if view is not None:
                rows.append(view)
        return {"chains": rows,
                "low": [r for r in rows if r.get("state") == "low"],
                "floor_transfers": int(self.settings.gas_floor_transfers or 0)}

    # ---------- swaps ----------

    def swap_quote(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """What a conversion would return, and the minimum the wallet would accept for it.

        Read-only: nothing is signed, nothing is filed. The agent may ask (that is how it notices a
        payment it cannot make), and the answer contains no secret — a router address, a rate and
        the slippage ceiling the operator set.
        """
        self._require_unlocked()
        kind, asset_in, asset_out, amount = self._swap_params(params)
        adapter = self.adapters.get(kind)
        if adapter is None or not hasattr(adapter, "quote_swap"):
            raise WalletError("no_swap_route", f"{kind} cannot swap")
        operator = str(params.get("operator") or "")
        quote = adapter.quote_swap(asset_in, asset_out, amount, operator=operator)
        bps = self._swap_slippage_bps()
        quote["slippage_bps"] = bps
        quote["min_out"] = str(self._swap_min_out(Decimal(quote["amount_out"]), bps))
        quote["operators"] = [r["operator"] for r in adapter.swap_routers()]
        return quote

    def _swap_slippage_bps(self) -> int:
        try:
            bps = int(self.settings.swap_slippage_bps)
        except (TypeError, ValueError):
            return 50
        return max(0, min(bps, 500))        # a hard ceiling: 5% is already a lot to give away

    @staticmethod
    def _swap_min_out(quoted: Decimal, bps: int) -> Decimal:
        keep = Decimal(1) - (Decimal(int(bps)) / Decimal(10_000))
        return (Decimal(quoted) * keep).quantize(Decimal("0.000000000001"), rounding=ROUND_DOWN)

    def _swap_params(self, params: Dict[str, Any]) -> tuple:
        kind = str(params.get("kind") or "")
        asset_in = str(params.get("asset") or params.get("from") or "").upper()
        asset_out = str(params.get("asset_out") or params.get("to") or "").upper()
        if not asset_in or not asset_out:
            raise WalletError("bad_swap", "a swap needs a coin to sell and a coin to buy")
        if not kind:
            kind = self.DEFAULT_CHAIN.get(asset_in, "")
        if not kind:
            raise WalletError("bad_chain", f"{asset_in} is not a coin this wallet knows")
        if asset_in == asset_out:
            raise WalletError("bad_swap", f"swapping {asset_in} for itself is not a swap")
        for asset in (asset_in, asset_out):
            if asset not in self._chain_assets(kind):
                raise WalletError("asset_chain_mismatch",
                                  f"{asset} is not a coin on {kind} (it holds "
                                  f"{', '.join(self._chain_assets(kind))})")
        try:
            amount = Decimal(str(params.get("amount")))
        except (InvalidOperation, ValueError) as exc:
            raise WalletError("bad_amount", f"{params.get('amount')!r} is not an amount") from exc
        if not amount.is_finite() or amount <= 0:
            raise WalletError("bad_amount", "a swap needs a positive amount")
        return kind, asset_in, asset_out, amount

    def swap_routers(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """The trusted operators, per gas-paying chain. Code, not configuration."""
        kind = str(params.get("kind") or "")
        rows: List[Dict[str, Any]] = []
        from .chains.evm import SWAP_ROUTERS
        allowed = [str(name) for name in (self.settings.swap_routers or [])]
        if kind:
            chains = [kind]
        elif self.is_unlocked():
            chains = [c for c in self.settings.chains_enabled if c in SWAP_ROUTERS]
        else:
            # while locked the settings in memory are defaults, not this wallet's: answer from the
            # registry, which is code and cannot be wrong
            chains = sorted(SWAP_ROUTERS)
        for chain in chains:
            # read from the registry, not from a built adapter: this must answer while locked, and it
            # must answer the same thing whether or not anything is unlocked
            for name, entry in sorted(SWAP_ROUTERS.get(chain, {}).items()):
                if allowed and name not in allowed:
                    continue
                rows.append({"kind": chain, "operator": name, "label": entry["label"],
                             "router": entry["router"], "fee_bps": entry["fee_bps"]})
        return {"operators": rows, "swaps_enabled": bool(self.settings.swaps_enabled),
                "suggest_swaps": bool(self.settings.suggest_swaps),
                "slippage_bps": self._swap_slippage_bps()}

    def swap_request(self, params: Dict[str, Any], from_agent: bool = False) -> Dict[str, Any]:
        """File a conversion for a human to approve. The agent may ask; only a PIN converts."""
        self._require_unlocked()
        if not self.settings.swaps_enabled:
            raise WalletError("swaps_disabled",
                              "swapping is switched off in this wallet's settings")
        if from_agent and not self.settings.suggest_swaps:
            raise WalletError("swaps_not_suggested",
                              "this wallet does not take swap suggestions: the operator turned "
                              "that off, so convert it yourself and file a payment")
        kind, asset_in, asset_out, amount = self._swap_params(params)
        adapter = self.adapters.get(kind)
        if adapter is None or not hasattr(adapter, "quote_swap"):
            raise WalletError("no_swap_route", f"{kind} cannot swap")
        operators = [row["operator"] for row in adapter.swap_routers()]
        operator = str(params.get("operator") or "")
        if operator and operator not in operators:
            raise WalletError("unknown_operator",
                              f"{operator!r} is not a trusted swap operator "
                              f"({', '.join(operators) or 'none on this chain'})")
        if not operators:
            raise WalletError("no_swap_route", f"{kind} has no trusted swap operator")
        allowed = [str(name) for name in (self.settings.swap_routers or [])]
        if allowed:
            operators = [name for name in operators if name in allowed]
            if not operators:
                raise WalletError("no_swap_route",
                                  "every trusted operator on this chain is switched off by "
                                  "settings.swap_routers")

        quote = adapter.quote_swap(asset_in, asset_out, amount, operator=operator or operators[0])
        bps = self._swap_slippage_bps()
        min_out = self._swap_min_out(Decimal(quote["amount_out"]), bps)

        # Caps are the operator's, and they are about what leaves the wallet: the amount sold.
        per_tx = self.settings.cap(self.settings.per_tx_cap, asset_in)
        if per_tx > 0 and amount > per_tx:
            raise WalletError("over_per_tx_cap",
                              f"{amount} {asset_in} is above the {per_tx} {asset_in} per-payment cap")
        plan = adapter.plan_swap(asset_in, amount, asset_out, min_out,
                                 operator=quote["operator"])
        self._check_gas_floor_for_swap(kind, plan)

        req = SpendRequest(request_id="req_" + secrets.token_hex(6), kind=kind, asset=asset_in,
                           amount=amount, payee=plan.payee, memo=str(params.get("memo") or ""),
                           why=str(params.get("why") or ""),
                           profile=str(params.get("profile") or ""),
                           fee_estimate=str(plan.fee),
                           swap={"asset_out": asset_out, "min_out": str(min_out),
                                 "operator": quote["operator"], "label": quote["label"],
                                 "router": quote["router"], "quoted_out": quote["amount_out"],
                                 "slippage_bps": bps, "path": quote["path"]})
        req.gas = self._swap_gas_block(kind, plan)
        self.plans[req.request_id] = plan
        self.requests[req.request_id] = req
        self._save_requests()
        if self.ledger is not None:
            self.ledger.append("swap_request", "pending", "manual_approval",
                               {"request_id": req.request_id, "kind": kind,
                                "asset": req.asset, "amount": str(amount), **req.swap,
                                "fee": str(plan.fee)})
        approval = self._issue_approval(req)
        return {"request": req.to_dict(), "quote": quote, "min_out": str(min_out),
                "fee": str(plan.fee), "approval_id": approval.approval_id,
                "approval_expires_at": approval.expires_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "decision": {"allow": True, "rule": "manual_approval", "code": "needs_operator",
                             "reason": "a conversion moves money too: it needs the PIN"},
                "needs": "operator approval"}

    def _swap_gas_block(self, kind: str, plan: Any) -> Dict[str, str]:
        view = self._gas_view(kind)
        if not view or view.get("error"):
            return {}
        native = view["native"]
        after = Decimal(view["held"]) - Decimal(str(getattr(plan, "fee", 0) or 0))
        if native == str(getattr(plan, "asset", "")):
            after -= Decimal(str(getattr(plan, "amount", 0) or 0))
        return {"native": native, "floor": view["floor"], "after": str(after)}

    def _check_gas_floor_for_swap(self, kind: str, plan: Any) -> None:
        """A swap pays gas like anything else, and it may spend the gas coin itself."""
        view = self._gas_view(kind)
        if not view or view.get("error"):
            return
        native = view["native"]
        held = Decimal(view["held"])
        cost = Decimal(view["cost"])
        floor = self._gas_floor(native, cost)
        spend = Decimal(str(getattr(plan, "fee", 0) or 0))
        if native == str(getattr(plan, "asset", "")):
            spend += Decimal(str(getattr(plan, "amount", 0) or 0))
        if held - spend < floor:
            raise ChainError("gas_floor",
                             f"this swap would leave {held - spend:.9f} {native}, below the "
                             f"{floor:.9f} {native} floor; nothing was filed")

    def _fee_reserve(self, kind: str, asset: str) -> Decimal:
        """What a transfer on this chain actually costs, not the operator's fee *ceiling*.

        `fee_cap` is the most a fee is allowed to be. Reserving it tied up that amount in every
        sandbox forever, and `_destroy_sandbox` destroyed any balance at or below it — so a swept
        sandbox lost up to the cap with no record. Adapters that know their fee publish it; the cap
        stays as a conservative fallback.
        """
        adapter = self.adapters.get(kind)
        for name in ("fee_estimate", "fee"):
            value = getattr(adapter, name, None) if adapter is not None else None
            if callable(value):
                try:
                    value = value(asset)
                except TypeError:
                    try:
                        value = value()
                    except Exception:      # noqa: BLE001 - a hint must never break a sweep
                        value = None
                except Exception:          # noqa: BLE001
                    value = None
            if value is not None:
                try:
                    amount = Decimal(str(value))
                except (InvalidOperation, ValueError):
                    amount = Decimal("0")
                if amount > 0:
                    return amount
        return Decimal(str(self.settings.fee_cap.get(asset, "0") or "0"))

    def _sandbox_live_balance(self, sandbox: Sandbox, ttl_seconds: float = 10.0,
                              chain_errors: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """What the sandbox sub-account actually holds on chain, best-effort.

        ``remaining`` is bookkeeping the agent's own spends decrement; it says nothing about money
        that was never funded, or funding that arrived late. This is the number a human checks, and
        it must never turn "the node is down" into a confident zero: the state says which it is.
        """
        cached = self._sandbox_balances_cache.get(sandbox.sandbox_id)
        now = time.monotonic()
        if cached is not None and now - cached[0] < ttl_seconds:
            return dict(cached[1])
        if not self.is_unlocked():
            row = {"live_balance": None, "live_balance_state": "locked",
                   "live_balance_error": "wallet is locked; unlock to read the chain"}
        elif sandbox.kind not in self.adapters:
            row = {"live_balance": None, "live_balance_state": "unavailable",
                   "live_balance_error": f"chain {sandbox.kind} is not available"}
        elif chain_errors and sandbox.kind in chain_errors:
            # one dead node must not cost a timeout per sandbox row: the first failure is reused
            row = {"live_balance": None, "live_balance_state": "unavailable",
                   "live_balance_error": chain_errors[sandbox.kind]}
        else:
            reader = getattr(self.adapters[sandbox.kind], "balance_at", None)
            if reader is None:
                row = {"live_balance": None, "live_balance_state": "unsupported",
                       "live_balance_error": f"{sandbox.kind} cannot report a sub-account balance"}
            else:
                try:
                    row = {"live_balance": f"{Decimal(str(reader(sandbox.account_index))):f}"
                                           if sandbox.account_index >= 0 else None,
                           "live_balance_state": "live", "live_balance_error": ""}
                except Exception as exc:          # noqa: BLE001 - a dead node is not a balance
                    row = {"live_balance": None, "live_balance_state": "unavailable",
                           "live_balance_error": f"{type(exc).__name__}: {str(exc)[:120]}"}
        self._sandbox_balances_cache[sandbox.sandbox_id] = (now, dict(row))
        return row

    def sandboxes_list(self) -> Dict[str, Any]:
        # ``profiles`` rides along so the pane can build the ACL chips from a route that has existed
        # since the first release, instead of depending on a newly added endpoint.
        rows = []
        chain_errors: Dict[str, str] = {}
        for sandbox in self.sandboxes.values():
            row = sandbox.to_dict()
            # after a lock the chain read is impossible, not zero: report it as unknown
            live = self._sandbox_live_balance(sandbox, chain_errors=chain_errors)
            if live.get("live_balance_state") == "unavailable" and sandbox.kind not in chain_errors:
                chain_errors[sandbox.kind] = live.get("live_balance_error", "")
            row.update(live)
            rows.append(row)
        return {"sandboxes": rows, **self.profiles_list()}

    def _sandbox_create_pooled(self, params: Dict[str, Any], kind: str, asset: str,
                               budget: Decimal, hours: int, label: str, mode: str,
                               destruct: int, proposed: bool) -> Dict[str, Any]:
        """A budget with no separate address: the money stays where it is, the daemon enforces the cap.

        Used for chains where funding a sandbox means an on-chain transfer of its own (Bitcoin: a
        confirmation window and a fee that can dwarf a small budget). The containment is identical —
        the agent may spend up to `budget`, only to blessed destinations, until `expires_at` — and the
        human keeps the PIN on every payment. Nothing is swept, because nothing left.
        """
        sandbox_id = "sbx_" + secrets.token_hex(5)
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=hours)) \
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        payees = [] if proposed else self._checked_payees(kind, params.get("allowed_payees"))
        # `--one-time` used to be dropped here: the mode was overwritten with "pooled", so a budget
        # meant for a single purchase on Bitcoin took as many payments as fitted in it.
        stored_mode = "one_time" if mode == "one_time" else "pooled"
        sandbox = Sandbox(sandbox_id=sandbox_id, kind=kind, asset=asset, account_index=-1,
                          address="", budget=budget, spent=Decimal("0"), created_at=now_iso(),
                          expires_at=expires_at, label=label, mode=stored_mode,
                          self_destruct_seconds=destruct, allowed_payees=payees,
                          # a proposal cannot name who may spend it, any more than whom it may pay
                          allowed_profiles=[] if proposed else
                          [str(x).strip() for x in (params.get("allowed_profiles") or [])
                           if str(x).strip()],
                          # an operator's budget is armed at once: there is no funding transfer to
                          # wait for. An agent's proposal stays off until `sandbox_arm`.
                          enabled=not proposed, revoked_at="", destroyed_at="")
        self.sandboxes[sandbox_id] = sandbox
        self._save_sandboxes()
        self._record("sandbox", "created" if not proposed else "proposed",
                     "operator" if not proposed else "agent",
                     {"sandbox_id": sandbox_id, "kind": kind, "asset": asset,
                      "budget": format(budget, "f"), "mode": stored_mode, "pooled": True,
                      "label": label, "allowed_payees": payees,
                      "note": "policy budget: no funds moved, the main account pays"})
        result: Dict[str, Any] = {
            "sandbox": sandbox.to_dict(),
            "funding": {"status": "not_needed", "reason": "a pooled budget moves no funds"},
            "note": "this chain cannot hold a separate sandbox account, so the budget is enforced "
                    "against the main account — same caps and allowlist, every payment still asks "
                    "for the PIN"}
        if proposed:
            result["needs"] = ("operator arms it: `hermes wallet sandbox arm --sandbox-id "
                               f"{sandbox_id}` (or the Arm button in the Wallet pane)")
            if params.get("allowed_payees") or params.get("allowed_profiles"):
                result["ignored"] = ["allowed_payees", "allowed_profiles"]
                result["ignored_reason"] = ("a proposal may not name its own destinations; the "
                                            "operator sets them, with the passphrase")
        return result

    def _checked_payees(self, kind: str, raw: Any) -> List[str]:
        """Normalise a list of destination addresses for one chain, or refuse by name."""
        checked: List[str] = []
        for item in (raw or []):
            text = str(item).strip()
            if not text:
                continue
            try:
                key = payee_key(kind, text)
            except ValueError as exc:
                raise WalletError("bad_payee", f"{text[:24]}…: {exc}") from exc
            if key not in checked:
                checked.append(key)
        return checked

    def sandbox_arm(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Operator: switch on a budget the agent asked for on a chain where nothing is funded.

        On Monero a proposal is armed by approving its funding transfer. A pooled budget has no
        transfer to approve, so a proposal there had no way to become usable at all — it sat
        "unfunded" for ever. Arming is the same decision (the agent may now ask to spend up to this
        much), so it costs the same secret a mandate does: the passphrase.
        """
        self._require_unlocked()
        sandbox_id = str(params.get("sandbox_id", ""))
        sandbox = self.sandboxes.get(sandbox_id)
        if sandbox is None:
            raise WalletError("unknown_sandbox", "sandbox id is not recognised")
        if sandbox.account_index >= 0:
            raise WalletError("not_pooled",
                              "this sandbox holds a real account: it is armed by approving its "
                              "funding request (`hermes wallet requests`)")
        if sandbox.revoked_at or sandbox.destroyed_at:
            raise WalletError("sandbox_closed", "that sandbox is closed; create a new one")
        if sandbox.expired():
            raise WalletError("sandbox_expired",
                              "that sandbox has expired; create a new one instead of arming it")
        if sandbox.enabled:
            return {"sandbox": sandbox.to_dict(), "armed": False, "already": True}
        self._require_passphrase(params, "arming a budget lets the agent ask to spend from it")
        sandbox.enabled = True
        self._save_sandboxes()
        self._record("sandbox", "armed", "operator",
                     {"sandbox_id": sandbox_id, "kind": sandbox.kind, "asset": sandbox.asset,
                      "budget": format(sandbox.budget, "f"), "mode": sandbox.mode})
        return {"sandbox": sandbox.to_dict(), "armed": True}

    def sandbox_set_unattended(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Hand a budget to the agent, or take it back.

        On: the agent may pay *any* destination from this budget without a per-payment PIN, until the
        money runs out or the budget expires. That is the strongest thing an operator can grant short
        of unlocking the wallet, so it costs both secrets (passphrase + PIN) and leaves a receipt.
        Off is free: turning a gate back on never needs permission.
        """
        self._require_unlocked()
        sandbox_id = str(params.get("sandbox_id", ""))
        sandbox = self.sandboxes.get(sandbox_id)
        if sandbox is None:
            raise WalletError("unknown_sandbox", "sandbox id is not recognised")
        on = bool(params.get("on", True))
        if sandbox.revoked_at or sandbox.destroyed_at:
            raise WalletError("sandbox_closed", "that sandbox is closed; create a new one")
        if sandbox.tampered:
            raise WalletError("sandbox_tampered",
                              "this budget's record was edited outside the wallet, so it cannot be "
                              "armed; revoke it and make a new one")
        if on:
            if sandbox.expired():
                raise WalletError("sandbox_expired",
                                  "that budget has expired; create a new one instead of arming it")
            self._require_passphrase(params,
                                     "an unattended budget lets the agent pay any address from it")
            self._verify_pin(params)
        if not on and not sandbox.unattended:
            return {"sandbox": sandbox.to_dict(), "unattended": False, "already": True}
        was_unattended = sandbox.unattended
        sandbox.unattended = on
        sandbox.armed_at = now_iso() if on else ""
        self._save_sandboxes()
        self._record("sandbox", "unattended_on" if on else "unattended_off", "operator",
                     {"sandbox_id": sandbox_id, "kind": sandbox.kind, "asset": sandbox.asset,
                      "budget": format(sandbox.budget, "f"), "remaining": format(sandbox.remaining(), "f"),
                      "was_unattended": was_unattended})
        # A pooled budget is not custody: the coins are still in the main account, so the budget
        # number is the only thing holding the agent in. Say so on the way out, in every surface.
        warning = None
        if on and sandbox.mode == "pooled":
            warning = ("this budget is pooled — the money never left your main account, so the budget "
                       "number is the only containment, not a separate account")
        return {"sandbox": sandbox.to_dict(), "unattended": on,
                "custody": "pooled" if sandbox.mode == "pooled" else "account", "warning": warning}

    def sandbox_topup(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """The agent asking for more money: file a funding transfer, the human decides.

        Deliberately the only way an agent can grow a budget: it creates a *request*, never a
        transfer, and the request is a payment to this sandbox's own address that an operator
        approves with the PIN. An approved top-up raises the ceiling of the same budget instead of
        creating a second account and stranding the remainder.
        """
        self._require_unlocked()
        sandbox_id = str(params.get("sandbox_id", "")).strip()
        sandbox = self.sandboxes.get(sandbox_id)
        if sandbox is None:
            raise WalletError("unknown_sandbox", "sandbox id is not recognised")
        if sandbox.tampered:
            raise WalletError("sandbox_tampered",
                              "this budget's record was edited outside the wallet; revoke it")
        if sandbox.revoked_at or sandbox.destroyed_at:
            raise WalletError("sandbox_closed", "that sandbox is closed; ask for a new budget")
        if sandbox.expired():
            raise WalletError("sandbox_expired", "that budget expired; ask for a new one")
        if sandbox.account_index < 0:
            raise WalletError("not_fundable",
                              "this budget is enforced against the main balance and holds no address "
                              "of its own; ask the operator to raise it")
        try:
            amount = parse_amount(params.get("amount"), sandbox.asset)
        except (ValueError, ArithmeticError) as exc:
            raise WalletError("bad_amount", f"amount: {exc}") from exc
        why = str(params.get("why", "") or "").strip()
        funding = self.request_tx({"kind": sandbox.kind, "asset": sandbox.asset,
                                   "amount": str(amount), "payee": sandbox.address,
                                   "memo": f"top up sandbox {sandbox.label or sandbox_id}",
                                   "why": why or "more budget for the current task",
                                   "funding_for": sandbox_id})
        self._record("sandbox", "topup_requested", "agent",
                     {"sandbox_id": sandbox_id, "amount": str(amount), "asset": sandbox.asset,
                      "remaining": format(sandbox.remaining(), "f")})
        return {"sandbox": sandbox.to_dict(), "funding": funding}

    def sandbox_create(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Create a capped sub-wallet and file the funding transfer for operator approval."""
        self._require_unlocked()
        kind = str(params.get("kind", "")).strip()
        asset = str(params.get("asset", "")).upper().strip()
        proposed = bool(params.get("proposed"))
        unattended = bool(params.get("unattended"))
        if unattended:
            # A proposal is the agent asking; an armed budget is the human handing over money. Only
            # the second can exist unattended, and only with the secret itself — the operator token
            # is a file on the same uid, so a token is not a human.
            if proposed:
                raise WalletError("unattended_needs_operator",
                                  "a proposed budget cannot arm itself; the operator arms it")
            self._require_passphrase(params, "an unattended budget spends without asking a human")
            self._verify_pin(params)
        if not kind:
            raise WalletError("bad_chain", "a sandbox needs a chain (kind)")
        if not asset:
            asset = self.NATIVE_ASSET.get(kind, "")
        # Everything below used to raise a bare ValueError (reported as `internal_error`), or was only
        # caught by the funding request *after* a sub-account and a sandbox record already existed.
        if asset not in self._chain_assets(kind):
            raise WalletError("asset_chain_mismatch",
                              f"{asset or '(no coin)'} is not a coin on {kind} (it holds "
                              f"{', '.join(self._chain_assets(kind))})")
        try:
            budget = parse_amount(params.get("budget"), asset)
            exact = budget.quantize(Decimal(1).scaleb(-self.ASSET_DECIMALS.get(asset, 12)))
        except (ValueError, ArithmeticError) as exc:
            raise WalletError("bad_amount", f"budget: {exc}") from exc
        if exact != budget:
            # the funding transfer is quantised to the coin's precision and must equal the budget
            # exactly, so a budget finer than the coin could never be funded
            raise WalletError("bad_amount",
                              f"{asset} has {self.ASSET_DECIMALS.get(asset, 12)} decimal places; "
                              f"a budget of {budget} cannot be expressed")
        try:
            hours = int(params.get("lifetime_hours", 24))
        except (TypeError, ValueError) as exc:
            raise WalletError("bad_setting", "lifetime_hours must be a whole number") from exc
        label = str(params.get("label", ""))[:40]
        mode = str(params.get("mode", "budget") or "budget").strip().lower()
        if mode not in ("budget", "one_time", "pooled"):
            raise WalletError("bad_setting", "mode must be 'budget', 'one_time' or 'pooled'")
        try:
            destruct = int(params.get("self_destruct_seconds",
                                      getattr(self.settings, "sandbox_destruct_seconds", 1800)))
        except (TypeError, ValueError) as exc:
            raise WalletError("bad_setting", "self_destruct_seconds must be a whole number") from exc
        if destruct < 60 or destruct > 86400:
            raise WalletError("bad_setting", "self_destruct_seconds must be 60..86400")
        if hours < 1 or hours > self.settings.max_mandate_lifetime_hours:
            raise WalletError("bad_setting", f"lifetime_hours must be 1..{self.settings.max_mandate_lifetime_hours}")
        adapter = self.adapters.get(kind)
        if adapter is None:
            raise WalletError("chain_unavailable", f"chain {kind} is not available")
        if mode == "pooled" and getattr(adapter, "supports_accounts", False):
            # `pooled` on an account chain used to create a real sub-account AND a funding request,
            # then no-op every sweep — money in an account nothing would return. Refuse instead:
            # on this chain the normal funded sandbox is the better mechanism.
            raise WalletError("bad_setting",
                              f"{kind} can hold a real sandbox account; drop mode='pooled' and fund it")
        if not getattr(adapter, "supports_accounts", False):
            # No separate account on this chain, so the sandbox is a *policy* budget over the main
            # account (or its own key later). Moving funds to a fresh Bitcoin address would cost a
            # fee and take a confirmation window before the agent could spend a cent — unusable for a
            # task that is running now — and the containment we actually need is the cap plus the
            # destination allowlist, both of which the daemon enforces either way.
            return self._sandbox_create_pooled(params, kind, asset, budget, hours, label, mode,
                                               destruct, proposed)
        # checked before the account exists: a bad address used to leave an orphan sub-account behind
        payees = [] if proposed else self._checked_payees(kind, params.get("allowed_payees"))
        try:
            account = adapter.create_account(label or "agent sandbox")
        except ChainError as exc:
            raise WalletError(exc.code, exc.message) from exc

        sandbox = Sandbox(
            sandbox_id="sbx_" + secrets.token_hex(5), kind=kind, asset=asset,
            account_index=account["account_index"], address=account["address"],
            budget=budget, spent=Decimal("0"), created_at=now_iso(),
            expires_at=(datetime.now(timezone.utc) + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            label=label, mode=mode, self_destruct_seconds=destruct,
            # A proposal cannot bless its own destinations: the agent naming an address here would
            # turn the operator's single funding approval into "pay this attacker, no questions".
            # Recommended destinations are reported, never applied; an operator sets them (with a
            # passphrase) after creation.
            allowed_profiles=[] if proposed else
            [str(p).strip() for p in (params.get("allowed_profiles") or []) if str(p).strip()],
            allowed_payees=payees,
            unattended=unattended,
            armed_at=now_iso() if unattended else "",
        )
        self.sandboxes[sandbox.sandbox_id] = sandbox
        self._save_sandboxes()

        # Funding is a real transfer to the new account, so it goes through a human.
        funding = self.request_tx({"kind": kind, "asset": asset, "amount": str(budget),
                                   "payee": sandbox.address, "memo": f"fund sandbox {sandbox.label}",
                                   "why": ("sandbox budget requested by the agent" if proposed
                                           else "sandbox budget"),
                                   "funding_for": sandbox.sandbox_id})
        if self.ledger is not None:
            self.ledger.append("sandbox", "proposed" if proposed else "created",
                               "agent" if proposed else "operator",
                               {"sandbox_id": sandbox.sandbox_id, "account_index": sandbox.account_index,
                                "budget": str(budget), "asset": asset, "kind": kind,
                                "mode": mode, "self_destruct_seconds": destruct,
                                "unattended": unattended})
        result = {"sandbox": sandbox.to_dict(), "funding": funding}
        if proposed and (params.get("allowed_payees") or params.get("allowed_profiles")):
            result["ignored"] = ["allowed_payees", "allowed_profiles"]
            result["ignored_reason"] = ("a proposal may not name its own destinations; the operator "
                                        "sets them after funding, with the passphrase")
        return result

    def sandbox_propose(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """The agent asks for a budget; the human still moves the money.

        Same object as `sandbox_create`, filed by the agent instead of the operator. The funding
        transfer lands as a *pending* request, and only an operator-scope approval can broadcast it —
        so this gives an agent a way to ask for spending room without giving it one.
        """
        result = self.sandbox_create({**params, "proposed": True})
        # a pooled proposal says how it is armed; a funded one waits for its transfer
        return {"needs": "operator approval of the funding request", **result}

    def sandbox_sweep(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Return an unfunded remainder to the main account, then close the sandbox.

        The transfer spends *from* the sandbox account, so it takes the sandbox's no-approval path —
        the operator asked for the sweep, and the money goes home, not to a third party.
        """
        self._require_unlocked()
        sandbox_id = str(params.get("sandbox_id", ""))
        sandbox = self.sandboxes.get(sandbox_id)
        if sandbox is None:
            raise WalletError("unknown_sandbox", "sandbox id is not recognised")
        # Deliberately no "is it enabled" gate: a sweep only ever pays this wallet's own address
        # (checked in request_tx), so recovering dust from a closed or destroyed sandbox is always
        # safe - and refusing it stranded up to a fee's worth per sandbox forever.
        if sandbox.account_index < 0:
            sandbox.enabled = False
            sandbox.revoked_at = sandbox.revoked_at or now_iso()
            self._save_sandboxes()
            self._record("sandbox", "closed", "operator",
                         {"sandbox_id": sandbox_id, "spent": str(sandbox.spent),
                          "budget": str(sandbox.budget),
                          "reason": "pooled budget: no funds were ever moved, so there is nothing to sweep"})
            return {"swept": "0", "sent": False, "sandbox": sandbox.to_dict(),
                    "note": "a pooled budget holds no funds of its own — the main balance never moved"}
        adapter = self.adapters.get(sandbox.kind)
        if adapter is None:
            raise WalletError("chain_unavailable", f"chain {sandbox.kind} is not available")
        # Sweep what is actually in the account, not what the bookkeeping thinks is left: after one
        # sweep `spent` equals `budget`, so remaining() is 0 while coins may still sit in the account.
        remaining = sandbox.remaining()

        def read_balance() -> Decimal:
            """What the account actually holds; raises when it cannot be read."""
            reader = getattr(adapter, "balance_at", None)
            if reader is not None:
                return Decimal(str(reader(sandbox.account_index)))
            live = adapter.account_balance(sandbox.account_index)
            return Decimal(str(live.get("unlocked", live.get("balance", "0"))))

        try:
            remaining = read_balance()
        except Exception:  # noqa: BLE001 - fall back to the budget arithmetic
            pass
        try:
            home_address = adapter.address()          # account 0 of the same wallet
        except ChainError as exc:
            raise WalletError(exc.code, exc.message) from exc

        # What this sweep will really cost. `_fee_reserve` is a floor (fee_cap for a chain that has no
        # static estimate, which is every real Monero adapter); planning the transfer once gives the
        # actual fee, so the amount sent is balance - fee rather than balance - ceiling. Without this
        # every real teardown left ~0.0005 XMR behind, and the destroy then orphaned it.
        reserve = self._fee_reserve(sandbox.kind, sandbox.asset)
        try:
            probe_amount = remaining - reserve if remaining > reserve else Decimal("0")
            probe = adapter.plan_transfer(probe_amount, home_address,
                                          account_index=sandbox.account_index,
                                          asset=sandbox.asset)
            planned_fee = Decimal(str(getattr(probe, "fee", 0) or 0))
            if planned_fee > 0:
                reserve = planned_fee
        except Exception:  # noqa: BLE001 - an unplannable sweep is caught below at request_tx
            pass

        amount = remaining - reserve
        if amount <= 0:
            sandbox.enabled = False
            self._save_sandboxes()
            # A receipt, because "nothing to sweep" is a state change (the sandbox closes) and the
            # ledger used to have no record of it.
            self._record("sandbox", "swept_none", "operator",
                         {"sandbox_id": sandbox_id, "left": str(remaining),
                          "reserve": str(reserve),
                          "reason": "the remainder is not worth a transfer fee"})
            return {"swept": "0", "sent": False, "fee_reserved": str(reserve),
                    "left": str(remaining), "sandbox": sandbox.to_dict(),
                    "note": "nothing left to sweep"}

        # A sweep is a self-transfer, not a payment: it moves the operator's own money back to their
        # own account 0, and the payee was computed from the adapter. Sending it through `request_tx`
        # made the PIN gate convert it into a pending approval whose id was then dropped on the floor —
        # the pane's "Withdraw left" reported a success-shaped `swept: "0"` and left the budget funded.
        try:
            # plan for the amount that will actually be sent (the probe above used the reserve floor)
            plan = adapter.plan_transfer(amount, home_address, account_index=sandbox.account_index,
                                         asset=sandbox.asset)
        except ChainError as exc:
            self._record("sandbox", "sweep_failed", "chain",
                         {"sandbox_id": sandbox_id, "code": exc.code,
                          "reason": exc.message[:200], "left": str(remaining)})
            return {"swept": "0", "sent": False, "fee_reserved": str(reserve), "to": home_address,
                    "sandbox": sandbox.to_dict(),
                    "note": f"the sweep did not go through ({exc.code}): {exc.message[:120]}"}
        req = SpendRequest(request_id="req_" + secrets.token_hex(6), kind=sandbox.kind,
                           asset=sandbox.asset, amount=amount, payee=home_address,
                           memo=f"close sandbox {sandbox.label}"[:200],
                           why="return unused sandbox budget", sandbox_id=sandbox_id,
                           sweep_of=sandbox_id, created_at=now_iso())
        req.authorised_by = "operator"          # the operator asked; the destination is ours
        req.status = "approved"
        req.fee_estimate = str(reserve)
        self.requests[req.request_id] = req
        self._save_requests()
        try:
            execution = self._broadcast(req, plan, adapter)
        except WalletError as exc:
            req.status = "failed"
            self._save_requests()
            self._record("sandbox", "sweep_failed", "chain",
                         {"sandbox_id": sandbox_id, "code": exc.code,
                          "reason": exc.message[:200], "left": str(remaining)})
            return {"swept": "0", "sent": False, "fee_reserved": str(reserve),
                    "to": home_address, "sandbox": sandbox.to_dict(),
                    "note": f"the sweep did not go through ({exc.code}): {exc.message[:120]}"}
        tx = str(execution.get("tx_hash") or "")
        # Close the sandbox only when the account is actually empty: a partial sweep (a balance that
        # did not all unlock, or a probe that read less than the account holds) must leave it open so
        # the rest can be recovered.
        # `_broadcast` owns the bookkeeping for a sweep: it re-reads the account, closes the sandbox
        # only when the money is really gone, and writes `swept` or `swept_partial`. Doing it here too
        # wrote the receipt twice.
        leftover = None
        try:
            leftover = read_balance()
        except Exception:  # noqa: BLE001 - unreadable is not "empty"
            leftover = None

        return {"swept": format(amount, "f"), "sent": True, "fee_reserved": str(reserve),
                "to": home_address, "tx_hash": tx, "sandbox": sandbox.to_dict(),
                "transfer": execution, "sweep_rule": "sweep",
                "note": ("" if leftover is not None and leftover <= 0
                         else f"{leftover} {sandbox.asset} is still in the sandbox"
                         if leftover is not None
                         else "sent; the sandbox balance could not be read back yet")}

    def _arm_self_destruct(self, sandbox: Sandbox) -> None:
        """Start the one-time countdown, once, when the payment it existed for has settled."""
        if sandbox.mode != "one_time" or sandbox.destroy_at or sandbox.destroyed_at:
            return
        due = datetime.now(timezone.utc) + timedelta(seconds=sandbox.self_destruct_seconds)
        sandbox.spent_at = now_iso()
        sandbox.destroy_at = due.strftime("%Y-%m-%dT%H:%M:%SZ")
        self._save_sandboxes()
        if self.ledger is not None:
            self.ledger.append("sandbox", "self_destruct_armed", "policy",
                               {"sandbox_id": sandbox.sandbox_id, "asset": sandbox.asset,
                                "spent": str(sandbox.spent), "destroy_at": sandbox.destroy_at,
                                "self_destruct_seconds": sandbox.self_destruct_seconds})

    def reap_sandboxes(self) -> List[Dict[str, Any]]:
        """Destroy one-time sandboxes whose clock has run out. Safe to call on any tick.

        Nothing happens while the wallet is locked: a sweep needs the signer, and destroying a
        sandbox that still holds money would strand it. Waiting is always cheaper than losing funds.
        """
        due: List[Dict[str, Any]] = []
        if not self.is_unlocked():
            return due
        now = datetime.now(timezone.utc)
        for sandbox in list(self.sandboxes.values()):
            if sandbox.mode != "one_time" or sandbox.destroyed_at:
                continue
            reason = ""
            if sandbox.destroy_at:
                try:
                    when = datetime.strptime(sandbox.destroy_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
                if now >= when:
                    reason = "one_time_elapsed"
            elif sandbox.spent == 0 and sandbox.expired():
                # funded for a payment that never came: sweep it home instead of leaving money in a
                # forgotten sub-account
                reason = "lifetime_elapsed"
            if not reason:
                continue
            try:
                outcome = self._destroy_sandbox(sandbox, {"reason": reason})
            except WalletError as exc:
                if not sandbox.destroy_blocked:      # keep the specific reason, not just the code
                    sandbox.destroy_blocked = exc.code
                    self._save_sandboxes()
                outcome = {"sandbox_id": sandbox.sandbox_id, "destroyed": False,
                           "blocked": sandbox.destroy_blocked or exc.code, "reason": exc.message}
            due.append(outcome)
        return due

    def _destroy_sandbox(self, sandbox: Sandbox, params: Dict[str, Any]) -> Dict[str, Any]:
        """Close a sandbox forever, but only once it holds nothing worth keeping.

        The order matters and is the whole safety story: read the live balance, send the remainder
        home, read it again, and only then destroy. Destroying keys first would make the money
        unrecoverable, so every check here fails towards keeping the sandbox alive.
        """
        if sandbox.destroyed_at:
            return {"sandbox_id": sandbox.sandbox_id, "destroyed": False, "reason": "already_destroyed"}
        if sandbox.account_index < 0:
            # A pooled budget never held funds of its own, so there is no balance to read and nothing
            # to sweep: closing it is bookkeeping. Without this branch a one-time budget on Bitcoin
            # asked the chain for an account that does not exist and stayed "blocked" for ever.
            sandbox.destroyed_at = now_iso()
            sandbox.destroy_blocked = ""
            sandbox.enabled = False
            sandbox.last_balance = "0"
            self._save_sandboxes()
            receipt = {"sandbox_id": sandbox.sandbox_id, "asset": sandbox.asset, "kind": sandbox.kind,
                       "spent": str(sandbox.spent), "left": "0", "swept": "0", "mode": sandbox.mode,
                       "pooled": True, "reason": str(params.get("reason", "manual")),
                       "keys": "policy budget over the main account: no funds were held, none moved"}
            self._record("sandbox", "destroyed", "policy", receipt)
            return {"sandbox_id": sandbox.sandbox_id, "destroyed": True, **receipt}
        adapter = self.adapters.get(sandbox.kind)
        if adapter is None:
            sandbox.destroy_blocked = "chain_unavailable"
            self._save_sandboxes()
            raise WalletError("chain_unavailable",
                              f"{sandbox.kind} is not available; keeping the sandbox so nothing is lost")
        reserve = self._fee_reserve(sandbox.kind, sandbox.asset)
        # "unlocked" is the wrong question for destruction: change from the one-time payment can still
        # be confirming while the unlocked balance reads 0, and destroying on that would strand it
        reader = getattr(adapter, "total_balance_at", None) or getattr(adapter, "balance_at", None)
        if reader is None:
            sandbox.destroy_blocked = "balance_unsupported"
            self._save_sandboxes()
            raise WalletError("unsupported",
                              f"{sandbox.kind} cannot report a sandbox balance; refusing to destroy")
        try:
            balance = Decimal(str(reader(sandbox.account_index)))
        except (ChainError, WalletError) as exc:
            code = getattr(exc, "code", "balance_unreadable")
            sandbox.destroy_blocked = f"balance_unreadable:{code}"
            self._save_sandboxes()
            raise WalletError("balance_unreadable",
                              f"cannot read the sandbox balance ({code}); keeping it alive") from exc
        sandbox.last_balance = str(balance)

        swept: Dict[str, Any] = {"attempted": False}
        if balance > reserve:
            swept = {"attempted": True}
            try:
                swept.update(self.sandbox_sweep({"sandbox_id": sandbox.sandbox_id}))
                transfer = swept.get("transfer") or {}
                if swept.get("note") == "nothing left to sweep":
                    # the account was already emptied by a previous pass: that is the goal state, not
                    # a failure, and refusing here wedged the sandbox in "sweep_failed" forever
                    pass
                elif not transfer.get("auto") and not transfer.get("tx_hash"):
                    # the sweep was deferred (node down, or it was filed for approval): the money is
                    # still in the sandbox, so this is not a destruction, it is a retry
                    code = (transfer.get("decision") or {}).get("code", "deferred")
                    sandbox.destroy_blocked = f"sweep_failed:{code}"
                    self._save_sandboxes()
                    raise WalletError("sweep_failed", f"sweep was deferred ({code}); keeping the sandbox alive")
            except WalletError as exc:
                # do not overwrite the specific reason a deferred sweep already recorded
                if not sandbox.destroy_blocked:
                    sandbox.destroy_blocked = f"sweep_failed:{exc.code}"
                    self._save_sandboxes()
                raise WalletError("sweep_failed",
                                  f"could not return {balance} {sandbox.asset} home ({exc.code}); "
                                  f"keeping the sandbox alive until it can") from exc
            try:
                balance = Decimal(str(reader(sandbox.account_index)))
            except (ChainError, WalletError):
                pass
            sandbox.last_balance = str(balance)
            if balance > reserve:
                sandbox.destroy_blocked = "sweep_pending"
                self._save_sandboxes()
                raise WalletError("sweep_pending",
                                  f"{balance} {sandbox.asset} is still in the sandbox; waiting for the sweep to settle")

        if balance > 0 and self.ledger is not None:
            # whatever the sweep could not move is destroyed with the keys: that is a fact the chain
            # must carry, or the numbers in `swept` and `left` never add up to what was there
            self.ledger.append("sandbox", "swept_dust", "policy",
                               {"sandbox_id": sandbox.sandbox_id, "left": str(balance),
                                "reserve": str(reserve),
                                "reason": "remainder is below the transfer fee; unrecoverable once the keys go"})
        sandbox.destroyed_at = now_iso()
        sandbox.destroy_blocked = ""
        sandbox.enabled = False
        self._save_sandboxes()
        receipt = {"sandbox_id": sandbox.sandbox_id, "asset": sandbox.asset, "kind": sandbox.kind,
                   "spent": str(sandbox.spent), "left": str(balance), "reserve": str(reserve),
                   "swept": swept.get("swept", "0"), "mode": sandbox.mode,
                   "reason": str(params.get("reason", "manual")),
                   "keys": "sub-account of the main wallet: no separate key existed to destroy"}
        if self.ledger is not None:
            self.ledger.append("sandbox", "destroyed", "policy", receipt)
        return {"sandbox_id": sandbox.sandbox_id, "destroyed": True, **receipt}

    def sandbox_destroy(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Operator: destroy now instead of waiting for the clock. Same fund guards as the reaper."""
        self._require_unlocked()
        sandbox_id = str(params.get("sandbox_id", ""))
        sandbox = self.sandboxes.get(sandbox_id)
        if sandbox is None:
            raise WalletError("unknown_sandbox", "sandbox id is not recognised")
        if sandbox.mode != "one_time" and not params.get("force"):
            raise WalletError("not_one_time",
                              "only one-time sandboxes self-destruct; pass force to close a budget sandbox")
        return self._destroy_sandbox(sandbox, {"reason": str(params.get("reason", "manual"))})

    def sandbox_purge(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Operator: forget sandboxes that are already closed and hold nothing.

        A destroyed or revoked budget keeps a row so the pane can say what happened. Once the
        operator has seen it, the row is dead weight — and on a recording it is noise. Only rows
        with no live account and nothing ever spent are dropped: a purge must never be able to
        hide a budget that still holds money or a history of spending.
        """
        self._require_unlocked()
        dropped: List[str] = []
        kept: Dict[str, Any] = {}
        for sid, sandbox in self.sandboxes.items():
            closed = bool(sandbox.destroyed_at or sandbox.revoked_at) or \
                sandbox.status in ("destroyed", "revoked", "expired")
            if closed and sandbox.account_index < 0 and sandbox.spent == 0:
                dropped.append(sid)
            else:
                kept[sid] = sandbox
        if dropped:
            self.sandboxes = kept
            self._save_sandboxes()
        return {"purged": dropped, "kept": len(kept)}

    def requests_purge(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Operator: forget requests that are over and never reached a chain.

        A denied or cancelled request already has a receipt in the ledger; the row here is only a
        working record. Anything pending, approved or carrying a tx hash is kept — a purge must not
        be able to erase a payment that happened, or one still in flight.
        """
        self._require_unlocked()
        dropped: List[str] = []
        kept: Dict[str, Any] = {}
        for rid, req in self.requests.items():
            dead = (req.status in ("denied", "cancelled", "expired", "retry")
                    and not (getattr(req, "tx_hash", "") or ""))
            if dead:
                dropped.append(rid)
            else:
                kept[rid] = req
        if dropped:
            self.requests = kept
            self._save_requests()
        return {"purged": dropped, "kept": len(kept)}

    def sandbox_set_profiles(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Operator: restrict which Hermes profiles may spend from a sandbox (empty = all)."""
        self._require_unlocked()
        sandbox_id = str(params.get("sandbox_id", ""))
        sandbox = self.sandboxes.get(sandbox_id)
        if sandbox is None:
            raise WalletError("unknown_sandbox", "sandbox id is not recognised")
        profiles = [str(p).strip() for p in (params.get("allowed_profiles") or []) if str(p).strip()]
        before = set(sandbox.allowed_profiles)
        after = set(profiles)
        widened = (not after) or bool(after - before)      # empty list = every profile
        if widened:
            # tightening is free; letting more profiles spend a budget is a policy change and gets
            # the same treatment as raising a cap
            self._require_passphrase(params, "widening a sandbox's profile list")
        sandbox.allowed_profiles = profiles
        self._save_sandboxes()
        if self.ledger is not None:
            self.ledger.append("sandbox", "profiles_changed", "operator",
                               {"sandbox_id": sandbox_id, "allowed_profiles": profiles})
        return {"sandbox": sandbox.to_dict()}

    def sandbox_set_payees(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Operator: the addresses this budget may pay without asking (empty = contacts only).

        `sandbox_propose` refuses to let the agent name destinations, and the operator cannot set
        them at creation because the proposal does not exist yet — so this is the setter that makes
        "bless it after funding, with the passphrase" true. Adding an address is a policy change
        (it widens what the agent may pay unattended) and costs the passphrase; removing one does not.
        """
        self._require_unlocked()
        sandbox_id = str(params.get("sandbox_id", ""))
        sandbox = self.sandboxes.get(sandbox_id)
        if sandbox is None:
            raise WalletError("unknown_sandbox", "sandbox id is not recognised")
        if sandbox.tampered:
            raise WalletError("sandbox_tampered",
                              "this budget's record was edited outside the wallet; revoke it")
        if sandbox.revoked_at or sandbox.destroyed_at:
            raise WalletError("sandbox_closed", "that sandbox is closed; it cannot take new payees")
        raw = params.get("allowed_payees")
        if raw is None:
            raise WalletError("bad_setting", "allowed_payees is required (a list, empty to clear)")
        checked: List[str] = []
        for address in raw:
            text = str(address).strip()
            if not text:
                continue
            try:
                checked.append(payee_key(sandbox.kind, text))
            except ValueError as exc:
                raise WalletError("bad_payee", f"{text[:24]}…: {exc}") from exc
        before, after = set(sandbox.allowed_payees), set(checked)
        if after - before:
            self._require_passphrase(params, "letting this budget pay a new destination unattended")
        sandbox.allowed_payees = checked
        self._save_sandboxes()
        self._record("sandbox", "payees_changed", "operator",
                     {"sandbox_id": sandbox_id, "allowed_payees": checked,
                      "added": sorted(after - before), "removed": sorted(before - after)})
        return {"sandbox": sandbox.to_dict()}

    def sandbox_revoke(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Close a sandbox. By default the leftover budget is swept home, not abandoned.

        Revoking used to leave the remainder sitting in a sub-account nobody would ever look at
        again. The sweep is the same operator-initiated transfer as `wallet sandbox sweep`, so it
        still goes through policy and a receipt; if the chain or signer is down it is skipped with a
        reason rather than failing the revoke.
        """
        sandbox_id = str(params.get("sandbox_id", ""))
        sandbox = self.sandboxes.get(sandbox_id)
        if sandbox is None:
            raise WalletError("unknown_sandbox", "sandbox id is not recognised")
        was_funded = sandbox.enabled
        was_unattended = sandbox.unattended
        sandbox.enabled = False
        # Closing the budget ends the hand-over too: a late "arm" on a revoked sandbox must not be
        # the only thing standing between an injected agent and the remainder.
        sandbox.unattended = False
        sandbox.armed_at = ""
        if not sandbox.revoked_at:
            sandbox.revoked_at = now_iso()
        self._save_sandboxes()
        if self.ledger is not None:
            self.ledger.append("sandbox", "revoked", "operator",
                               {"sandbox_id": sandbox_id, "spent": str(sandbox.spent),
                                "remaining": str(sandbox.remaining()),
                                "was_unattended": was_unattended})

        # A funding request for this sandbox can still be pending. Approving it later would move money
        # into a budget the operator just closed, so it is cancelled here, and a late approval is
        # refused in `_broadcast`.
        cancelled: List[str] = []
        for req in list(self.requests.values()):
            if req.funding_for == sandbox_id and req.status == "pending":
                req.status = "cancelled"
                for approval in list(self.approvals.values()):
                    if approval.request_id == req.request_id and not approval.used:
                        approval.used = True
                cancelled.append(req.request_id)
                self._record("spend_request", "cancelled", "operator",
                             {"request_id": req.request_id, "code": "sandbox_revoked",
                              "reason": "the sandbox was revoked before its funding was approved"})
        if cancelled:
            self._save_requests()

        sweep: Dict[str, Any] = {"attempted": False, "cancelled_funding": cancelled}
        if params.get("sweep", True) and was_funded and sandbox.remaining() > 0:
            if sandbox.kind not in self.adapters:
                sweep = {"attempted": False, "cancelled_funding": cancelled,
                         "note": f"{sandbox.kind} is not available right now; "
                                 "the remainder stays in the sub-account"}
            else:
                sweep = {"attempted": True, "cancelled_funding": cancelled}
                try:
                    # `_sandbox_decision` lets a sweep out of a revoked sandbox on purpose, so no
                    # temporary re-enable is needed (that toggle used to be the only reason the
                    # sweep worked, and it also briefly re-armed the budget).
                    swept = self.sandbox_sweep({"sandbox_id": sandbox_id})
                    sweep["result"] = swept
                    sweep["swept"] = swept.get("swept")
                    sweep["sent"] = swept.get("sent", False)
                except WalletError as exc:
                    sweep["error"] = exc.code
                    sweep["reason"] = exc.message
                    sweep["sent"] = False
        return {"sandbox": sandbox.to_dict(), "sweep": sweep}

    def _payee_is_blessed(self, sandbox: "Sandbox", payee: str) -> bool:
        """Has a human already said yes to this destination for this budget?"""
        if payee in set(sandbox.allowed_payees):
            return True
        # a saved contact is a human decision too: the address was typed into the address book by
        # someone, and the agent can only name it, never substitute a different one
        for contact in self.contacts.values():
            if str(getattr(contact, "created_by", "operator")) != "operator":
                continue          # the agent's own address book is not a human decision
            saved = contact.addresses.get(sandbox.kind)
            if saved and payee_key(sandbox.kind, saved) == payee:
                return True
        return False

    def _sandbox_decision(self, req: "SpendRequest") -> Optional[Decision]:
        """Allow a spend without a human when it is inside an active sandbox's remaining budget."""
        sandbox = self.sandboxes.get(req.sandbox_id)
        if sandbox is None:
            # A sandbox id that names no budget must never mean "no sandbox": returning None fell
            # through to the normal path, which planned the payment from account 0 — the main
            # balance — with the budget and the destination allowlist silently dropped. A one
            # character typo was enough to spend 0.10 from a 0.05 budget out of the wallet itself.
            return Decision(False, "sandbox", "unknown_sandbox",
                            f"no sandbox '{req.sandbox_id[:24]}' — nothing was planned or signed")
        limit = self._sandbox_limits(req, sandbox)
        if limit is not None:
            return limit
        if req.sweep_of == sandbox.sandbox_id:
            # Returning the balance home is not spending the budget: the funds go to our own address,
            # and this must work after the budget is spent or the sandbox is closed - that is exactly
            # when a teardown needs it. Checked *after* the ACL/funding gates, because a sweep used to
            # bypass all of them (any profile could force one), and *before* the budget arithmetic,
            # which a sweep legitimately exceeds.
            return Decision(True, "sandbox", "sandbox_sweep",
                            "returning the sandbox balance to the wallet")
        if sandbox.unattended and not sandbox.allowed_payees:
            # The operator handed over this budget deliberately: the ceiling is the containment, and
            # asking for a destination blessing would make the arm meaningless. If they narrowed the
            # list, the list still decides (checked just below).
            pass
        elif not self._payee_is_blessed(sandbox, req.payee):
            # Returning None escalates to the normal path: a human is asked to approve *this*
            # destination. Before this, a prompt-injected agent with a funded sandbox could pay any
            # address on the planet as long as the total stayed under the budget. It comes LAST: the
            # escalation used to sit above the budget, expiry and scope checks, so a payment to a new
            # address skipped every one of them on its way to the approval card.
            return None
        return Decision(True, "sandbox", "sandbox_ok",
                        f"inside sandbox budget ({sandbox.remaining()} left)")

    def _sandbox_limits(self, req: "SpendRequest", sandbox: "Sandbox") -> Optional[Decision]:
        """The limits a budget imposes whoever approves the payment: a refusal, or None when inside.

        Shared by the request path and by `approve`: a human saying yes to a destination is not a
        human raising the budget, reopening an expired sandbox or paying a different coin out of it.
        """
        if sandbox.tampered and req.sweep_of != sandbox.sandbox_id:
            # someone edited sandboxes.json: the address, the arm or the allowlist in this row cannot
            # be trusted. Refuse every spend and every funding, but keep the sweep: the destination
            # of a sweep is this wallet's own account 0 (derived from the seed, never from the row),
            # so it is the one action that gets the money somewhere safe.
            return Decision(False, "sandbox", "sandbox_tampered",
                            "this budget's record was edited outside the wallet; revoke it (the "
                            "leftover goes to your main account) and make a new one")
        if sandbox.allowed_profiles and (not req.profile or req.profile not in sandbox.allowed_profiles):
            return Decision(False, "sandbox", "sandbox_profile",
                            f"profile '{req.profile}' may not spend from this sandbox")
        if sandbox.destroyed_at:
            return Decision(False, "sandbox", "sandbox_destroyed",
                            "this sandbox was destroyed; its key material is gone")
        if sandbox.revoked_at:
            # Only a sweep home is legitimate from a closed budget; anything else is a spend the
            # operator already ended.
            if req.sweep_of == sandbox.sandbox_id:
                return None
            return Decision(False, "sandbox", "sandbox_closed",
                            "this sandbox was revoked; its budget is closed")
        if not sandbox.enabled:
            return Decision(False, "sandbox", "sandbox_unfunded", "sandbox is not funded yet")
        if req.sweep_of == sandbox.sandbox_id:
            return None                 # a sweep legitimately exceeds the budget arithmetic below
        # a spend from the sandbox pays its own network fee out of the same budget, so the last
        # payment has to leave the worst-case fee behind or it cannot be signed at all. An x402
        # payment is an EIP-3009 authorisation the *facilitator* settles: this wallet sends no
        # transaction, so reserving a gas fee left a 0.05 budget able to buy exactly one 0.025 widget.
        x402 = (req.challenge or {}).get("settlement") == "x402"
        reserve = Decimal("0") if x402 else self._fee_reserve(sandbox.kind, req.asset)
        if req.amount + reserve > sandbox.remaining():
            return Decision(False, "sandbox", "sandbox_budget",
                            f"{req.amount} + {reserve} fee exceeds the {sandbox.remaining()} left "
                            f"in this sandbox — ask for more with wallet_request_sandbox("
                            f"sandbox_id={sandbox.sandbox_id!r})")
        # A sweep is not a second payment: it is the sandbox emptying itself on the way out, and the
        # one-time rule must not block the very transfer that makes a safe teardown possible.
        if sandbox.mode == "one_time" and sandbox.spent > 0 and req.sweep_of != sandbox.sandbox_id:
            return Decision(False, "sandbox", "sandbox_one_time_used",
                            "this sandbox is for a single payment; it has already been spent")
        if sandbox.expired():
            return Decision(False, "sandbox", "sandbox_expired", "sandbox has expired")
        if (sandbox.kind, sandbox.asset) != (req.kind, req.asset):
            return Decision(False, "sandbox", "sandbox_scope", "sandbox covers another chain or asset")
        if req.amount > sandbox.remaining():
            return Decision(False, "sandbox", "sandbox_budget",
                            f"amount {req.amount} exceeds the remaining sandbox budget "
                            f"{sandbox.remaining()} {req.asset} — ask for more with "
                            f"wallet_request_sandbox(sandbox_id={sandbox.sandbox_id!r})")
        return None

    def export_seed(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Operator-only recovery seed export.

        Requires an unlocked wallet AND the passphrase again (re-verified against the envelope), and
        writes a receipt that records *that it happened* without ever containing the words.
        """
        self._require_unlocked()
        if not params.get("confirm"):
            raise WalletError("confirm_required", "set confirm=true to export the recovery seed")
        try:
            keystore.open_sealed(keystore.load(self.keystore_path), str(params.get("passphrase", "")))
        except keystore.KeystoreError as exc:
            raise WalletError("bad_passphrase", "passphrase does not match the keystore") from exc
        kind = str(params.get("kind", "monero-mainnet"))
        adapter = self.adapters.get(kind) or self.adapters.get("monero-mainnet")
        if adapter is None:
            raise WalletError("chain_unavailable", f"chain {kind} is not available")
        try:
            words = adapter.mnemonic()
        except ChainError as exc:
            raise WalletError(exc.code, exc.message) from exc
        if not words:
            raise WalletError("unsupported", "this wallet has no exportable mnemonic")
        if self.ledger is not None:
            self.ledger.append("wallet", "seed_exported", "operator",
                               {"kind": kind, "words": len(words.split())})
        return {"mnemonic": words, "kind": kind, "words": len(words.split()),
                "warning": "Anyone with these words owns this wallet. Write them down offline."}

    # ---------- settings ----------

    ASSET_DECIMALS = {"XMR": 12, "BTC": 8, "ETH": 18, "POL": 18, "USDC": 6, "USDT": 6,
                      "DAI": 18, "XRP": 6}

    CONFIG_KEYS = ("fee_cap", "chains_enabled", "assets_enabled", "per_tx_cap", "daily_cap",
                   "approval_ttl_seconds", "autolock_minutes", "sandbox_destruct_seconds",
                   "approval_threshold", "payee_allowlist", "monero_rpc_url",
                   "monero_provider", "unattended_policy", "prefer_asset", "monero_only",
                   "prefer_monero", "monero_wallet_file", "pin_every_payment",
                   "gas_floor", "gas_floor_transfers", "gas_reserve",
                   "swaps_enabled", "suggest_swaps", "swap_slippage_bps", "swap_routers")

    # Settings that change what the wallet will do by itself. Toggling a coin or a node URL is a
    # preference; raising a cap, emptying an allowlist or changing the unattended policy is policy.
    # autolock is deliberately *not* here: it is checked directionally below, because shortening the
    # window (or locking sooner) removes authority and needs no secret, while lengthening it or
    # turning it off keeps keys loaded for longer and does.
    # includes the signer endpoint: pointing it at another host changes which address the wallet
    # reports (and where the human is told to send money), sends the signer's basic-auth credentials
    # there, and makes a later sweep "our address" somebody else's.
    SENSITIVE_KEYS = ("per_tx_cap", "daily_cap", "fee_cap", "payee_allowlist", "approval_threshold",
                      "unattended_policy", "monero_only", "prefer_monero", "approval_ttl_seconds",
                      "monero_rpc_url", "monero_provider", "monero_wallet_file",
                      # turning the per-payment gate OFF is exactly the change a token must not make:
                      # it is the difference between "asks me every time" and "does not"
                      "pin_every_payment",
                      # the floor and the refill address decide whether a payment can brick the
                      # chain, and where a human is told to send money: both are policy
                      "gas_floor", "gas_floor_transfers", "gas_reserve",
                      # a swap signs calldata against a contract: whether it is possible at all, how
                      # much slippage is tolerated, and which operators may be used are all policy
                      "swaps_enabled", "suggest_swaps", "swap_slippage_bps", "swap_routers")

    def config_set(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Operator-only settings change; re-signs the config and rebuilds adapters."""
        self._require_unlocked()
        if any(key in params for key in self.SENSITIVE_KEYS):
            self._require_passphrase(params, "these settings decide what the wallet may spend")
        if "autolock_minutes" in params:
            try:
                asked = int(params["autolock_minutes"])
            except (TypeError, ValueError):
                asked = -1
            current = int(getattr(self.settings, "autolock_minutes", 0) or 0)
            # 0 means "never lock" and anything larger keeps the keys loaded for longer: both widen
            # the unlocked window, so both cost a passphrase. Tightening is free.
            if asked == 0 or asked > current:
                self._require_passphrase(params, "keeping the wallet unlocked for longer")
        updates = {k: params[k] for k in self.CONFIG_KEYS if k in params}
        if not updates:
            raise WalletError("no_updates", f"nothing to change (allowed: {', '.join(self.CONFIG_KEYS)})")
        candidate = replace(self.settings, **updates)
        if "chains_enabled" in updates and "assets_enabled" not in updates:
            backed = {self.NATIVE_ASSET.get(chain, "") for chain in updates["chains_enabled"]}
            missing = sorted(a for a in backed if a and a not in self.settings.assets_enabled)
            if missing:
                raise WalletError("bad_setting",
                                  f"those chains need a coin that is not enabled: {', '.join(missing)}")
        if "assets_enabled" in updates:
            needed: List[str] = []
            for asset in candidate.assets_enabled:
                for chain in self.ASSET_CHAINS.get(asset, []):
                    if chain not in needed:
                        needed.append(chain)
            # The simulator is not a coin: keep it exactly as the operator left it — including when
            # the caller sent `chains_enabled` in the same call, which the coin menu does. Before
            # this, passing both keys silently discarded the explicit chain list, so enabling the
            # simulator always looked like it worked and never did.
            for chain in list(self.settings.chains_enabled) + list(updates.get("chains_enabled") or []):
                if chain.startswith("sim-") and chain not in needed:
                    needed.append(chain)
            # A sandbox lives on a chain. Dropping the chain hides the adapter, which means the budget
            # can never be swept home again - so refuse until the operator closes it.
            stranded = [sb for sb in self.sandboxes.values()
                        if sb.enabled and sb.kind not in needed and sb.remaining() > 0]
            if stranded:
                names = ", ".join(f"{sb.sandbox_id} ({sb.remaining()} {sb.asset})" for sb in stranded[:3])
                raise WalletError("sandbox_active",
                                  f"sweep or destroy these sandboxes before removing their coin: {names}")
            candidate.chains_enabled = needed
        # Validate before persisting: a bad value must not brick the signed config.
        for key in ("chains_enabled", "assets_enabled", "payee_allowlist"):
            value = getattr(candidate, key)
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise WalletError("bad_setting", f"{key} must be a list of strings")
            # Dedupe, keep order, normalise case. A UI that toggles coins sends "the whole list with
            # this one added"; a stale render sends one that is already there, and the duplicate used
            # to be persisted - the coin then appeared twice on every surface.
            if key == "assets_enabled":
                value = [v.strip().upper() for v in value if v.strip()]
            elif key == "chains_enabled":
                value = [v.strip() for v in value if v.strip()]
            setattr(candidate, key, [v for i, v in enumerate(value) if v not in value[:i]])
        for key in ("per_tx_cap", "daily_cap"):
            value = getattr(candidate, key)
            if not isinstance(value, dict) or not all(isinstance(v, str) for v in value.values()):
                raise WalletError("bad_setting", f"{key} must map asset -> decimal string")
        try:
            candidate.approval_threshold = config.Decimal(str(candidate.approval_threshold))
        except Exception as exc:  # noqa: BLE001
            raise WalletError("bad_setting", "approval_threshold must be a number") from exc
        if not str(candidate.monero_rpc_url).startswith(("http://", "https://")):
            raise WalletError("bad_setting", "monero_rpc_url must be http(s)")
        # The signer holds the view/spend keys and authenticates to this endpoint with the wallet's
        # credentials, so "somewhere else" is not a preference. Loopback only, which is also what
        # _autostart_signer forces.
        host = str(candidate.monero_rpc_url).split("://", 1)[-1].split("/", 1)[0]
        if not host.startswith(("127.0.0.1", "localhost", "[::1]")):
            raise WalletError("bad_setting",
                              f"monero_rpc_url must point at the local signer (got {host}); "
                              f"a remote endpoint would receive the signer's credentials")
        try:
            minutes = int(candidate.autolock_minutes)
        except (TypeError, ValueError) as exc:
            raise WalletError("bad_setting", "autolock_minutes must be a whole number") from exc
        if minutes < 0 or minutes > 1440:
            raise WalletError("bad_setting", "autolock_minutes must be 0 (never) or 1..1440")
        candidate.autolock_minutes = minutes
        # one knob: the internal TTL is always the switch in seconds
        candidate.unlock_ttl_seconds = minutes * 60
        try:
            approval = int(candidate.approval_ttl_seconds)
        except (TypeError, ValueError) as exc:
            raise WalletError("bad_setting", "approval_ttl_seconds must be a whole number") from exc
        if not 30 <= approval <= 86400:
            raise WalletError("bad_setting", "approval_ttl_seconds must be 30..86400")
        candidate.approval_ttl_seconds = approval
        if not isinstance(candidate.monero_only, bool):
            raise WalletError("bad_setting", "monero_only must be true or false")
        if not isinstance(candidate.prefer_monero, bool):
            raise WalletError("bad_setting", "prefer_monero must be true or false")
        if str(candidate.prefer_asset).upper() not in [a.upper() for a in candidate.assets_enabled]:
            raise WalletError("bad_setting", "prefer_asset must be one of assets_enabled")
        if candidate.unattended_policy not in ("deny", "mandate-only"):
            raise WalletError("bad_setting", "unattended_policy must be deny or mandate-only")

        before = self.settings.to_dict()
        config.save(self.config_path, candidate, self.master or b"")
        self.settings = candidate
        self._build_adapters()
        if self.ledger is not None:
            self.ledger.append("config", "changed", "operator",
                               {"before": before, "after": candidate.to_dict()})
        return {"settings": candidate.to_dict(), "changed": sorted(updates)}

    # ---------- human gate ----------

    def set_human_gate(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """The only way to switch the passphrase prompt off.

        Switching it *off* is the dangerous direction: with the prompt off, the operator token on disk
        is enough to approve a payment, and anything running as this user can read that token. So off
        requires operator scope, an unlocked keystore, an explicit confirm, and the passphrase again,
        re-verified against the envelope. Switching it back *on* removes authority, so it needs only
        the operator scope and the confirm.
        """
        self._require_unlocked()
        enabled = bool(params.get("enabled", True))
        passphrase = "" if params.get("passphrase") is None else str(params.get("passphrase"))
        if not params.get("confirm"):
            raise WalletError("confirm_required", "set confirm=true to change the human gate")
        if not enabled:
            if not passphrase:
                raise WalletError("passphrase_required",
                                  "turning the prompt off needs the wallet passphrase again")
            try:
                keystore.open_sealed(keystore.load(self.keystore_path), passphrase)
            except keystore.KeystoreError as exc:
                raise WalletError("bad_passphrase", "passphrase does not match the keystore") from exc
        before = self.settings.human_gate
        self.settings.human_gate = enabled
        config.save(self.config_path, self.settings, self.master or b"")
        if self.ledger is not None:
            self.ledger.append("config", "changed", "operator",
                               {"setting": "human_gate", "before": before, "after": enabled,
                                "signed": True})
        return {"human_gate": enabled, "changed": before != enabled}

    # ---------- ledger ----------

    def _ledger_verified(self) -> Optional[bool]:
        """True/False, or None when there is nothing to check.

        Reports read the same JSONL the signatures cover, so a forged line used to inflate a total
        silently. The public key is not a secret, so this works while locked too.
        """
        try:
            if self.ledger is not None:
                ok, _reason, _n = self.ledger.verify()
                return bool(ok)
            pub = self.home / "ledger.jsonl.pub"
            if not pub.exists() or not self.ledger_path.exists():
                return None
            ok, _reason, count = verify_file(self.ledger_path, pub.read_text(encoding="utf-8").strip())
            if not ok:
                return False
            lines = [line for line in self.ledger_path.read_text(encoding="utf-8").splitlines()
                     if line.strip()]
            last = json.loads(lines[-1]) if lines else {}
            return bool(self._check_anchor_locked(count, last).get("ok", True))
        except Exception:                                    # noqa: BLE001 - never break a report
            return None

    def _receipt_entries(self):
        """Every receipt this wallet wrote, live chain plus archives, locked or not.

        Reporting needs signatures only to *verify*; summing and exporting read the same public
        JSONL. Reading the files directly while locked keeps `ledger --csv` and spend totals usable
        without the passphrase, exactly like `ledger_tail`.
        """
        if self.ledger is not None:
            yield from self.ledger.entries_with_archives()
            return
        for path in sorted(self.home.glob("ledger-*.jsonl")) + [self.ledger_path]:
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue

    def spend_totals(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """What has been spent, per asset and per payee, inside a window.

        Caps are enforced against these same approved receipts (`Ledger.totals`), so a report that
        says "1.2 XMR this month" is the number policy was already using — not a second ledger
        implementation that can disagree with it.
        """
        since = parse_window(params.get("since"))
        until = parse_window(params.get("until"))
        if since and until and since > until:
            raise WalletError("bad_window", "since is after until")
        if self._ledger_verified() is False:
            raise WalletError("bad_ledger",
                              "the receipt chain does not verify, so these totals cannot be trusted; "
                              "run `hermes wallet verify` and restore from a backup")
        sums: Dict[str, Decimal] = {}
        real: Dict[str, Decimal] = {}
        simulated: Dict[str, Decimal] = {}
        real_count: Dict[str, int] = {}
        simulated_count: Dict[str, int] = {}
        paid: Dict[tuple, Decimal] = {}
        counts: Dict[tuple, int] = {}
        fees: Dict[str, Decimal] = {}
        internal: Dict[str, Decimal] = {}
        assets_seen: List[str] = []
        for entry in self._receipt_entries():
            if entry.get("decision") != "approved":
                continue
            ts = str(entry.get("ts", ""))
            if since and ts < since:
                continue
            if until and ts > until:
                continue
            details = entry.get("details") or {}
            if details.get("settlement") == "x402" and details.get("x402_delivered") is not True:
                # an authorisation nobody settled is not spending; counting it inflated `real_spent`
                continue
            asset = str(details.get("asset") or "")
            if not asset:
                continue
            if details.get("funding_for") or details.get("sweep_of"):
                # money between our own accounts (funding a budget, sweeping it home, a merge): the
                # fee is real, the transfer is not spending, and adding it made a 0.02 sandbox look
                # like 0.02 spent on a payee
                try:
                    internal[asset] = internal.get(asset, Decimal("0")) + Decimal(
                        str(details.get("amount") or "0"))
                except (InvalidOperation, ArithmeticError, ValueError):
                    pass
                continue

            try:
                amount = Decimal(str(details.get("amount", "0")))
            except (InvalidOperation, ArithmeticError):
                continue
            payee = str(details.get("payee") or "")
            if asset not in sums:
                sums[asset] = Decimal("0")
                assets_seen.append(asset)
            sums[asset] += amount
            try:
                fees[asset] = fees.get(asset, Decimal("0")) + Decimal(str(details.get("fee") or "0"))
            except (InvalidOperation, ArithmeticError, ValueError):
                pass              # an unparseable fee is not a number to add; the chain check covers lies
            is_sim = str(details.get("kind") or "").startswith("sim")
            bucket = simulated if is_sim else real
            bucket[asset] = bucket.get(asset, Decimal("0")) + amount
            counter = simulated_count if is_sim else real_count
            counter[asset] = counter.get(asset, 0) + 1
            paid[(payee, asset)] = paid.get((payee, asset), Decimal("0")) + amount
            counts[(payee, asset)] = counts.get((payee, asset), 0) + 1

        assets: Dict[str, str] = {}
        by_asset: Dict[str, Dict[str, Any]] = {}
        for asset in assets_seen:
            if self.ledger is not None:
                total = self.ledger.totals(asset, since=since or None, until=until or None)
            else:
                total = sums[asset]
            assets[asset] = f"{total:f}"
            # `spent` stays the headline number; the split is what stops a demo payment on the
            # simulator from being reported as money that left the wallet.
            by_asset[asset] = {"spent": f"{total:f}",
                               "fees": f"{fees.get(asset, Decimal('0')):f}",
                               "total_out": f"{total + fees.get(asset, Decimal('0')):f}",
                               "count": sum(count for (payee, held), count in counts.items()
                                            if held == asset),
                               "real_spent": f"{real.get(asset, Decimal('0')):f}",
                               "real_count": real_count.get(asset, 0),
                               "simulated_spent": f"{simulated.get(asset, Decimal('0')):f}",
                               "simulated_count": simulated_count.get(asset, 0)}

        payees = []
        for key in paid:
            payee, asset = key
            if self.ledger is not None:
                spent = self.ledger.totals(asset, payee=payee, since=since or None,
                                           until=until or None)
            else:
                spent = paid[key]
            payees.append({"payee": payee, "asset": asset, "spent": f"{spent:f}",
                           "count": counts[key]})
        payees.sort(key=lambda row: (row["asset"], row["payee"]))
        # `assets` carries the per-asset object the Desktop pane reads (spent/count); `totals` is
        # the same number flat, for a terminal or a one-liner that does not want to dig.
        return {"assets": by_asset, "totals": assets, "payees": payees,
                "fees": {asset: f"{value:f}" for asset, value in fees.items()},
                "internal_transfers": {asset: f"{value:f}" for asset, value in internal.items()},
                "verified": self._ledger_verified(),
                "real_totals": {asset: f"{value:f}" for asset, value in real.items()},
                "simulated_totals": {asset: f"{value:f}" for asset, value in simulated.items()},
                "since": since, "until": until}

    def ledger_csv(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """The receipt chain as CSV: every entry, not just the newest page.

        `seq,ts,kind,decision,rule,asset,amount,fee,payee,tx_hash` — columns an accountant or a
        spreadsheet can diff against the signed JSONL. Amounts stay strings: a float column would
        round money. `fee` was missing, so a CSV total understated what left the wallet.
        """
        since = parse_window(params.get("since"))
        until = parse_window(params.get("until"))
        if since and until and since > until:
            raise WalletError("bad_window", "since is after until")
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(["seq", "ts", "kind", "decision", "rule",
                         "asset", "amount", "fee", "payee", "tx_hash"])
        rows = 0
        for entry in self._receipt_entries():
            ts = str(entry.get("ts", ""))
            if since and ts < since:
                continue
            if until and ts > until:
                continue
            details = entry.get("details") or {}
            writer.writerow([entry.get("seq", ""), ts, entry.get("kind", ""),
                             entry.get("decision", ""), entry.get("rule", ""),
                             details.get("asset", ""), details.get("amount", ""),
                             details.get("fee", ""), details.get("payee", ""),
                             details.get("tx_hash", "")])
            rows += 1
        return {"csv": buffer.getvalue(), "rows": rows, "since": since, "until": until,
                "verified": self._ledger_verified()}

    def ledger_tail(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Recent receipts. Readable while locked: they are public, signed data.

        Returning [] when locked made the pane's Receipts section look like the history had been
        erased, which is the one thing a hash-chained ledger exists to disprove.
        """
        limit = max(1, min(int(params.get("limit", 20) or 20), 500))
        if self.ledger is None:
            entries: List[Dict[str, Any]] = []
            try:
                for line in self.ledger_path.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        entries.append(json.loads(line))
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                return {"entries": [], "locked": True}
            return {"entries": entries[-limit:], "locked": True}
        return {"entries": self.ledger.tail(limit)}

    def _anchor_path(self) -> Path:
        return self.home / "ledger.anchor"

    def _attach_anchor(self) -> None:
        if self.ledger is not None:
            self.ledger.on_append = self._write_anchor
            self._write_anchor(self.ledger.last())

    def _record_anchor_verdict(self) -> None:
        """Read the anchor the previous process left, before this one overwrites it.

        Truncating the chain is the cheapest way to erase `authority_state` (which is how a stale
        `sandboxes.json` is caught), so a chain that no longer matches its anchor must not be taken
        at face value. Recourse stays available: the operator can re-anchor deliberately, and the
        money in a closed budget is still sweepable home.
        """
        self.ledger_rewound = ""
        if self.ledger is None:
            return
        rows = list(self.ledger.entries())
        verdict = self._check_anchor(len(rows), rows[-1] if rows else None)
        if not verdict.get("ok"):
            self.ledger_rewound = str(verdict.get("reason") or "the ledger does not match its anchor")

    def _write_anchor(self, entry: Optional[Dict[str, Any]]) -> None:
        """Pin the newest sequence + signature under an HMAC of the master key.

        The chain is verifiable with the public key alone, which means a *truncated* chain also
        verifies: deleting entries removes the evidence with them. The anchor is what makes
        "fewer receipts than yesterday" a failure instead of a clean pass.
        """
        if not entry or not self.master:
            return
        body = f"{entry['seq']}:{entry.get('sig', '')}".encode("utf-8")
        mac = hmac.new(self.master, body, hashlib.sha256).hexdigest()
        # atomic, like the keystore: a truncated anchor fails closed, but a crash should not turn a
        # healthy ledger into an unreadable one
        self._write_private_json(self._anchor_path(),
                                 {"seq": entry["seq"], "sig": entry.get("sig", ""), "mac": mac})

    def _check_anchor(self, count: int, last: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        path = self._anchor_path()
        if not path.exists():
            if count == 0:
                # initialize() always writes a "wallet created" receipt, so a wallet with no receipts
                # has had its chain deleted - the exact act the anchor exists to catch
                if self.exists():
                    return {"anchored": False, "ok": False,
                            "reason": "the ledger is empty but the wallet exists — receipts were deleted"}
                return {"anchored": False, "ok": True, "reason": "no receipts yet"}
            # deleting the anchor used to make truncation invisible: "no anchor" is now a failure
            return {"anchored": False, "ok": False,
                    "reason": "anchor file is missing — receipts may have been truncated"}
        try:
            anchor = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"anchored": True, "ok": False, "reason": "anchor is unreadable"}
        if self.master:
            expected = hmac.new(self.master, f"{anchor.get('seq')}:{anchor.get('sig', '')}".encode(),
                                hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, str(anchor.get("mac", ""))):
                return {"anchored": True, "ok": False, "reason": "anchor signature is invalid"}
        if count < int(anchor.get("seq", 0)) or (last or {}).get("sig") != anchor.get("sig"):
            return {"anchored": True, "ok": False,
                    "reason": f"ledger has {count} entries but {anchor.get('seq')} were recorded"}
        return {"anchored": True, "ok": True}

    def ledger_archive(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Clear the *view* of history without destroying the record.

        The receipts are a hash chain: deleting the file would also delete the evidence that anything
        happened, and the agent could do that to hide its tracks. So this moves the current chain to
        `ledger-<ts>.jsonl`, starts a new one, and writes the archive's last seq+signature into the
        first entry — the old chain stays on disk, still verifiable with the same public key.
        """
        self._require_unlocked()
        # No passphrase: this archives rather than deletes, so the record survives and there is
        # nothing an attacker gains by doing it. Asking for the secret here was friction pretending
        # to be security.
        if self.ledger is None:
            raise WalletError("locked", "unlock first")
        last = self.ledger.last()
        if last is None:
            return {"archived": None, "note": "there is nothing to archive"}

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        archive = self.home / f"ledger-{stamp}.jsonl"
        suffix = 1
        # second-resolution names collide; os.replace would then destroy the previous archive, which
        # is the one file this whole design exists to keep
        while archive.exists():
            suffix += 1
            archive = self.home / f"ledger-{stamp}-{suffix}.jsonl"
        try:
            os.replace(self.ledger_path, archive)
            os.chmod(archive, 0o600)
        except OSError as exc:
            raise WalletError("archive_failed", f"could not move the ledger: {exc}") from exc

        # a fresh chain with the same signing key; its first entry points at what came before
        self.ledger = Ledger(self.ledger_path, base64.b64decode(self.payload["ledger_seed"]))
        self._attach_anchor()
        self.ledger.append("ledger", "archived", "operator",
                           {"file": archive.name, "archived_through_seq": last["seq"],
                            "archived_through_sig": last.get("sig", ""),
                            "entries": last["seq"],
                            "note": "caps and totals are computed across archives as well"})
        self._write_anchor(self.ledger.last())
        return {"archived": archive.name, "through_seq": last["seq"],
                "entries_before": last["seq"], "entries_now": 1,
                "note": "the old chain is kept on disk and still verifies with the same public key"}

    def ledger_verify(self) -> Dict[str, Any]:
        """Verification needs only the public key, so it works while the wallet is locked."""
        if self.ledger is not None:
            ok, message, count = self.ledger.verify()
            anchor = self._check_anchor(count, self.ledger.last())
            if ok and anchor.get("ok") is False:
                ok, message = False, anchor.get("reason", "ledger does not match its anchor")
            return {"ok": ok, "message": message, "entries": count, "pubkey": self.ledger.pubkey_hex,
                    "anchor": anchor}
        pub_path = self.ledger_path.with_suffix(self.ledger_path.suffix + ".pub")
        if not pub_path.exists():
            raise WalletError("no_ledger", "no receipt ledger yet")
        pubkey = pub_path.read_text(encoding="ascii").strip()
        ok, message, count = verify_file(self.ledger_path, pubkey)
        last = None
        try:
            lines = [line for line in self.ledger_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            if lines:
                last = json.loads(lines[-1])
        except (OSError, json.JSONDecodeError):
            last = None
        anchor = self._check_anchor_locked(count, last)
        if ok and anchor.get("ok") is False:
            ok, message = False, anchor.get("reason", "ledger does not match its anchor")
        return {"ok": ok, "message": message, "entries": count, "pubkey": pubkey, "anchor": anchor}

    def _check_anchor_locked(self, count: int, last: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Anchor check without the master key: the HMAC cannot be verified, the sequence can."""
        path = self._anchor_path()
        if not path.exists():
            return ({"anchored": False, "ok": True, "reason": "no receipts yet"} if count == 0 else
                    {"anchored": False, "ok": False,
                     "reason": "anchor file is missing — receipts may have been truncated"})
        try:
            anchor = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"anchored": True, "ok": False, "reason": "anchor is unreadable"}
        if count < int(anchor.get("seq", 0)) or (last or {}).get("sig") != anchor.get("sig"):
            return {"anchored": True, "ok": False,
                    "reason": f"ledger has {count} entries but {anchor.get('seq')} were recorded"}
        return {"anchored": True, "ok": True, "note": "anchor signature not checked while locked"}


# ---------- dispatch table ----------

def build_methods() -> Dict[str, tuple[str, Callable[..., Dict[str, Any]]]]:
    """method -> (scope, callable). Anything not here is not reachable."""
    return {
        "status": (AGENT, lambda s, p: s.status()),
        "chain_status": (AGENT, lambda s, p: s.chain_status()),
        "balances": (AGENT, lambda s, p: s.balances(fresh=bool(p.get("fresh")))),
        # Operator scope: it names the refill address the operator keeps outside this wallet, and a
        # gas shortfall is already reported to the agent by the `gas_floor` refusal it gets back.
        "gas": (OPERATOR, lambda s, p: s.gas_report()),
        # The agent may *ask* about a conversion (a payment it cannot make is usually a swap away)
        # and file one, but filing only ever produces a card: the PIN is what converts.
        "swap_quote": (AGENT, lambda s, p: s.swap_quote(p)),
        "swap_routers": (AGENT, lambda s, p: s.swap_routers(p)),
        "swap_request": (AGENT, lambda s, p: s.swap_request(p, from_agent=p.get("_scope") == AGENT)),
        "nodes": (AGENT, lambda s, p: s.nodes()),
        "config_get": (AGENT, lambda s, p: s.config_get()),
        "requests_list": (AGENT, lambda s, p: s.requests_list()),
        "request_tx": (AGENT, lambda s, p: s.request_tx(p, unattended=bool(p.get("unattended")))),
        "mandates_propose": (AGENT, lambda s, p: s.mandates_propose(p)),
        "mandates_list": (AGENT, lambda s, p: s.mandates_list()),
        "mandates_revoke": (AGENT, lambda s, p: s.mandates_revoke(p)),
        "ledger_tail": (AGENT, lambda s, p: s.ledger_tail(p)),
        "ledger_archive": (OPERATOR, lambda s, p: s.ledger_archive(p)),
        "request_operator_prompt": (AGENT, lambda s, p: s.request_operator_prompt(p)),
        "clear_operator_prompt": (OPERATOR, lambda s, p: s.clear_operator_prompt()),
        "ledger_verify": (AGENT, lambda s, p: s.ledger_verify()),
        "unlock": (OPERATOR, lambda s, p: s.unlock_pin(str(p.get("pin"))) if p.get("pin")
                   else s.unlock(str(p.get("passphrase", "")))),
        "lock": (OPERATOR, lambda s, p: s.lock()),
        "approve": (OPERATOR, lambda s, p: s.approve(p)),
        "deny": (OPERATOR, lambda s, p: s.deny(p)),
        "mandates_activate": (OPERATOR, lambda s, p: s.mandates_activate(p)),
        "set_human_gate": (OPERATOR, lambda s, p: s.set_human_gate(p)),
        "config_set": (OPERATOR, lambda s, p: s.config_set(p)),
        "export_seed": (OPERATOR, lambda s, p: s.export_seed(p)),
        "merchants": (AGENT, lambda s, p: s.merchants(p)),
        "signer_status": (AGENT, lambda s, p: s.signer_status()),
        "signer_repair": (OPERATOR, lambda s, p: s.signer_repair(p)),
        "mandates_create": (OPERATOR, lambda s, p: s.mandates_create(p)),
        "mandates_update": (OPERATOR, lambda s, p: s.mandates_update(p)),
        "mandates_set_enabled": (OPERATOR, lambda s, p: s.mandates_set_enabled(p)),
        "mandates_set_unattended": (OPERATOR, lambda s, p: s.mandates_set_unattended(p)),
        "mandates_delete": (OPERATOR, lambda s, p: s.mandates_delete(p)),
        "doctor": (AGENT, lambda s, p: s.doctor()),
        "create_keystore": (OPERATOR, lambda s, p: s.create_keystore(p)),
        "pay_url": (AGENT, lambda s, p: s.pay_url(p)),
        "receive_address": (AGENT, lambda s, p: s.receive_address(p)),
        "incoming": (AGENT, lambda s, p: s.incoming()),
        "orders_close": (AGENT, lambda s, p: s.orders_close(p)),
        "cancel_request": (AGENT, lambda s, p: s.cancel_request(p)),
        "spend_totals": (AGENT, lambda s, p: s.spend_totals(p)),
        "ledger_csv": (AGENT, lambda s, p: s.ledger_csv(p)),
        "verify_payment": (AGENT, lambda s, p: s.verify_payment(p)),
        "signer_install": (OPERATOR, lambda s, p: s.signer_install(p)),
        "signer_start": (OPERATOR, lambda s, p: s.signer_start(p)),
        "signer_stop": (OPERATOR, lambda s, p: s.signer_stop(p)),
        "wallet_init": (OPERATOR, lambda s, p: s.wallet_init(p)),
        "wallet_import": (OPERATOR, lambda s, p: s.wallet_import(p)),
        "contacts_list": (AGENT, lambda s, p: s.contacts_list()),
        "contact_save": (AGENT, lambda s, p: s.contact_save(p)),
        "contact_delete": (OPERATOR, lambda s, p: s.contact_delete(p)),
        "sandboxes_list": (AGENT, lambda s, p: s.sandboxes_list()),
        "sandbox_create": (OPERATOR, lambda s, p: s.sandbox_create(p)),
        "sandbox_propose": (AGENT, lambda s, p: s.sandbox_propose(p)),
        "readiness": (AGENT, lambda s, p: s.readiness(p)),
        "sandbox_revoke": (OPERATOR, lambda s, p: s.sandbox_revoke(p)),
        # deposit addresses: an agent may *see* them and their balance; creating, arming or merging is
        # the operator's — a merge moves money, even if it moves it home
        "deposits_list": (AGENT, lambda s, p: s.deposits_list()),
        "deposit_create": (OPERATOR, lambda s, p: s.deposit_create(p)),
        "deposit_set_auto": (OPERATOR, lambda s, p: s.deposit_set_auto(p)),
        "deposit_merge": (OPERATOR, lambda s, p: s.deposit_merge(p)),
        "deposits_auto_merge": (OPERATOR, lambda s, p: {"merged": s.deposits_auto_merge()}),
        # money paid into an order's account: the operator can sweep it now, the reaper does it on a timer
        "orders_merge": (OPERATOR, lambda s, p: {"merged": s.orders_merge()}),
        # the reaper is also a timer in the daemon; this is the operator's "do it now"
        # a dict like every other reply: the bare list broke the pane's route as soon as it was
        # not empty (the dashboard declares an object, and the client reads `[] or {}`)
        "reap_sandboxes": (OPERATOR, lambda s, p: {"reaped": s.reap_sandboxes()}),
        "sandbox_set_profiles": (OPERATOR, lambda s, p: s.sandbox_set_profiles(p)),
        "sandbox_set_payees": (OPERATOR, lambda s, p: s.sandbox_set_payees(p)),
        "sandbox_arm": (OPERATOR, lambda s, p: s.sandbox_arm(p)),
        # handing over a budget is the operator's act; asking for more money is the agent's
        "sandbox_set_unattended": (OPERATOR, lambda s, p: s.sandbox_set_unattended(p)),
        "sandbox_topup": (AGENT, lambda s, p: s.sandbox_topup(p)),
        # the PIN: readable state for the agent (it may tell the user a PIN is needed), settable only
        # by an operator who also types the passphrase
        "pin_status": (AGENT, lambda s, p: s.pin_status()),
        "set_pin": (OPERATOR, lambda s, p: s.set_pin(p)),
        "change_passphrase": (OPERATOR, lambda s, p: s.change_passphrase(p)),
        "sandbox_destroy": (OPERATOR, lambda s, p: s.sandbox_destroy(p)),
        # forgetting a closed budget is operator housekeeping: it removes a row, never money
        "sandbox_purge": (OPERATOR, lambda s, p: s.sandbox_purge(p)),
        "requests_purge": (OPERATOR, lambda s, p: s.requests_purge(p)),
        "orders_purge": (OPERATOR, lambda s, p: s.orders_purge(p)),
        "sandboxes_reap": (OPERATOR, lambda s, p: {"reaped": s.reap_sandboxes()}),
        "sandbox_sweep": (OPERATOR, lambda s, p: s.sandbox_sweep(p)),
    }
