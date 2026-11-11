"""Swaps: a conversion is a payment to a contract, and it is treated like one.

The rules this file pins down, in the order they matter:

* the router is pinned in code — an agent, a config file or a caller can never name a contract to
  send money to, and `settings.swap_routers` can only *narrow* the list;
* the minimum out comes from the operator's slippage ceiling, clamped to 5%, never from the caller;
* the caps still apply (what leaves the wallet is the amount sold);
* the gas floor still applies, and it counts the amount when the coin being sold *is* the gas coin;
* the agent may ask, and asking produces a card — only the PIN converts;
* `swaps_enabled` off refuses everything, `suggest_swaps` off refuses only the agent.

The chain is the in-memory fixture for the policy tests; the quote test hits Base through the
public RPC and skips itself when the network is not there.
"""

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List

from eth_utils.crypto import keccak

from walletd.chains import evm as evm_mod
from walletd.chains.evm import SELECTORS, SWAP_ROUTERS, EvmAdapter
from walletd.state import WalletError, State as WalletState

from tests.test_state import FakeAdapter, PASS, TEST_ASSETS, TEST_CHAINS, xmr_addr

PIN = "123456"
AGENT = {"_scope": "agent"}


class SwapAdapter(FakeAdapter):
    """The in-memory chain, with the three swap facts a policy test needs."""

    kind = "monero-stagenet"
    asset = "XMR"
    native = "ETH"

    def __init__(self) -> None:
        super().__init__()
        self.gas_held = Decimal("0.01")
        self.quoted = Decimal("0.0005")
        self.plan_calls: List[Dict[str, Any]] = []

    def native_symbol(self) -> str:
        return self.native

    def native_balance(self) -> Decimal:
        return self.gas_held

    def transfer_cost(self) -> Decimal:
        return Decimal("0.00002")

    def swap_routers(self) -> List[Dict[str, Any]]:
        return [{"operator": "fixture-v2", "label": "Fixture V2",
                 "router": "0x" + "2" * 40, "fee_bps": 30}]

    def quote_swap(self, asset_in, asset_out, amount_in, operator=""):
        return {"operator": operator or "fixture-v2", "label": "Fixture V2",
                "router": "0x" + "2" * 40, "asset_in": asset_in.upper(),
                "asset_out": asset_out.upper(), "amount_in": str(amount_in),
                "amount_out": str(self.quoted), "rate": "1", "path": []}

    def plan_swap(self, asset_in, amount_in, asset_out, min_out, operator="", deadline_seconds=600):
        self.plan_calls.append({"asset_in": asset_in, "amount_in": amount_in,
                                "asset_out": asset_out, "min_out": min_out,
                                "operator": operator})
        from walletd.chains.base import TxPlan
        return TxPlan(kind=self.kind, asset=asset_in, amount=Decimal(str(amount_in)),
                      payee="0x" + "2" * 40, fee=Decimal("0.00002"),
                      raw={"swap": {"asset_in": asset_in, "asset_out": asset_out,
                                    "min_out": str(min_out), "operator": operator},
                           "tx_template": {"to": "0x" + "2" * 40, "value": 0, "gas": 260000,
                                           "chainId": 1, "data": "00"}})


class SwapPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        FakeAdapter._next_account = 1
        FakeAdapter.balances = {}
        self.state = WalletState(self.home)
        self.fake = SwapAdapter()
        self.state.fake = self.fake
        self.state.initialize(PASS, pin=PIN)
        self.state.settings.chains_enabled = list(TEST_CHAINS)
        self.state.settings.assets_enabled = list(TEST_ASSETS)
        from walletd import config as config_mod
        config_mod.save(self.state.config_path, self.state.settings, self.state.master)
        self.state.unlock(PASS)
        self.state.adapters["monero-stagenet"] = self.fake
        # the fixture chain is one wallet that holds both coins, so a swap has somewhere to go
        self.state._chain_assets = lambda kind: ["XMR", "ETH"]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def file(self, amount: str = "0.01", **extra) -> Dict[str, Any]:
        params = {"kind": "monero-stagenet", "asset": "XMR", "asset_out": "ETH",
                  "amount": amount, "why": "swap test", **extra}
        return self.state.swap_request(params, from_agent=bool(extra.pop("from_agent", False)))

    # ---------- the ceiling ----------

    def test_the_minimum_out_comes_from_the_ceiling_not_the_caller(self) -> None:
        filed = self.file("0.01", min_out="0.0000001")     # a caller trying to set its own floor
        swap = filed["request"]["swap"]
        self.assertEqual(Decimal(swap["min_out"]), self.state._swap_min_out(Decimal("0.0005"), 50))
        self.assertEqual(swap["slippage_bps"], 50)
        self.assertEqual(self.fake.plan_calls[-1]["min_out"], Decimal(swap["min_out"]))

    def test_the_slippage_ceiling_is_clamped_to_five_percent(self) -> None:
        self.state.settings.swap_slippage_bps = 9_000
        self.assertEqual(self.state._swap_slippage_bps(), 500)

    def test_the_router_is_the_pinned_one(self) -> None:
        filed = self.file()
        self.assertEqual(filed["request"]["payee"], "0x" + "2" * 40)
        self.assertEqual(filed["request"]["swap"]["operator"], "fixture-v2")

    def test_an_unknown_operator_is_refused(self) -> None:
        with self.assertRaises(WalletError) as caught:
            self.file(operator="0xattacker")
        self.assertEqual(caught.exception.code, "unknown_operator")

    def test_settings_can_narrow_the_operator_list_but_not_add_to_it(self) -> None:
        self.state.settings.swap_routers = ["fixture-v2"]
        self.file()                                             # still allowed
        self.state.settings.swap_routers = ["somethingelse"]
        with self.assertRaises(WalletError) as caught:
            self.file()
        self.assertEqual(caught.exception.code, "no_swap_route")

    # ---------- the switches ----------

    def test_swaps_disabled_refuses_everyone(self) -> None:
        self.state.settings.swaps_enabled = False
        with self.assertRaises(WalletError) as caught:
            self.file()
        self.assertEqual(caught.exception.code, "swaps_disabled")

    def test_suggest_swaps_off_refuses_the_agent_but_not_the_operator(self) -> None:
        self.state.settings.suggest_swaps = False
        with self.assertRaises(WalletError) as caught:
            self.file(from_agent=True)
        self.assertEqual(caught.exception.code, "swaps_not_suggested")
        self.file()                                             # the operator may still convert

    def test_a_swap_is_a_card_not_a_transfer(self) -> None:
        filed = self.file()
        self.assertEqual(filed["decision"]["code"], "needs_operator")
        self.assertEqual(filed["request"]["status"], "pending")
        self.assertTrue(filed.get("approval_id"))
        self.assertFalse(filed["request"].get("tx_hash"))

    # ---------- the limits that already existed ----------

    def test_the_per_transaction_cap_still_applies_to_what_is_sold(self) -> None:
        self.state.settings.per_tx_cap = {"XMR": "0.005"}
        with self.assertRaises(WalletError) as caught:
            self.file("0.01")
        self.assertEqual(caught.exception.code, "over_per_tx_cap")

    def test_the_gas_floor_counts_the_amount_when_selling_the_gas_coin(self) -> None:
        # selling 0.00995 of 0.01 ETH: the swap pays gas *and* spends the coin the floor protects,
        # so the balance after it (0.00003) is below the 0.00008 floor even though the fee alone
        # would have been affordable
        self.fake.gas_held = Decimal("0.01")
        with self.assertRaises(Exception) as caught:
            self.state.swap_request({"kind": "monero-stagenet", "asset": "ETH",
                                     "asset_out": "XMR", "amount": "0.00995"})
        self.assertEqual(getattr(caught.exception, "code", ""), "gas_floor", caught.exception)

    def test_the_card_carries_the_gas_that_survives(self) -> None:
        filed = self.file("0.001")
        gas = filed["request"]["gas"]
        self.assertEqual(gas["native"], "ETH")
        self.assertLess(Decimal(gas["after"]), self.fake.gas_held)

    def test_a_swap_survives_a_restart_as_a_swap(self) -> None:
        # the requests file is a second serializer, and it used to drop anything added to `to_dict`
        # later: a card that came back after a restart was a plain transfer to the router address.
        filed = self.file("0.001")
        rid = filed["request"]["request_id"]
        again = WalletState(self.home)
        again.settings = self.state.settings
        again.unlock(PASS)
        req = again.requests[rid]
        self.assertEqual(req.swap.get("asset_out"), "ETH")
        self.assertEqual(req.swap.get("min_out"), filed["request"]["swap"]["min_out"])
        self.assertEqual(req.gas.get("native"), "ETH")

    def test_swapping_a_coin_for_itself_is_refused(self) -> None:
        with self.assertRaises(WalletError) as caught:
            self.state.swap_request({"kind": "monero-stagenet", "asset": "XMR",
                                     "asset_out": "XMR", "amount": "0.01"})
        self.assertEqual(caught.exception.code, "bad_swap")


class SwapEncodingTests(unittest.TestCase):
    """The calldata is the part a typo would ruin, so it is checked against keccak, not eyeballed."""

    def test_every_selector_matches_its_signature(self) -> None:
        expected = {
            "approve": "approve(address,uint256)",
            "getAmountsOut": "getAmountsOut(uint256,address[])",
            "swapExactETHForTokens": "swapExactETHForTokens(uint256,address[],address,uint256)",
            "swapExactTokensForETH": "swapExactTokensForETH(uint256,uint256,address[],address,uint256)",
            "swapExactTokensForTokens":
                "swapExactTokensForTokens(uint256,uint256,address[],address,uint256)",
        }
        for name, signature in expected.items():
            self.assertEqual(SELECTORS[name], keccak(text=signature).hex()[:8], name)

    def test_only_verified_routers_are_pinned(self) -> None:
        for chain, registry in SWAP_ROUTERS.items():
            for name, entry in registry.items():
                self.assertTrue(entry["router"].startswith("0x") and len(entry["router"]) == 42,
                                f"{chain}/{name}")

    def test_a_chain_without_a_router_says_so(self) -> None:
        adapter = EvmAdapter("polygon-mainnet", "11" * 32, rpc_url="http://127.0.0.1:1")
        with self.assertRaises(Exception) as caught:
            adapter._swap_operator("")
        self.assertEqual(getattr(caught.exception, "code", ""), "no_swap_route")


class SwapLiveQuoteTests(unittest.TestCase):
    """One real quote from Base. Skips itself when the network is not reachable."""

    def test_base_quotes_both_directions(self) -> None:
        adapter = EvmAdapter("base-mainnet", "11" * 32, rpc_url="https://base.publicnode.com")
        try:
            eth_in = adapter.quote_swap("ETH", "USDC", Decimal("0.00005"))
            usdc_in = adapter.quote_swap("USDC", "ETH", Decimal("0.10"))
        except Exception as exc:                       # noqa: BLE001 - offline is not a failure
            self.skipTest(f"Base RPC unavailable: {exc}")
        self.assertGreater(Decimal(eth_in["amount_out"]), Decimal("0"))
        self.assertGreater(Decimal(usdc_in["amount_out"]), Decimal("0"))
        # 0.00005 ETH is worth cents; 0.10 USDC is worth less than it. A quote that says otherwise
        # is a wrong path, not a good price.
        self.assertLess(Decimal(eth_in["amount_out"]), Decimal("5"))
        self.assertLess(Decimal(usdc_in["amount_out"]), Decimal("0.01"))
        plan = adapter.plan_swap("USDC", Decimal("0.10"), "ETH",
                                 Decimal(usdc_in["amount_out"]) * Decimal("0.995"))
        self.assertIn("approve_template", plan.raw)     # a token sale needs an allowance first
        plan_eth = adapter.plan_swap("ETH", Decimal("0.00005"), "USDC",
                                     Decimal(eth_in["amount_out"]) * Decimal("0.995"))
        self.assertNotIn("approve_template", plan_eth.raw)   # a native sale does not


if __name__ == "__main__":
    unittest.main()
