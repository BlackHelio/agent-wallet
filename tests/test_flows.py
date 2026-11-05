"""Flows added on top of the core wallet: orders, cancellation, spend reports, CSV export.

These sit at the seams the reviews pointed at: the agent-facing reads (`incoming`, `spend_totals`,
`ledger_csv`) and the cancellation path. Each test uses the same in-memory chain fixture as
`test_state`, so nothing here needs a daemon or a network.
"""

from __future__ import annotations

import argparse
import csv
import io
import tempfile
import unittest
from pathlib import Path

from tests.test_state import (FakeAdapter, PASS, TEST_ASSETS, TEST_CHAINS, WalletState,
                              xmr_addr)
from walletd import config as config_mod
from walletd.state import WalletError, build_methods

XMR_A = xmr_addr(10)
XMR_B = xmr_addr(40)


class FlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        FakeAdapter._next_account = 1
        FakeAdapter.balances = {}
        self.state = WalletState(self.home)
        self.state.fake = FakeAdapter()
        self.state.initialize(PASS)
        self.state.settings.chains_enabled = list(TEST_CHAINS)
        self.state.settings.assets_enabled = list(TEST_ASSETS)
        config_mod.save(self.state.config_path, self.state.settings, self.state.master)
        self.state.unlock(PASS)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _paid(self, amount: str, payee: str, asset: str = "XMR",
              kind: str = "monero-stagenet") -> None:
        self.state.ledger.append("spend_request", "approved", "manual_approval",
                                 {"request_id": f"r-{payee[:6]}-{amount}", "kind": kind,
                                  "asset": asset, "amount": amount, "payee": payee,
                                  "tx_hash": "aa" * 32})

    # ---------- spend totals ----------

    def test_spend_totals_per_asset_and_per_payee(self) -> None:
        self._paid("0.1", XMR_A)
        self._paid("0.2", XMR_A)
        self._paid("0.001", "bc1qtest", asset="BTC", kind="bitcoin-testnet")
        self._paid("0.05", "0xabc", asset="USDC", kind="base-sepolia")
        payload = self.state.spend_totals({})
        self.assertEqual(payload["totals"], {"XMR": "0.3", "BTC": "0.001", "USDC": "0.05"})
        self.assertEqual(payload["assets"]["XMR"], {"spent": "0.3", "count": 2})
        self.assertEqual(payload["assets"]["BTC"]["count"], 1)
        rows = {(row["payee"], row["asset"]): row for row in payload["payees"]}
        self.assertEqual(rows[(XMR_A, "XMR")]["spent"], "0.3")
        self.assertEqual(rows[(XMR_A, "XMR")]["count"], 2)
        self.assertEqual(rows[("bc1qtest", "BTC")]["count"], 1)
        self.assertEqual(rows[("0xabc", "USDC")]["spent"], "0.05")
        self.assertEqual(payload["since"], "")
        self.assertEqual(payload["until"], "")

    def test_spend_totals_ignore_denied_and_pending_receipts(self) -> None:
        self._paid("0.1", XMR_A)
        self.state.ledger.append("spend_request", "denied", "policy",
                                 {"asset": "XMR", "amount": "9", "payee": XMR_B})
        self.state.ledger.append("spend_request", "pending", "policy",
                                 {"asset": "XMR", "amount": "9", "payee": XMR_B})
        payload = self.state.spend_totals({})
        self.assertEqual(payload["totals"], {"XMR": "0.1"})
        self.assertEqual(len(payload["payees"]), 1)

    def test_spend_totals_windows(self) -> None:
        self._paid("0.1", XMR_A)
        self.assertEqual(self.state.spend_totals({"since": "30d"})["totals"]["XMR"], "0.1")
        self.assertEqual(self.state.spend_totals({"since": "2999-01-01T00:00:00Z"})["totals"], {})
        self.assertEqual(self.state.spend_totals({"until": "2000-01-01T00:00:00Z"})["totals"], {})
        self.assertEqual(self.state.spend_totals({"until": "2999-01-01T00:00:00Z"})["totals"]
                         ["XMR"], "0.1")
        with self.assertRaises(WalletError) as caught:
            self.state.spend_totals({"since": "yesterday"})
        self.assertEqual(caught.exception.code, "bad_window")
        with self.assertRaises(WalletError):
            self.state.spend_totals({"since": "2999-01-01", "until": "2000-01-01"})

    def test_spend_totals_still_counts_archived_receipts(self) -> None:
        self._paid("0.1", XMR_A)
        self.state.ledger_archive({})
        self._paid("0.2", XMR_A)
        payload = self.state.spend_totals({})
        self.assertEqual(payload["totals"]["XMR"], "0.3")
        self.assertEqual(payload["payees"][0]["count"], 2)

    def test_spend_totals_reads_receipts_while_locked(self) -> None:
        self._paid("0.1", XMR_A)
        self.state.lock()
        payload = self.state.spend_totals({})
        self.assertEqual(payload["totals"]["XMR"], "0.1")

    # ---------- CSV ----------

    def test_ledger_csv_has_the_promised_columns_and_every_row(self) -> None:
        before = len(list(self.state.ledger.entries()))
        self._paid("0.1", XMR_A)
        self._paid("0.2", XMR_B)
        payload = self.state.ledger_csv({})
        rows = list(csv.reader(io.StringIO(payload["csv"])))
        self.assertEqual(rows[0], ["seq", "ts", "kind", "decision", "rule",
                                   "asset", "amount", "payee", "tx_hash"])
        self.assertEqual(payload["rows"], before + 2)
        self.assertEqual(len(rows), before + 3)
        spends = [row for row in rows[1:] if row[2] == "spend_request"]
        self.assertEqual(len(spends), 2)
        self.assertEqual(spends[0][5:], ["XMR", "0.1", XMR_A, "aa" * 32])
        self.assertEqual(spends[1][6], "0.2")
        # amounts stay strings: csv gives text back, not a rounded float
        self.assertIsInstance(spends[0][6], str)

    def test_ledger_csv_window_and_locked_export(self) -> None:
        self._paid("0.1", XMR_A)
        self.assertEqual(self.state.ledger_csv({"until": "2000-01-01T00:00:00Z"})["rows"], 0)
        live = list(csv.reader(io.StringIO(self.state.ledger_csv({"since": "30d"})["csv"])))
        spends = [row for row in live if row[2] == "spend_request"]
        self.assertEqual(len(spends), 1)
        # locking writes its own receipt, so compare the spend rows rather than the total
        self.state.lock()
        payload = self.state.ledger_csv({"since": "30d"})
        locked = list(csv.reader(io.StringIO(payload["csv"])))
        self.assertEqual([row for row in locked if row[2] == "spend_request"], spends)
        self.assertIn("0.1", payload["csv"])
        with self.assertRaises(WalletError) as caught:
            self.state.ledger_csv({"since": "nonsense"})
        self.assertEqual(caught.exception.code, "bad_window")

    # ---------- wiring: RPC, routes, tools, CLI ----------

    def test_new_rpc_methods_are_registered_for_the_agent_scope(self) -> None:
        methods = build_methods()
        for name in ("incoming", "cancel_request", "spend_totals", "ledger_csv"):
            self.assertIn(name, methods)
            self.assertEqual(methods[name][0], "agent")

    def test_dashboard_serves_the_new_routes(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "dashboard" / "plugin_api.py").read_text(
            encoding="utf-8")
        for route in ('@router.get("/incoming")', '@router.post("/cancel")',
                      '@router.get("/spend")', '@router.get("/ledger.csv")'):
            self.assertIn(route, source)
        self.assertIn("PlainTextResponse", source)

    def test_tools_are_declared(self) -> None:
        from wallet.tools import TOOL_SPECS

        names = {spec[0] for spec in TOOL_SPECS}
        self.assertTrue({"wallet_incoming", "wallet_cancel", "wallet_spend"} <= names)

    def test_cli_parses_the_new_commands(self) -> None:
        from wallet import cli

        parser = argparse.ArgumentParser()
        cli.setup(parser)
        incoming = parser.parse_args(["incoming"])
        self.assertIs(incoming.func, cli.cmd_incoming)
        spend = parser.parse_args(["spend", "--since", "30d"])
        self.assertIs(spend.func, cli.cmd_spend)
        self.assertEqual(spend.since, "30d")
        cancel = parser.parse_args(["cancel", "req-1", "--reason", "duplicate"])
        self.assertIs(cancel.func, cli.cmd_cancel)
        self.assertEqual(cancel.request_id, "req-1")
        ledger = parser.parse_args(["ledger", "--csv", "--since", "7d"])
        self.assertTrue(ledger.csv)
        self.assertEqual(ledger.since, "7d")


if __name__ == "__main__":
    unittest.main()
