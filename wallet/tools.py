"""Agent-facing tools.

Every one of these is a *request* or a *read*. None of them can sign, approve, unlock or touch key
material — that is the point, and it is enforced on the other side of the socket.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict

from . import client, daemon

_MAX_TEXT = 400


def _json(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True)


def _error(exc: Exception) -> str:
    return _json({"ok": False, "error": type(exc).__name__, "message": str(exc)[:_MAX_TEXT]})


def _call(method: str, params: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Call the daemon, starting it once if it is not listening."""
    try:
        return client.call(method, params or {})
    except client.WalletUnavailable as exc:
        started = daemon.start()
        if not started.get("ok"):
            raise client.WalletUnavailable(f"{exc}; daemon start failed: {started.get('error') or started}") from exc
        return client.call(method, params or {})


def wallet_nodes(**_: Any) -> str:
    """Health of the predefined public nodes for each enabled chain."""
    try:
        return _json({"ok": True, **_call("nodes")})
    except Exception as exc:
        return _error(exc)


def wallet_merchants(query: str = "", category: str = "", **_: Any) -> str:
    """Shops that take crypto, with the exact steps to buy from each."""
    try:
        return _json({"ok": True, **_call("merchants", {"query": query, "category": category})})
    except Exception as exc:
        return _error(exc)


def wallet_contacts(**_: Any) -> str:
    """The address book: names with the chain addresses saved for each."""
    try:
        return _json({"ok": True, **_call("contacts_list")})
    except Exception as exc:
        return _error(exc)


def wallet_contact_save(name: str, kind: str, address: str, notes: str = "", **_: Any) -> str:
    """Save or update a contact (validated per chain, so a bad paste fails now, not at payment)."""
    try:
        return _json({"ok": True, **_call("contact_save", {
            "name": name, "addresses": {kind: address}, "notes": notes[:200]})})
    except Exception as exc:
        return _error(exc)


def wallet_sandboxes(**_: Any) -> str:
    """Capped sub-wallets the agent may spend from without per-payment approval."""
    try:
        return _json({"ok": True, **_call("sandboxes_list")})
    except Exception as exc:
        return _error(exc)


def wallet_balances(**_: Any) -> str:
    """Balances per chain and asset, with the address to receive on.

    Check this before quoting a price to the user: a payment the wallet cannot afford is refused at
    plan time, and on EVM a transfer without gas is refused before it is signed.
    """
    try:
        return _json({"ok": True, **_call("balances")})
    except Exception as exc:
        return _error(exc)


def wallet_chains(**_: Any) -> str:
    """Which chains are live, their height and rpc."""
    try:
        return _json({"ok": True, **_call("chain_status")})
    except Exception as exc:
        return _error(exc)


def wallet_status(**_: Any) -> str:
    try:
        status = _call("status")
        return _json({"ok": True, "wallet": status})
    except Exception as exc:
        return _error(exc)


def wallet_request_payment(kind: str, asset: str, amount: str, payee: str,
                           memo: str = "", why: str = "", sandbox_id: str = "",
                           idempotency_key: str = "", **_: Any) -> str:
    """File a spend request for a human to approve.

    Confirm with the user first. A saved contact or a previous payment is a lead to resolve, not
    permission to send: print the full address and amount back and wait for a yes.
    """
    try:
        params = {"kind": kind, "asset": asset, "amount": amount, "payee": payee,
                  "memo": memo[:200], "why": why[:_MAX_TEXT],
                  "profile": client.current_profile()}
        if sandbox_id:
            params["sandbox_id"] = sandbox_id
        if idempotency_key:
            params["idempotency_key"] = idempotency_key[:64]
        result = _call("request_tx", params)
        return _json({"ok": True, **result})
    except Exception as exc:
        return _error(exc)


def wallet_receive_address(label: str = "", kind: str = "", expected: str = "", **_: Any) -> str:
    """Get a fresh address for one incoming payment (a new subaddress on Monero).

    Use this whenever money is coming in for a specific order: reusing one address links every
    purchase together for anyone who sees two of them. Pass `expected` (the order total) so
    wallet_incoming can say waiting / partial / paid instead of only showing a balance.
    """
    try:
        params: Dict[str, Any] = {"label": label[:40]}
        if kind:
            params["kind"] = kind
        if expected:
            params["expected"] = expected
        return _json({"ok": True, **_call("receive_address", params)})
    except Exception as exc:
        return _error(exc)


def wallet_incoming(**_: Any) -> str:
    """Incoming orders: the subaddress opened for each one, and whether it has been paid.

    `receive_address` only hands out addresses; this is the other half — what has actually landed on
    each of them, and whether it covers what the order asked for.
    """
    try:
        return _json({"ok": True, **_call("incoming")})
    except Exception as exc:
        return _error(exc)


def wallet_cancel(request_id: str, reason: str = "", **_: Any) -> str:
    """Withdraw a pending request you filed, before a human approves it.

    Only your own pending requests can be cancelled; an approved or denied one is history.
    """
    try:
        return _json({"ok": True, **_call("cancel_request",
                                          {"request_id": request_id, "reason": reason[:200]})})
    except Exception as exc:
        return _error(exc)


def wallet_spend(since: str = "", until: str = "", **_: Any) -> str:
    """What was actually spent, per asset and per payee, inside a window.

    `since`/`until` take `30d`, `12h`, `2w` or an ISO timestamp. This reads the same approved
    receipts the caps are enforced against.
    """
    try:
        return _json({"ok": True, **_call("spend_totals", {"since": since, "until": until})})
    except Exception as exc:
        return _error(exc)


def wallet_verify_payment(txid: str, tx_key: str, address: str, kind: str = "", **_: Any) -> str:
    """Prove a payment arrived, using the tx key the payer hands you (Monero only).

    Amounts and parties are hidden on Monero; the tx key is what makes a payment checkable by the
    recipient. Never mark an order paid without this.
    """
    try:
        params: Dict[str, Any] = {"txid": txid, "tx_key": tx_key, "address": address}
        if kind:
            params["kind"] = kind
        return _json({"ok": True, **_call("verify_payment", params)})
    except Exception as exc:
        return _error(exc)


def wallet_pay_url(url: str, why: str = "", sandbox_id: str = "",
                   idempotency_key: str = "", **_: Any) -> str:
    """Fetch `url` and pay it only if the server answers HTTP 402 with x402 terms.

    The price, asset, chain and payee come from that response — you cannot set them — so an injected
    or guessed number never reaches the human as an approval. If the page answers 200 and shows a
    price in HTML, read it yourself and use wallet_request_payment instead: this tool pays 402s.
    """
    try:
        params = {"url": url, "why": why[:_MAX_TEXT], "profile": client.current_profile()}
        if sandbox_id:
            params["sandbox_id"] = sandbox_id
        if idempotency_key:
            params["idempotency_key"] = idempotency_key[:64]
        result = _call("pay_url", params)
        return _json({"ok": True, **result})
    except Exception as exc:
        return _error(exc)


def wallet_request_approval(request_id: str = "", message: str = "", **_: Any) -> str:
    """Ask the human to approve in the Wallet pane; the passphrase is typed there, never here.

    Use this instead of asking for a passphrase in chat: a secret typed into the conversation ends up
    in the transcript and in the model's context. The pane pops the prompt with the field focused and
    the approval lands as a receipt.
    """
    try:
        params: Dict[str, Any] = {"message": message[:200]}
        if request_id:
            params["request_id"] = request_id
        result = _call("request_operator_prompt", params)
        return _json({"ok": True, **result,
                      "tell_the_user": "Open the Wallet page in Hermes Desktop — the prompt is waiting "
                                       "there. Never type the passphrase into this chat."})
    except Exception as exc:
        return _error(exc)


def wallet_request_unlock(**_: Any) -> str:
    """Ask the human to type the passphrase in the Wallet pane. The agent never sees it."""
    try:
        status = _call("status")
    except Exception as exc:
        return _error(exc)
    if not status.get("locked"):
        return _json({"ok": True, "already_unlocked": True})
    return _json({
        "ok": True,
        "unlocked": False,
        "message": "Open the Wallet pane in Hermes Desktop and type the passphrase. "
                   "The agent cannot supply it and never receives it.",
    })


def wallet_mandate_propose(kind: str, asset: str, payee: str, per_tx_cap: str, period_cap: str,
                           absolute_cap: str, period_hours: int = 24, label: str = "",
                           interval: str = "", **_: Any) -> str:
    """Propose a standing autopay. It stays inert until a human activates it — proposing is not
    granting. `interval` accepts 6h / 2d / weekly as well as `period_hours`."""
    try:
        result = _call("mandates_propose", {
            "kind": kind, "asset": asset, "payee": payee, "per_tx_cap": per_tx_cap,
            "period_cap": period_cap, "absolute_cap": absolute_cap,
            "period_hours": int(period_hours), "interval": interval or "",
            "label": label[:80],
        })
        return _json({"ok": True, **result,
                      "note": "The mandate is inert until a human activates it in the Wallet pane."})
    except Exception as exc:
        return _error(exc)


def wallet_mandate_list(**_: Any) -> str:
    try:
        return _json({"ok": True, **_call("mandates_list")})
    except Exception as exc:
        return _error(exc)


def wallet_mandate_revoke(mandate_id: str, reason: str = "", **_: Any) -> str:
    try:
        return _json({"ok": True, **_call("mandates_revoke",
                                          {"mandate_id": mandate_id, "reason": reason[:200]})})
    except Exception as exc:
        return _error(exc)


def wallet_ledger(limit: int = 20, verify: bool = False, **_: Any) -> str:
    try:
        payload: Dict[str, Any] = {"ok": True, **_call("ledger_tail", {"limit": int(limit)})}
        if verify:
            payload["verification"] = _call("ledger_verify")
        return _json(payload)
    except Exception as exc:
        return _error(exc)


def wallet_receipt_verify(**_: Any) -> str:
    try:
        return _json({"ok": True, **_call("ledger_verify")})
    except Exception as exc:
        return _error(exc)


def _schema(name: str, description: str, properties: Dict[str, Any], required: list[str]) -> Dict[str, Any]:
    return {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required}}


_KIND = {"type": "string", "description": "Chain id, e.g. monero-stagenet, bitcoin-testnet, base-sepolia"}
_ASSET = {"type": "string", "description": "Asset ticker (XMR, BTC, XRP, ETH, USDC, POL). Omit it and the chain decides: xrp-mainnet means XRP, bitcoin-mainnet means BTC. A ticker that is not a coin on that chain is refused."}
_AMOUNT = {"type": "string", "description": "Decimal amount as a string, e.g. \"0.05\""}
_PAYEE = {"type": "string", "description": "Destination address, or a saved contact name"}

TOOL_SPECS: list[tuple[str, str, Callable[..., str], Dict[str, Any]]] = [
    ("wallet_merchants", "Shops that accept crypto, with per-shop buying steps.", wallet_merchants,
     _schema("wallet_merchants",
             "Look up where to buy something with crypto: returns shops, the coins each accepts, "
             "whether an account is needed, and the steps to pay. Check the merchant page for the "
             "final answer on coin support.",
             {"query": {"type": "string", "description": "What the user wants, e.g. 'vpn', 'gift card'"},
              "category": {"type": "string", "description": "privacy | domains | gift-cards | travel"}},
             [])),
    ("wallet_contacts", "List saved contacts (name + chain addresses).", wallet_contacts,
     _schema("wallet_contacts", "List the address book: contact names and the addresses saved per "
                                "chain. Resolve a name with wallet_request_payment(payee=\"<name>\").",
             {}, [])),
    ("wallet_contact_save", "Save or update a contact name -> address.", wallet_contact_save,
     _schema("wallet_contact_save",
             "Save a contact so the user can later say 'send Aiden 5' instead of pasting an address. "
             "The address is validated for that chain before it is stored.",
             {"name": {"type": "string", "description": "Contact name, e.g. Aiden"},
              "kind": _KIND, "address": _PAYEE,
              "notes": {"type": "string", "description": "Optional note, e.g. 'flatmate, splits rent'"}},
             ["name", "kind", "address"])),
    ("wallet_sandboxes", "List sandboxes: budget, spent, remaining, expiry.", wallet_sandboxes,
     _schema("wallet_sandboxes", "List your sandboxes: each has a budget, what has been spent, what "
                                 "is left and when it expires. Spending inside one needs no approval.",
             {}, [])),
    ("wallet_nodes", "Public node health for each enabled chain.", wallet_nodes,
     _schema("wallet_nodes", "List the predefined public endpoints for each enabled chain with a live "
                             "height and health flag. Read-only.", {}, [])),
    ("wallet_balances", "Balances and receive addresses per chain/asset. Check before quoting a price.",
     wallet_balances, _schema("wallet_balances",
                              "Balances and receive addresses per chain and asset.", {}, [])),
    ("wallet_chains", "Chain availability, height and RPC endpoint.", wallet_chains,
     _schema("wallet_chains", "Report each enabled chain: available or not, current height, and the "
                              "RPC endpoint in use. Read-only.", {}, [])),
    ("wallet_status", "Wallet lock state, chains, caps and pending approvals.", wallet_status,
     _schema("wallet_status", "Read the wallet's lock state, enabled chains, pending requests and "
                             "active mandates. Never returns key material. A human approves payments "
                             "in the Wallet pane of Hermes Desktop, and the passphrase is typed "
                             "there — never ask for it in chat.", {}, [])),
    ("wallet_receive_address",
     "Get a fresh receiving address for one incoming payment (Monero subaddress). Use per order.",
     wallet_receive_address,
     _schema("wallet_receive_address",
             "Fresh receiving address for one incoming payment: a new Monero subaddress per order, so "
             "two purchases cannot be linked on-chain.",
             {"label": {"type": "string", "description": "what this address is for, e.g. 'order 42'"},
              "kind": {"type": "string", "description": "chain kind; defaults to the enabled Monero chain"},
              "expected": {"type": "string", "description": "order total as a decimal string, so the incoming list can say paid/partial"}},
             [])),
    ("wallet_incoming",
     "Incoming orders: each labelled subaddress, what was expected and what has arrived.",
     wallet_incoming,
     _schema("wallet_incoming",
             "List incoming orders opened with wallet_receive_address(): the label, address, "
             "expected amount, received amount, waiting/paid/partial status and the last transaction "
             "seen for it. Read-only.",
             {}, [])),
    ("wallet_cancel", "Withdraw a pending payment request you filed (before it is approved).",
     wallet_cancel,
     _schema("wallet_cancel",
             "Cancel one of your own pending payment requests. Only pending requests can be "
             "cancelled; the cancellation is receipted.",
             {"request_id": {"type": "string", "description": "the pending request to withdraw"},
              "reason": {"type": "string", "description": "why it is no longer needed"}},
             ["request_id"])),
    ("wallet_spend", "Spend per asset and per payee over a window (e.g. the last 30 days).",
     wallet_spend,
     _schema("wallet_spend",
             "Report what was actually spent: totals per asset and per payee, computed from the "
             "same approved receipts the spend caps use.",
             {"since": {"type": "string", "description": "window start: 30d, 12h, 2w or an ISO timestamp"},
              "until": {"type": "string", "description": "window end: ISO timestamp or relative age"}},
             [])),
    ("wallet_verify_payment",
     "Prove a payment arrived before treating an order as paid. Monero needs the payer's tx_key; "
     "BTC/EVM need only the txid and the address that was paid.",
     wallet_verify_payment,
     _schema("wallet_verify_payment",
             "Prove a payment arrived: the tx key for Monero, the txid and paid address for BTC/EVM.",
             {"txid": {"type": "string"}, "tx_key": {"type": "string"},
              "address": {"type": "string", "description": "the address that was paid"},
              "kind": {"type": "string", "description": "chain kind; defaults to Monero"},
              "asset": {"type": "string", "description": "asset ticker for EVM tokens, e.g. USDC"}},
             ["txid", "address"])),
    ("wallet_pay_url", "Pay an HTTP 402 (x402) endpoint: the price comes from the server, not from you. "
     "Confirm the URL and the quoted price with the user before paying it.",
     wallet_pay_url,
     _schema("wallet_pay_url",
             "Fetch the URL; if it answers 402 with x402 terms, price the purchase from that response "
             "and open a request. You cannot set the amount, asset or payee, so show the user the URL "
             "and the price the server quoted and get a yes before calling this.",
             {"url": {"type": "string", "description": "the resource URL that answered 402 / quotes a price"},
              "why": {"type": "string", "description": "what this purchase is for (shown to the approver)"},
              "sandbox_id": {"type": "string", "description": "spend from a sandbox budget instead of asking"}},
             ["url"])),
    ("wallet_request_payment",
     "Request a payment; a human must approve it in the Wallet pane. Confirm the destination and "
     "amount with the user in chat first - including when the address came from a saved contact or "
     "a previous payment: resolve it, show the full address, and ask. Then pass idempotency_key so a "
     "retry never pays twice, and call wallet_request_approval to put the ask in front of them.",
     wallet_request_payment,
     _schema("wallet_request_payment",
             "Ask to send crypto. Creates a pending request; a human approves it in the Wallet pane. "
             "ALWAYS confirm the exact address, asset, network and amount with the user in chat "
             "before calling this - even when the destination came from a saved contact or a past "
             "payment (show the resolved address in full and ask). "
             "Call wallet_request_approval to raise that prompt for them. "
             "Denied automatically when nobody can approve, or when policy caps are exceeded.",
             {"kind": _KIND, "asset": _ASSET, "amount": _AMOUNT, "payee": _PAYEE,
              "memo": {"type": "string", "description": "What the payment is for"},
              "why": {"type": "string", "description": "Why the agent is asking"},
              "sandbox_id": {"type": "string", "description": "Spend from this sandbox (no approval needed inside its budget)"}},
             ["kind", "asset", "amount", "payee"])),
    ("wallet_request_approval",
     "Ask the human to approve a payment in the Wallet pane (passphrase typed there, not in chat).",
     wallet_request_approval,
     _schema("wallet_request_approval",
             "Raise the approval prompt in the Wallet pane for a pending request. The passphrase is "
             "typed there and never reaches you.",
             {"request_id": {"type": "string", "description": "the pending request to approve"},
              "message": {"type": "string", "description": "what you are asking for, shown in the pane"}},
             [])),
    ("wallet_request_unlock",
     "Ask the human to unlock the wallet in the Wallet pane. Never ask for the passphrase in chat.",
     wallet_request_unlock,
     _schema("wallet_request_unlock",
             "Ask the human to unlock the wallet: the Wallet pane in Hermes Desktop prompts for the "
             "passphrase, which never reaches you and must never be requested in chat.", {}, [])),
    ("wallet_mandate_propose", "Propose an auto-pay mandate (inert until a human activates it).",
     wallet_mandate_propose,
     _schema("wallet_mandate_propose",
             "Propose a capped, expiring auto-pay mandate for one payee. It does nothing until a "
             "human activates it in the Wallet pane.",
             {"kind": _KIND, "asset": _ASSET, "payee": _PAYEE,
              "per_tx_cap": _AMOUNT, "period_cap": _AMOUNT, "absolute_cap": _AMOUNT,
              "period_hours": {"type": "integer", "description": "Spend window in hours (1-168)"},
              "label": {"type": "string", "description": "Human label, e.g. the vendor name"}},
             ["kind", "asset", "payee", "per_tx_cap", "period_cap", "absolute_cap"])),
    ("wallet_mandate_list", "List mandates and their status.", wallet_mandate_list,
     _schema("wallet_mandate_list", "List proposed, active, expired and revoked mandates.", {}, [])),
    ("wallet_mandate_revoke", "Revoke a mandate immediately.", wallet_mandate_revoke,
     _schema("wallet_mandate_revoke", "Revoke an auto-pay mandate. Always allowed, no approval needed.",
             {"mandate_id": {"type": "string"}, "reason": {"type": "string"}}, ["mandate_id"])),
    ("wallet_ledger", "Read recent receipts, optionally verifying the chain.", wallet_ledger,
     _schema("wallet_ledger", "Read the signed receipt ledger and optionally verify its hash chain.",
             {"limit": {"type": "integer"}, "verify": {"type": "boolean"}}, [])),
    ("wallet_receipt_verify", "Verify the receipt chain (tamper detection).", wallet_receipt_verify,
     _schema("wallet_receipt_verify",
             "Verify every receipt signature and hash link. Returns ok=false at the first break.",
             {}, [])),
]
