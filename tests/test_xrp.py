"""XRP adapter: address derivation, validation, and the settings migration that enables it.

The derivation is checked against a value produced by xrpl-py (`encode_classic_address` over the same
account id), so this is not "it round-trips with itself" - it agrees with the reference implementation.
"""

from __future__ import annotations

import unittest
from decimal import Decimal

from walletd import config
from walletd.chains.xrp import RPC, XrpAdapter, address_from_secret, is_address
from walletd.policy import payee_key

PIN = "123456"   # the per-payment secret; every approval needs it


class XrpAddressTests(unittest.TestCase):
    def test_derivation_matches_the_reference_implementation(self) -> None:
        # xrpl-py: encode_classic_address(RIPEMD160(SHA256(compressed_pubkey)))
        # vectors from ecdsa's own point arithmetic + xrpl-py's encode_classic_address. The first
        # version of this test locked in a parity bug (it read the parity of x, not y), so these are
        # the y-parity values: address derivation is the one place where "looks valid" is not enough.
        self.assertEqual(address_from_secret("11" * 32), "rQrFNnvPGNNMqRcVRMbtBK3hpYaPzoa8K9")
        self.assertEqual(address_from_secret("ab" * 32), "rQ96gj2GRMMNKRZn98LEbwe1CLEE2mLJuv")
        self.assertEqual(address_from_secret("00" * 31 + "01"), "rBgGZ9tc4him9KBzD8fKFiQz3fSZpaSwMH")

    def test_validation_checks_the_checksum_not_just_the_shape(self) -> None:
        self.assertTrue(is_address("rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh"))
        self.assertFalse(is_address("rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTi"))   # last char changed
        self.assertFalse(is_address("rNotAnAddressAtAll1111111111"))
        self.assertFalse(is_address("0xabc"))
        self.assertFalse(is_address(""))

    def test_policy_accepts_a_real_address_and_rejects_a_corrupt_one(self) -> None:
        good = "rLW5BG18gaMFZJqFkWNNqUcdMcyfMcyHQu"
        self.assertEqual(payee_key("xrp-mainnet", good), good)
        with self.assertRaises(ValueError):
            payee_key("xrp-mainnet", good[:-1] + "v")


class XrpAdapterTests(unittest.TestCase):
    def test_an_unfunded_account_reports_zero_not_an_error(self) -> None:
        adapter = XrpAdapter("xrp-mainnet", "11" * 32)
        adapter._call = lambda method, params: {"account_data": None}   # noqa: SLF001 - fixture
        self.assertEqual(adapter.balance(), Decimal("0"))

    def test_balance_is_converted_from_drops(self) -> None:
        adapter = XrpAdapter("xrp-mainnet", "11" * 32)
        adapter._call = lambda method, params: {"account_data": {"Balance": "2500000"}}  # noqa: SLF001
        self.assertEqual(adapter.balance(), Decimal("2.5"))

    def test_sending_is_refused_rather_than_faked(self) -> None:
        adapter = XrpAdapter("xrp-mainnet", "11" * 32)
        with self.assertRaises(Exception) as caught:
            adapter.plan_transfer(Decimal("1"), "rLW5BG18gaMFZJqFkWNNqUcdMcyfMcyHQu")
        self.assertEqual(getattr(caught.exception, "code", ""), "unsupported")

    def test_every_declared_network_has_an_endpoint(self) -> None:
        for kind in RPC:
            self.assertTrue(XrpAdapter(kind, "11" * 32).rpc_url.startswith("https://"))


class XrpMigrationTests(unittest.TestCase):
    def test_an_older_settings_document_reads_as_schema_one(self) -> None:
        legacy = config.Settings.from_dict({"chains_enabled": ["monero-mainnet"], "assets_enabled": ["XMR"]})
        self.assertEqual(legacy.schema, 1)

    def test_the_migration_adds_xrp_once(self) -> None:
        from pathlib import Path
        import tempfile

        from tests.test_state import PASS, WalletState

        with tempfile.TemporaryDirectory() as tmp:
            state = WalletState(Path(tmp))
            state.initialize(PASS, pin=PIN)
            # pretend this home predates XRP
            state.settings = config.Settings.from_dict(
                {"chains_enabled": ["monero-mainnet"], "assets_enabled": ["XMR"]})
            self.assertTrue(state._migrate_settings())
            self.assertIn("xrp-mainnet", state.settings.chains_enabled)
            self.assertIn("XRP", state.settings.assets_enabled)
            self.assertEqual(state.settings.schema, 3)
            self.assertFalse(state._migrate_settings(), "a second unlock must not re-add anything")
            # schema 3 also shrank the wallet to the three coins it ships with
            self.assertEqual(sorted(state.settings.assets_enabled), ["BTC", "XMR", "XRP"])
            self.assertEqual(sorted(state.settings.chains_enabled),
                             ["bitcoin-mainnet", "monero-mainnet", "xrp-mainnet"])
            # a chain the operator removed afterwards stays removed
            state.settings.chains_enabled = ["monero-mainnet"]
            self.assertFalse(state._migrate_settings())
            self.assertNotIn("xrp-mainnet", state.settings.chains_enabled)


if __name__ == "__main__":
    unittest.main()
