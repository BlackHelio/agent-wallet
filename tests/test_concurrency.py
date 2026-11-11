"""The daemon is threaded: two concurrent calls must not corrupt the receipt chain.

The ledger is a hash chain (`seq`, `prev`, `hash`). Two threads that both read `last()` and then
both append produce two entries with the same seq and the same prev — the chain is broken
permanently, and every later entry chains from the break. This test drives real RPC calls in
parallel and then verifies the chain end to end.
"""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from walletd.rpc import ServerThread, call
from walletd.state import State
from walletd.tokens import AGENT, TokenStore

PASS = "test-passphrase-123"
PIN = "123456"   # the per-payment secret; every approval needs it
ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def xmr_addr(offset: int) -> str:
    body = (ALPHABET[offset:] + ALPHABET[:offset]) * 3
    return "57" + body[:93]


class ConcurrentRpcTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.state = State(self.home)
        self.state.initialize(PASS, pin=PIN)
        # the PIN gate itself is tested in tests/test_pin.py; these tests are about policy
        # accounting (budgets, fees, sweeps), so they run with the gate off and their approvals
        # do not need to type it
        self.state.settings.pin_every_payment = False
        self.state.unlock(PASS)
        self.state.settings.daily_cap = {"XMR": "100"}
        self.state.settings.per_tx_cap = {"XMR": "1"}
        self.tokens = TokenStore(self.home / "tokens")
        self.tokens.mint_all()
        self.socket = str(self.home / "walletd.sock")
        self.server = ServerThread(self.socket, self.state, self.tokens)
        self.server.__enter__()

    def tearDown(self) -> None:
        self.server.__exit__(None, None, None)
        self.tmp.cleanup()

    def _call(self, method: str, params: dict | None = None) -> dict:
        return call(self.socket, method, params, token=self.tokens.read(AGENT))

    def test_parallel_requests_keep_the_chain_valid(self) -> None:
        errors: list[Exception] = []
        baseline = sum(1 for _ in self.state.ledger.entries())

        def fire(index: int) -> None:
            try:
                self._call("request_tx", {"kind": "monero-stagenet", "asset": "XMR",
                                          "amount": "0.01", "payee": xmr_addr(100 + index),
                                          "why": f"parallel {index}"})
            except Exception as exc:  # noqa: BLE001 - collected and asserted below
                errors.append(exc)

        threads = [threading.Thread(target=fire, args=(i,)) for i in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [], f"requests failed: {errors[:2]}")
        self.assertIsNotNone(self.state.ledger)
        ok, reason, length = self.state.ledger.verify()
        self.assertTrue(ok, reason)
        self.assertEqual(length, baseline + 16)
        seqs = [entry["seq"] for entry in self.state.ledger.entries()]
        self.assertEqual(seqs, list(range(1, baseline + 17)))

    def test_parallel_contacts_keep_the_chain_valid(self) -> None:
        errors: list[Exception] = []

        def save(index: int) -> None:
            try:
                self._call("contact_save", {"name": f"person-{index}",
                                            "addresses": {"monero-stagenet": xmr_addr(200 + index)}})
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=save, args=(i,)) for i in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [], f"saves failed: {errors[:2]}")
        self.assertIsNotNone(self.state.ledger)
        ok, reason, _ = self.state.ledger.verify()
        self.assertTrue(ok, reason)
        self.assertEqual(len(self.state.contacts), 12)


if __name__ == "__main__":
    unittest.main()
