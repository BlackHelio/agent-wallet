"""The simulated chain's ledger: real balances, no network.

`sim-mainnet` exists so the whole flow — fund a sandbox, spend from it, sweep the remainder home,
tear it down — can be shown and reviewed without money. That demo is only honest if the simulator
keeps balances the way a chain does:

* an account is debited when it pays, and the fee is paid by the sender as well as the amount;
* an account is credited when the payee is one of our own addresses;
* a payment to anything else leaves our control, so it lowers the total instead of coming back;
* an account that was never funded holds zero — the simulator never invents money for it.

Only account 0 is seeded, and only when no ledger file exists yet. Everything is `Decimal` (never a
float) and every read is side-effect free: the file is written when value moves, not when it is
looked at. A missing file is "the demo has not moved anything yet", not an error; a corrupt one is
an error, because silently starting over would erase balances a user was shown.
"""

from __future__ import annotations

import hashlib
import json
import os
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import ChainError

VERSION = 1
LEDGER_NAME = "sim-balances.json"
ATOMIC = Decimal(10) ** 12                  # 1 XMR = 1e12 atomic units, same as the real chain
QUANTUM = Decimal("0.000000000001")         # 12 dp: the simulator has no sub-atomic amounts
FEE = Decimal("0.00003")                    # fixed: a simulated network never reprices
START_BALANCE = Decimal("1")                # account 0 only, and only on a fresh ledger
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
PAYMENT_HISTORY = 200                       # proofs stay answerable; the file stays small


def parse_amount(value: Any) -> Decimal:
    """A money value from a caller (str/Decimal/int) at the simulator's precision."""
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ChainError("bad_amount", f"{value!r} is not an amount") from exc
    if not parsed.is_finite():
        raise ChainError("bad_amount", f"{value!r} is not an amount")
    return parsed.quantize(QUANTUM)


def format_amount(value: Any) -> str:
    """Fixed-point text, exact, trailing zeros trimmed. Never `float`."""
    text = format(parse_amount(value), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _address(salt: str, index: int) -> str:
    """A deterministic, Monero-shaped address for one sub-account.

    Same home + same index always gives the same address, so a restart does not move the address a
    user was told to fund. It has no key behind it: this is a simulator, and nothing can spend it.
    """
    digest = hashlib.shake_256(f"agent-wallet/sim/v1/address|{salt}|{int(index)}".encode()).digest(94)
    body = "".join(B58[byte % 58] for byte in digest[:93])
    return "4" + "0123456789AB"[digest[93] % 12] + body


class SimLedger:
    """Per-account balances for `sim-mainnet`, persisted next to the wallet."""

    def __init__(self, home: Optional[Path] = None, seed: Decimal = START_BALANCE):
        self._home = Path(home) if home is not None else None
        self._seed = parse_amount(seed)
        self._state: Optional[Dict[str, Any]] = None

    # ---------- storage ----------

    @property
    def path(self) -> Path:
        home = self._home
        if home is None:
            from ..home import resolve_home      # the one resolver every component uses

            home = resolve_home()
        return Path(home) / LEDGER_NAME

    @property
    def salt(self) -> str:
        """Per-install salt: addresses are stable, and not identical on every machine."""
        return hashlib.sha256(f"agent-wallet/sim/v1|{self.path}".encode()).hexdigest()

    def reload(self) -> None:
        """Forget the in-memory copy — the next read comes from disk (restart semantics)."""
        self._state = None

    def _blank(self) -> Dict[str, Any]:
        return {"version": VERSION, "next_account": 1, "nonce": 0,
                "accounts": {"0": {"label": "main", "balance": format_amount(self._seed)}},
                "payments": {}}

    def state(self) -> Dict[str, Any]:
        if self._state is not None:
            return self._state
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self._state = self._blank()
            return self._state
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ChainError("sim_ledger_unreadable", f"{self.path}: {exc}") from exc
        self._state = self._validate(raw)
        return self._state

    def _validate(self, raw: Any) -> Dict[str, Any]:
        if not isinstance(raw, dict) or int(raw.get("version", 0) or 0) != VERSION:
            raise ChainError("sim_ledger_unreadable", f"{self.path}: unknown format")
        accounts: Dict[str, Dict[str, str]] = {}
        for key, row in (raw.get("accounts") or {}).items():
            try:
                index = int(key)
                balance = parse_amount((row or {})["balance"])
            except (KeyError, TypeError, ValueError, InvalidOperation, ChainError) as exc:
                raise ChainError("sim_ledger_unreadable",
                                 f"{self.path}: bad account entry {key!r}") from exc
            if balance < 0:
                raise ChainError("sim_ledger_unreadable", f"{self.path}: negative balance on {index}")
            accounts[str(index)] = {"label": str((row or {}).get("label", ""))[:40],
                                    "balance": format_amount(balance)}
        try:
            next_account = max(1, int(raw.get("next_account", 1) or 1))
            nonce = int(raw.get("nonce", 0) or 0)
        except (TypeError, ValueError) as exc:
            raise ChainError("sim_ledger_unreadable", f"{self.path}: bad counters") from exc
        payments = raw.get("payments")
        return {"version": VERSION, "next_account": next_account, "nonce": nonce,
                "accounts": accounts,
                "payments": dict(payments) if isinstance(payments, dict) else {}}

    def _save(self) -> None:
        state = self.state()
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
        os.replace(tmp, path)
        os.chmod(path, 0o600)

    # ---------- reads (never write, never invent) ----------

    def balance(self, account_index: int) -> Decimal:
        row = self.state()["accounts"].get(str(int(account_index)))
        return Decimal(row["balance"]) if row else Decimal("0")

    def total(self) -> Decimal:
        return sum((Decimal(row["balance"]) for row in self.state()["accounts"].values()),
                   Decimal("0"))

    def address(self, account_index: int) -> str:
        return _address(self.salt, int(account_index))

    def account_index_for(self, address: str) -> Optional[int]:
        """Which of our accounts a payee is, or None when the value leaves our control."""
        wanted = str(address or "")
        if not wanted:
            return None
        for key in self.state()["accounts"]:
            if self.address(int(key)) == wanted:
                return int(key)
        return None

    def accounts(self) -> List[Dict[str, Any]]:
        rows = []
        for key in sorted(self.state()["accounts"], key=int):
            row = self.state()["accounts"][key]
            rows.append({"account_index": int(key), "address": self.address(int(key)),
                         "balance": row["balance"], "unlocked": row["balance"],
                         "label": row.get("label", "")})
        return rows

    # ---------- value movement ----------

    def _account(self, account_index: int) -> Dict[str, str]:
        accounts = self.state()["accounts"]
        row = accounts.get(str(int(account_index)))
        if row is None:
            raise ChainError("unknown_account", f"account {int(account_index)} does not exist")
        return row

    def create_account(self, label: str) -> Dict[str, Any]:
        state = self.state()
        index = int(state["next_account"])
        state["next_account"] = index + 1
        state["accounts"][str(index)] = {"label": str(label or "")[:40], "balance": "0"}
        self._save()
        return {"account_index": index, "address": self.address(index)}

    def open_payment(self, account_index: int, amount: Any, payee: str, fee: Any = FEE) -> Dict[str, Any]:
        """Check the funds and mint a deterministic tx hash. Nothing has moved yet."""
        state = self.state()
        index = int(account_index)
        value = parse_amount(amount)
        cost = parse_amount(fee)
        held = self.balance(index)
        if value + cost > held:
            raise ChainError(
                "insufficient_funds",
                f"account {index} holds {format_amount(held)} XMR; "
                f"{format_amount(value)} + {format_amount(cost)} fee does not fit")
        nonce = int(state["nonce"]) + 1
        state["nonce"] = nonce
        digest = hashlib.sha256(
            f"agent-wallet/sim/v1/tx|{self.salt}|{nonce}|{index}|"
            f"{format_amount(value)}|{payee}".encode()).hexdigest()
        tx_key = hashlib.sha256(
            f"agent-wallet/sim/v1/key|{self.salt}|{nonce}|{payee}".encode()).hexdigest()
        self._save()                    # the nonce must survive a restart, or a hash could repeat
        return {"simulated": True, "tx_hash": "sim" + digest[:61], "tx_key": tx_key,
                "atomic": int(value * ATOMIC), "account_index": index}

    def settle(self, plan: Any) -> Dict[str, Any]:
        """Move the value: debit sender (amount + fee), credit a payee that is ours."""
        raw = getattr(plan, "raw", {}) or {}
        tx_hash = str(raw.get("tx_hash", ""))
        if not tx_hash:
            raise ChainError("plan_failed", "this plan was never built by the simulator")
        payments = self.state()["payments"]
        previous = payments.get(tx_hash)
        if previous:
            # Broadcasting the same plan twice must not spend twice: a retry after a restart used
            # to be able to double-debit a simulated account.
            return {"tx_hash": tx_hash, "tx_key": str(raw.get("tx_key", "")),
                    "fee": previous.get("fee", "0"), "confirmations": 1, "simulated": True,
                    "external": previous.get("to") is None, "amount": previous.get("amount", "0"),
                    "already_settled": True}
        index = int(raw.get("account_index", 0))
        amount = parse_amount(getattr(plan, "amount", 0))
        fee = parse_amount(getattr(plan, "fee", 0))
        sender = self._account(index)
        held = Decimal(sender["balance"])
        if amount + fee > held:
            raise ChainError("insufficient_funds",
                             f"account {index} holds {format_amount(held)} XMR; "
                             f"{format_amount(amount)} + {format_amount(fee)} fee does not fit")
        sender["balance"] = format_amount(held - amount - fee)
        target = self.account_index_for(getattr(plan, "payee", ""))
        if target is not None:
            receiver = self._account(target)
            receiver["balance"] = format_amount(Decimal(receiver["balance"]) + amount)
        # A payee that is not ours is an external sink: the value has left our control, so the
        # total drops by amount + fee and nothing is credited back to us.
        payments[tx_hash] = {"from": index, "to": target, "amount": format_amount(amount),
                             "fee": format_amount(fee)}
        if len(payments) > PAYMENT_HISTORY:
            for stale in list(payments)[:-PAYMENT_HISTORY]:
                payments.pop(stale, None)
        self._save()
        return {"tx_hash": tx_hash, "tx_key": str(raw.get("tx_key", "")),
                "fee": format_amount(fee), "confirmations": 1, "simulated": True,
                "external": target is None, "amount": format_amount(amount)}

    def proof(self, txid: str, address: str) -> Dict[str, Any]:
        row = self.state()["payments"].get(str(txid))
        if not row:
            return {"received": False, "amount": "0", "confirmations": 0, "in_pool": False,
                    "reason": "no such simulated transaction"}
        ours = self.account_index_for(address)
        return {"received": ours is not None and row.get("to") == ours,
                "amount": str(row.get("amount", "0")), "confirmations": 1, "in_pool": False}

