# Performance: what was slow, what fixed it, and why it isn't Rust

Numbers from `scripts/bench.py` against the live daemon and the real wallet home
(321 receipts at the time of the first run, 4 chains live, Monero signer up). Re-run it after any
change that could plausibly touch latency; it prints a median per operation.

## Before → after

| Operation | Before | After | Why it was slow |
|---|---|---|---|
| `chain_status` (CLI `chains`, pane) | **1871 ms** | **0.44 ms** | It asked every adapter for a node height, on every call, while holding the daemon's single mutex — so every other RPC waited behind it too |
| `spend_totals` (pane, `wallet spend`) | **63 ms** | **0.65 ms** | Re-parsed every receipt and re-summed per asset *and* per payee on each call |
| `ledger.totals()` at 40 000 receipts | **115 ms** | **~0 ms warm** | Same: full scan per call. Caps are evaluated per payment, so this was on the payment path |
| `ledger.last()` | O(n) per call | O(1) | `append()` calls `last()` to chain the hash — building N receipts was O(N²); 50 000 took minutes |
| `ledger_tail(40)` | 2.3 ms | 0.9 ms | Materialised and parsed the whole file to take 40 rows |
| `GET /incoming` | 12.4 ms | 6.4 ms | Rebuilt order rows from a full receipt scan |
| `balances`, cache expired | **3260 ms** | **1873 ms** (fresh) / **6 ms** (poll) | Four chains read one after another, each a network round trip, all inside the daemon lock |
| pane idle cost | 1 poll / 3 s, always | 1 poll / 5 s, paused when the window is hidden | Six HTTP round trips per tick, forever, even minimised |
| `GET /status` | 0.78 ms | 0.66 ms | It read `addresses.json` from disk on every poll |

Unchanged because they are already cheap, or deliberately not optimised:

| Operation | Time | Note |
|---|---|---|
| `request_tx` (policy + plan, no network) | 1.2 ms | |
| `request + approve`, no network | 76 ms | **70 ms of that is scrypt** |
| `keystore.open_sealed` (scrypt n=2¹⁵) | 71 ms | The cost is the security property. Speeding it up weakens the keystore |
| `eth-account` EIP-1559 signing | 2.6 ms | |
| `ledger.append` (write + fsync) | 0.97 ms | Durability, not overhead |
| `GET /status /balances /requests /sandboxes` | 2–3 ms | |

## What changed, in code

- `walletd/ledger.py`: entries are parsed once and kept in memory (`_load_entries`,
  `_load_archives`), `append()` extends that cache instead of re-reading the file, `last()`/`tail()`
  are O(1), and `totals()` memoises per argument tuple with a generation counter that any write
  bumps. Archiving replaces the `Ledger` object, so its cache starts clean.
- `walletd/state.py`: `chain_status()` memoises for 20 s (and is invalidated when the adapter set is
  rebuilt or the wallet locks), `_cached_addresses()` keeps the address map in memory instead of
  reading a file per `/status`, and `_cache_addresses()` drops that cache after a write.
- `walletd/state.py` (`balances`): the four live chains are independent HTTP calls, so they are read
  in a thread pool instead of one after another (3.26 s → 1.87 s, bounded by the slowest chain), the
  cache lives for 60 s instead of 10 s, and `fresh=1` (the pane's Refresh button, or any human
  waiting) skips it. A 10 s cache under a 5 s poll meant every second tick paid the full cost.
- `desktop/plugin.js`: poll every 5 s, skip the tick entirely while the window is hidden, and the
  Refresh button asks for `fresh=1` while the poll never does.

The lock matters as much as the timings: a 1.9 s `chain_status` and a 3.3 s `balances` both held the
daemon's single mutex, so a pane polling `/status` during either waited — this is what produced the
30 s client timeouts and the plugin being dropped from the app earlier. Caching and parallelism
removed the long critical sections without touching the "one writer at a time" rule. A *fresh*
balance read is still ~1.9 s under the lock; that is the cost of asking four chains at once and is
now something a human asks for, not something a poll triggers.

## Why not Rust

The measurements decide it:

1. **The payment path is 92% deliberate KDF.** `request + approve` is 76 ms; scrypt is 70 ms of it.
   A Rust scrypt would be maybe 2× faster — i.e. ~35 ms — while the whole point of those 70 ms is to
   make guessing the passphrase expensive. Making it cheaper is a downgrade disguised as an
   optimisation. If the wait ever matters, the answer is a cheaper *design* (unlock once, auto-lock),
   not a faster KDF.
2. **Signing is noise.** EIP-1559 signing is 2.6 ms; a Monero `plan_transfer` + `broadcast` is
   hundreds of milliseconds to seconds of network. Replacing `embit`/`eth-account` with
   `libsecp256k1` bindings would save single-digit milliseconds inside a multi-hundred-millisecond
   flow.
3. **The two real bottlenecks were algorithmic, and Rust would not have fixed them.** A full receipt
   scan is O(n) in any language; `last()` per append is O(N²) in any language. Both are gone with an
   index and a cache — ~40 lines of Python, no build step, no new artifact to ship.
4. **A native binary is a new failure mode.** The one place this project already ships a native
   dependency (`monero-wallet-rpc`) is the most operationally annoying part of it: download, SHA256
   pin, port allocation, digest auth, signing-vs-starting. Adding a second one to save 2 ms would be
   a bad trade.

### Where a Rust rewrite *would* be viable — for packaging, not speed

The plugin currently needs `embit`, `eth-account`, `eth-keys` and `pynacl` wheels, which is why the
host has to be told `PYTHONPATH=<wallet home>/pylibs` at all (Python 3.14 wheels for pure-Python
crypto were the reason the dependency workaround exists). A single static `wallet-signer` sidecar
doing secp256k1 (ECDSA + Schnorr), keccak, bech32/base58, RLP, EIP-1559/EIP-3009 and Ed25519 receipts
would remove four Python dependencies and the whole wheel problem, and it would be pinned and
verified the same way the Monero signer already is.

That is a real win — for installation robustness and supply-chain surface, not for latency. It is
also a big change (new IPC contract, duplicated crypto tests, a second pinned binary), so it belongs
after the live-money test, not before it.
