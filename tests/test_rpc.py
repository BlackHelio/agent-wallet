from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from walletd.rpc import ServerThread, call
from walletd.state import State, WalletError
from walletd.tokens import AGENT, OPERATOR, TokenStore

PASS = "test-passphrase-123"
ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def xmr_addr(offset: int) -> str:
    body = (ALPHABET[offset:] + ALPHABET[:offset]) * 3
    return "57" + body[:93]


class RpcTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.state = State(self.home)
        self.state.initialize(PASS)
        from walletd import config as config_mod

        self.state.settings.chains_enabled = ["monero-stagenet", "sim-mainnet"]
        self.state.settings.assets_enabled = ["XMR", "BTC", "ETH", "POL", "USDC"]
        config_mod.save(self.state.config_path, self.state.settings, self.state.master)
        self.tokens = TokenStore(self.home / "tokens")
        self.tokens.mint_all()
        self.socket = str(self.home / "walletd.sock")
        self.server = ServerThread(self.socket, self.state, self.tokens)
        self.server.__enter__()

    def tearDown(self) -> None:
        self.server.__exit__(None, None, None)
        self.tmp.cleanup()

    def _call(self, method: str, params: dict | None = None, scope: str = AGENT) -> dict:
        return call(self.socket, method, params, token=self.tokens.read(scope))

    def test_agent_can_read_status(self) -> None:
        result = self._call("status")
        self.assertIn("locked", result)
        self.assertNotIn("master", json.dumps(result))

    def test_agent_cannot_unlock(self) -> None:
        with self.assertRaises(WalletError) as caught:
            self._call("unlock", {"passphrase": PASS}, scope=AGENT)
        self.assertEqual(caught.exception.code, "forbidden")

    def test_agent_cannot_approve(self) -> None:
        with self.assertRaises(WalletError) as caught:
            self._call("approve", {"request_id": "x", "approval_id": "y"}, scope=AGENT)
        self.assertEqual(caught.exception.code, "forbidden")

    def test_agent_cannot_change_human_gate(self) -> None:
        with self.assertRaises(WalletError) as caught:
            self._call("set_human_gate", {"enabled": False, "passphrase": PASS, "confirm": True})
        self.assertEqual(caught.exception.code, "forbidden")

    def test_unknown_token_rejected(self) -> None:
        with self.assertRaises(WalletError) as caught:
            call(self.socket, "status", {}, token="deadbeef")
        self.assertEqual(caught.exception.code, "unauthorized")

    def test_unknown_method_rejected(self) -> None:
        with self.assertRaises(WalletError) as caught:
            self._call("delete_everything", {})
        self.assertEqual(caught.exception.code, "unknown_method")

    def test_full_flow_over_socket(self) -> None:
        self._call("unlock", {"passphrase": PASS}, scope=OPERATOR)
        # An approval needs a signer to sign on: give the state a stub adapter for stagenet, the way
        # a real install has one whenever its chain is enabled and the signer is up.
        from .test_state import FakeAdapter

        adapter = FakeAdapter()
        adapter.kind = "monero-stagenet"
        self.state.adapters["monero-stagenet"] = adapter
        addr = xmr_addr(20)
        requested = self._call("request_tx", {"kind": "monero-stagenet", "asset": "XMR",
                                              "amount": "0.01", "payee": addr, "why": "unit test"})
        self.assertIn("approval_id", requested)
        approved = self._call("approve", {"request_id": requested["request"]["request_id"],
                                          "approval_id": requested["approval_id"],
                                          "passphrase": PASS}, scope=OPERATOR)
        self.assertIn(approved["request"]["status"], ("approved", "sent", "confirmed"))
        verified = self._call("ledger_verify")
        self.assertTrue(verified["ok"], verified)

    def test_wrong_passphrase_is_a_clean_error(self) -> None:
        with self.assertRaises(WalletError) as caught:
            self._call("unlock", {"passphrase": "nope"}, scope=OPERATOR)
        self.assertEqual(caught.exception.code, "bad_passphrase")

    def test_socket_mode_is_0600(self) -> None:
        self.assertEqual(os.stat(self.socket).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
