"""The gas floor: a payment must not leave the chain unable to pay for the next one.

The failure this file exists for is quiet. Nothing errors at the moment the wallet spends its last
ETH: the transfer succeeds, the receipt says `sent`, and the *next* payment on that chain can never
be signed again — until a human notices, funds the address, and wonders what happened. An agent
that can be told to "send it all" can walk the wallet into that corner without ever doing anything
that looks like a mistake.

So the floor is a policy, enforced where every other limit is enforced, and it is measured in
transfers rather than in a fixed amount: four more transfers' worth of gas at the current fee
ceiling. A chain that pays its fee in the coin being sent (Monero, Bitcoin) has no separate gas
balance and is not affected; neither is a 402 purchase, which the seller submits and pays for.
"""

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from walletd.chains.base import TxPlan
from walletd.config import Settings
from walletd.state import WalletError, State as WalletState

from tests.test_state import FakeAdapter, PASS, TEST_ASSETS, TEST_CHAINS, xmr_addr

PIN = "123456"
AGENT = {"_scope": "agent"}

#: what one transfer costs on the fixture chain, and what the address holds
COST = Decimal("0.00002")


class GasAdapter(FakeAdapter):
    """The in-memory chain, plus the three facts a gas-paying chain has to answer."""

    kind = "monero-stagenet"          # so the existing policy plumbing accepts it
    asset = "XMR"
    native = "ETH"

    def __init__(self) -> None:
        super().__init__()
        self.gas_held = Decimal("0.0001")     # 5 transfers' worth

    def native_symbol(self) -> str:
        return self.native

    def native_balance(self) -> Decimal:
        return self.gas_held

    def transfer_cost(self) -> Decimal:
        return COST


class GasFloorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        FakeAdapter._next_account = 1
        FakeAdapter.balances = {}
        self.state = WalletState(self.home)
        self.fake = GasAdapter()
        self.state.fake = self.fake
        self.state.initialize(PASS, pin=PIN)
        self.state.settings.chains_enabled = list(TEST_CHAINS)
        self.state.settings.assets_enabled = list(TEST_ASSETS)
        from walletd import config as config_mod
        config_mod.save(self.state.config_path, self.state.settings, self.state.master)
        self.state.unlock(PASS)
        self.state.adapters["monero-stagenet"] = self.fake

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def req(self, asset: str = "XMR") -> SimpleNamespace:
        return SimpleNamespace(kind="monero-stagenet", asset=asset)

    def plan(self, fee: str = "0.00002", amount: str = "0.01", x402: bool = False) -> TxPlan:
        return TxPlan(kind="monero-stagenet", asset="XMR", amount=Decimal(amount),
                      payee=xmr_addr(1), fee=Decimal(fee), raw={"x402": True} if x402 else {})

    # ---------- the floor ----------

    def test_the_floor_is_four_transfers_by_default(self) -> None:
        self.assertEqual(self.state.settings.gas_floor_transfers, 4)
        self.assertEqual(self.state._gas_floor("ETH", COST), COST * 4)

    def test_a_transfer_that_would_leave_less_than_the_floor_is_refused(self) -> None:
        self.fake.gas_held = Decimal("0.00009")          # 4.5 transfers: one more leaves 3.5
        with self.assertRaises(Exception) as caught:
            self.state._check_gas_floor(self.req(), self.plan())
        self.assertEqual(getattr(caught.exception, "code", ""), "gas_floor", caught.exception)
        self.assertIn("below the", caught.exception.message)

    def test_a_transfer_that_leaves_exactly_the_floor_is_allowed(self) -> None:
        self.fake.gas_held = Decimal("0.0001")           # 5 transfers: one more leaves exactly 4
        self.state._check_gas_floor(self.req(), self.plan())     # must not raise

    def test_a_native_payment_counts_the_amount_as_well_as_the_fee(self) -> None:
        # paying in the gas coin itself: the amount leaves the same balance the floor protects
        self.fake.gas_held = Decimal("0.001")
        with self.assertRaises(Exception) as caught:
            self.state._check_gas_floor(self.req("ETH"), self.plan(amount="0.00099"))
        self.assertEqual(getattr(caught.exception, "code", ""), "gas_floor", caught.exception)

    def test_an_explicit_floor_overrides_the_transfer_count(self) -> None:
        self.state.settings.gas_floor = {"ETH": "0.0005"}
        self.fake.gas_held = Decimal("0.0004")
        with self.assertRaises(Exception):
            self.state._check_gas_floor(self.req(), self.plan())
        self.fake.gas_held = Decimal("0.0006")
        self.state._check_gas_floor(self.req(), self.plan())     # must not raise

    def test_a_402_authorisation_is_exempt(self) -> None:
        """It costs no gas: the seller's facilitator submits and pays for it."""
        self.fake.gas_held = Decimal("0")
        self.state._check_gas_floor(self.req(), self.plan(x402=True))

    def test_a_chain_with_no_separate_gas_coin_is_not_affected(self) -> None:
        plain = FakeAdapter()
        self.state.adapters["monero-stagenet"] = plain
        self.assertIsNone(self.state._gas_view("monero-stagenet"))
        self.state._check_gas_floor(self.req(), self.plan())     # must not raise
        self.assertEqual(self.state.gas_report()["chains"], [])

    # ---------- what a human sees ----------

    def test_the_report_names_the_shortfall_and_the_address(self) -> None:
        self.fake.gas_held = Decimal("0.00001")
        report = self.state.gas_report()
        row = report["chains"][0]
        self.assertEqual(row["state"], "low")
        self.assertEqual(Decimal(row["shortfall"]), COST * 4 - Decimal("0.00001"))
        self.assertEqual(row["address"], self.fake.address())
        self.assertEqual([r["kind"] for r in report["low"]], ["monero-stagenet"])

    def test_the_report_is_empty_while_locked(self) -> None:
        self.state.lock()
        self.assertTrue(self.state.gas_report().get("locked"))

    def test_the_card_carries_what_the_payment_leaves_behind(self) -> None:
        self.fake.gas_held = Decimal("0.001")
        out = self.state.request_tx({**AGENT, "kind": "monero-stagenet", "asset": "XMR",
                                     "amount": "0.01", "payee": xmr_addr(1), "why": "gas test"})
        self.assertEqual(out["request"]["status"], "pending", out["decision"])
        receipt = [e for e in self.state.ledger.entries() if e.get("kind") == "spend_request"]
        gas = receipt[-1]["details"]["gas"]
        self.assertEqual(gas["native"], "ETH")
        self.assertEqual(Decimal(gas["after"]), Decimal("0.001") - COST)

    # ---------- through the real money path ----------

    def test_a_payment_that_would_strand_the_chain_is_deferred_not_denied(self) -> None:
        """`gas_floor` is "fund it", not "a human said no": a top-up makes the same request payable."""
        self.fake.gas_held = Decimal("0.00009")
        out = self.state.request_tx({**AGENT, "kind": "monero-stagenet", "asset": "XMR",
                                     "amount": "0.01", "payee": xmr_addr(1), "why": "gas test"})
        self.assertEqual(out["request"]["status"], "retry", out["decision"])
        self.assertEqual(out["decision"]["code"], "gas_floor")
        self.assertTrue(out["decision"]["retryable"])
        self.assertFalse(out["decision"]["allow"])

    def test_the_settings_survive_a_signed_round_trip(self) -> None:
        """A key `to_dict` forgets is a key `config set` writes and the next unlock loses."""
        from walletd import config as config_mod
        self.state.settings.gas_floor = {"ETH": "0.0005"}
        self.state.settings.gas_reserve = {"ETH": "0x" + "1" * 40}
        self.state.settings.gas_floor_transfers = 7
        config_mod.save(self.state.config_path, self.state.settings, self.state.master)
        loaded = config_mod.load(self.state.config_path, self.state.master).settings
        self.assertEqual(loaded.gas_floor, {"ETH": "0.0005"})
        self.assertEqual(loaded.gas_reserve, {"ETH": "0x" + "1" * 40})
        self.assertEqual(loaded.gas_floor_transfers, 7)

    def test_changing_the_floor_needs_the_passphrase(self) -> None:
        """It decides whether a payment can brick the chain, so a token must not be able to."""
        with self.assertRaises(WalletError) as caught:
            self.state.config_set({"gas_floor_transfers": 0})
        self.assertIn("passphrase", str(caught.exception).lower() + str(getattr(caught.exception, "code", "")))
        self.state.config_set({"gas_floor_transfers": 0, "passphrase": PASS})
        self.assertEqual(self.state.settings.gas_floor_transfers, 0)

    def test_zero_transfers_disables_the_floor(self) -> None:
        self.state.settings.gas_floor_transfers = 0
        self.fake.gas_held = Decimal("0.00002")
        self.state._check_gas_floor(self.req(), self.plan())     # must not raise


if __name__ == "__main__":
    unittest.main()
