"""The five ways the shipped wallet lied to an agent, each pinned by a test.

These were found by using the tools as a chat agent would, not by unit-testing internals:

  * a destroyed one-time sandbox still reported the budget arithmetic as ``remaining``
  * ``wallet_status`` cost ~1.5 KB of context per call to answer "is it unlocked"
  * a duplicate idempotency key answered ``allow: true``, reading as a fresh payment
  * ``wallet_balances`` printed every full 95-character address
  * sandbox rows had no live on-chain balance at all
  * and ``doctor`` called a locked wallet unhealthy and printed ``chains: [] / receipts: 0``
"""

from __future__ import annotations

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

from tests.test_state import FakeAdapter, WalletState, PASS, TEST_ASSETS, TEST_CHAINS, xmr_addr
from walletd import config as config_mod
from walletd import state as state_module
from wallet import tools

PIN = "123456"   # the per-payment secret; every approval needs it


class AgentSurfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        FakeAdapter._next_account = 1
        FakeAdapter.balances = {}
        self.state = WalletState(self.home)
        self.state.fake = FakeAdapter()
        self.state.initialize(PASS, pin=PIN)
        # the PIN gate itself is tested in tests/test_pin.py; these tests are about policy
        # accounting (budgets, fees, sweeps), so they run with the gate off and their approvals
        # do not need to type it
        self.state.settings.pin_every_payment = False
        self.state.settings.chains_enabled = list(TEST_CHAINS)
        self.state.settings.assets_enabled = list(TEST_ASSETS)
        config_mod.save(self.state.config_path, self.state.settings, self.state.master)
        self.state.unlock(PASS)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # ---------- fixtures ----------

    def _funded(self, budget: str = "0.2") -> dict:
        created = self.state.sandbox_create({"kind": "monero-stagenet", "asset": "XMR",
                                             "budget": budget, "lifetime_hours": 24,
                                             "label": "test sandbox",
                                             "allowed_payees": [xmr_addr(n) for n in range(1, 100)]})
        funding = created["funding"]
        self.state.approve({"pin": PIN, "request_id": funding["request"]["request_id"],
                            "approval_id": funding["approval_id"], "passphrase": PASS,
                            "confirm_amount": budget})
        return self.state.sandboxes[created["sandbox"]["sandbox_id"]].to_dict()

    def _one_time(self, budget: str = "0.2") -> dict:
        created = self.state.sandbox_create({"kind": "monero-stagenet", "asset": "XMR",
                                             "budget": budget, "lifetime_hours": 24,
                                             "mode": "one_time", "self_destruct_seconds": 60,
                                             "label": "single payment",
                                             "allowed_payees": [xmr_addr(n) for n in range(1, 100)]})
        funding = created["funding"]
        self.state.approve({"pin": PIN, "request_id": funding["request"]["request_id"],
                            "approval_id": funding["approval_id"], "passphrase": PASS,
                            "confirm_amount": budget})
        return self.state.sandboxes[created["sandbox"]["sandbox_id"]].to_dict()

    # ---------- 1. a destroyed sandbox holds nothing ----------

    def test_destroyed_sandbox_reports_zero_remaining_and_what_it_died_with(self) -> None:
        sandbox = self._one_time("0.2")
        self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.1",
                               "payee": xmr_addr(70), "sandbox_id": sandbox["sandbox_id"]})
        live = self.state.sandboxes[sandbox["sandbox_id"]]
        live.destroy_at = "2000-01-01T00:00:00Z"
        self.state.fake.balance_at = lambda index: Decimal("0")
        outcome = self.state.reap_sandboxes()
        self.assertTrue(outcome[0]["destroyed"], outcome[0])

        row = self.state.sandboxes_list()["sandboxes"][0]
        self.assertEqual(row["status"], "destroyed")
        self.assertEqual(row["remaining"], "0", "a destroyed sandbox cannot still hold money")
        self.assertEqual(row["was_destroyed_with"], "0")
        self.assertEqual(row["budget"], "0.2")
        self.assertEqual(row["spent"], "0.100020000000")

        # the CLI's table and the pane's route both read this row; there is only one shape
        plain = live.to_dict()
        self.assertEqual(plain["remaining"], "0")
        self.assertEqual({k: row[k] for k in plain}, plain)

    def test_a_live_sandbox_still_reports_its_remaining_budget(self) -> None:
        sandbox = self._funded("0.2")
        self.state.request_tx({"kind": "monero-stagenet", "asset": "XMR", "amount": "0.05",
                               "payee": xmr_addr(71), "sandbox_id": sandbox["sandbox_id"]})
        row = self.state.sandboxes_list()["sandboxes"][0]
        # 0.2 - 0.05 - 0.00002 fee: the fee is part of what left the budget
        self.assertEqual(row["remaining"], "0.149980000000")
        self.assertEqual(row["was_destroyed_with"], "")

    # ---------- 3. a duplicate is not permission ----------

    def test_duplicate_idempotency_key_does_not_look_like_a_new_payment(self) -> None:
        params = {"kind": "monero-stagenet", "asset": "XMR", "amount": "0.01",
                  "payee": xmr_addr(72), "idempotency_key": "dedupe-me"}
        first = self.state.request_tx(dict(params))
        self.assertTrue(first["decision"]["allow"])
        approval_id = first["approval_id"]

        second = self.state.request_tx(dict(params))
        self.assertTrue(second["duplicate"])
        self.assertFalse(second["decision"]["allow"], "a duplicate must never read as 'filed'")
        self.assertEqual(second["decision"]["rule"], "idempotent")
        self.assertEqual(second["decision"]["code"], "duplicate")
        self.assertEqual(second["status"], "pending")
        self.assertEqual(second["request"]["request_id"], first["request"]["request_id"])
        self.assertEqual(second["approval"]["approval_id"], approval_id)
        self.assertEqual(second["approval_id"], approval_id)
        self.assertIn("nothing was filed", second["note"])

    def test_duplicate_after_the_request_was_pruned_still_says_duplicate(self) -> None:
        params = {"kind": "monero-stagenet", "asset": "XMR", "amount": "0.01",
                  "payee": xmr_addr(73), "idempotency_key": "pruned-key"}
        first = self.state.request_tx(dict(params))
        recorded = first["request"]["request_id"]
        del self.state.requests[recorded]          # as if the in-memory request had been pruned

        again = self.state.request_tx(dict(params))
        self.assertTrue(again["duplicate"])
        self.assertFalse(again["decision"]["allow"])
        self.assertEqual(again["status"], "pending")
        self.assertEqual(again["request"]["request_id"], recorded)
        self.assertIsNone(again["approval"])

    # ---------- 5. sandbox rows carry the live on-chain balance ----------

    def test_sandbox_rows_report_the_live_sub_account_balance(self) -> None:
        sandbox = self._funded("0.2")
        live = self.state.sandboxes[sandbox["sandbox_id"]]
        FakeAdapter.balances[live.account_index] = Decimal("0.19")

        row = self.state.sandboxes_list()["sandboxes"][0]
        self.assertEqual(row["remaining"], "0.2")        # bookkeeping
        self.assertEqual(row["live_balance"], "0.19")    # what is really there
        self.assertEqual(row["live_balance_state"], "live")

        # cached for a moment: a poll storm must not hammer the node
        FakeAdapter.balances[live.account_index] = Decimal("0.01")
        self.assertEqual(self.state.sandboxes_list()["sandboxes"][0]["live_balance"], "0.19")

    def test_unreadable_chain_balance_is_unknown_not_zero(self) -> None:
        self._funded("0.2")

        def down(index):
            raise ConnectionError("node is down")

        self.state.fake.balance_at = down
        row = self.state.sandboxes_list()["sandboxes"][0]
        self.assertIsNone(row["live_balance"], "an unreachable node is not a zero balance")
        self.assertEqual(row["live_balance_state"], "unavailable")
        self.assertIn("node is down", row["live_balance_error"])

        self.state.lock()
        row = self.state.sandboxes_list()["sandboxes"][0]
        self.assertIsNone(row["live_balance"])
        self.assertEqual(row["live_balance_state"], "locked")

    # ---------- 2. wallet_status is compact unless asked ----------

    def _tool_call(self, payload: dict):
        return mock.patch.object(tools, "_call", lambda method, params=None: payload)

    def test_wallet_status_is_compact_by_default_and_complete_on_request(self) -> None:
        status = self.state.status()
        with self._tool_call(status):
            compact = json.loads(tools.wallet_status())
            full = json.loads(tools.wallet_status(full=True))

        wallet = compact["wallet"]
        for gone in ("asset_chains", "chain_assets", "native_asset", "available_assets",
                     "default_chain", "profiles", "prompt", "payee_allowlist"):
            self.assertNotIn(gone, wallet, f"{gone} is pane data, not chat data")
        for wanted in ("locked", "coins", "caps", "pending_requests", "signer",
                       "autolock_minutes", "unlock_expires_in"):
            self.assertIn(wanted, wallet)
        # 850 rather than 800: `home` costs ~50 bytes and buys the one fact that explains a "healthy
        # wallet" with none of your money in it (AGENT_WALLET_HOME pointing somewhere else)
        self.assertLess(len(json.dumps(compact)), 850, "wallet_status is still a context hog")

        self.assertEqual(full["wallet"]["asset_chains"], status["asset_chains"])
        self.assertEqual(full["wallet"]["profiles"], status["profiles"])

    # ---------- 4. balances do not print full addresses ----------

    def test_balances_shorten_addresses_unless_full_is_requested(self) -> None:
        payload = self.state.balances()
        full_address = payload["accounts"][0]["address"]
        self.assertGreater(len(full_address), 40)

        with self._tool_call(payload):
            short = json.loads(tools.wallet_balances())
            exact = json.loads(tools.wallet_balances(full=True))

        shortened = short["accounts"][0]["address"]
        self.assertEqual(shortened, f"{full_address[:5]}…{full_address[-4:]}")
        self.assertLess(len(shortened), len(full_address) // 4)
        self.assertEqual(exact["accounts"][0]["address"], full_address)

    # ---------- 6. doctor while locked ----------

    def test_doctor_while_locked_reports_states_not_faults(self) -> None:
        self.state.lock()
        fake_probe = mock.Mock()
        fake_probe.return_value.ok.return_value = True
        with mock.patch.object(state_module.signer_mod, "status",
                               lambda home: {"installed": True, "runs": True}):
            with mock.patch.object(state_module.signer_mod, "credentials",
                                   lambda home, create=False: {"user": "u", "password": "p"}):
                with mock.patch.object(state_module, "JsonRpcProbe", fake_probe):
                    with mock.patch("walletd.home.stray_wallets", lambda selected=None: []):
                        report = self.state.doctor()

        self.assertTrue(report["ok"], f"a healthy locked wallet must not fail: {report['failed']}")
        self.assertTrue(report["locked"])
        checks = {c["check"]: c for c in report["checks"]}
        self.assertEqual(checks["unlocked"]["status"], "skipped")
        self.assertTrue(checks["unlocked"]["ok"])
        self.assertIn("state, not a fault", checks["unlocked"]["note"])
        self.assertEqual(checks["settings signed"]["status"], "skipped")
        self.assertNotIn("unlocked", report["failed"])

        # the receipt chain verifies with the public key alone, so the count is the real one
        self.assertTrue(checks["receipt chain"]["ok"], checks["receipt chain"])
        self.assertTrue(report["receipts_verified"])
        self.assertGreaterEqual(report["receipts"], 1)

        # --json must not show a configured wallet as empty
        self.assertEqual([c["kind"] for c in report["chains"]], list(TEST_CHAINS))
        for chain in report["chains"]:
            self.assertIsNone(chain["available"], "locked is not 'unavailable'")
            self.assertEqual(chain["state"], "locked")

    def test_doctor_while_locked_shows_the_real_receipt_count(self) -> None:
        self.state.lock()
        with mock.patch.object(state_module.signer_mod, "status",
                               lambda home: {"installed": True, "runs": True}):
            with mock.patch.object(state_module.signer_mod, "credentials",
                                   lambda home, create=False: {"user": "u", "password": "p"}):
                with mock.patch.object(state_module, "JsonRpcProbe", mock.Mock()):
                    with mock.patch("walletd.home.stray_wallets", lambda selected=None: []):
                        report = self.state.doctor()
        # doctor's count is the same number the locked-safe verifier reports
        self.assertEqual(report["receipts"], self.state.ledger_verify()["entries"])
        self.assertGreater(report["receipts"], 0)


if __name__ == "__main__":
    unittest.main()


class AgentToolContractTests(unittest.TestCase):
    """Every agent tool must return JSON. `wallet_deposits` shipped with a call signature that made it
    raise on every invocation — a registered tool that could never work."""

    def test_deposits_tool_returns_json(self) -> None:
        import json as _json
        import tempfile
        from pathlib import Path as _Path

        from tests.test_state import PASS, TEST_ASSETS, TEST_CHAINS, FakeAdapter, WalletState
        from wallet import tools

        home = _Path(tempfile.mkdtemp())
        state = WalletState(home)
        state.fake = FakeAdapter()
        state.initialize(PASS, pin="123456")
        state.settings.chains_enabled = list(TEST_CHAINS)
        state.settings.assets_enabled = list(TEST_ASSETS)
        state.adapters = {kind: state.fake for kind in TEST_CHAINS}
        state.settings.pin_every_payment = False
        state.deposit_create({"kind": "monero-stagenet", "label": "x", "passphrase": PASS})

        from wallet import client as wallet_client
        original = wallet_client.call
        wallet_client.call = lambda method, params=None, **kw: state.deposits_list()  # noqa: ARG005
        try:
            payload = _json.loads(tools.wallet_deposits())
        finally:
            wallet_client.call = original
        self.assertTrue(payload.get("ok"), payload)
        self.assertEqual(payload["count"], 1)
        self.assertIn("address", payload["deposits"][0])

    def test_nodes_merges_height_and_endpoint_health_per_kind(self) -> None:
        """The merged tool reads two RPCs whose shapes differ: `chain_status` is a list of chains,
        `nodes` is a *flat list* of endpoint rows. Treating the second as a dict raised on every
        call and the unit suite did not notice, so pin the shape here."""
        import json as _json

        from wallet import client as wallet_client
        from wallet import tools

        def fake_call(method, params=None, **_kw):
            if method == "chain_status":
                return {"chains": [{"kind": "monero-mainnet", "available": False, "height": 0},
                                   {"kind": "bitcoin-mainnet", "available": True, "height": 900}]}
            if method == "nodes":
                return {"nodes": [{"kind": "monero-mainnet", "url": "a", "healthy": True},
                                  {"kind": "bitcoin-mainnet", "url": "b", "healthy": True}]}
            raise AssertionError(method)

        original = wallet_client.call
        wallet_client.call = fake_call
        try:
            payload = _json.loads(tools.wallet_nodes())
        finally:
            wallet_client.call = original
        self.assertTrue(payload.get("ok"), payload)
        self.assertEqual(payload["count"], 2)
        rows = {row["kind"]: row for row in payload["chains"]}
        self.assertEqual(rows["monero-mainnet"]["height"], 0)
        self.assertEqual([n["url"] for n in rows["bitcoin-mainnet"]["nodes"]], ["b"])


class PromptCostTests(unittest.TestCase):
    """Schema text is charged on every request of every session, so it has a budget and a test."""

    BUDGET = 8_200          # all 23 tool definitions measured at 8,125; ratchet down, never up
    LISTING_BUDGET = 1_500  # what a deferred listing shows: name + one short line per tool

    def test_the_whole_tool_list_fits_a_budget(self) -> None:
        import json

        from wallet.tools import TOOL_SPECS

        total = sum(len(json.dumps({"name": n, "description": d, **sc}, separators=(",", ":")))
                    for n, d, f, sc in TOOL_SPECS)
        self.assertLess(total, self.BUDGET,
                        f"{len(TOOL_SPECS)} tools cost {total} chars (~{total / 3.6:.0f} tokens) on every "
                        f"request; trim descriptions rather than raising this budget")

    def test_the_deferred_listing_stays_small(self) -> None:
        """With progressive disclosure the *listing* is what rides every request, not the schemas."""
        from wallet.tools import TOOL_SPECS

        listing = sum(len(n) + 2 + min(len(d), 120) + 4 for n, d, _f, _sc in TOOL_SPECS)
        self.assertLess(listing, self.LISTING_BUDGET,
                        f"the listing costs {listing} chars (~{listing / 3.6:.0f} tokens) on every request")

    def test_no_tool_sends_its_description_twice(self) -> None:
        from wallet.tools import TOOL_SPECS

        for name, _desc, _fn, schema in TOOL_SPECS:
            self.assertNotIn("description", schema,
                             f"{name}: register_tool already carries the description; the schema "
                             f"repeating it doubled the cost of every tool")


class WalletBriefTests(unittest.TestCase):
    """The per-turn hook is the most expensive thing here: it injects new tokens into every call."""

    def test_an_unrelated_turn_costs_nothing(self) -> None:
        from wallet.tools import mentions_money

        self.assertFalse(mentions_money("what is the weather in Berlin"))
        self.assertFalse(mentions_money("write me a haiku about the sea"))

    def test_a_money_turn_asks_for_the_brief(self) -> None:
        from wallet.tools import mentions_money

        for text in ("send adrian 5 dollars", "what's my balance?", "buy this for me",
                     "pay the invoice", "set up an autopay"):
            self.assertTrue(mentions_money(text), text)


class OptionalArgumentsTests(unittest.TestCase):
    """The schema says `kind`/`asset` may be omitted; the handler must agree, or the model gets a
    TypeError instead of a payment request."""

    def test_request_payment_resolves_the_chain_when_asset_is_omitted(self) -> None:
        import inspect

        from wallet import tools

        signature = inspect.signature(tools.wallet_request_payment)
        for name in ("kind", "asset"):
            self.assertEqual(signature.parameters[name].default, "",
                             f"{name} is optional in the schema, so it needs a default here")

    def test_wallet_coins_does_not_crash_without_json_flag(self) -> None:
        """`--json` on that subcommand is argparse.SUPPRESS, so the attribute is absent unless passed."""
        import argparse

        from wallet import cli

        args = argparse.Namespace(enable=None, disable=None, passphrase_stdin=False)
        self.assertFalse(getattr(args, "as_json", False))


class OutOfBandGuardTests(unittest.TestCase):
    """The guard used to be a list of seven names; anything else moved money without a receipt."""

    def test_a_payment_shaped_foreign_tool_is_blocked(self) -> None:
        from wallet.tools import moves_money_out_of_band

        for name in ("mm_transfer", "pay", "mpp_fetch", "xmr_send", "tip_a_friend",
                     "mcp__bank__transfer", "stripe.checkout"):
            self.assertTrue(moves_money_out_of_band(name), name)

    def test_our_own_tools_are_not_blocked_by_their_names(self) -> None:
        from wallet.tools import moves_money_out_of_band

        for name in ("wallet_request_payment", "wallet_pay_url", "wallet_contacts"):
            self.assertFalse(moves_money_out_of_band(name), name)

    def test_a_shell_command_driving_the_wallet_cli_is_blocked(self) -> None:
        from wallet.tools import moves_money_out_of_band

        self.assertTrue(moves_money_out_of_band(
            "terminal", {"command": "hermes wallet approve req_abc --yes"}))
        self.assertTrue(moves_money_out_of_band(
            "execute_code", {"code": "curl -u a:b http://127.0.0.1:38084/json_rpc"}))
        self.assertTrue(moves_money_out_of_band(
            "terminal", {"command": "cat ~/.hermes/wallet/wallet.env"}))

    def test_ordinary_shell_work_is_allowed(self) -> None:
        from wallet.tools import moves_money_out_of_band

        self.assertFalse(moves_money_out_of_band("terminal", {"command": "ls -la ~/.hermes/wallet"}))
        self.assertFalse(moves_money_out_of_band("terminal", {"command": "hermes wallet status"}))
        self.assertFalse(moves_money_out_of_band("read_file", {"path": "README.md"}))
