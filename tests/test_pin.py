"""The PIN gate: every payment, one card, one secret, and no way around it.

The policy tests run with `pin_every_payment` off (see `tests/test_state.py` setUp) so they can keep
testing budgets and fees; this module owns the gate itself, at the product default.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests.test_state import FakeAdapter, PASS, TEST_ASSETS, TEST_CHAINS, WalletError, WalletState, xmr_addr

PIN = "123456"
WRONG = "000000"

AGENT = {"_scope": "agent"}


class PinGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        FakeAdapter._next_account = 1
        FakeAdapter.balances = {}
        self.state = WalletState(self.home)
        self.state.fake = FakeAdapter()
        self.state.initialize(PASS, pin=PIN)
        self.state.settings.chains_enabled = list(TEST_CHAINS)
        self.state.settings.assets_enabled = list(TEST_ASSETS)
        self.state.adapters = {kind: self.state.fake for kind in TEST_CHAINS}
        # the product default: the gate is on
        self.assertTrue(self.state.settings.pin_every_payment)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # ---------- helpers ----------
    def _filed(self, amount: str = "0.01", payee: str | None = None) -> dict:
        out = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": amount,
                                     "payee": payee or xmr_addr(30), "why": "test", **AGENT})
        self.assertTrue(out["decision"]["allow"], out)
        self.assertTrue(out.get("approval_id"), out)
        return out

    # ---------- the gate exists ----------
    def test_the_pin_is_required_by_default(self) -> None:
        status = self.state.pin_status()
        self.assertTrue(status["set"])
        self.assertTrue(status["required"])
        self.assertEqual(status["attempts_left"], 5)

    def test_an_approval_without_the_pin_is_refused(self) -> None:
        filed = self._filed()
        with self.assertRaises(WalletError) as caught:
            self.state.approve({"request_id": filed["request"]["request_id"],
                                "approval_id": filed["approval_id"]})
        self.assertEqual(caught.exception.code, "pin_required")
        # and nothing was signed: the request is still waiting
        self.assertEqual(self.state.requests[filed["request"]["request_id"]].status, "pending")

    def test_a_six_digit_passphrase_is_read_as_the_pin(self) -> None:
        # A card built before the PIN field existed posts the digits as `passphrase`. Refusing that
        # looks to the operator like "the PIN does not work" when nothing is wrong with the PIN.
        filed = self._filed()
        done = self.state.approve({"passphrase": PIN, "request_id": filed["request"]["request_id"],
                                   "approval_id": filed["approval_id"]})
        self.assertEqual(done["request"]["status"], "sent")
        self.assertEqual(done["request"]["authorised_by"], "pin")

    def test_the_pin_pays(self) -> None:
        filed = self._filed()
        done = self.state.approve({"pin": PIN, "request_id": filed["request"]["request_id"],
                                   "approval_id": filed["approval_id"]})
        self.assertEqual(done["request"]["status"], "sent")
        self.assertEqual(done["request"]["authorised_by"], "pin")

    def test_the_pin_unlocks_the_wallet_too(self) -> None:
        # One secret, both jobs: the PIN that authorises a payment is the same
        # one that opens the pane. Six digits can never be a passphrase
        # (minimum eight), so trying them on the passphrase path is safe.
        self.state.lock()
        self.assertTrue(self.state.status()["locked"])
        self.state.unlock(PIN)
        self.assertFalse(self.state.status()["locked"])

    # ---------- wrong guesses are counted, then locked out ----------
    def test_wrong_pins_are_counted_and_then_locked(self) -> None:
        filed = self._filed()
        for attempt in range(1, 5):
            with self.assertRaises(WalletError) as caught:
                self.state.approve({"pin": WRONG, "request_id": filed["request"]["request_id"],
                                    "approval_id": filed["approval_id"]})
            self.assertEqual(caught.exception.code, "bad_pin")
            self.assertEqual(self.state.pin_status()["attempts_left"], 5 - attempt)
        with self.assertRaises(WalletError) as caught:
            self.state.approve({"pin": WRONG, "request_id": filed["request"]["request_id"],
                                "approval_id": filed["approval_id"]})
        self.assertEqual(caught.exception.code, "pin_locked")
        # even the right PIN is refused while the lockout stands
        with self.assertRaises(WalletError) as caught:
            self.state.approve({"pin": PIN, "request_id": filed["request"]["request_id"],
                                "approval_id": filed["approval_id"]})
        self.assertEqual(caught.exception.code, "pin_locked")

    def test_the_lockout_survives_a_restart(self) -> None:
        filed = self._filed()
        for _ in range(5):
            with self.assertRaises(WalletError):
                self.state.approve({"pin": WRONG, "request_id": filed["request"]["request_id"],
                                    "approval_id": filed["approval_id"]})
        # a fresh State over the same home reads the counter from disk, not from memory
        again = WalletState(self.home)
        again.fake = FakeAdapter()
        again.unlock(PASS)
        self.assertEqual(again.pin_status()["attempts_left"], 0)

    def test_a_good_pin_clears_the_counter(self) -> None:
        filed = self._filed()
        with self.assertRaises(WalletError):
            self.state.approve({"pin": WRONG, "request_id": filed["request"]["request_id"],
                                "approval_id": filed["approval_id"]})
        self.assertEqual(self.state.pin_status()["attempts_left"], 4)
        self.state.approve({"pin": PIN, "request_id": filed["request"]["request_id"],
                            "approval_id": filed["approval_id"]})
        self.assertEqual(self.state.pin_status()["attempts_left"], 5)

    # ---------- the card ----------
    def test_the_card_cannot_be_retargeted_after_the_pin(self) -> None:
        filed = self._filed()
        with self.assertRaises(WalletError) as caught:
            self.state.approve({"pin": PIN, "request_id": filed["request"]["request_id"],
                                "approval_id": filed["approval_id"],
                                "expect_payee": xmr_addr(31)})
        self.assertEqual(caught.exception.code, "card_changed")

    def test_the_card_amount_cannot_change_after_the_pin(self) -> None:
        filed = self._filed("0.01")
        with self.assertRaises(WalletError) as caught:
            self.state.approve({"pin": PIN, "request_id": filed["request"]["request_id"],
                                "approval_id": filed["approval_id"],
                                "expect_amount": "0.05"})
        self.assertEqual(caught.exception.code, "card_changed")

    def test_a_card_that_matches_is_accepted(self) -> None:
        filed = self._filed("0.01")
        done = self.state.approve({"pin": PIN, "request_id": filed["request"]["request_id"],
                                   "approval_id": filed["approval_id"],
                                   "expect_amount": "0.01",
                                   "expect_payee": filed["request"]["payee"]})
        self.assertEqual(done["request"]["status"], "sent")

    # ---------- budgets no longer mean "no human" ----------
    def test_a_sandbox_spend_asks_for_the_pin(self) -> None:
        payee = xmr_addr(31)
        created = self.state.sandbox_create({"kind": "monero-stagenet", "asset": "XMR",
                                             "budget": "0.05", "lifetime_hours": 24,
                                             "label": "pin test", "allowed_payees": [payee]})
        sandbox_id = created["sandbox"]["sandbox_id"]
        funding = created["funding"]
        self.state.approve({"pin": PIN, "request_id": funding["request"]["request_id"],
                            "approval_id": funding["approval_id"], "confirm_amount": "0.05"})
        out = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.01",
                                     "payee": payee, "sandbox_id": sandbox_id, **AGENT})
        self.assertEqual(out["decision"]["code"], "needs_pin",
                         "an allowlisted destination is allowed, and still asks for the PIN")
        self.assertFalse(out.get("auto"))

    def test_an_unattended_autopay_is_the_only_way_past_the_popup(self) -> None:
        payee = xmr_addr(32)
        proposal = self.state.mandates_propose({"kind": "monero-stagenet", "asset": "XMR",
                                                "payee": payee, "per_tx_cap": "0.02",
                                                "period_cap": "0.05", "absolute_cap": "0.1",
                                                "period_hours": 24, "label": "subscription"})
        mandate_id = proposal["mandate"]["mandate_id"]
        self.state.mandates_activate({"mandate_id": mandate_id, "passphrase": PASS})
        params = {"kind": "monero-stagenet", "asset": "XMR", "amount": "0.01", "payee": payee,
                  **AGENT}
        first = self.state.request_tx(dict(params))
        self.assertEqual(first["decision"]["code"], "needs_pin",
                         "an active autopay still asks: only `unattended` skips the popup")
        # the operator's explicit opt-out, which costs the passphrase and the PIN
        self.state.mandates[mandate_id].unattended = True
        second = self.state.request_tx(dict(params))
        self.assertTrue(second.get("auto"), second)
        self.assertEqual(second["request"]["authorised_by"], "mandate")

    # ---------- changing the PIN costs the passphrase ----------
    def test_setting_a_pin_needs_the_passphrase(self) -> None:
        with self.assertRaises(WalletError) as caught:
            self.state.set_pin({"pin": "654321"})
        self.assertEqual(caught.exception.code, "passphrase_required")
        with self.assertRaises(WalletError) as caught:
            self.state.set_pin({"pin": "654321", "passphrase": "wrong-passphrase"})
        self.assertEqual(caught.exception.code, "bad_passphrase")

    def test_setting_a_pin_requires_six_digits(self) -> None:
        with self.assertRaises(WalletError) as caught:
            self.state.set_pin({"pin": "12345", "passphrase": PASS})
        self.assertEqual(caught.exception.code, "bad_pin_format")

    def test_a_new_pin_replaces_the_old_one(self) -> None:
        self.state.set_pin({"pin": "654321", "passphrase": PASS, "confirm": "654321"})
        filed = self._filed()
        with self.assertRaises(WalletError) as caught:
            self.state.approve({"pin": PIN, "request_id": filed["request"]["request_id"],
                                "approval_id": filed["approval_id"]})
        self.assertEqual(caught.exception.code, "bad_pin")
        self.state.approve({"pin": "654321", "request_id": filed["request"]["request_id"],
                            "approval_id": filed["approval_id"]})

    def test_a_wallet_without_a_pin_cannot_approve(self) -> None:
        home = Path(tempfile.mkdtemp())
        state = WalletState(home)
        state.initialize(PASS)          # no pin: the pane asks for one on first open
        self.assertFalse(state.pin_status()["set"])
        state.settings.chains_enabled = list(TEST_CHAINS)
        state.settings.assets_enabled = list(TEST_ASSETS)
        state.adapters = {kind: FakeAdapter() for kind in TEST_CHAINS}
        filed = state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.001",
                                  "payee": xmr_addr(33), **AGENT})
        with self.assertRaises(WalletError) as caught:
            state.approve({"pin": PIN, "request_id": filed["request"]["request_id"],
                           "approval_id": filed["approval_id"]})
        self.assertEqual(caught.exception.code, "pin_not_set")


if __name__ == "__main__":
    unittest.main()


class PinHardeningTests(PinGateTests):
    """The lockout is a limit only if it is atomic and its counter cannot be reset."""

    def test_parallel_wrong_pins_are_all_counted(self) -> None:
        import threading

        filed = self._filed()
        errors: list = []

        def guess() -> None:
            try:
                self.state.approve({"pin": WRONG, "request_id": filed["request"]["request_id"],
                                    "approval_id": filed["approval_id"]})
            except WalletError as exc:
                errors.append(exc.code)

        threads = [threading.Thread(target=guess) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # every guess hit the counter, and the tail of them hit the lockout
        self.assertIn("pin_locked", errors)
        self.assertEqual(self.state.pin_status()["attempts_left"], 0)

    def test_a_hand_edited_counter_is_a_lockout_not_a_reset(self) -> None:
        import json

        filed = self._filed()
        for _ in range(5):
            with self.assertRaises(WalletError):
                self.state.approve({"pin": WRONG, "request_id": filed["request"]["request_id"],
                                    "approval_id": filed["approval_id"]})
        # the attacker's move: put the counter back
        self.state.pin_state_path.write_text(json.dumps({"fails": 0, "locked_until": ""}))
        with self.assertRaises(WalletError) as caught:
            self.state.approve({"pin": PIN, "request_id": filed["request"]["request_id"],
                                "approval_id": filed["approval_id"]})
        self.assertEqual(caught.exception.code, "pin_locked")
