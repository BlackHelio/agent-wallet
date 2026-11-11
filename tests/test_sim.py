"""The simulated chain must behave like a chain, not like a comment.

`sim-mainnet` is what the demo uses to show funding a sandbox, spending from it, sweeping the
remainder home and tearing it down. Every one of those numbers used to read zero: the adapter
bookkept nothing, so a revoke reported `swept: "0"` and a self-destruct receipt reported `left: 0`.
These tests pin the modelled balances — exact `Decimal` values, no floats — and prove the adapter
makes no network call to produce them.
"""

from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from walletd.chains.base import ChainError
from walletd.chains.monero import XmrAdapter
from walletd.chains.sim import FEE, SimLedger

PIN = "123456"   # the per-payment secret; every approval needs it

FUNDED = Decimal("0.004")          # the demo budget
SPEND = Decimal("0.001")           # the demo spend
RESERVE = Decimal("0.0005")        # settings.fee_cap["XMR"], what sandbox_sweep keeps back
EXTERNAL = "4" + "1" + "z" * 93    # nothing of ours: paying it must lower the total


class NoNetwork:
    """Stand-in for JsonRpc: any call is a test failure, not a slow timeout."""

    url = "http://127.0.0.1:1/json_rpc"

    def call(self, method, params=None):  # noqa: ANN001 - mirrors JsonRpc.call
        raise AssertionError(f"the simulator tried to reach a node: {method}")


class SimChainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.adapter = self._adapter()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _adapter(self) -> XmrAdapter:
        adapter = XmrAdapter("sim-mainnet", "http://127.0.0.1:1/json_rpc", "agent-wallet", "",
                             provider="simulated", simulate=True, home=self.home)
        adapter.rpc = NoNetwork()          # type: ignore[assignment]  # every read below must be local
        return adapter

    # ---------- helpers ----------

    def _sandbox(self) -> dict:
        return self.adapter.create_account("agent sandbox")

    def _send(self, amount, payee: str, account: int = 0) -> dict:
        plan = self.adapter.plan_transfer(Decimal(str(amount)), payee, account_index=account)
        return self.adapter.broadcast(plan)

    def _fund(self, account: dict, amount=FUNDED) -> dict:
        return self._send(amount, account["address"], account=0)

    def _balance(self, index: int) -> Decimal:
        return Decimal(str(self.adapter.balance_at(index)))

    # ---------- the model ----------

    def test_only_account_zero_is_seeded_and_never_for_a_new_account(self) -> None:
        self.assertEqual(self._balance(0), Decimal("1"))
        # reading balances must not create the ledger file: nothing has moved yet
        self.assertFalse((self.home / "sim-balances.json").exists())
        account = self._sandbox()
        self.assertEqual(self._balance(account["account_index"]), Decimal("0"))

    def test_funding_then_a_partial_spend_leaves_the_right_remainder(self) -> None:
        account = self._sandbox()
        self._fund(account)
        self.assertEqual(self._balance(account["account_index"]), FUNDED)
        # the sender paid the amount *and* the fee
        self.assertEqual(self._balance(0), Decimal("1") - FUNDED - FEE)

        self._send(SPEND, EXTERNAL, account=account["account_index"])
        self.assertEqual(self._balance(account["account_index"]), Decimal("0.00297"))

    def test_sweep_returns_the_remainder_home(self) -> None:
        account = self._sandbox()
        self._fund(account)
        self._send(SPEND, EXTERNAL, account=account["account_index"])
        sandbox = self._balance(account["account_index"])
        home_before = self._balance(0)

        # what state.sandbox_sweep sends: everything except one fee-cap of headroom
        swept = sandbox - RESERVE
        self._send(swept, self.adapter.address(), account=account["account_index"])

        self.assertEqual(self._balance(0), home_before + swept)
        self.assertEqual(self._balance(account["account_index"]), RESERVE - FEE)   # dust, honestly
        self.assertEqual(str(swept), "0.00247")

    def test_a_one_time_sandbox_that_spent_its_budget_leaves_zero(self) -> None:
        account = self._sandbox()
        self._fund(account)
        # the whole spendable balance goes to the purchase (amount + fee), as a one-time sandbox does
        self._send(FUNDED - FEE, EXTERNAL, account=account["account_index"])
        self.assertEqual(self._balance(account["account_index"]), Decimal("0"))
        # the teardown receipt reports exactly what is left, and that is a real zero
        self.assertEqual(str(self._balance(account["account_index"])), "0")

    def test_paying_an_external_address_lowers_the_total(self) -> None:
        account = self._sandbox()
        self._fund(account)
        total_before = self._ledger_total()
        self._send(SPEND, EXTERNAL, account=account["account_index"])
        self.assertEqual(self._ledger_total(), total_before - SPEND - FEE)

    def test_a_payment_between_our_own_accounts_only_costs_the_fee(self) -> None:
        account = self._sandbox()
        self._fund(account)
        total_before = self._ledger_total()
        self._send(SPEND, self.adapter.address(), account=account["account_index"])
        self.assertEqual(self._ledger_total(), total_before - FEE)
        self.assertEqual(self._balance(0), Decimal("0.99597") + SPEND)

    def test_a_restart_reloads_balances_and_addresses(self) -> None:
        account = self._sandbox()
        self._fund(account)
        self._send(SPEND, EXTERNAL, account=account["account_index"])
        expected = {i["account_index"]: i["balance"] for i in self.adapter.accounts()}
        path = self.home / "sim-balances.json"
        from tests import assert_private
        assert_private(self, path)

        restarted = self._adapter()
        self.assertEqual(restarted.address(), self.adapter.address())
        self.assertEqual(restarted.create_account("next")["account_index"], 2)
        self.assertEqual({i["account_index"]: i["balance"] for i in restarted.accounts()},
                         {**expected, 2: "0"})
        self.assertEqual(restarted.balance_at(account["account_index"]),
                         self.adapter.balance_at(account["account_index"]))

    def test_insufficient_funds_is_refused_at_plan_time(self) -> None:
        with self.assertRaises(ChainError) as caught:
            self.adapter.plan_transfer(Decimal("2"), EXTERNAL, account_index=0)
        self.assertEqual(caught.exception.code, "insufficient_funds")
        # nothing moved, and the refusal did not invent a payment
        self.assertEqual(self._balance(0), Decimal("1"))

    def test_broadcasting_the_same_plan_twice_moves_value_once(self) -> None:
        plan = self.adapter.plan_transfer(SPEND, EXTERNAL, account_index=0)
        first = self.adapter.broadcast(plan)
        second = self.adapter.broadcast(plan)
        self.assertEqual(first["tx_hash"], second["tx_hash"])
        self.assertTrue(second.get("already_settled"))
        self.assertEqual(self._balance(0), Decimal("1") - SPEND - FEE)

    def test_the_tx_hash_keeps_its_shape_and_is_deterministic(self) -> None:
        plan = self.adapter.plan_transfer(SPEND, EXTERNAL, account_index=0)
        txid = plan.raw["tx_hash"]
        self.assertTrue(txid.startswith("sim"), txid)
        self.assertEqual(len(txid), 64)
        int(txid[3:], 16)                       # hex, no exceptions
        self.assertEqual(len(plan.raw["tx_key"]), 64)

        # same ledger state, same inputs, same payment => the same hash (there is no clock in it)
        (self.home / "sim-balances.json").unlink()   # the plan was never broadcast; only a nonce
        twin = self._adapter()
        self.assertEqual(twin.plan_transfer(SPEND, EXTERNAL, account_index=0).raw["tx_hash"], txid)
        # a second payment, however, must not collide with the first
        other = self.adapter.plan_transfer(SPEND, EXTERNAL, account_index=0).raw["tx_hash"]
        self.assertNotEqual(other, txid)

    def test_a_payment_proof_is_read_from_the_ledger(self) -> None:
        account = self._sandbox()
        receipt = self._fund(account)
        proof = self.adapter.payment_proof(receipt["tx_hash"], receipt["tx_key"],
                                           account["address"])
        self.assertTrue(proof["received"])
        self.assertEqual(Decimal(proof["amount"]), FUNDED)
        self.assertTrue(self.adapter.verify_payment(receipt["tx_hash"], receipt["tx_key"],
                                                    account["address"]))
        self.assertFalse(self.adapter.verify_payment(receipt["tx_hash"], receipt["tx_key"], EXTERNAL))

    def test_sim_only_features_report_unsupported(self) -> None:
        self.assertEqual(self.adapter.status()["provider"], "simulated")
        self.assertTrue(self.adapter.available())
        with self.assertRaises(ChainError) as caught:
            self.adapter.mnemonic()
        self.assertEqual(caught.exception.code, "unsupported")
        with self.assertRaises(ChainError) as caught:
            self.adapter.restore_wallet("word " * 25)
        self.assertEqual(caught.exception.code, "unsupported")

    def test_addresses_fit_the_policy_shape_and_are_unique_per_account(self) -> None:
        import re

        pattern = re.compile(r"^[48][0-9AB][1-9A-HJ-NP-Za-km-z]{93}$")
        first, second = self._sandbox(), self._sandbox()
        self.assertRegex(first["address"], pattern)
        self.assertNotEqual(first["address"], second["address"])
        self.assertEqual(self.adapter.address(), self._adapter().address())

    def test_a_corrupt_ledger_is_an_error_not_a_fresh_start(self) -> None:
        (self.home / "sim-balances.json").write_text("{not json", encoding="utf-8")
        adapter = self._adapter()
        with self.assertRaises(ChainError) as caught:
            adapter.balance_at(0)
        self.assertEqual(caught.exception.code, "sim_ledger_unreadable")

    # ---------- the numbers the demo prints ----------

    def _ledger_total(self) -> Decimal:
        return Decimal(str(self.adapter.balance_at(0))) + sum(
            (Decimal(str(row["balance"])) for row in self.adapter.accounts()
             if row["account_index"] != 0), Decimal("0"))

    def test_the_demo_numbers_for_a_004_budget_and_a_001_spend(self) -> None:
        """0.004 funded, 0.001 spent: `swept` is what the revoke prints, `left` what teardown prints."""
        account = self._sandbox()
        self._fund(account)
        self._send(SPEND, EXTERNAL, account=account["account_index"])
        remaining = Decimal(str(self.adapter.balance_at(account["account_index"])))
        swept = remaining - RESERVE

        receipt = self._send(swept, self.adapter.address(), account=account["account_index"])
        self.assertTrue(receipt["tx_hash"].startswith("sim"))
        left = Decimal(str(self.adapter.balance_at(account["account_index"])))

        self.assertEqual(str(remaining), "0.00297")     # after 0.004 - 0.001 - fee
        self.assertEqual(str(swept), "0.00247")         # what `swept:` reports
        self.assertEqual(str(left), "0.00047")          # what `left:` reports (dust < reserve)
        self.assertLess(left, RESERVE, "teardown may only proceed when the dust is under the reserve")


class SimLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_balances_are_decimal_and_never_float(self) -> None:
        ledger = SimLedger(self.home)
        self.assertIsInstance(ledger.balance(0), Decimal)
        self.assertEqual(ledger.balance(0), Decimal("1"))
        self.assertEqual(ledger.balance(7), Decimal("0"), "an account that was never funded is 0")

    def test_a_second_ledger_instance_reads_what_the_first_wrote(self) -> None:
        adapter = XmrAdapter("sim-mainnet", "", "w", "", simulate=True, home=self.home)
        adapter.rpc = NoNetwork()          # type: ignore[assignment]
        account = adapter.create_account("sbx")
        adapter.broadcast(adapter.plan_transfer(FUNDED, account["address"], account_index=0))
        reopened = SimLedger(self.home)
        self.assertEqual(reopened.balance(account["account_index"]), FUNDED)
        self.assertEqual(reopened.balance(0), Decimal("1") - FUNDED - FEE)

    def test_the_file_is_private(self) -> None:
        ledger = SimLedger(self.home)
        ledger.create_account("sbx")
        path = self.home / "sim-balances.json"
        from tests import assert_private
        assert_private(self, path)


class SimDemoThroughTheDaemonTests(unittest.TestCase):
    """The numbers the user actually sees, produced by the real sandbox code paths.

    `sandbox_revoke` returns `swept` and the one-time teardown receipt carries `left`; both used to
    be "0" because `balance_at` always answered zero. This drives `State` exactly as the demo does.
    """

    PASS = "correct horse battery"

    def setUp(self) -> None:
        from walletd import config as config_mod
        from walletd.state import State

        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.state = State(self.home)
        self.state.initialize(self.PASS, pin=PIN)
        # the PIN gate itself is tested in tests/test_pin.py; these tests are about policy
        # accounting (budgets, fees, sweeps), so they run with the gate off and their approvals
        # do not need to type it
        self.state.settings.pin_every_payment = False
        self.state.settings.chains_enabled = ["sim-mainnet"]
        self.state.settings.assets_enabled = ["XMR"]
        master = self.state.master
        assert master is not None
        config_mod.save(self.state.config_path, self.state.settings, master)
        self.state.unlock(self.PASS)
        self.sim = XmrAdapter("sim-mainnet", "", "agent-wallet", "",
                              provider="simulated", simulate=True, home=self.home)
        self.sim.rpc = NoNetwork()          # type: ignore[assignment]
        self.state.adapters["sim-mainnet"] = self.sim

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _sandbox(self, mode: str = "budget") -> str:
        created = self.state.sandbox_create({"kind": "sim-mainnet", "asset": "XMR",
                                             "budget": "0.004", "mode": mode, "label": "demo",
                                             "allowed_payees": [EXTERNAL]})
        funding = created["funding"]
        self.state.approve({"pin": PIN, "request_id": funding["request"]["request_id"],
                            "approval_id": funding["approval_id"], "passphrase": self.PASS})
        return created["sandbox"]["sandbox_id"]

    def test_revoke_reports_the_real_swept_amount(self) -> None:
        sandbox_id = self._sandbox()
        spend = self.state.request_tx({"kind": "sim-mainnet", "asset": "XMR", "amount": "0.001",
                                       "payee": EXTERNAL, "sandbox_id": sandbox_id})
        self.assertTrue(spend["decision"]["allow"], spend["decision"])
        revoked = self.state.sandbox_revoke({"sandbox_id": sandbox_id})
        # 0.004 funded, 0.001 spent, two fees of 0.00003: the sweep now reserves the fee the chain
        # actually charges (0.00003), not the operator's 0.0005 fee *ceiling*
        self.assertEqual(revoked["sweep"]["swept"], "0.00294")   # was "0" with a bookkeeping-only sim
        self.assertEqual(revoked["sweep"]["result"]["transfer"]["tx_hash"][:3], "sim")

    def test_one_time_teardown_reports_the_dust_it_could_not_move(self) -> None:
        sandbox_id = self._sandbox("one_time")
        self.state.request_tx({"kind": "sim-mainnet", "asset": "XMR", "amount": "0.001",
                               "payee": EXTERNAL, "sandbox_id": sandbox_id})
        receipt = self.state.sandbox_destroy({"sandbox_id": sandbox_id, "reason": "demo"})
        self.assertTrue(receipt["destroyed"])
        # the reserve is the chain's real fee, so nothing above a single fee is stranded; whatever is
        # left is dust no transfer can move, and it must be recorded rather than silently dropped
        self.assertLessEqual(Decimal(str(receipt["left"])), Decimal("0.00003"))
        # 0.004 funded - 0.001 spent - 0.00003 spend fee = 0.00297 in the account; the sweep pays its
        # own 0.00003, so 0.00294 goes home and nothing is stranded
        self.assertEqual(Decimal(str(receipt["swept"])), Decimal("0.00294"))
        dust = [e for e in self.state.ledger.entries()
                if e.get("kind") == "sandbox" and e.get("decision") == "swept_dust"]
        if Decimal(str(receipt["left"])) > 0:
            self.assertTrue(dust, "dust left behind was not recorded")
        self.assertEqual(self.state.balances()["accounts"][0]["balance"], "0.99891")


if __name__ == "__main__":
    unittest.main()
