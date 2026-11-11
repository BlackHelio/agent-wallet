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
import http.client
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional

# A real x402 seller names the token by its contract address, not by a ticker: `asset` is the USDC
# contract and `extra.name` is a display string ("USD Coin"), so both have to be mapped before a quote
# can be priced. Verified against a live seller (tbs-digital weather, $0.001 on eip155:8453).
TOKEN_ADDRESSES = {
    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": "USDC",   # Base
    "0x036cbd53842c5426634e7929541ec2318f3dcf7e": "USDC",   # Base Sepolia
    "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359": "USDC",   # Polygon (native)
    "0x2791bca1f2de4661ed88a30c99a7a9449aa84174": "USDC",   # Polygon (bridged)
}
TOKEN_NAMES = {"USD COIN": "USDC", "USDCOIN": "USDC", "USDC": "USDC"}

# Atomic-unit decimals per asset, and the assets this wallet can actually settle.
ASSET_DECIMALS = {"USDC": 6, "USDT": 6, "DAI": 18, "ETH": 18, "POL": 18, "BTC": 8, "XMR": 12}
SETTLEABLE = {"XMR", "BTC", "ETH", "USDC"}

# x402 network ids -> the chain kinds this plugin uses. Real sellers speak CAIP-2 (`eip155:<chain id>`),
# not friendly names: a live seller answering `eip155:8453` was refused as "unsupported", so the whole
# "buy from a real x402 endpoint" story only worked against our own demo server.
NETWORKS = {
    "base": "base-mainnet",
    "base-mainnet": "base-mainnet",
    "base-sepolia": "base-sepolia",
    "polygon": "polygon-mainnet",
    "polygon-amoy": "polygon-amoy",
    # CAIP-2
    "eip155:8453": "base-mainnet",
    "eip155:84532": "base-sepolia",
    "eip155:137": "polygon-mainnet",
    "eip155:80002": "polygon-amoy",
    # Deliberately absent: Ethereum mainnet ("ethereum", eip155:1) and Arbitrum. They used to be
    # mapped onto Base "because it shares the EVM adapter" — which priced a quote for one network
    # and signed it for another: the seller is never paid where it is looking. A network without
    # an adapter is refused by name instead.
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


def _extra(accept: Dict[str, Any]) -> Dict[str, Any]:
    """The offer's `extra` block, or an empty one. This is a stranger's JSON: a string or a list
    where an object belongs used to raise AttributeError out of the parser."""
    extra = accept.get("extra")
    return extra if isinstance(extra, dict) else {}


def _asset_of(accept: Dict[str, Any]) -> str:
    symbol = str(accept.get("asset") or accept.get("symbol") or accept.get("currency") or "")
    if symbol.upper() in SETTLEABLE:
        return symbol.upper()
    # A token contract, which is what the x402 spec actually sends.
    known = TOKEN_ADDRESSES.get(symbol.lower())
    if known:
        return known
    name = str(_extra(accept).get("name") or "").upper()
    if TOKEN_NAMES.get(name) in SETTLEABLE:
        return TOKEN_NAMES[name]
    if symbol.startswith("0x") or symbol.startswith("0X"):
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
        if scheme != "exact":
            # "exact" is the one scheme this wallet can settle (an EIP-3009 authorisation for a fixed
            # amount). Any other used to fall through to a plain on-chain transfer, which pays the
            # address and satisfies nothing the seller's facilitator is waiting for.
            errors.append(f"payment scheme {scheme!r} is not supported (only 'exact')")
            continue
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
            description=str(accept.get("description") or _extra(accept).get("description") or "")[:200],
            network=network, digest=_digest(accept), raw=accept)
    raise QuoteError("unsupported_quote", "; ".join(sorted(set(errors)))[:300] or "no usable quote")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A 402 quote is read from the exact URL the human approved, never from wherever it bounces."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102 - urllib hook
        return None


def _is_public(address: Any) -> bool:
    """True only for an address on the public internet.

    `is_private` alone misses carrier-grade NAT (100.64/10) and other non-global ranges, and an
    IPv4-mapped IPv6 address (`::ffff:127.0.0.1`) is judged by the IPv4 address inside it.
    """
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    if (address.is_private or address.is_loopback or address.is_link_local or address.is_reserved
            or address.is_multicast or address.is_unspecified):
        return False
    return bool(address.is_global)


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
        address = ipaddress.ip_address(str(info[4][0]).split("%")[0])
        if not _is_public(address):
            raise QuoteError("bad_url",
                             f"{host} resolves to {address}, which is not a public address; "
                             f"a 402 quote may only come from the public internet")
    return host


def _resolve_pinned(url: str, allow_loopback: bool = False) -> tuple:
    """Resolve once, validate, and return (host, ip, port, scheme) to connect to.

    The old check resolved the name, then handed the *name* to urllib, which resolved it again — a
    DNS-rebinding attacker answers with a public IP for the check and 127.0.0.1 for the connect, so
    the guard passed and the socket landed on the dashboard. The fix is to pin the vetted IP and use
    it for the connection, with the original host only in the `Host:` header.
    """
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or ""
    if not host:
        raise QuoteError("bad_url", "the url has no host")
    if parsed.scheme not in ("http", "https"):
        raise QuoteError("bad_url", "only http(s) URLs can be quoted")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    loopback = host in ("127.0.0.1", "localhost", "::1")
    if allow_loopback and loopback:
        return host, "127.0.0.1", port, parsed.scheme
    if os.environ.get("AGENT_WALLET_ALLOW_PRIVATE_402") == "1":
        return host, host, port, parsed.scheme
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise QuoteError("unreachable", f"{host} does not resolve: {exc}") from exc
    for info in infos:
        raw = info[4][0]
        address = ipaddress.ip_address(raw.split("%")[0])
        if not _is_public(address):
            raise QuoteError("bad_url",
                             f"{host} resolves to {address}, which is not a public address; "
                             f"a page the wallet reads must be on the public internet")
        return host, raw, port, parsed.scheme
    raise QuoteError("unreachable", f"{host} did not resolve to any address")


def _get_pinned(url: str, timeout: float, allow_loopback: bool, accept: str,
                max_bytes: int = 64 * 1024, extra_headers: dict | None = None) -> tuple:
    """One GET on a pinned socket. Returns (status, headers, body). No redirects, no re-resolution."""
    host, ip, port, scheme = _resolve_pinned(url, allow_loopback=allow_loopback)
    parsed = urllib.parse.urlsplit(url)
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    deadline = max(1.0, min(float(timeout), 15.0))
    sock = socket.create_connection((ip, port), timeout=deadline)
    try:
        if scheme == "https":
            context = ssl.create_default_context()
            sock = context.wrap_socket(sock, server_hostname=host)
        conn = http.client.HTTPConnection(host, port, timeout=deadline)
        conn.sock = sock                       # the pinned socket: connect() is skipped
        default_port = 443 if scheme == "https" else 80
        host_header = (f"[{host}]" if ":" in host else host) + ("" if port == default_port else f":{port}")
        headers = {"Accept": accept, "User-Agent": "agent-wallet", "Host": host_header,
                   "Connection": "close"}
        headers.update(extra_headers or {})
        conn.request("GET", path, headers=headers)
        response = conn.getresponse()
        body = response.read(max_bytes)
        return response.status, response.headers, body
    except (OSError, http.client.HTTPException) as exc:
        raise QuoteError("unreachable", f"{url}: {exc}") from exc
    finally:
        try:
            sock.close()
        except OSError:
            pass


def fetch(url: str, timeout: float = 15.0, allow_loopback: bool = False) -> Quote:
    """GET the URL and read its 402 demand, on a pinned IP. Never follows a redirect."""
    status, headers, body = _get_pinned(url, timeout, allow_loopback,
                                        accept="application/json")
    if status != 402:
        raise QuoteError("not_payment", f"{url} answered HTTP {status}; nothing to pay")
    return parse(headers, body, url)


def submit(url: str, payload: Dict[str, Any], timeout: float = 30.0,
           allow_loopback: bool = False) -> Dict[str, Any]:
    """Hand the signed authorisation to the resource, which settles it through its facilitator.

    x402 carries the payment in an `X-PAYMENT` header: base64 of the JSON envelope. The URL is one an
    agent can influence, so this goes out on the same pinned socket as a quote — a signed
    authorisation handed to `127.0.0.1` because the name rebound is the whole attack.
    """
    envelope = base64.b64encode(json.dumps(payload).encode("utf-8")).decode()
    # The request records whether its quote was allowed from loopback (the bundled demo seller is the
    # only case); submitting to the same URL with the same allowance is what makes that demo settle.
    _resolve_pinned(url, allow_loopback=allow_loopback)
    try:
        status, _headers, body = _get_pinned(
            url, timeout, allow_loopback=allow_loopback, accept="application/json", max_bytes=4000,
            extra_headers={"X-PAYMENT": envelope})
    except QuoteError as exc:
        return {"status": 0, "settled": False, "error": exc.message[:200]}
    return {"status": status, "settled": 200 <= status < 300,
            "body": body.decode("utf-8", "replace")[:1000]}
