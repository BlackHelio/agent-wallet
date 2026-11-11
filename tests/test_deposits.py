"""Deposit addresses: receive into an account, merge it home, prove it with a receipt."""

from __future__ import annotations

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from tests.test_state import FakeAdapter, PASS, TEST_ASSETS, TEST_CHAINS, WalletError, WalletState

PIN = "123456"


class DepositAddressTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        FakeAdapter._next_account = 1
        FakeAdapter.balances = {0: Decimal("10")}
        self.state = WalletState(self.home)
        self.state.fake = FakeAdapter()
        self.state.initialize(PASS, pin=PIN)
        self.state.settings.chains_enabled = list(TEST_CHAINS)
        self.state.settings.assets_enabled = list(TEST_ASSETS)
        self.state.adapters = {kind: self.state.fake for kind in TEST_CHAINS}
        self.state.settings.pin_every_payment = False

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _create(self, label: str = "shop", auto: bool = True) -> dict:
        return self.state.deposit_create({"kind": "monero-stagenet", "label": label,
                                          "auto_merge": auto,
                                          "passphrase": PASS if auto else None})["deposit"]

    # ---------- an address you can hand out ----------
    def test_creating_a_deposit_address_opens_its_own_account(self) -> None:
        row = self._create("order 42")
        self.assertTrue(row["address"])
        self.assertNotEqual(row["account_index"], 0)
        self.assertEqual(row["label"], "order 42")
        self.assertEqual(row["balance"], "0")
        self.assertTrue(row["auto_merge"])

    def test_the_address_survives_a_restart(self) -> None:
        row = self._create("shop")
        again = WalletState(self.home)
        again.fake = FakeAdapter()
        again.unlock(PASS)
        again._load_deposits()
        self.assertIn(row["deposit_id"], again.deposits)
        self.assertEqual(again.deposits[row["deposit_id"]].address, row["address"])

    # ---------- money arrives ----------
    def test_an_incoming_payment_shows_on_the_deposit_and_not_in_the_main_balance(self) -> None:
        row = self._create("shop")
        FakeAdapter.balances[row["account_index"]] = Decimal("0.5")
        listed = self.state.deposits_list()["deposits"][0]
        self.assertEqual(listed["balance"], "0.5")
        self.assertEqual(listed["unlocked"], "0.5")

    # ---------- merging ----------
    def test_merge_sends_to_our_own_account_and_writes_a_receipt(self) -> None:
        row = self._create("shop")
        FakeAdapter.balances[row["account_index"]] = Decimal("0.5")
        result = self.state.deposit_merge({"deposit_id": row["deposit_id"]})
        self.assertTrue(result["sent"])
        # the payee is our own account 0, and the amount is what left, minus the fee
        amount, payee, account = self.state.fake.planned[-1]
        self.assertEqual(payee, self.state.fake.address())
        self.assertEqual(account, row["account_index"])
        self.assertLess(Decimal(result["merged"]), Decimal("0.5"))
        receipt = [e for e in self.state.ledger.tail(20) if e["kind"] == "deposit"
                   and e["decision"] == "merged"][-1]
        self.assertEqual(receipt["details"]["deposit_id"], row["deposit_id"])
        self.assertEqual(receipt["details"]["to"], self.state.fake.address())
        self.assertEqual(receipt["details"]["tx_hash"], result["tx_hash"])
        self.assertEqual(self.state.deposits[row["deposit_id"]].merged_total, result["merged"])

    def test_an_empty_deposit_merges_nothing_and_says_why(self) -> None:
        row = self._create("shop")
        result = self.state.deposit_merge({"deposit_id": row["deposit_id"]})
        self.assertFalse(result["sent"])
        self.assertIn("unlocks", result["note"])

    def test_a_balance_below_the_fee_is_skipped_with_a_receipt(self) -> None:
        row = self._create("shop")
        FakeAdapter.balances[row["account_index"]] = Decimal("0.000001")
        result = self.state.deposit_merge({"deposit_id": row["deposit_id"]})
        self.assertFalse(result["sent"])
        kinds = [e["decision"] for e in self.state.ledger.tail(20) if e["kind"] == "deposit"]
        self.assertIn("merge_skipped", kinds)

    def test_an_unknown_deposit_is_refused(self) -> None:
        with self.assertRaises(WalletError) as caught:
            self.state.deposit_merge({"deposit_id": "dep_nope"})
        self.assertEqual(caught.exception.code, "unknown_deposit")

    # ---------- the timer ----------
    def test_auto_merge_pays_only_the_deposits_that_asked_for_it(self) -> None:
        armed = self._create("armed", auto=True)
        manual = self._create("manual", auto=False)
        FakeAdapter.balances[armed["account_index"]] = Decimal("0.3")
        FakeAdapter.balances[manual["account_index"]] = Decimal("0.3")
        merged = self.state.deposits_auto_merge()
        self.assertEqual([m["deposit_id"] for m in merged if m.get("sent")], [armed["deposit_id"]])
        self.assertEqual(self.state.deposits[manual["deposit_id"]].merged_total, "0")

    def test_one_failure_does_not_stop_the_other_deposits(self) -> None:
        first = self._create("first")
        second = self._create("second")
        for row in (first, second):
            FakeAdapter.balances[row["account_index"]] = Decimal("0.2")

        original = self.state.deposit_merge
        calls = {"n": 0}

        def flaky(params):
            calls["n"] += 1
            if calls["n"] == 1:
                raise WalletError("chain_unavailable", "node down")
            return original(params)

        self.state.deposit_merge = flaky
        result = self.state.deposits_auto_merge()
        self.assertTrue(any(r.get("error") for r in result))
        self.assertTrue(any(r.get("sent") for r in result))

    # ---------- authority ----------
    def test_an_agent_cannot_create_or_merge(self) -> None:
        from walletd.tokens import AGENT, OPERATOR
        from walletd.state import build_methods

        methods = build_methods()
        for name in ("deposit_create", "deposit_merge", "deposit_set_auto", "deposits_auto_merge"):
            self.assertEqual(methods[name][0], OPERATOR, f"{name} is agent-reachable")
        self.assertEqual(methods["deposits_list"][0], AGENT)


if __name__ == "__main__":
    unittest.main()


class OrderMergeTests(unittest.TestCase):
    """A paid order's account is money sitting where nothing reads it: `balances` reads account 0."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        FakeAdapter._next_account = 1
        FakeAdapter.balances = {0: Decimal("10")}
        self.state = WalletState(self.home)
        self.state.fake = FakeAdapter()
        self.state.initialize(PASS, pin=PIN)
        self.state.settings.chains_enabled = list(TEST_CHAINS)
        self.state.settings.assets_enabled = list(TEST_ASSETS)
        self.state.adapters = {kind: self.state.fake for kind in TEST_CHAINS}
        self.state.settings.pin_every_payment = False

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _order(self) -> dict:
        return self.state.receive_address({"kind": "monero-stagenet", "label": "order",
                                           "expected": "0.05"})

    def test_a_paid_order_is_swept_into_the_main_balance_once(self) -> None:
        order = self._order()
        FakeAdapter.balances[order["account_index"]] = Decimal("0.05")
        merged = self.state.orders_merge()
        self.assertEqual(len(merged), 1, merged)
        self.assertTrue(merged[0]["sent"], merged)
        # everything spendable left the order's account (the fake only debits the transfer, so what
        # remains is at most the fee we already subtracted)
        self.assertLessEqual(FakeAdapter.balances[order["account_index"]], Decimal("0.00002"))
        receipt = [e for e in self.state.ledger.tail(20)
                   if e["kind"] == "order" and e["decision"] == "merged"][-1]
        self.assertEqual(receipt["details"]["order_id"], order["order_id"])
        # a second pass does not pay twice
        self.assertEqual(self.state.orders_merge(), [])

    def test_an_unpaid_order_is_not_touched(self) -> None:
        self._order()
        self.assertEqual([r for r in self.state.orders_merge() if r.get("sent")], [])

    def test_the_sweep_goes_to_our_own_account_only(self) -> None:
        order = self._order()
        FakeAdapter.balances[order["account_index"]] = Decimal("0.05")
        self.state.orders_merge()
        _amount, payee, _account = self.state.fake.planned[-1]
        self.assertEqual(payee, self.state.fake.address())


class MergeGuardTests(unittest.TestCase):
    """The merge path is the only place the daemon moves money without a human in the loop."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        FakeAdapter._next_account = 1
        FakeAdapter.balances = {0: Decimal("10")}
        self.state = WalletState(self.home)
        self.state.fake = FakeAdapter()
        self.state.initialize(PASS, pin=PIN)
        self.state.settings.chains_enabled = list(TEST_CHAINS)
        self.state.settings.assets_enabled = list(TEST_ASSETS)
        self.state.adapters = {kind: self.state.fake for kind in TEST_CHAINS}
        self.state.settings.pin_every_payment = False

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_account_zero_is_never_merged(self) -> None:
        """A forged deposits.json pointing at account 0 would pay a fee to move 10 XMR to itself."""
        before = self.state.fake.broadcast_count
        with self.assertRaises(WalletError) as caught:
            self.state._merge_account("monero-stagenet", 0, "forged", {"deposit_id": "dep_x"})
        self.assertEqual(caught.exception.code, "bad_deposit")
        self.assertEqual(self.state.fake.broadcast_count, before)

    def test_a_sandbox_account_is_never_merged_as_a_deposit(self) -> None:
        created = self.state.sandbox_create({"kind": "monero-stagenet", "asset": "XMR",
                                             "budget": "0.1", "lifetime_hours": 24})
        sandbox = self.state.sandboxes[created["sandbox"]["sandbox_id"]]
        before = sandbox.enabled
        with self.assertRaises(WalletError) as caught:
            self.state._merge_account("monero-stagenet", sandbox.account_index, "hijack",
                                      {"deposit_id": "dep_x"})
        self.assertEqual(caught.exception.code, "sandbox_account")
        self.assertEqual(sandbox.enabled, before, "the refusal must not touch the sandbox")

    def test_orders_merge_refuses_to_act_on_a_broken_chain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            state = WalletState(home)
            state.fake = FakeAdapter()
            state.initialize(PASS, pin=PIN)
            state.settings.chains_enabled = list(TEST_CHAINS)
            state.settings.assets_enabled = list(TEST_ASSETS)
            state.adapters = {kind: state.fake for kind in TEST_CHAINS}
            state.settings.pin_every_payment = False
            order = state.receive_address({"kind": "monero-stagenet", "label": "o",
                                           "expected": "0.05"})
            FakeAdapter.balances[order["account_index"]] = Decimal("0.05")
            # forge a line into the chain on disk; verify() must notice and the sweep must stop
            with state.ledger.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"seq": 9999, "kind": "receive_address",
                                         "decision": "created",
                                         "details": {"order_id": "ord_forged",
                                                     "account_index": 0}}) + "\n")
            state.ledger = type(state.ledger)(state.ledger.path, b"\x00" * 32) if False else state.ledger
            before = state.fake.broadcast_count
            self.assertEqual(state.orders_merge(), [])
            self.assertEqual(state.fake.broadcast_count, before)


class PooledSandboxTests(unittest.TestCase):
    """Bitcoin (and any chain without accounts) gets a budget the daemon enforces, not a transfer."""

    class NoAccounts(FakeAdapter):
        supports_accounts = False
        kind = "bitcoin-testnet"
        asset = "BTC"

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        FakeAdapter._next_account = 1
        FakeAdapter.balances = {0: Decimal("1")}
        self.state = WalletState(self.home)
        self.fake = self.NoAccounts()
        self.state.fake = self.fake
        self.state.initialize(PASS, pin=PIN)
        self.state.settings.chains_enabled = ["bitcoin-testnet"]
        self.state.settings.assets_enabled = ["BTC"]
        self.state.adapters = {"bitcoin-testnet": self.fake}
        self.state.settings.pin_every_payment = False

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_a_chain_without_accounts_gets_a_pooled_budget_without_moving_money(self) -> None:
        created = self.state.sandbox_create({"kind": "bitcoin-testnet", "asset": "BTC",
                                             "budget": "0.0005", "hours": 4, "label": "btc task"})
        sandbox = created["sandbox"]
        self.assertEqual(sandbox["mode"], "pooled")
        self.assertEqual(sandbox["account_index"], -1)
        self.assertTrue(sandbox["enabled"], "a pooled budget is armed at once: nothing to fund")
        self.assertEqual(created["funding"]["status"], "not_needed")
        self.assertEqual(self.fake.broadcast_count, 0, "no funding transfer may be sent")

    def test_a_pooled_budget_caps_the_agent(self) -> None:
        payee = "tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx"
        created = self.state.sandbox_create({"kind": "bitcoin-testnet", "asset": "BTC",
                                             "budget": "0.0005", "hours": 4,
                                             "allowed_payees": [payee], "passphrase": PASS})
        sandbox_id = created["sandbox"]["sandbox_id"]
        over = self.state.request_tx({"kind": "bitcoin-testnet", "asset": "BTC",
                                      "amount": "0.001", "payee": payee, "why": "vendor",
                                      "sandbox_id": sandbox_id, "_scope": "agent"})
        self.assertFalse(over["decision"]["allow"])
        self.assertEqual(over["decision"]["code"], "sandbox_budget")
        fine = self.state.request_tx({"kind": "bitcoin-testnet", "asset": "BTC",
                                      "amount": "0.0001", "payee": payee, "why": "vendor",
                                      "sandbox_id": sandbox_id, "_scope": "agent"})
        self.assertTrue(fine["decision"]["allow"], fine)
        self.assertEqual(fine["request"]["kind"], "bitcoin-testnet")

    def test_a_pooled_budget_says_there_is_nothing_to_sweep(self) -> None:
        created = self.state.sandbox_create({"kind": "bitcoin-testnet", "asset": "BTC",
                                             "budget": "0.0005", "hours": 4})
        result = self.state.sandbox_sweep({"sandbox_id": created["sandbox"]["sandbox_id"]})
        self.assertFalse(result["sent"])
        self.assertEqual(result["swept"], "0")
        self.assertIn("never moved", result["note"])
