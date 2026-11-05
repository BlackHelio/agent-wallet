from __future__ import annotations

import json
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from walletd.ledger import Ledger, verify_file


class LedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "ledger.jsonl"
        self.ledger = Ledger(self.path, b"seed-for-tests")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_chain_verifies(self) -> None:
        for i in range(3):
            self.ledger.append("spend_request", "approved", "manual_approval",
                               {"asset": "XMR", "amount": "0.1", "kind": "monero-stagenet", "payee": "p"})
        ok, message, count = self.ledger.verify()
        self.assertTrue(ok, message)
        self.assertEqual(count, 3)
        self.assertEqual([e["seq"] for e in self.ledger.entries()], [1, 2, 3])

    def test_tampered_field_fails(self) -> None:
        self.ledger.append("spend_request", "approved", "manual_approval", {"asset": "XMR", "amount": "0.1"})
        lines = self.path.read_text(encoding="utf-8").splitlines()
        entry = json.loads(lines[0])
        entry["details"]["amount"] = "9.9"
        self.path.write_text(json.dumps(entry) + "\n", encoding="utf-8")
        ok, message, _ = verify_file(self.path, self.ledger.pubkey_hex)
        self.assertFalse(ok)
        self.assertIn("signature", message)

    def test_reordered_entries_fail(self) -> None:
        self.ledger.append("a", "approved", "r", {})
        self.ledger.append("b", "approved", "r", {})
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.path.write_text(lines[1] + "\n" + lines[0] + "\n", encoding="utf-8")
        ok, message, _ = verify_file(self.path, self.ledger.pubkey_hex)
        self.assertFalse(ok)
        self.assertIn("break", message)

    def test_deleted_middle_entry_fails(self) -> None:
        for name in ("a", "b", "c"):
            self.ledger.append(name, "approved", "r", {})
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.path.write_text(lines[0] + "\n" + lines[2] + "\n", encoding="utf-8")
        ok, _, _ = verify_file(self.path, self.ledger.pubkey_hex)
        self.assertFalse(ok)

    def test_wrong_pubkey_fails(self) -> None:
        self.ledger.append("a", "approved", "r", {})
        other = Ledger(Path(self.tmp.name) / "other.jsonl", b"other-seed")
        ok, _, _ = verify_file(self.path, other.pubkey_hex)
        self.assertFalse(ok)

    def test_totals_respect_filters(self) -> None:
        self.ledger.append("spend_request", "approved", "mandate",
                           {"asset": "XMR", "amount": "0.1", "kind": "monero-stagenet", "payee": "addr"})
        self.ledger.append("spend_request", "approved", "mandate",
                           {"asset": "XMR", "amount": "0.2", "kind": "monero-stagenet", "payee": "addr"})
        self.ledger.append("spend_request", "denied", "per_tx_cap",
                           {"asset": "XMR", "amount": "9.0", "kind": "monero-stagenet", "payee": "addr"})
        self.assertEqual(self.ledger.totals("XMR", kind="monero-stagenet", payee="addr"), Decimal("0.3"))
        self.assertEqual(self.ledger.totals("XMR", payee="other"), Decimal("0"))
        self.assertEqual(self.ledger.totals("BTC"), Decimal("0"))

    def test_file_mode_is_0600(self) -> None:
        self.ledger.append("a", "approved", "r", {})
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
