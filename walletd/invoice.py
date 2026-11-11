"""Read a checkout page and find the one thing a wallet can pay.

The product is not a directory of shops — it is "buy this": the agent finds the checkout, the wallet
pays the invoice, and the *card* tells the human where that address came from. So this module does the
only honest thing an automated wallet can do with a web page: fetch it (SSRF-guarded, no cross-host
redirects, bounded size), and look for a payment target it can prove is there — a `monero:`/`bitcoin:`/
`ripple:`/`ethereum:` URI, or a bare address of a known shape.

It never guesses. No target → `no_target`. Two different addresses of the same coin → `ambiguous_target`
and the human has to say which. An address the agent invented is exactly the attack the PIN card exists
to stop, so the wallet only ever reports what the page said.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

from . import pay402

MAX_BYTES = 512 * 1024
MAX_TARGETS = 8

#: Shapes we can recognise with confidence. Anything looser would match prose and invoices for other
#: coins, and a false positive here is a payment to the wrong address.
PATTERNS = {
    "XMR": re.compile(r"\b([48][0-9AB][1-9A-HJ-NP-Za-km-z]{93})\b"),
    "BTC": re.compile(r"\b(bc1[ac-hj-np-z02-9]{11,71}|tb1[ac-hj-np-z02-9]{11,71})\b"),
    "XRP": re.compile(r"\b(r[1-9A-HJ-NP-Za-km-z]{24,34})\b"),
    "USDC": re.compile(r"\b(0x[0-9a-fA-F]{40})\b"),
}
SCHEMES = {
    "monero": "XMR",
    "bitcoin": "BTC",
    "ripple": "XRP",
    "xrp": "XRP",
    "ethereum": "USDC",
}
AMOUNT_PARAMS = ("tx_amount", "amount", "value")

#: `monero:49…?tx_amount=0.01` inside an href, or any scheme URI in text. The body may contain
#: `@`, `-`, `/` and `?` (EIP-681), so it is captured whole and parsed per scheme.
URI_RE = re.compile(r"(monero|bitcoin|ripple|xrp|ethereum):([^\s\"'<>]+)", re.I)

#: EIP-681: `ethereum:[pay-]target[@chain][/function][?params]`. For a token transfer the *recipient*
#: is `?address=`, and the target is the token contract — paying the contract burns the money, which
#: is exactly what taking "the first thing after the colon" did.
ETH_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def parse_ethereum(body: str) -> tuple:
    """(payee, amount_or_None) from an EIP-681 body, or (None, None) when it names no recipient."""
    target, _, query = body.partition("?")
    params = urllib.parse.parse_qs(query)
    if target.startswith("pay-"):
        target = target[4:]
    target = target.split("@")[0].split("/")[0]
    recipient = (params.get("address") or [""])[0].strip()
    if recipient and ETH_ADDRESS_RE.match(recipient):
        # A token transfer: the payee is the recipient; the target above is only the contract.
        # `uint256=` is in the *token's* base units and the page does not say which token decimals —
        # guessing would pay 1e-12 of what the shop asked, so the amount is left to the human/agent.
        return recipient, None
    if ETH_ADDRESS_RE.match(target):
        # `value=` is wei of the chain's NATIVE coin. Every `ethereum:` target is filed as USDC (the
        # one EVM asset this module offers), so repeating that figure would turn "1 ETH" into
        # "1 USDC" on the card. The address is real; the amount is the human's to state.
        return target, None
    return None, None


def _atomic(raw: str) -> Optional[Decimal]:
    """A wei/base-unit figure. Reported in whole units only when the shift is unambiguous — an unknown
    token's decimals are not guessed."""
    if not raw or not raw.isdigit():
        return None
    try:
        return Decimal(raw) / Decimal(10 ** 18)      # native value is always 18 decimals
    except (InvalidOperation, ArithmeticError):
        return None
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
HREF_RE = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.I)


@dataclass
class Target:
    asset: str
    payee: str
    amount: Optional[Decimal] = None
    source: str = "text"            # link | text
    scheme: str = ""
    context: str = ""               # the surrounding characters, for the card
    #: The chain this address actually belongs to (monero-mainnet / -stagenet / -testnet). A shop
    #: testing on stagenet used to have its invoice dropped because every XMR target was validated
    #: as mainnet.
    kind: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"asset": self.asset, "payee": self.payee, "kind": self.kind,
                "amount": None if self.amount is None else format(self.amount, "f"),
                "source": self.source, "scheme": self.scheme, "context": self.context[:160]}


@dataclass
class Invoice:
    url: str
    title: str
    targets: List[Target] = field(default_factory=list)
    asset_hint: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"url": self.url, "title": self.title,
                "targets": [t.to_dict() for t in self.targets], "asset_hint": self.asset_hint}


def _text(html: str) -> str:
    """The page with tags stripped, so a bare address in prose is still found (and tag soup is not)."""
    return re.sub(r"<[^>]+>", " ", html)


def _amount(query: str) -> Optional[Decimal]:
    params = urllib.parse.parse_qs(query or "")
    for name in AMOUNT_PARAMS:
        raw = (params.get(name) or [""])[0].strip()
        if not raw:
            continue
        try:
            value = Decimal(raw)
        except (InvalidOperation, ValueError):
            continue
        if value > 0:
            return value
    return None


def extract(html: str, url: str = "") -> Invoice:
    """Find payment targets in a page: URI links first, then URIs in text, then bare addresses."""
    title = ""
    match = TITLE_RE.search(html)
    if match:
        title = re.sub(r"\s+", " ", match.group(1)).strip()[:120]

    targets: List[Target] = []
    seen: set[tuple[str, str]] = set()

    def add(asset: str, payee: str, amount: Optional[Decimal], source: str, scheme: str, context: str) -> None:
        key = (asset, payee)
        if key in seen or len(targets) >= MAX_TARGETS:
            return
        # Real money: a real chain's address must pass its own checksum before a human is even offered
        # it. A page with a one-character typo is the common case, and it burns the payment.
        from . import addresses as _addresses
        if asset == "XMR":
            for network in ("monero-mainnet", "monero-stagenet", "monero-testnet"):
                if _addresses.xmr_valid(payee, network):
                    targets_kind = network
                    break
            else:
                return                      # no network's checksum accepts it: not an address
        else:
            targets_kind = ""
        if asset == "BTC" and not _addresses.bech32_valid(payee):
            return
        if asset == "XRP":
            # the shape alone ("r" + 24..34 base58 characters) also matches ordinary words and
            # identifiers; without the checksum a stray match made a one-address page "ambiguous"
            from .chains.xrp import is_address as _xrp_is_address
            if not _xrp_is_address(payee):
                return
        seen.add(key)
        targets.append(Target(asset=asset, payee=payee, amount=amount, source=source,
                              scheme=scheme, context=context, kind=targets_kind))

    # 1. hrefs: an explicit payment link is the strongest evidence a page offers
    for href in HREF_RE.findall(html):
        candidate = href.strip()
        uri = URI_RE.match(candidate)
        if not uri:
            continue
        scheme, body = uri.group(1).lower(), uri.group(2)
        asset = SCHEMES[scheme]
        if scheme.startswith("ethereum"):
            address, value = parse_ethereum(body)
            if address:
                add(asset, address, value, "link", scheme, candidate)
            continue
        address, _, query = body.partition("?")
        add(asset, address, _amount(query), "link", scheme, candidate)

    # 2. the same URIs in the page text (rendered as text, or a plain-text invoice)
    for scheme, body in URI_RE.findall(html):
        scheme = scheme.lower()
        asset = SCHEMES.get(scheme)
        if asset is None:
            continue
        if scheme.startswith("ethereum"):
            address, value = parse_ethereum(body)
            if address:
                add(asset, address, value, "text", scheme, f"ethereum:{body}"[:140])
            continue
        address, _, query = body.partition("?")
        add(asset, address, _amount(query), "text", scheme, f"{scheme}:{address}")

    # 3. a bare address, if the page names one without a scheme
    body = _text(html)
    for asset, pattern in PATTERNS.items():
        for found in pattern.findall(body):
            start = max(0, body.find(found) - 60)
            add(asset, found, None, "text", "", body[start:start + 140].strip())

    return Invoice(url=url, title=title, targets=targets)


def fetch(url: str, timeout: float = 12.0, allow_loopback: bool = False) -> Invoice:
    """GET a checkout page on a pinned IP and extract its payment targets."""
    status, headers, raw = pay402._get_pinned(
        url, max(1.0, min(float(timeout), 15.0)), allow_loopback,
        accept="text/html,application/xhtml+xml", max_bytes=MAX_BYTES)
    kind = (headers.get("Content-Type") or "").lower()
    if status >= 400 and not raw:
        raise pay402.QuoteError("unreachable", f"{url} answered HTTP {status}")
    if kind and "html" not in kind and "text" not in kind:
        raise pay402.QuoteError("not_a_page", f"{url} is {kind or 'not a page'}, not a checkout")
    page = extract(raw.decode("utf-8", "replace"), url)
    if not page.targets:
        raise pay402.QuoteError(
            "no_target",
            f"no payable address on {url}"
            + (f" ({page.title})" if page.title else "")
            + " — nothing was filed. Ask the operator for the address, or use a checkout that "
              "states one.")
    return page


def one(invoice: Invoice) -> Target:
    """The single target, or a refusal. Two addresses of one coin are a decision, not a default."""
    if not invoice.targets:
        raise pay402.QuoteError("no_target", "nothing payable on that page")
    if len(invoice.targets) == 1:
        return invoice.targets[0]
    linked = [t for t in invoice.targets if t.source == "link"]
    others = [t for t in invoice.targets if t.source != "link"]
    # One link wins only when every other candidate is a *different asset* (a shop offering XMR and
    # BTC has one link each, and both are legitimate choices). Two addresses of the same coin — one in
    # a link, one in prose — is an ambiguous page, and a hostile page must not decide which one a
    # signature goes to by marking its favourite as a link.
    if len(linked) == 1 and all(t.asset != linked[0].asset for t in others):
        return linked[0]
    listed = "; ".join(f"{t.asset} {t.payee[:12]}…" for t in invoice.targets[:4])
    assets = {t.asset for t in invoice.targets}
    raise pay402.QuoteError(
        "ambiguous_target",
        f"{invoice.url} shows {len(invoice.targets)} payment addresses ({listed})"
        + (f" for {', '.join(sorted(assets))}" if len(assets) > 1 else "")
        + " — ask the operator which one to pay")
