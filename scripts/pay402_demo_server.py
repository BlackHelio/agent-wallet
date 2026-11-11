"""A throwaway HTTP 402 endpoint for the demo: it quotes a price, and settles when paid.

Run it, then `hermes wallet pay-url http://127.0.0.1:38091/sku/42`. It answers 402 with x402 terms
until it receives an `X-PAYMENT` header, at which point it says thank you and echoes what it got —
which is exactly what a real x402 seller does (the difference is that a real one verifies the
authorisation on-chain through a facilitator).
"""

from __future__ import annotations

import argparse
import base64
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PAY_TO = "0x1234567890abcdef1234567890abcdef12345678"
# What a real seller asks for: USDC on Base mainnet. Quoting a testnet we do not enable made the
# documented demo dead-end with `chain_not_enabled` before anything was shown.
ACCEPT = {
    "scheme": "exact",
    "network": "base-mainnet",
    "maxAmountRequired": "25000",          # atomic USDC -> 0.025
    "resource": "/sku/42",
    "description": "ACME Widget, 1 unit",
    "payTo": PAY_TO,
    "asset": "USDC",
    "extra": {"name": "USDC", "version": "2"},
}


class Handler(BaseHTTPRequestHandler):
    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        paid = self.headers.get("X-PAYMENT")
        if not paid:
            self._json(402, {"x402Version": 1, "accepts": [ACCEPT]})
            return
        try:
            envelope = json.loads(base64.b64decode(paid))
            authorization = envelope["payload"]["authorization"]
        except (ValueError, KeyError, TypeError):
            self._json(400, {"error": "unreadable X-PAYMENT envelope"})
            return
        self._json(200, {"ok": True, "item": "ACME Widget, 1 unit",
                         "paid": {"from": authorization["from"], "value": authorization["value"],
                                  "signature": envelope["payload"]["signature"][:24] + "…"}})

    def log_message(self, format, *args) -> None:  # noqa: A002
        return


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="throwaway x402 seller for the wallet demo")
    parser.add_argument("port", nargs="?", type=int, default=38091)
    parser.add_argument("--network", default=ACCEPT["network"],
                        help="x402 network id to quote (base-mainnet, polygon, ...)")
    parser.add_argument("--asset", default=ACCEPT["asset"], help="asset to quote")
    parser.add_argument("--price", default=ACCEPT["maxAmountRequired"],
                        help="price in atomic units of --asset")
    cli = parser.parse_args()
    ACCEPT.update(network=cli.network, asset=cli.asset, maxAmountRequired=cli.price,
                  extra={"name": cli.asset, **({"version": "2"} if cli.asset == "USDC" else {})})
    # bind BEFORE announcing: a client that trusts the banner otherwise races the listen() call and
    # gets "connection refused" on a seller that is in fact starting
    server = ThreadingHTTPServer(("127.0.0.1", cli.port), Handler)
    print(f"402 demo seller on http://127.0.0.1:{cli.port}/sku/42 "
          f"quoting {cli.price} atomic {cli.asset} on {cli.network}", flush=True)
    server.serve_forever()
