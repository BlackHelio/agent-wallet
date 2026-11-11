"""Hash-chained, Ed25519-signed receipts.

One JSONL line per decision — approved, denied, revoked, config change. `verify()` walks the file,
re-checks every signature and every link, and names the first break. The signing seed lives in the
keystore and is independent of any spending key, so receipts stay verifiable after the wallet is
rotated.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from nacl.signing import SigningKey, VerifyKey

ZERO = "0" * 64
ISO = "%Y-%m-%dT%H:%M:%SZ"


def _now() -> str:
    return datetime.now(timezone.utc).strftime(ISO)


def _canonical(entry: Dict[str, Any]) -> bytes:
    body = {k: v for k, v in entry.items() if k != "sig"}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _seed32(seed: bytes) -> bytes:
    return seed if len(seed) == 32 else hashlib.sha256(seed).digest()


def network_class(kind: Any) -> str:
    """"sim", "test" or "main": which money a chain id is about.

    A daily cap is a cap on an *asset*, and USDC on Base is the same money as USDC on Polygon — but
    play money on the simulator and worthless testnet coins are not, and must not eat (or hide
    behind) the cap on real funds.
    """
    text = str(kind or "").lower()
    if text.startswith("sim"):
        return "sim"
    if text.endswith(("stagenet", "testnet", "sepolia", "amoy", "regtest")):
        return "test"
    return "main"


def verify_file(path: Path, pubkey_hex: str) -> Tuple[bool, str, int]:
    """Verify a ledger with the public key only — no keystore, no passphrase."""
    verify_key = VerifyKey(bytes.fromhex(pubkey_hex))
    previous_sig = ZERO
    expected_seq = 1
    count = 0
    path = Path(path)
    if not path.exists():
        return True, "empty", 0
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            count += 1
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                return False, f"unparseable entry at line {count}", count
            if entry.get("seq") != expected_seq:
                return False, f"sequence break at entry {count} (expected {expected_seq}, found {entry.get('seq')})", count
            if entry.get("prev") != previous_sig:
                return False, f"chain break at seq {expected_seq}", count
            signature = entry.get("sig")
            if not isinstance(signature, str):
                return False, f"missing signature at seq {expected_seq}", count
            try:
                verify_key.verify(_canonical(entry), bytes.fromhex(signature))
            except Exception:
                return False, f"bad signature at seq {expected_seq}", count
            previous_sig = signature
            expected_seq += 1
    return True, "ok", count


class Ledger:
    def __init__(self, path: Path, seed: bytes):
        # The daemon appends from more than one thread: the reaper, the signer autostart, and the RPC
        # workers. Two of them took `last()` before either wrote, so both wrote the same `seq` and the
        # chain forked — which is exactly what `verify` reported on the live wallet at seq 325.
        self._append_lock = threading.Lock()
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._signing = SigningKey(_seed32(seed))
        self.pubkey_hex = bytes(self._signing.verify_key).hex()
        pub = self.path.with_suffix(self.path.suffix + ".pub")
        if not pub.exists():
            fd = os.open(pub, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="ascii") as handle:
                handle.write(self.pubkey_hex + "\n")
        try:
            os.chmod(self.path, 0o600)
        except FileNotFoundError:
            pass

    # ---------- read ----------

    # Parsing the whole file on every read made `last()` O(n) and every `totals()` O(n) too - and
    # `append()` calls `last()`, so writing N receipts cost O(N^2). The entries are kept in memory
    # once and extended on append; anything that rewrites the files on disk bumps `_generation`.
    _entries_cache: Optional[List[Dict[str, Any]]] = None
    _archives_cache: Optional[List[Dict[str, Any]]] = None
    _generation: int = 0
    _totals_cache: Optional[Dict[Tuple, Decimal]] = None

    def _invalidate(self) -> None:
        self._entries_cache = None
        self._archives_cache = None
        self._totals_cache = None
        self._generation += 1

    def _load_entries(self) -> List[Dict[str, Any]]:
        if self._entries_cache is not None:
            return self._entries_cache
        rows: List[Dict[str, Any]] = []
        if self.path.exists():
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if line:
                        try:
                            rows.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
        self._entries_cache = rows
        return rows

    def _load_archives(self) -> List[Dict[str, Any]]:
        if self._archives_cache is not None:
            return self._archives_cache
        rows: List[Dict[str, Any]] = []
        for path in sorted(self.path.parent.glob(self.path.stem + "-*.jsonl")):
            try:
                with path.open("r", encoding="utf-8") as handle:
                    for line in handle:
                        line = line.strip()
                        if line:
                            try:
                                rows.append(json.loads(line))
                            except json.JSONDecodeError:
                                continue
            except OSError:
                continue
        self._archives_cache = rows
        return rows

    def entries(self) -> Iterator[Dict[str, Any]]:
        yield from list(self._load_entries())

    def last(self) -> Optional[Dict[str, Any]]:
        rows = self._load_entries()
        return rows[-1] if rows else None

    def entries_with_archives(self) -> Iterator[Dict[str, Any]]:
        """Every entry this wallet ever wrote: the live chain plus each archived one.

        Caps are computed from these. Reading only the live file would reset every daily, period and
        lifetime cap the moment history was archived — archiving is a view operation and must never
        hand out fresh budget.
        """
        yield from list(self._load_archives())
        yield from self.entries()

    def totals(self, asset: str, kind: Optional[str] = None, since: Optional[str] = None,
               payee: Optional[str] = None, include_archives: bool = True,
               until: Optional[str] = None, mandate_id: Optional[str] = None,
               network: Optional[str] = None) -> Decimal:
        """Sum approved amounts.

        `mandate_id` scopes the total to one autopay's own spending. Receipts written since the
        field existed carry it; for older ones the only evidence is the creation floor, and those
        are counted only when they are *strictly* newer than it — a payment made in the same second
        as the autopay was created cannot have been authorised by it.

        `network` ("main" / "test" / "sim", see `network_class`) sums one asset across every chain
        of that class, which is what a per-asset daily cap has to be measured against.
        """
        key = (asset, kind, since, payee, include_archives, until, mandate_id, network,
               self._generation)
        if self._totals_cache is None:
            self._totals_cache = {}
        cached = self._totals_cache.get(key)
        if cached is not None:
            return cached
        total = Decimal("0")
        source = self.entries_with_archives() if include_archives else self.entries()
        for entry in source:
            if entry.get("decision") != "approved":
                continue
            details = entry.get("details", {})
            if details.get("asset") != asset:
                continue
            # money between our own accounts — funding a sandbox, sweeping it home, a merge — is not
            # spending: it has no payee, and counting it charged a daily cap twice for the same money
            # and made a 0.02 budget look like 0.02 paid to somebody
            if details.get("funding_for") or details.get("sweep_of"):
                continue
            if kind is not None and details.get("kind") != kind:
                continue
            if network is not None and network_class(details.get("kind")) != network:
                continue
            if payee is not None and details.get("payee") != payee:
                continue
            if mandate_id is not None:
                entry_mandate = str(details.get("mandate_id") or "")
                if entry_mandate:
                    if entry_mandate != mandate_id:
                        continue
                elif not since or entry.get("ts", "") <= since:
                    continue
            elif since is not None and entry.get("ts", "") < since:
                continue
            if until is not None and entry.get("ts", "") > until:
                continue
            try:
                total += Decimal(str(details.get("amount", "0")))
            except (InvalidOperation, ValueError):
                continue
        if len(self._totals_cache) > 512:      # bounded: a long-running daemon must not grow forever
            self._totals_cache.clear()
        self._totals_cache[key] = total
        return total

    # ---------- write ----------

    #: called with the finished entry, so the owner can pin a high-water mark
    on_append = None

    def append(self, kind: str, decision: str, rule: str, details: Dict[str, Any]) -> Dict[str, Any]:
        """Write one receipt. Serialised: `seq` and `prev` are read and written under one lock."""
        with self._append_lock:
            return self._append_locked(kind, decision, rule, details)

    def _append_locked(self, kind: str, decision: str, rule: str,
                       details: Dict[str, Any]) -> Dict[str, Any]:
        entries = self._load_entries()
        previous = entries[-1] if entries else None
        entry = {
            "seq": (previous["seq"] + 1) if previous else 1,
            "prev": previous["sig"] if previous else ZERO,
            "ts": _now(),
            "kind": kind,
            "decision": decision,
            "rule": rule,
            "details": details,
        }
        signature = self._signing.sign(_canonical(entry)).signature.hex()
        entry["sig"] = signature
        line = json.dumps(entry, sort_keys=True, separators=(",", ":"))
        existed = self.path.exists()
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            if not existed:
                os.chmod(self.path, 0o600)
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        # keep the read cache in step with the file: the entry we just wrote is the newest one
        entries.append(entry)
        self._totals_cache = None
        self._generation += 1
        if callable(self.on_append):
            try:
                self.on_append(entry)
            except Exception:  # noqa: BLE001 - a receipt already written must not be undone
                pass
        return entry

    # ---------- verify ----------

    def verify(self) -> Tuple[bool, str, int]:
        return verify_file(self.path, self.pubkey_hex)

    def tail(self, limit: int = 20) -> List[Dict[str, Any]]:
        rows = self._load_entries()
        return rows[-limit:] if limit > 0 else []
