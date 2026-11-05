"""Paying an HTTP 402: the price must come from the server, never from the caller.

A real HTTP server answers, so the parse path (headers, atomic units, base64 payload) is exercised
the way it will be in anger. The wallet has no EVM signing adapter yet, so a valid quote ends in a
`chain_unavailable` denial — but only after the *quoted* amount and payee reached policy, which is
the property under test.
"""

from __future__ import annotations

import base64
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from walletd import pay402
from walletd.state import State, WalletError

PASS = "test-passphrase-123"
PAY_TO = "0x1234567890abcdef1234567890abcdef12345678"


def _accepts(**overrides) -> dict:
    accept = {
        "scheme": "exact",
        "network": "base-sepolia",
        "maxAmountRequired": "10000",          # atomic USDC -> 0.01
        "resource": "https://shop.example/item/42",
        "description": "One widget",
        "payTo": PAY_TO,
        "asset": "USDC",
        "extra": {"name": "USDC", "version": "2"},
    }
    accept.update(overrides)
    return accept


class _Handler(BaseHTTPRequestHandler):
    payload: dict = {"accepts": [_accepts()]}
    as_header = False
    seen_payment = None      # the X-PAYMENT envelope the seller received

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        paid = self.headers.get("X-PAYMENT")
        if paid:
            _Handler.seen_payment = paid
            body = json.dumps({"ok": True, "item": "widget", "paid": True}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = json.dumps(self.payload).encode()
        if self.as_header:
            self.send_response(402)
            self.send_header("PAYMENT-REQUIRED",
                             base64.b64encode(body).decode())
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(402)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args) -> None:  # noqa: A002 - http.server API
        return


import os
os.environ.setdefault("AGENT_WALLET_ALLOW_PRIVATE_402", "1")   # the fixture serves from 127.0.0.1


class Pay402Tests(unittest.TestCase):
    def setUp(self) -> None:
        _Handler.payload = {"accepts": [_accepts()]}
        _Handler.as_header = False
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/item/42"

        self.tmp = tempfile.TemporaryDirectory()
        self.state = State(Path(self.tmp.name))
        self.state.initialize(PASS)
        from walletd import config as config_mod

        self.state.settings.chains_enabled = ["base-sepolia", "sim-mainnet"]
        # the wallet ships with XMR/BTC/XRP; a 402 test spends USDC on an EVM chain, so enable it
        self.state.settings.assets_enabled = ["USDC", "ETH", "XMR"]
        config_mod.save(self.state.config_path, self.state.settings, self.state.master)
        self.state.unlock(PASS)

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    # ---------- parsing ----------

    def test_json_body_quote_is_read_in_atomic_units(self) -> None:
        quote = pay402.fetch(self.url)
        self.assertEqual(quote.amount, pay402.Decimal("0.01"))
        self.assertEqual(quote.asset, "USDC")
        self.assertEqual(quote.kind, "base-sepolia")
        self.assertEqual(quote.payee, PAY_TO)
        self.assertEqual(quote.description, "One widget")
        self.assertTrue(quote.digest)

    def test_header_quote_is_decoded(self) -> None:
        _Handler.as_header = True
        quote = pay402.fetch(self.url)
        self.assertEqual(quote.amount, pay402.Decimal("0.01"))

    def test_lightning_invoice_is_refused_loudly(self) -> None:
        _Handler.payload = {}
        _Handler.as_header = False

        class _Lightning(_Handler):
            def do_GET(self) -> None:  # noqa: N802
                self.send_response(402)
                self.send_header("WWW-Authenticate", 'L402 macaroon="abc", invoice="lnbc1..."')
                self.send_header("Content-Length", "0")
                self.end_headers()

        server = ThreadingHTTPServer(("127.0.0.1", 0), _Lightning)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with self.assertRaises(pay402.QuoteError) as caught:
                pay402.fetch(f"http://127.0.0.1:{server.server_port}/x")
            self.assertEqual(caught.exception.code, "unsupported_scheme")
        finally:
            server.shutdown()
            server.server_close()

    def test_unsupported_network_is_reported_not_guessed(self) -> None:
        _Handler.payload = {"accepts": [_accepts(network="solana")]}
        with self.assertRaises(pay402.QuoteError) as caught:
            pay402.fetch(self.url)
        self.assertEqual(caught.exception.code, "unsupported_quote")
        self.assertIn("solana", caught.exception.message)

    def test_http_200_means_nothing_to_pay(self) -> None:
        class _Free(_Handler):
            def do_GET(self) -> None:  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

        server = ThreadingHTTPServer(("127.0.0.1", 0), _Free)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with self.assertRaises(pay402.QuoteError) as caught:
                pay402.fetch(f"http://127.0.0.1:{server.server_port}/free")
            self.assertEqual(caught.exception.code, "not_payment")
        finally:
            server.shutdown()
            server.server_close()

    # ---------- the wallet's own behaviour ----------

    def test_pay_url_prices_the_request_from_the_server(self) -> None:
        result = self.state.pay_url({"url": self.url, "why": "buy a widget"})
        request = result["request"]
        self.assertEqual(pay402.Decimal(request["amount"]), pay402.Decimal("0.01"))
        self.assertEqual(request["payee"], PAY_TO.lower())
        self.assertEqual(request["asset"], "USDC")
        self.assertEqual(request["challenge_sha256"], result["quote"]["challenge_sha256"])
        # A priced purchase is an ordinary pending request: the human still decides.
        self.assertEqual(result["decision"]["code"], "needs_operator")
        self.assertEqual(request["status"], "pending")

    def test_the_caller_cannot_set_the_price(self) -> None:
        """A 402 request carries no amount/payee fields at all: extra params are ignored."""
        honest = self.state.pay_url({"url": self.url})
        injected = self.state.pay_url({"url": self.url, "amount": "5000", "payee": "0x" + "9" * 40})
        self.assertEqual(injected["request"]["amount"], honest["request"]["amount"])
        self.assertEqual(injected["request"]["payee"], honest["request"]["payee"])

    def test_a_different_quote_is_a_different_digest(self) -> None:
        first = pay402.fetch(self.url).digest
        _Handler.payload = {"accepts": [_accepts(maxAmountRequired="25000")]}
        second = pay402.fetch(self.url)
        self.assertNotEqual(first, second.digest)
        self.assertEqual(second.amount, pay402.Decimal("0.025"))

    def test_bad_url_raises_a_typed_error(self) -> None:
        with self.assertRaises(WalletError) as caught:
            self.state.pay_url({"url": "file:///etc/passwd"})
        self.assertEqual(caught.exception.code, "bad_url")


class SubmitIsNotAnSsrfTests(unittest.TestCase):
    """settle() sends an EIP-3009 signature in a header: it must only ever reach a public host."""

    def test_submit_refuses_a_private_host(self) -> None:
        import os
        from walletd import pay402

        saved = os.environ.pop("AGENT_WALLET_ALLOW_PRIVATE_402", None)
        try:
            for url in ("http://127.0.0.1:9119/api/x", "http://[::1]/x", "http://169.254.169.254/latest"):
                with self.assertRaises(pay402.QuoteError) as caught:
                    pay402.submit(url, {"payload": "secret"})
                self.assertEqual(caught.exception.code, "bad_url", url)
        finally:
            if saved is not None:
                os.environ["AGENT_WALLET_ALLOW_PRIVATE_402"] = saved

    def test_submit_refuses_a_non_http_scheme(self) -> None:
        import os
        from walletd import pay402

        saved = os.environ.pop("AGENT_WALLET_ALLOW_PRIVATE_402", None)
        try:
            with self.assertRaises(pay402.QuoteError) as caught:
                pay402.submit("file:///etc/passwd", {})
            self.assertEqual(caught.exception.code, "bad_url")
        finally:
            if saved is not None:
                os.environ["AGENT_WALLET_ALLOW_PRIVATE_402"] = saved


class X402SettlementTests(unittest.TestCase):
    """The whole purchase story: quote → pending → approve → signed authorisation → seller accepts."""

    def setUp(self) -> None:
        _Handler.payload = {"accepts": [_accepts()]}
        _Handler.as_header = False
        _Handler.seen_payment = None
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/item/42"

        self.tmp = tempfile.TemporaryDirectory()
        self.state = State(Path(self.tmp.name))
        self.state.initialize(PASS)
        self.state.unlock(PASS)
        from walletd import config as config_mod

        self.state.settings.chains_enabled = ["base-sepolia"]
        self.state.settings.assets_enabled = ["ETH", "USDC"]
        config_mod.save(self.state.config_path, self.state.settings, self.state.master)

        # a working EVM adapter with a funded balance and a stub node
        from walletd.chains.evm import EvmAdapter

        adapter = EvmAdapter("base-sepolia", "11" * 32)
        adapter._rpc = lambda method, params: {
            "eth_chainId": hex(84532), "eth_blockNumber": hex(1), "eth_gasPrice": hex(10 ** 9),
            "eth_getTransactionCount": hex(3), "eth_getBalance": hex(10 ** 18),
            "eth_call": hex(100 * 10 ** 6),
        }.get(method, "0x0")
        self.state.adapters["base-sepolia"] = adapter

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def test_approving_a_402_purchase_presents_the_authorisation(self) -> None:
        priced = self.state.pay_url({"url": self.url, "why": "buy a widget"})
        self.assertEqual(priced["decision"]["code"], "needs_operator")
        request_id = priced["request"]["request_id"]
        approval_id = priced["approval_id"]

        result = self.state.approve({"request_id": request_id, "approval_id": approval_id,
                                     "confirm_amount": priced["request"]["amount"],
                                     "passphrase": PASS})
        self.assertEqual(result["settlement"], "x402")
        self.assertTrue(result["x402_response"]["settled"], result["x402_response"])
        self.assertIsNotNone(_Handler.seen_payment, "the seller never received an X-PAYMENT header")

        # the header is the base64 envelope carrying our signature and the quoted amount
        import base64 as b64
        import json as js

        envelope = js.loads(b64.b64decode(_Handler.seen_payment))
        self.assertEqual(envelope["scheme"], "exact")
        self.assertEqual(envelope["network"], "base-sepolia")
        self.assertEqual(envelope["payload"]["authorization"]["value"], "10000")
        self.assertTrue(envelope["payload"]["signature"])

        receipt = self.state.ledger.last()
        self.assertEqual(receipt["decision"], "approved")
        self.assertEqual(receipt["details"]["x402_response"]["status"], 200)

    def test_the_authorisation_is_signed_by_this_wallet(self) -> None:
        from eth_account import Account
        from eth_account.messages import encode_typed_data

        from walletd.chains.evm import CHAINS

        priced = self.state.pay_url({"url": self.url})
        plan = self.state.plans[priced["request"]["request_id"]]
        payload = plan.raw["x402"]["payload"]
        authorization = payload["authorization"]
        typed = {
            "types": {
                "EIP712Domain": [{"name": "name", "type": "string"}, {"name": "version", "type": "string"},
                                 {"name": "chainId", "type": "uint256"},
                                 {"name": "verifyingContract", "type": "address"}],
                "TransferWithAuthorization": [
                    {"name": "from", "type": "address"}, {"name": "to", "type": "address"},
                    {"name": "value", "type": "uint256"}, {"name": "validAfter", "type": "uint256"},
                    {"name": "validBefore", "type": "uint256"}, {"name": "nonce", "type": "bytes32"}],
            },
            "primaryType": "TransferWithAuthorization",
            "domain": {"name": CHAINS["base-sepolia"]["tokens_name"]["USDC"], "version": "2",
                       "chainId": 84532,
                       "verifyingContract": CHAINS["base-sepolia"]["tokens"]["USDC"][0]},
            "message": {"from": authorization["from"], "to": authorization["to"],
                        "value": int(authorization["value"]),
                        "validAfter": int(authorization["validAfter"]),
                        "validBefore": int(authorization["validBefore"]),
                        "nonce": bytes.fromhex(authorization["nonce"][2:])},
        }
        recovered = Account.recover_message(encode_typed_data(full_message=typed),
                                            signature=payload["signature"])
        self.assertEqual(recovered.lower(), self.state.adapters["base-sepolia"].address().lower())

    def test_a_seller_that_refuses_is_reported_not_hidden(self) -> None:
        class Refusing(_Handler):
            def do_GET(self) -> None:  # noqa: N802
                if self.headers.get("X-PAYMENT"):
                    body = b'{"error":"payment rejected"}'
                    self.send_response(402)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                super().do_GET()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Refusing)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/item/42"
            priced = self.state.pay_url({"url": url})
            result = self.state.approve({"request_id": priced["request"]["request_id"],
                                         "approval_id": priced["approval_id"],
                                         "confirm_amount": priced["request"]["amount"],
                                         "passphrase": PASS})
            self.assertFalse(result["x402_response"]["settled"])
            self.assertEqual(result["x402_response"]["status"], 402)
            self.assertIn("rejected", result["x402_response"]["body"])
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
