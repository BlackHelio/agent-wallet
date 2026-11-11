"""Extend `pane_probe.py` to the two pane surfaces it skipped: Contacts and Sandboxes.

Same method: the installed `dashboard/plugin_api.py` is loaded the way the host loads it, pointed at
a real `walletd` on a scratch home, and then driven over HTTP:

    contacts: save -> listed -> add a second chain -> change an address (passphrase rules)
              -> delete (passphrase) -> a contact saved here blesses a sandbox destination
    sandboxes: create -> fund with the PIN -> list shows the live balance -> bless a payee
               (passphrase rules) -> spend is sandbox_ok -> sweep -> second sweep refused

Run:  python3 scripts/pane_probe_extra.py
"""

from __future__ import annotations

import importlib.util
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PLUGIN = Path(os.environ.get("AGENT_WALLET_PLUGIN") or Path(__file__).resolve().parents[1])
PASS = "probe-passphrase-123"
PIN = "424242"

home = Path(tempfile.mkdtemp(prefix="pane-extra-"))
print(f"  probe home: {home}")
os.environ["AGENT_WALLET_HOME"] = str(home)
sys.path.insert(0, str(PLUGIN))

results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: object = "") -> None:
    results.append((bool(ok), name, str(detail)[:200]))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   {str(detail)[:180]}" if detail else ""))


def load_api():
    """The installed copy, the way the host loads it (module registered before exec)."""
    name = "hermes_dashboard_plugin_agent-wallet"
    spec = importlib.util.spec_from_file_location(name, PLUGIN / "dashboard" / "plugin_api.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def boot():
    env = {**os.environ, "PYTHONPATH": str(PLUGIN), "AGENT_WALLET_HOME": str(home)}
    env.pop("WALLETD_DISABLE_CHAINS", None)
    subprocess.run([sys.executable, "-m", "walletd", "--home", str(home), "init"],
                   env={**env, "WALLETD_PASSPHRASE": PASS, "WALLETD_PIN": PIN},
                   capture_output=True, timeout=180)
    log = open(home / "probe-daemon.log", "w")
    proc = subprocess.Popen([sys.executable, "-m", "walletd", "--home", str(home), "serve"],
                            env=env, stdout=log, stderr=subprocess.STDOUT,
                            start_new_session=True)
    for _ in range(80):
        if (home / "walletd.sock").exists():
            break
        time.sleep(0.25)
    from wallet import client
    for _ in range(40):
        try:
            client.call("status")
            break
        except Exception:
            time.sleep(0.25)
    client.call("unlock", {"passphrase": PASS}, scope=client.OPERATOR)
    settings = client.call("config_get").get("settings") or {}
    chains = list(settings.get("chains_enabled") or []) + ["sim-mainnet"]
    client.call("config_set", {"chains_enabled": chains, "passphrase": PASS}, scope=client.OPERATOR)
    return proc


_DAEMON = None


def kill_daemons():
    """Stop the daemon this probe started, and any other walletd serving this probe home.

    It used to walk /proc, which does not exist on Windows or macOS — the cleanup crashed there and
    left the daemon running. The plugin's own `daemon.stop` identifies the process by its pidfile and
    argv on every platform, and refuses to signal anything that is not this home's walletd.
    """
    try:
        if _DAEMON is not None and _DAEMON.poll() is None:
            _DAEMON.terminate()
            _DAEMON.wait(timeout=5)
    except Exception:  # noqa: BLE001 - cleanup must not mask the probe's own result
        pass
    try:
        from wallet import daemon
        daemon.stop(home)
    except Exception:  # noqa: BLE001
        pass
    time.sleep(0.3)


def main() -> int:
    global _DAEMON
    proc = _DAEMON = boot()
    api = load_api()
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(api.router, prefix="/api/plugins/agent-wallet")
    http = TestClient(app)
    base = "/api/plugins/agent-wallet"

    def get(path, **params):
        resp = http.get(base + path, params=params)
        if resp.status_code >= 400:
            raise RuntimeError(f"{path} -> {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    def post(path, body):
        return http.post(base + path, json=body)

    # ---- contacts -------------------------------------------------------------------------
    A1 = "4A" + "B" * 93
    A2 = "4B" + "C" * 93
    BTC = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"

    # A contact saved from the pane is operator-grade: it blesses the address, so the daemon asks
    # for the passphrase the Contacts section collects. This step sent none and was refused, which
    # failed it and the four checks that depend on the contact existing.
    saved = post("/contact/save", {"name": "Adrian", "kind": "sim-mainnet",
                                   "address": A1, "notes": "probe", "passphrase": PASS})
    row = (saved.json().get("contact") or {})
    check(saved.status_code < 400 and (row.get("addresses") or {}).get("sim-mainnet") == A1,
          "a contact saves and the pane sees its address",
          f"{saved.status_code} {(row.get('addresses') or {})}")

    listed = {c["name"]: c for c in get("/contacts").get("contacts") or []}
    check("Adrian" in listed and (listed["Adrian"].get("addresses") or {}).get("sim-mainnet") == A1,
          "the saved contact appears in GET /contacts",
          list(listed))
    check(listed.get("Adrian", {}).get("created_by") == "operator",
          "the pane's contact form records an operator-grade contact",
          listed.get("Adrian", {}).get("created_by"))

    second = post("/contact/save", {"name": "Adrian", "kind": "bitcoin-mainnet", "address": BTC})
    check(second.status_code >= 400 and "passphrase" in second.text.lower(),
          "adding a chain to an existing contact needs the passphrase too",
          f"{second.status_code} {second.text[:80]}")
    second_ok = post("/contact/save", {"name": "Adrian", "kind": "bitcoin-mainnet",
                                       "address": BTC, "passphrase": PASS})
    addrs = ((second_ok.json() or {}).get("contact") or {}).get("addresses") or {}
    check(second_ok.status_code < 400 and addrs.get("bitcoin-mainnet") == BTC
          and addrs.get("sim-mainnet") == A1,
          "with the passphrase the second chain merges and the first stays",
          f"{second_ok.status_code} {addrs}")

    moved = post("/contact/save", {"name": "Adrian", "kind": "sim-mainnet", "address": A2})
    check(moved.status_code >= 400 and "passphrase" in moved.text.lower(),
          "changing an address without the passphrase is refused",
          f"{moved.status_code} {moved.text[:80]}")
    unchanged = {c["name"]: c for c in get("/contacts").get("contacts") or []}
    check((unchanged.get("Adrian", {}).get("addresses") or {}).get("sim-mainnet") == A1,
          "the refused change left the address alone",
          (unchanged.get("Adrian", {}).get("addresses") or {}).get("sim-mainnet"))

    moved_ok = post("/contact/save", {"name": "Adrian", "kind": "sim-mainnet",
                                      "address": A2, "passphrase": PASS})
    check(moved_ok.status_code < 400
          and ((moved_ok.json() or {}).get("contact") or {}).get("addresses", {}).get("sim-mainnet") == A2,
          "with the passphrase the new address is stored",
          f"{moved_ok.status_code} {moved_ok.text[:80]}")

    no_pass = post("/contact/delete", {"name": "Adrian"})
    check(no_pass.status_code >= 400 and "passphrase" in no_pass.text.lower(),
          "deleting a contact needs the passphrase", f"{no_pass.status_code} {no_pass.text[:80]}")
    still = [c["name"] for c in get("/contacts").get("contacts") or []]
    check("Adrian" in still, "the refused delete left the contact in place", still)

    # ---- a contact saved this way blesses a sandbox destination ---------------------------
    # allowed_payees stays empty: only the saved contact may make this auto-payable.
    made = post("/sandbox/create", {"kind": "sim-mainnet", "asset": "XMR", "budget": "0.01",
                                    "lifetime_hours": 24, "label": "bless"})
    sbx = ((made.json() or {}).get("sandbox") or {})
    sid = sbx.get("sandbox_id", "")
    check(made.status_code < 400 and not sbx.get("allowed_payees"),
          "a sandbox starts with an empty allowlist", f"{made.status_code} {made.text[:200]}")
    funding = (made.json() or {}).get("funding") or {}
    freqs = (funding.get("request") or {}).get("request_id", "")
    fab = funding.get("approval_id") or (funding.get("approval") or {}).get("approval_id", "")
    fpay = (funding.get("request") or {}).get("payee", "")
    fapp = post("/approve", {"request_id": freqs, "approval_id": fab, "pin": PIN,
                             "expect_payee": fpay})
    funded = ((fapp.json() or {}).get("request") or {}) if fapp.status_code < 400 else {}
    check(funded.get("status") == "sent", "funding the sandbox is approved with the PIN",
          f"{fapp.status_code} {funded.get('status') or fapp.text[:80]}")

    rows = {s["sandbox_id"]: s for s in get("/sandboxes").get("sandboxes") or []}
    live = rows.get(sid, {}).get("live_balance")
    check(rows.get(sid, {}).get("enabled") is True and live not in (None, "", "0"),
          "GET /sandboxes shows the funded, live balance",
          f"enabled={rows.get(sid, {}).get('enabled')} live={live} state={rows.get(sid, {}).get('live_balance_state')}")

    # the contact holds A2 after the passphrase change, so a spend to the NAME must be blessed
    spend = post("/request", {"kind": "sim-mainnet", "asset": "XMR", "amount": "0.0001",
                              "payee": "Adrian", "sandbox_id": sid, "why": "contact blessing"})
    srow = spend.json() if spend.status_code < 400 else {}
    code = (srow.get("decision") or {}).get("code", "")
    rule = (srow.get("decision") or {}).get("rule")
    # The PIN is the gate on every payment, so a blessed destination inside a budget is *allowed by
    # policy* and still waits for the human: `needs_pin`, never a silent send.
    check(code == "needs_pin",
          "a spend to a contact saved in the pane is allowed by policy and waits for the PIN",
          f"{spend.status_code} {code} rule={rule} status={(srow.get('request') or {}).get('status')}")
    check(code in ("sandbox_ok", "needs_pin") and rule in ("sandbox", "pin"),
          "the contact blesses it (sandbox/pin rule, not manual_approval)",
          f"{code} rule={rule}")

    # control: an unblessed destination must NOT be sandbox_ok
    loose = post("/request", {"kind": "sim-mainnet", "asset": "XMR", "amount": "0.0001",
                              "payee": "4A" + "E" * 93, "sandbox_id": sid, "why": "control"})
    lrow = loose.json() if loose.status_code < 400 else {}
    lcode = (lrow.get("decision") or {}).get("code", "")
    check(lcode != "sandbox_ok" and (lrow.get("decision") or {}).get("rule") != "sandbox",
          "an unblessed destination is not sandbox_ok",
          f"{lcode} rule={(lrow.get('decision') or {}).get('rule')}")

    # ---- sandboxes: blessing an explicit payee -------------------------------------------
    made2 = post("/sandbox/create", {"kind": "sim-mainnet", "asset": "XMR", "budget": "0.02",
                                     "lifetime_hours": 24, "label": "spend"})
    sbx2 = ((made2.json() or {}).get("sandbox") or {})
    sid2 = sbx2.get("sandbox_id", "")
    f2 = (made2.json() or {}).get("funding") or {}
    r2 = (f2.get("request") or {})
    a2 = f2.get("approval_id") or (f2.get("approval") or {}).get("approval_id", "")
    f2res = post("/approve", {"request_id": r2.get("request_id", ""), "approval_id": a2,
                              "pin": PIN, "expect_payee": r2.get("payee", "")})
    check(f2res.status_code < 400 and ((f2res.json() or {}).get("request") or {}).get("status") == "sent",
          "the second sandbox is funded", f"{f2res.status_code} {f2res.text[:80]}")

    payee = "4B" + "E" * 93
    wide = post("/sandbox/payees", {"sandbox_id": sid2, "allowed_payees": [payee]})
    check(wide.status_code >= 400 and "passphrase" in wide.text.lower(),
          "adding an allowed payee without the passphrase is refused",
          f"{wide.status_code} {wide.text[:80]}")
    rows = {s["sandbox_id"]: s for s in get("/sandboxes").get("sandboxes") or []}
    check(not rows.get(sid2, {}).get("allowed_payees"),
          "the refused change left the allowlist empty", rows.get(sid2, {}).get("allowed_payees"))

    wide_ok = post("/sandbox/payees", {"sandbox_id": sid2, "allowed_payees": [payee],
                                       "passphrase": PASS})
    check(wide_ok.status_code < 400
          and payee in (((wide_ok.json() or {}).get("sandbox") or {}).get("allowed_payees") or []),
          "with the passphrase the payee is blessed",
          f"{wide_ok.status_code} {((wide_ok.json() or {}).get('sandbox') or {}).get('allowed_payees')}")

    hit = post("/request", {"kind": "sim-mainnet", "asset": "XMR", "amount": "0.0001",
                            "payee": payee, "sandbox_id": sid2, "why": "blessed payee"})
    hrow = hit.json() if hit.status_code < 400 else {}
    hcode = (hrow.get("decision") or {}).get("code", "")
    hstatus = (hrow.get("request") or {}).get("status", "")
    check(hcode == "needs_pin" and hstatus == "pending",
          "a spend to a blessed payee is allowed by the budget and still asks for the PIN",
          f"{hit.status_code} {hcode} status={hstatus}")

    # ---- handing a budget over -----------------------------------------------------------
    no_pass = post("/sandbox/unattended", {"sandbox_id": sid2, "on": True, "pin": PIN})
    check(no_pass.status_code >= 400 and "passphrase" in no_pass.text.lower(),
          "arming a budget without the passphrase is refused",
          f"{no_pass.status_code} {no_pass.text[:80]}")
    no_pin = post("/sandbox/unattended", {"sandbox_id": sid2, "on": True, "passphrase": PASS})
    check(no_pin.status_code >= 400 and "pin" in no_pin.text.lower(),
          "arming a budget without the PIN is refused", f"{no_pin.status_code} {no_pin.text[:80]}")
    rows = {s["sandbox_id"]: s for s in get("/sandboxes").get("sandboxes") or []}
    check(not rows.get(sid2, {}).get("unattended"),
          "a refused arm left the budget asking for the PIN", rows.get(sid2, {}).get("unattended"))

    arm = post("/sandbox/unattended", {"sandbox_id": sid2, "on": True, "passphrase": PASS,
                                       "pin": PIN})
    check(arm.status_code < 400 and (arm.json() or {}).get("unattended") is True,
          "with the passphrase and the PIN the budget is handed over",
          f"{arm.status_code} {arm.text[:80]}")
    free = post("/request", {"kind": "sim-mainnet", "asset": "XMR", "amount": "0.0002",
                             "payee": payee, "sandbox_id": sid2, "why": "a shop, unseen"})
    frow2 = free.json() if free.status_code < 400 else {}
    check((frow2.get("request") or {}).get("status") == "sent"
          and (frow2.get("request") or {}).get("authorised_by") == "sandbox",
          "a handed-over budget spends without a PIN",
          f"{free.status_code} {(frow2.get('request') or {}).get('status')} "
          f"by {(frow2.get('request') or {}).get('authorised_by')}")

    dis = post("/sandbox/unattended", {"sandbox_id": sid2, "on": False})
    check(dis.status_code < 400 and (dis.json() or {}).get("unattended") is False,
          "taking the hand-over back is free", f"{dis.status_code} {dis.text[:80]}")
    again = post("/request", {"kind": "sim-mainnet", "asset": "XMR", "amount": "0.0002",
                              "payee": payee, "sandbox_id": sid2, "why": "after the hand-back"})
    arow = again.json() if again.status_code < 400 else {}
    check((arow.get("decision") or {}).get("code") == "needs_pin",
          "with the hand-over back, the same spend asks for the PIN again",
          f"{again.status_code} {(arow.get('decision') or {}).get('code')}")

    # ---- sweep ---------------------------------------------------------------------------
    first = post("/sandbox/sweep", {"sandbox_id": sid2})
    frow = first.json() if first.status_code < 400 else {}
    transfer = (frow.get("transfer") or {}) if isinstance(frow.get("transfer"), dict) else {}
    check(first.status_code < 400 and str(frow.get("swept")) not in ("0", "0.0", "None")
          and frow.get("sent") is True,
          "a sweep returns the remainder home",
          f"{first.status_code} swept={frow.get('swept')} sent={frow.get('sent')} note={frow.get('note')}")

    # If the sweep only filed a card, the pane has to be able to finish it; drive that path so the
    # rest of the assertions run against a swept sandbox and the gap is measured, not assumed.
    if not frow.get("sent") and transfer.get("approval_id"):
        treq = transfer.get("request") or {}
        fin = post("/approve", {"request_id": treq.get("request_id", ""),
                                "approval_id": transfer.get("approval_id", ""), "pin": PIN,
                                "expect_payee": treq.get("payee", "")})
        check(fin.status_code < 400
              and ((fin.json() or {}).get("request") or {}).get("status") == "sent",
              "the sweep's card can be paid with the PIN (a second, manual step)",
              f"{fin.status_code} {(fin.json() or {}).get('request', {}).get('status')}")

    ledger = get("/ledger", limit=200)
    entries = ledger.get("entries") if isinstance(ledger, dict) else ledger
    swept_rows = [e for e in entries or []
                  if e.get("kind") == "sandbox" and e.get("decision") in ("swept", "swept_partial")
                  and (e.get("details") or {}).get("sandbox_id") == sid2]
    check(bool(swept_rows), "the receipt says the sandbox was swept",
          [e.get("decision") for e in swept_rows])

    after = {s["sandbox_id"]: s for s in get("/sandboxes").get("sandboxes") or []}.get(sid2, {})
    check(after.get("enabled") is False or str(after.get("live_balance")) in ("0", "0E-8", "0.0", "None"),
          "the swept sandbox is closed / empty",
          f"enabled={after.get('enabled')} live={after.get('live_balance')}")

    again = post("/sandbox/sweep", {"sandbox_id": sid2})
    arow = again.json() if again.status_code < 400 else {}
    check(again.status_code >= 400 or arow.get("sent") is False or str(arow.get("swept")) in ("0", "0.0"),
          "a second sweep is refused",
          f"{again.status_code} swept={arow.get('swept')} sent={arow.get('sent')} {again.text[:60]}")

    # ---- the switch the docs promise -----------------------------------------------------
    # config.py:36-38 says turning the PIN off is a passphrase-level change in the pane, and it is
    # the only thing between a blessed sandbox spend and `sandbox_ok`.
    cfg = post("/config", {"pin_every_payment": False, "passphrase": PASS})
    check(cfg.status_code < 400,
          "the pane can turn OFF pin-every-payment (config.py says it can)",
          f"{cfg.status_code} {cfg.text[:100]}")
    if cfg.status_code < 400:
        retry = post("/request", {"kind": "sim-mainnet", "asset": "XMR", "amount": "0.0001",
                                  "payee": "Adrian", "sandbox_id": sid, "why": "gate off"})
        rrow = retry.json() if retry.status_code < 400 else {}
        check((rrow.get("decision") or {}).get("code") == "sandbox_ok",
              "with the gate off the blessed spend is sandbox_ok",
              (rrow.get("decision") or {}).get("code"))

    # ---- delete with the passphrase -------------------------------------------------------
    gone = post("/contact/delete", {"name": "Adrian", "passphrase": PASS})
    names = [c["name"] for c in get("/contacts").get("contacts") or []]
    check(gone.status_code < 400 and "Adrian" not in names,
          "with the passphrase the contact is deleted", f"{gone.status_code} {names}")

    failed = [name for ok, name, _ in results if not ok]
    print(f"\n  {len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        kill_daemons()
