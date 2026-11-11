"""Regressions from the 2026-11-10 review. One test (or a small group) per bug that was found.

Each one pins the corrected behaviour and says in its docstring what used to happen. They are grouped
by where the bug lived, not by how bad it was: policy and amounts, sandboxes, autopays, chains and
quotes, the agent's tools, and the plumbing.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

from tests.test_state import (FakeAdapter, PASS, PIN, TEST_ASSETS, TEST_CHAINS, WalletState,
                              xmr_addr)
from walletd import addresses, compat, fx, pay402
from walletd.chains.base import ChainError
from walletd.chains.evm import EvmAdapter
from walletd.ledger import Ledger, network_class
from walletd.policy import (Mandate, SpendContext, SpendRequest, evaluate, parse_amount)
from walletd.state import State, WalletError, build_methods

TESTNET_BTC = "tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx"
EVM_PAYEE = "0x" + "ab" * 20


def _state(home: Path, pin_gate: bool = True) -> WalletState:
    """A wallet on the test chains, unlocked, with the in-memory chain double."""
    from walletd import config as config_mod

    FakeAdapter._next_account = 1
    FakeAdapter.balances = {}
    state = WalletState(home)
    state.fake = FakeAdapter()
    state.initialize(PASS, pin=PIN)
    state.settings.chains_enabled = list(TEST_CHAINS)
    state.settings.assets_enabled = list(TEST_ASSETS)
    state.settings.pin_every_payment = pin_gate
    config_mod.save(state.config_path, state.settings, state.master)
    state.unlock(PASS)
    return state


class _Home(unittest.TestCase):
    pin_gate = True

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.state = _state(self.home, self.pin_gate)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _approve(self, out: dict) -> dict:
        return self.state.approve({"pin": PIN, "passphrase": PASS,
                                   "request_id": out["request"]["request_id"],
                                   "approval_id": out["approval_id"],
                                   "confirm_amount": out["request"]["amount"]})

    def _funded(self, budget: str = "0.2", payees=None, **extra) -> str:
        created = self.state.sandbox_create({
            "kind": "monero-stagenet", "asset": "XMR", "budget": budget, "lifetime_hours": 24,
            "allowed_payees": payees if payees is not None else [xmr_addr(1)], **extra})
        self._approve(created["funding"])
        return created["sandbox"]["sandbox_id"]


# ---------------------------------------------------------------- policy and amounts

class AmountAndAllowlistTests(_Home):
    def test_nan_and_infinity_are_not_amounts(self) -> None:
        for bad in ("NaN", "Infinity", "-Infinity", "sNaN"):
            with self.assertRaises(ValueError, msg=bad):
                parse_amount(bad, "XMR")

    def test_an_autopay_cannot_be_proposed_with_unbounded_caps(self) -> None:
        with self.assertRaises(WalletError) as caught:
            self.state.mandates_propose({"kind": "monero-stagenet", "asset": "XMR",
                                         "payee": xmr_addr(3), "per_tx_cap": "Infinity",
                                         "period_cap": "Infinity", "absolute_cap": "Infinity"})
        self.assertEqual(caught.exception.code, "bad_mandate")

    def test_a_nan_request_is_a_refusal_not_a_crash(self) -> None:
        with self.assertRaises(WalletError) as caught:
            self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "NaN",
                                   "payee": xmr_addr(3)})
        self.assertEqual(caught.exception.code, "bad_amount")

    def test_an_allowlisted_case_sensitive_address_is_allowed(self) -> None:
        """Monero, XRP and legacy Bitcoin addresses keep their case. The allowlist was lower-cased
        wholesale, so an allowlisted address never matched and every payment to it was refused."""
        allowed = xmr_addr(10)
        self.assertNotEqual(allowed, allowed.lower())
        self.state.config_set({"payee_allowlist": [allowed], "passphrase": PASS})
        ok = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.01",
                                    "payee": allowed})
        self.assertTrue(ok["decision"]["allow"], ok["decision"])
        other = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                       "amount": "0.01", "payee": xmr_addr(61)})
        self.assertEqual(other["decision"]["code"], "payee_not_allowed")

    def test_an_evm_allowlist_entry_still_matches_in_any_case(self) -> None:
        settings = self.state.settings
        settings.payee_allowlist = [EVM_PAYEE.upper().replace("0X", "0x")]
        request = SpendRequest(request_id="r", kind="base-sepolia", asset="ETH",
                               amount=Decimal("0.001"), payee=EVM_PAYEE)
        self.assertTrue(evaluate(request, settings, SpendContext(), None).allow)

    def test_a_coin_ticker_after_the_amount_is_accepted(self) -> None:
        out = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                     "amount": "0.01 XMR", "payee": xmr_addr(3)})
        self.assertEqual(Decimal(out["request"]["amount"]), Decimal("0.01"))

    def test_the_daily_cap_is_per_coin_not_per_chain(self) -> None:
        """USDC on two chains is the same money: each chain used to get a full day's cap."""
        for kind in ("base-sepolia", "polygon-amoy"):
            self.state.ledger.append("spend_request", "approved", "test",
                                     {"asset": "USDC", "kind": kind, "amount": "30",
                                      "payee": EVM_PAYEE})
        # real money and play money are different pots, and neither is this one
        self.state.ledger.append("spend_request", "approved", "test",
                                 {"asset": "USDC", "kind": "base-mainnet", "amount": "7",
                                  "payee": EVM_PAYEE})
        request = SpendRequest(request_id="r", kind="base-sepolia", asset="USDC",
                               amount=Decimal("1"), payee=EVM_PAYEE)
        self.assertEqual(self.state._context(request, None).daily_spent, Decimal("60"))
        self.assertEqual([network_class(k) for k in ("sim-mainnet", "monero-stagenet",
                                                     "bitcoin-testnet", "base-mainnet")],
                         ["sim", "test", "test", "main"])


# ---------------------------------------------------------------- sandboxes

class SandboxLimitTests(_Home):
    def test_a_new_destination_does_not_skip_the_budget(self) -> None:
        """The escalation to a human sat above the budget check, so a payment to an address nobody
        had blessed went to the approval card without ever being measured against the budget."""
        sandbox_id = self._funded("0.2")
        out = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.4",
                                     "payee": xmr_addr(50), "sandbox_id": sandbox_id})
        self.assertFalse(out["decision"]["allow"])
        self.assertEqual(out["decision"]["code"], "sandbox_budget")

    def test_a_new_destination_inside_the_budget_still_asks_the_human(self) -> None:
        sandbox_id = self._funded("0.2")
        out = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.05",
                                     "payee": xmr_addr(50), "sandbox_id": sandbox_id})
        self.assertTrue(out["decision"]["allow"])
        self.assertEqual(out["decision"]["code"], "needs_operator")
        self.assertIn("approval_id", out)

    def test_a_new_destination_does_not_reopen_an_expired_budget(self) -> None:
        sandbox_id = self._funded("0.2")
        self.state.sandboxes[sandbox_id].expires_at = "2000-01-01T00:00:00Z"
        out = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.05",
                                     "payee": xmr_addr(50), "sandbox_id": sandbox_id})
        self.assertEqual(out["decision"]["code"], "sandbox_expired")

    def test_another_coin_cannot_be_charged_to_a_budget(self) -> None:
        sandbox_id = self._funded("0.2")
        out = self.state.request_tx({"kind": "bitcoin-testnet", "asset": "BTC",
                                     "amount": "0.0001", "payee": TESTNET_BTC,
                                     "sandbox_id": sandbox_id})
        self.assertEqual(out["decision"]["code"], "sandbox_scope")
        self.assertEqual(self.state.sandboxes[sandbox_id].spent, Decimal("0"))

    def test_the_budget_is_measured_again_at_approval(self) -> None:
        """Two requests that each fit an untouched budget do not both fit it."""
        sandbox_id = self._funded("0.2")
        first = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                       "amount": "0.15", "payee": xmr_addr(1),
                                       "sandbox_id": sandbox_id})
        second = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                        "amount": "0.15", "payee": xmr_addr(1),
                                        "sandbox_id": sandbox_id})
        self.assertEqual(first["decision"]["code"], "needs_pin")
        self.assertEqual(second["decision"]["code"], "needs_pin")
        self._approve(first)
        sent_before = self.state.fake.broadcast_count
        with self.assertRaises(WalletError) as caught:
            self._approve(second)
        self.assertEqual(caught.exception.code, "sandbox_budget")
        self.assertEqual(self.state.fake.broadcast_count, sent_before, "nothing may be sent")
        self.assertEqual(self.state.requests[second["request"]["request_id"]].status, "denied")
        self.assertLessEqual(self.state.sandboxes[sandbox_id].spent, Decimal("0.2"))

    def test_a_revoked_budget_cannot_be_spent_by_a_card_still_on_screen(self) -> None:
        sandbox_id = self._funded("0.2")
        pending = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR",
                                         "amount": "0.05", "payee": xmr_addr(1),
                                         "sandbox_id": sandbox_id})
        self.state.sandbox_revoke({"sandbox_id": sandbox_id, "sweep": False})
        with self.assertRaises(WalletError) as caught:
            self._approve(pending)
        self.assertEqual(caught.exception.code, "sandbox_closed")

    def test_a_bad_budget_or_address_is_refused_before_anything_is_created(self) -> None:
        accounts_before = FakeAdapter._next_account
        for params, code in (
                ({"budget": "abc"}, "bad_amount"),
                ({"budget": "0.1", "asset": "BTC"}, "asset_chain_mismatch"),
                ({"budget": "0.1", "lifetime_hours": "soon"}, "bad_setting"),
                ({"budget": "0.1", "allowed_payees": ["not-an-address"]}, "bad_payee")):
            with self.assertRaises(WalletError, msg=str(params)) as caught:
                self.state.sandbox_create({"kind": "monero-stagenet", "asset": "XMR",
                                           "lifetime_hours": 24, **params})
            self.assertEqual(caught.exception.code, code, params)
        self.assertEqual(self.state.sandboxes, {})
        self.assertEqual(FakeAdapter._next_account, accounts_before, "no orphan sub-account")


class PooledBudgetTests(unittest.TestCase):
    """Budgets on a chain without sub-accounts: enforced against the main balance."""

    class NoAccounts(FakeAdapter):
        supports_accounts = False
        kind = "bitcoin-testnet"
        asset = "BTC"

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        FakeAdapter._next_account = 1
        FakeAdapter.balances = {0: Decimal("1")}
        self.state = WalletState(Path(self.tmp.name))
        self.fake = self.NoAccounts()
        self.state.fake = self.fake
        self.state.initialize(PASS, pin=PIN)
        self.state.settings.chains_enabled = ["bitcoin-testnet"]
        self.state.settings.assets_enabled = ["BTC"]
        self.state.adapters = {"bitcoin-testnet": self.fake}
        self.state.settings.pin_every_payment = False

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _pay(self, sandbox_id: str, amount: str = "0.0001") -> dict:
        return self.state.request_tx({"kind": "bitcoin-testnet", "asset": "BTC", "amount": amount,
                                      "payee": TESTNET_BTC, "why": "vendor",
                                      "sandbox_id": sandbox_id, "_scope": "agent"})

    def test_one_time_means_one_payment_here_too(self) -> None:
        """`--one-time` was overwritten with "pooled" on these chains, so a budget meant for a
        single purchase took every payment that fitted in it."""
        created = self.state.sandbox_create({"kind": "bitcoin-testnet", "asset": "BTC",
                                             "budget": "0.0005", "mode": "one_time",
                                             "self_destruct_seconds": 60,
                                             "allowed_payees": [TESTNET_BTC]})
        sandbox = created["sandbox"]
        self.assertEqual(sandbox["mode"], "one_time")
        self.assertTrue(sandbox["pooled"])
        first = self._pay(sandbox["sandbox_id"])
        self.assertTrue(first.get("auto"), first)
        second = self._pay(sandbox["sandbox_id"])
        self.assertEqual(second["decision"]["code"], "sandbox_one_time_used")

        # ...and when its clock runs out it closes, without asking the chain for an account
        self.state.sandboxes[sandbox["sandbox_id"]].destroy_at = "2000-01-01T00:00:00Z"
        outcome = self.state.reap_sandboxes()
        self.assertTrue(outcome and outcome[0]["destroyed"], outcome)
        self.assertEqual(self.state.sandboxes[sandbox["sandbox_id"]].status(), "destroyed")

    def test_a_proposed_budget_can_be_armed_by_the_operator(self) -> None:
        """There is no funding transfer to approve on these chains, so a proposal had no way to
        become usable at all."""
        proposed = self.state.sandbox_propose({"kind": "bitcoin-testnet", "asset": "BTC",
                                               "budget": "0.0005",
                                               "allowed_payees": [TESTNET_BTC]})
        sandbox_id = proposed["sandbox"]["sandbox_id"]
        self.assertFalse(proposed["sandbox"]["enabled"])
        self.assertIn("arm", proposed["needs"])
        self.assertEqual(proposed["ignored"], ["allowed_payees", "allowed_profiles"])
        self.assertEqual(self._pay(sandbox_id)["decision"]["code"], "sandbox_unfunded")

        with self.assertRaises(WalletError) as caught:
            self.state.sandbox_arm({"sandbox_id": sandbox_id})
        self.assertEqual(caught.exception.code, "passphrase_required")
        armed = self.state.sandbox_arm({"sandbox_id": sandbox_id, "passphrase": PASS})
        self.assertTrue(armed["armed"])
        self.assertEqual(armed["sandbox"]["status"], "active")
        # armed is not blessed: the destination still goes to the human
        self.assertEqual(self._pay(sandbox_id)["decision"]["code"], "needs_operator")

    def test_arming_is_refused_for_a_funded_or_closed_budget(self) -> None:
        proposed = self.state.sandbox_propose({"kind": "bitcoin-testnet", "asset": "BTC",
                                               "budget": "0.0005"})
        sandbox_id = proposed["sandbox"]["sandbox_id"]
        self.state.sandbox_revoke({"sandbox_id": sandbox_id})
        with self.assertRaises(WalletError) as caught:
            self.state.sandbox_arm({"sandbox_id": sandbox_id, "passphrase": PASS})
        self.assertEqual(caught.exception.code, "sandbox_closed")


# ---------------------------------------------------------------- contacts and autopays

class ContactAndMandateTests(_Home):
    def test_promoting_an_agent_contact_needs_the_passphrase(self) -> None:
        """Re-saving the agent's contact on the operator token turned it into a blessed one."""
        saved = self.state.contact_save({"name": "eve", "_scope": "agent",
                                         "addresses": {"monero-stagenet": xmr_addr(5)}})
        self.assertEqual(saved["contact"]["created_by"], "agent")
        with self.assertRaises(WalletError) as caught:
            self.state.contact_save({"name": "eve", "_scope": "operator",
                                     "addresses": {"monero-stagenet": xmr_addr(5)}})
        self.assertEqual(caught.exception.code, "passphrase_required")
        self.assertEqual(self.state.contacts["eve"].created_by, "agent")
        promoted = self.state.contact_save({"name": "eve", "_scope": "operator",
                                            "passphrase": PASS,
                                            "addresses": {"monero-stagenet": xmr_addr(5)}})
        self.assertEqual(promoted["contact"]["created_by"], "operator")

    def _autopay(self) -> str:
        made = self.state.mandates_create({"kind": "monero-stagenet", "asset": "XMR",
                                           "payee": xmr_addr(7), "per_tx_cap": "0.01",
                                           "period_cap": "0.05", "absolute_cap": "0.5",
                                           "passphrase": PASS})
        return made["mandate"]["mandate_id"]

    def test_unattended_and_revoked_survive_a_restart(self) -> None:
        kept, gone = self._autopay(), self._autopay()
        self.state.mandates_set_unattended({"mandate_id": kept, "unattended": True,
                                            "passphrase": PASS, "pin": PIN})
        self.state.mandates_revoke({"mandate_id": gone})
        restarted = State(self.home)
        self.assertTrue(restarted.mandates[kept].unattended)
        self.assertEqual(restarted.mandates[gone].status(), "revoked")
        self.assertFalse(restarted.mandates[gone].unattended)

    def test_a_revoked_autopay_cannot_be_switched_back_on(self) -> None:
        mandate_id = self._autopay()
        self.state.mandates_revoke({"mandate_id": mandate_id})
        for call, params in ((self.state.mandates_set_enabled, {"enabled": True}),
                             (self.state.mandates_activate, {}),
                             (self.state.mandates_update, {"activate": True}),
                             (self.state.mandates_set_unattended, {"unattended": True, "pin": PIN})):
            with self.assertRaises(WalletError, msg=call.__name__) as caught:
                call({"mandate_id": mandate_id, "passphrase": PASS, **params})
            self.assertEqual(caught.exception.code, "mandate_revoked", call.__name__)
        self.assertEqual(self.state.mandates[mandate_id].status(), "revoked")

    def test_two_proposals_in_one_second_are_two_autopays(self) -> None:
        ids = {self.state.mandates_propose({"kind": "monero-stagenet", "asset": "XMR",
                                            "payee": xmr_addr(n), "per_tx_cap": "0.01",
                                            "period_cap": "0.05", "absolute_cap": "0.5"}
                                           )["mandate"]["mandate_id"] for n in (7, 8, 9)}
        self.assertEqual(len(ids), 3)
        self.assertEqual(len(self.state.mandates), 3)

    def test_the_usage_shown_is_the_usage_enforced(self) -> None:
        """A payment made before the autopay existed is not its spending."""
        self.state.ledger.append("spend_request", "approved", "test",
                                 {"asset": "XMR", "kind": "monero-stagenet", "amount": "0.03",
                                  "payee": xmr_addr(7)})
        mandate_id = self._autopay()
        self.state.mandates[mandate_id].created_at = "2999-01-01T00:00:00Z"
        row = next(m for m in self.state.mandates_list()["mandates"]
                   if m["mandate_id"] == mandate_id)
        self.assertEqual(Decimal(row["usage"]["period_spent"]), Decimal("0"))
        self.assertEqual(Decimal(row["usage"]["lifetime_spent"]), Decimal("0"))


# ---------------------------------------------------------------- chains and quotes

class ChainTests(unittest.TestCase):
    def _fees(self, gas_price: int, tip) -> dict:
        adapter = EvmAdapter("base-mainnet", "11" * 32)

        def rpc(method, params):
            if method == "eth_gasPrice":
                return hex(gas_price)
            if method == "eth_maxPriorityFeePerGas":
                if tip is None:
                    raise ChainError("rpc_error", "method not found")
                return hex(tip)
            raise AssertionError(method)

        with mock.patch.object(adapter, "_rpc", side_effect=rpc):
            return adapter._fees()

    def test_the_tip_never_exceeds_the_fee_ceiling(self) -> None:
        """On Base the gas price is a fraction of a gwei; a 1 gwei floor on the tip put it above
        the ceiling and every node rejected the transaction."""
        for gas_price, tip in ((5_000_000, None), (5_000_000, 10 ** 9), (1, None),
                               (30 * 10 ** 9, 25 * 10 ** 9), (0, None)):
            fees = self._fees(gas_price, tip)
            self.assertGreater(fees["maxFeePerGas"], 0)
            self.assertGreater(fees["maxPriorityPerGas"], 0)
            self.assertLessEqual(fees["maxPriorityPerGas"], fees["maxFeePerGas"], (gas_price, tip))

    def test_the_nodes_own_tip_is_used_when_it_fits(self) -> None:
        self.assertEqual(self._fees(30 * 10 ** 9, 25 * 10 ** 9)["maxPriorityPerGas"], 25 * 10 ** 9)

    def test_the_authorisation_names_the_network_the_seller_named(self) -> None:
        adapter = EvmAdapter("base-mainnet", "11" * 32)
        plan = adapter.plan_authorization(Decimal("0.01"), EVM_PAYEE, "USDC",
                                          resource="https://s.example/x", network="eip155:8453")
        self.assertEqual(plan.raw["x402"]["network"], "eip155:8453")
        default = adapter.plan_authorization(Decimal("0.01"), EVM_PAYEE, "USDC")
        self.assertEqual(default.raw["x402"]["network"], "base-mainnet")

    def test_bitcoin_addresses_of_every_kind_are_checked_properly(self) -> None:
        """The witness program length was compared in 5-bit groups against limits in bytes, which
        refused every P2WSH and taproot address; legacy addresses were sent to the bech32 check."""
        good = ["bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",                       # P2WPKH
                "bc1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qccfmv3",   # P2WSH
                "bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqzk5jj0",   # taproot
                "1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2",                               # P2PKH
                "3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy"]                               # P2SH
        from walletd.policy import payee_key
        for address in good:
            self.assertTrue(addresses.btc_valid(address), address)
            payee_key("bitcoin-mainnet", address)
        for address in ("bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t5",          # one character off
                        "1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN3",
                        "bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqzk5jj1",
                        TESTNET_BTC):                                          # wrong network
            self.assertFalse(addresses.btc_valid(address), address)
            with self.assertRaises(ValueError, msg=address):
                payee_key("bitcoin-mainnet", address)
        self.assertTrue(addresses.btc_valid(TESTNET_BTC, "test"))

    def test_a_rebroadcast_that_is_already_known_is_a_success(self) -> None:
        from walletd.chains.base import TxPlan
        from walletd.chains.bitcoin import BtcAdapter

        adapter = BtcAdapter("bitcoin-testnet", "22" * 32, api_url="http://127.0.0.1:9")
        plan = TxPlan(kind="bitcoin-testnet", asset="BTC", amount=Decimal("0.001"),
                      payee=TESTNET_BTC, raw={"raw_tx": "00", "txid": "ab" * 32})
        with mock.patch.object(adapter, "_http", side_effect=ChainError(
                "rpc_error", "/tx: HTTP 400 sendrawtransaction RPC error: txn-already-in-mempool")):
            self.assertEqual(adapter.broadcast(plan)["tx_hash"], "ab" * 32)
        with mock.patch.object(adapter, "_http", side_effect=ChainError(
                "rpc_error", "/tx: HTTP 400 bad-txns-inputs-missingorspent")):
            with self.assertRaises(ChainError):
                adapter.broadcast(plan)


class QuoteTests(unittest.TestCase):
    @staticmethod
    def _accept(**overrides) -> dict:
        row = {"scheme": "exact", "network": "base-sepolia", "asset": "USDC",
               "maxAmountRequired": "10000", "payTo": EVM_PAYEE}
        row.update(overrides)
        return row

    def _parse(self, accept: dict) -> pay402.Quote:
        body = json.dumps({"accepts": [accept]}).encode()
        return pay402.parse({}, body, "https://s.example/x")

    def test_a_quote_for_ethereum_mainnet_is_not_signed_for_base(self) -> None:
        for network in ("eip155:1", "ethereum", "eip155:42161"):
            with self.assertRaises(pay402.QuoteError, msg=network) as caught:
                self._parse(self._accept(network=network))
            self.assertEqual(caught.exception.code, "unsupported_quote")
            self.assertIn("not supported", caught.exception.message)
        self.assertEqual(self._parse(self._accept(network="eip155:8453")).kind, "base-mainnet")

    def test_only_the_exact_scheme_is_priced(self) -> None:
        with self.assertRaises(pay402.QuoteError) as caught:
            self._parse(self._accept(scheme="upto"))
        self.assertIn("scheme", caught.exception.message)

    def test_a_malformed_extra_block_is_not_a_crash(self) -> None:
        for extra in ("USD Coin", ["a"], 7, None):
            self.assertEqual(self._parse(self._accept(extra=extra)).asset, "USDC")

    def test_only_public_addresses_are_public(self) -> None:
        import ipaddress
        for text in ("127.0.0.1", "10.0.0.1", "100.64.0.1", "169.254.169.254", "::1",
                     "::ffff:127.0.0.1", "::ffff:10.0.0.1", "0.0.0.0"):
            self.assertFalse(pay402._is_public(ipaddress.ip_address(text)), text)
        self.assertTrue(pay402._is_public(ipaddress.ip_address("8.8.8.8")))

    def test_an_xrp_shaped_word_is_not_an_address(self) -> None:
        from walletd import invoice
        real = "rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh"
        page = invoice.extract(f"<p>pay {real}</p><p>ref rAbCdEfGhJkLmNpQrStUvWxYz123456</p>",
                               "https://s.example")
        self.assertEqual([t.payee for t in page.targets], [real])

    def test_a_native_ethereum_value_is_not_reported_as_usdc(self) -> None:
        from walletd import invoice
        page = invoice.extract(f'<a href="ethereum:{EVM_PAYEE}?value=1000000000000000000">pay</a>',
                               "https://s.example")
        self.assertEqual(page.targets[0].payee, EVM_PAYEE)
        self.assertIsNone(page.targets[0].amount, "1 ETH must not become '1 USDC'")


class FiatTests(unittest.TestCase):
    def test_every_spelling_of_a_currency(self) -> None:
        cases = {"10$": ("10", "USD"), "$10": ("10", "USD"), "10 usd": ("10", "USD"),
                 "usd 10": ("10", "USD"), "10 dollars": ("10", "USD"), "5 bucks": ("5", "USD"),
                 "10 euros": ("10", "EUR"), "10 euro": ("10", "EUR"), "10 EUR": ("10", "EUR"),
                 "€10": ("10", "EUR"), "7.50 pounds": ("7.50", "GBP"),
                 "£7.50": ("7.50", "GBP"), "1,000 usd": ("1000", "USD")}
        for text, (amount, currency) in cases.items():
            self.assertEqual(fx.split_fiat(text), (Decimal(amount), currency), text)

    def test_a_coin_amount_is_not_fiat(self) -> None:
        for text in ("0.05", "10 usdc", "0.001 xmr", "", None):
            self.assertIsNone(fx.split_fiat(text), text)

    def test_nonsense_is_refused(self) -> None:
        for text in ("ten dollars", "nan usd", "inf $", "-5 usd", "10 usd eur"):
            with self.assertRaises(fx.FxError, msg=text):
                fx.split_fiat(text)


# ---------------------------------------------------------------- the agent's tools

class ToolTests(unittest.TestCase):
    def test_pay_url_passes_the_amount_it_documents(self) -> None:
        from wallet import tools
        with mock.patch.object(tools, "_call", return_value={}) as call:
            tools.wallet_pay_url(url="https://shop.example/invoice", amount="10$", why="hosting")
        self.assertEqual(call.call_args[0][1]["amount"], "10$")
        with mock.patch.object(tools, "_call", return_value={}) as call:
            tools.wallet_pay_url(url="https://shop.example/invoice")
        self.assertNotIn("amount", call.call_args[0][1])

    def test_a_chain_without_a_coin_is_left_to_the_daemon(self) -> None:
        from wallet import tools
        with mock.patch.object(tools, "_call", return_value={}) as call:
            tools.wallet_request_payment(amount="0.0001", payee=TESTNET_BTC,
                                         kind="bitcoin-testnet")
        method, params = call.call_args[0]
        self.assertEqual(method, "request_tx")
        self.assertEqual((params["kind"], params["asset"]), ("bitcoin-testnet", ""))

    def test_no_chain_and_no_coin_means_the_preferred_coin(self) -> None:
        from wallet import tools
        status = {"default_chain": {"XMR": "monero-mainnet", "BTC": "bitcoin-mainnet"},
                  "preferred_asset": "BTC"}
        with mock.patch.object(tools, "_call",
                               side_effect=lambda m, p=None: status if m == "status" else {}) as call:
            tools.wallet_request_payment(amount="0.0001", payee="adrian")
        params = call.call_args[0][1]
        self.assertEqual((params["kind"], params["asset"]), ("bitcoin-mainnet", "BTC"))

    def test_the_guard_leaves_messaging_tools_alone(self) -> None:
        from wallet.tools import moves_money_out_of_band
        for name in ("send_message", "send_dm", "send_document", "send_voice", "send_image_file",
                     "send_to_url", "show_tip", "mcp__slack__send_message", "send_email"):
            self.assertFalse(moves_money_out_of_band(name), name)
        for name in ("xmr_send", "send_payment", "send_btc", "mm_transfer", "tip_a_friend",
                     "stripe.checkout", "mcp__bank__transfer"):
            self.assertTrue(moves_money_out_of_band(name), name)


# ---------------------------------------------------------------- plumbing

class PlumbingTests(_Home):
    pin_gate = False

    def test_a_paid_order_is_not_starved_by_unpaid_ones(self) -> None:
        """The per-call limit counted orders *looked at*, so four unpaid orders at the head of the
        list were re-checked on every tick and nothing behind them was ever merged."""
        opened = [self.state.receive_address({"kind": "monero-stagenet", "label": f"order {n}"})
                  for n in range(7)]
        paid = opened[-1]
        FakeAdapter.balances[paid["account_index"]] = Decimal("0.05")
        results = self.state.orders_merge()
        sent = [row for row in results if row.get("sent")]
        self.assertEqual([row["order_id"] for row in sent], [paid["order_id"]], results)
        # merged once: the next pass has nothing left to send
        self.assertEqual([r for r in self.state.orders_merge() if r.get("sent")], [])

    def test_a_dust_account_is_reported_once_not_every_tick(self) -> None:
        order = self.state.receive_address({"kind": "monero-stagenet", "label": "dust"})
        FakeAdapter.balances[order["account_index"]] = Decimal("0.00001")    # below the fee
        for _ in range(5):
            self.state.orders_merge()
        skipped = [e for e in self.state.ledger.entries() if e["decision"] == "merge_skipped"]
        self.assertEqual(len(skipped), 1)

    def test_state_files_are_replaced_whole(self) -> None:
        target = self.home / "probe.json"
        compat.write_json_atomic(target, {"a": 1})
        compat.write_json_atomic(target, {"a": 2, "b": [1, 2]})
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"a": 2, "b": [1, 2]})
        self.assertEqual([p.name for p in self.home.iterdir() if p.name.endswith(".tmp")], [])

    def test_a_failed_write_leaves_the_old_file_and_no_debris(self) -> None:
        target = self.home / "probe.json"
        compat.write_json_atomic(target, {"keep": True})
        with self.assertRaises(TypeError):
            compat.write_json_atomic(target, {"bad": object()})
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"keep": True})
        self.assertEqual([p.name for p in self.home.iterdir() if p.name.endswith(".tmp")], [])

    def test_every_request_survives_being_saved_and_reloaded(self) -> None:
        out = self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.01",
                                     "payee": xmr_addr(3), "idempotency_key": "retry-1"})
        again = State(self.home)
        self.assertIn(out["request"]["request_id"], again.requests)
        self.assertEqual(again.idempotency["retry-1"]["request_id"], out["request"]["request_id"])

    def test_every_rpc_reply_is_an_object(self) -> None:
        reply = build_methods()["reap_sandboxes"][1](self.state, {})
        self.assertIsInstance(reply, dict)
        self.assertEqual(reply["reaped"], [])
        self.assertIn("sandbox_arm", build_methods())

    def test_pin_status_while_locked_is_unknown_not_locked_out(self) -> None:
        with self.assertRaises(WalletError):
            self.state.settings.pin_every_payment = True
            self.state._verify_pin({"pin": "000000"})
        self.state.lock()
        status = self.state.pin_status()
        self.assertTrue(status["wallet_locked"])
        self.assertEqual(status["locked_until"], "")
        self.assertIsNone(status["attempts_left"])

    def test_verify_reports_a_deleted_ledger_instead_of_crashing(self) -> None:
        self.state.lock()
        (self.home / "ledger.jsonl").unlink()
        root = Path(__file__).resolve().parents[1]
        env = {**os.environ, "PYTHONPATH": str(root)}
        run = subprocess.run([sys.executable, "-m", "walletd", "--home", str(self.home), "verify"],
                             cwd=str(root), env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(run.returncode, 1, run.stderr)
        self.assertFalse(json.loads(run.stdout)["ok"])
        self.assertIn("were recorded", run.stdout)


class TransportTests(unittest.TestCase):
    def test_an_endpoint_file_can_only_name_loopback(self) -> None:
        self.assertEqual(compat.parse_endpoint("tcp://127.0.0.1:4242\n"), ("127.0.0.1", 4242))
        for text in ("tcp://10.0.0.5:4242", "tcp://evil.example:80", "tcp://127.0.0.1:0",
                     "tcp://127.0.0.1:99999", "http://127.0.0.1:80", "", "tcp://127.0.0.1:x"):
            self.assertIsNone(compat.parse_endpoint(text), text)

    def test_only_this_homes_daemon_is_recognised(self) -> None:
        home = Path(tempfile.gettempdir()) / "wallet-a"
        argv = ["python", "-m", "walletd", "--home", str(home), "serve"]
        self.assertTrue(compat.is_walletd_serving(argv, home))
        self.assertFalse(compat.is_walletd_serving(argv, home.with_name("wallet-b")))
        self.assertFalse(compat.is_walletd_serving(["python", "-m", "walletd", "--home",
                                                    str(home), "status"], home))
        self.assertFalse(compat.is_walletd_serving(None, home))

    def test_an_oversized_line_is_refused_without_being_buffered(self) -> None:
        from walletd import rpc
        from walletd.tokens import TokenStore

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            state = State(home)
            tokens = TokenStore(home / "tokens")
            tokens.mint_all()
            sock = home / "walletd.sock"
            with rpc.ServerThread(str(sock), state, tokens):
                with compat.connect(sock, 10.0) as conn:
                    conn.sendall(b"x" * (rpc.MAX_LINE + 10))
                    reply = b""
                    while not reply.endswith(b"\n"):
                        chunk = conn.recv(65536)
                        if not chunk:
                            break
                        reply += chunk
            self.assertEqual(json.loads(reply)["error"]["code"], "too_large")


class LedgerNetworkTests(unittest.TestCase):
    def test_totals_can_be_summed_per_network_class(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Ledger(Path(tmp) / "ledger.jsonl", b"s" * 32)
            for kind, amount in (("monero-mainnet", "1"), ("sim-mainnet", "5"),
                                 ("monero-stagenet", "9")):
                ledger.append("spend_request", "approved", "t",
                              {"asset": "XMR", "kind": kind, "amount": amount, "payee": "p"})
            self.assertEqual(ledger.totals("XMR", network="main"), Decimal("1"))
            self.assertEqual(ledger.totals("XMR", network="sim"), Decimal("5"))
            self.assertEqual(ledger.totals("XMR", network="test"), Decimal("9"))
            self.assertEqual(ledger.totals("XMR"), Decimal("15"))


if __name__ == "__main__":
    unittest.main()
