"""A small, curated playbook of shops that take crypto, with the steps to buy from each.

This is deliberately *data*, not policy: the agent reads it to know where to go and what to do, and
the merchant page is still the authority. Every entry carries `verify`: crypto acceptance changes
without notice, and Monero in particular is accepted by far fewer shops than BTC — the tool says so
on every answer.
"""

from __future__ import annotations

from typing import Any, Dict, List

MERCHANTS: List[Dict[str, Any]] = [
    {
        "name": "Mullvad VPN",
        "url": "https://mullvad.net",
        "category": "privacy",
        "accepts": ["XMR", "BTC", "cash"],
        "needs_account": False,
        "price_hint": "€5/month flat, no account number needed",
        "steps": [
            "Open mullvad.net and pick a plan length",
            "Choose 'Cryptocurrency' at checkout — it shows an address and a 30-minute timer",
            "Pay from the wallet (XMR is accepted directly here)",
            "Copy the generated account number; the timer covers slow confirmations",
        ],
        "notes": "One of the few mainstream services that takes Monero directly.",
    },
    {
        "name": "Namecheap",
        "url": "https://www.namecheap.com",
        "category": "domains",
        "accepts": ["BTC", "ETH", "USDT"],
        "needs_account": True,
        "price_hint": "domains from ~$9/year",
        "steps": [
            "Create an account and put the domain in the cart",
            "At checkout choose the crypto option (BitPay)",
            "Pay the invoice; the domain is registered after one confirmation",
        ],
        "notes": "No Monero. Keep the account email reachable — renewal reminders live there.",
    },
    {
        "name": "Bitrefill",
        "url": "https://www.bitrefill.com",
        "category": "gift-cards",
        "accepts": ["BTC", "ETH", "LTC", "USDT"],
        "needs_account": False,
        "price_hint": "gift cards from ~$5",
        "steps": [
            "Search the brand, choose the denomination",
            "Select the coin, pay the invoice",
            "The code appears on the order page and is emailed",
        ],
        "notes": "Does not take Monero. Add a note to the receipt with the redemption code.",
    },
    {
        "name": "Monero.com (Cake Pay)",
        "url": "https://monero.com",
        "category": "gift-cards",
        "accepts": ["XMR"],
        "needs_account": False,
        "price_hint": "gift cards, XMR-native",
        "steps": [
            "Open monero.com in a browser and pick a gift card",
            "Pay the Monero address it shows, from this wallet",
            "Keep the order id — the card arrives in the same session",
        ],
        "notes": "The XMR-native option; fewer brands than Bitrefill.",
    },
    {
        "name": "Coinsbee",
        "url": "https://www.coinsbee.com",
        "category": "gift-cards",
        "accepts": ["BTC", "ETH", "LTC", "XMR"],
        "needs_account": False,
        "price_hint": "gift cards from ~$5",
        "steps": [
            "Pick a brand and denomination",
            "Select the coin at checkout — Monero appears on some products only",
            "Pay, then copy the code from the confirmation page",
        ],
        "notes": "Per-product coin support varies; check the coin list on the product page.",
    },
    {
        "name": "Travala",
        "url": "https://www.travala.com",
        "category": "travel",
        "accepts": ["BTC", "ETH", "USDT", "XMR"],
        "needs_account": True,
        "price_hint": "hotels and flights, crypto-native booking",
        "steps": [
            "Search a destination and dates",
            "At payment, choose the crypto option and the coin",
            "Pay the address shown; the booking confirms after the network confirms",
        ],
        "notes": "Crypto support is per-property; the payment page is the authority.",
    },
]

ADVICE = (
    "Monero is accepted by far fewer merchants than BTC/USDC — always confirm the coin on the "
    "merchant's own payment page before promising the user it will work. If a shop only takes BTC "
    "or USDC, say so and ask the user whether to switch off 'Monero only'."
)


def search(query: str = "", category: str = "") -> Dict[str, Any]:
    q = (query or "").strip().lower()
    cat = (category or "").strip().lower()
    rows = []
    for merchant in MERCHANTS:
        haystack = " ".join([merchant["name"], merchant["category"], " ".join(merchant["accepts"]),
                             merchant.get("price_hint", ""), merchant.get("notes", "")]).lower()
        if cat and merchant["category"].lower() != cat:
            continue
        if q and q not in haystack:
            continue
        rows.append(merchant)
    return {"merchants": rows, "count": len(rows), "advice": ADVICE,
            "note": "Crypto acceptance changes without notice — the merchant page is the authority."}
