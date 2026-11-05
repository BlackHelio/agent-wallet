"""agent-wallet — approval-gated crypto wallet for Hermes agents.

The agent half registers 22 tools, a `pre_tool_call` guard, a per-turn wallet brief, a frozen
system-prompt section, and `hermes wallet`. Everything
privileged happens in `walletd`, a separate process; nothing here can sign.
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Any, Dict

from .wallet import cli as wallet_cli
from .wallet.tools import TOOL_SPECS

# Payment-capable tools from other plugins. They must not move money behind this plugin's back:
# the agent's route is wallet_request_payment, which produces a receipt.
FOREIGN_PAYMENT_TOOLS = {
    "mm_transfer", "mm_swap_execute", "mm_sign",
    "mpp_fetch", "pay", "pay_cli", "agentwalletapi_transfer_send",
}

GUARD_MESSAGE = (
    "Blocked by agent-wallet: this tool moves funds outside the wallet's approval and receipt "
    "pipeline. Use wallet_request_payment (or wallet_mandate_propose for recurring payments) so a "
    "human approves it and the ledger records it."
)


def _guard(tool_name: str = "", args: Dict[str, Any] | None = None, **_: Any) -> Dict[str, Any] | None:
    if tool_name in FOREIGN_PAYMENT_TOOLS:
        return {"action": "block", "message": GUARD_MESSAGE}
    return None


# Deadline (time.monotonic()) until which the per-turn brief is skipped. It used to be a boolean that
# nothing read, so a dead daemon cost 1.5s on *every* LLM call instead of backing off.
WALLET_BRIEF_FAIL = 0.0


def _wallet_brief(session_id: str = "", user_message: str = "", is_first_turn: bool = False,
                  **_: Any) -> Dict[str, Any] | None:
    """Tell every ordinary chat that this plugin exists, and what state it is in.

    Registered on `pre_llm_call`, whose documented purpose is exactly this: injecting a little
    context into the current turn. Kept to ~300 characters so it costs nothing noticeable, and it
    never includes a secret — a passphrase belongs in the Wallet pane, not in the transcript.
    """
    global WALLET_BRIEF_FAIL
    if WALLET_BRIEF_FAIL and time.monotonic() < WALLET_BRIEF_FAIL:
        return None                      # still in the back-off window: no probe, no delay
    try:
        from .wallet import client as wallet_client

        status = wallet_client.call("status", timeout=1.5)
    except Exception:  # noqa: BLE001 - a dead daemon must not slow every turn
        WALLET_BRIEF_FAIL = time.monotonic() + 60.0      # try again in a minute, not next turn
        return None
    WALLET_BRIEF_FAIL = 0.0

    bits = ["agent-wallet is installed"]
    bits.append("locked" if status.get("locked") else "unlocked")
    pending = int(status.get("pending_requests") or 0)
    if pending:
        bits.append(f"{pending} payment(s) waiting for the human")
    minutes = int(status.get("autolock_minutes") or 0)
    if minutes and not status.get("locked"):
        bits.append(f"auto-locks in {minutes}m idle")
    caps = (status.get("caps") or {}).get("per_tx") or {}
    if caps:
        bits.append("per-tx caps " + ", ".join(f"{k} {v}" for k, v in sorted(caps.items())))
    bits.append("read it with wallet_status/wallet_balances; ask for money with wallet_request_payment "
                "or wallet_pay_url; when a human is needed call wallet_request_approval so the Wallet "
                "pane shows the prompt — never ask for the passphrase in chat")
    return {"context": "[wallet] " + "; ".join(bits) + "."}


#: frozen into each new session's system prompt, so every surface knows the plugin exists — including
#: the ones that do not run the per-turn hook (query mode). Static text on purpose: a value that
#: changes per turn would break prompt caching for the whole conversation.
WALLET_SECTION = (
    "agent-wallet is installed: an approval-gated crypto wallet (XMR, BTC, ETH/POL, USDC). "
    "Read it with wallet_status and wallet_balances. Ask for money with wallet_request_payment, or "
    "price a purchase from a seller's HTTP 402 with wallet_pay_url. A human approves every manual "
    "payment, so when one is waiting call wallet_request_approval — that raises the prompt in the "
    "Wallet pane, where the passphrase is typed. Never ask the user for the wallet passphrase in "
    "chat: it must not enter the conversation."
)


def _cli_setup(sub: argparse.ArgumentParser) -> None:
    wallet_cli.setup(sub)


def _cli(args: argparse.Namespace) -> int:
    return wallet_cli.run(args)


def _surface(ctx, name: str):
    """Look up a public registration hook, or None if this Hermes build does not have it.

    Registration must never raise: one missing optional surface would make the loader report the
    whole plugin as failed to load, taking the tools down with it. Every surface here is documented
    public API — this is about surviving a version that renames or drops an optional one, not about
    reaching into internals.
    """
    method = getattr(ctx, name, None)
    if not callable(method):
        print(f"[agent-wallet] this Hermes build has no ctx.{name}(); that surface is skipped",
              file=sys.stderr)
        return None
    return method


def register(ctx) -> None:
    register_tool = _surface(ctx, "register_tool")
    if register_tool is None:
        raise RuntimeError("agent-wallet needs ctx.register_tool(); this Hermes build has no tools API")
    # One rejected kwarg used to take the whole plugin down with it (every tool vanishes, and the
    # error reads like the plugin is broken rather than one argument). Register what this build
    # accepts, skip the rest, and report per tool.
    registered, skipped = 0, []
    for name, description, handler, schema in TOOL_SPECS:
        call = dict(name=name, toolset="wallet", schema=schema,
                    handler=lambda args, _handler=handler, **_: _handler(**(args or {})),
                    description=description, emoji="wallet")
        while True:
            try:
                register_tool(**call)
                registered += 1
                break
            except TypeError as exc:
                # drop the argument this build does not know (e.g. a renamed `emoji`) and retry once
                bad = str(exc).split("'")[1] if "'" in str(exc) else ""
                if bad and bad in call:
                    call.pop(bad)
                    continue
                skipped.append(f"{name}: {exc}")
                break
            except Exception as exc:  # noqa: BLE001 - one bad tool must not hide the rest
                skipped.append(f"{name}: {type(exc).__name__}: {exc}")
                break
    if skipped:
        print(f"[agent-wallet] registered {registered} tool(s); skipped {len(skipped)}: "
              + "; ".join(skipped)[:400], file=sys.stderr)

    register_hook = _surface(ctx, "register_hook")
    if register_hook is not None:
        register_hook("pre_tool_call", _guard)
        # so a plain chat knows the wallet is installed and what it can do
        register_hook("pre_llm_call", _wallet_brief)

    register_section = _surface(ctx, "register_system_prompt_section")
    if register_section is not None:
        try:
            register_section("agent-wallet", WALLET_SECTION, position="after_memory")
        except (ValueError, TypeError) as exc:      # id collision or an older/newer signature
            print(f"[agent-wallet] could not add the prompt section: {exc}", file=sys.stderr)

    register_cli = _surface(ctx, "register_cli_command")
    if register_cli is not None:
        register_cli(
            "wallet",
            help="Agent wallet: status, daemon control, requests, mandates, receipts",
            setup_fn=_cli_setup,
            handler_fn=_cli,
        )
