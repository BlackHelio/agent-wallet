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
import secrets
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import os
import threading
import urllib.request

from . import config, keystore
from . import merchants as merchant_book
from . import pay402
from . import signer as signer_mod
from .chains import nodes as node_registry
from .chains.base import ChainAdapter, ChainError
from .chains.bitcoin import BtcAdapter
from .chains.xrp import XrpAdapter
from .chains.evm import EvmAdapter
from .chains.monero import XmrAdapter
from .ledger import Ledger, _now, verify_file
from .policy import Decision, Mandate, SpendContext, SpendRequest, evaluate, now_iso, parse_amount, payee_key
from .tokens import AGENT, OPERATOR

VERSION = "0.2.0"


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

    def to_dict(self) -> Dict[str, Any]:
        return {"contact_id": self.contact_id, "name": self.name, "addresses": dict(self.addresses),
                "notes": self.notes, "created_at": self.created_at, "updated_at": self.updated_at}


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
    enabled: bool = False          # becomes true when the funding transfer is broadcast
    # A one-time sandbox exists for exactly one agent payment and then destroys itself. The clock
    # starts when that payment settles, never at creation, so a slow funding approval cannot eat
    # the window.
    mode: str = "budget"           # budget | one_time
    self_destruct_seconds: int = 1800
    spent_at: str = ""
    destroy_at: str = ""
    destroyed_at: str = ""
    destroy_blocked: str = ""      # why the destruction is waiting (never destroyed while it holds funds)
    last_balance: str = ""

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
        return {"sandbox_id": self.sandbox_id, "kind": self.kind, "asset": self.asset,
                "account_index": self.account_index, "address": self.address,
                "budget": str(self.budget), "spent": str(self.spent),
                "remaining": str(self.remaining()), "created_at": self.created_at,
                "expires_at": self.expires_at, "label": self.label,
                "allowed_profiles": list(self.allowed_profiles),
                "enabled": self.enabled, "status": self.status(),
                "mode": self.mode, "self_destruct_seconds": self.self_destruct_seconds,
                "spent_at": self.spent_at, "destroy_at": self.destroy_at,
                "destroyed_at": self.destroyed_at, "destroy_blocked": self.destroy_blocked,
                "last_balance": self.last_balance, "destroy_in": self.destroy_in()}


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
        self.contacts_path = self.home / "contacts.json"
        self.mandates_path = self.home / "mandates.json"
        self.requests_path = self.home / "requests.json"
        self.idempotency_path = self.home / "idempotency.json"
        # key -> request_id. Money movement is the one place where "did my retry already pay?" has
        # to be answerable across a crash, so this is persisted, not in-memory.
        self.idempotency: Dict[str, str] = {}
        self.contacts: Dict[str, Contact] = {}
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
        self.unlocked_at: Optional[datetime] = None
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
        # Load the public state at startup, not only on unlock. Sandboxes, contacts, mandates and
        # pending requests are plain JSON on disk (no key material), and a locked wallet that reports
        # "no contacts, no sandboxes, no pending requests" reads as "everything was deleted".
        self._load_sandboxes()
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

    def initialize(self, passphrase: str) -> Dict[str, Any]:
        if self.exists():
            raise WalletError("already_initialized", "keystore already exists")
        if not passphrase or len(passphrase) < 8:
            raise WalletError("weak_passphrase", "passphrase must be at least 8 characters")
        payload = keystore.create(self.keystore_path, passphrase)
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
        self.ledger.append("wallet", "created", "operator", {"human_gate": True})
        return {"ok": True, "pubkey": self.ledger.pubkey_hex}

    def unlock(self, passphrase: str) -> Dict[str, Any]:
        try:
            envelope = keystore.load(self.keystore_path)
            payload = keystore.open_sealed(envelope, passphrase)
        except keystore.InvalidPassphrase as exc:
            raise WalletError("bad_passphrase", "passphrase does not match the keystore") from exc
        except keystore.NewerKeystore as exc:
            raise WalletError("keystore_too_new", str(exc)) from exc
        except keystore.CorruptKeystore as exc:
            raise WalletError("corrupt_keystore", "keystore is missing or unreadable") from exc
        self.payload = self._ensure_chain_keys(payload, passphrase)
        self.master = base64.b64decode(payload["master"])
        self.ledger = Ledger(self.ledger_path, base64.b64decode(payload["ledger_seed"]))
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
        self._load_contacts()
        self._load_mandates()
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
        self.ledger.append("wallet", "unlocked", "operator",
                           {"config_signed": result.signed, "config_tampered": result.tampered})
        return {"ok": True, "human_gate": self.settings.human_gate, "config_tampered": result.tampered}

    def _load_contacts(self) -> None:
        self.contacts = {}
        try:
            raw = json.loads(self.contacts_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
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
                )
            except (KeyError, TypeError):
                continue

    def _save_contacts(self) -> None:
        payload = {"contacts": [c.to_dict() for c in self.contacts.values()]}
        tmp = self.contacts_path.with_name(self.contacts_path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
        os.replace(tmp, self.contacts_path)
        os.chmod(self.contacts_path, 0o600)

    @staticmethod
    def _contact_key(name: str) -> str:
        return " ".join(str(name or "").strip().lower().split())

    def _load_sandboxes(self) -> None:
        """Sandboxes are real on-chain accounts: survive a restart or they are lost money."""
        self.sandboxes = {}
        try:
            raw = json.loads(self.sandboxes_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
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
                    enabled=bool(row.get("enabled", False)),
                    mode=str(row.get("mode", "budget")),
                    self_destruct_seconds=int(row.get("self_destruct_seconds", 1800) or 1800),
                    spent_at=str(row.get("spent_at", "")),
                    destroy_at=str(row.get("destroy_at", "")),
                    destroyed_at=str(row.get("destroyed_at", "")),
                    destroy_blocked=str(row.get("destroy_blocked", "")),
                    last_balance=str(row.get("last_balance", "")),
                )
            except (KeyError, ValueError):
                continue

    def _save_sandboxes(self) -> None:
        payload = {"sandboxes": [s.to_dict() for s in self.sandboxes.values()]}
        tmp = self.sandboxes_path.with_name(self.sandboxes_path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
        os.replace(tmp, self.sandboxes_path)
        os.chmod(self.sandboxes_path, 0o600)

    def _build_adapters(self) -> None:
        """Construct a live adapter per enabled chain. No adapter may read the keystore."""
        self.adapters = {}
        self.chain_notes = {}
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
                                     rpc_user=creds["user"], rpc_password=creds["password"])
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
                                     rpc_user=creds["user"], rpc_password=creds["password"])
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

    def balances(self, ttl_seconds: float = 10.0) -> Dict[str, Any]:
        """Per-chain address + balance, from the live adapter only (never from a cached number).

        When the wallet is locked there is nothing to read, but saying "no chain is reachable" is a
        lie: the chains are fine, the signer just cannot decrypt yet. Each row carries a `state` so
        the UI can say the true thing.
        """
        cached_row = self._balances_cache
        if cached_row is not None and (datetime.now(timezone.utc) - cached_row[0]).total_seconds() < ttl_seconds:
            return cached_row[1]
        if not self.is_unlocked():
            cached = self._cached_addresses()
            rows = []
            for kind in self.settings.chains_enabled:
                for asset in self._chain_assets(kind):
                    row = {"kind": kind, "asset": asset, "balance": "0",
                           "available": False, "state": "locked", "error": "locked"}
                    if kind in cached:
                        row["address"] = cached[kind]  # public data: funding works while locked
                    rows.append(row)
            payload = {"accounts": rows, "state": "locked", "addresses": cached}
            self._balances_cache = (datetime.now(timezone.utc), payload)
            return payload

        self._heal_signer()
        rows = []
        for kind, adapter in self.adapters.items():
            base: Dict[str, Any] = {"kind": kind}
            try:
                provider = adapter.status().get("provider")
                if provider:
                    base["provider"] = provider
                base["address"] = adapter.address()
                base["available"] = True
                # EVM chains hold several assets at one address; Monero/Bitcoin hold one.
                if hasattr(adapter, "balance_all"):
                    for asset, amount in adapter.balance_all().items():
                        # an EVM chain holds every token it knows; only the enabled ones are this
                        # wallet's coins, or adding ETH silently adds USDC to the page as well
                        if asset not in self.settings.assets_enabled:
                            continue
                        rows.append({**base, "asset": asset, "balance": f"{amount:f}"})
                    continue
                base["asset"] = adapter.asset
                base["balance"] = f"{adapter.balance():f}"
            except Exception as exc:                      # noqa: BLE001 - a dead chain must not break the page
                base["available"] = False
                base["error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
                # No balance at all: 0.0000 reads as "this wallet is empty", which is a different
                # (and wrong) statement from "we could not ask".
                base["balance"] = None
                base.setdefault("asset", self._chain_assets(kind)[0])
            rows.append(base)
        for kind, note in getattr(self, "chain_notes", {}).items():
            rows.append({"kind": kind, "asset": self._chain_assets(kind)[0], "available": False,
                         "balance": "0", "state": "signer", "error": note})
        payload = {"accounts": rows}
        self._balances_cache = (datetime.now(timezone.utc), payload)
        return payload

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

    def chain_status(self) -> Dict[str, Any]:
        self._heal_signer()
        chains = [a.status() for a in self.adapters.values()]
        for kind, note in getattr(self, "chain_notes", {}).items():
            chains.append({"kind": kind, "available": False, "error": note})
        return {"chains": chains}

    def lock(self) -> Dict[str, Any]:
        self._invalidate_balances()
        """Locking must actually take the keys away, not just forget the keystore.

        The XMR spend key lives inside `monero-wallet-rpc`, and the BTC/EVM keys sit in the adapters
        we built while unlocked. Clearing `master` alone left all three usable, while the CLI said
        "drop the key from memory".
        """
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

    def status(self) -> Dict[str, Any]:
        return {
            "version": VERSION,
            "locked": not self.is_unlocked(),
            "initialized": self.exists(),
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
            "payee_allowlist": list(self.settings.payee_allowlist),
            "unattended_policy": self.settings.unattended_policy,
            "approval_ttl_seconds": self.settings.approval_ttl_seconds,
            "autolock_minutes": int(getattr(self.settings, "autolock_minutes", 0) or 0),
            "unlock_expires_in": self.unlock_expires_in(),
            "signer_installed": signer_mod.binary_path(self.home).exists(),
            "signer_reachable": any(k.startswith(("monero", "sim-")) for k in self.adapters),
            "ledger_archives": len(list(self.home.glob("ledger-*.jsonl"))),
            "ledger_pubkey": self.ledger.pubkey_hex if self.ledger else None,
            "pending_requests": sum(1 for r in self.requests.values() if r.status == "pending"),
            "prompt": dict(self.prompt) if self.prompt else None,
            "active_mandates": sum(1 for m in self.mandates.values() if m.status() == "active"),
            # The profile list rides on /status on purpose: the dashboard imports a plugin's routes
            # once at startup, so a pane talking to an older backend cannot reach a newly added
            # route at all - but /status has existed since the first version.
            "profiles": self.profiles_list()["profiles"],
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
        self.prompt = {
            "kind": "approval" if req is not None else "unlock",
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

    def _context(self, req: SpendRequest, mandate: Optional[Mandate]) -> SpendContext:
        if self.ledger is None:
            return SpendContext(unattended=True)
        now = datetime.now(timezone.utc)
        day_start = now.strftime("%Y-%m-%dT00:00:00Z")
        if mandate is not None:
            window_start = (now - timedelta(hours=mandate.period_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
            period = self.ledger.totals(req.asset, kind=req.kind, since=window_start, payee=req.payee)
            lifetime = self.ledger.totals(req.asset, kind=req.kind, payee=req.payee)
        else:
            period = self.ledger.totals(req.asset, kind=req.kind)
            lifetime = period
        daily = self.ledger.totals(req.asset, kind=req.kind, since=day_start)
        return SpendContext(unattended=False, period_spent=period, daily_spent=daily, lifetime_spent=lifetime)

    def request_tx(self, params: Dict[str, Any], unattended: bool = False) -> Dict[str, Any]:
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
                    "decision": {"allow": False, "rule": "idempotent", "code": "duplicate",
                                 "reason": "this idempotency key already produced a request"},
                    "duplicate": True, "idempotency_key": key,
                    "note": "the original request is no longer held in memory; its receipt is in the "
                            "ledger. Ask for it again only if you intend a new payment.",
                }
            if existing is not None:
                # A retry after a timeout or a crash must not become a second payment. Return the
                # request that already exists, with its live approval if it is still pending.
                row = existing.to_dict()
                approval = self._live_approval(existing.request_id)
                payload: Dict[str, Any] = {
                    "request": row,
                    "decision": {"allow": existing.status == "pending", "rule": "idempotent",
                                 "code": "duplicate", "reason": f"request already exists ({existing.status})"},
                    "duplicate": True,
                    "idempotency_key": key,
                }
                if approval is not None:
                    payload["approval_id"] = approval.approval_id
                    payload["approval_expires_at"] = approval.expires_at.strftime("%Y-%m-%dT%H:%M:%SZ")
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
        try:
            amount = parse_amount(params.get("amount"), asset)
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
        resolved, contact_name = self.resolve_payee(kind, str(params.get("payee", "")))
        try:
            payee = payee_key(kind, resolved)
        except ValueError:
            # Keep the raw string so policy can deny it *and* the ledger records the attempt.
            payee = str(params.get("payee", "")).strip()[:200]
            contact_name = ""
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
            challenge=dict(params.get("challenge") or {}),
            idempotency_key=key,
            profile=str(params.get("profile", "")),
        )

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
        ctx = self._context(req, mandate)
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
                   "idempotency_key": req.idempotency_key}
        if not decision.allow:
            req.status = "denied"
            self._save_requests()
            if self.ledger is not None:
                self.ledger.append("spend_request", "denied", decision.rule,
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
                    account = self.sandboxes[req.sandbox_id].account_index
                # A priced 402 purchase settles differently: we sign an off-chain authorization
                # that the facilitator submits (and pays the gas for), instead of broadcasting a
                # transfer ourselves.
                if req.challenge and hasattr(adapter, "plan_authorization") and req.challenge.get("scheme") == "exact":
                    plan = adapter.plan_authorization(req.amount, req.payee, req.asset,
                                                      resource=req.challenge.get("resource", ""))
                else:
                    plan = adapter.plan_transfer(req.amount, req.payee, account_index=account,
                                                 asset=req.asset)
                cap = Decimal(str(self.settings.fee_cap.get(req.asset, "0") or "0"))
                if cap > 0 and plan.fee > cap and not plan.raw.get("x402"):
                    raise ChainError("fee_too_high",
                                     f"network fee {plan.fee} {req.asset} exceeds the {cap} cap")
                self.plans[req.request_id] = plan
                details["fee"] = str(plan.fee)
            except ChainError as exc:
                retryable = exc.code in ("rpc_unreachable", "daemon_unavailable", "rpc_error",
                                         "signer_unreachable")
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
                req.status = "failed" if exc.code not in ("locked",) else "pending"
                self._save_requests()
                if self.ledger is not None:
                    self.ledger.append("spend_request", "denied", "broadcast",
                                       {"request_id": req.request_id, "kind": req.kind,
                                        "asset": req.asset, "amount": str(req.amount),
                                        "code": exc.code, "reason": exc.message[:200]})
                raise
            if self.ledger is not None:
                self.ledger.append("spend_request", "approved", "mandate",
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
            "needs": "operator approval",
        }

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
            try:
                result = adapter.broadcast(plan, fee_cap=cap)
            except TypeError:      # an adapter that predates the cap parameter
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
            if not expected or req.payee != expected or req.amount != sandbox.budget:
                # A transfer to somebody else is not funding: arming the sandbox on one would let the
                # agent open a no-approval budget with an unrelated (or attacker) payment.
                if self.ledger is not None:
                    self.ledger.append("sandbox", "funding_ignored", "policy",
                                       {"sandbox_id": sandbox.sandbox_id, "payee": req.payee,
                                        "expected_payee": expected, "amount": str(req.amount),
                                        "expected_amount": str(sandbox.budget)})
                sandbox = None          # not funding: keep going (a sweep can still apply)
            if sandbox is not None:
                sandbox.enabled = True
                self._save_sandboxes()
                if self.ledger is not None:
                    self.ledger.append("sandbox", "funded", "operator",
                                       {"sandbox_id": sandbox.sandbox_id, "tx_hash": req.tx_hash,
                                        "budget": str(sandbox.budget), "asset": sandbox.asset})
        if req.sweep_of and req.sweep_of in self.sandboxes:
            swept = self.sandboxes[req.sweep_of]
            reserve = Decimal(str(self.settings.fee_cap.get(swept.asset, "0") or "0"))
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
            sandbox.spent += req.amount
            self._save_sandboxes()
            self._arm_self_destruct(sandbox)
        explorer_tx = getattr(adapter, "explorer_tx", "") if adapter is not None else ""
        execution = {"signed": True, **result}
        if explorer_tx and result.get("tx_hash"):
            execution["explorer"] = explorer_tx.replace("{tx}", result["tx_hash"])
        return execution

    # Fields that describe the payment versus fields that describe what happened to it. The digest a
    # human approves is over the first kind only: adding "authorised_by" once (a legitimate state
    # change) must not make the approval look like a different payment.
    _DIGEST_EXCLUDED = ("status", "tx_hash", "authorised_by")

    def _digest(self, req: SpendRequest) -> str:
        body = req.to_dict()
        for key in self._DIGEST_EXCLUDED:
            body.pop(key, None)
        raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def _issue_approval(self, req: SpendRequest) -> Approval:
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
        self._require_unlocked()
        approval_id = str(params.get("approval_id", ""))
        request_id = str(params.get("request_id", ""))
        approval, req = self._peek_approval(approval_id, request_id)

        # Re-evaluate at approval time: caps are checked when a request is *made*, so an agent that
        # queues several pendings would otherwise multiply the daily cap (3 × 0.4 with a 1.0 cap).
        fresh = evaluate(req, self.settings, self._context(req, None), None)
        if not fresh.allow:
            approval.used = True
            req.status = "denied"
            if self.ledger is not None:
                self.ledger.append("spend_request", "denied", fresh.rule,
                                   {"request_id": req.request_id, "kind": req.kind, "asset": req.asset,
                                    "amount": str(req.amount), "payee": req.payee,
                                    "code": fresh.code, "reason": fresh.reason})
            raise WalletError(fresh.code, fresh.reason)

        if self.settings.human_gate:
            # A manual payment is authorised by the human, not by an unlocked process: the agent can
            # read the operator token off disk, so the passphrase is the real boundary.
            try:
                keystore.open_sealed(keystore.load(self.keystore_path), str(params.get("passphrase", "")))
            except keystore.KeystoreError as exc:
                raise WalletError("passphrase_required",
                                  "approving a payment needs the wallet passphrase") from exc
        confirm_amount = params.get("confirm_amount")
        if self.settings.human_gate and req.amount > self.settings.approval_threshold:
            typed = Decimal(str(confirm_amount).strip()) if confirm_amount not in (None, "") else None
            if typed is None or typed != req.amount:
                raise WalletError("confirm_amount_required",
                                  f"type the amount ({req.amount}) to approve this payment")
        # every check passed: record that a human authorised this specific request. `_broadcast`
        # refuses to sign anything without it, and nothing else in the daemon can set it.
        req.authorised_by = "passphrase"
        approval.used = True
        self.prompt = None
        if req.idempotency_key:
            self._remember_idempotency(req.idempotency_key, req.request_id, "approved")
        if self.ledger is not None:
            self.ledger.append("spend_request", "broadcasting", "manual_approval",
                               {"request_id": req.request_id, "kind": req.kind, "asset": req.asset,
                                "amount": str(req.amount), "payee": req.payee})
        adapter = self.adapters.get(req.kind)
        if adapter is None:
            # Approving would otherwise record "approved" while no transaction exists — the receipt
            # would claim a payment the chain never saw.
            req.status = "denied"
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
                    account = self.sandboxes[req.sandbox_id].account_index
                if req.challenge and hasattr(adapter, "plan_authorization"):
                    plan = adapter.plan_authorization(req.amount, req.payee, req.asset,
                                                      resource=req.challenge.get("resource", ""))
                else:
                    plan = adapter.plan_transfer(req.amount, req.payee, account_index=account,
                                                 asset=req.asset)
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
        execution = self._broadcast(req, plan, adapter)     # raises WalletError on chain failure
        if execution.get("settlement") == "x402":
            target = str((req.challenge or {}).get("url") or plan.raw.get("resource") or "")
            if target:
                execution["x402_response"] = pay402.submit(target, plan.raw["x402"],
                                                           timeout=float(self.settings.http_timeout_seconds))
        if self.ledger is not None:
            details = {"request_id": req.request_id, "kind": req.kind, "asset": req.asset,
                       "amount": str(req.amount), "payee": req.payee,
                       "code": "approved_by_operator", "reason": "operator approved",
                       **execution}
            self.ledger.append("spend_request", "approved", "manual_approval", details)
        return {"request": req.to_dict(), **execution}

    def deny(self, params: Dict[str, Any]) -> Dict[str, Any]:
        # No unlock requirement: refusing a payment removes authority, and auto-lock can fire while
        # the operator is reading the approval card.
        req = self._consume_approval(str(params.get("approval_id", "")), str(params.get("request_id", "")))
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
            "why": str(params.get("why") or f"402 purchase at {url}")[:300],
            "sandbox_id": str(params.get("sandbox_id", "")),
            "profile": str(params.get("profile", "")),
            "challenge_sha256": quote.digest,
            "challenge": {**quote.raw, "resource": quote.resource, "network": quote.network,
                          "url": url, "scheme": quote.scheme},
            "idempotency_key": str(params.get("idempotency_key") or "")[:64],
        }, unattended=unattended)
        return {"quote": quote.to_dict(), **request}

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
            return {"kind": kind, "address": adapter.address(), "account_index": 0,
                    "reused": True, "note": "this chain has no subaddresses; the wallet address is reused"}

        label = str(params.get("label") or params.get("memo") or "payment")[:40]
        expected = str(params.get("expected") or params.get("amount") or "").strip()
        if expected:
            try:
                if Decimal(expected) <= 0:
                    raise InvalidOperation
            except (InvalidOperation, ArithmeticError):
                raise WalletError("bad_amount", f"expected {expected!r} is not a positive amount") from None
            expected = f"{Decimal(expected):f}"
        created = adapter.create_account(label)
        if self.ledger is not None:
            self.ledger.append("receive_address", "created", "agent",
                               {"kind": kind, "account_index": created["account_index"],
                                "address": created["address"], "label": label,
                                "expected": expected})
        return {"kind": kind, "address": created["address"],
                "account_index": created["account_index"], "label": label,
                "expected": expected, "reused": False}

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

        notes: List[str] = []
        orders: List[Dict[str, Any]] = []
        accounts_by_kind: Dict[str, Dict[int, Dict[str, Any]]] = {}
        unsupported: set = set()
        for _ts, details in sorted(opens, key=lambda row: row[0]):
            kind = str(details.get("kind") or "")
            address = str(details.get("address") or "")
            try:
                index = int(details.get("account_index") or 0)
            except (TypeError, ValueError):
                index = 0
            adapter = self.adapters.get(kind)
            if adapter is None or not getattr(adapter, "supports_accounts", False):
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
            orders.append({
                "label": str(details.get("label") or ""),
                "address": address or str(account.get("address", "")),
                "account_index": index,
                "kind": kind,
                "asset": self.NATIVE_ASSET.get(kind) or getattr(adapter, "asset", ""),
                "expected": expected,
                "received": f"{held:f}",
                "status": status,
                "last_tx": last_tx.get(address, ""),
            })

        if unsupported:
            notes.append("no subaddresses on " + ", ".join(sorted(unsupported))
                         + "; that chain reuses the wallet address")
        if not orders and not unsupported and not any(
                getattr(a, "supports_accounts", False) for a in self.adapters.values()):
            notes.append("no enabled chain has subaddresses; the wallet address is reused")
        return {"orders": orders, "note": "; ".join(notes)}

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
                    authorised_by=str(row.get("authorised_by", "")))
            except (KeyError, ArithmeticError, ValueError):
                continue
        # Approval tokens are in memory only, so a request loaded without one can never be approved.
        # Doing this at load time (not only in unlock) means no path can resurrect a dead request.
        self._expire_unapprovable_requests()
        # keep the file bounded: 200 most recent, and drop anything older than a week
        if len(self.requests) > 200:
            ordered = sorted(self.requests.values(), key=lambda r: r.created_at)
            for stale in ordered[:-200]:
                self.requests.pop(stale.request_id, None)

    def _save_requests(self) -> None:
        rows = [{"request_id": r.request_id, "kind": r.kind, "asset": r.asset,
                 "amount": str(r.amount), "payee": r.payee, "memo": r.memo, "why": r.why,
                 "sandbox_id": r.sandbox_id, "profile": r.profile, "funding_for": r.funding_for,
                 "sweep_of": r.sweep_of, "challenge_sha256": r.challenge_sha256,
                 "challenge": dict(r.challenge), "idempotency_key": r.idempotency_key,
                 "created_at": r.created_at, "status": r.status, "tx_hash": r.tx_hash}
                for r in self.requests.values()]
        fd = os.open(self.requests_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"requests": rows}, handle, indent=2, sort_keys=True)

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
        fd = os.open(self.idempotency_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"keys": self.idempotency}, handle, indent=2, sort_keys=True)

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
                    label=str(row.get("label", "")))
            except (KeyError, ValueError, ArithmeticError):
                continue   # a malformed row must not take the rest of the file with it

    def _save_mandates(self) -> None:
        rows = [m.to_dict() for m in self.mandates.values()]
        path = self.mandates_path
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"mandates": rows}, handle, indent=2, sort_keys=True)

    def mandates_propose(self, params: Dict[str, Any]) -> Dict[str, Any]:
        # The agent may propose, never replace: a caller-supplied mandate_id used to overwrite an
        # active autopay's record (its caps and history) with an inert proposal - a delete that
        # `mandates_delete` guards with the passphrase.
        existing_id = str(params.get("mandate_id") or "")
        if existing_id and existing_id in self.mandates:
            raise WalletError("mandate_exists",
                              "that autopay already exists; propose a new one with a different id")
        params = {**params, "mandate_id": ""}
        try:
            mandate = Mandate.parse(params, self.settings)
        except ValueError as exc:
            raise WalletError("bad_mandate", str(exc)) from exc
        mandate.enabled = False                     # proposals are inert until an operator activates
        mandate.created_by = "agent"
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
            mandate = Mandate.parse(params, self.settings)
        except ValueError as exc:
            raise WalletError("bad_mandate", str(exc)) from exc
        mandate.enabled = bool(params.get("activate", True))
        if mandate.enabled and mandate.expired():
            raise WalletError("mandate_expired", "that expiry is already in the past")
        mandate.created_by = "operator"
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
        candidate.enabled = (bool(params["activate"]) if "activate" in params else mandate.enabled)
        if candidate.enabled and candidate.expired():
            raise WalletError("mandate_expired", "that expiry is already in the past")
        self.mandates[mandate_id] = candidate
        self._save_mandates()
        if self.ledger is not None:
            self.ledger.append("mandate", "updated", "operator",
                               {"before": mandate.to_dict(), "after": candidate.to_dict()})
        return {"mandate": candidate.to_dict()}

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
        if mandate.expired():
            raise WalletError("mandate_expired", "mandate has expired")
        mandate.enabled = True
        mandate.created_by = "operator"
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
                try:
                    period = self.ledger.totals(m.asset, kind=m.kind, since=since, payee=m.payee)
                    lifetime = self.ledger.totals(m.asset, kind=m.kind, payee=m.payee)
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

    def doctor(self) -> Dict[str, Any]:
        """One call that answers 'is this wallet healthy?' without touching funds."""
        from . import config as config_mod

        signer = signer_mod.status(self.home)
        loaded = config_mod.load(self.config_path, self.master)
        from .home import stray_wallets
        strays = stray_wallets(self.home)
        checks = [
            {"check": "keystore", "ok": self.exists()},
            # a second wallet on this machine, in a directory this daemon does not serve: the classic
            # way a user funds an address they cannot spend from
            {"check": "one wallet home", "ok": not strays,
             "detail": ("other home(s) with a keystore: " + ", ".join(str(s) for s in strays[:3]))
                       if strays else ""},
            {"check": "unlocked", "ok": self.is_unlocked()},
            {"check": "settings signed", "ok": bool(loaded.signed) and not loaded.tampered},
            {"check": "signer installed", "ok": bool(signer["installed"])},
            {"check": "signer runs", "ok": bool(signer.get("runs")),
             "detail": signer.get("reason", "")},
            {"check": "signer reachable",
             "ok": bool(JsonRpcProbe(self.settings.monero_rpc_url,
                                    *((lambda c: (c["user"], c["password"]))(
                                        signer_mod.credentials(self.home, create=False)))).ok())},
            {"check": "receipt chain", "ok": bool(self.ledger and self.ledger.verify()[0])},
            {"check": "receipt anchor", "ok": bool(self.ledger_verify().get("anchor", {}).get("ok"))},
            {"check": "contacts file", "ok": self.contacts_path.exists() or not self.contacts},
            {"check": "sandboxes file", "ok": self.sandboxes_path.exists() or not self.sandboxes},
        ]
        return {
            "ok": all(c["ok"] for c in checks),
            "checks": checks,
            "chains": self.chain_status()["chains"],
            "contacts": len(self.contacts),
            "sandboxes": len(self.sandboxes),
            "pending_requests": sum(1 for r in self.requests.values() if r.status == "pending"),
            "wallet_file": self.settings.monero_wallet_file,
            "rpc_url": self.settings.monero_rpc_url,
            "receipts": self.ledger.verify()[2] if self.ledger else 0,
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

    def _ensure_chain_keys(self, payload: Dict[str, Any], passphrase: str) -> Dict[str, Any]:
        """Add EVM/Bitcoin signing keys to an existing keystore, once, on unlock.

        Keystores created before those chains existed have no `keys` block. Generating them here and
        re-sealing with the passphrase we just verified keeps one unlock fast and leaves the file with
        exactly the same protection as before.
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
        keystore.write_atomic(self.keystore_path, keystore.seal(payload, passphrase))
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
        """Addresses are public: cache them so the page can show a funding address while locked."""
        try:
            return json.loads((self.home / "addresses.json").read_text(encoding="utf-8")).get("addresses", {})
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def _cache_addresses(self) -> None:
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
                          rpc_user=creds["user"], rpc_password=creds["password"])

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
                return {"kind": kind, "created": False, "address": address,
                        "signer": provisioned}
            created = adapter.create_wallet()
            address = adapter.address()
        except ChainError as exc:
            raise WalletError(exc.code, exc.message) from exc
        self._build_adapters()
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
        now = now_iso()
        contact = Contact(
            contact_id=existing.contact_id if existing else "con_" + secrets.token_hex(4),
            name=name, key=key,
            addresses={**(existing.addresses if existing else {}), **checked},
            notes=str(params.get("notes", existing.notes if existing else ""))[:200],
            created_at=existing.created_at if existing else now, updated_at=now,
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
        home = Path(getattr(self, "home", Path.home() / ".hermes" / "wallet"))
        for base in (home.parent / "profiles", Path.home() / ".hermes" / "profiles"):
            try:
                if base.is_dir():
                    names.update(child.name for child in base.iterdir()
                                 if child.is_dir() and not child.name.startswith("."))
            except OSError:
                continue
        current = os.environ.get("HERMES_PROFILE") or "default"
        names.add(current)
        return {"profiles": sorted(names), "current": current}

    def sandboxes_list(self) -> Dict[str, Any]:
        # ``profiles`` rides along so the pane can build the ACL chips from a route that has existed
        # since the first release, instead of depending on a newly added endpoint.
        return {"sandboxes": [s.to_dict() for s in self.sandboxes.values()],
                **self.profiles_list()}

    def sandbox_create(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Create a capped sub-wallet and file the funding transfer for operator approval."""
        self._require_unlocked()
        kind = str(params.get("kind", ""))
        asset = str(params.get("asset", "")).upper()
        budget = parse_amount(params.get("budget"), asset)
        hours = int(params.get("lifetime_hours", 24))
        label = str(params.get("label", ""))[:40]
        mode = str(params.get("mode", "budget") or "budget").strip().lower()
        if mode not in ("budget", "one_time"):
            raise WalletError("bad_setting", "mode must be 'budget' or 'one_time'")
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
        if not getattr(adapter, "supports_accounts", False):
            raise WalletError("unsupported", f"{kind} cannot hold sandbox accounts")
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
            allowed_profiles=[str(p).strip() for p in (params.get("allowed_profiles") or []) if str(p).strip()],
        )
        self.sandboxes[sandbox.sandbox_id] = sandbox
        self._save_sandboxes()

        # Funding is a real transfer to the new account, so it goes through a human.
        funding = self.request_tx({"kind": kind, "asset": asset, "amount": str(budget),
                                   "payee": sandbox.address, "memo": f"fund sandbox {sandbox.label}",
                                   "why": "sandbox budget", "funding_for": sandbox.sandbox_id})
        if self.ledger is not None:
            self.ledger.append("sandbox", "created", "operator",
                               {"sandbox_id": sandbox.sandbox_id, "account_index": sandbox.account_index,
                                "budget": str(budget), "asset": asset, "kind": kind,
                                "mode": mode, "self_destruct_seconds": destruct})
        return {"sandbox": sandbox.to_dict(), "funding": funding}

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
        adapter = self.adapters.get(sandbox.kind)
        if adapter is None:
            raise WalletError("chain_unavailable", f"chain {sandbox.kind} is not available")
        reserve = Decimal(str(self.settings.fee_cap.get(sandbox.asset, "0") or "0"))
        # Sweep what is actually in the account, not what the bookkeeping thinks is left: after one
        # sweep `spent` equals `budget`, so remaining() is 0 while coins may still sit in the account.
        remaining = sandbox.remaining()
        reader = getattr(adapter, "balance_at", None)
        if reader is not None:
            try:
                remaining = Decimal(str(reader(sandbox.account_index)))
            except Exception:  # noqa: BLE001 - fall back to the budget arithmetic
                pass
        if remaining - reserve <= 0:
            sandbox.enabled = False
            self._save_sandboxes()
            return {"swept": "0", "sandbox": sandbox.to_dict(), "note": "nothing left to sweep"}
        try:
            home_address = adapter.address()          # account 0 of the same wallet
        except ChainError as exc:
            raise WalletError(exc.code, exc.message) from exc
        result = self.request_tx({"kind": sandbox.kind, "asset": sandbox.asset,
                                  "amount": str(remaining - reserve), "payee": home_address,
                                  "memo": f"close sandbox {sandbox.label}",
                                  "why": "return unused sandbox budget",
                                  "sandbox_id": sandbox_id, "sweep_of": sandbox_id,
                                  "profile": str(params.get("profile", ""))})
        return {"swept": str(remaining - reserve), "fee_reserved": str(reserve),
                "to": home_address, "transfer": result, "sweep_rule": "sweep"}

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
        adapter = self.adapters.get(sandbox.kind)
        if adapter is None:
            sandbox.destroy_blocked = "chain_unavailable"
            self._save_sandboxes()
            raise WalletError("chain_unavailable",
                              f"{sandbox.kind} is not available; keeping the sandbox so nothing is lost")
        reserve = Decimal(str(self.settings.fee_cap.get(sandbox.asset, "0") or "0"))
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
        sandbox.enabled = False
        self._save_sandboxes()
        if self.ledger is not None:
            self.ledger.append("sandbox", "revoked", "operator",
                               {"sandbox_id": sandbox_id, "spent": str(sandbox.spent),
                                "remaining": str(sandbox.remaining())})

        sweep: Dict[str, Any] = {"attempted": False}
        if params.get("sweep", True) and was_funded and sandbox.remaining() > 0:
            if sandbox.kind not in self.adapters:
                sweep = {"attempted": False, "note": f"{sandbox.kind} is not available right now; "
                                                     "the remainder stays in the sub-account"}
            else:
                sweep = {"attempted": True}
                try:
                    # re-enable just long enough for the sweep to take the sandbox path, then close
                    sandbox.enabled = True
                    swept = self.sandbox_sweep({"sandbox_id": sandbox_id})
                    sweep["result"] = swept
                    sweep["swept"] = swept.get("swept")
                except WalletError as exc:
                    sweep["error"] = exc.code
                    sweep["reason"] = exc.message
                finally:
                    sandbox.enabled = False
                    self._save_sandboxes()
        return {"sandbox": sandbox.to_dict(), "sweep": sweep}

    def _sandbox_decision(self, req: "SpendRequest") -> Optional[Decision]:
        """Allow a spend without a human when it is inside an active sandbox's remaining budget."""
        sandbox = self.sandboxes.get(req.sandbox_id)
        if sandbox is None:
            return None
        if req.sweep_of == sandbox.sandbox_id:
            # Returning the balance home is not spending the budget: the funds go to our own address,
            # and this must work after the budget is spent or the sandbox is closed - that is exactly
            # when a teardown needs it. The payee is checked in request_tx before this point.
            return Decision(True, "sandbox", "sandbox_sweep",
                            f"returning the sandbox balance to the wallet")
        if sandbox.allowed_profiles and (not req.profile or req.profile not in sandbox.allowed_profiles):
            return Decision(False, "sandbox", "sandbox_profile",
                            f"profile '{req.profile}' may not spend from this sandbox")
        if not sandbox.enabled:
            return Decision(False, "sandbox", "sandbox_unfunded", "sandbox is not funded yet")
        # a spend from the sandbox pays its own network fee out of the same budget, so the last
        # payment has to leave the worst-case fee behind or it cannot be signed at all
        reserve = Decimal(str(self.settings.fee_cap.get(req.asset, "0") or "0"))
        if req.amount + reserve > sandbox.remaining():
            return Decision(False, "sandbox", "sandbox_budget",
                            f"{req.amount} + {reserve} fee exceeds the {sandbox.remaining()} left "
                            f"in this sandbox")
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
                            f"amount {req.amount} exceeds the remaining sandbox budget {sandbox.remaining()} {req.asset}")
        return Decision(True, "sandbox", "sandbox_ok",
                        f"inside sandbox budget ({sandbox.remaining()} left)")

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
                   "monero_wallet_file")

    # Settings that change what the wallet will do by itself. Toggling a coin or a node URL is a
    # preference; raising a cap, emptying an allowlist or changing the unattended policy is policy.
    # autolock is deliberately *not* here: it is checked directionally below, because shortening the
    # window (or locking sooner) removes authority and needs no secret, while lengthening it or
    # turning it off keeps keys loaded for longer and does.
    # includes the signer endpoint: pointing it at another host changes which address the wallet
    # reports (and where the human is told to send money), sends the signer's basic-auth credentials
    # there, and makes a later sweep "our address" somebody else's.
    SENSITIVE_KEYS = ("per_tx_cap", "daily_cap", "fee_cap", "payee_allowlist", "approval_threshold",
                      "unattended_policy", "monero_only", "approval_ttl_seconds",
                      "monero_rpc_url", "monero_provider", "monero_wallet_file")

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
            # the simulator is not a coin: keep it exactly as the operator left it
            if "sim-mainnet" in self.settings.chains_enabled and "sim-mainnet" not in needed:
                needed.append("sim-mainnet")
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
        sums: Dict[str, Decimal] = {}
        paid: Dict[tuple, Decimal] = {}
        counts: Dict[tuple, int] = {}
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
            asset = str(details.get("asset") or "")
            if not asset:
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
            by_asset[asset] = {"spent": f"{total:f}",
                               "count": sum(count for (payee, held), count in counts.items()
                                            if held == asset)}

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
                "since": since, "until": until}

    def ledger_csv(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """The receipt chain as CSV: every entry, not just the newest page.

        `seq,ts,kind,decision,rule,asset,amount,payee,tx_hash` — columns an accountant or a
        spreadsheet can diff against the signed JSONL. Amounts stay strings: a float column would
        round money.
        """
        since = parse_window(params.get("since"))
        until = parse_window(params.get("until"))
        if since and until and since > until:
            raise WalletError("bad_window", "since is after until")
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(["seq", "ts", "kind", "decision", "rule",
                         "asset", "amount", "payee", "tx_hash"])
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
                             details.get("payee", ""), details.get("tx_hash", "")])
            rows += 1
        return {"csv": buffer.getvalue(), "rows": rows, "since": since, "until": until}

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
        path = self._anchor_path()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"seq": entry["seq"], "sig": entry.get("sig", ""), "mac": mac}, handle)

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
        "balances": (AGENT, lambda s, p: s.balances()),
        "nodes": (AGENT, lambda s, p: s.nodes()),
        "accounts": (AGENT, lambda s, p: s.accounts()),
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
        "unlock": (OPERATOR, lambda s, p: s.unlock(str(p.get("passphrase", "")))),
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
        "mandates_delete": (OPERATOR, lambda s, p: s.mandates_delete(p)),
        "doctor": (AGENT, lambda s, p: s.doctor()),
        "create_keystore": (OPERATOR, lambda s, p: s.create_keystore(p)),
        "pay_url": (AGENT, lambda s, p: s.pay_url(p)),
        "receive_address": (AGENT, lambda s, p: s.receive_address(p)),
        "incoming": (AGENT, lambda s, p: s.incoming()),
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
        "sandbox_revoke": (OPERATOR, lambda s, p: s.sandbox_revoke(p)),
        "sandbox_set_profiles": (OPERATOR, lambda s, p: s.sandbox_set_profiles(p)),
        "sandbox_destroy": (OPERATOR, lambda s, p: s.sandbox_destroy(p)),
        "sandboxes_reap": (OPERATOR, lambda s, p: {"reaped": s.reap_sandboxes()}),
        "sandbox_sweep": (OPERATOR, lambda s, p: s.sandbox_sweep(p)),
    }
