#!/usr/bin/env python3
"""A page for watching a spend happen: the payment, the sandbox it came out of, and the gas the chain
still holds above its floor.

Read-only. It talks to the running `walletd` over its own socket with the operator token, so it can
never move money — the only writes in this file are HTTP responses.

    python3 scripts/live_view.py            # http://127.0.0.1:8791/  (loopback; --bind to expose)
    python3 scripts/live_view.py --port 9000 --home /path/to/wallet

Why a separate page instead of a dashboard route: the dashboard sits behind the app's session, and the
point of this one is to be opened on a second screen (or a phone) while a payment is in flight. It shows
three numbers that are otherwise spread across three surfaces — the amount and its confirmations, the
budget it came out of, and whether the chain can still afford to move anything.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from wallet import client  # noqa: E402

CACHE: Dict[str, tuple[float, Any]] = {}
TTL = 1.5          # chain calls; the local state is cheap and read every poll


def _cached(key: str, fn):
    now = time.time()
    hit = CACHE.get(key)
    if hit and now - hit[0] < TTL:
        return hit[1]
    value = fn()
    CACHE[key] = (now, value)
    return value


def _call(method: str, params: Optional[Dict[str, Any]] = None, *, operator: bool = False) -> Any:
    return client.call(method, params or {}, scope=client.OPERATOR if operator else client.AGENT,
                       home=HOME, timeout=20)


def _money(value: Any, places: int = 6) -> str:
    try:
        d = Decimal(str(value))
    except Exception:
        return str(value or "—")
    text = f"{d:.{places}f}".rstrip("0").rstrip(".")
    return text or "0"


def snapshot() -> Dict[str, Any]:
    if FIXTURE:
        with open(FIXTURE, encoding="utf-8") as fh:
            data = json.load(fh)
        data["asof"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        data.setdefault("home", str(HOME))
        return data
    out: Dict[str, Any] = {"asof": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "home": str(HOME)}
    try:
        status = _cached("status", lambda: _call("status"))
        out["wallet"] = {"locked": bool(status.get("locked")),
                         "chains": status.get("chains") or [],
                         "pending": status.get("pending_requests", 0)}
    except Exception as exc:                                    # noqa: BLE001
        out["wallet"] = {"error": f"{type(exc).__name__}: {exc}"}
        return out
    if out["wallet"]["locked"]:
        return out

    # the newest spend that actually reached a chain
    try:
        rows = _cached("requests", lambda: _call("requests_list").get("requests") or [])
        spent = [r for r in rows if r.get("txid") or r.get("tx_hash")]
        spent.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
        row = spent[0] if spent else None
    except Exception:                                            # noqa: BLE001
        row = None
    if row:
        out["spend"] = {
            "amount": _money(row.get("amount")),
            "asset": str(row.get("asset") or "").upper(),
            "kind": row.get("kind") or "",
            "payee": str(row.get("payee") or ""),
            "status": row.get("status") or "",
            "txid": row.get("txid") or row.get("tx_hash") or "",
            "at": row.get("created_at") or "",
            "sandbox_id": row.get("sandbox_id") or "",
        }
        # confirmations are the one number that changes while you watch
        try:
            proof = _cached(f"proof:{out['spend']['txid']}", lambda: _call("verify_payment", {
                "kind": row.get("kind"), "txid": out["spend"]["txid"],
                "address": row.get("address") or row.get("payee") or "",
                "asset": row.get("asset")}))
            out["spend"]["confirmations"] = int(proof.get("confirmations") or 0)
            out["spend"]["settled"] = bool(proof.get("settled"))
        except Exception as exc:                                 # noqa: BLE001
            out["spend"]["proof_error"] = f"{type(exc).__name__}: {exc}"

    # the budget it came out of (the newest one, or the one the spend names)
    try:
        boxes = _cached("sandboxes", lambda: _call("sandboxes_list").get("sandboxes") or [])
        live = [b for b in boxes if b.get("status") in ("active", "armed")]
        wanted = (out.get("spend") or {}).get("sandbox_id")
        pick = next((b for b in live if b.get("sandbox_id") == wanted), None) or \
            (sorted(live, key=lambda b: str(b.get("created_at") or ""), reverse=True)[:1] or [None])[0]
        if pick:
            out["sandbox"] = {
                "id": pick.get("sandbox_id"), "status": pick.get("status"),
                "asset": str(pick.get("asset") or "").upper(), "kind": pick.get("kind"),
                "budget": _money(pick.get("budget")), "spent": _money(pick.get("spent")),
                "remaining": _money(pick.get("remaining")),
                "expires_at": pick.get("expires_at"), "unattended": pick.get("unattended"),
                "payees": len(pick.get("allowed_payees") or []),
            }
    except Exception:                                            # noqa: BLE001
        pass

    # gas: what the chain holds, what it must keep
    try:
        gas = _cached("gas", lambda: _call("gas", operator=True))
        kind = (out.get("spend") or {}).get("kind")
        chains = gas.get("chains") or []
        chosen = next((c for c in chains if c.get("kind") == kind), None) or (chains[:1] or [None])[0]
        if chosen:
            out["gas"] = {k: chosen.get(k) for k in
                          ("kind", "native", "held", "cost", "floor", "headroom", "transfers_left",
                           "state", "reserve", "shortfall", "address")}
    except Exception as exc:                                     # noqa: BLE001
        out["gas"] = {"error": f"{type(exc).__name__}: {exc}"}

    try:
        tail = _cached("ledger", lambda: _call("ledger_tail", {"limit": 1}))
        entries = tail.get("entries") or []
        out["ledger"] = {"seq": (entries[-1] or {}).get("seq") if entries else 0,
                         "count": tail.get("count") or len(entries)}
    except Exception:                                            # noqa: BLE001
        pass
    return out


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>agent-wallet · live</title>
<style>
  :root{--bg:#0b0f14;--card:#131922;--line:#1f2833;--ink:#e8eef5;--muted:#8b9aad;
        --ok:#3ddc97;--warn:#ffb648;--bad:#ff6b6b;--accent:#5aa9ff}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
       font:15px/1.45 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;
       display:flex;flex-direction:column;height:100vh;overflow:hidden}
  header{display:flex;align-items:center;gap:12px;padding:18px 22px 10px}
  header h1{font-size:15px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted);margin:0;font-weight:600}
  header .chain{color:var(--accent);font-weight:600;font-size:13px}
  header .asof{margin-left:auto;color:var(--muted);font:12px ui-monospace,monospace}
  main{flex:1;display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px;padding:0 22px 14px;min-height:0}
  .card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px;
        display:flex;flex-direction:column;min-height:0}
  .label{font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted);font-weight:600}
  .bigcap{font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);margin-top:14px}
  .big{font-size:46px;font-weight:650;letter-spacing:-.02em;margin:2px 0 10px;
       font-variant-numeric:tabular-nums;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .big small{font-size:18px;color:var(--muted);font-weight:500;margin-left:8px;letter-spacing:.02em}
  .pill{display:inline-block;padding:3px 10px;border-radius:999px;font-size:11px;font-weight:650;
        letter-spacing:.06em;text-transform:uppercase;border:1px solid}
  .pill.sent{color:var(--accent);border-color:#2b4a6b;background:#122436}
  .pill.confirmed,.pill.ok{color:var(--ok);border-color:#1e4a38;background:#0f2a20}
  .pill.pending{color:var(--warn);border-color:#5a441c;background:#2a2110}
  .row{display:flex;justify-content:space-between;font-size:13px;color:var(--muted);margin-top:9px}
  .row b{color:var(--ink);font-weight:600;font-variant-numeric:tabular-nums}
  .bar{height:8px;border-radius:6px;background:#0d131b;border:1px solid var(--line);overflow:hidden;margin-top:12px}
  .bar i{display:block;height:100%;background:linear-gradient(90deg,#2f7fd1,#5aa9ff);width:0;transition:width .5s}
  .sub{margin-top:12px;font-size:12.5px;color:var(--muted);line-height:1.5}
  .sub b{color:var(--ink);font-weight:500}
  .sub a{color:var(--accent);text-decoration:none}
  .foot{margin-top:18px;font-size:12px;color:#a9b7c6;
        font-family:ui-monospace,monospace;word-break:break-all;line-height:1.5}
  .foot a{color:var(--accent);text-decoration:underline;text-underline-offset:2px}
  .foot .k{color:var(--ink);font-weight:500}
  .empty{color:#a9b7c6;font-size:12px;margin-top:18px;line-height:1.5;font-family:ui-monospace,monospace}
  .barcap{margin-top:7px;font-size:11px;color:var(--muted);letter-spacing:.04em}
  footer{display:flex;gap:18px;padding:0 22px 14px;color:var(--muted);font:11.5px ui-monospace,monospace}
</style></head>
<body>
<header>
  <h1>agent-wallet · live</h1>
  <span class="chain" id="chain">—</span>
  <span class="asof" id="asof">—</span>
</header>
<main>
  <section class="card">
    <span class="label">the spend</span>
    <div class="bigcap" id="payee">no spend yet</div>
    <div class="big" id="amount">—<small id="asset"></small></div>
    <div><span class="pill pending" id="status">waiting</span></div>
    <div class="sub" id="conf">the first one will appear here as it happens</div>
    <div class="foot" id="hash"></div>
  </section>

  <section class="card">
    <span class="label">the sandbox</span>
    <div class="bigcap">remaining in the budget</div>
    <div class="big" id="remaining">—<small id="sandboxAsset"></small></div>
    <div class="row"><span>spent</span><b id="spent">—</b></div>
    <div class="row"><span>budget</span><b id="budget">—</b></div>
    <div class="bar"><i id="bar"></i></div>
    <div class="barcap" id="barCap"></div>
    <div class="row" style="margin-top:14px"><span id="sandboxStatus">—</span><b id="expires"></b></div>
    <div class="empty" id="sandboxId">no budget yet — the agent asks for one before it can spend</div>
  </section>

  <section class="card">
    <span class="label">the gas</span>
    <div class="bigcap">held on the chain</div>
    <div class="big" id="gasHeld">—<small id="gasNative"></small></div>
    <div class="row"><span>must keep (floor)</span><b id="gasFloor">—</b></div>
    <div class="row"><span>transfers left (est.)</span><b id="gasLeft">—</b></div>
    <div class="sub" id="gasState"></div>
    <div class="foot" id="gasAddr"></div>
  </section>
</main>
<footer><span id="ledger">ledger —</span><span id="locked"></span></footer>

<script>
const $ = (id) => document.getElementById(id);
// Everything interpolated into innerHTML goes through esc(), including quotes: the values are
// daemon-produced hex, decimals, addresses and enum strings, so escaping them fully keeps
// innerHTML safe even if a chain ever returned something unexpected.
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const EXPLORER = { 'base-mainnet': 'https://basescan.org/tx/', 'polygon-mainnet': 'https://polygonscan.com/tx/',
                   'arbitrum-mainnet': 'https://arbiscan.io/tx/' };
const short = (a) => a && a.length > 26 ? a.slice(0, 12) + '…' + a.slice(-10) : (a || '');
const rel = (iso) => { const t = Date.parse(iso); if (!isFinite(t)) return '';
  const d = t - Date.now(); if (d <= 0) return 'expired';
  const h = Math.floor(d / 3600000), m = Math.round((d % 3600000) / 60000);
  return (h >= 1 ? `${h}h ${m}m` : `${m}m`) + ' left'; };
const fmt = (x) => { const n = Number(x); if (!isFinite(n)) return String(x);
  const t = n.toFixed(8).replace(/0+$/, '').replace(/\.$/, ''); return t || '0'; };

async function tick(){
  let d;
  try { d = await (await fetch('data', {cache:'no-store'})).json(); }
  catch(e){ $('locked').textContent = 'viewer cannot reach the daemon'; return; }
  $('asof').textContent = d.asof || '';
  const w = d.wallet || {};
  const _k = (d.spend && d.spend.kind) || (d.gas && d.gas.kind) || (w.chains || [])[0] || '';
  $('chain').textContent = _k ? '· ' + _k : '';
  $('locked').textContent = w.locked ? 'wallet locked — unlock it to see the numbers' : '';
  if (w.locked) return;

  const s = d.spend;
  if (s) {
    $('amount').firstChild.nodeValue = s.amount;
    $('asset').textContent = s.asset;
    $('payee').textContent = 'to ' + (s.payee || '—');
    const conf = s.confirmations || 0;
    const st = $('status');
    if (s.settled) { st.textContent = 'confirmed'; st.className = 'pill confirmed'; }
    else if (conf)  { st.textContent = 'confirming'; st.className = 'pill sent'; }
    else            { st.textContent = 'sent'; st.className = 'pill sent'; }
    $('conf').innerHTML = s.settled
      ? `<b>${conf} confirmations</b> on ${esc(s.kind)}`
      : conf ? `<b>${conf} confirmations</b> so far` : 'awaiting the first confirmation';
    const base = EXPLORER[s.kind] || '';
    $('hash').innerHTML = s.txid
      ? `<span class="k">tx</span> <a href="${base}${esc(s.txid)}" target="_blank">${esc(short(s.txid))}</a> · open in explorer ↗`
      : '<span class="k">tx</span> —';
  } else {
    $('amount').firstChild.nodeValue = '—'; $('asset').textContent = '';
    $('payee').textContent = 'no spend yet';
    const st = $('status'); st.textContent = 'waiting'; st.className = 'pill pending';
    $('conf').innerHTML = 'the first one will appear here as it happens';
    $('hash').innerHTML = '';
  }

  const b = d.sandbox;
  if (b) {
    $('remaining').firstChild.nodeValue = b.remaining;
    $('sandboxAsset').textContent = b.asset;
    $('spent').textContent = b.spent + ' ' + b.asset;
    $('budget').textContent = b.budget + ' ' + b.asset;
    const pct = Math.max(0, Math.min(100, 100 * (Number(b.spent || 0) / (Number(b.budget) || 1))));
    $('bar').style.width = pct.toFixed(1) + '%';
    $('barCap').textContent = 'spent ' + pct.toFixed(0) + '% of the budget';
    $('sandboxStatus').textContent = b.status + (b.unattended ? ' · handed to the agent' : '');
    $('expires').textContent = b.expires_at ? rel(b.expires_at) : '';
    $('sandboxId').textContent = b.id || b.sandbox_id || '';
  } else {
    $('remaining').firstChild.nodeValue = '—'; $('sandboxAsset').textContent = '';
    $('spent').textContent = '—'; $('budget').textContent = '—'; $('bar').style.width = '0';
    $('barCap').textContent = '';
    $('sandboxStatus').textContent = '—'; $('expires').textContent = '';
    $('sandboxId').textContent = 'no budget yet — the agent asks for one before it can spend';
  }

  const g = d.gas;
  if (g && !g.error) {
    $('gasHeld').firstChild.nodeValue = fmt(g.held);
    $('gasNative').textContent = g.native;
    $('gasFloor').textContent = fmt(g.floor) + ' ' + g.native;
    $('gasLeft').textContent = '≈ ' + g.transfers_left;
    $('gasState').innerHTML = g.state === 'low'
      ? `<b style="color:#ff6b6b">below the floor</b> — short ${esc(g.shortfall)} ${esc(g.native)}; the wallet will refuse to send until it is topped up`
      : 'above the floor — spending stops before gas runs out, so a transfer never pays a fee it cannot cover';
    $('gasAddr').innerHTML = g.address ? `<span class="k">gas</span> ${esc(short(g.address))}` : '';
  }

  if (d.ledger) $('ledger').textContent = 'ledger seq ' + (d.ledger.seq ?? '—');
}
tick(); setInterval(tick, 1200);
</script>
</body></html>"""


SNAPSHOT: Dict[str, Any] = {"warming": True}
SNAPSHOT_LOCK = threading.Lock()
SNAPSHOT_AT = 0.0


def refresher(interval: float) -> None:
    """One slow poller, so no HTTP request ever waits on a chain call.

    A `gas` read is an RPC to a public node and takes seconds when the node is slow; serving that
    inline made every browser poll hang and time out. The page reads the last good snapshot instead,
    and `age` tells it how fresh that is.
    """
    global SNAPSHOT, SNAPSHOT_AT
    while True:
        started = time.time()
        try:
            snap = snapshot()
        except Exception as exc:                                  # noqa: BLE001
            snap = {"error": f"{type(exc).__name__}: {exc}", "asof": time.strftime("%FT%TZ", time.gmtime())}
        snap["age"] = round(time.time() - started, 2)
        with SNAPSHOT_LOCK:
            SNAPSHOT = snap
            SNAPSHOT_AT = time.time()
        time.sleep(max(0.2, interval))


class Handler(BaseHTTPRequestHandler):
    server_version = "agent-wallet-live/1"

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:                                     # noqa: N802
        path = self.path.split("?")[0]
        if path in ("/", "/index.html", "/live"):
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        elif path in ("/data", "/data.json"):
            with SNAPSHOT_LOCK:
                snap = dict(SNAPSHOT)
            snap["served_age"] = round(time.time() - SNAPSHOT_AT, 2) if SNAPSHOT_AT else None
            self._send(200, json.dumps(snap, default=str).encode(), "application/json")
        elif path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
        elif path == "/healthz":
            self._send(200, b"ok", "text/plain")
        else:
            self._send(404, b"not found", "text/plain")

    def log_message(self, fmt: str, *args: Any) -> None:           # quiet by default
        if VERBOSE:
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))


def _default_home() -> Path:
    """The wallet home, resolved the same way the CLI resolves it.

    A hardcoded author path here meant the page only ever worked on the machine it was written on.
    """
    if os.environ.get("AGENT_WALLET_HOME"):
        return Path(os.environ["AGENT_WALLET_HOME"]).expanduser()
    if os.environ.get("HERMES_HOME"):
        return Path(os.environ["HERMES_HOME"]).expanduser() / "wallet"
    return Path.home() / ".hermes" / "wallet"


HOME: Path = _default_home()
FIXTURE: Optional[str] = None   # a snapshot-shaped JSON; design checks without a wallet
VERBOSE = False


def main() -> int:
    global HOME, VERBOSE
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8791)
    ap.add_argument("--bind", default="127.0.0.1",
                    help="loopback by default: this page shows payees and balances")
    ap.add_argument("--home", default=None,
                    help="wallet home (default: $AGENT_WALLET_HOME, else $HERMES_HOME/wallet, "
                         "else ~/.hermes/wallet)")
    ap.add_argument("--interval", type=float, default=1.2, help="seconds between refreshes")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--fixture", help="serve this snapshot JSON instead of polling a wallet (design checks only)")
    args = ap.parse_args()
    global FIXTURE
    HOME = Path(args.home).expanduser() if args.home else _default_home()
    FIXTURE = args.fixture
    VERBOSE = args.verbose
    if not (HOME / "walletd.sock").exists():
        print(f"no daemon socket at {HOME}/walletd.sock — start the wallet first", file=sys.stderr)
    threading.Thread(target=refresher, args=(args.interval,), daemon=True).start()
    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    print(f"live view on http://{args.bind}:{args.port}/  (home {HOME})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
