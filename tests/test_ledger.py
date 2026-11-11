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
        from tests import assert_private
        assert_private(self, self.path)


if __name__ == "__main__":
    unittest.main()


class ConcurrentAppendTests(unittest.TestCase):
    """The daemon appends from several threads; the chain must stay one line of history."""

    def test_concurrent_appends_do_not_fork_the_chain(self) -> None:
        import tempfile
        import threading
        from pathlib import Path

        from walletd.ledger import Ledger

        home = Path(tempfile.mkdtemp())
        ledger = Ledger(home / "ledger.jsonl", b"\x11" * 32)
        errors: list = []

        def worker(n: int) -> None:
            try:
                for i in range(10):
                    ledger.append("bench", "ok", "thread", {"n": n, "i": i})
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        rows = [json.loads(line) for line in (home / "ledger.jsonl").read_text().splitlines()]
        self.assertEqual([r["seq"] for r in rows], list(range(1, 81)))
        prev = "0" * 64
        for row in rows:
            self.assertEqual(row["prev"], prev, f"chain break at seq {row['seq']}")
            prev = row["sig"]
        ok, message, count = ledger.verify()
        self.assertTrue(ok, message)
        self.assertEqual(count, 80)


class AnchorInVerifyFileTests(unittest.TestCase):
    """The chain alone cannot see a truncated tail; the anchor can, and the standalone verifier ignored it."""

    def test_a_truncated_ledger_is_not_ok_with_an_anchor_present(self) -> None:
        import json
        import subprocess
        import sys
        import tempfile
        from pathlib import Path as _Path

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = _Path(tmp.name)
        ledger = Ledger(home / "ledger.jsonl", b"k" * 32)
        for i in range(4):
            ledger.append("wallet", "checked", "operator", {"i": i})
        (home / "ledger.jsonl.pub").write_text(ledger.pubkey_hex, encoding="ascii")
        last = ledger.last()
        (home / "ledger.anchor").write_text(json.dumps({"seq": last["seq"], "sig": last["sig"]}),
                                            encoding="utf-8")

        # the file-writer attacker deletes the last two receipts
        lines = (home / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
        (home / "ledger.jsonl").write_text("\n".join(lines[:-2]) + "\n", encoding="utf-8")

        from walletd.ledger import verify_file
        ok, _message, count = verify_file(home / "ledger.jsonl", ledger.pubkey_hex)
        self.assertTrue(ok, "the chain alone is self-consistent — that is the point of the anchor")
        self.assertEqual(count, 2)

        run = subprocess.run([sys.executable, "-m", "walletd", "--home", str(home), "verify"],
                             capture_output=True, text=True, cwd=str(_Path(__file__).resolve().parents[1]))
        self.assertNotEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("were recorded", run.stdout)
