"""Agent-facing tools.

Every one of these is a *request* or a *read*. None of them can sign, approve, unlock or touch key
material — that is the point, and it is enforced on the other side of the socket.
"""

from __future__ import annotations

import re

import json
from typing import Any, Callable, Dict

from . import client, daemon

_MAX_TEXT = 400


def _json(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True)


def _error(exc: Exception) -> str:
    """One shape for every failure: a code the agent can branch on, and a message for the human.

    `client` already prefixes daemon refusals with their code ("insufficient_funds: …"), so split it
    out instead of returning the class name — `{"error": "WalletUnavailable"}` told an agent nothing.
    """
    text = str(exc)
    code, sep, message = text.partition(": ")
    if not sep:
        if isinstance(exc, client.WalletUnavailable):
            code = "unavailable"        # a dead daemon is a state, not a class name
        elif isinstance(exc, (ValueError, TypeError)):
            code = "bad_arguments"
        else:
            code = "internal"
        message = text
    return _json({"ok": False, "error": code[:_MAX_TEXT], "message": message[:_MAX_TEXT]})


_DEAD_DAEMON = ("not listening", "has not been started", "did not answer in time",
                "closed the connection without answering")


def _call(method: str, params: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Call the daemon, starting it once if it is *not listening*.

    `client` raises WalletUnavailable for every daemon refusal — `per_tx_cap`, `insufficient_funds`,
    `busy` — so a blanket retry restarted the daemon and **replayed the mutating call** after each
    refusal. Only a genuinely dead socket is worth a restart.
    """
    try:
        return client.call(method, params or {})
    except client.WalletUnavailable as exc:
        if not any(marker in str(exc) for marker in _DEAD_DAEMON):
            raise
        started = daemon.start()
        if not started.get("ok"):
            raise client.WalletUnavailable(f"{exc}; daemon start failed: {started.get('error') or started}") from exc
        return client.call(method, params or {})


def wallet_readiness(kind: str = "", asset: str = "", amount: str = "", payee: str = "",
                     **_: Any) -> str:
    """Would a payment of this size go through right now? Builds it, sends nothing."""
    try:
        params: Dict[str, Any] = {}
        for key, value in (("kind", kind), ("asset", asset), ("amount", amount), ("payee", payee)):
            if value:
                params[key] = value
        return _json({"ok": True, **_call("readiness", params)})
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


def wallet_deposits(**_: Any) -> str:
    """Deposit addresses: where money is expected, how much has arrived, and whether it merged home."""
    try:
        result = _call("deposits_list")
    except Exception as exc:
        return _error(exc)
    rows = result.get("deposits", [])
    return _json({"ok": True, "count": len(rows), "deposits": [
        {"deposit_id": r.get("deposit_id"), "label": r.get("label"), "address": r.get("address"),
         "balance": r.get("balance"), "spendable": r.get("unlocked"),
         "auto_merge": r.get("auto_merge"), "merged_total": r.get("merged_total"),
         # an address is public; the balance is the operator's business, but the agent needs it to
         # answer "did my invoice get paid?" without asking for anything
         "error": r.get("error")}
        for r in rows]})


def wallet_sandboxes(**_: Any) -> str:
    """Capped sub-wallets: a budget that bounds what it can lose on one task.

    Every spend from a sandbox still asks the human for the PIN; the budget bounds the amount, not the
    gate. Never tell the user a sandbox lets you spend without their approval."""
    try:
        return _json({"ok": True, **_call("sandboxes_list")})
    except Exception as exc:
        return _error(exc)


def wallet_request_sandbox(kind: str = "", asset: str = "", budget: str = "", label: str = "",
                           mode: str = "budget", hours: int = 24,
                           self_destruct_seconds: int = 1800,
                           allowed_payees: Any = None, sandbox_id: str = "", **_: Any) -> str:
    """Ask the human for a capped budget, or for more money on the one you already have.

    With no `sandbox_id` this creates an unfunded budget plus the transfer that funds it: asking
    costs nothing and moves nothing. With a `sandbox_id` it asks to *top up* that budget — the human
    approves a transfer into the same account, so the ceiling rises and nothing is stranded.

    Either way the human decides. Once they arm a budget (a separate, deliberate act that costs
    their passphrase and PIN) you may spend inside it without asking per payment; when the money
    runs out, ask here again.
    """
    try:
        if sandbox_id:
            return _json({"ok": True,
                          **_call("sandbox_topup", {"sandbox_id": sandbox_id,
                                                    "amount": budget,
                                                    "why": label or f"more budget for {sandbox_id}"})})
        if not kind or not asset or not budget:
            return _json({"ok": False, "error": {"code": "bad_arguments",
                                                 "message": "a new budget needs kind, asset and budget"}})
        params: Dict[str, Any] = {"kind": kind, "asset": asset, "budget": budget,
                                  "label": label[:40], "mode": mode,
                                  "lifetime_hours": int(hours),
                                  "self_destruct_seconds": int(self_destruct_seconds)}
        if allowed_payees:
            params["allowed_payees"] = [str(a) for a in allowed_payees][:20]
        return _json({"ok": True, **_call("sandbox_propose", params)})
    except Exception as exc:
        return _error(exc)


def wallet_orders(**_: Any) -> str:
    """Incoming orders: the address opened for each one, and what has actually arrived on it.

    One row per address from `wallet_receive_address`, with status waiting / partial / paid / closed,
    and an `order_id` that `wallet_close_order` takes. Read-only; `wallet_verify_payment` is what
    proves a single transfer, this is the per-order running total.
    """
    try:
        result = _call("incoming")
    except Exception as exc:
        return _error(exc)
    # The daemon still calls the RPC `incoming`; the agent only ever sees the `orders` name, so a
    # model that read the schema never has to know the old one existed.
    orders = result.pop("orders", None)
    if orders is not None:
        result = {"orders": orders, **result}
    return _json({"ok": True, **result})


def wallet_close_order(order_id: str, reason: str = "", **_: Any) -> str:
    """Close a settled order (no money moves)."""
    try:
        return _json({"ok": True, **_call("orders_close", {"order_id": order_id,
                                                           "reason": reason[:200]})})
    except Exception as exc:
        return _error(exc)


def wallet_nodes(**_: Any) -> str:
    """Which chains are live, their height, and the node behind each.

    One read for "is XMR/BTC/XRP reachable right now, and from where", including the public
    endpoints tried when a chain is unavailable — it replaced a separate node-health and
    chain-height tool. Read-only and cheap.
    """
    try:
        chains = _call("chain_status").get("chains") or []
        rows = _call("nodes").get("nodes") or []          # a flat list of endpoint rows
    except Exception as exc:
        return _error(exc)
    by_kind: Dict[str, list] = {}
    for node in rows if isinstance(rows, list) else []:
        by_kind.setdefault(str((node or {}).get("kind", "")), []).append(node)
    return _json({"ok": True, "count": len(chains), "chains": [
        {**(row if isinstance(row, dict) else {"kind": str(row)}),
         "nodes": by_kind.get(str((row or {}).get("kind", "")) if isinstance(row, dict) else "", [])}
        for row in chains]})


def wallet_balances(full: bool = False, **_: Any) -> str:
    """Balances per chain and asset, with the address to receive on.

    Check this before quoting a price to the user: a payment the wallet cannot afford is refused at
    plan time, and on EVM a transfer without gas is refused before it is signed.

    Addresses come back shortened (``49Tmd…ajgm``) because a chat agent does not need 95 characters
    per row; pass ``full=True`` when the user has to see or copy the exact string.
    """
    try:
        payload = _call("balances")
        if not full:
            payload = _shorten_addresses(payload)
        return _json({"ok": True, **payload})
    except Exception as exc:
        return _error(exc)


def _short_address(value: Any) -> Any:
    """``49Tmd…ajgm``: enough to recognise the address, not enough to burn the context window."""
    if not isinstance(value, str) or len(value) <= 12:
        return value
    return f"{value[:5]}…{value[-4:]}"


def _shorten_addresses(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Shorten every address in a balances payload, leaving the rest of the shape untouched."""
    out = dict(payload)
    if isinstance(out.get("accounts"), list):
        out["accounts"] = [{**row, "address": _short_address(row.get("address"))}
                           if isinstance(row, dict) and "address" in row else row
                           for row in out["accounts"]]
    if isinstance(out.get("addresses"), dict):
        out["addresses"] = {kind: _short_address(addr) for kind, addr in out["addresses"].items()}
    return out


def _compact_status(status: Dict[str, Any]) -> Dict[str, Any]:
    """What a chat agent needs to plan a payment: lock state, coins, caps, what is waiting.

    The full status carries four tables that only the Wallet pane uses (asset_chains, chain_assets,
    native_asset, available_assets, defaults, policy, profiles). Returning all of it on every call
    cost ~1.5 KB of context to answer "is it unlocked and what can I spend".
    """
    return {
        "home": status.get("home"),
        "locked": status.get("locked"),
        "initialized": status.get("initialized"),
        "coins": status.get("assets") or [],
        "chains": status.get("chains") or [],
        "caps": status.get("caps") or {},
        "pending_requests": status.get("pending_requests", 0),
        "active_mandates": status.get("active_mandates", 0),
        "human_gate": status.get("human_gate"),
        "autolock_minutes": status.get("autolock_minutes", 0),
        "unlock_expires_in": status.get("unlock_expires_in"),
        "signer": {"installed": status.get("signer_installed"),
                   "reachable": status.get("signer_reachable")},
        "more": "wallet_status(full=True) adds per-coin chains, defaults, policy and profiles",
    }


def wallet_status(full: bool = False, **_: Any) -> str:
    """Lock state, coins, caps, pending approvals — compact unless ``full`` is asked for."""
    try:
        status = _call("status")
        payload: Dict[str, Any] = {"ok": True, "wallet": status if full else _compact_status(status)}
        # A locked wallet is the one state where the agent cannot do the obvious next thing, and it
        # used to answer "unlock in the pane" as prose — which raises nothing, so the operator saw a
        # sentence instead of a prompt. Say what to call, not what the user should go and find.
        if status.get("locked"):
            payload["locked_next"] = ("call wallet_request_approval(message=...) to put the unlock "
                                      "prompt in the Wallet pane; do not ask for the PIN in chat")
        return _json(payload)
    except Exception as exc:
        return _error(exc)


def wallet_request_payment(amount: str, payee: str, kind: str = "", asset: str = "",
                           memo: str = "", why: str = "", sandbox_id: str = "",
                           source_url: str = "", idempotency_key: str = "", **_: Any) -> str:
    """File a spend request for a human to approve.

    Confirm with the user first. A saved contact or a previous payment is a lead to resolve, not
    permission to send: print the full address and amount back and wait for a yes.
    """
    try:
        # `kind` and `asset` are optional in the schema ("omit and the chain decides"), so the daemon
        # picks the enabled Monero chain and its coin rather than the model having to know both.
        kind, asset = str(kind or "").strip(), str(asset or "").strip().upper()
        if not kind:
            # the daemon returns `default_chain` flat; the CLI is what nests everything under
            # "wallet", so reading only the nested shape made every call without `kind` fail with
            # `chain_unavailable` ("no enabled chain for XMR")
            status = _call("status")
            wallet = status.get("wallet") if isinstance(status.get("wallet"), dict) else status
            chains = wallet.get("default_chain") or {}
            asset = asset or str(wallet.get("preferred_asset") or "XMR").upper()
            kind = chains.get(asset, "")
            if not kind:
                return _json({"ok": False, "error": "chain_unavailable",
                              "message": f"no enabled chain for {asset}; nothing was filed"})
        # A chain with no coin is left for the daemon ("the chain decides"). Defaulting it to XMR
        # here turned `kind="bitcoin-mainnet"` into "XMR is not a coin on bitcoin-mainnet".
        params = {"kind": kind, "asset": asset, "amount": amount, "payee": payee,
                  "memo": memo[:200], "why": why[:_MAX_TEXT],
                  "profile": client.current_profile()}
        if sandbox_id:
            params["sandbox_id"] = sandbox_id
        if source_url:
            # Recorded, never trusted: the daemon marks provenance it did not fetch as
            # `verified: false`, and the card says "agent says". Only pay_url can verify a page.
            from urllib.parse import urlparse
            host = urlparse(source_url).hostname or ""
            params["provenance"] = {"url": source_url[:300], "host": host,
                                    "method": "agent", "title": ""}
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
    wallet_orders can say waiting / partial / paid instead of only showing a balance.
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


def wallet_verify_payment(txid: str, address: str, tx_key: str = "", kind: str = "",
                          asset: str = "", **_: Any) -> str:
    """Prove a payment arrived, using the tx key the payer hands you (Monero only).

    Amounts and parties are hidden on Monero; the tx key is what makes a payment checkable by the
    recipient. Never mark an order paid without this.
    """
    try:
        params: Dict[str, Any] = {"txid": txid, "tx_key": tx_key, "address": address}
        if kind:
            params["kind"] = kind
        if asset:
            params["asset"] = asset.upper()
        return _json({"ok": True, **_call("verify_payment", params)})
    except Exception as exc:
        return _error(exc)


def wallet_pay_url(url: str, why: str = "", sandbox_id: str = "",
                   idempotency_key: str = "", amount: str = "", **_: Any) -> str:
    """Pay a checkout URL: an x402 endpoint, or an invoice page that states an address.

    Two shapes, one rule — **the wallet reads the price, you do not supply it.**

    - a 402/x402 endpoint: the amount, asset, chain and payee come from that response;
    - any other checkout/invoice page: the wallet reads the page and uses a `monero:`/`bitcoin:`/
      `ripple:`/`ethereum:` URI or a bare address it can see there, and records the page's host and
      title on the request so the human sees *where* the address came from.

    If the page has no address, or more than one, nothing is filed: you get `no_target` or
    `ambiguous_target` and must ask the operator. A page that names an address but no amount needs you
    to pass `amount` (e.g. `10$`).
    """
    try:
        params = {"url": url, "why": why[:_MAX_TEXT], "profile": client.current_profile()}
        if sandbox_id:
            params["sandbox_id"] = sandbox_id
        if idempotency_key:
            params["idempotency_key"] = idempotency_key[:64]
        if str(amount or "").strip():
            # The schema has always offered `amount` for a page that names an address but no price,
            # and the daemon has always read it — but this function never took the argument, so it
            # fell into **_ and the call failed with `amount_required` however it was made. Only an
            # invoice page uses it: a 402 quote prices itself and ignores it.
            params["amount"] = str(amount).strip()[:40]
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
    """Recent receipts, and whether the hash chain still verifies.

    `verify=True` walks every signature and hash link and sets `verification.ok=false` at the first
    break; it is not a second tool.
    """
    try:
        payload: Dict[str, Any] = {"ok": True, **_call("ledger_tail", {"limit": int(limit)})}
        if verify:
            payload["verification"] = _call("ledger_verify")
        return _json(payload)
    except Exception as exc:
        return _error(exc)


#: Payment-capable tools from other plugins, and the shapes that count as one.
FOREIGN_PAYMENT_TOOLS = {
    "mm_transfer", "mm_swap_execute", "mm_sign",
    "mpp_fetch", "pay", "pay_cli", "agentwalletapi_transfer_send",
}

_PAYMENT_NAME = re.compile(
    r"(?:^|[_.])(pay|payments?|send|transfer|spend|tip|invoice|checkout|buy|purchase|withdraw|"
    r"swap|bridge|topup|top_up|deposit)(?:$|[_.])", re.IGNORECASE)

#: "send" and "tip" are also what messaging and UI tools are called. Matching on the verb alone blocked
#: Hermes's own `send_message`, `send_dm`, `send_document`, `send_voice`… and `show_tip` the moment
#: this plugin was enabled. A name that says what it sends, and that thing is not money, is not ours.
_NOT_MONEY = re.compile(
    r"(?:^|[_.])(message|messages|dm|document|image|sticker|video|voice|audio|photo|file|files|"
    r"email|mail|sms|text|notification|reaction|url|keys?|keystrokes?|show|tooltip)(?:$|[_.])",
    re.IGNORECASE)

#: Shell-class tools can move money too. The guard cannot read every language, but it can refuse the
#: two things an injected agent does: drive the wallet CLI directly, or talk to the signer's RPC.
SHELL_TOOLS = {"terminal", "execute_code", "shell", "bash", "run_command", "run_shell_command",
               "process", "code_exec", "python"}
_MOVES_MONEY_IN_TEXT = re.compile(
    r"(hermes\s+wallet\s+(approve|pay-url|request|send|sandbox|deposit|mandates|pause|resume)\b"
    r"|monero-wallet-rpc"
    r"|/json_rpc\b"
    r"|wallet\.env"
    r"|walletd\.sock)", re.IGNORECASE)


def moves_money_out_of_band(tool_name: str, args: Any = None) -> bool:
    """Would calling this tool move money without passing the wallet's approval pipeline?

    A hardcoded list of seven names was defeated by any tool that was not on it (`xmr_send`, a new
    plugin's `tip`, an MCP tool). So: match the intent in the name (except our own `wallet_*`), and
    for shell-class tools look for the wallet CLI or the signer's RPC in the arguments.
    """
    name = str(tool_name or "")
    if name in FOREIGN_PAYMENT_TOOLS:
        return True
    if (not name.startswith("wallet_") and _PAYMENT_NAME.search(name.replace("__", "."))
            and not _NOT_MONEY.search(name.replace("__", "."))):
        return True
    if name in SHELL_TOOLS and isinstance(args, dict):
        blob = " ".join(str(value) for value in args.values() if isinstance(value, (str, bytes)))
        return bool(_MOVES_MONEY_IN_TEXT.search(blob))
    return False


#: Words that mean a turn might involve money. The per-turn brief only fires on one of these (or when
#: the daemon already holds a request), because a message injected every turn is new tokens every turn.
MONEY_HINTS = (
    "wallet", "balance", "send", "pay", "payment", "paid", "transfer", "invoice", "deposit",
    "refund", "tip", "buy", "purchase", "subscription", "autopay", "budget", "sandbox",
    "crypto", "coin", "xmr", "monero", "btc", "bitcoin", "xrp", "usdc", "ethereum", "pol",
    "$", "€", "£",
)


def pending_recent(seconds: float = 1800.0) -> bool:
    """True when the wallet recently told us something is waiting for the human."""
    try:
        from . import client as wallet_client
        import time

        return bool(wallet_client.PENDING_SEEN_AT) and \
            (time.monotonic() - wallet_client.PENDING_SEEN_AT) < seconds
    except Exception:                                        # noqa: BLE001
        return False


def mentions_money(text: str) -> bool:
    lowered = (text or "").lower()
    return any(hint in lowered for hint in MONEY_HINTS)


def _schema(name: str, description: str, properties: Dict[str, Any], required: list[str]) -> Dict[str, Any]:
    """The tool's parameter schema — deliberately without a description.

    `register_tool(... description=...)` already carries one, and the schema dict used to repeat it, so
    every tool sent its description twice on every request. The `description` argument stays in the
    signature (callers pass it) but is not emitted: one description per tool, and the caller's is the
    one the model reads first.
    """
    del description
    return {"name": name,
            "parameters": {"type": "object", "properties": properties, "required": required}}


#: The agent's tool surface, one entry per *distinct act*. Names that were synonyms of an entry here
#: (`wallet_incoming` for `wallet_orders`, `wallet_chains` for `wallet_nodes`, `wallet_receipt_verify`
#: for `wallet_ledger(verify=True)`, `wallet_request_unlock` for `wallet_status`) are gone, not
#: aliased: every alias is another description the model pays for and another name it can pick wrong.
# Schema text is charged on every request of every session, whether or not the wallet is used, so
# these stay one clause. Anything longer belongs in the docs, not in the tool list the model re-reads.
_KIND = {"type": "string", "description": "chain id, e.g. monero-mainnet"}
_ASSET = {"type": "string", "description": "ticker; omit and the chain decides"}
_AMOUNT = {"type": "string", "description": "amount, coin (0.05) or fiat (10$)"}
_PAYEE = {"type": "string", "description": "address or saved contact name"}

TOOL_SPECS: list[tuple[str, str, Callable[..., str], Dict[str, Any]]] = [
    ("wallet_merchants", "Where to buy something with crypto; not for paying.", wallet_merchants,
     _schema("wallet_merchants",
             "Find shops that take crypto: coins accepted, whether an account is needed, how to pay. "
             "The merchant page is the final word on coin support.",
             {"query": {"type": "string", "description": "What the user wants, e.g. 'vpn', 'gift card'"},
              "category": {"type": "string", "description": "privacy | domains | gift-cards | travel"}},
             [])),
    ("wallet_contacts", "List saved contacts (name + chain addresses).", wallet_contacts,
     _schema("wallet_contacts", "List the address book. A contact name works as payee on "
                                "wallet_request_payment.",
             {}, [])),
    ("wallet_contact_save", "Save or update a contact name -> address.", wallet_contact_save,
     _schema("wallet_contact_save",
             "Save a contact so the user can say 'send Aiden 5'; the address is validated for that "
             "chain now, not at payment.",
             {"name": {"type": "string", "description": "Contact name, e.g. Aiden"},
              "kind": _KIND, "address": _PAYEE,
              "notes": {"type": "string", "description": "Optional note, e.g. 'flatmate, splits rent'"}},
             ["name", "kind", "address"])),
    ("wallet_deposits", "Deposit addresses and what arrived.", wallet_deposits,
     _schema("wallet_deposits",
             "Read the deposit addresses and what has arrived on each. Creating, arming and merging "
             "are the operator's: tell them the command or the pane section. For a one-off order use "
             "wallet_receive_address.",
             {}, [])),
    ("wallet_sandboxes", "Budgets for tasks: spent, remaining, expiry.", wallet_sandboxes,
     _schema("wallet_sandboxes",
             "List sandboxes: budget, spent, remaining, on-chain balance, expiry. A spend still needs "
             "the PIN unless the human armed it.",
             {}, [])),
    ("wallet_request_sandbox", "Ask for a budget for this task.", wallet_request_sandbox,
     _schema("wallet_request_sandbox",
             "Ask for a budget (kind+asset+budget) or more on an existing one (sandbox_id+budget); the "
             "human approves the transfer. Never say a budget means spending without approval.",
             {"kind": {"type": "string", "description": "chain id, e.g. monero-mainnet"},
              "asset": {"type": "string", "description": "coin, e.g. XMR"},
              "budget": {"type": "string", "description": "total budget to move in, e.g. '0.05'"},
              "label": {"type": "string", "description": "what the budget is for"},
              "mode": {"type": "string", "description": "budget (default) or one_time"},
              "hours": {"type": "integer", "description": "how long it stays open (default 24)"},
              "sandbox_id": {"type": "string",
                             "description": "top up this budget instead of creating one"}},
             [])),
    ("wallet_orders", "Incoming orders and what arrived.", wallet_orders,
     _schema("wallet_orders",
             "List orders by id: label, address, expected, arrived, waiting/partial/paid/closed — "
             "answers 'did the customer pay yet?' but proves no single transfer "
             "(wallet_verify_payment does).",
             {}, [])),
    ("wallet_close_order", "Close a settled order (no money moves).", wallet_close_order,
     _schema("wallet_close_order",
             "Close an order from wallet_orders that is settled; money stays put and the close is "
             "receipted. Never a refund or a transfer.",
             {"order_id": {"type": "string", "description": "id from wallet_orders"},
              "reason": {"type": "string", "description": "why it is being closed"}},
             ["order_id"])),
    ("wallet_readiness", "Would a payment go through now? Sends nothing.", wallet_readiness,
     _schema("wallet_readiness",
             "Dry-run a payment of this size (keystore, signer, node, balance, fee, caps) and build it "
             "without broadcasting; use it before promising a payment works, not as a balance read.",
             {"kind": {"type": "string", "description": "chain id (default: the first Monero chain)"},
              "asset": {"type": "string", "description": "coin (default: the chain's native)"},
              "amount": {"type": "string", "description": "the amount you intend to send"},
              "payee": {"type": "string", "description": "destination (default: your own address)"}},
             [])),
    ("wallet_status", "Lock state, coins, caps, pending approvals.", wallet_status,
     _schema("wallet_status",
             "Read lock state, coins, caps, pending requests and mandates — also answers 'is it "
             "unlocked?': never ask for the passphrase in chat, send them to the Wallet pane. Compact; "
             "full=True adds chains, defaults and policy.",
             {"full": {"type": "boolean",
                       "description": "Also return per-coin chains, defaults, policy and profiles"}},
             [])),
    ("wallet_nodes", "Chain availability, height and node health.", wallet_nodes,
     _schema("wallet_nodes",
             "Report each enabled chain once: up or not, height, RPC in use, and the nodes tried when "
             "it is down. For diagnosing a failure, not for balances.",
             {}, [])),
    ("wallet_balances", "Balances and addresses per chain.", wallet_balances,
     _schema("wallet_balances",
             "Balances and receive addresses per chain and asset, addresses shortened; full=True for "
             "the exact strings when the user must see or copy one.",
             {"full": {"type": "boolean",
                       "description": "Return full addresses instead of 49Tmd…ajgm"}},
             [])),
    ("wallet_receive_address", "Fresh address for one incoming payment.", wallet_receive_address,
     _schema("wallet_receive_address",
             "Fresh address for one incoming payment, a new subaddress per order so two purchases "
             "cannot be linked; pass expected so wallet_orders can say paid/partial. Not deposits.",
             {"label": {"type": "string", "description": "what this address is for, e.g. 'order 42'"},
              "kind": {"type": "string", "description": "chain kind; defaults to the enabled Monero chain"},
              "expected": {"type": "string", "description": "order total as a decimal string, so wallet_orders can say paid/partial"}},
             [])),
    ("wallet_cancel", "Withdraw a pending request you filed.", wallet_cancel,
     _schema("wallet_cancel",
             "Withdraw one of your own requests that is still pending; an approved or denied one is "
             "history. Receipted.",
             {"request_id": {"type": "string", "description": "the pending request to withdraw"},
              "reason": {"type": "string", "description": "why it is no longer needed"}},
             ["request_id"])),
    ("wallet_spend", "Spend per asset and payee over a window.", wallet_spend,
     _schema("wallet_spend",
             "Totals actually spent, per asset and payee, from the receipts the caps enforce; for "
             "totals, not a single payment.",
             {"since": {"type": "string", "description": "window start: 30d, 12h, 2w or an ISO timestamp"},
              "until": {"type": "string", "description": "window end: ISO timestamp or relative age"}},
             [])),
    ("wallet_ledger", "Recent receipts; verify=True proves the chain is intact.", wallet_ledger,
     _schema("wallet_ledger",
             "Read the signed receipt ledger, and with verify=True walk every signature and hash link "
             "to detect tampering; for the audit trail, not a balance.",
             {"limit": {"type": "integer", "description": "how many recent receipts"},
              "verify": {"type": "boolean", "description": "walk the whole chain for tampering"}},
             [])),
    ("wallet_verify_payment", "Prove a payment arrived before treating an order as paid.",
     wallet_verify_payment,
     _schema("wallet_verify_payment",
             "Prove one transfer arrived: Monero needs the payer's tx_key, BTC/EVM only the txid and "
             "paid address. Never mark an order paid without it; for a running total use "
             "wallet_orders.",
             {"txid": {"type": "string"}, "tx_key": {"type": "string"},
              "address": {"type": "string", "description": "the address that was paid"},
              "kind": {"type": "string", "description": "chain kind; defaults to Monero"},
              "asset": {"type": "string", "description": "asset ticker for EVM tokens, e.g. USDC"}},
             ["txid", "address"])),
    ("wallet_pay_url", "Pay a checkout/invoice URL; the page prices it.", wallet_pay_url,
     _schema("wallet_pay_url",
             "Pay a checkout or invoice URL the page prices (x402, or a page with a cash-addr URI); "
             "show the user the URL and quote and get a yes, and never invent the price. Use "
             "wallet_request_payment when you already know payee and amount.",
             {"url": {"type": "string", "description": "checkout or invoice URL"},
              "why": {"type": "string", "description": "why, shown to the approver"},
              "amount": {"type": "string", "description": "only when the page states no price"},
              "sandbox_id": {"type": "string", "description": "spend inside this budget"},
              "idempotency_key": {"type": "string", "description": "retry key; never pays twice"}},
             ["url"])),
    ("wallet_request_payment", "Ask a human to approve a payment.", wallet_request_payment,
     _schema("wallet_request_payment",
             "File a spend request a human approves with their PIN; confirm the exact address, asset, "
             "network and amount in chat first, then call wallet_request_approval. Pass "
             "idempotency_key so a retry never pays twice; a fiat amount is quoted by the daemon. Use "
             "wallet_pay_url when a page sets the price.",
             {"kind": _KIND, "asset": _ASSET,
              "amount": {"type": "string",
                         "description": "coin (0.001) or fiat (10$); pass the user's figure, the daemon quotes"},
              "payee": _PAYEE,
              "memo": {"type": "string", "description": "what it is for"},
              "why": {"type": "string", "description": "why, shown to the approver"},
              "source_url": {"type": "string", "description": "page you found it on (unverified)"},
              "sandbox_id": {"type": "string", "description": "spend inside this budget"},
              "idempotency_key": {"type": "string", "description": "retry key; never pays twice"}},
             ["amount", "payee"])),
    ("wallet_request_approval", "Raise the approval card in the Wallet pane.", wallet_request_approval,
     _schema("wallet_request_approval",
             "Raise the prompt in the Wallet pane for a request you filed, or to ask for the unlock; "
             "the passphrase is typed there and never reaches you — never ask for it in chat.",
             {"request_id": {"type": "string", "description": "the pending request to approve"},
              "message": {"type": "string", "description": "what you are asking for, shown in the pane"}},
             [])),
    ("wallet_mandate_propose", "Propose an autopay (inert until activated).", wallet_mandate_propose,
     _schema("wallet_mandate_propose",
             "Propose a capped, expiring auto-pay for one payee; inert until a human activates it in "
             "the pane. Proposing is not granting — say so.",
             {"kind": _KIND, "asset": _ASSET, "payee": _PAYEE,
              "per_tx_cap": _AMOUNT, "period_cap": _AMOUNT, "absolute_cap": _AMOUNT,
              "period_hours": {"type": "integer", "description": "Spend window in hours"},
              "interval": {"type": "string", "description": "or 6h / 2d / weekly"},
              "label": {"type": "string", "description": "Human label, e.g. the vendor name"}},
             ["kind", "asset", "payee", "per_tx_cap", "period_cap", "absolute_cap"])),
    ("wallet_mandate_list", "List mandates and their status.", wallet_mandate_list,
     _schema("wallet_mandate_list", "List proposed, active, paused, expired and revoked auto-pays. "
                                    "Read-only.", {}, [])),
    ("wallet_mandate_revoke", "Revoke a mandate immediately.", wallet_mandate_revoke,
     _schema("wallet_mandate_revoke",
             "Revoke an auto-pay for good; always allowed and needs no approval. To pause it "
             "temporarily, the user does that in the Wallet pane.",
             {"mandate_id": {"type": "string"}, "reason": {"type": "string"}}, ["mandate_id"])),
]
