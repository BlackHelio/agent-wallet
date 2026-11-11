"""Fiat amounts: "send adrian 10$" has to become a coin amount, honestly.

The rule this module exists for: **the daemon quotes, the human sees both numbers, the coin amount is
what gets signed.** A price fetched from a third party decides how much coin "10 USD" means, so a bad
quote must never be silently trusted:

- a quote older than `MAX_AGE` is refreshed before it is used;
- a quote that moves more than `MAX_DEVIATION` against the last good one within a day is refused
  (`fx_suspect`) — that is the shape of a poisoned or broken feed, and accepting it would send a
  wildly different amount than the moment before;
- a quote that cannot be fetched refuses the request (`fx_unavailable`). The agent is told to ask the
  human for a coin amount; it is never allowed to invent the number.

Prices are cached in memory and on disk (`fx.json`, 0600) so a restart or a burst of requests does not
hammer the source, and so the last good rate is still known when a new one looks wrong.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

#: The source is a constant endpoint: no caller-supplied URL, so nothing here can be pointed at an
#: internal service. CoinGecko's public API needs no key.
SOURCE = "coingecko"
ENDPOINT = "https://api.coingecko.com/api/v3/simple/price"
COIN_IDS = {
    "XMR": "monero",
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "POL": "matic-network",
    "USDC": "usd-coin",
    "XRP": "ripple",
}
CURRENCIES = {"USD": "usd", "EUR": "eur", "GBP": "gbp"}

MAX_AGE = 120                 # seconds a quote may be reused without asking again
#: How old a cached quote may be before it stops being a fallback. A price API that rate-limits is a
#: normal Tuesday; refusing every fiat payment because of a 429 is worse than pricing off a quote the
#: card dates, as long as the coin amount was already fixed when the request was filed.
STALE_MAX_AGE = 6 * 3600
MAX_DEVIATION = Decimal("0.30")   # refuse a new quote this far from the last good one…
DEVIATION_WINDOW = 24 * 3600      # …if the last good one is recent enough to mean anything
CACHE_FILE = "fx.json"

#: How many decimals a coin amount is rounded to when it is derived from a fiat figure. Six for the
#: three main coins (all their chains have finer base units); two for a stablecoin.
DECIMALS = {"XMR": 6, "BTC": 6, "XRP": 6, "ETH": 6, "POL": 6, "USDC": 2}


class FxError(Exception):
    """A quote could not be produced, or could not be trusted."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def normalise_currency(value: str) -> str:
    code = str(value or "").strip().upper().lstrip("$")
    if code not in CURRENCIES:
        raise FxError("bad_currency", f"unsupported currency {value!r}; known: {', '.join(CURRENCIES)}")
    return code


def _now_epoch() -> float:
    return time.time()


def _load_cache(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_cache(path: Path, data: Dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_name(f".{path.name}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        pass          # a cache that cannot be written is not a reason to refuse a price


#: Kraken's public ticker as the second source: no key, no quota worth worrying about, and it quotes
#: the pairs this wallet actually holds.
KRAKEN_PAIRS = {"XMR": "XMRUSD", "BTC": "XXBTZUSD", "ETH": "XETHZUSD",
                "USDC": "USDCUSD", "POL": "MATICUSD", "XRP": "XXRPZUSD"}
KRAKEN_ENDPOINT = "https://api.kraken.com/0/public/Ticker"


def _fetch_kraken(asset: str, currency: str, timeout: float) -> Decimal:
    pair = KRAKEN_PAIRS.get(asset)
    if not pair or currency != "USD":
        raise FxError("fx_unsupported", f"no {asset}/{currency} pair at kraken")
    url = f"{KRAKEN_ENDPOINT}?pair={pair}"
    request = urllib.request.Request(url, headers={"User-Agent": "agent-wallet/0.4",
                                                  "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise FxError("fx_unavailable", f"kraken: {str(exc)[:100]}") from exc
    if payload.get("error"):
        raise FxError("fx_unavailable", f"kraken: {payload['error']}")
    result = payload.get("result") or {}
    row = next(iter(result.values()), {})
    try:
        return Decimal(str(row["c"][0]))       # "c" = last trade closed [price, lot volume]
    except (KeyError, IndexError, TypeError, InvalidOperation) as exc:
        raise FxError("fx_unavailable", "kraken returned no price") from exc


def _fetch_any(asset: str, coin_id: str, currency: str, timeout: float) -> tuple:
    """First working source wins; both failing is an outage, and the caller may still have a cache."""
    problems = []
    for name, call in (("coingecko", lambda: _fetch(coin_id, currency, timeout)),
                       ("kraken", lambda: _fetch_kraken(asset, currency, timeout))):
        try:
            return call(), name
        except FxError as exc:
            problems.append(f"{name}: {exc.message}")
    raise FxError("fx_unavailable", "; ".join(problems)[:220])


def _fetch(coin_id: str, currency: str, timeout: float) -> Decimal:
    url = f"{ENDPOINT}?ids={coin_id}&vs_currencies={CURRENCIES[currency]}"
    request = urllib.request.Request(url, headers={"User-Agent": "agent-wallet/0.4",
                                                   "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - every failure is the same fact to the caller
        raise FxError("fx_unavailable", f"could not reach {SOURCE}: {str(exc)[:120]}") from exc
    try:
        value = Decimal(str(payload[coin_id][CURRENCIES[currency]]))
    except (KeyError, TypeError, InvalidOperation) as exc:
        raise FxError("fx_unavailable", f"{SOURCE} did not quote {coin_id} in {currency}") from exc
    if value <= 0:
        raise FxError("fx_unavailable", f"{SOURCE} quoted a non-positive rate for {coin_id}")
    return value


def _valid_entry(entry: Any, asset: str, currency: str) -> bool:
    """A cache row has to look like a quote before it is used: a forged `rate` of 1e6 priced 10 USD as
    0.00001 XMR, and the card's rate line was the only thing that showed it."""
    if not isinstance(entry, dict) or not isinstance(entry.get("quote"), dict):
        return False
    quote_row = entry["quote"]
    if quote_row.get("asset") != asset or quote_row.get("currency") != currency:
        return False
    try:
        rate = Decimal(str(quote_row.get("rate")))
    except (InvalidOperation, ValueError):
        return False
    if rate <= 0 or rate > Decimal("100000000"):
        return False
    try:
        float(entry.get("at") or 0)
    except (TypeError, ValueError):
        return False
    return True


def _mac(payload: bytes, secret: Optional[bytes]) -> str:
    if not secret:
        return ""
    import hashlib as _hashlib
    import hmac as _hmac
    return _hmac.new(secret, payload, _hashlib.sha256).hexdigest()


def quote(asset: str, currency: str = "USD", home: Optional[Path] = None,
          timeout: float = 6.0, force: bool = False, secret: Optional[bytes] = None) -> Dict[str, Any]:
    """Price one coin in one currency, cached, deviation-checked.

    Returns `{asset, currency, rate, source, quoted_at, age_seconds, cached}`.
    Raises `FxError` when there is no trustworthy price.
    """
    asset = str(asset or "").upper()
    currency = normalise_currency(currency)
    if asset not in COIN_IDS:
        raise FxError("fx_unsupported", f"no fiat pricing for {asset}")
    if asset == "USDC":
        # A stablecoin *is* the unit: quoting it against USD through a third party would only add a
        # way to be wrong. Other currencies are not supported rather than invented.
        if currency != "USD":
            raise FxError("fx_unsupported", f"USDC is priced in USD only (asked for {currency})")
        return {"asset": asset, "currency": currency, "rate": "1", "source": "fixed",
                "quoted_at": _iso(_now_epoch()), "age_seconds": 0, "cached": True}

    cache_path = (Path(home) / CACHE_FILE) if home is not None else None
    cache = _load_cache(cache_path) if cache_path else {}
    key = f"{asset}:{currency}"
    entry = cache.get(key) or {}
    if secret and entry:
        # With a key, *every* row must carry a valid MAC. A row with no MAC is a forgery as much as a
        # row with the wrong one — that is the shape a hand-written cache takes.
        body = {k: v for k, v in entry.items() if k != "mac"}
        if not _hmac_compare(str(entry.get("mac") or ""),
                             _mac(json.dumps(body, sort_keys=True).encode(), secret)):
            entry = {}                      # not signed by this wallet: ignore it and refetch
    if not _valid_entry(entry, asset, currency):
        entry = {}
    age = _now_epoch() - float(entry.get("at", 0) or 0) if entry else float("inf")

    if entry and not force and age <= MAX_AGE:
        return {**entry["quote"], "age_seconds": int(age)}

    try:
        rate, source = _fetch_any(asset, COIN_IDS[asset], currency, timeout)
    except FxError as exc:
        # a rate-limited API must not stop a payment the human is looking at: fall back to the last
        # good quote, dated, as long as it is recent enough to be a price rather than a memory
        if entry and age <= STALE_MAX_AGE:
            return {**entry["quote"], "age_seconds": int(age), "stale": True,
                    "error": exc.message}
        raise

    previous = Decimal(str(entry["quote"]["rate"])) if entry.get("quote", {}).get("rate") else None
    previous_age = _now_epoch() - float(entry.get("at", 0) or 0) if previous is not None else None
    if previous is not None and previous_age is not None and previous_age <= DEVIATION_WINDOW:
        if abs(rate - previous) / previous > MAX_DEVIATION:
            # Refusing beats guessing: the request becomes "ask the human for a coin amount".
            raise FxError("fx_suspect",
                          f"{asset}/{currency} moved from {previous} to {rate} — refusing to price a "
                          f"payment off that; ask the operator for the amount in {asset}")

    result = {"asset": asset, "currency": currency, "rate": format(rate, "f"),
              "source": source, "quoted_at": _iso(_now_epoch()), "age_seconds": 0}
    if cache_path is not None:
        row = {"at": _now_epoch(), "quote": result}
        row["mac"] = _mac(json.dumps(row, sort_keys=True).encode(), secret)
        cache[key] = row
        _save_cache(cache_path, cache)
    return {**result, "cached": False}


def _hmac_compare(a: str, b: str) -> bool:
    import hmac as _hmac
    return bool(a) and bool(b) and _hmac.compare_digest(a, b)


def to_coin(fiat: Decimal, rate: Decimal, asset: str) -> Decimal:
    """Fiat → coin, rounded to the asset's decimals (never up: a payment must not exceed what was
    asked for)."""
    if not Decimal(fiat).is_finite() or not Decimal(rate).is_finite():
        raise FxError("bad_fiat", "a fiat amount must be a finite number")
    if rate <= 0:
        raise FxError("fx_unavailable", "rate must be positive")
    coin = (fiat / rate).quantize(Decimal(1).scaleb(-DECIMALS.get(asset.upper(), 6)),
                                 rounding="ROUND_DOWN")
    if coin <= 0:
        raise FxError("fx_too_small",
                      f"{fiat} at {rate} is below one base unit of {asset}; pay a larger amount")
    return coin


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


#: Every way a currency is written, longest first so "euros" is not read as "eur" + "os" (which is
#: what happened: the three-letter code was stripped out of the word and "10 os" was not a number).
_FIAT_WORDS = (
    ("dollars", "USD"), ("dollar", "USD"), ("bucks", "USD"), ("buck", "USD"),
    ("euros", "EUR"), ("euro", "EUR"), ("pounds", "GBP"), ("pound", "GBP"),
    ("usd", "USD"), ("eur", "EUR"), ("gbp", "GBP"),
)
_FIAT_SYMBOLS = {"$": "USD", "\u20ac": "EUR", "\u00a3": "GBP"}


def split_fiat(value: Any) -> Optional[Tuple[Decimal, str]]:
    """Read a fiat amount out of what a human or an agent typed.

    `10$`, `$10`, `10 usd`, `usd 10`, `10 euros`, `\u20ac10`, `10 dollars`. Returns None for anything that
    names no currency (a plain coin amount), and raises `bad_fiat` when it names one but the rest is
    not a positive, finite number — or names two.
    """
    if value is None:
        return None
    text = str(value).strip().lower().replace(",", "")
    if not text:
        return None
    found: list = []
    for symbol, code in _FIAT_SYMBOLS.items():
        if symbol in text:
            found.append(code)
            text = text.replace(symbol, " ")
    for word, code in _FIAT_WORDS:
        # whole words only: "usdc" is a coin, not dollars with a stray letter
        pattern = r"(?<![a-z])" + word + r"(?![a-z])"
        import re as _re
        if _re.search(pattern, text):
            found.append(code)
            text = _re.sub(pattern, " ", text)
    if not found:
        return None
    if len(set(found)) > 1:
        raise FxError("bad_fiat", f"{value!r} names more than one currency")
    text = text.strip()
    try:
        amount = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise FxError("bad_fiat", f"{value!r} is not a fiat amount") from exc
    if not amount.is_finite() or amount <= 0:
        raise FxError("bad_fiat", "a fiat amount must be a positive number")
    return amount, found[0]
