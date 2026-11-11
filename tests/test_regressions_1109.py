"""Guards added after a brutal review round: the things that could lose money or lie about it.

Each test here exists because a reviewer reproduced the bug, not because the code looked risky.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from walletd.backup import INCLUDED
from tests.test_state import FakeAdapter, PASS, TEST_ASSETS, TEST_CHAINS, WalletError, WalletState, xmr_addr

PIN = "123456"


class PooledSandboxRefusalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        FakeAdapter._next_account = 1
        FakeAdapter.balances = {}
        self.state = WalletState(Path(self.tmp.name))
        self.state.fake = FakeAdapter()
        self.state.initialize(PASS, pin=PIN)
        self.state.settings.chains_enabled = list(TEST_CHAINS)
        self.state.settings.assets_enabled = list(TEST_ASSETS)
        self.state.adapters = {kind: self.state.fake for kind in TEST_CHAINS}
        self.state.settings.pin_every_payment = False

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_pooled_is_refused_where_a_real_account_exists(self) -> None:
        """A pooled sandbox on an account chain armed a real sub-account that no sweep would empty."""
        with self.assertRaises(WalletError) as caught:
            self.state.sandbox_create({"kind": "monero-stagenet", "asset": "XMR", "budget": "0.1",
                                       "mode": "pooled", "proposed": True})
        self.assertEqual(caught.exception.code, "bad_setting")
        self.assertIn("real sandbox account", caught.exception.message)
        self.assertEqual(self.state.sandboxes, {})

    def test_pooled_still_works_where_the_chain_has_no_accounts(self) -> None:
        self.state.fake.supports_accounts = False
        out = self.state.sandbox_create({"kind": "monero-stagenet", "asset": "XMR",
                                         "budget": "0.1", "proposed": True})
        self.assertEqual(out["sandbox"]["mode"], "pooled")
        self.assertEqual(out["sandbox"]["account_index"], -1)


class LedgerHonestyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        FakeAdapter._next_account = 1
        FakeAdapter.balances = {}
        self.state = WalletState(Path(self.tmp.name))
        self.state.fake = FakeAdapter()
        self.state.initialize(PASS, pin=PIN)
        self.state.settings.chains_enabled = list(TEST_CHAINS)
        self.state.settings.assets_enabled = list(TEST_ASSETS)
        self.state.adapters = {kind: self.state.fake for kind in TEST_CHAINS}
        self.state.settings.pin_every_payment = False

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _pay(self, amount: str) -> None:
        out = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": amount,
                                     "payee": xmr_addr(41), "why": "test", "_scope": "agent"})
        self.state.approve({"pin": PIN, "passphrase": PASS,
                            "request_id": out["request"]["request_id"],
                            "approval_id": out["approval_id"]})

    def test_spend_totals_report_the_fees_too(self) -> None:
        """The fee is money that left the wallet; a total without it understates the spend."""
        self._pay("0.01")
        totals = self.state.spend_totals({})
        asset = totals["assets"]["XMR"]
        self.assertEqual(asset["spent"], "0.010000000000")
        self.assertGreater(float(asset["fees"]), 0.0)
        self.assertEqual(float(asset["total_out"]),
                         float(asset["spent"]) + float(asset["fees"]))
        self.assertTrue(totals["verified"])

    def test_a_tampered_chain_refuses_to_be_totalled(self) -> None:
        self._pay("0.01")
        path = self.state.home / "ledger.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
        row = json.loads(lines[-1])
        row["details"]["amount"] = "9.0"
        lines[-1] = json.dumps(row)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.state.ledger = None          # force the on-disk read the report uses
        with self.assertRaises(WalletError) as caught:
            self.state.spend_totals({})
        self.assertEqual(caught.exception.code, "bad_ledger")

    def test_the_csv_carries_the_fee_and_the_verdict(self) -> None:
        self._pay("0.01")
        out = self.state.ledger_csv({})
        header = out["csv"].splitlines()[0].split(",")
        self.assertIn("fee", header)
        self.assertIn("verified", out)


class BackupCompletenessTests(unittest.TestCase):
    def test_deposits_and_their_accounts_are_in_the_backup(self) -> None:
        """Restoring without deposits.json left the money on-chain and nothing knowing where."""
        self.assertIn("deposits.json", INCLUDED)
        self.assertIn("sim-balances.json", INCLUDED)


class UnattendedMandateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        FakeAdapter._next_account = 1
        FakeAdapter.balances = {}
        self.state = WalletState(Path(self.tmp.name))
        self.state.fake = FakeAdapter()
        self.state.initialize(PASS, pin=PIN)
        self.state.settings.chains_enabled = list(TEST_CHAINS)
        self.state.settings.assets_enabled = list(TEST_ASSETS)
        self.state.adapters = {kind: self.state.fake for kind in TEST_CHAINS}
        self.state.settings.pin_every_payment = True
        self.made = self.state.mandates_create({
            "kind": "monero-stagenet", "asset": "XMR", "payee": xmr_addr(42),
            "per_tx_cap": "0.01", "period_cap": "0.05", "absolute_cap": "0.1",
            "interval": "24h", "passphrase": PASS, "activate": True})
        self.mandate_id = self.made["mandate"]["mandate_id"]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _spend(self, amount: str = "0.005") -> dict:
        return self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": amount,
                                      "payee": xmr_addr(42), "why": "sub", "_scope": "agent"})

    def test_an_autopay_still_asks_for_the_pin_by_default(self) -> None:
        out = self._spend()
        self.assertEqual(out["decision"]["code"], "needs_pin")

    def test_the_waiver_needs_the_passphrase_and_the_pin(self) -> None:
        with self.assertRaises(WalletError) as caught:
            self.state.mandates_set_unattended({"mandate_id": self.mandate_id, "unattended": True,
                                                "passphrase": PASS, "pin": "000000"})
        self.assertEqual(caught.exception.code, "bad_pin")
        out = self.state.mandates_set_unattended({"mandate_id": self.mandate_id,
                                                  "passphrase": PASS, "pin": PIN})
        self.assertTrue(out["mandate"]["unattended"])
        # …and now that autopay alone pays without asking
        self.assertTrue(self._spend()["decision"]["allow"])

    def test_creating_an_autopay_cannot_smuggle_the_waiver(self) -> None:
        made = self.state.mandates_create({
            "kind": "monero-stagenet", "asset": "XMR", "payee": xmr_addr(43),
            "per_tx_cap": "0.01", "period_cap": "0.05", "absolute_cap": "0.1",
            "passphrase": PASS, "activate": True, "unattended": True})
        self.assertFalse(made["mandate"]["unattended"])

    def test_revoking_removes_the_waiver_and_reads_as_revoked(self) -> None:
        self.state.mandates_set_unattended({"mandate_id": self.mandate_id,
                                            "passphrase": PASS, "pin": PIN})
        out = self.state.mandates_revoke({"mandate_id": self.mandate_id, "reason": "test"})
        self.assertFalse(out["mandate"]["unattended"])
        self.assertEqual(out["mandate"]["status"], "revoked")

    def test_pausing_is_not_revoking(self) -> None:
        out = self.state.mandates_set_enabled({"mandate_id": self.mandate_id, "enabled": False})
        self.assertEqual(out["mandate"]["status"], "paused")


class ChallengeProvenanceTests(unittest.TestCase):
    def test_a_caller_cannot_hand_itself_a_challenge(self) -> None:
        """`_from_pay_url` used to be read out of params, so an agent could aim a signed x402
        authorisation at its own host and read the response."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        FakeAdapter._next_account = 1
        FakeAdapter.balances = {}
        state = WalletState(Path(tmp.name))
        state.fake = FakeAdapter()
        state.initialize(PASS, pin=PIN)
        state.settings.chains_enabled = list(TEST_CHAINS)
        state.settings.assets_enabled = list(TEST_ASSETS)
        state.adapters = {kind: state.fake for kind in TEST_CHAINS}
        out = state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.001",
                                "payee": xmr_addr(44), "why": "test", "_scope": "agent",
                                "_from_pay_url": True,
                                "challenge": {"url": "http://127.0.0.1:9/x", "settlement": "x402"}})
        self.assertFalse(state.requests[out["request"]["request_id"]].challenge,
                         "a caller-supplied _from_pay_url must not attach a challenge")


if __name__ == "__main__":
    unittest.main()
