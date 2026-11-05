# Build log

## 2026-11-05 — walletd core (trusted half) working

### Implemented

| File | What it does |
|---|---|
| `walletd/keystore.py` | scrypt (n=2¹⁵, r=8) + XChaCha20-Poly1305-IETF envelope, atomic 0600 write, fsync'd, versioned; no plaintext in the file |
| `walletd/tokens.py` | per-boot `agent` / `operator` tokens, 0600, constant-time compare |
| `walletd/config.py` | **signed** settings (HMAC-SHA256 under a keystore-derived key) — the AI-proof human gate |
| `walletd/policy.py` | spend requests, mandates, payee validation (XMR/BTC/EVM), deterministic deny codes |
| `walletd/ledger.py` | Ed25519 receipts, hash chain, `verify_file()` offline verification using the pubkey only |
| `walletd/rpc.py` | UDS JSON-RPC over a 0600 socket, `SO_PEERCRED` uid check, scope-gated dispatch, no tracebacks over the wire |
| `walletd/state.py` | init / unlock / lock, request → approval, mandates, ledger views, `set_human_gate` |
| `walletd/__main__.py` | `init`, `serve`, `status`, `verify`, `tail` |

### Verified by running it

**43 unit tests pass** (`python3 -m unittest discover -s tests -t .`), covering:
keystore round-trip / wrong passphrase / ciphertext tamper / 0600 mode; ledger chain, reorder,
field tamper, deleted entry, wrong pubkey; policy caps, allowlists, unattended deny, mandate
expiry/scope/period-cap/exact-payee; approval replay, digest mismatch, amount confirmation;
human gate (`bad_passphrase`, `confirm_required`, disable+re-enable, tampered file → defaults +
gate forced on); secret-leak scan over every agent-facing view; and a socket test that proves
`agent` scope is refused for `unlock`, `approve`, `set_human_gate`.

**Live smoke against the real daemon** (`tests/smoke_live.py`, 12 steps, output reproduced):

```
2. agent tries to approve            -> refused: approve requires the operator scope
3. request while locked              -> deny / unattended_no_operator
5. request 0.01 XMR after unlock     -> pending + approval_id, rule manual_approval
6. agent self-approves               -> refused: approve requires the operator scope
7. operator approves                 -> approved
8. agent proposes mandate            -> status revoked (inert), request needs approval
9. operator activates mandate        -> auto-paid: true, status approved
10. agent revokes mandate            -> auto-paid: false, needs approval again
11. ledger verify                    -> ok, 10 entries
12. one byte edited then verify      -> exit 1, "bad signature at seq 10"
```

### Decisions taken while building

1. **A locked wallet behaves as unattended.** No operator means no manual path; requests deny
   with `unattended_no_operator` and a receipt. Caught by a test that initially passed wrongly
   because the explicit `unattended` flag clobbered the locked signal.
2. **A tampered config restores defaults, not "the values you wrote".** Trusting unsigned values
   (even with the human gate forced on) would let a file edit raise every cap. Restoring defaults
   means the worst an attacker gets is "the wallet is more restrictive than before".
3. **A mistyped amount does not burn the approval.** `approve` peeks before consuming, so a
   failed `confirm_amount` can be retried; the nonce is spent only on success.
4. **Bad payees become receipts, not exceptions.** The raw payee string is kept and policy denies
   it, so the attempt is auditable; a malformed amount is still a hard error because there is no
   meaningful amount to record.

## 2026-11-05 (later) — Hermes plugin half + Desktop pane + a live profile

### Implemented

| File | What it does |
|---|---|
| `plugin.yaml` | 8 tools, `pre_tool_call` hook, no env, `requires_hermes >=0.21.0` |
| `__init__.py` | registers the tools, the foreign-payment guard, and `hermes wallet` |
| `wallet/client.py` | UDS client; **one wallet per profile** (`$HERMES_HOME/wallet`) |
| `wallet/daemon.py` | starts `walletd` as an argv-list child, waits for the socket, never shells out |
| `wallet/tools.py` | 8 tool handlers; every one is a request or a read |
| `dashboard/plugin_api.py` | FastAPI router at `/api/plugins/agent-wallet/*`, holds the **operator** token server-side |
| `desktop/plugin.js` | the pane: unlock, approval cards, mandates with Activate/Revoke, receipts + Verify, settings page, status-bar chip |
| `skills/send-xmr`, `skills/pay-priced-api` | procedures that must route through `wallet_request_payment` |

### Verified

- `hermes plugins validate` **passes all 16 checks**: manifest, capability probe, declared tools and
  hooks match, **security scan safe**, **no core override**, **desktop surface inside the SDK**.
- `tests/smoke_pane_backend.py` drives the real router against a real daemon: unlock 200, wrong
  passphrase 400 `bad_passphrase`, pending request carries `approval_id`, approve 200,
  verify 200 true, config read, human gate toggled and refused with a bad passphrase.
- `node --check` on the pane file (copied to `.mjs`) parses clean.
- A real second profile: `hermes profile create wallet --clone`, plugin copied to
  `profiles/wallet/plugins/agent-wallet`, `hermes -p wallet plugins enable agent-wallet` →
  *"enabled … hooks active now, tools next session"*; `hermes -p wallet wallet up` starts that
  profile's own daemon; `wallet status` and `wallet verify` (`ok, 1 entry`) work.
- Ledger verification no longer needs the key: `ledger_verify` falls back to the pubkey file, so
  `wallet verify` works on a locked wallet.

## 2026-11-05 (later still) — sidebar button + full CLI parity

- **Desktop**: the pane became a top-left **sidebar nav** row (`SIDEBAR_NAV_AREA`,
  `codicon: credit-card`) opening a `/wallet` route page, plus a ⌘K palette command — the same
  shape as Kanban / Memory. `node --check` passes.
- **CLI**: `hermes wallet` now covers the whole operator lifecycle — `status, up, down, unlock,
  lock, requests, approve, deny, mandates [list|activate|revoke], ledger, verify, gate on|off`.
  Passphrases via `getpass`, `--passphrase-stdin` for scripts only, no `--passphrase VALUE` flag.
- **Verified on a real profile** (`hermes -p wallet`, own keystore):
  unlock → agent request `pending apr_…` → `wallet requests` lists it →
  `wallet approve req_… --amount 0.01 --yes` → `status: approved` → `wallet verify` `ok, 4 entries`
  → `gate off`/`gate on` flip `human_gate` with a signed config → agent proposes mandate →
  `wallet mandates activate --mandate-id …` → `status: active` → next request `auto=true`.

## 2026-11-05 — signer as a background service

`hermes wallet service install|status|uninstall` writes a `systemd --user` unit
(`agent-wallet.service`, `Restart=always`, `NoNewPrivileges`, `PrivateTmp`, `WantedBy=default.target`)
or a launchd agent; containers get an explicit "no service manager, on-demand start" message.

Verified on the main PC: unit enabled + started, then `kill <MainPID>` → systemd restarted it
(`Restart=always`) and `hermes wallet status` answered immediately afterwards.

## 2026-11-05 — why the pane said "wants a running signer" (three bugs, all fixed)

1. **No `dashboard/manifest.json`.** The web server discovers plugin backends by scanning
   `<plugin>/dashboard/manifest.json`; without it the plugin's API was never mounted, so the page's
   `ctx.rest('/status')` 404'd. Added (`{"api": "plugin_api.py", "tab": {"hidden": true}}`).
2. **`No module named 'wallet'`.** Even with the manifest, the server imports `plugin_api.py` by
   path via `spec_from_file_location`, so the plugin root is not on `sys.path`; the mount was
   skipped (log: `Failed to load plugin agent-wallet API routes: No module named 'wallet'`).
   Fixed by adding exactly this plugin's root to `sys.path` before the import.
3. **`/status` never started the signer.** Only mutating endpoints auto-started `walletd`, so a cold
   page reported `running: false` forever. The agent-scope helper now starts it and retries once.

Verified by replaying the server's own mount path (`spec_from_file_location` under the same module
name + `include_router(prefix="/api/plugins/agent-wallet")`) against a live daemon:
`status 200, running true`, `config 200` with the cap table.

## 2026-11-06 — Monero adapter, live against a stagenet node

### Built

| File | What it does |
|---|---|
| `walletd/chains/base.py` | `ChainAdapter` contract, `TxPlan` (broadcast material never leaves the process), stdlib `JsonRpc`, stable error codes |
| `walletd/chains/monero.py` | `XmrAdapter` over `monero-wallet-rpc`: open wallet, address, balance, `transfer --do-not-relay` plan → `relay_tx` broadcast → `get_transfer_by_txid` read-back, `check_tx_key` for third-party payment proof |
| `walletd/state.py` | adapters built at unlock; `request_tx` plans **before** asking a human (unfunded wallet fails here, with a receipt); mandate path broadcasts immediately; `approve` broadcasts and stores `tx_hash` + `tx_key` in the receipt |
| `walletd/config.py` | settings signature now covers the **stored** dict, so adding a field no longer invalidates every existing install (that bug surfaced live as `config_tampered: true` after adding `monero_rpc_url`) |
| `walletd/policy.py` | per-network Monero address prefixes: mainnet `4/8`, stagenet `5/7`, testnet `9/A` (the old single regex rejected the stagenet address the node produced) |
| `wallet/tools.py`, `wallet/cli.py` | `wallet_chains` tool + `hermes wallet chains` |

### Live environment (helio)

- `xmr-stagenet` (the Monero node, `sethforprivacy/simple-monero` image), data `/home/helio/srv/monero-stagenet`,
  RPC bound to `127.0.0.1:38081` only.
- `xmr-wallet-stagenet` (`--entrypoint monero-wallet-rpc`, because the image's entrypoint already sets
  `--rpc-bind-ip`/`--rpc-bind-port` and rejects duplicates), wallet dir `/home/helio/srv/monero-wallet-stagenet`,
  RPC `127.0.0.1:38083`.
- The Hermes container reaches it through an SSH local-forward (`-L 38083:127.0.0.1:38083`), so the
  wallet RPC is never exposed on the LAN.

### Verified live

```
chain_status  -> available true, height 84294, rpc http://127.0.0.1:38083/json_rpc
address       -> 57gztAFWL2QVN73rZ1gb…   (real stagenet address, created by create_wallet)
balance       -> 0
request 0.001 -> decision allow=false code=insufficient_funds reason="not enough money"
ledger        -> denied entry with code insufficient_funds, chain verifies ok
```

### Blocker (next session, 10 minutes of waiting, not work)

The stagenet node has **0 outgoing peers**, so sync crawls (84k / 2.24M). Until it is synced and the
wallet is funded (a stagenet faucet, or block rewards from running the node), a *broadcast* cannot be demonstrated — the
plan/broadcast code paths are covered by unit tests with an in-memory adapter, and by the real
"insufficient funds" round-trip above. Next: add seed peers or `--add-exclusive-node`, sync, fund,
then record the first real tx (txid + tx key in the receipt) for the demo video.

## 2026-11-06 — full-page wallet UI

The page is no longer a narrow column: it fills the workspace (`h-full w-full`, 4-column balance
grid at `xl`), with:

- **coin badges** drawn as inline SVG (per-asset colour + glyph, ETH as a diamond) — no network
  images, no bundled assets;
- **balance cards** from a new `GET /api/plugins/agent-wallet/balances` → `walletd.balances()`:
  address, balance, live/offline per chain, click-to-copy address, and a totals line in the header;
- **approval cards** with the amount, full payee, the agent's `why`, typed-amount confirm and
  Approve/Deny;
- **mandate cards** with the three caps, short payee, expiry and Activate/Revoke;
- **receipt table** (seq, kind, amount, relative time) and a Verify button;
- header chips for lock state, signer, passphrase prompt and pending count.

Also fixed: the page component was being *called* during render instead of mounted
(`render: () => WalletPage(ctx)` → `h(WalletPage, { ctx })`), which breaks hooks on re-render.

## 2026-11-06 — external node, settings API, per-machine tunnel

- Dropped the local stagenet node. The wallet now uses a **public Monero node**
  (`xmr-node.cakewallet.com:18081`); public *stagenet* endpoints were unreachable from this host,
  so the working wallet is mainnet and read-only until funded.
- New operator RPC `config_set` (+ `hermes wallet config set --json '{...}'`): chains, assets,
  caps, threshold, allowlist and `monero_rpc_url` are validated, re-signed and receipted, and the
  chain adapters are rebuilt immediately. No hand-editing a signed file.
- `hermes wallet service env --set KEY=VALUE` writes `<wallet home>/wallet.env` (0600), and the
  systemd unit now loads it through `EnvironmentFile=-…` — so `service install` can be re-run
  without losing per-machine settings (which wallet file, its password).
- The main PC reaches the wallet RPC through its own `systemd --user` unit
  `wallet-rpc-tunnel.service` (ssh `-L 38084`), so no RPC is exposed on the LAN.

Live: helio `monero-mainnet` at height 3,799,464, address `43s4Sdo…`, balance 0; the PC reports
the same through its tunnel, so the Desktop page shows the XMR card as live.

## 2026-11-06 — public node registry (no local nodes, ever)

`walletd/chains/nodes.py` holds the predefined endpoints and a health probe per protocol:

| Chain | Providers (first healthy wins) |
|---|---|
| monero-mainnet | cakewallet `xmr-node.cakewallet.com:18081`, stackwallet `monero.stackwallet.com:18081` |
| monero-stagenet / testnet | rino community nodes |
| bitcoin-mainnet | mempool.space, blockstream (Esplora REST) |
| bitcoin-testnet | mempool.space/testnet, blockstream/testnet |
| base / base-sepolia | base.org + publicnode fallbacks |
| polygon / arbitrum | polygon-rpc.com, arb1.arbitrum.io |

Probes are real: monero `get_info`, esplora `/blocks/tip/height`, evm `eth_blockNumber`, cached 120 s
with skip-on-failure. New `wallet_nodes` tool + `hermes wallet nodes`; the balance cards show the
provider that served the wallet (`cakewallet · 3.79M`).

Live: cakewallet and stackwallet both report height 3,799,466 and `synced: true`.

**What still runs locally:** only `monero-wallet-rpc` — the key holder (~40 MB, no chain data). It
is not a node and holds no blockchain; the chain is entirely remote. Bitcoin will need its own
signer decision (Electrum server vs. a local wallet daemon) before it can spend.

## 2026-11-06 — one owner for the signer process

`daemon.start()` used to spawn a detached walletd whenever the socket was missing — including on a
machine where `agent-wallet.service` exists. Two daemons then raced for the same socket, the unit
crash-looped (`activating` forever) while the ad-hoc copy served requests. Fixed: if a user service
unit exists, `start()` asks systemd (`reset-failed` + `start`) and never spawns; the fallback spawn
stays for service-less hosts (containers).

Also: `monero_provider` setting so the UI names the public provider it is actually using
(`cakewallet`), since the wallet daemon's upstream is not discoverable over its own RPC.

## 2026-11-06 — "failed to load" was a TDZ bug, plus a render harness

The app's own log had the answer:
`[error-boundary:contrib:plugin:agent-wallet:agent-wallet:wallet-route] ReferenceError: Cannot access 'chains' before initialization`.

The request-form effect listed `chains.join(',')` in its dependency array while `chains` was declared
further down the component body — a temporal dead zone error on every render, which the app surfaces
as a failed contribution.

Fixed by deriving `chains`, `assets` and `wallet` **above** every effect/closure that reads them.

Regression guard: `desktop/render-check.mjs` loads the plugin with stubbed SDK + React, calls
`register()`, then executes each contribution's `render()`. React's stub calls function components
inline, so render-time errors throw here instead of in the app. Proven both ways: the fixed file
prints `rendered wallet-route: 23 text nodes` / `render-check: OK`; the pre-fix ordering throws.

## 2026-11-06 — sandbox wallets, node control, chain-driven UI

### Sandbox wallets (a budget the agent owns)
`walletd.sandboxes` holds capped sub-wallets backed by real Monero **accounts**:

- `sandbox_create` (operator) → `create_account` on the signer, then a funding transfer to that
  account's own subaddress, filed as a normal pending request. The sandbox is `unfunded` until that
  transfer is approved and broadcast, then `active`.
- spending with `sandbox_id` skips the human: `request_tx` asks `_sandbox_decision`, which allows
  the payment while `spent + amount <= budget`, the sandbox is active and unexpired, then broadcasts
  from that account and increments `spent`. Over-budget → `sandbox_budget` deny with a receipt.
- `sandbox_revoke` disables it immediately (funds stay in the account; sweeping is a later feature).
- surface: `sandbox_create|list|revoke` RPC, `hermes wallet sandbox list|create|revoke`,
  `wallet_sandboxes` tool, and a Sandboxes section in the UI with a spend bar and a create form.

Live on mainnet: created sandbox `sbx_3f4c871e96` → real account #1 with subaddress
`84RmGyYq…`; its funding request was denied `insufficient_funds` because the wallet is empty, and
the sandbox correctly stayed `unfunded`.

### Node control
The settings page now lists every known public endpoint for the chain (label, URL, live height)
and lets the operator edit the wallet RPC the signer talks to, saved through the signed
`config_set` path (`POST /config`). Receipts carry an `explorer` link when the chain adapter defines
one — Monero deliberately has none.

### UI
- the request form's **chain drives the asset list** (`CHAIN_ASSETS`), so picking base-sepolia can
  never offer XMR.
- balances are shown in three places: hero total, per-asset row (right-aligned, tabular), and the
  sandbox spend bar.
- receipts link to a block explorer when available, and stay capped at 6 rows until expanded.

## 2026-11-06 — sandbox ACLs, coin filters, persistent sandboxes

- **Profile ACL on sandboxes**: `allowed_profiles` (empty = every profile). The agent half sends
  `client.current_profile()` with each request; `_sandbox_decision` checks the ACL **before** any
  budget state, so an unauthorised profile learns nothing (deny code `sandbox_profile`). Editable
  from the UI card and via `POST /sandbox/profiles`.
- **Sandboxes now persist** to `<wallet home>/sandboxes.json` (0600) and reload on unlock — a
  sandbox is a real on-chain account, so losing it on a daemon restart would strand funds. Verified
  across a restart: `sbx_d027acda3e` came back `unfunded`, profiles=all, 0.02 XMR.
- **Coin visibility toggles** in the Assets header (per-asset pills, persisted via `ctx.storage`),
  with an explicit "every coin is hidden" state.
- **Balances while locked**: the page says why they are empty ("the signer needs the passphrase to
  open the wallet and ask the node") instead of showing the offline placeholder.

## 2026-11-06 — the "just tell it what to buy" layer

- **Contacts** (`contacts.json`, 0600): `name -> {chain: address}`, validated per chain at save time.
  The agent may save and read; deleting needs operator scope. `request_tx` accepts a **name** for
  `payee` and resolves it (memo becomes `to Aiden`). Live: request with `payee="Aiden"`,
  `asset="AUTO"` → resolved to the saved address, approved, `sent simulated=True`.
- **Coin preference**: `prefer_asset` (used when the agent passes `asset: AUTO`) and
  `monero_only` — the latter refuses any non-XMR payment with code `monero_only` and the reason
  spells out that most merchants do not take Monero. Both editable in Settings, with the warning
  inline.
- **Merchant playbook** (`walletd/merchants.py`): Mullvad, Namecheap, Bitrefill, Monero.com/Cake
  Pay, Coinsbee, Travala — each with accepted coins, whether an account is needed, and the exact
  steps to pay; plus a standing reminder that crypto support changes and the shop's checkout is the
  authority. Surface: `wallet_merchants` tool, `hermes wallet merchants`, and the answer carries
  `advice` about Monero acceptance being spotty.
- **Contrast**: page tokens brightened (`--ui-border` ~0.55 alpha, secondary/tertiary text near
  white) and every card is now `border-2`, so the page reads clearly on a black desktop.
- 60 tests green (contacts validation, name resolution, missing-chain error, unknown name denied
  with a receipt, monero_only, AUTO preference, sweep); render harness 37 text nodes.

## 2026-11-06 — the signer installs itself

`hermes wallet signer install` downloads the **official** Monero release tarball, verifies its
SHA256 against the Monero project's GPG-signed `hashes.txt`, extracts only `monero-wallet-rpc` into
`<wallet home>/bin/`, and marks it executable. `signer start` then runs it on loopback against the
first healthy registry node, sets `monero_rpc_url` to it, and rebuilds the adapters.

Verified live on helio, with the docker signer and the SSH tunnel stopped:

```
signer install → sha256 ba2928962c10… , "Monero 'Fluorine Fermi' (v0.18.5.4-release)"
signer start   → pid 37093, http://127.0.0.1:38085/json_rpc, node cakewallet, height 3,799,518
wallet init    → created, address 4ABeR5e9JgvCEj9xbNw9py…
balances       → monero-mainnet 0.000000000000 live
demo           → approved 0.001 XMR [simulated]
```

Two bugs surfaced doing it: `create_wallet` returns no address (read it back after opening), and a
daemon started ad-hoc could not see `wallet.env`, so it opened the wallet with an empty password and
created a second one — walletd now falls back to reading `<home>/wallet.env` itself.

### Not yet verified

- The pane has not been rendered in the Electron app — the SDK body shape for `ctx.rest` POSTs is
  assumed (`{method, body}`) with a JSON-string retry; needs one live look, and the unified desktop
  half is **opt-in** in Capabilities → Plugins.
- No chain adapter yet: `approve` marks a request approved and returns `signed: false`.

### Next (in order)

1. `walletd/chains/monero.py` — `monero-wallet-rpc` adapter: subaddress per counterparty,
   `transfer` with `do_not_relay` → approve → `relay_tx`, and a payment proof (tx key) stored in
   the receipt. XMR is the first chain because it is the one Helio asked for.
2. `walletd/chains/bitcoin.py` — `bitcoind` wallet RPC: `getnewaddress`, `sendtoaddress`, PSBT
   path for approval-then-broadcast.
3. `walletd/chains/evm.py` — EIP-1559 + EIP-3009 for x402-style payments, Base Sepolia.
4. Hermes plugin half (`plugin.yaml`, tools, `pre_tool_call` guard, `plugin_api.py`).
5. Desktop pane (unlock, approvals with decoded intent, mandates + Revoke, ledger).
