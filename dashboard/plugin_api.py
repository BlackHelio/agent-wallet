"""Backend for the Desktop pane: /api/plugins/agent-wallet/*.

Holds the operator token server-side. The renderer sends a passphrase or an approval id; it never
sees a token and never talks to the socket itself.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

# The web server imports this file by path (spec_from_file_location), so the plugin root is not on
# sys.path the way it is for the package entrypoint. Add exactly our own root, nothing else.
_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
# append, never insert(0): this runs inside the web server's process, and putting our own `wallet`
# and `dashboard` packages first would shadow any app or third-party module with those names.
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.append(str(_PLUGIN_ROOT))

from wallet import client, daemon  # noqa: E402  (path set above)

router = APIRouter()


def _op(method: str, params: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None) -> Dict[str, Any]:
    try:
        kwargs: Dict[str, Any] = {"scope": client.OPERATOR}
        if timeout is not None:
            kwargs["timeout"] = timeout
        return client.call(method, params or {}, **kwargs)
    except client.WalletUnavailable as exc:
        message = str(exc)
        if "not listening" in message or "no token" in message:
            started = daemon.start()
            if started.get("ok"):
                try:
                    return client.call(method, params or {}, scope=client.OPERATOR)
                except client.WalletUnavailable as retry_exc:
                    message = str(retry_exc)
        # The daemon answered, so this is a refusal (bad passphrase, policy, expiry), not an outage -
        # unless it is an internal_error, which means the daemon crashed on the request and saying
        # "bad request" would send the operator hunting in the wrong place.
        if "stale_walletd" in message:
            # 503: the plugin is fine, the thing it talks to is out of date
            raise HTTPException(status_code=503, detail=message) from exc
        code = 500 if "internal_error" in message else 400
        raise HTTPException(status_code=code, detail=_advice(message)) from exc


def _advice(detail: str) -> str:
    """Say the next step that can actually fix the problem.

    `start failed: no_keystore` used to come with "run hermes wallet up", which cannot create a
    keystore — the pane sent the operator in a circle.
    """
    if "no_keystore" in detail:
        return (f"{detail} — this wallet home has no keystore yet; "
                f"run `hermes wallet demo` (or `hermes wallet init`) once to create it")
    if "not listening" in detail:
        return f"{detail} — run `hermes wallet up`"
    return detail


def _ag(method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Read through the agent scope, starting the signer if it is not running yet."""
    try:
        return client.call(method, params or {}, scope=client.AGENT)
    except client.WalletUnavailable as exc:
        detail = str(exc)
        if "not listening" in detail or "no token" in detail:
            started = daemon.start()
            if started.get("ok"):
                try:
                    return client.call(method, params or {}, scope=client.AGENT)
                except client.WalletUnavailable as retry_exc:
                    detail = str(retry_exc)
                    exc = retry_exc
            else:
                detail = f"{detail}; start failed: {started.get('error') or started.get('message') or started}"
        raise HTTPException(status_code=503, detail=_advice(detail)) from exc


class UnlockBody(BaseModel):
    passphrase: Optional[str] = None
    pin: Optional[str] = None          # the six digits; either secret opens the wallet


class ContactBody(BaseModel):
    name: str
    kind: str
    address: str
    notes: str = ""
    passphrase: Optional[str] = None      # changing an existing address needs it


class ContactDelete(BaseModel):
    name: str
    passphrase: Optional[str] = None


class RequestBody(BaseModel):
    kind: str
    asset: str
    amount: str
    payee: str
    memo: str = ""
    why: str = ""
    # the pane's sandbox picker sends this; undeclared fields are dropped silently by Pydantic
    sandbox_id: str = ""
    # a retry of the same submission reuses this; a deliberate second payment gets a new one
    idempotency_key: str = ""


class SandboxBody(BaseModel):
    kind: str
    asset: str = "XMR"
    budget: str
    lifetime_hours: int = 24
    label: str = ""
    allowed_profiles: list[str] = []
    # Empty means "saved contacts only": anything else has to be approved by a human, because the
    # destination is the one thing a prompt-injected agent would change.
    allowed_payees: list[str] = []
    mode: str = "budget"                 # budget | one_time
    self_destruct_seconds: int = 1800


class SandboxRef(BaseModel):
    """A sandbox a route acts on, where there is nothing else to say about it."""
    sandbox_id: str


class SandboxRevoke(BaseModel):
    sandbox_id: str
    sweep: Optional[bool] = None      # default: walletd sweeps the leftover budget home


class ApproveBody(BaseModel):
    request_id: str
    approval_id: str
    confirm_amount: Optional[str] = None
    passphrase: Optional[str] = None      # only for a wallet that runs without a PIN
    # The PIN and the card. `expect_*` is what the pane drew: if the request moved after the numbers
    # were shown, the daemon refuses instead of signing something the human did not see.
    pin: Optional[str] = None
    expect_amount: Optional[str] = None
    expect_payee: Optional[str] = None


class PinBody(BaseModel):
    pin: str
    confirm: Optional[str] = None
    passphrase: Optional[str] = None
    current_pin: Optional[str] = None  # the alternative to the passphrase, once a PIN exists


class PassphraseBody(BaseModel):
    passphrase: str          # the current one
    new_passphrase: str
    confirm: Optional[str] = None


class DenyBody(BaseModel):
    request_id: str
    approval_id: str
    reason: str = ""


class CancelBody(BaseModel):
    request_id: str
    reason: str = ""


class ReceiveBody(BaseModel):
    label: str = ""
    expected: str = ""
    kind: str = ""


class MandateBody(BaseModel):
    mandate_id: str
    passphrase: Optional[str] = None      # resuming/activating needs it; pausing does not


class MandateCreateBody(BaseModel):
    kind: str
    asset: str = "XMR"
    payee: str
    per_tx_cap: str
    period_cap: str
    absolute_cap: Optional[str] = None
    interval: str = "24h"
    expires_at: Optional[str] = None
    label: str = ""
    activate: bool = True
    passphrase: Optional[str] = None


class MandateEditBody(BaseModel):
    mandate_id: str
    per_tx_cap: Optional[str] = None
    period_cap: Optional[str] = None
    absolute_cap: Optional[str] = None
    interval: Optional[str] = None
    expires_at: Optional[str] = None
    label: Optional[str] = None
    passphrase: Optional[str] = None


class MandateEnableBody(BaseModel):
    mandate_id: str
    enabled: bool = True
    passphrase: Optional[str] = None


class MandateUnattendedBody(BaseModel):
    mandate_id: str
    unattended: bool = True
    passphrase: Optional[str] = None      # required to turn it ON (walletd enforces it)
    pin: Optional[str] = None             # ...and so is the PIN: it waives the per-payment gate


class HumanGateBody(BaseModel):
    enabled: bool
    passphrase: Optional[str] = None      # required only when disabling (walletd enforces it)
    confirm: bool = False


@router.get("/profiles")
def profiles() -> Dict[str, Any]:
    """The Hermes profiles that exist on this machine, for the sandbox access list.

    A sandbox can be limited to named profiles; asking the operator to type names invites typos and
    silently-wrong ACLs, so the pane offers the ones that are actually here.
    """
    home = client.wallet_home().parent
    names = {"default"}
    base = home / "profiles"
    if base.is_dir():
        try:
            names.update(child.name for child in base.iterdir()
                         if child.is_dir() and not child.name.startswith("."))
        except OSError:
            pass
    current = os.environ.get("HERMES_PROFILE") or "default"
    names.add(current)
    return {"ok": True, "profiles": sorted(names), "current": current}


@router.get("/nodes")
def nodes() -> Dict[str, Any]:
    """Endpoint health for the chains this wallet has enabled (the Settings page lists them)."""
    return _ag("nodes")


@router.get("/status")
def status() -> Dict[str, Any]:
    try:
        return {"ok": True, "wallet": _ag("status"), "running": True}
    except HTTPException:
        return {"ok": True, "running": False,
                "wallet": {"locked": True, "initialized": (client.wallet_home() / "keystore.json").exists(),
                           "human_gate": True, "config_signed": True, "config_tampered": False,
                           "chains": [], "assets": [], "pending_requests": 0, "active_mandates": 0}}


@router.post("/start")
def start() -> Dict[str, Any]:
    return daemon.start()


@router.post("/unlock")
def unlock(body: UnlockBody) -> Dict[str, Any]:
    if body.pin:
        return _op("unlock", {"pin": body.pin})
    return _op("unlock", {"passphrase": body.passphrase or ""})


@router.post("/lock")
def lock() -> Dict[str, Any]:
    return _op("lock")


@router.post("/request")
def request_payment(body: RequestBody) -> Dict[str, Any]:
    """File a request from the UI (same agent scope as the tools, so the same policy applies)."""
    params: Dict[str, Any] = {"kind": body.kind, "asset": body.asset, "amount": body.amount,
                              "payee": body.payee, "memo": body.memo, "why": body.why,
                              "profile": client.current_profile()}
    if body.sandbox_id:
        params["sandbox_id"] = body.sandbox_id
    if body.idempotency_key:
        # the pane generates one per submission so a transport retry cannot file the same payment
        # twice; dropping it here made that guard socket-only
        params["idempotency_key"] = body.idempotency_key
    return _ag("request_tx", params)


@router.get("/requests")
def requests() -> Dict[str, Any]:
    return {"ok": True, **_ag("requests_list")}


@router.post("/cancel")
def cancel(body: CancelBody) -> Dict[str, Any]:
    """Withdraw a pending request from the pane (same agent scope as the tool)."""
    return _ag("cancel_request", {"request_id": body.request_id, "reason": body.reason})


@router.post("/approve")
def approve(body: ApproveBody) -> Dict[str, Any]:
    params: Dict[str, Any] = {"request_id": body.request_id, "approval_id": body.approval_id}
    if body.confirm_amount is not None:
        params["confirm_amount"] = body.confirm_amount
    for field in ("passphrase", "pin", "expect_amount", "expect_payee"):
        value = getattr(body, field)
        if value is not None:
            params[field] = value
    return _op("approve", params)


class DepositBody(BaseModel):
    label: str = ""
    kind: str = ""
    auto_merge: bool = True


class DepositMerge(BaseModel):
    deposit_id: str


class DepositRef(BaseModel):
    deposit_id: str
    auto_merge: bool = True


@router.get("/deposits")
def deposits() -> Dict[str, Any]:
    """Deposit addresses and their live balances. Read-only, agent scope: it is our own money."""
    return _ag("deposits_list")


@router.post("/deposits/create")
def deposits_create(body: DepositBody) -> Dict[str, Any]:
    """Operator: open a named receive account whose balance merges into the main wallet."""
    return _op("deposit_create", {"label": body.label, "kind": body.kind,
                                  "auto_merge": body.auto_merge})


@router.post("/deposits/merge")
def deposits_merge(body: DepositMerge) -> Dict[str, Any]:
    """Operator: merge one deposit account into the main account now."""
    return _op("deposit_merge", {"deposit_id": body.deposit_id})


@router.post("/deposits/auto")
def deposits_auto(body: DepositRef) -> Dict[str, Any]:
    """Operator: arm or disarm automatic merging for one deposit address."""
    return _op("deposit_set_auto", {"deposit_id": body.deposit_id, "auto_merge": body.auto_merge})


@router.post("/orders/merge")
def orders_merge() -> Dict[str, Any]:
    """Operator: sweep paid order accounts into the main balance (the reaper also does this)."""
    return _op("orders_merge")


@router.get("/pin/status")
def pin_status() -> Dict[str, Any]:
    """Whether a PIN is set, and whether it is the gate. Readable while locked (set = unknown).

    Operator scope — a local read that must not be answerable from the agent token — but a wallet
    that is *locked* is the case this route exists for, not a failure: report the "locked" refusal
    as an unlocked-unknown status line instead of letting `_op` turn it into HTTP 400.
    """
    try:
        return _op("pin_status")
    except HTTPException as exc:
        if exc.status_code == 400 and "locked" in str(exc.detail):
            return {"ok": True, "locked": True, "set": None, "unlocks": None, "required": None}
        raise


@router.post("/pin/set")
def pin_set(body: PinBody) -> Dict[str, Any]:
    """Set or replace the six-digit PIN. Costs the passphrase — or, once a PIN exists, the current PIN:
    a six-digit secret that opens the wallet is a credential in its own right, and demanding a passphrase
    the operator may not have would trap them."""
    return _op("set_pin", {"pin": body.pin,
                           "confirm": body.confirm if body.confirm is not None else body.pin,
                           "passphrase": body.passphrase or "",
                           "current_pin": body.current_pin or ""})


@router.post("/passphrase/change")
def passphrase_change(body: PassphraseBody) -> Dict[str, Any]:
    """Re-seal the keystore under a new passphrase. Needs the current one; changes nothing else."""
    return _op("change_passphrase", {
        "passphrase": body.passphrase,
        "new_passphrase": body.new_passphrase,
        "confirm": body.confirm if body.confirm is not None else body.new_passphrase,
    })


@router.post("/deny")
def deny(body: DenyBody) -> Dict[str, Any]:
    return _op("deny", {"request_id": body.request_id, "approval_id": body.approval_id,
                        "reason": body.reason})


@router.get("/mandates")
def mandates() -> Dict[str, Any]:
    """`{ok, mandates}`, because the poll reads `mandates.mandates`.

    The daemon's key for this list *is* `mandates`; it used to leave here bare, so the only reader
    was left to guess and a daemon that answered `{ok, rows}` would empty the section silently.
    Spelled out here the way /requests and /incoming already are.
    """
    result = _ag("mandates_list")
    return {"ok": True, **result, "mandates": result.get("mandates") or []}


@router.post("/mandates/create")
def mandate_create(body: MandateCreateBody) -> Dict[str, Any]:
    """Create an autopay directly, as the operator (passphrase required by walletd)."""
    params = {k: v for k, v in body.model_dump().items() if v is not None}
    if not params.get("absolute_cap"):
        params["absolute_cap"] = body.period_cap
    return _op("mandates_create", params)


@router.post("/mandates/update")
def mandate_update(body: MandateEditBody) -> Dict[str, Any]:
    params = {k: v for k, v in body.model_dump().items() if v is not None}
    return _op("mandates_update", params)


@router.post("/mandates/enable")
def mandate_enable(body: MandateEnableBody) -> Dict[str, Any]:
    """Pause (no passphrase) or resume (passphrase, because it restores spending power)."""
    params = {k: v for k, v in body.model_dump().items() if v is not None}
    return _op("mandates_set_enabled", params)


@router.post("/mandates/unattended")
def mandate_unattended(body: MandateUnattendedBody) -> Dict[str, Any]:
    """The one setting that removes the PIN in front of a payment: passphrase + PIN to turn on,
    free to turn off (removing authority never needs authority)."""
    params = {k: v for k, v in body.model_dump().items() if v is not None}
    return _op("mandates_set_unattended", params)


@router.post("/mandates/delete")
def mandate_delete(body: MandateBody) -> Dict[str, Any]:
    params = {k: v for k, v in body.model_dump().items() if v is not None}
    return _op("mandates_delete", params)


@router.post("/mandates/activate")
def activate(body: MandateBody) -> Dict[str, Any]:
    params = {"mandate_id": body.mandate_id}
    if body.passphrase is not None:
        params["passphrase"] = body.passphrase
    return _op("mandates_activate", params)


@router.post("/mandates/revoke")
def revoke(body: MandateBody) -> Dict[str, Any]:
    """Turning an autopay off is an operator action, and the passphrase the pane sends must survive
    the trip - it was being dropped, so revoking from the pane always failed with passphrase_required."""
    params: Dict[str, Any] = {"mandate_id": body.mandate_id}
    if body.passphrase:
        params["passphrase"] = body.passphrase
    return _op("mandates_revoke", params)


@router.get("/balances")
def balances(fresh: int = 0) -> Dict[str, Any]:
    """`fresh=1` skips the 60 s cache — the pane's Refresh button, and a human waiting for a number."""
    return _ag("balances", {"fresh": bool(fresh)})


class KeystoreBody(BaseModel):
    passphrase: str


class InitBody(BaseModel):
    kind: str = "monero-mainnet"


@router.post("/keystore/create")
def keystore_create(body: KeystoreBody) -> Dict[str, Any]:
    """First run: no keystore exists yet, so there is nothing to unlock. Create it here."""
    return _op("create_keystore", {"passphrase": body.passphrase})


@router.post("/wallet/init")
def wallet_init(body: InitBody) -> Dict[str, Any]:
    """Create the chain wallet if it does not exist (keys generated by the signer)."""
    return _op("wallet_init", {"kind": body.kind})


@router.get("/contacts")
def contacts() -> Dict[str, Any]:
    return _ag("contacts_list")


@router.post("/contact/save")
def contact_save(body: ContactBody) -> Dict[str, Any]:
    params: Dict[str, Any] = {"name": body.name, "addresses": {body.kind: body.address},
                              "notes": body.notes}
    if body.passphrase:
        params["passphrase"] = body.passphrase
    # Operator scope, because a contact saved by a *human* is what may bless a destination later
    # (`created_by: operator`). On the agent token the same act recorded `created_by: agent`, which
    # blesses nothing — so the GUI and the CLI disagreed about whether Adrian was trusted.
    return _op("contact_save", params)


@router.post("/contact/delete")
def contact_delete(body: ContactDelete) -> Dict[str, Any]:
    params: Dict[str, Any] = {"name": body.name}
    if body.passphrase:
        params["passphrase"] = body.passphrase
    return _op("contact_delete", params)


@router.get("/sandboxes")
def sandboxes() -> Dict[str, Any]:
    return _ag("sandboxes_list")


@router.post("/sandbox/create")
def sandbox_create(body: SandboxBody) -> Dict[str, Any]:
    """Operator action: carve a capped sub-wallet; the funding transfer still needs approval."""
    return _op("sandbox_create", {"kind": body.kind, "asset": body.asset, "budget": body.budget,
                                  "lifetime_hours": body.lifetime_hours, "label": body.label,
                                  "allowed_profiles": body.allowed_profiles, "mode": body.mode,
                                  "allowed_payees": body.allowed_payees,
                                  "self_destruct_seconds": body.self_destruct_seconds})


class SandboxProfiles(BaseModel):
    sandbox_id: str
    allowed_profiles: list[str] = []
    passphrase: Optional[str] = None      # required by the daemon to widen the list


@router.post("/sandbox/profiles")
def sandbox_profiles(body: SandboxProfiles) -> Dict[str, Any]:
    """Operator: restrict a sandbox to named Hermes profiles (empty list = all of them)."""
    return _op("sandbox_set_profiles", {"sandbox_id": body.sandbox_id,
                                        "allowed_profiles": body.allowed_profiles,
                                        "passphrase": body.passphrase})


class SandboxPayees(BaseModel):
    sandbox_id: str
    allowed_payees: list[str] = []
    passphrase: Optional[str] = None      # required to add an address, free to remove one


@router.post("/sandbox/payees")
def sandbox_payees(body: SandboxPayees) -> Dict[str, Any]:
    """Operator: change which addresses a budget may pay unattended.

    Widening costs the passphrase (the CLI's `sandbox payees` does the same); the pane could only set
    them at creation, so a budget made by an agent could never be blessed afterwards.
    """
    return _op("sandbox_set_payees", {"sandbox_id": body.sandbox_id,
                                      "allowed_payees": body.allowed_payees,
                                      "passphrase": body.passphrase})


class SandboxArm(BaseModel):
    sandbox_id: str
    passphrase: Optional[str] = None      # required by the daemon: arming grants spending room


@router.post("/sandbox/arm")
def sandbox_arm(body: SandboxArm) -> Dict[str, Any]:
    """Operator: switch on a budget the agent proposed on a chain with nothing to fund."""
    params: Dict[str, Any] = {"sandbox_id": body.sandbox_id}
    if body.passphrase:
        params["passphrase"] = body.passphrase
    return _op("sandbox_arm", params)


class SandboxUnattended(BaseModel):
    sandbox_id: str
    on: bool = True
    passphrase: Optional[str] = None      # the daemon also wants the PIN; both are required to arm
    pin: Optional[str] = None


@router.post("/sandbox/unattended")
def sandbox_unattended(body: SandboxUnattended) -> Dict[str, Any]:
    """Operator: hand this budget to the agent, or take it back.

    On is the strongest thing the pane can grant short of unlocking the wallet: the agent may pay any
    address from this budget until the money or the clock runs out. It costs the passphrase and the
    PIN, and it never touches the main account. Off is free.
    """
    params: Dict[str, Any] = {"sandbox_id": body.sandbox_id, "on": body.on}
    if body.passphrase:
        params["passphrase"] = body.passphrase
    if body.pin:
        params["pin"] = body.pin
    return _op("sandbox_set_unattended", params)


@router.post("/sandbox/reap")
def sandbox_reap() -> Dict[str, Any]:
    """Operator: run the reaper now (expired budgets, one-time teardowns)."""
    return _op("reap_sandboxes", {})


@router.post("/sandbox/sweep")
def sandbox_sweep(body: SandboxRef) -> Dict[str, Any]:
    """Return the unused budget to the main account and close the sandbox."""
    return _op("sandbox_sweep", {"sandbox_id": body.sandbox_id,
                                 "profile": client.current_profile()})


@router.post("/sandbox/destroy")
def sandbox_destroy(body: SandboxRef) -> Dict[str, Any]:
    """Operator: destroy a one-time sandbox now. Same fund guards as the automatic reaper."""
    return _op("sandbox_destroy", {"sandbox_id": body.sandbox_id, "reason": "manual pane"})


@router.post("/sandbox/revoke")
def sandbox_revoke(body: SandboxRevoke) -> Dict[str, Any]:
    params = {"sandbox_id": body.sandbox_id}
    if getattr(body, "sweep", None) is not None:
        params["sweep"] = body.sweep
    return _op("sandbox_revoke", params)


class ArchiveBody(BaseModel):
    passphrase: Optional[str] = None


@router.post("/signer/install")
def signer_install() -> Dict[str, Any]:
    """First run on a machine that has no signer binary: download the pinned release."""
    return _op("signer_install", {}, timeout=900)


@router.get("/ledger/archives")
def ledger_archives() -> Dict[str, Any]:
    """Archived receipt chains, so 'clear history' is a view change and not a data loss."""
    home = client.wallet_home()
    rows = []
    for path in sorted(home.glob("ledger-*.jsonl")):
        try:
            count = sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
        except OSError:
            count = 0
        rows.append({"file": path.name, "entries": count, "bytes": path.stat().st_size})
    return {"ok": True, "archives": rows}


@router.post("/ledger/archive")
def ledger_archive(body: ArchiveBody) -> Dict[str, Any]:
    """Clear the view of history: archive the chain and start a new one.

    No passphrase: the archived chain stays on disk and still verifies, so this cannot destroy
    evidence — it only changes what the pane shows.
    """
    return _op("ledger_archive", {"passphrase": body.passphrase} if body.passphrase else {})


@router.get("/ledger")
def ledger(limit: int = 20) -> Dict[str, Any]:
    return _ag("ledger_tail", {"limit": max(1, min(limit, 200))})


@router.get("/ledger.csv")
def ledger_csv(since: str = "", until: str = "") -> PlainTextResponse:
    """Every receipt as CSV, for a spreadsheet or an accountant.

    Amounts leave as the strings the ledger stored, so nothing is rounded on the way out.
    """
    payload = _ag("ledger_csv", {"since": since, "until": until})
    return PlainTextResponse(payload.get("csv", ""), media_type="text/csv")


@router.post("/receive")
def receive(body: ReceiveBody) -> Dict[str, Any]:
    """A fresh subaddress for one incoming order (the pane's Receive button)."""
    params: Dict[str, Any] = {"label": body.label}
    if body.expected:
        params["expected"] = body.expected
    if body.kind:
        params["kind"] = body.kind
    return _ag("receive_address", params)


@router.get("/incoming")
def incoming() -> Dict[str, Any]:
    """Orders opened with receive_address(), joined to what actually arrived on each subaddress."""
    return {"ok": True, **_ag("incoming")}


@router.get("/spend")
def spend(since: str = "", until: str = "") -> Dict[str, Any]:
    """Spend per asset and per payee inside a window (`30d`, `12h`, or an ISO timestamp).

    No window means "today", because the pane labels this number "spent today" and compares it to
    the daily cap — which is enforced from local-midnight UTC. Asking for a longer history is
    explicit (`?since=30d` or `?since=1970-01-01`).
    """
    window_start = since or datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
    return _ag("spend_totals", {"since": window_start, "until": until})


@router.post("/verify")
def verify() -> Dict[str, Any]:
    return _op("ledger_verify")


@router.post("/config")
def config_set(body: Dict[str, Any]) -> Dict[str, Any]:
    """Operator settings change: validated, re-signed and receipted inside walletd."""
    return _op("config_set", body)


@router.get("/config")
def config() -> Dict[str, Any]:
    return _ag("config_get")


@router.post("/prompt/clear")
def prompt_clear() -> Dict[str, Any]:
    """Dismiss the agent's prompt without acting on it."""
    return _op("clear_operator_prompt", {})


@router.post("/human-gate")
def human_gate(body: HumanGateBody) -> Dict[str, Any]:
    return _op("set_human_gate", {"enabled": body.enabled, "passphrase": body.passphrase,
                                  "confirm": body.confirm})
