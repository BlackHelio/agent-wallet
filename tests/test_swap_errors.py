"""An adapter refusal must reach the caller with its own code.

Found by a black-box lane: `walletd/rpc.py` caught only `WalletError`, so every `ChainError` — the gas
floor, a chain with no pinned router, an operator the adapter does not know — arrived at the client as
`internal_error: ChainError`. That is indistinguishable from a crash and tells a caller nothing about
what to do next. The fix belongs in the dispatch (one place, every method), so these tests drive the
real socket rather than the method body.

The subject is the dispatch, so most cases raise `ChainError` from a fixture adapter — but one case
uses the real Polygon adapter, which has no pinned router and refuses before any network call.
"""

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict

from walletd.chains.base import ChainError, TxPlan
from walletd.rpc import ServerThread, call
from walletd.state import State as WalletState, WalletError
from walletd.tokens import AGENT, OPERATOR, TokenStore

PASS = "swap-error-passphrase"
PIN = "123456"


class RaisingAdapter:
    """The smallest adapter that can refuse at each of the three depths."""

    kind = "base-mainnet"
    asset = "ETH"

    def __init__(self, code: str) -> None:
        self.code = code

    def native_symbol(self) -> str:
        return "ETH"

    def native_balance(self) -> Decimal:
        return Decimal("0")

    def transfer_cost(self) -> Decimal:
        return Decimal("0.00002")

    def swap_routers(self):
        return [{"operator": "fixture-v2", "label": "Fixture V2",
                 "router": "0x" + "2" * 40, "fee_bps": 30}]

    def quote_swap(self, asset_in, asset_out, amount_in, operator=""):
        raise ChainError(self.code, f"refused at quote: {self.code}")

    def plan_swap(self, *args, **kwargs):
        raise ChainError(self.code, f"refused at plan: {self.code}")


class SwapErrorCodeTests(unittest.TestCase):
    def setUp(self) -> None:
        from walletd import config as config_mod

        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.state = WalletState(self.home)
        self.state.initialize(PASS, pin=PIN)
        self.state.settings.chains_enabled = ["polygon-mainnet", "base-mainnet"]
        self.state.settings.assets_enabled = ["POL", "ETH", "USDC"]
        config_mod.save(self.state.config_path, self.state.settings, self.state.master)
        self.state.unlock(PASS)
        self.tokens = TokenStore(self.home / "tokens")
        self.tokens.mint_all()
        self.socket = str(self.home / "walletd.sock")
        self.server = ServerThread(self.socket, self.state, self.tokens)
        self.server.__enter__()

    def tearDown(self) -> None:
        self.server.__exit__(None, None, None)
        self.tmp.cleanup()

    def _error_code(self, method: str, params: Dict[str, Any], scope: str = AGENT) -> str:
        with self.assertRaises(WalletError) as caught:
            call(self.socket, method, params, token=self.tokens.read(scope))
        return caught.exception.code

    def test_a_chain_with_no_router_answers_no_swap_route(self) -> None:
        # the real Polygon adapter: no pinned operator, so it refuses before any network call
        code = self._error_code("swap_quote", {"kind": "polygon-mainnet", "asset": "POL",
                                               "asset_out": "USDC", "amount": "0.1"})
        self.assertEqual(code, "no_swap_route")

    def test_the_gas_floor_answers_gas_floor(self) -> None:
        # a wallet holding nothing cannot pay a swap's fee; that refusal has to name itself
        self.state.adapters["base-mainnet"] = RaisingAdapter("gas_floor")
        code = self._error_code("swap_request", {"kind": "base-mainnet", "asset": "ETH",
                                                 "asset_out": "USDC", "amount": "0.00001"},
                                scope=OPERATOR)
        self.assertEqual(code, "gas_floor")

    def test_an_unknown_operator_answers_unknown_operator(self) -> None:
        self.state.adapters["base-mainnet"] = RaisingAdapter("unknown_operator")
        code = self._error_code("swap_quote", {"kind": "base-mainnet", "asset": "ETH",
                                               "asset_out": "USDC", "amount": "0.00001"})
        self.assertEqual(code, "unknown_operator")

    def test_a_chain_error_never_says_internal_error(self) -> None:
        self.state.adapters["base-mainnet"] = RaisingAdapter("swap_price_moved")
        code = self._error_code("swap_quote", {"kind": "base-mainnet", "asset": "ETH",
                                               "asset_out": "USDC", "amount": "0.00001"})
        self.assertNotEqual(code, "internal_error")
        self.assertEqual(code, "swap_price_moved")


if __name__ == "__main__":
    unittest.main()
