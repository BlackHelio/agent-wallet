"""Read a payment demand off the wire instead of taking the agent's word for it.

The dangerous shape of "go buy me an X" is that the model reports a price and an address. A prompt
injection, a hallucination, or a helpful-looking page can make those numbers anything. So this
fetches the URL, reads the *server's* 402 demand, and turns exactly that into a spend request the
human sees: amount, asset, payee and the resource all come from the response, and a digest of the
challenge is bound into the request so an approval cannot be retargeted afterwards.

Supported today:
* x402 (Coinbase's HTTP 402 scheme) — `PAYMENT-REQUIRED` header (base64 JSON) or a JSON body with
  an `accepts` array.
* L402 / Lightning — recognised and refused with a clear code, because there is no Lightning
  adapter yet. Refusing loudly beats paying an invoice the wallet cannot settle.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import os
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional

# Atomic-unit decimals per asset, and the assets this wallet can actually settle.
ASSET_DECIMALS = {"USDC": 6, "USDT": 6, "DAI": 18, "ETH": 18, "POL": 18, "BTC": 8, "XMR": 12}
SETTLEABLE = {"XMR", "BTC", "ETH", "USDC"}

# x402 network ids -> the chain kinds this plugin uses.
NETWORKS = {
    "base": "base-mainnet",
    "base-mainnet": "base-mainnet",
    "base-sepolia": "base-sepolia",
    "ethereum": "base-mainnet",
    "polygon": "polygon-mainnet",
    "arbitrum": "arbitrum-mainnet",
}


class QuoteError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Quote:
    url: str
    scheme: str
    kind: str
    asset: str
    amount: Decimal
    payee: str
    resource: str = ""
    description: str = ""
    network: str = ""
    digest: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "url": self.url, "scheme": self.scheme, "kind": self.kind, "asset": self.asset,
            "amount": str(self.amount), "payee": self.payee, "resource": self.resource,
            "description": self.description, "network": self.network,
            "challenge_sha256": self.digest,
        }


def _digest(payload: Any) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _atomic_to_decimal(value: Any, asset: str) -> Decimal:
    """x402 quotes atomic units as strings ("10000" = 0.01 USDC)."""
    try:
        atomic = Decimal(str(value))
    except (ArithmeticError, ValueError) as exc:
        raise QuoteError("bad_quote", f"amount is not a number: {value!r}") from exc
    scale = ASSET_DECIMALS.get(asset.upper())
    if scale is None:
        # Guessing 18 would misprice a BTC or XMR quote by ten orders of magnitude.
        raise QuoteError("unknown_asset", f"no decimal scale known for {asset!r}")
    return atomic / (Decimal(10) ** scale)


def _accepts_from(headers: Any, body: bytes) -> List[Dict[str, Any]]:
    header = headers.get("PAYMENT-REQUIRED") or headers.get("X-Payment-Required") or ""
    if header:
        try:
            decoded = base64.b64decode(header + "=" * (-len(header) % 4)).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise QuoteError("bad_challenge", "PAYMENT-REQUIRED is not base64 JSON") from exc
        try:
            payload = json.loads(decoded)
        except json.JSONDecodeError as exc:
            raise QuoteError("bad_challenge", "PAYMENT-REQUIRED is not JSON") from exc
        return _as_accepts(payload)

    if body:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return []
        return _as_accepts(payload)
    return []


def _as_accepts(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, dict):
        if isinstance(payload.get("accepts"), list):
            return [row for row in payload["accepts"] if isinstance(row, dict)]
        if payload.get("scheme") or payload.get("payTo"):
            return [payload]
        if isinstance(payload.get("x402"), dict):
            return _as_accepts(payload["x402"])
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    return []


def _asset_of(accept: Dict[str, Any]) -> str:
    symbol = str(accept.get("asset") or accept.get("symbol") or accept.get("currency") or "")
    symbol = symbol.upper()
    if symbol in SETTLEABLE:
        return symbol
    # x402 puts the token contract in `asset` and the symbol in `extra.name`; a bare address is USDC
    # on Base in every deployment seen so far, but we refuse to guess when a name is present.
    name = str((accept.get("extra") or {}).get("name") or "").upper()
    if name in SETTLEABLE:
        return name
    if symbol.startswith("0X"):
        raise QuoteError("unknown_asset", "the quote pays a token contract we cannot price")
    raise QuoteError("unknown_asset", f"unsupported asset {symbol or '(none)'}")


def parse(headers: Any, body: bytes, url: str) -> Quote:
    """Turn a 402 response into a quote, or raise with a reason the agent can report."""
    accepts = _accepts_from(headers, body)
    if not accepts:
        challenge = (headers.get("WWW-Authenticate") or "") if headers else ""
        if challenge.lower().startswith("l402") or "invoice=" in challenge.lower():
            raise QuoteError("unsupported_scheme",
                             "L402/Lightning invoice: no Lightning adapter in this wallet yet")
        raise QuoteError("no_quote", "the response carries no payment terms this wallet understands")

    errors: List[str] = []
    for accept in accepts:
        scheme = str(accept.get("scheme") or "exact").lower()
        network = str(accept.get("network") or "").lower()
        kind = NETWORKS.get(network)
        if not kind:
            errors.append(f"network {network or '(none)'} is not supported")
            continue
        try:
            asset = _asset_of(accept)
        except QuoteError as exc:
            errors.append(exc.message)
            continue
        amount_raw = (accept.get("maxAmountRequired") or accept.get("amount")
                      or accept.get("maxAmount") or accept.get("price"))
        if amount_raw in (None, ""):
            errors.append("the quote has no amount")
            continue
        payee = str(accept.get("payTo") or accept.get("payee") or accept.get("address") or "").strip()
        if not payee:
            errors.append("the quote has no payee")
            continue
        return Quote(
            url=url, scheme=scheme, kind=kind, asset=asset,
            amount=_atomic_to_decimal(amount_raw, asset), payee=payee,
            resource=str(accept.get("resource") or accept.get("resourceUrl") or url)[:400],
            description=str(accept.get("description") or accept.get("extra", {}).get("description") or "")[:200],
            network=network, digest=_digest(accept), raw=accept)
    raise QuoteError("unsupported_quote", "; ".join(sorted(set(errors)))[:300] or "no usable quote")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A 402 quote is read from the exact URL the human approved, never from wherever it bounces."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102 - urllib hook
        return None


def _public_host(url: str, allow_loopback: bool = False) -> str:
    """Resolve the host and refuse anything that is not a public address.

    This is an agent-supplied URL, so without this a prompt injection can ask the wallet to fetch
    http://127.0.0.1:9119/ (the dashboard), 169.254.169.254 (cloud metadata) or any LAN service, and
    the response body comes back in the QuoteError. Cheap check, real hole.
    """
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or ""
    if not host:
        raise QuoteError("bad_url", "the url has no host")
    if parsed.scheme not in ("http", "https"):
        raise QuoteError("bad_url", "only http(s) URLs can be quoted")
    if allow_loopback and (parsed.hostname in ("127.0.0.1", "localhost", "::1")):
        # the demo's seller runs on loopback. Only the operator CLI passes this, and only for
        # 127.0.0.1 - never for a LAN address, and never for the agent's tool.
        return host
    if os.environ.get("AGENT_WALLET_ALLOW_PRIVATE_402") == "1":
        # test-only: the unit tests serve 402 demands from 127.0.0.1, and no production code path
        # sets this. Anything else that sets it is choosing to let the agent reach the LAN.
        return host
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise QuoteError("unreachable", f"{host} does not resolve: {exc}") from exc
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if address.is_private or address.is_loopback or address.is_link_local \
                or address.is_reserved or address.is_multicast or address.is_unspecified:
            raise QuoteError("bad_url",
                             f"{host} resolves to {address}, which is not a public address; "
                             f"a 402 quote may only come from the public internet")
    return host


def fetch(url: str, timeout: float = 15.0, allow_loopback: bool = False) -> Quote:
    """GET the URL and read its 402 demand. Never follows a redirect to another host."""
    _public_host(url, allow_loopback=allow_loopback)
    timeout = max(1.0, min(float(timeout), 15.0))
    request = urllib.request.Request(url, method="GET",
                                     headers={"Accept": "application/json",
                                              "User-Agent": "agent-wallet/402"})
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            if response.status != 402:
                raise QuoteError("not_payment",
                                 f"{url} answered HTTP {response.status}; nothing to pay")
            return parse(response.headers, response.read(64 * 1024), url)
    except urllib.error.HTTPError as exc:
        with exc:      # an unread/unclosed response leaks a socket per call
            if exc.code != 402:
                raise QuoteError("not_payment", f"{url} answered HTTP {exc.code}; nothing to pay") from exc
            return parse(exc.headers, exc.read(64 * 1024), url)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise QuoteError("unreachable", f"{url}: {exc}") from exc


def submit(url: str, payload: Dict[str, Any], timeout: float = 30.0) -> Dict[str, Any]:
    """Hand the signed authorisation to the resource, which settles it through its facilitator.

    x402 carries the payment in an `X-PAYMENT` header: base64 of the JSON envelope. A 200 means the
    seller accepted it (and, in a real deployment, already moved the money); anything else is
    reported as-is with the body, because a refused authorisation is information the operator needs.
    """
    # The URL is the one stored on the request, which an agent can influence. Without this check a
    # prompt-injected agent files a 402 request pointing at http://127.0.0.1:9119/..., the human
    # approves what looks like a purchase, and the daemon hands a signed EIP-3009 authorisation to
    # localhost and returns the response body to the agent.
    _public_host(url)
    timeout = max(1.0, min(float(timeout), 30.0))
    envelope = base64.b64encode(json.dumps(payload).encode("utf-8")).decode()
    request = urllib.request.Request(
        url, method="GET",
        headers={"X-PAYMENT": envelope, "Accept": "application/json",
                 "User-Agent": "agent-wallet/402"})
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read(4000)
            return {"status": response.status, "settled": 200 <= response.status < 300,
                    "body": body.decode("utf-8", "replace")[:1000]}
    except urllib.error.HTTPError as exc:
        with exc:
            body = exc.read(2000).decode("utf-8", "replace")
            return {"status": exc.code, "settled": False, "body": body[:1000]}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"status": 0, "settled": False, "error": str(exc)[:200]}
