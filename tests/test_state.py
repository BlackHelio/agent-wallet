from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from walletd import config, keystore
from walletd import state as state_module
from walletd.chains.base import ChainAdapter, ChainError, TxPlan
from walletd.policy import Mandate
from walletd.state import State, WalletError

PASS = "test-passphrase-123"
PIN = "123456"   # the per-payment secret; every approval needs it
# The product now defaults to real chains. Tests are explicit about the nets they assert on, so a
# default change never silently rewrites what the suite is proving.
TEST_CHAINS = ["monero-stagenet", "sim-mainnet", "bitcoin-testnet", "base-sepolia"]
TEST_ASSETS = ["XMR", "BTC", "ETH", "POL", "USDC"]
ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


class FakeAdapter(ChainAdapter):
    """In-memory chain double: plans and 'broadcasts' without a network.

    Production binds MoneroAdapter; these tests are about policy, approvals and receipts, so the
    chain step is a fixture rather than the subject.
    """

    kind = "monero-stagenet"
    asset = "XMR"
    supports_accounts = True

    def __init__(self) -> None:
        self.broadcast_count = 0
        self.planned: list = []
        self.fee = Decimal("0.00002")

    def available(self) -> bool:
        return True

    def status(self):
        return {"kind": self.kind, "available": True, "height": 1}

    def address(self) -> str:
        # a valid *stagenet* address: the fixture is bound to monero-stagenet, and a mainnet-shaped
        # address here made every sweep fail validation once sandbox spends were policed
        return "57" + "1" * 93

    def balance(self):
        return Decimal("10")

    def create_account(self, label):
        index = FakeAdapter._next_account
        FakeAdapter._next_account += 1
        FakeAdapter.balances.setdefault(index, Decimal("0"))
        return {"account_index": index, "address": "58" + "2" * 93}

    def account_balance(self, account_index):
        held = FakeAdapter.balances.get(account_index, Decimal("0"))
        return {"balance": held, "unlocked": held}

    def address_for(self, account_index):
        # account 0 is the wallet's own address; a deposit account gets its own
        return self.address() if int(account_index) == 0 else ("58" + "2" * 93)

    def plan_transfer(self, amount, payee, account_index=0, asset=""):
        self.planned.append((amount, payee, account_index))
        return TxPlan(kind=self.kind, asset="XMR", amount=amount, payee=payee,
                      fee=self.fee, raw={"tx_blob": "deadbeef", "tx_hash": "aa" * 32,
                                         "tx_key": "bb" * 32})

    def payment_proof(self, txid, tx_key, address):
        return {"received": False, "amount": "0", "confirmations": 0, "in_pool": False,
                "reason": "transaction not found"}

    def broadcast(self, plan):
        self.broadcast_count += 1
        # model the chain: a transfer out of an account lowers that account's balance, which is what
        # "the sweep emptied it" means to the code that decides whether to close a sandbox
        amount, _payee, account = self.planned[-1]
        held = FakeAdapter.balances.get(account)
        if held is not None:
            FakeAdapter.balances[account] = max(Decimal("0"), held - Decimal(str(amount)))
        return {"tx_hash": plan.raw["tx_hash"], "tx_key": plan.raw["tx_key"],
                "fee": str(plan.fee), "confirmations": 0}

    # sub-accounts (sandboxes)
    supports_accounts = True
    _next_account = 1

    def create_account(self, label):
        index = FakeAdapter._next_account
        FakeAdapter._next_account += 1
        # distinct per account, so a test can tell two purchases apart
        # 95 chars: prefix + version nibble + 93 body — a real stagenet shape, distinct per account
        return {"account_index": index, "address": "5" + str(index) + "1" * 93}

    def accounts(self):
        return [{"account_index": 0, "address": "57" + "1" * 93, "balance": "10", "label": "main"}]

    # a per-account balance, so a sweep can be tested against what is really in the account
    balances: dict = {}

    def balance_at(self, account_index):
        return FakeAdapter.balances.get(account_index, Decimal("0"))

    def set_balance(self, account_index, amount):
        FakeAdapter.balances[account_index] = Decimal(str(amount))

    def mnemonic(self):
        return " ".join(["test"] * 25)


class WalletState(State):
    """State whose adapter map always contains the in-memory chain fixture."""

    def _build_adapters(self) -> None:
        super()._build_adapters()
        self.adapters[self.fake.kind] = self.fake


def xmr_addr(offset: int) -> str:
    body = (ALPHABET[offset:] + ALPHABET[:offset]) * 3
    return "57" + body[:93]


class StateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        FakeAdapter._next_account = 1          # each test starts from a clean account counter
        FakeAdapter.balances = {}
        self.state = WalletState(self.home)
        self.state.fake = FakeAdapter()
        self.state.initialize(PASS, pin=PIN)
        # the PIN gate itself is tested in tests/test_pin.py; these tests are about policy
        # accounting (budgets, fees, sweeps), so they run with the gate off
        self.state.settings.pin_every_payment = False
        # the PIN gate itself is tested in tests/test_pin.py; these tests are about policy
        # accounting (budgets, fees, sweeps), so they run with the gate off and their approvals
        # do not need to type it
        self.state.settings.pin_every_payment = False
        # the prefer-Monero policy ships off now; this suite still covers it, so turn it on here
        self.state.settings.prefer_monero = True
        # pin the test nets: the product defaults to real chains now, and this suite asserts on stagenet
        self._pin_chains()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _pin_chains(self) -> None:
        """Write the test chain list into the *signed* settings.

        `unlock()` reloads settings from disk, so assigning to the in-memory object is not enough —
        the first unlock would put the product defaults back and every request would be denied.
        """
        from walletd import config as config_mod

        self.state.settings.chains_enabled = list(TEST_CHAINS)
        self.state.settings.assets_enabled = list(TEST_ASSETS)
        config_mod.save(self.state.config_path, self.state.settings, self.state.master)

    def _chain(self) -> FakeAdapter:
        return self.state.fake

    def _approve(self, request_id: str, approval_id: str, amount: str | None = None) -> dict:
        params = {"request_id": request_id, "approval_id": approval_id, "passphrase": PASS,
                  "pin": PIN}
        if amount is not None:
            params["confirm_amount"] = amount
        return self.state.approve(params)

    # ---------- basics ----------

    def test_initialize_writes_keystore_and_ledger(self) -> None:
        self.assertTrue((self.home / "keystore.json").exists())
        self.assertTrue((self.home / "ledger.jsonl").exists())
        ok, message, count = self.state.ledger.verify()
        self.assertTrue(ok, message)
        self.assertEqual(count, 1)

    def test_locked_wallet_refuses_to_spend(self) -> None:
        self.state.lock()
        result = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                        "amount": "0.01", "payee": xmr_addr(1)})
        self.assertFalse(result["decision"]["allow"])
        self.assertEqual(result["decision"]["code"], "unattended_no_operator")

    def test_lock_clears_material(self) -> None:
        self.state.lock()
        self.assertIsNone(self.state.master)
        self.assertIsNone(self.state.payload)
        self.assertFalse(self.state.is_unlocked())

    def test_unlock_rejects_wrong_passphrase(self) -> None:
        with self.assertRaises(WalletError) as caught:
            self.state.unlock("not-the-passphrase")
        self.assertEqual(caught.exception.code, "bad_passphrase")

    def test_six_digits_unlock_through_the_passphrase_path(self) -> None:
        """The pane used to send the PIN as a passphrase; six digits can only be a PIN.

        The passphrase has an eight-character minimum, so a six-digit secret can never match one.
        Without this, an older desktop bundle put the PIN in the passphrase field and the daemon
        answered "passphrase does not match the keystore" — the wallet could not be opened at all.
        """
        self.state.lock()
        self.state.unlock(PIN)
        self.assertTrue(self.state.is_unlocked())

    def test_six_digits_that_are_not_the_pin_still_fail(self) -> None:
        self.state.lock()
        with self.assertRaises(WalletError) as caught:
            self.state.unlock("999999")
        self.assertEqual(caught.exception.code, "bad_pin")

    def test_orders_purge_forgets_only_orders_that_received_nothing(self) -> None:
        """Phantom orders come back from archived receipts; only empty ones may be forgotten.

        `receive_address` writes a receipt per address and archiving keeps the chain, so a wallet
        used for testing listed 11 phantom orders forever — even after the ledger was archived.
        """
        self.state.unlock(PASS)
        fake = {"orders": [
            {"order_id": "ord_empty", "received": "0", "last_tx": "sim0000"},
            {"order_id": "ord_paid", "received": "0.5", "last_tx": "tx1"},
        ]}
        original = self.state.incoming
        self.state.incoming = lambda: fake          # no chain needed to exercise the rule
        try:
            out = self.state.orders_purge({})
        finally:
            self.state.incoming = original
        self.assertEqual(out["purged"], ["ord_empty"])
        self.assertEqual(out["kept"], ["ord_paid"])
        self.assertIn("ord_empty", self.state._purged_orders())
        self.assertNotIn("ord_paid", self.state._purged_orders())

    def test_requests_purge_keeps_what_is_still_open(self) -> None:
        """Purging forgets dead requests only: one still pending has to survive.

        The pane's Orders list is gone, but the rows were still there and showed up in a fresh
        install's history. Anything pending, approved or carrying a tx hash is kept, so a purge can
        never erase a payment that happened or one still in flight.
        """
        self.state.unlock(PASS)
        filed = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                       "amount": "0.01", "payee": xmr_addr(3)})
        rid = filed["request"]["request_id"]
        self.assertEqual(self.state.requests_purge({})["purged"], [])
        self.assertIn(rid, self.state.requests)
        self.state.cancel_request({"request_id": rid})
        self.assertEqual(self.state.requests_purge({})["purged"], [rid])
        self.assertNotIn(rid, self.state.requests)

    # ---------- manual approval path ----------

    def test_manual_flow_approve(self) -> None:
        self.state.unlock(PASS)
        addr = xmr_addr(2)
        result = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                        "amount": "0.01", "payee": addr, "why": "api call"})
        self.assertTrue(result["decision"]["allow"])
        approval_id = result["approval_id"]
        approved = self._approve(result["request"]["request_id"], approval_id)
        self.assertEqual(approved["request"]["status"], "sent")
        self.assertTrue(approved["signed"])
        self.assertEqual(len(approved["tx_hash"]), 64)
        ok, message, _ = self.state.ledger.verify()
        self.assertTrue(ok, message)

    def test_approval_replay_refused(self) -> None:
        self.state.unlock(PASS)
        result = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                        "amount": "0.01", "payee": xmr_addr(3)})
        payload = {"request_id": result["request"]["request_id"], "approval_id": result["approval_id"]}
        self._approve(payload["request_id"], payload["approval_id"])
        with self.assertRaises(WalletError) as caught:
            self._approve(payload["request_id"], payload["approval_id"])
        self.assertEqual(caught.exception.code, "approval_replayed")

    def test_amount_confirmation_required_above_threshold(self) -> None:
        self.state.unlock(PASS)
        result = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                        "amount": "0.2", "payee": xmr_addr(4)})
        base = {"request_id": result["request"]["request_id"], "approval_id": result["approval_id"]}
        with self.assertRaises(WalletError) as caught:
            self._approve(base["request_id"], base["approval_id"])
        self.assertEqual(caught.exception.code, "confirm_amount_required")
        with self.assertRaises(WalletError) as caught:
            self._approve(base["request_id"], base["approval_id"], "0.02")
        self.assertEqual(caught.exception.code, "confirm_amount_mismatch")
        self.assertIn("0.2", caught.exception.message)
        approved = self._approve(base["request_id"], base["approval_id"], "0.2")
        self.assertEqual(approved["request"]["status"], "sent")

    def test_a_typed_amount_is_checked_below_the_threshold_too(self) -> None:
        """Approving 0.01 while the human typed 0.02 must not send 0.01.

        Below the approval threshold the typed amount is optional, so the old code skipped the
        comparison entirely: the operator's number was decoration and the request's number went out.
        """
        self.state.unlock(PASS)
        result = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                        "amount": "0.01", "payee": xmr_addr(11)})
        self.assertLessEqual(result["request"]["amount"], str(self.state.settings.approval_threshold))
        with self.assertRaises(WalletError) as caught:
            self._approve(result["request"]["request_id"], result["approval_id"], "0.02")
        self.assertEqual(caught.exception.code, "confirm_amount_mismatch")
        # nothing was signed by the refused approval
        self.assertEqual(self.state.requests[result["request"]["request_id"]].status, "pending")
        approved = self._approve(result["request"]["request_id"], result["approval_id"], "0.01")
        self.assertEqual(approved["request"]["status"], "sent")

    def test_a_new_autopay_has_full_headroom_despite_earlier_payments(self) -> None:
        """Creating an autopay must not count history that predates it.

        The period/lifetime totals used to be taken over all time, so a payee that had been paid
        manually looked exhausted the moment an autopay was granted for it: 0 of 0.01 left, and the
        first payment the autopay existed for was denied by the cap it had just been given.
        """
        self.state.unlock(PASS)
        payee = xmr_addr(21)
        # pay this payee twice by hand first: 0.1 + 0.1, all of it before any mandate exists
        for _ in range(2):
            filed = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                           "amount": "0.1", "payee": payee})
            self._approve(filed["request"]["request_id"], filed["approval_id"], "0.1")
        proposal = self.state.mandates_propose({
            "kind": "monero-stagenet", "asset": "XMR", "payee": payee,
            "per_tx_cap": "0.05", "period_cap": "0.1", "absolute_cap": "0.2",
            "period_hours": 24, "label": "vendor retainer"})
        mandate_id = proposal["mandate"]["mandate_id"]
        self.assertEqual(proposal["mandate"]["status"], "proposed")
        activated = self.state.mandates_activate({"mandate_id": mandate_id, "passphrase": PASS})
        self.assertEqual(activated["mandate"]["status"], "active")
        inside = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                       "amount": "0.04", "payee": payee})
        self.assertEqual(inside["decision"]["code"], "mandate_ok")
        self.assertNotIn("approval_id", inside)   # no human needed: the mandate covered it

    def test_a_revoked_sandbox_says_revoked_not_unfunded(self) -> None:
        self.state.unlock(PASS)
        made = self.state.sandbox_create({"kind": "monero-stagenet", "asset": "XMR",
                                          "budget": "0.02", "passphrase": PASS, "label": "revoke me"})
        sandbox_id = made["sandbox"]["sandbox_id"]
        funding = made["funding"]["request"]
        self._approve(funding["request_id"], made["funding"]["approval_id"], "0.02")
        revoked = self.state.sandbox_revoke({"sandbox_id": sandbox_id, "passphrase": PASS})
        self.assertEqual(revoked["sandbox"]["status"], "revoked")
        self.assertTrue(revoked["sandbox"]["revoked_at"])
        # the money is still visible: it did not vanish with the sandbox's funding state
        self.assertGreater(float(revoked["sandbox"]["remaining"]), 0.0)

    def test_a_message_only_prompt_is_not_an_unlock(self) -> None:
        self.state.unlock(PASS)
        out = self.state.request_operator_prompt({"message": "I need 0.3 XMR for the invoice"})
        self.assertEqual(out["prompt"]["kind"], "note")
        explicit = self.state.request_operator_prompt({"kind": "unlock"})
        self.assertEqual(explicit["prompt"]["kind"], "unlock")

    def test_a_request_can_be_denied_with_only_its_id(self) -> None:
        """Refusing a payment must not require the approval token."""
        self.state.unlock(PASS)
        filed = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                       "amount": "0.01", "payee": xmr_addr(12)})
        request_id = filed["request"]["request_id"]
        denied = self.state.deny({"request_id": request_id, "reason": "not now"})
        self.assertEqual(denied["request"]["status"], "denied")
        # the live approval for it is burned, so a stale card cannot approve it afterwards
        with self.assertRaises(WalletError):
            self._approve(request_id, filed["approval_id"], "0.01")

    def test_the_typed_amount_threshold_is_per_coin(self) -> None:
        """One scalar for six coins makes the guard unreachable (BTC) or constant noise (XRP)."""
        self.assertLessEqual(self.state.settings.threshold("BTC"),
                             self.state.settings.cap(self.state.settings.per_tx_cap, "BTC"))
        self.assertEqual(self.state.settings.threshold("BTC"), Decimal("0.001"))
        self.assertEqual(self.state.settings.threshold("XMR"), Decimal("0.02"))
        # an explicit override wins
        self.state.settings.approval_thresholds = {"XMR": "0.5"}
        self.assertEqual(self.state.settings.threshold("XMR"), Decimal("0.5"))

    def test_a_request_says_whether_the_amount_must_be_typed(self) -> None:
        self.state.unlock(PASS)
        small = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                       "amount": "0.01", "payee": xmr_addr(31)})
        self.assertFalse(small["confirm_amount_required"])
        self.assertFalse(small["request"]["confirm_amount_required"])
        big = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                     "amount": "0.5", "payee": xmr_addr(32)})
        # 0.5 is above the per-tx cap, so this one is denied by policy, not typed: use one inside it
        if not big["decision"]["allow"]:
            big = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                         "amount": "0.05", "payee": xmr_addr(33)})
        self.assertTrue(big["confirm_amount_required"])

    def test_readiness_answers_go_or_no_go_without_sending(self) -> None:
        self.state.unlock(PASS)
        before = self.state.adapters["monero-stagenet"].broadcast_count
        result = self.state.readiness({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.01"})
        self.assertEqual(result["verdict"], "go", result["blocking"])
        checks = {c["check"]: c for c in result["checks"]}
        self.assertTrue(checks["keystore unlocked"]["ok"])
        self.assertTrue(checks["human gate"]["ok"])
        self.assertTrue(checks["node reachable"]["ok"])
        self.assertTrue(checks["transaction builds"]["ok"])
        self.assertEqual(self.state.adapters["monero-stagenet"].broadcast_count, before)
        # asking for more than the fixture holds is a NO_GO with the reason named
        poor = self.state.readiness({"kind": "monero-stagenet", "asset": "XMR", "amount": "50"})
        self.assertEqual(poor["verdict"], "no_go")
        self.assertIn("enough money", poor["blocking"])

    def test_per_tx_cap_denied(self) -> None:
        self.state.unlock(PASS)
        result = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                        "amount": "5", "payee": xmr_addr(5)})
        self.assertFalse(result["decision"]["allow"])
        self.assertEqual(result["decision"]["code"], "per_tx_cap")
        last = self.state.ledger.last()
        self.assertEqual(last["decision"], "denied")

    def test_invalid_payee_denied(self) -> None:
        self.state.unlock(PASS)
        result = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                        "amount": "0.01", "payee": "not-an-address"})
        self.assertFalse(result["decision"]["allow"])
        self.assertEqual(result["decision"]["code"], "invalid_payee")

    def test_disabled_chain_denied(self) -> None:
        self.state.unlock(PASS)
        self.state.settings.chains_enabled = ["monero-stagenet"]
        result = self.state.request_tx({"kind": "base-sepolia", "asset": "USDC",
                                        "amount": "0.1", "payee": "0x" + "ab" * 20})
        self.assertFalse(result["decision"]["allow"])
        self.assertEqual(result["decision"]["code"], "chain_not_enabled")

    # ---------- mandates ----------

    def _mandate(self, addr: str) -> dict:
        proposal = self.state.mandates_propose({
            "kind": "monero-stagenet", "asset": "XMR", "payee": addr,
            "per_tx_cap": "0.2", "period_cap": "0.3", "absolute_cap": "1",
            "period_hours": 24, "label": "api vendor",
        })
        return proposal["mandate"]

    def test_mandate_proposal_is_inert_until_activated(self) -> None:
        addr = xmr_addr(7)
        mandate = self._mandate(addr)
        # a proposal that was never switched on is "proposed"; "revoked" is reserved for one the
        # operator activated and later turned off
        self.assertEqual(mandate["status"], "proposed")
        result = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                        "amount": "0.1", "payee": addr})
        self.assertIn("approval_id", result)   # manual path, not auto

    def test_activated_mandate_auto_approves_then_hits_period_cap(self) -> None:
        self.state.unlock(PASS)
        addr = xmr_addr(8)
        mandate = self._mandate(addr)
        self.state.mandates_activate({"mandate_id": mandate["mandate_id"], "passphrase": PASS})

        first = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.15", "payee": addr})
        self.assertTrue(first.get("auto"), first)
        self.assertEqual(first["request"]["status"], "sent")
        self.assertTrue(first["signed"])

        second = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.2", "payee": addr})
        self.assertFalse(second["decision"]["allow"])
        self.assertEqual(second["decision"]["code"], "period_cap")

    def test_mandate_requires_exact_payee(self) -> None:
        self.state.unlock(PASS)
        addr = xmr_addr(9)
        mandate = self._mandate(addr)
        self.state.mandates_activate({"mandate_id": mandate["mandate_id"], "passphrase": PASS})
        lookalike = xmr_addr(10)
        result = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                        "amount": "0.1", "payee": lookalike})
        self.assertFalse(result.get("auto"))
        self.assertIn("approval_id", result)

    def test_revoked_mandate_stops_auto_pay(self) -> None:
        self.state.unlock(PASS)
        addr = xmr_addr(11)
        mandate = self._mandate(addr)
        self.state.mandates_activate({"mandate_id": mandate["mandate_id"], "passphrase": PASS})
        self.state.mandates_revoke({"mandate_id": mandate["mandate_id"], "reason": "vendor changed"})
        result = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                        "amount": "0.1", "payee": addr})
        self.assertFalse(result.get("auto"))
        self.assertIn("approval_id", result)

    def test_mandate_cannot_exceed_lifetime_horizon(self) -> None:
        with self.assertRaises(ValueError):
            Mandate.parse({"kind": "monero-stagenet", "asset": "XMR", "payee": xmr_addr(12),
                           "per_tx_cap": "0.1", "period_cap": "0.1", "absolute_cap": "0.1",
                           "expires_at": "2099-01-01T00:00:00Z"}, self.state.settings)

    # ---------- unattended ----------

    def test_unattended_run_denies_with_receipt(self) -> None:
        self.state.unlock(PASS)
        result = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                        "amount": "0.01", "payee": xmr_addr(13)}, unattended=True)
        self.assertFalse(result["decision"]["allow"])
        self.assertEqual(result["decision"]["code"], "unattended_no_operator")
        self.assertEqual(self.state.ledger.last()["decision"], "denied")

    # ---------- chain execution ----------

    def test_chain_error_denies_before_approval(self) -> None:
        from walletd.chains.base import ChainError

        class Broke(FakeAdapter):
            def plan_transfer(self, amount, payee, account_index=0, asset=""):
                raise ChainError("insufficient_funds", "not enough unlocked XMR")

        self.state.unlock(PASS)
        self.state.adapters["monero-stagenet"] = Broke()
        result = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                        "amount": "0.01", "payee": xmr_addr(30)})
        self.assertFalse(result["decision"]["allow"])
        self.assertEqual(result["decision"]["code"], "insufficient_funds")
        # no money yet is not a refusal: the request stays retryable, and the receipt says so instead
        # of reading like a human said no
        self.assertEqual(result["decision"]["retryable"], True)
        self.assertEqual(self.state.ledger.last()["decision"], "deferred")

    def test_broadcast_failure_is_recorded(self) -> None:
        from walletd.chains.base import ChainError
        from walletd.state import WalletError

        class Flaky(FakeAdapter):
            def broadcast(self, plan):
                raise ChainError("rpc_error", "relay_tx failed")

        self.state.unlock(PASS)
        self.state.adapters["monero-stagenet"] = Flaky()
        result = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                        "amount": "0.01", "payee": xmr_addr(31)})
        with self.assertRaises(WalletError) as caught:
            self._approve(result["request"]["request_id"], result["approval_id"])
        self.assertEqual(caught.exception.code, "broadcast_failed")
        self.assertEqual(self.state.ledger.last()["details"]["code"], "rpc_error")


    # ---------- the promise: no passphrase, no transaction ----------

    def _pending(self, amount: str = "0.001") -> dict:
        """A request an agent filed and a human has not approved yet."""
        self.state.unlock(PASS)
        return self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                      "amount": amount, "payee": xmr_addr(70),
                                      "why": "test payment"})

    def test_a_locked_wallet_cannot_sign_anything(self) -> None:
        """The physical guarantee: locked means no key in memory, so nothing can be broadcast."""
        pending = self._pending()
        request_id = pending["request"]["request_id"]
        approval_id = pending["approval_id"]
        before = self.state.fake.broadcast_count
        self.state.lock()
        with self.assertRaises(WalletError) as caught:
            self._approve(request_id, approval_id, "0.001")
        self.assertIn(caught.exception.code, ("locked", "passphrase_required"))
        self.assertEqual(self.state.fake.broadcast_count, before, "a locked wallet signed a transfer")

    def test_approval_without_the_passphrase_never_reaches_the_signer(self) -> None:
        pending = self._pending()
        before = self.state.fake.broadcast_count
        for passphrase in ("", "not-the-passphrase"):
            with self.assertRaises(WalletError) as caught:
                self.state.approve({"pin": PIN, "request_id": pending["request"]["request_id"],
                                    "approval_id": pending["approval_id"],
                                    "passphrase": passphrase, "confirm_amount": "0.001"})
            self.assertEqual(caught.exception.code, "passphrase_required")
        self.assertEqual(self.state.fake.broadcast_count, before)
        self.assertEqual(self.state.requests[pending["request"]["request_id"]].authorised_by, "")

    def test_broadcast_refuses_a_request_nobody_authorised(self) -> None:
        """Defence in depth: even a direct call to the signing step refuses an unauthorised request."""
        pending = self._pending()
        req = self.state.requests[pending["request"]["request_id"]]
        plan = self.state.plans.get(req.request_id)
        before = self.state.fake.broadcast_count
        with self.assertRaises(WalletError) as caught:
            self.state._broadcast(req, plan, self.state.fake)
        self.assertEqual(caught.exception.code, "not_authorised")
        self.assertEqual(self.state.fake.broadcast_count, before)

    def test_authorised_by_records_which_authority_signed(self) -> None:
        pending = self._pending()
        self._approve(pending["request"]["request_id"], pending["approval_id"], "0.001")
        self.assertEqual(self.state.requests[pending["request"]["request_id"]].authorised_by,
                         "passphrase")

    def test_a_mandate_spend_still_needs_an_unlocked_wallet(self) -> None:
        """Standing authority is not a key: with the wallet locked there is nothing to sign with."""
        self.state.unlock(PASS)
        created = self.state.mandates_create({"kind": "monero-stagenet", "asset": "XMR",
                                             "payee": xmr_addr(71), "per_tx_cap": "0.01",
                                             "period_cap": "0.05", "absolute_cap": "0.05",
                                             "interval": "24h", "passphrase": PASS})
        self.assertEqual(created["mandate"]["status"], "active")
        before = self.state.fake.broadcast_count
        self.state.lock()
        out = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.001",
                                     "payee": xmr_addr(71)})
        self.assertFalse(out.get("auto"), out)
        self.assertEqual(self.state.fake.broadcast_count, before,
                         "a mandate signed a payment while the wallet was locked")
        self.assertEqual((out.get("request") or {}).get("tx_hash", ""), "",
                         "a locked mandate spend produced a transaction hash")

    def test_the_gate_off_still_requires_an_unlocked_wallet(self) -> None:
        """Turning the prompt off removes the approval, not the key: a locked daemon still cannot sign."""
        self.state.unlock(PASS)
        self.state.set_human_gate({"enabled": False, "passphrase": PASS, "confirm": True})
        pending = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                         "amount": "0.001", "payee": xmr_addr(72)})
        before = self.state.fake.broadcast_count
        self.state.lock()
        with self.assertRaises(WalletError):
            self.state.approve({"pin": PIN, "request_id": pending["request"]["request_id"],
                                "approval_id": pending["approval_id"]})
        self.assertEqual(self.state.fake.broadcast_count, before)

    # ---------- sandboxes ----------

    def _funded_sandbox(self, budget: str = "0.2") -> dict:
        self.state.unlock(PASS)
        created = self.state.sandbox_create({"kind": "monero-stagenet", "asset": "XMR",
                                            "budget": budget, "lifetime_hours": 24,
                                            "label": "test sandbox",
                                            # these tests are about budgets, not destinations
                                            "allowed_payees": [xmr_addr(n) for n in range(1, 100)]})
        funding = created["funding"]
        self._approve(funding["request"]["request_id"], funding["approval_id"], budget)
        return self.state.sandboxes[created["sandbox"]["sandbox_id"]].to_dict()

    def test_sandbox_is_unfunded_until_the_transfer_is_approved(self) -> None:
        self.state.unlock(PASS)
        created = self.state.sandbox_create({"kind": "monero-stagenet", "asset": "XMR",
                                            "budget": "0.2", "lifetime_hours": 24})
        self.assertEqual(created["sandbox"]["status"], "unfunded")
        spend = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.05",
                                       "payee": xmr_addr(40), "sandbox_id": created["sandbox"]["sandbox_id"]})
        self.assertFalse(spend["decision"]["allow"])
        self.assertEqual(spend["decision"]["code"], "sandbox_unfunded")

    def test_sandbox_spends_without_approval_and_stops_at_the_budget(self) -> None:
        sandbox = self._funded_sandbox("0.2")
        self.assertEqual(sandbox["status"], "active")
        self.assertTrue(self.state.sandboxes[sandbox["sandbox_id"]].enabled)

        first = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.15",
                                       "payee": xmr_addr(41), "sandbox_id": sandbox["sandbox_id"]})
        self.assertTrue(first.get("auto"), first)
        self.assertEqual(first["decision"]["rule"], "sandbox")
        # 0.15 + the chain's fee (0.00002): the fee leaves the same account, so it leaves the budget
        self.assertEqual(self.state.sandboxes[sandbox["sandbox_id"]].spent, Decimal("0.15002"))

        over = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.15",
                                      "payee": xmr_addr(42), "sandbox_id": sandbox["sandbox_id"]})
        self.assertFalse(over["decision"]["allow"])
        self.assertEqual(over["decision"]["code"], "sandbox_budget")

    # ---------- one-time sandboxes destroy themselves ----------

    def _one_time(self, budget: str = "0.2", destruct: int = 60) -> dict:
        self.state.unlock(PASS)
        created = self.state.sandbox_create({"kind": "monero-stagenet", "asset": "XMR",
                                             "budget": budget, "lifetime_hours": 24,
                                             "mode": "one_time", "self_destruct_seconds": destruct,
                                             "label": "single payment",
                                             "allowed_payees": [xmr_addr(n) for n in range(1, 100)]})
        funding = created["funding"]
        self._approve(funding["request"]["request_id"], funding["approval_id"], budget)
        return self.state.sandboxes[created["sandbox"]["sandbox_id"]].to_dict()

    def test_one_time_sandbox_takes_one_payment_and_arms_the_clock(self) -> None:
        sandbox = self._one_time("0.2")
        first = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.1",
                                       "payee": xmr_addr(50), "sandbox_id": sandbox["sandbox_id"]})
        self.assertTrue(first.get("auto"), first)
        live = self.state.sandboxes[sandbox["sandbox_id"]]
        self.assertTrue(live.destroy_at, "the clock must start when the payment settles")
        self.assertEqual(live.spent, Decimal("0.10002"))     # amount + the payment's fee
        self.assertLessEqual(live.destroy_in(), 60)

        second = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.05",
                                        "payee": xmr_addr(51), "sandbox_id": sandbox["sandbox_id"]})
        self.assertFalse(second["decision"]["allow"])
        self.assertEqual(second["decision"]["code"], "sandbox_one_time_used")

    def test_budget_sandbox_never_arms_a_destruct_clock(self) -> None:
        sandbox = self._funded_sandbox("0.2")
        self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.05",
                               "payee": xmr_addr(52), "sandbox_id": sandbox["sandbox_id"]})
        self.assertEqual(self.state.sandboxes[sandbox["sandbox_id"]].destroy_at, "")
        self.assertEqual(self.state.reap_sandboxes(), [])

    def test_self_destruct_waits_while_the_sandbox_still_holds_funds(self) -> None:
        """The single most important guard: keys go last, money goes home first."""
        sandbox = self._one_time("0.2")
        self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.1",
                               "payee": xmr_addr(53), "sandbox_id": sandbox["sandbox_id"]})
        live = self.state.sandboxes[sandbox["sandbox_id"]]
        live.destroy_at = "2000-01-01T00:00:00Z"
        self.state.fake.balance_at = lambda index: Decimal("0.15")   # still funded
        outcome = self.state.reap_sandboxes()
        self.assertFalse(outcome[0]["destroyed"], outcome)
        live = self.state.sandboxes[sandbox["sandbox_id"]]
        self.assertEqual(live.destroyed_at, "")
        self.assertEqual(live.destroy_blocked, "sweep_pending")
        self.assertGreater(self.state.fake.broadcast_count, 0, "it should try to sweep before giving up")

    def test_self_destruct_sweeps_then_destroys_and_records_it(self) -> None:
        sandbox = self._one_time("0.2")
        self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.1",
                               "payee": xmr_addr(54), "sandbox_id": sandbox["sandbox_id"]})
        live = self.state.sandboxes[sandbox["sandbox_id"]]
        live.destroy_at = "2000-01-01T00:00:00Z"
        self.state.fake.balance_at = lambda index: Decimal("0")
        outcome = self.state.reap_sandboxes()
        self.assertTrue(outcome[0]["destroyed"], outcome)
        live = self.state.sandboxes[sandbox["sandbox_id"]]
        self.assertEqual(live.status(), "destroyed")
        self.assertFalse(live.enabled)
        receipts = [e for e in self.state.ledger.entries()
                    if e["kind"] == "sandbox" and e["decision"] == "destroyed"]
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["details"]["sandbox_id"], sandbox["sandbox_id"])
        self.assertIn("keys", receipts[0]["details"])
        # a destroyed sandbox cannot be spent from again
        after = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.01",
                                       "payee": xmr_addr(55), "sandbox_id": sandbox["sandbox_id"]})
        self.assertFalse(after["decision"]["allow"])

    def test_failed_sweep_keeps_the_sandbox_alive(self) -> None:
        sandbox = self._one_time("0.2")
        self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.1",
                               "payee": xmr_addr(56), "sandbox_id": sandbox["sandbox_id"]})
        live = self.state.sandboxes[sandbox["sandbox_id"]]
        live.destroy_at = "2000-01-01T00:00:00Z"
        self.state.fake.balance_at = lambda index: Decimal("0.15")

        def broke(*args, **kwargs):
            raise ChainError("rpc_unreachable", "node is down")

        self.state.fake.plan_transfer = broke
        outcome = self.state.reap_sandboxes()
        self.assertFalse(outcome[0]["destroyed"], outcome)
        live = self.state.sandboxes[sandbox["sandbox_id"]]
        self.assertEqual(live.destroyed_at, "")
        self.assertTrue(live.destroy_blocked.startswith("sweep_failed"), live.destroy_blocked)

    def test_reaper_does_nothing_while_locked(self) -> None:
        sandbox = self._one_time("0.2")
        self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.1",
                               "payee": xmr_addr(57), "sandbox_id": sandbox["sandbox_id"]})
        self.state.sandboxes[sandbox["sandbox_id"]].destroy_at = "2000-01-01T00:00:00Z"
        self.state.lock()
        self.assertEqual(self.state.reap_sandboxes(), [])
        self.assertEqual(self.state.sandboxes[sandbox["sandbox_id"]].destroyed_at, "")

    def test_revoked_sandbox_stops_spending(self) -> None:
        sandbox = self._funded_sandbox("0.2")
        live = self.state.sandboxes[sandbox["sandbox_id"]]
        self.state.fake.set_balance(live.account_index, "0.15")
        self.state.sandbox_revoke({"sandbox_id": sandbox["sandbox_id"]})
        spend = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.01",
                                       "payee": xmr_addr(43), "sandbox_id": sandbox["sandbox_id"]})
        self.assertFalse(spend["decision"]["allow"])
        self.assertEqual(spend["decision"]["code"], "sandbox_closed")

    def test_sandbox_profile_acl(self) -> None:
        self.state.unlock(PASS)
        created = self.state.sandbox_create({"kind": "monero-stagenet", "asset": "XMR",
                                             "budget": "0.2", "lifetime_hours": 24,
                                             "allowed_profiles": ["wallet"]})
        sandbox_id = created["sandbox"]["sandbox_id"]
        self.assertEqual(created["sandbox"]["allowed_profiles"], ["wallet"])
        blocked = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.01",
                                         "payee": xmr_addr(50), "sandbox_id": sandbox_id,
                                         "profile": "default"})
        self.assertFalse(blocked["decision"]["allow"])
        self.assertEqual(blocked["decision"]["code"], "sandbox_profile")

    def test_sandbox_profile_acl_allows_the_named_profile_after_funding(self) -> None:
        self.state.unlock(PASS)
        created = self.state.sandbox_create({"kind": "monero-stagenet", "asset": "XMR",
                                             "budget": "0.2", "lifetime_hours": 24,
                                             "allowed_profiles": ["wallet"],
                                             "allowed_payees": [xmr_addr(n) for n in range(1, 100)]})
        sandbox_id = created["sandbox"]["sandbox_id"]
        funding = created["funding"]
        self._approve(funding["request"]["request_id"], funding["approval_id"], "0.2")
        allowed = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.05",
                                         "payee": xmr_addr(51), "sandbox_id": sandbox_id,
                                         "profile": "wallet"})
        self.assertTrue(allowed.get("auto"), allowed)

    # ---------- recovery ----------

    def test_seed_export_needs_confirm_and_passphrase(self) -> None:
        from walletd.state import WalletError

        self.state.unlock(PASS)
        with self.assertRaises(WalletError) as caught:
            self.state.export_seed({"passphrase": PASS, "kind": "monero-stagenet"})
        self.assertEqual(caught.exception.code, "confirm_required")

        with self.assertRaises(WalletError) as caught:
            self.state.export_seed({"passphrase": "wrong", "confirm": True, "kind": "monero-stagenet"})
        self.assertEqual(caught.exception.code, "bad_passphrase")

        result = self.state.export_seed({"passphrase": PASS, "confirm": True, "kind": "monero-stagenet"})
        self.assertEqual(result["words"], 25)
        self.assertNotIn("test test", str(self.state.ledger_tail({"limit": 1})))

    def test_backup_roundtrip(self) -> None:
        from walletd import backup

        self.state.unlock(PASS)
        out = Path(self.tmp.name) / "backup.json"
        created = backup.create(self.home, out, "backup-passphrase")
        self.assertTrue(created["ok"])
        self.assertIn("keystore.json", created["files"])

        target = Path(self.tmp.name) / "restored"
        restored = backup.restore(out, target, "backup-passphrase")
        self.assertIn("keystore.json", restored["restored"])

        from walletd import keystore
        payload = keystore.open_sealed(keystore.load(target / "keystore.json"), PASS)
        self.assertIn("master", payload)

    def test_backup_refuses_wrong_passphrase(self) -> None:
        from walletd import backup, keystore

        self.state.unlock(PASS)
        out = Path(self.tmp.name) / "backup.json"
        backup.create(self.home, out, "right-passphrase")
        with self.assertRaises(keystore.InvalidPassphrase):
            backup.restore(out, Path(self.tmp.name) / "x", "wrong-passphrase")


    def test_a_funded_sandbox_blocks_removing_its_coin(self) -> None:
        """Removing the coin hides the adapter, and an unswept budget would be stranded forever."""
        sandbox = self._funded_sandbox("0.2")
        live = self.state.sandboxes[sandbox["sandbox_id"]]
        self.state.fake.set_balance(live.account_index, "0.2")
        with self.assertRaises(WalletError) as caught:
            self.state.config_set({"assets_enabled": ["BTC"]})
        self.assertEqual(caught.exception.code, "sandbox_active")
        self.assertIn("XMR", self.state.settings.assets_enabled)

    def test_sandbox_sweep_returns_the_remainder_and_closes(self) -> None:
        sandbox = self._funded_sandbox("0.2")
        self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.05",
                               "payee": xmr_addr(60), "sandbox_id": sandbox["sandbox_id"]})
        # the account really holds the 0.15 that is left: the sweep reads the chain, not the books
        live = self.state.sandboxes[sandbox["sandbox_id"]]
        self.state.fake.set_balance(live.account_index, "0.15")
        result = self.state.sandbox_sweep({"sandbox_id": sandbox["sandbox_id"]})
        # 0.15 minus the chain's *actual* fee (0.00002), not the fee_cap ceiling: reserving the cap
        # tied up 0.0005 in every sandbox and destroyed it on teardown
        self.assertEqual(Decimal(result["swept"]), Decimal("0.14998"))
        after = self.state.sandboxes[sandbox["sandbox_id"]]
        self.assertFalse(after.enabled)
        self.assertEqual(after.spent, after.budget)
        self.assertTrue(self.state.ledger.verify()[0])


    # ---------- second-review regressions ----------

    def test_a_partial_sweep_does_not_close_the_sandbox(self) -> None:
        """Only an account that is actually empty gets closed; otherwise the rest is stranded."""
        sandbox = self._funded_sandbox("0.2")
        live = self.state.sandboxes[sandbox["sandbox_id"]]
        # the account keeps its money: the fake does not model the transfer out in this test
        self.state.fake.balance_at = lambda index: Decimal("0.15")
        self.state.sandbox_sweep({"sandbox_id": sandbox["sandbox_id"]})
        after = self.state.sandboxes[sandbox["sandbox_id"]]
        self.assertTrue(after.enabled, "a sweep of 0.05 must not close a 0.15 account")
        partial = [e for e in self.state.ledger.entries()
                   if e["kind"] == "sandbox" and e["decision"] == "swept_partial"]
        self.assertEqual(len(partial), 1)
        self.assertEqual(partial[0]["details"]["left"], "0.15")

    def test_a_sweep_is_not_policed_as_a_spend(self) -> None:
        """A tightened per-tx cap must not be able to wedge a teardown for ever."""
        sandbox = self._funded_sandbox("0.2")
        live = self.state.sandboxes[sandbox["sandbox_id"]]
        self.state.fake.set_balance(live.account_index, "0.2")
        self.state.config_set({"per_tx_cap": {"XMR": "0.01", "BTC": "0.001", "ETH": "0.05", "USDC": "25"},
                               "passphrase": PASS})
        result = self.state.sandbox_sweep({"sandbox_id": sandbox["sandbox_id"]})
        self.assertIn("swept", result)
        self.assertEqual(result["sweep_rule"], "sweep")      # asserts the key exists, no fallback

    def test_deny_still_works_after_the_approval_window(self) -> None:
        pending = self._pending()
        approval = self.state.approvals[pending["approval_id"]]
        approval.expires_at = datetime(2000, 1, 1, tzinfo=timezone.utc)
        self.state.deny({"request_id": pending["request"]["request_id"],
                         "approval_id": pending["approval_id"], "reason": "too late"})
        self.assertEqual(self.state.requests[pending["request"]["request_id"]].status, "denied")

    def test_adding_a_new_chain_to_a_contact_needs_the_passphrase(self) -> None:
        """A new chain on an existing contact is an address the human has never seen."""
        self.state.contact_save({"name": "Aiden", "addresses": {"monero-stagenet": xmr_addr(90)}})
        with self.assertRaises(WalletError) as caught:
            self.state.contact_save({"name": "Aiden",
                                     "addresses": {"bitcoin-mainnet": "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"}})
        self.assertEqual(caught.exception.code, "passphrase_required")
        ok = self.state.contact_save({"name": "Aiden", "passphrase": PASS,
                                      "addresses": {"bitcoin-mainnet": "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"}})
        self.assertIn("bitcoin-mainnet", ok["contact"]["addresses"])

    def test_a_proposal_cannot_overwrite_an_existing_mandate(self) -> None:
        self.state.unlock(PASS)
        created = self.state.mandates_create({"kind": "monero-stagenet", "asset": "XMR",
                                             "payee": xmr_addr(91), "per_tx_cap": "0.01",
                                             "period_cap": "0.05", "absolute_cap": "0.05",
                                             "interval": "24h", "passphrase": PASS})
        mandate_id = created["mandate"]["mandate_id"]
        with self.assertRaises(WalletError) as caught:
            self.state.mandates_propose({"mandate_id": mandate_id, "kind": "monero-stagenet",
                                         "asset": "XMR", "payee": xmr_addr(92),
                                         "per_tx_cap": "0.001", "period_cap": "0.001",
                                         "absolute_cap": "0.001"})
        self.assertEqual(caught.exception.code, "mandate_exists")
        self.assertTrue(self.state.mandates[mandate_id].enabled)

    # ---------- third-review regressions ----------

    def test_the_signer_endpoint_cannot_be_repointed_without_the_passphrase(self) -> None:
        """It decides which address the human is told to fund, and it receives the signer's creds."""
        self.state.unlock(PASS)
        with self.assertRaises(WalletError) as caught:
            self.state.config_set({"monero_rpc_url": "http://127.0.0.1:9/json_rpc"})
        self.assertEqual(caught.exception.code, "passphrase_required")
        with self.assertRaises(WalletError) as caught:
            self.state.config_set({"monero_rpc_url": "https://attacker.example/json_rpc",
                                   "passphrase": PASS})
        self.assertEqual(caught.exception.code, "bad_setting")
        self.assertIn("local signer", caught.exception.message)

    def test_an_unreadable_balance_does_not_close_a_sandbox(self) -> None:
        sandbox = self._funded_sandbox("0.2")
        live = self.state.sandboxes[sandbox["sandbox_id"]]

        def broken(_index):
            raise ChainError("rpc_unreachable", "node is down")

        self.state.fake.balance_at = broken
        self.state.sandbox_sweep({"sandbox_id": sandbox["sandbox_id"]})
        after = self.state.sandboxes[sandbox["sandbox_id"]]
        self.assertTrue(after.enabled, "an unreadable balance was treated as 'swept'")
        self.assertNotEqual(after.spent, after.budget)

    def test_a_dead_request_stops_being_approvable(self) -> None:
        pending = self._pending()
        row = pending["request"]
        old = self.state.requests[row["request_id"]]
        old.created_at = "2000-01-01T00:00:00Z"          # well past any approval window
        listed = self.state.requests_list()["requests"]
        found = [r for r in listed if r["request_id"] == row["request_id"]][0]
        self.assertNotIn("approval_id", found,
                         "a request older than the approval window must not be re-issued an approval")

    # ---------- contacts ----------

    def test_contact_save_validates_the_address(self) -> None:
        self.state.unlock(PASS)
        with self.assertRaises(WalletError) as caught:
            self.state.contact_save({"name": "Aiden", "addresses": {"monero-stagenet": "nope"}})
        self.assertEqual(caught.exception.code, "bad_contact")

    def test_sending_to_a_contact_name_resolves_the_address(self) -> None:
        self.state.unlock(PASS)
        address = xmr_addr(70)
        saved = self.state.contact_save({"name": "Aiden", "addresses": {"monero-stagenet": address},
                                         "notes": "flatmate"})
        self.assertTrue(saved["created"])
        result = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                        "amount": "0.01", "payee": "aiden"})
        self.assertTrue(result["decision"]["allow"])
        self.assertEqual(result["request"]["payee"], address)
        self.assertIn("Aiden", result["request"]["memo"])

    def test_contact_without_that_chain_is_a_clear_error(self) -> None:
        self.state.unlock(PASS)
        self.state.contact_save({"name": "Aiden", "addresses": {"monero-stagenet": xmr_addr(71)}})
        with self.assertRaises(WalletError) as caught:
            self.state.request_tx({"kind": "bitcoin-testnet", "asset": "BTC",
                                   "amount": "0.001", "payee": "Aiden"})
        self.assertEqual(caught.exception.code, "contact_chain_missing")

    def test_unknown_name_is_denied_with_a_receipt(self) -> None:
        self.state.unlock(PASS)
        result = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                        "amount": "0.01", "payee": "nobody"})
        self.assertFalse(result["decision"]["allow"])
        self.assertEqual(result["decision"]["code"], "invalid_payee")
        self.assertEqual(self.state.ledger.last()["decision"], "denied")

    # ---------- coin preference ----------

    def test_monero_only_refuses_other_assets(self) -> None:
        # a real pairing: USDC lives on the EVM chain, not on a Monero one, and the mismatch is now
        # rejected before policy even runs
        self.state.unlock(PASS)
        self.state.settings.monero_only = True
        result = self.state.request_tx({"kind": "base-sepolia", "asset": "USDC",
                                        "amount": "1", "payee": "0x" + "1" * 40})
        self.assertFalse(result["decision"]["allow"])
        self.assertEqual(result["decision"]["code"], "monero_only")

    def test_a_coin_that_does_not_belong_to_the_chain_is_refused(self) -> None:
        self.state.unlock(PASS)
        with self.assertRaises(WalletError) as caught:
            self.state.request_tx({"kind": "monero-stagenet", "asset": "USDC",
                                   "amount": "1", "payee": xmr_addr(73)})
        self.assertEqual(caught.exception.code, "asset_chain_mismatch")

    def test_the_chain_supplies_the_default_asset(self) -> None:
        """Name a chain and omit the asset: you get that chain's own coin, not prefer_asset."""
        self.state.unlock(PASS)
        self.state.settings.prefer_asset = "XMR"
        out = self.state.request_tx({"kind": "bitcoin-testnet", "amount": "0.0001",
                                     "payee": "bcrt1q" + "1" * 30})
        self.assertEqual(out["request"]["asset"], "BTC")

    def test_asset_auto_uses_the_preference(self) -> None:
        self.state.unlock(PASS)
        self.state.settings.prefer_asset = "XMR"
        result = self.state.request_tx({"kind": "monero-stagenet", "asset": "AUTO",
                                        "amount": "0.01", "payee": xmr_addr(73)})
        self.assertEqual(result["request"]["asset"], "XMR")

    def test_queued_approvals_cannot_multiply_the_daily_cap(self) -> None:
        self.state.unlock(PASS)
        self.state.settings.daily_cap = {"XMR": "1.0"}
        self.state.settings.per_tx_cap = {"XMR": "10"}
        first = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                       "amount": "0.6", "payee": xmr_addr(80)})
        second = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                        "amount": "0.6", "payee": xmr_addr(81)})
        self._approve(first["request"]["request_id"], first["approval_id"], "0.6")
        with self.assertRaises(WalletError) as caught:
            self._approve(second["request"]["request_id"], second["approval_id"], "0.6")
        self.assertEqual(caught.exception.code, "daily_cap")
        self.assertEqual(self.state.ledger.last()["decision"], "denied")

    def test_sandbox_with_empty_profile_is_denied(self) -> None:
        self.state.unlock(PASS)
        created = self.state.sandbox_create({"kind": "monero-stagenet", "asset": "XMR",
                                             "budget": "0.2", "lifetime_hours": 24,
                                             "allowed_profiles": ["wallet"]})
        sandbox_id = created["sandbox"]["sandbox_id"]
        funding = created["funding"]
        self._approve(funding["request"]["request_id"], funding["approval_id"], "0.2")
        result = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                        "amount": "0.01", "payee": xmr_addr(82),
                                        "sandbox_id": sandbox_id})
        self.assertFalse(result["decision"]["allow"])
        self.assertEqual(result["decision"]["code"], "sandbox_profile")

    def test_mandates_survive_a_restart(self) -> None:
        self.state.unlock(PASS)
        proposed = self.state.mandates_propose({"kind": "monero-stagenet", "asset": "XMR",
                                                "payee": xmr_addr(90), "per_tx_cap": "0.01",
                                                "period_cap": "0.1", "period_hours": 24,
                                                "absolute_cap": "1"})
        mandate_id = proposed["mandate"]["mandate_id"]
        self.state.mandates_activate({"mandate_id": mandate_id, "passphrase": PASS})

        revived = State(self.state.home)
        revived.unlock(PASS)
        listed = {m["mandate_id"]: m for m in revived.mandates_list()["mandates"]}
        self.assertIn(mandate_id, listed)
        self.assertEqual(listed[mandate_id]["status"], "active")

    def test_adapters_use_the_keystore_password_not_a_plaintext_file(self) -> None:
        """The wallet-file password lives in the sealed keystore, not in wallet.env.

        It used to be plaintext on disk (0600, but readable by anything running as this user). The
        keystore copy wins, so an env file — stale, copied, or written by an attacker — cannot make
        the wallet open a different wallet file.
        """
        self.state.unlock(PASS)
        stored = self.state._signer_password()
        self.assertTrue(stored, "the keystore should carry a wallet-file password")
        (self.state.home / "wallet.env").write_text("AGENT_WALLET_MONERO_PASSWORD=attacker-value\n",
                                                    encoding="utf-8")
        passwords: list[str] = []

        class Spy:
            def __init__(self, *args, **kwargs):
                passwords.append(args[3] if len(args) > 3 else kwargs.get("wallet_password", ""))
                self.kind = args[0]

            def available(self) -> bool:
                return True

        disabled = os.environ.pop("WALLETD_DISABLE_CHAINS", None)
        original = state_module.XmrAdapter
        state_module.XmrAdapter = Spy  # type: ignore[assignment]
        try:
            self.state._build_adapters()
        finally:
            state_module.XmrAdapter = original  # type: ignore[assignment]
            if disabled is not None:
                os.environ["WALLETD_DISABLE_CHAINS"] = disabled

        self.assertTrue(passwords, "no adapter was built")
        self.assertEqual(set(passwords), {stored})
        self.assertNotIn("attacker-value", passwords)

    def test_each_purchase_gets_its_own_address(self) -> None:
        self.state.unlock(PASS)
        first = self.state.receive_address({"label": "order 1"})
        second = self.state.receive_address({"label": "order 2"})
        self.assertNotEqual(first["address"], second["address"])
        self.assertFalse(first["reused"])
        self.assertEqual([first["account_index"], second["account_index"]], [1, 2])

    def test_incoming_joins_each_order_to_what_arrived(self) -> None:
        """The receipt knows what the address was for; the chain knows the money. Join them."""
        self.state.unlock(PASS)
        partial = self.state.receive_address({"label": "order 1", "expected": "0.5"})
        paid = self.state.receive_address({"label": "order 2"})
        waiting = self.state.receive_address({"label": "order 3", "expected": "1"})
        self.state.fake.accounts = lambda: [
            {"account_index": partial["account_index"], "address": partial["address"],
             "balance": "0.2", "unlocked": "0.2", "label": "order 1"},
            {"account_index": paid["account_index"], "address": paid["address"],
             "balance": "0.01", "unlocked": "0.01", "label": "order 2"},
            {"account_index": waiting["account_index"], "address": waiting["address"],
             "balance": "0", "unlocked": "0", "label": "order 3"},
        ]
        self.state.ledger.append("payment_proof", "checked", "agent",
                                 {"kind": "monero-stagenet", "address": paid["address"],
                                  "txid": "cd" * 32, "tx_hash": "ab" * 32})
        payload = self.state.incoming()
        orders = {row["label"]: row for row in payload["orders"]}
        self.assertEqual(orders["order 1"]["status"], "partial")
        self.assertEqual(orders["order 1"]["received"], "0.2")
        self.assertEqual(orders["order 1"]["expected"], "0.5")
        self.assertEqual(orders["order 2"]["status"], "paid")
        self.assertEqual(orders["order 3"]["status"], "waiting")
        self.assertEqual(orders["order 1"]["asset"], "XMR")
        self.assertEqual(orders["order 1"]["account_index"], partial["account_index"])
        self.assertEqual(orders["order 2"]["last_tx"], "ab" * 32)
        self.assertEqual(orders["order 3"]["last_tx"], "")

    def test_incoming_does_not_hide_an_account_the_chain_no_longer_lists(self) -> None:
        self.state.unlock(PASS)
        opened = self.state.receive_address({"label": "vanished"})
        self.state.fake.accounts = lambda: []
        rows = self.state.incoming()["orders"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "waiting")
        self.assertEqual(rows[0]["address"], opened["address"])

    def test_incoming_degrades_when_the_chain_has_no_subaddresses(self) -> None:
        self.state.unlock(PASS)
        self.state.fake.supports_accounts = False
        self.state.adapters = {self.state.fake.kind: self.state.fake}
        payload = self.state.incoming()
        self.assertEqual(payload["orders"], [])
        self.assertIn("subaddress", payload["note"])

    def test_cancel_withdraws_only_a_pending_request_and_receipts_it(self) -> None:
        self.state.unlock(PASS)
        made = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                      "amount": "0.01", "payee": xmr_addr(97)})
        request_id = made["request"]["request_id"]
        out = self.state.cancel_request({"request_id": request_id, "reason": "changed my mind"})
        self.assertEqual(out["request"]["status"], "cancelled")
        last = list(self.state.ledger.entries())[-1]
        self.assertEqual((last["kind"], last["decision"], last["rule"]),
                         ("spend_request", "cancelled", "agent"))
        self.assertEqual(last["details"]["request_id"], request_id)
        self.assertEqual(last["details"]["code"], "cancelled_by_agent")
        # the cancelled request's approval token is dead, so a card left open cannot approve it
        with self.assertRaises(WalletError) as caught:
            self._approve(request_id, made["approval_id"])
        self.assertIn(caught.exception.code, ("approval_replayed", "request_not_pending"))
        # and cancelling it twice is refused, not a second receipt
        with self.assertRaises(WalletError) as caught:
            self.state.cancel_request({"request_id": request_id})
        self.assertEqual(caught.exception.code, "not_cancellable")

    def test_cancelling_an_approved_request_is_refused(self) -> None:
        self.state.unlock(PASS)
        made = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                      "amount": "0.01", "payee": xmr_addr(98)})
        self._approve(made["request"]["request_id"], made["approval_id"])
        with self.assertRaises(WalletError) as caught:
            self.state.cancel_request({"request_id": made["request"]["request_id"]})
        self.assertEqual(caught.exception.code, "not_cancellable")

    def test_cancelling_an_unknown_request_is_a_clear_error(self) -> None:
        self.state.unlock(PASS)
        with self.assertRaises(WalletError) as caught:
            self.state.cancel_request({"request_id": "nope"})
        self.assertEqual(caught.exception.code, "unknown_request")

    def test_a_cancelled_idempotency_key_does_not_pay_on_retry(self) -> None:
        self.state.unlock(PASS)
        params = {"kind": "monero-stagenet", "asset": "XMR", "amount": "0.01",
                  "payee": xmr_addr(99), "idempotency_key": "cancel-then-retry"}
        made = self.state.request_tx(dict(params))
        self.state.cancel_request({"request_id": made["request"]["request_id"]})
        again = self.state.request_tx(dict(params))
        self.assertTrue(again.get("duplicate"))
        self.assertEqual(again["request"]["status"], "cancelled")

    def test_a_wrong_tx_key_is_reported_as_not_received(self) -> None:
        self.state.unlock(PASS)
        proof = self.state.verify_payment({"txid": "ab" * 32, "tx_key": "cd" * 32,
                                           "address": "85" + "1" * 93})
        self.assertFalse(proof["received"])
        self.assertEqual(proof["amount"], "0")

    # ---------- what an operator token is not allowed to do ----------

    def test_activating_a_mandate_needs_the_passphrase(self) -> None:
        self.state.unlock(PASS)
        made = self.state.mandates_propose({"kind": "monero-stagenet", "asset": "XMR",
                                            "payee": xmr_addr(70), "per_tx_cap": "0.01",
                                            "period_cap": "0.1", "period_hours": 24,
                                            "absolute_cap": "1"})
        with self.assertRaises(WalletError) as caught:
            self.state.mandates_activate({"mandate_id": made["mandate"]["mandate_id"]})
        self.assertEqual(caught.exception.code, "passphrase_required")
        # with the passphrase it goes through
        self.state.mandates_activate({"mandate_id": made["mandate"]["mandate_id"], "passphrase": PASS})
        self.assertEqual(self.state.mandates[made["mandate"]["mandate_id"]].status(), "active")

    def test_caps_cannot_be_raised_without_the_passphrase(self) -> None:
        self.state.unlock(PASS)
        with self.assertRaises(WalletError) as caught:
            self.state.config_set({"per_tx_cap": {"XMR": "999"}})
        self.assertEqual(caught.exception.code, "passphrase_required")
        self.state.config_set({"per_tx_cap": {"XMR": "999"}, "passphrase": PASS})
        self.assertEqual(self.state.settings.per_tx_cap["XMR"], "999")
        # a coin toggle is a preference, not policy
        self.state.config_set({"chains_enabled": ["monero-stagenet"]})

    def test_deleting_receipts_is_detected(self) -> None:
        self.state.unlock(PASS)
        for index in range(3):
            self.state.ledger.append("test", "note", "test", {"i": index})
        self.assertTrue(self.state.ledger_verify()["ok"])
        path = self.state.ledger_path
        lines = path.read_text(encoding="utf-8").splitlines()
        path.write_text("\n".join(lines[:2]) + "\n", encoding="utf-8")
        result = self.state.ledger_verify()
        self.assertFalse(result["ok"])
        self.assertIn("recorded", result["message"])

    def test_a_sandbox_still_obeys_the_allowlist(self) -> None:
        sandbox = self._funded_sandbox("0.2")
        self.state.config_set({"payee_allowlist": [xmr_addr(10)], "passphrase": PASS})
        result = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                       "amount": "0.01", "payee": xmr_addr(61),
                                       "sandbox_id": sandbox["sandbox_id"]})
        self.assertFalse(result["decision"]["allow"])

    def test_funding_a_sandbox_at_the_wrong_address_does_not_arm_it(self) -> None:
        self.state.unlock(PASS)
        made = self.state.sandbox_create({"kind": "monero-stagenet", "asset": "XMR",
                                          "budget": "0.2", "lifetime_hours": 24})
        sandbox_id = made["sandbox"]["sandbox_id"]
        # refused before an approval is even issued: a payment to somebody else is not funding
        with self.assertRaises(WalletError) as caught:
            self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                   "amount": "0.2", "payee": xmr_addr(62),
                                   "funding_for": sandbox_id})
        self.assertEqual(caught.exception.code, "funding_mismatch")
        self.assertFalse(self.state.sandboxes[sandbox_id].enabled)
        # and a late approval of the real funding request cannot arm a revoked sandbox
        created = self.state.sandbox_create({"kind": "monero-stagenet", "asset": "XMR",
                                             "budget": "0.2", "lifetime_hours": 24})
        sid = created["sandbox"]["sandbox_id"]
        funding = created["funding"]
        self.state.sandbox_revoke({"sandbox_id": sid, "sweep": False})
        with self.assertRaises(WalletError) as closed:
            self._approve(funding["request"]["request_id"], funding["approval_id"], "0.2")
        # revoking cancelled the funding request and burned its approval, so a late approve is dead
        self.assertIn(closed.exception.code,
                      ("not_cancellable", "unknown_request", "unknown_approval", "approval_replayed"))
        self.assertFalse(self.state.sandboxes[sid].enabled)

    def test_a_fee_above_the_cap_is_refused(self) -> None:
        self.state.unlock(PASS)
        self._chain().fee = Decimal("0.5")
        result = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                       "amount": "0.01", "payee": xmr_addr(63)})
        self.assertFalse(result["decision"]["allow"])
        self.assertEqual(result["decision"]["code"], "fee_too_high")

    def test_an_amount_below_the_asset_precision_is_refused(self) -> None:
        with self.assertRaises(WalletError) as caught:
            self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                   "amount": "0.0000000000001", "payee": xmr_addr(64)})
        self.assertEqual(caught.exception.code, "bad_amount")

    def test_changing_a_contact_address_needs_the_passphrase(self) -> None:
        self.state.contact_save({"name": "Aiden", "addresses": {"monero-stagenet": xmr_addr(3)}})
        with self.assertRaises(WalletError) as caught:
            self.state.contact_save({"name": "Aiden",
                                     "addresses": {"monero-stagenet": xmr_addr(40)}})
        self.assertEqual(caught.exception.code, "passphrase_required")
        self.state.contact_save({"name": "Aiden",
                                 "addresses": {"monero-stagenet": xmr_addr(40)},
                                 "passphrase": PASS})
        receipt = self.state.ledger.last()
        self.assertEqual(receipt["decision"], "saved")
        self.assertIn("changed", receipt["details"])

    # ---------- retries must not pay twice ----------

    def test_the_same_idempotency_key_returns_the_same_request(self) -> None:
        self.state.unlock(PASS)
        params = {"kind": "monero-stagenet", "asset": "XMR", "amount": "0.01",
                  "payee": xmr_addr(90), "idempotency_key": "buy-42"}
        first = self.state.request_tx(dict(params))
        second = self.state.request_tx(dict(params))
        self.assertEqual(first["request"]["request_id"], second["request"]["request_id"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["approval_id"], first["approval_id"])
        pending = [r for r in self.state.requests.values() if r.status == "pending"]
        self.assertEqual(len(pending), 1)

    def test_a_retry_after_a_restart_still_deduplicates(self) -> None:
        self.state.unlock(PASS)
        params = {"kind": "monero-stagenet", "asset": "XMR", "amount": "0.01",
                  "payee": xmr_addr(91), "idempotency_key": "buy-43"}
        first = self.state.request_tx(dict(params))

        revived = WalletState(self.state.home)
        revived.fake = FakeAdapter()
        revived.unlock(PASS)
        again = revived.request_tx(dict(params))
        self.assertEqual(again["request"]["request_id"], first["request"]["request_id"])
        self.assertTrue(again["duplicate"])

    def test_a_retry_after_payment_does_not_pay_again(self) -> None:
        self.state.unlock(PASS)
        params = {"kind": "monero-stagenet", "asset": "XMR", "amount": "0.01",
                  "payee": xmr_addr(92), "idempotency_key": "buy-44"}
        made = self.state.request_tx(dict(params))
        self._approve(made["request"]["request_id"], made["approval_id"], "0.01")
        broadcasts = self._chain().broadcast_count
        again = self.state.request_tx(dict(params))
        self.assertEqual(again["request"]["request_id"], made["request"]["request_id"])
        self.assertEqual(self._chain().broadcast_count, broadcasts, "a retry broadcast again")

    def test_different_keys_are_different_payments(self) -> None:
        self.state.unlock(PASS)
        base = {"kind": "monero-stagenet", "asset": "XMR", "amount": "0.01", "payee": xmr_addr(93)}
        first = self.state.request_tx({**base, "idempotency_key": "a"})
        second = self.state.request_tx({**base, "idempotency_key": "b"})
        self.assertNotEqual(first["request"]["request_id"], second["request"]["request_id"])

    def test_a_backup_carries_every_state_file(self) -> None:
        from walletd import backup

        self.state.unlock(PASS)
        self.state.contact_save({"name": "Aiden", "addresses": {"monero-stagenet": xmr_addr(3)}})
        self.state.mandates_propose({"kind": "monero-stagenet", "asset": "XMR", "payee": xmr_addr(4),
                                     "per_tx_cap": "0.01", "period_cap": "0.1", "period_hours": 24,
                                     "absolute_cap": "1"})
        self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.01",
                               "payee": xmr_addr(5), "idempotency_key": "backup-1"})
        out = Path(self.tmp.name) / "state-backup.json"
        created = backup.create(self.home, out, "backup-passphrase")
        for name in ("contacts.json", "mandates.json", "requests.json", "idempotency.json",
                     "ledger.anchor"):
            self.assertIn(name, created["files"], f"{name} was left out of the backup")
        restored = Path(self.tmp.name) / "restored-state"
        backup.restore(out, restored, "backup-passphrase")
        for name in ("contacts.json", "mandates.json", "requests.json", "idempotency.json"):
            self.assertTrue((restored / name).exists(), f"{name} was not restored")

    # ---------- autopay lifecycle ----------

    def _mandate_params(self, **over) -> dict:
        params = {"kind": "monero-stagenet", "asset": "XMR", "payee": xmr_addr(70),
                  "per_tx_cap": "0.01", "period_cap": "0.05", "absolute_cap": "0.2",
                  "interval": "1d", "label": "rent", "activate": True, "passphrase": PASS}
        params.update(over)
        return params

    def test_autopay_can_be_created_edited_paused_resumed_and_deleted(self) -> None:
        self.state.unlock(PASS)
        made = self.state.mandates_create(self._mandate_params())
        mandate_id = made["mandate"]["mandate_id"]
        self.assertEqual(made["mandate"]["status"], "active")
        self.assertEqual(made["mandate"]["period_hours"], 24)

        edited = self.state.mandates_update({"mandate_id": mandate_id, "interval": "6h",
                                             "period_cap": "0.02", "passphrase": PASS})
        self.assertEqual(edited["mandate"]["period_hours"], 6)
        self.assertEqual(Decimal(edited["mandate"]["period_cap"]), Decimal("0.02"))

        paused = self.state.mandates_set_enabled({"mandate_id": mandate_id, "enabled": False})
        # paused is not revoked: one is reversible, the other is terminal
        self.assertEqual(paused["mandate"]["status"], "paused")
        resumed = self.state.mandates_set_enabled({"mandate_id": mandate_id, "enabled": True,
                                                   "passphrase": PASS})
        self.assertEqual(resumed["mandate"]["status"], "active")

        # survives a restart, then deletes cleanly
        revived = WalletState(self.state.home)
        revived.fake = FakeAdapter()
        revived.unlock(PASS)
        self.assertIn(mandate_id, {m["mandate_id"] for m in revived.mandates_list()["mandates"]})
        revived.mandates_delete({"mandate_id": mandate_id, "passphrase": PASS})
        self.assertNotIn(mandate_id, revived.mandates)
        after = WalletState(self.state.home)
        after.fake = FakeAdapter()
        after.unlock(PASS)
        self.assertNotIn(mandate_id, after.mandates)

    def test_autopay_creation_and_resume_need_the_passphrase(self) -> None:
        self.state.unlock(PASS)
        with self.assertRaises(WalletError) as caught:
            self.state.mandates_create(self._mandate_params(passphrase=None))
        self.assertEqual(caught.exception.code, "passphrase_required")

        made = self.state.mandates_create(self._mandate_params())
        mandate_id = made["mandate"]["mandate_id"]
        self.state.mandates_set_enabled({"mandate_id": mandate_id, "enabled": False})
        with self.assertRaises(WalletError) as caught:
            self.state.mandates_set_enabled({"mandate_id": mandate_id, "enabled": True})
        self.assertEqual(caught.exception.code, "passphrase_required")
        # pausing only removes authority, so it needs nothing
        self.state.mandates_set_enabled({"mandate_id": mandate_id, "enabled": False})

    def test_autopay_intervals_parse(self) -> None:
        from walletd.policy import parse_interval

        self.assertEqual(parse_interval("6h"), 6)
        self.assertEqual(parse_interval("2d"), 48)
        self.assertEqual(parse_interval("weekly"), 168)
        self.assertEqual(parse_interval(12), 12)
        self.assertEqual(parse_interval(None), 24)
        with self.assertRaises(ValueError):
            parse_interval("every other tuesday")

    def test_autopay_caps_are_validated_on_edit(self) -> None:
        self.state.unlock(PASS)
        made = self.state.mandates_create(self._mandate_params())
        mandate_id = made["mandate"]["mandate_id"]
        with self.assertRaises(WalletError) as caught:
            self.state.mandates_update({"mandate_id": mandate_id, "period_cap": "5",
                                        "absolute_cap": "1", "passphrase": PASS})
        self.assertEqual(caught.exception.code, "bad_mandate")

    def test_an_expired_autopay_cannot_be_resumed(self) -> None:
        self.state.unlock(PASS)
        made = self.state.mandates_create(self._mandate_params())
        mandate_id = made["mandate"]["mandate_id"]
        self.state.mandates[mandate_id].expires_at = "2020-01-01T00:00:00Z"
        self.state.mandates[mandate_id].enabled = False
        with self.assertRaises(WalletError) as caught:
            self.state.mandates_set_enabled({"mandate_id": mandate_id, "enabled": True,
                                             "passphrase": PASS})
        self.assertEqual(caught.exception.code, "mandate_expired")

    # ---------- clearing the receipt view ----------

    def test_archiving_history_keeps_the_old_chain_verifiable(self) -> None:
        self.state.unlock(PASS)
        for index in range(4):
            self.state.ledger.append("test", "note", "test", {"i": index})
        before = self.state.ledger.last()["seq"]

        result = self.state.ledger_archive({"passphrase": PASS})
        self.assertEqual(result["through_seq"], before)

        # the view is clean, and the new chain starts by pointing at what came before
        self.assertEqual(self.state.ledger.last()["seq"], 1)
        self.assertEqual(self.state.ledger.last()["details"]["archived_through_seq"], before)

        # the archive is on disk and still verifies with the same public key
        archive = self.state.home / result["archived"]
        self.assertTrue(archive.exists())
        from walletd.ledger import verify_file
        ok, reason, count = verify_file(archive, self.state.ledger.pubkey_hex)
        self.assertTrue(ok, reason)
        self.assertEqual(count, before)

        # and the live chain still verifies (anchor included)
        self.assertTrue(self.state.ledger_verify()["ok"])

    def test_archiving_history_needs_no_passphrase(self) -> None:
        """It archives rather than deletes: the record survives, so there is nothing to authorise."""
        before = self.state.ledger.last()["seq"]
        self.state.ledger.append("test", "note", "test", {})      # works while locked, as before
        result = self.state.ledger_archive({})
        self.assertEqual(result["through_seq"], before + 1)
        self.assertEqual(self.state.ledger.last()["kind"], "ledger")

    # ---------- turning the passphrase prompt off ----------

    def test_turning_the_prompt_off_needs_the_passphrase_again(self) -> None:
        self.state.unlock(PASS)
        with self.assertRaises(WalletError) as caught:
            self.state.set_human_gate({"enabled": False, "confirm": True})
        self.assertEqual(caught.exception.code, "passphrase_required")
        with self.assertRaises(WalletError) as caught:
            self.state.set_human_gate({"enabled": False, "confirm": True, "passphrase": "wrong-one"})
        self.assertEqual(caught.exception.code, "bad_passphrase")
        # and it needs the explicit confirmation, even with the right passphrase
        with self.assertRaises(WalletError) as caught:
            self.state.set_human_gate({"enabled": False, "passphrase": PASS})
        self.assertEqual(caught.exception.code, "confirm_required")

        result = self.state.set_human_gate({"enabled": False, "confirm": True, "passphrase": PASS})
        self.assertFalse(result["human_gate"])
        self.assertFalse(self.state.settings.human_gate)
        self.assertEqual(self.state.ledger.last()["details"]["setting"], "human_gate")

    def test_turning_the_prompt_back_on_removes_authority_so_it_needs_none(self) -> None:
        self.state.unlock(PASS)
        self.state.set_human_gate({"enabled": False, "confirm": True, "passphrase": PASS})
        result = self.state.set_human_gate({"enabled": True, "confirm": True})
        self.assertTrue(result["human_gate"])
        self.assertTrue(self.state.settings.human_gate)

    # ---------- the passphrase prompt the agent may raise ----------

    def test_an_agent_can_raise_a_prompt_but_never_sees_the_passphrase(self) -> None:
        self.state.unlock(PASS)
        made = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                      "amount": "0.01", "payee": xmr_addr(77)})
        request_id = made["request"]["request_id"]

        raised = self.state.request_operator_prompt({"request_id": request_id})
        self.assertEqual(raised["prompt"]["kind"], "approval")
        self.assertEqual(raised["prompt"]["request_id"], request_id)
        self.assertIn("never reaches you", raised["note"])
        self.assertEqual(self.state.status()["prompt"]["request_id"], request_id)

        # approving clears the prompt, and nothing about it carries a secret
        self._approve(request_id, made["approval_id"], "0.01")
        self.assertIsNone(self.state.status()["prompt"])

    def test_a_prompt_for_an_unknown_request_is_refused(self) -> None:
        self.state.unlock(PASS)
        with self.assertRaises(WalletError) as caught:
            self.state.request_operator_prompt({"request_id": "req_nope"})
        self.assertEqual(caught.exception.code, "unknown_request")

    def test_revoking_a_sandbox_returns_the_leftover_budget(self) -> None:
        sandbox = self._funded_sandbox("0.2")
        sandbox_id = sandbox["sandbox_id"]
        self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.05",
                               "payee": xmr_addr(66), "sandbox_id": sandbox_id})
        live = self.state.sandboxes[sandbox_id]
        self.state.fake.set_balance(live.account_index, "0.15")   # what is really in the account
        out = self.state.sandbox_revoke({"sandbox_id": sandbox_id})
        self.assertTrue(out["sweep"]["attempted"])
        self.assertEqual(Decimal(out["sweep"]["swept"]), Decimal("0.14998"))   # real fee, not the cap
        self.assertFalse(out["sandbox"]["enabled"])

    def test_the_sweep_can_be_skipped(self) -> None:
        sandbox = self._funded_sandbox("0.2")
        out = self.state.sandbox_revoke({"sandbox_id": sandbox["sandbox_id"], "sweep": False})
        self.assertFalse(out["sweep"]["attempted"])

    def test_archiving_history_does_not_reset_the_spend_caps(self) -> None:
        """Archiving is a view operation: it must never hand the agent fresh budget."""
        self.state.unlock(PASS)
        from walletd import config as config_mod

        self.state.settings.daily_cap = {"XMR": "2.0"}
        self.state.settings.per_tx_cap = {"XMR": "10"}
        config_mod.save(self.state.config_path, self.state.settings, self.state.master)
        for offset in (40, 41):
            made = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                          "amount": "0.6", "payee": xmr_addr(offset)})
            self._approve(made["request"]["request_id"], made["approval_id"], "0.6")
        self.assertEqual(self.state.ledger.totals("XMR"), Decimal("1.2"))

        self.state.ledger_archive({})
        self.assertEqual(self.state.ledger.totals("XMR"), Decimal("1.2"),
                         "totals lost the archived spend — a cap just reset itself")

        # 1.2 already spent against a 2.0/day cap: another 0.9 would cross it
        next_try = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                         "amount": "0.9", "payee": xmr_addr(42)})
        self.assertFalse(next_try["decision"]["allow"])
        self.assertEqual(next_try["decision"]["code"], "daily_cap")

    def test_a_pruned_request_still_answers_for_its_idempotency_key(self) -> None:
        """requests.json is pruned to the newest 200; the key must still refuse to pay twice."""
        self.state.unlock(PASS)
        params = {"kind": "monero-stagenet", "asset": "XMR", "amount": "0.01",
                  "payee": xmr_addr(95), "idempotency_key": "pruned-1"}
        first = self.state.request_tx(dict(params))

        # simulate the prune that a restart performs on a long-lived wallet
        revived = WalletState(self.state.home)
        revived.fake = FakeAdapter()
        revived.unlock(PASS)
        revived.requests.pop(first["request"]["request_id"], None)

        again = revived.request_tx(dict(params))
        self.assertTrue(again.get("duplicate"))
        self.assertNotIn("approval_id", again)
        self.assertEqual(len(revived.requests), 0, "a pruned request's key created a second payment")

    def test_denying_works_while_the_wallet_is_locked(self) -> None:
        """Auto-lock can fire while the human is reading the card; refusing must still work."""
        self.state.unlock(PASS)
        made = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                      "amount": "0.01", "payee": xmr_addr(96)})
        self.state.lock()
        out = self.state.deny({"request_id": made["request"]["request_id"],
                               "approval_id": made["approval_id"], "reason": "locked but refused"})
        self.assertEqual(out["request"]["status"], "denied")

    def test_deleting_a_contact_needs_the_passphrase(self) -> None:
        self.state.contact_save({"name": "Aiden", "addresses": {"monero-stagenet": xmr_addr(3)}})
        with self.assertRaises(WalletError) as caught:
            self.state.contact_delete({"name": "Aiden"})
        self.assertEqual(caught.exception.code, "passphrase_required")
        self.state.contact_delete({"name": "Aiden", "passphrase": PASS})
        self.assertNotIn("aiden", self.state.contacts)

    def test_a_sandbox_leaves_room_for_its_own_fee(self) -> None:
        """The fee comes out of the sandbox budget, so the last payment must not consume all of it."""
        sandbox = self._funded_sandbox("0.2")
        self.state.settings.fee_cap = {"XMR": "0.0005"}
        # 0.1998 + 0.0005 still fits in 0.2 …
        ok = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.1995",
                                    "payee": xmr_addr(67), "sandbox_id": sandbox["sandbox_id"]})
        self.assertTrue(ok["decision"]["allow"], ok["decision"])
        # … but 0.1999 + 0.0005 does not
        refused = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.1996",
                                        "payee": xmr_addr(68), "sandbox_id": sandbox["sandbox_id"]})
        self.assertFalse(refused["decision"]["allow"])
        self.assertEqual(refused["decision"]["code"], "sandbox_budget")

    def test_the_wallet_home_is_outside_the_plugin_directory(self) -> None:
        """Removing the plugin must not be able to take the wallet with it."""
        from wallet.client import wallet_home

        plugin_root = Path(__file__).resolve().parents[1]
        home = wallet_home().resolve()
        self.assertFalse(str(home).startswith(str(plugin_root)),
                         "the wallet home lives inside the plugin tree, so `plugins remove` would delete it")

    # ---------- the human gate ----------






    def test_human_gate_cannot_be_disabled_without_passphrase(self) -> None:
        self.state.unlock(PASS)
        with self.assertRaises(WalletError) as caught:
            self.state.set_human_gate({"enabled": False, "passphrase": "wrong", "confirm": True})
        self.assertEqual(caught.exception.code, "bad_passphrase")
        self.assertTrue(self.state.settings.human_gate)

    def test_human_gate_requires_explicit_confirm(self) -> None:
        self.state.unlock(PASS)
        with self.assertRaises(WalletError) as caught:
            self.state.set_human_gate({"enabled": False, "passphrase": PASS})
        self.assertEqual(caught.exception.code, "confirm_required")

    def test_human_gate_can_be_disabled_by_operator_with_passphrase(self) -> None:
        self.state.unlock(PASS)
        result = self.state.set_human_gate({"enabled": False, "passphrase": PASS, "confirm": True})
        self.assertTrue(result["changed"])
        self.assertFalse(result["human_gate"])
        self.assertTrue(self.state.ledger.verify()[0])

    def test_tampered_config_forces_human_gate_back_on(self) -> None:
        self.state.unlock(PASS)
        self.state.set_human_gate({"enabled": False, "passphrase": PASS, "confirm": True})
        path = self.home / "config.json"
        envelope = json.loads(path.read_text(encoding="utf-8"))
        envelope["settings"]["human_gate"] = False
        envelope["settings"]["per_tx_cap"]["XMR"] = "10000"
        path.write_text(json.dumps(envelope), encoding="utf-8")

        fresh = State(self.home)
        result = fresh.unlock(PASS)
        self.assertTrue(result["config_tampered"])
        self.assertTrue(fresh.settings.human_gate)
        self.assertEqual(fresh.settings.cap(fresh.settings.per_tx_cap, "XMR"), fresh.settings.cap(config.Settings().per_tx_cap, "XMR"))

    # ---------- the promise: no secrets in responses ----------

    def test_no_secret_material_in_agent_views(self) -> None:
        self.state.unlock(PASS)
        self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                               "amount": "5", "payee": xmr_addr(14)})
        views = [self.state.status(), self.state.accounts(), self.state.config_get(),
                 self.state.requests_list(), self.state.mandates_list(), self.state.ledger_tail({})]
        allowed = {"ledger_pubkey", "prev", "sig"}
        leaks: list[str] = []

        def walk(node, path="") -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    if key in allowed:
                        continue
                    walk(value, f"{path}.{key}")
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    walk(value, f"{path}[{index}]")
            elif isinstance(node, str) and len(node) == 64:
                try:
                    int(node, 16)
                except ValueError:
                    return
                leaks.append(f"{path} looks like a hex secret")

        for index, view in enumerate(views):
            walk(view, f"view{index}")
        self.assertEqual(leaks, [], leaks)


if __name__ == "__main__":
    unittest.main()


class SettingsNormalisationTests(unittest.TestCase):
    """A coin list is a set: the GUI toggles send "the whole list, plus this one"."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        FakeAdapter._next_account = 1
        FakeAdapter.balances = {}
        self.state = WalletState(self.home)
        self.state.fake = FakeAdapter()
        self.state.initialize(PASS, pin=PIN)
        # the PIN gate itself is tested in tests/test_pin.py; these tests are about policy
        # accounting (budgets, fees, sweeps), so they run with the gate off
        self.state.settings.pin_every_payment = False
        self.state.settings.chains_enabled = list(TEST_CHAINS)
        self.state.settings.assets_enabled = list(TEST_ASSETS)
        from walletd import config as config_mod
        config_mod.save(self.state.config_path, self.state.settings, self.state.master)
        self.state.unlock(PASS)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_prefer_monero_is_off_by_default(self) -> None:
        """A fresh install pays in whatever coin is enabled; the XMR preference is opt-in."""
        from walletd import config as config_mod
        self.assertFalse(config_mod.Settings().prefer_monero)

    def test_pin_exists_is_answered_while_locked(self) -> None:
        """A locked wallet still says a PIN is set — the pane needs that to ask for the PIN.

        It used to answer None until unlock, and the pane reads None as "no PIN", so a six-digit
        PIN was sent out as a passphrase and appeared to stop working after every auto-lock.
        """
        self.state.lock()
        pin = self.state.status()["pin"]
        self.assertIs(pin["set"], True)
        self.assertIs(pin["unlocks"], True)

    def test_a_duplicated_coin_list_is_deduped_on_write(self) -> None:
        self.state.config_set({"assets_enabled": ["XMR", "BTC", "XRP", "BTC", "XMR"], "passphrase": PASS})
        self.assertEqual(self.state.settings.assets_enabled, ["XMR", "BTC", "XRP"])
        # and the chains derived from it are unique too
        chains = self.state.settings.chains_enabled
        self.assertEqual(len(chains), len(set(chains)))

    def test_case_and_whitespace_are_normalised(self) -> None:
        self.state.config_set({"assets_enabled": ["xmr ", "Xmr", "btc"], "passphrase": PASS})
        self.assertEqual(self.state.settings.assets_enabled, ["XMR", "BTC"])

    def test_a_duplicated_list_on_disk_heals_on_load(self) -> None:
        from walletd import config as config_mod
        broken = dict(self.state.settings.to_dict())
        broken["assets_enabled"] = ["XMR", "XMR", "BTC"]
        config_mod.save(self.state.config_path, self.state.settings, self.state.master)
        raw = json.loads(self.state.config_path.read_text())
        raw["settings"]["assets_enabled"] = ["XMR", "XMR", "BTC"]
        # re-sign the tampered-but-valid document so the load path is exercised, not the tamper guard
        config_mod.save(self.state.config_path, config_mod.Settings.from_dict(raw["settings"]),
                        self.state.master)
        loaded = config_mod.load(self.state.config_path, self.state.master).settings
        self.assertEqual(loaded.assets_enabled, ["XMR", "BTC"])

    def test_status_reports_which_chains_have_a_wallet(self) -> None:
        """The pane offered "create a wallet" on every visit, even with one sitting right there."""
        from unittest import mock

        self.assertFalse(self.state.status()["wallets"]["monero-stagenet"])

        class _SignerDouble:
            """The signer's wallet calls, without a signer: this test is about what `status` reports
            afterwards. Unpatched, wallet_init probed 127.0.0.1:38084 and, finding nothing, downloaded
            the 120 MB signer release — from a suite that promises to stay offline."""

            def close_wallet(self): return None
            def wallet_exists(self): return False
            def create_wallet(self): return {}
            def address(self): return "57" + "1" * 93

        with mock.patch.object(self.state, "ensure_signer", return_value={"reachable": True}), \
                mock.patch.object(self.state, "_fresh_monero_adapter", return_value=_SignerDouble()):
            self.state.wallet_init({"kind": "monero-stagenet"})
        wallets = self.state.status()["wallets"]
        self.assertTrue(wallets["monero-stagenet"], wallets)
        # and it survives a lock, because the answer comes from the cached address
        self.state.lock()
        self.assertTrue(self.state.status()["wallets"]["monero-stagenet"])

    def test_each_coin_appears_once_on_every_surface(self) -> None:
        self.state.config_set({"assets_enabled": ["XMR", "BTC", "XRP", "BTC"], "passphrase": PASS})
        status = self.state.status()
        self.assertEqual(status["assets"], ["XMR", "BTC", "XRP"])
        self.assertEqual(len(status["assets"]), len(set(status["assets"])))
        kinds = [row["kind"] for row in self.state.balances()["accounts"]]
        self.assertEqual(len(kinds), len(set(kinds)), kinds)


class HardeningTests(unittest.TestCase):
    """The rules that exist for a compromised agent: destinations and the Monero preference."""

    PASS = PASS

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        FakeAdapter._next_account = 1
        FakeAdapter.balances = {}
        self.state = WalletState(self.home)
        self.state.fake = FakeAdapter()
        self.state.initialize(PASS, pin=PIN)
        # the PIN gate itself is tested in tests/test_pin.py; these tests are about policy
        # accounting (budgets, fees, sweeps), so they run with the gate off
        self.state.settings.pin_every_payment = False
        # prefer-Monero ships off now; this class is precisely about that rule, so turn it on
        self.state.settings.prefer_monero = True
        self.state.settings.chains_enabled = list(TEST_CHAINS)
        self.state.settings.assets_enabled = list(TEST_ASSETS)
        from walletd import config as config_mod
        config_mod.save(self.state.config_path, self.state.settings, self.state.master)
        self.state.unlock(PASS)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # ---------- destination gate ----------

    def _sandbox(self, budget: str = "0.2", payees=None) -> str:
        created = self.state.sandbox_create({"kind": "monero-stagenet", "asset": "XMR",
                                             "budget": budget, "lifetime_hours": 24,
                                             "label": "hardening",
                                             "allowed_payees": payees or []})
        funding = created["funding"]
        self.state.approve({"pin": PIN, "request_id": funding["request"]["request_id"],
                            "approval_id": funding["approval_id"], "passphrase": PASS,
                            "confirm_amount": budget})
        return created["sandbox"]["sandbox_id"]

    def test_an_unknown_destination_in_a_sandbox_needs_a_human(self) -> None:
        """The attack this exists for: injection picks a new address, the budget still covers it."""
        sandbox_id = self._sandbox("0.2")
        out = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.05",
                                     "payee": xmr_addr(90), "sandbox_id": sandbox_id,
                                     "_scope": "agent"})
        self.assertFalse(out.get("auto"))
        self.assertEqual(out["decision"]["code"], "needs_operator")
        self.assertIn("approval_id", out)          # a human can still approve this one
        self.assertEqual(self.state.sandboxes[sandbox_id].spent, Decimal("0"))

    def test_enabling_a_coin_keeps_the_simulator_the_caller_asked_for(self) -> None:
        """The coin menu sends chains_enabled and assets_enabled together; the asset branch used to
        overwrite the chain list, so turning the simulator on looked like it worked and did not."""
        self.state.config_set({"chains_enabled": ["monero-stagenet", "sim-mainnet"],
                               "assets_enabled": ["XMR"], "passphrase": PASS})
        self.assertIn("sim-mainnet", self.state.settings.chains_enabled)

    def test_an_allowlisted_destination_is_allowed_by_policy(self) -> None:
        # With the PIN gate off (see setUp) this is the policy layer's verdict: an allowlisted
        # destination is *allowed*. In the product it is still allowed — and still asks for the PIN.

        payee = xmr_addr(91)
        sandbox_id = self._sandbox("0.2", payees=[payee])
        out = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.05",
                                     "payee": payee, "sandbox_id": sandbox_id, "_scope": "agent"})
        self.assertTrue(out.get("auto"), out)
        self.assertEqual(out["decision"]["rule"], "sandbox")

    def test_a_contact_the_agent_saved_is_not_a_blessing(self) -> None:
        """Otherwise the agent could save the attacker as "Aiden" and then auto-pay it."""
        payee = xmr_addr(92)
        self.state.contact_save({"name": "Aiden", "addresses": {"monero-stagenet": payee},
                                 "_scope": "agent"})
        sandbox_id = self._sandbox("0.2")
        out = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.05",
                                     "payee": payee, "sandbox_id": sandbox_id, "_scope": "agent"})
        self.assertFalse(out.get("auto"))
        self.assertEqual(out["decision"]["code"], "needs_operator")

    def test_a_contact_a_human_saved_does_bless(self) -> None:
        payee = xmr_addr(93)
        # a human's contact is a blessing, and blessing costs the passphrase (see
        # test_an_agent_cannot_forge_an_operator_blessing)
        self.state.contact_save({"name": "Vendor", "addresses": {"monero-stagenet": payee},
                                 "_scope": "operator", "passphrase": PASS})
        sandbox_id = self._sandbox("0.2")
        out = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.05",
                                     "payee": payee, "sandbox_id": sandbox_id, "_scope": "agent"})
        self.assertTrue(out.get("auto"), out)

    # ---------- prefer Monero ----------

    def test_an_agent_must_say_why_it_needs_another_coin(self) -> None:
        out = self.state.request_tx({"kind": "bitcoin-testnet", "asset": "BTC", "amount": "0.0001",
                                     "payee": "tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx",
                                     "_scope": "agent"})
        self.assertFalse(out["decision"]["allow"])
        self.assertEqual(out["decision"]["code"], "prefer_monero_needs_reason")
        self.assertIn("XMR", out["decision"]["reason"])

    def test_a_stated_reason_gets_past_the_preference(self) -> None:
        out = self.state.request_tx({"kind": "bitcoin-testnet", "asset": "BTC", "amount": "0.0001",
                                     "payee": "tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx",
                                     "why": "this shop only takes BTC", "_scope": "agent"})
        self.assertNotEqual(out["decision"]["code"], "prefer_monero_needs_reason")

    def test_an_operator_payment_is_not_blocked_by_the_preference(self) -> None:
        out = self.state.request_tx({"kind": "bitcoin-testnet", "asset": "BTC", "amount": "0.0001",
                                     "payee": "tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx",
                                     "_scope": "operator"})
        self.assertNotEqual(out["decision"].get("code"), "prefer_monero_needs_reason")

    def test_status_reports_the_preference_so_the_agent_can_plan(self) -> None:
        status = self.state.status()
        self.assertTrue(status["prefer_monero"])
        self.assertEqual(status["preferred_asset"], "XMR")


class SandboxHardeningTests(unittest.TestCase):
    """The holes the blunt review found in the sandbox: who may bless a destination, whether a
    closed budget can come back, and whether a sweep tells the truth."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        FakeAdapter._next_account = 1
        FakeAdapter.balances = {}
        self.state = WalletState(self.home)
        self.state.fake = FakeAdapter()
        self.state.initialize(PASS, pin=PIN)
        # the PIN gate itself is tested in tests/test_pin.py; these tests are about policy
        # accounting (budgets, fees, sweeps), so they run with the gate off
        self.state.settings.pin_every_payment = False
        self.state.settings.chains_enabled = list(TEST_CHAINS)
        self.state.settings.assets_enabled = list(TEST_ASSETS)
        from walletd import config as config_mod
        config_mod.save(self.state.config_path, self.state.settings, self.state.master)
        self.state.unlock(PASS)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _funded(self, budget: str = "0.2") -> dict:
        made = self.state.sandbox_create({"kind": "monero-stagenet", "asset": "XMR",
                                          "budget": budget, "lifetime_hours": 24,
                                          "allowed_payees": [xmr_addr(n) for n in range(1, 100)]})
        funding = made["funding"]
        self.state.approve({"pin": PIN, "request_id": funding["request"]["request_id"],
                            "approval_id": funding["approval_id"], "passphrase": PASS,
                            "confirm_amount": budget})
        return made["sandbox"]

    def test_an_agent_proposal_cannot_name_its_own_payees(self) -> None:
        """Naming the destination is the whole attack: fund once, pay the attacker forever."""
        made = self.state.sandbox_propose({"kind": "monero-stagenet", "asset": "XMR",
                                           "budget": "0.2", "label": "research",
                                           "allowed_payees": [xmr_addr(77)],
                                           "allowed_profiles": ["agent-profile"]})
        sandbox = self.state.sandboxes[made["sandbox"]["sandbox_id"]]
        self.assertEqual(sandbox.allowed_payees, [])
        self.assertEqual(sandbox.allowed_profiles, [])
        self.assertIn("allowed_payees", made.get("ignored", []))
        # the recommended address is not a blessing: a spend to it still needs a human
        funding = made["funding"]
        self.state.approve({"pin": PIN, "request_id": funding["request"]["request_id"],
                            "approval_id": funding["approval_id"], "passphrase": PASS,
                            "confirm_amount": "0.2"})
        out = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.05",
                                     "payee": xmr_addr(77), "sandbox_id": sandbox.sandbox_id,
                                     "_scope": "agent"})
        self.assertFalse(out.get("auto"), out)

    def test_revoking_cancels_the_funding_request(self) -> None:
        made = self.state.sandbox_create({"kind": "monero-stagenet", "asset": "XMR",
                                          "budget": "0.2", "lifetime_hours": 24})
        funding = made["funding"]
        self.state.sandbox_revoke({"sandbox_id": made["sandbox"]["sandbox_id"], "sweep": False})
        req = self.state.requests[funding["request"]["request_id"]]
        self.assertEqual(req.status, "cancelled")
        # and the approval token it came with is dead
        with self.assertRaises(WalletError):
            self.state.approve({"pin": PIN, "request_id": req.request_id, "approval_id": funding["approval_id"],
                                "passphrase": PASS, "confirm_amount": "0.2"})

    def test_a_revoked_budget_cannot_be_spent_from(self) -> None:
        sandbox = self._funded("0.2")
        self.state.sandbox_revoke({"sandbox_id": sandbox["sandbox_id"], "sweep": False})
        out = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.01",
                                     "payee": xmr_addr(78), "sandbox_id": sandbox["sandbox_id"],
                                     "_scope": "agent"})
        self.assertFalse(out["decision"]["allow"])
        self.assertEqual(out["decision"]["code"], "sandbox_closed")

    def test_a_sweep_can_still_rescue_a_revoked_budget(self) -> None:
        sandbox = self._funded("0.2")
        self.state.fake.set_balance(self.state.sandboxes[sandbox["sandbox_id"]].account_index, "0.15")
        self.state.sandbox_revoke({"sandbox_id": sandbox["sandbox_id"], "sweep": False})
        swept = self.state.sandbox_sweep({"sandbox_id": sandbox["sandbox_id"]})
        self.assertTrue(swept["sent"], swept)      # money must still be recoverable after a revoke

    def test_a_sweep_that_did_not_go_out_does_not_claim_it_did(self) -> None:
        """A sweep is broadcast directly now (a self-transfer, not a payment), so the failure to
        model is the chain refusing it — not a policy denial."""
        sandbox = self._funded("0.2")
        sid = sandbox["sandbox_id"]
        self.state.fake.set_balance(self.state.sandboxes[sid].account_index, "0.15")

        def refuse(_plan):
            raise ChainError("insufficient_funds", "the account does not hold that much")

        self.state.fake.broadcast = refuse
        out = self.state.sandbox_sweep({"sandbox_id": sid})
        self.assertEqual(out["swept"], "0")
        self.assertFalse(out["sent"])
        self.assertIn("did not go through", out["note"])
        self.assertTrue(self.state.sandboxes[sid].enabled, "a failed sweep must not close the sandbox")

    def test_an_expired_budget_cannot_be_funded(self) -> None:
        made = self.state.sandbox_create({"kind": "monero-stagenet", "asset": "XMR",
                                          "budget": "0.2", "lifetime_hours": 1})
        sandbox = self.state.sandboxes[made["sandbox"]["sandbox_id"]]
        sandbox.expires_at = "2000-01-01T00:00:00Z"
        funding = made["funding"]
        with self.assertRaises(WalletError) as caught:
            self.state.approve({"pin": PIN, "request_id": funding["request"]["request_id"],
                                "approval_id": funding["approval_id"], "passphrase": PASS,
                                "confirm_amount": "0.2"})
        self.assertEqual(caught.exception.code, "sandbox_expired")
        self.assertFalse(sandbox.enabled)

    def test_an_agent_cannot_sweep_a_budget_shut(self) -> None:
        """Closing a budget is the operator's call: a sweep from the agent ends its own spending room."""
        sandbox = self._funded("0.2")
        sid = sandbox["sandbox_id"]
        own = self.state.fake.address()
        with self.assertRaises(WalletError) as caught:
            self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.01",
                                   "payee": own, "sandbox_id": sid, "sweep_of": sid,
                                   "_scope": "agent"})
        self.assertEqual(caught.exception.code, "sweep_operator_only")

    def test_spend_receipts_say_which_budget_paid(self) -> None:
        sandbox = self._funded("0.2")
        out = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.05",
                                     "payee": xmr_addr(79), "sandbox_id": sandbox["sandbox_id"]})
        self.assertTrue(out.get("auto"), out)
        entry = [e for e in self.state.ledger.entries()
                 if e.get("kind") == "spend_request" and e.get("decision") == "approved"][-1]
        self.assertEqual(entry["details"].get("sandbox_id"), sandbox["sandbox_id"])
        # the budget was charged the fee too, so remaining matches what the chain holds
        live = self.state.sandboxes[sandbox["sandbox_id"]]
        self.assertEqual(live.spent, Decimal("0.05002"))


class MandateByNameTests(unittest.TestCase):
    """An autopay names its payee the same way a payment does."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        FakeAdapter._next_account = 1
        FakeAdapter.balances = {}
        self.state = WalletState(self.home)
        self.state.fake = FakeAdapter()
        self.state.initialize(PASS, pin="123456")
        self.state.settings.chains_enabled = list(TEST_CHAINS)
        self.state.settings.assets_enabled = list(TEST_ASSETS)
        self.state.adapters = {kind: self.state.fake for kind in TEST_CHAINS}
        self.state.settings.pin_every_payment = False

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_a_saved_contact_name_resolves_when_proposing_an_autopay(self) -> None:
        address = xmr_addr(60)
        self.state.contact_save({"name": "adrian", "addresses": {"monero-stagenet": address},
                                 "_scope": "operator", "passphrase": PASS})
        proposal = self.state.mandates_propose({"kind": "monero-stagenet", "asset": "XMR",
                                                "payee": "adrian", "per_tx_cap": "0.01",
                                                "period_cap": "0.02", "absolute_cap": "0.1",
                                                "period_hours": 168, "label": "weekly"})
        self.assertEqual(proposal["mandate"]["payee"], address)
        self.assertEqual(proposal["mandate"]["status"], "proposed")

    def test_a_weekly_autopay_is_not_expired_by_its_own_default(self) -> None:
        """The lifetime ceiling used to be 168 h, so a weekly autopay died when payment two was due."""
        proposal = self.state.mandates_propose({"kind": "monero-stagenet", "asset": "XMR",
                                                "payee": xmr_addr(61), "per_tx_cap": "0.01",
                                                "period_cap": "0.02", "absolute_cap": "0.5",
                                                "period_hours": 168, "label": "weekly"})
        expires = proposal["mandate"]["expires_at"]
        self.assertGreater(expires, "2026-11-15", "a weekly autopay must outlive its second payment")


class InternalTransferTests(unittest.TestCase):
    """Funding a sandbox and sweeping it home are our own money moving between our own accounts.

    Counting them as spending made `wallet spend` report 0.04 after a 0.0178 payment plus a 0.02
    budget, and charged the daily cap twice for the same coin.
    """

    def setUp(self) -> None:
        import tempfile
        from pathlib import Path as _Path

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        FakeAdapter._next_account = 1
        FakeAdapter.balances = {}
        self.state = WalletState(_Path(tmp.name))
        self.state.fake = FakeAdapter()
        self.state.initialize(PASS, pin="123456")
        self.state.settings.chains_enabled = list(TEST_CHAINS)
        self.state.settings.assets_enabled = list(TEST_ASSETS)
        self.state.adapters = {kind: self.state.fake for kind in TEST_CHAINS}
        self.state.unlock(PASS)

    def test_a_funded_budget_is_not_spending(self) -> None:
        base = {"kind": "monero-stagenet", "asset": "XMR", "amount": "0.02", "payee": "", "why": "x"}
        self.state.ledger.append("spend_request", "approved", "sandbox",
                                 {**base, "funding_for": "sbx_abc"})
        self.state.ledger.append("spend_request", "approved", "manual_approval",
                                 {**base, "amount": "0.01", "payee": xmr_addr(51)})
        totals = self.state.spend_totals({})
        self.assertEqual(float(totals["totals"].get("XMR") or 0), 0.01,
                         "only the payment to somebody is spending")
        self.assertEqual(float(totals["internal_transfers"].get("XMR") or 0), 0.02)


class ForgedBlessingTests(unittest.TestCase):
    """A contact saved *as the operator* is what blesses a destination for a sandbox.

    Creating one used to cost nothing, so a prompt-injected agent holding the operator token (a 0600
    file on the same uid) could mint a blessing and let a funded sandbox auto-pay any address. It now
    costs the passphrase, and an unknown scope fails closed to "agent".
    """

    def setUp(self) -> None:
        import tempfile
        from pathlib import Path as _Path

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        FakeAdapter._next_account = 1
        FakeAdapter.balances = {}
        self.state = WalletState(_Path(tmp.name))
        self.state.fake = FakeAdapter()
        self.state.initialize(PASS, pin="123456")
        self.state.settings.chains_enabled = list(TEST_CHAINS)
        self.state.settings.assets_enabled = list(TEST_ASSETS)
        self.state.adapters = {kind: self.state.fake for kind in TEST_CHAINS}
        self.state.unlock(PASS)

    def _save(self, **extra):
        return self.state.contact_save({"name": "Vendor",
                                        "addresses": {"monero-stagenet": xmr_addr(94)}, **extra})

    def test_an_operator_contact_needs_the_passphrase(self) -> None:
        with self.assertRaises(WalletError) as caught:
            self._save(_scope="operator")
        self.assertEqual(caught.exception.code, "passphrase_required")
        self.assertEqual(self.state.contacts, {})

    def test_the_passphrase_makes_it_a_blessing(self) -> None:
        out = self._save(_scope="operator", passphrase=PASS)
        self.assertEqual(out["contact"]["created_by"], "operator")

    def test_an_unknown_scope_is_never_a_blessing(self) -> None:
        out = self._save()
        self.assertEqual(out["contact"]["created_by"], "agent")


# ---------- balances: every enabled chain tells the truth ----------


class EvmLike:
    """An EVM-shaped chain double: native coin plus tokens, one `balance_all` read."""

    asset = "ETH"

    def __init__(self, kind: str, amounts: dict, fail: str = "") -> None:
        self.kind = kind
        self.amounts = dict(amounts)
        self.fail = fail

    def status(self):
        return {"kind": self.kind, "available": not self.fail, "provider": "test-rpc"}

    def address(self) -> str:
        return "0x" + "ab" * 20

    def balance_all(self):
        if self.fail:
            raise ChainError("rpc_unreachable", self.fail)
        return dict(self.amounts)


class PairLike:
    """A single-coin chain double (BTC/XRP shape): holds `asset` only."""

    supports_accounts = False

    def __init__(self, kind: str, asset: str, amount, fail: str = "") -> None:
        self.kind = kind
        self.asset = asset
        self.amount = amount
        self.fail = fail

    def status(self):
        return {"kind": self.kind, "available": not self.fail, "provider": "test-node"}

    def address(self) -> str:
        return "r-test-address"

    def balance(self):
        if self.fail:
            raise ChainError("node_unreachable", self.fail)
        return self.amount


class BalancesTruthTests(unittest.TestCase):
    """`balances()` is the answer to "where is my money". Each test pins one way it used to lie."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        FakeAdapter._next_account = 1
        FakeAdapter.balances = {}
        self.state = WalletState(self.home)
        self.state.fake = FakeAdapter()
        self.state.initialize(PASS, pin=PIN)
        self.state.settings.chains_enabled = ["base-mainnet", "bitcoin-mainnet", "xrp-mainnet"]
        self.state.settings.assets_enabled = ["ETH", "USDC", "BTC", "XRP"]
        config.save(self.state.config_path, self.state.settings, self.state.master)
        self.state.adapters = {}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _rows(self, **kwargs) -> list:
        payload = self.state.balances(fresh=True, **kwargs)
        return payload["accounts"]

    def test_an_errored_chain_still_appears_with_its_error_text(self) -> None:
        self.state.adapters = {
            "bitcoin-mainnet": PairLike("bitcoin-mainnet", "BTC", Decimal("0.5")),
            "xrp-mainnet": PairLike("xrp-mainnet", "XRP", None, fail="HTTP 503 from xrplcluster.com"),
        }
        rows = self._rows()
        self.assertEqual({r["kind"] for r in rows}, {"base-mainnet", "bitcoin-mainnet", "xrp-mainnet"})
        xrp = next(r for r in rows if r["kind"] == "xrp-mainnet")
        self.assertFalse(xrp["available"])
        self.assertIsNone(xrp["balance"], "an unread balance must not be printed as 0")
        self.assertIn("HTTP 503 from xrplcluster.com", xrp["error"])
        self.assertNotEqual(xrp.get("state"), "locked")

    def test_a_real_balance_is_reported_unmodified(self) -> None:
        # the live base-mainnet wallet reading, digit for digit
        self.state.adapters = {
            "base-mainnet": EvmLike("base-mainnet", {"ETH": Decimal("0.000193371234095216"),
                                                     "USDC": Decimal("0")}),
            "bitcoin-mainnet": PairLike("bitcoin-mainnet", "BTC", Decimal("0.00000001")),
            "xrp-mainnet": PairLike("xrp-mainnet", "XRP", Decimal("10.000000")),
        }
        rows = self._rows()
        eth = next(r for r in rows if r["kind"] == "base-mainnet" and r["asset"] == "ETH")
        self.assertTrue(eth["available"])
        self.assertEqual(eth["balance"], "0.000193371234095216")
        usdc = next(r for r in rows if r["kind"] == "base-mainnet" and r["asset"] == "USDC")
        self.assertEqual(usdc["balance"], "0", "USDC on the same chain is read too")
        self.assertIn("tokens", eth)
        btc = next(r for r in rows if r["kind"] == "bitcoin-mainnet")
        self.assertEqual(btc["balance"], "0.00000001")
        xrp = next(r for r in rows if r["kind"] == "xrp-mainnet")
        self.assertEqual(xrp["balance"], "10.000000", "no rounding on the way out")

    def test_no_enabled_chain_disappears(self) -> None:
        # one chain answered, one recorded a reason, one has neither adapter nor note
        self.state.adapters = {"bitcoin-mainnet": PairLike("bitcoin-mainnet", "BTC", Decimal("1"))}
        self.state.chain_notes = {"base-mainnet": "EVM RPC unreachable"}
        rows = self._rows()
        kinds = [r["kind"] for r in rows]
        self.assertEqual(kinds, ["base-mainnet", "base-mainnet", "bitcoin-mainnet", "xrp-mainnet"],
                         "one row per (chain, enabled coin), in the operator's chain order")
        self.assertEqual([(r["kind"], r["asset"]) for r in rows],
                         [("base-mainnet", "ETH"), ("base-mainnet", "USDC"),
                          ("bitcoin-mainnet", "BTC"), ("xrp-mainnet", "XRP")])
        base = next(r for r in rows if r["kind"] == "base-mainnet")
        self.assertEqual(base["error"], "EVM RPC unreachable")
        self.assertIsNone(base["balance"])
        xrp = next(r for r in rows if r["kind"] == "xrp-mainnet")
        self.assertFalse(xrp["available"])
        self.assertIsNone(xrp["balance"])
        self.assertTrue(xrp["error"])

    def test_a_disabled_chain_is_omitted_not_zeroed(self) -> None:
        self.state.settings.chains_enabled = ["bitcoin-mainnet"]
        self.state.settings.assets_enabled = ["BTC"]
        self.state.adapters = {"bitcoin-mainnet": PairLike("bitcoin-mainnet", "BTC", Decimal("2"))}
        rows = self._rows()
        self.assertEqual([(r["kind"], r["asset"]) for r in rows], [("bitcoin-mainnet", "BTC")])

    def test_a_warm_cache_does_not_drop_a_newly_enabled_chain(self) -> None:
        self.state.adapters = {"bitcoin-mainnet": PairLike("bitcoin-mainnet", "BTC", Decimal("1"))}
        self.state.balances(fresh=True)
        self.state.settings.chains_enabled = ["bitcoin-mainnet", "xrp-mainnet"]
        rows = self.state.balances(fresh=False)["accounts"]     # inside the TTL
        self.assertEqual([(r["kind"], r["asset"]) for r in rows],
                         [("bitcoin-mainnet", "BTC"), ("xrp-mainnet", "XRP")])

    def test_a_locked_wallet_shows_every_chain_as_locked_not_zero(self) -> None:
        rows = self._rows_locked()
        self.assertEqual(sorted(r["kind"] for r in rows),
                         ["base-mainnet", "base-mainnet", "bitcoin-mainnet", "xrp-mainnet"])
        for row in rows:
            self.assertEqual(row["state"], "locked")
            self.assertIsNone(row["balance"])

    def _rows_locked(self) -> list:
        probe = tempfile.TemporaryDirectory()
        self.addCleanup(probe.cleanup)
        state = WalletState(Path(probe.name))
        state.initialize(PASS, pin=PIN)
        state.settings.chains_enabled = ["base-mainnet", "bitcoin-mainnet", "xrp-mainnet"]
        state.settings.assets_enabled = ["ETH", "USDC", "BTC", "XRP"]
        config.save(state.config_path, state.settings, state.master)
        state.lock()
        return state.balances(fresh=True)["accounts"]

    def test_an_enabled_chain_the_build_cannot_reach_still_gets_a_row(self) -> None:
        self.state.settings.chains_enabled = ["ethereum-mainnet", "bitcoin-mainnet"]
        self.state.settings.assets_enabled = ["ETH", "BTC"]
        self.state.adapters = {"bitcoin-mainnet": PairLike("bitcoin-mainnet", "BTC", Decimal("1"))}
        rows = self._rows()
        eth = next(r for r in rows if r["kind"] == "ethereum-mainnet")
        self.assertFalse(eth["available"])
        self.assertIsNone(eth["balance"])
        self.assertIn("not supported", eth["error"])
