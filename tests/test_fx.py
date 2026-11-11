"""Fiat amounts: "send adrian 10$" must become a coin amount, safely.

No network: every quote is stubbed. What is tested is the arithmetic, the refusal paths and the rule
that the coin amount — not the fiat string — is what gets stored, capped and later signed.
"""

from __future__ import annotations

import tempfile
import unittest
import json
from decimal import Decimal
from pathlib import Path
from unittest import mock

from tests.test_state import FakeAdapter, PASS, TEST_ASSETS, TEST_CHAINS, WalletError, WalletState, xmr_addr
from walletd import fx

PIN = "123456"
AGENT = {"_scope": "agent"}


class SplitFiatTests(unittest.TestCase):
    def test_the_shapes_a_person_types(self) -> None:
        for text in ("10$", "$10", "10 usd", "USD 10", "10  $  "):
            self.assertEqual(fx.split_fiat(text), (Decimal("10"), "USD"), text)

    def test_a_coin_amount_is_not_fiat(self) -> None:
        self.assertIsNone(fx.split_fiat("10"))
        self.assertIsNone(fx.split_fiat("0.001 XMR"))

    def test_a_bad_fiat_amount_is_refused_not_read_as_coin(self) -> None:
        with self.assertRaises(fx.FxError) as caught:
            fx.split_fiat("ten dollars")
        self.assertEqual(caught.exception.code, "bad_fiat")


class ConvertTests(unittest.TestCase):
    def test_fiat_to_coin_rounds_down(self) -> None:
        # 10 USD at 611.05 is 0.01636…: rounding *up* would pay more than was asked for
        coin = fx.to_coin(Decimal("10"), Decimal("611.05"), "XMR")
        self.assertEqual(coin, Decimal("0.016365"))

    def test_an_amount_below_one_base_unit_is_refused(self) -> None:
        with self.assertRaises(fx.FxError) as caught:
            fx.to_coin(Decimal("0.000001"), Decimal("100000"), "XMR")
        self.assertEqual(caught.exception.code, "fx_too_small")

    def test_a_missing_rate_is_an_error_not_zero(self) -> None:
        with self.assertRaises(fx.FxError):
            fx.to_coin(Decimal("10"), Decimal("0"), "XMR")


class QuoteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_a_quote_is_cached_and_then_reused(self) -> None:
        with mock.patch.object(fx, "_fetch", return_value=Decimal("500")) as fetch:
            first = fx.quote("XMR", "USD", home=self.home, force=True)
            second = fx.quote("XMR", "USD", home=self.home)
        self.assertEqual(fetch.call_count, 1, "a cached quote must not hit the network again")
        self.assertEqual(first["rate"], "500")
        self.assertEqual(second["rate"], "500")

    def test_a_wild_jump_is_refused_rather_than_trusted(self) -> None:
        with mock.patch.object(fx, "_fetch", return_value=Decimal("500")):
            fx.quote("XMR", "USD", home=self.home, force=True)
        # a feed that suddenly says XMR is worth 10× more is the shape of a poisoned source
        with mock.patch.object(fx, "_fetch", return_value=Decimal("5000")):
            with self.assertRaises(fx.FxError) as caught:
                fx.quote("XMR", "USD", home=self.home, force=True)
        self.assertEqual(caught.exception.code, "fx_suspect")

    def test_a_rate_limited_source_falls_back_to_the_cache_it_already_has(self) -> None:
        """CoinGecko 429s. Refusing a payment the human is looking at is worse than a dated quote."""
        with mock.patch.object(fx, "_fetch", return_value=Decimal("500")):
            fx.quote("XMR", "USD", home=self.home, force=True)
        # age the entry past the cache window but inside the fallback window
        cache = json.loads((self.home / "fx.json").read_text())
        cache["XMR:USD"]["at"] -= fx.MAX_AGE + 5
        (self.home / "fx.json").write_text(json.dumps(cache))
        with mock.patch.object(fx, "_fetch", side_effect=fx.FxError("fx_unavailable", "429")), \
                mock.patch.object(fx, "_fetch_kraken", side_effect=fx.FxError("fx_unavailable", "429")):
            row = fx.quote("XMR", "USD", home=self.home)
        self.assertTrue(row.get("stale"), row)
        self.assertEqual(row["rate"], "500")

    def test_a_dead_source_says_so(self) -> None:
        with mock.patch.object(fx, "_fetch", side_effect=fx.FxError("fx_unavailable", "offline")), \
                mock.patch.object(fx, "_fetch_kraken", side_effect=fx.FxError("fx_unavailable", "offline")):
            with self.assertRaises(fx.FxError) as caught:
                fx.quote("XMR", "USD", home=self.home, force=True)
        self.assertEqual(caught.exception.code, "fx_unavailable")

    def test_stablecoin_needs_no_third_party(self) -> None:
        with mock.patch.object(fx, "_fetch", side_effect=AssertionError("must not fetch USDC")):
            self.assertEqual(fx.quote("USDC", "USD")["rate"], "1")


class FxCacheTests(unittest.TestCase):
    """The cache is a file anything running as this uid can write."""

    def test_a_forged_rate_is_ignored_and_the_cache_refetched(self) -> None:
        import json

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            good = fx.quote("USDC", "USD", home=home)          # seeds a row for a fixed-rate asset
            self.assertEqual(good["rate"], "1")
            (home / "fx.json").write_text(json.dumps({
                "XMR:USD": {"at": 9e18, "quote": {"asset": "XMR", "currency": "USD",
                                                  "rate": "1000000", "source": "coingecko"}}}))
            # the daemon passes the master key, so a row nobody signed is ignored outright — and an
            # ignored row must not become the "previous" rate the deviation check compares against
            secret = b"master-key-for-this-test"
            with mock.patch.object(fx, "_fetch", return_value=Decimal("500")) as fetch:
                row = fx.quote("XMR", "USD", home=home, force=True, secret=secret)
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(row["rate"], "500")


class FiatRequestTests(unittest.TestCase):
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
        self.state.settings.pin_every_payment = False   # this file tests pricing, not the gate

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_requesting_dollars_files_the_coin_amount_and_keeps_the_fiat(self) -> None:
        with mock.patch.object(fx, "_fetch", return_value=Decimal("500")):
            out = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                         "amount": "10$", "payee": xmr_addr(40), **AGENT})
        request = out["request"]
        self.assertEqual(request["amount"], "0.020000000000")      # 10 / 500, quantised for the chain
        self.assertEqual(request["fiat"]["amount"], "10")
        self.assertEqual(request["fiat"]["currency"], "USD")
        self.assertEqual(request["fiat"]["rate"], "500")
        self.assertEqual(request["fiat"]["coin_amount"], "0.020000")

    def test_the_cap_applies_to_the_coin_amount(self) -> None:
        """A fiat figure must never be a way around the per-tx cap."""
        with mock.patch.object(fx, "_fetch", return_value=Decimal("1")):
            out = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                         "amount": "1000000$", "payee": xmr_addr(41), **AGENT})
        self.assertFalse(out["decision"]["allow"])
        self.assertIn(out["decision"]["code"], ("per_tx_cap", "cap_exceeded"))

    def test_an_unquotable_currency_refuses_instead_of_guessing(self) -> None:
        with mock.patch.object(fx, "_fetch", side_effect=fx.FxError("fx_unavailable", "down")), \
                mock.patch.object(fx, "_fetch_kraken", side_effect=fx.FxError("fx_unavailable", "down")):
            with self.assertRaises(WalletError) as caught:
                self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                       "amount": "10$", "payee": xmr_addr(42), **AGENT})
        self.assertEqual(caught.exception.code, "fx_unavailable")

    def test_amount_and_fiat_together_is_ambiguous(self) -> None:
        with self.assertRaises(WalletError) as caught:
            self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "1",
                                   "fiat_amount": "10", "payee": xmr_addr(43), **AGENT})
        self.assertEqual(caught.exception.code, "bad_amount")


if __name__ == "__main__":
    unittest.main()
