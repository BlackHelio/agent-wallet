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

    def entries(self) -> Iterator[Dict[str, Any]]:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    yield json.loads(line)

    def last(self) -> Optional[Dict[str, Any]]:
        last = None
        for last in self.entries():
            pass
        return last

    def entries_with_archives(self) -> Iterator[Dict[str, Any]]:
        """Every entry this wallet ever wrote: the live chain plus each archived one.

        Caps are computed from these. Reading only the live file would reset every daily, period and
        lifetime cap the moment history was archived — archiving is a view operation and must never
        hand out fresh budget.
        """
        for path in sorted(self.path.parent.glob(self.path.stem + "-*.jsonl")):
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if line:
                        try:
                            yield json.loads(line)
                        except json.JSONDecodeError:
                            continue
        yield from self.entries()

    def totals(self, asset: str, kind: Optional[str] = None, since: Optional[str] = None,
               payee: Optional[str] = None, include_archives: bool = True,
               until: Optional[str] = None) -> Decimal:
        total = Decimal("0")
        source = self.entries_with_archives() if include_archives else self.entries()
        for entry in source:
            if entry.get("decision") != "approved":
                continue
            details = entry.get("details", {})
            if details.get("asset") != asset:
                continue
            if kind is not None and details.get("kind") != kind:
                continue
            if payee is not None and details.get("payee") != payee:
                continue
            if since is not None and entry.get("ts", "") < since:
                continue
            if until is not None and entry.get("ts", "") > until:
                continue
            try:
                total += Decimal(str(details.get("amount", "0")))
            except (InvalidOperation, ValueError):
                continue
        return total

    # ---------- write ----------

    #: called with the finished entry, so the owner can pin a high-water mark
    on_append = None

    def append(self, kind: str, decision: str, rule: str, details: Dict[str, Any]) -> Dict[str, Any]:
        previous = self.last()
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
        rows = list(self.entries())
        return rows[-limit:]
