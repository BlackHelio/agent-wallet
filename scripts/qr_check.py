#!/usr/bin/env python3
"""Cross-check the pane's inline QR encoder against an independent implementation.

The pane cannot import a QR library (it ships as one file, uncompiled), so it carries its own
encoder — byte mode, error correction L, versions 1-6. That code is the only thing between a user and
an unscannable deposit code, so it is compared cell-by-cell against `qrcode` (python), a different
implementation of the same standard.

Both sides are pinned to the same version, ECC level and mask, which leaves no degree of freedom: any
difference is a bug in one of them.

    python3 scripts/qr_check.py [plugin.js]        (needs the `qrcode` package)

Exit 0 = every vector matches. Needs `node` and the `qrcode` package.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLUGIN = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO / "desktop" / "plugin.js"

#: Real payloads this wallet draws: a bare Monero address, the same with the URI prefix, a long one
#: that must NOT fit (the pane falls back to the address), plus the short chains.
VECTORS = [
    "57" + "A" * 93,                                    # 95 bytes: a Monero address
    "monero:" + "57" + "A" * 93,                        # 102: with the URI scheme
    "bitcoin:bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4?amount=0.002",
    "ripple:rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh?amount=10000000",
    "monero:57" + "A" * 93 + "?tx_amount=0.001",        # 123: over v5-L, still inside v6
    "x" * 140,                                          # 140: beyond the encoder, must be refused
]

#: (account, amount) → the URI a scanner must receive. Expectations are written by hand from the
#: chain's own spec, not from the code under test.
URI_CASES = [
    ({"kind": "monero-mainnet", "address": "4A" + "b" * 93, "asset": "XMR"}, "0.001",
     "monero:4A" + "b" * 93 + "?tx_amount=0.001"),
    ({"kind": "monero-mainnet", "address": "4A" + "b" * 93, "asset": "XMR"}, "",
     "monero:4A" + "b" * 93),
    ({"kind": "bitcoin-mainnet", "address": "bc1qtest", "asset": "BTC"}, "0.002",
     "bitcoin:bc1qtest?amount=0.002"),
    # XRPL carries drops: 10 XRP is 10,000,000
    ({"kind": "xrp-mainnet", "address": "rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh", "asset": "XRP"}, "10",
     "ripple:rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh?amount=10000000"),
    # A token row must encode a *transfer*, not a native send: `ethereum:<wallet>` would fund the
    # wallet with ETH and deposit the wrong asset.
    ({"kind": "base-mainnet", "address": "0xAbC0000000000000000000000000000000000001", "asset": "USDC",
      "tokens": {"USDC": {"address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "decimals": 6}},
      "chain_id": 8453}, "10",
     "ethereum:0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913@8453/transfer"
     "?address=0xAbC0000000000000000000000000000000000001&uint256=10000000"),
    ({"kind": "base-mainnet", "address": "0xAbC0000000000000000000000000000000000001", "asset": "USDC",
      "tokens": {"USDC": {"address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "decimals": 6}},
      "chain_id": 8453}, "",
     "ethereum:0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913@8453/transfer"
     "?address=0xAbC0000000000000000000000000000000000001"),
    # no contract known: draw nothing at all rather than a code for the wrong asset
    ({"kind": "base-mainnet", "address": "0xAbC0000000000000000000000000000000000001", "asset": "USDC"}, "",
     ""),
    ({"kind": "base-mainnet", "address": "0xAbC0000000000000000000000000000000000001", "asset": "ETH",
      "chain_id": 8453}, "0.01",
     "ethereum:0xAbC0000000000000000000000000000000000001@8453?value=10000000000000000"),
    ({"kind": "sim-mainnet", "address": "simaddr", "asset": "XMR"}, "1",
     "simaddr"),
]

DRIVER = """
const mod = await import(process.env.MOD_URL)
const vectors = JSON.parse(process.env.VECTORS)
const accounts = JSON.parse(process.env.URIS)
const matrices = []
for (const [i, text] of vectors.entries()) {
  for (let mask = 0; mask < 8; mask++) {
    const m = mod.qrEncode(text, mask)
    matrices.push({ i, mask, text, matrix: m ? m.map((row) => row.join('')) : null })
  }
}
const uris = accounts.map(({ account, amount }) => mod.paymentUri(account, amount))
console.log(JSON.stringify({ matrices, uris }))
"""


def build_node_harness() -> Path:
    """A copy of the pane with the app's SDK/react imports rewritten to stubs (same trick as the
    render check), so the module imports outside Hermes."""
    tmp = Path(tempfile.mkdtemp(prefix="qr-check-"))
    (tmp / "sdk.mjs").write_text(
        "export const PALETTE_AREA='palette'\n"
        "export const ROUTES_AREA='routes'\n"
        "export const SIDEBAR_NAV_AREA='sidebar.nav'\n"
        "export const STATUSBAR_AREAS={left:'',right:''}\n"
        "export const TITLEBAR_AREAS={left:'',center:'',right:''}\n"
        "export const host={notify:()=>{},navigate:()=>{},request:async()=>({}),state:{gateway:{get:()=>'open'}}}\n"
        "export const useValue=(a)=>(a&&typeof a.get==='function'?a.get():a)\n"
    )
    (tmp / "react.mjs").write_text(
        "export function createElement() { return null }\n"
        "export function useState(v) { return [typeof v === 'function' ? v() : v, () => {}] }\n"
        "export function useEffect() {}\n"
        "export function useCallback(fn) { return fn }\n"
        "export function useMemo(fn) { return fn() }\n"
        "export function useRef(v) { return { current: v } }\n"
    )
    source = PLUGIN.read_text(encoding="utf-8")
    source = (source
              .replace("from '@hermes/plugin-sdk'", f"from '{(tmp / 'sdk.mjs').as_uri()}'")
              .replace("from 'react/jsx-runtime'", f"from '{(tmp / 'react.mjs').as_uri()}'")
              .replace("from 'react'", f"from '{(tmp / 'react.mjs').as_uri()}'"))
    (tmp / "plugin.mjs").write_text(source, encoding="utf-8")
    (tmp / "driver.mjs").write_text(DRIVER, encoding="utf-8")
    return tmp


def reference(text: str, mask: int, version: int):
    """The same symbol from the `qrcode` package: byte mode, ECC L, this version, this mask."""
    import qrcode
    from qrcode.constants import ERROR_CORRECT_L
    from qrcode.util import MODE_8BIT_BYTE, QRData
    qr = qrcode.QRCode(version=version, error_correction=ERROR_CORRECT_L, box_size=1, border=0,
                       mask_pattern=mask)
    # Force byte mode. Without this the reference happily encodes "57AAAA…" as *alphanumeric*, which
    # is legal and more compact — a different symbol for the same text, and not what the pane draws.
    qr.add_data(QRData(text.encode("utf-8"), mode=MODE_8BIT_BYTE))
    qr.make(fit=False)
    return qr.get_matrix()


def main() -> int:
    if shutil.which("node") is None:
        print("qr-check: node is not available", file=sys.stderr)
        return 2
    try:
        import qrcode  # noqa: F401
    except ImportError:
        print("qr-check: the `qrcode` package is not importable (PYTHONPATH?)", file=sys.stderr)
        return 2

    tmp = build_node_harness()
    env = {"MOD_URL": (tmp / "plugin.mjs").as_uri(),
           "VECTORS": json.dumps(VECTORS),
           "URIS": json.dumps([{"account": a, "amount": m} for a, m, _ in URI_CASES]),
           "PATH": "/usr/local/bin:/usr/bin:/bin"}
    try:
        proc = subprocess.run(["node", str(tmp / "driver.mjs")], capture_output=True, text=True,
                              env=env, timeout=120)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if proc.returncode != 0:
        print(proc.stderr[-2000:], file=sys.stderr)
        return 2
    payload = json.loads(proc.stdout)
    rows = payload["matrices"]

    failures = 0
    checked = 0
    for row in rows:
        text = row["text"]
        if row["matrix"] is None:
            # too long for the pane's versions: the caller must fall back, never truncate
            print(f"  skip  {len(text):>3}B mask {row['mask']}  (refused, as designed)")
            continue
        version = (len(row["matrix"]) - 17) // 4
        ref = reference(text, row["mask"], version)
        checked += 1
        if len(ref) != len(row["matrix"]):
            print(f"  FAIL  {len(text):>3}B v{version} mask {row['mask']}: size {len(ref)} vs {len(row['matrix'])}")
            failures += 1
            continue
        diff = sum(1 for y in range(len(ref)) for x in range(len(ref)) if int(ref[y][x]) != int(row["matrix"][y][x]))
        if diff:
            print(f"  FAIL  {len(text):>3}B v{version} mask {row['mask']}: {diff} of {len(ref) ** 2} modules differ")
            failures += 1
        else:
            print(f"  ok    {len(text):>3}B v{version} mask {row['mask']}  ({len(ref)}×{len(ref)})")

    # ---- the deposit URIs: the amount must be in the chain's own unit --------------------------
    for (account, amount, expected), got in zip(URI_CASES, payload["uris"]):
        checked += 1
        if got != expected:
            failures += 1
            print(f"  FAIL  uri {account['kind']} {account['asset']} amount={amount!r}\n"
                  f"        expected {expected}\n        got      {got}")
        else:
            print(f"  ok    uri {account['kind']:<18} {account['asset']:<4} {amount or '-':<8} {got[:48]}")

    print(f"\nqr-check: {checked} check(s), {failures} mismatch(es)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
