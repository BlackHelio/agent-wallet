# Architecture

## Shape

```
┌──────────────────────── Hermes Desktop (renderer) ────────────────────────┐
│  Wallet pane  (desktop/plugin.js)                                         │
│  · SDK only: @hermes/plugin-sdk, react, react/jsx-runtime — no JSX         │
│  · holds NO token, NO address secret, calls only ctx.rest()               │
└───────────────────────────────┬───────────────────────────────────────────┘
                                │  /api/plugins/agent-wallet/*   (HTTP, same host)
┌───────────────────────────────▼───────────────────────────────────────────┐
│  Hermes plugin backend  (plugin_api.py, server side)                      │
│  · holds the OPERATOR token (never sent to the renderer)                  │
│  · allowlists methods; cannot sign, cannot read secrets                   │
└───────────────────────────────┬───────────────────────────────────────────┘
                                │  UDS JSON-RPC v1, 0600 socket, peer-uid check
┌───────────────────────────────▼───────────────────────────────────────────┐
│  walletd  (separate process, no Hermes imports)                           │
│  keystore · policy · mandates · chain adapters · ledger                    │
│  · the ONLY place key material is ever decrypted                          │
│  · operator scope required for: unlock, approve, mandate create, export    │
└───────┬───────────────────────────────┬───────────────────────────────────┘
        │ JSON-RPC (HTTPS)              │ Ed25519 receipts → JSONL hash chain
┌───────▼──────────┐            ┌───────▼──────────┐
│ Chain RPC (Base) │            │ <HERMES_HOME>/   │
│ untrusted        │            │ wallet/          │
└──────────────────┘            └──────────────────┘

┌───────────────────────────────┐
│  Agent (LLM) + its shell      │  ── agent-scope tools ──▶ walletd (request only)
│  UNTRUSTED in the threat model│
└───────────────────────────────┘
```

The agent process is treated as **hostile**. It can call request methods; it cannot approve,
unlock, create a mandate, or read anything secret. That single asymmetry is the product.

**One home, one resolver.** The keystore, wallets and ledger live at `<HERMES_HOME>/wallet` —
outside the plugin directory, so removing the plugin cannot delete money. `walletd/home.py` is the
single place that decides this, in a fixed order: `AGENT_WALLET_HOME`, then `HERMES_HOME/wallet`,
then `~/.hermes/wallet`, then `XDG_STATE_HOME/agent-wallet` (or `~/.local/state/agent-wallet`) as a
last resort when no Hermes home exists. The CLI, the daemon, the systemd unit and the pane all call
that resolver, so two components cannot disagree about where the wallet is.

## Processes and lifetime

> **Running it is not the user's job.** `hermes wallet service install` writes a
> `systemd --user` unit (`Restart=always`, `NoNewPrivileges`, `PrivateTmp`) or a launchd agent, so
> the signer is always there. On a box without a service manager (a container) the plugin
> auto-starts it on first use. The encrypted file is the vault; the daemon is the only thing that
> can open it, which is exactly why it is a separate process.

| Process | Started by | Dies with | Holds |
|---|---|---|---|
| `walletd` | `hermes wallet serve` (foreground) or a user unit | its own lifetime; socket + pidfile | all key material, after operator unlock |
| Hermes plugin | Hermes, in-process | Hermes | agent token (client side), operator token (in `plugin_api.py`) |
| Desktop pane | app | app | no credentials at all |

If `walletd` is down, every tool returns `walletd_unavailable` — there is no degraded mode that
falls back to signing in-process. Ever.

## UDS JSON-RPC v1

Envelope: `{"jsonrpc":"2.0","id":N,"method":"...","params":{...}}`.
Every response is checked by a test that refuses any string longer than 64 hex chars outside
`transaction`, `tx_hash` fields — that is how "no seed leaks to the agent" stays true over time.

Scopes: `agent` (line A) and `operator` (line B).

| Method | Scope | Notes |
|---|---|---|
| `status` | agent | locked/unlocked, chain, version, bounds |
| `accounts` | agent | public addresses + labels only |
| `balances` | agent | chain RPC read |
| `chain_info` | agent | chain id, rpc health, block height |
| `request_tx` | agent | creates a pending request; never signs |
| `request_swap_quote` | agent | quote + expected out + slippage |
| `request_swap` | agent | swap as a pending request |
| `requests_list` / `request_get` | agent | the agent may see its own requests |
| `mandates_list` | agent | |
| `mandates_propose` | agent | creates a **proposal**, not an active mandate |
| `mandates_revoke` | agent | revocation is always allowed |
| `ledger_tail` / `ledger_verify` | agent | receipts, hashes, decisions |
| `unlock` | **operator** | passphrase; arms the signing key for `unlock_ttl` |
| `lock` | operator | zeroizes |
| `approve` / `deny` | **operator** | nonce + 120 s TTL, bound to the exact tx hash |
| `mandates_activate` | **operator** | proposal → live mandate |
| `export_seed` | **operator** | passphrase re-entry + explicit confirm; no agent route exists |

`approve` is refused unless: request exists, not expired, `nonce` matches, the tx hash in the
approval equals the stored hash, and the wallet is unlocked. Replays are refused by nonce burn.

## Mandates (auto-pay)

```yaml
id: mnd_01H…
payee: "0xSeller…"            # exact address or allowlist key
asset: USDC
per_tx_cap: "0.02"
period: "24h"
period_cap: "0.20"
absolute_cap: "1.00"
expires_at: 2026-11-12T00:00:00Z
enabled: true
created_by: operator          # a proposal the agent filed, activated by a human
```

Evaluation order inside `walletd`, on every request, before any signature:
`enabled` → not expired → asset matches → payee matches → `per_tx_cap` → `period_cap` →
`absolute_cap` → global daily budget → chain allowlist. Any miss = `deny` with a reason code and
a receipt. The agent cannot edit these fields; it can only propose and revoke.

## Hermes surfaces used (all public, verified in the installed tree)

| Need | Surface | Reference |
|---|---|---|
| Agent tools | `ctx.register_tool(...)` | plugin guide |
| Blocking a spend | `pre_tool_call` hook — returns block message, may rewrite args | `model_tools.py:766 _pre_dispatch_guards`, `hermes_cli/plugins.py::_dispatch_pre_tool_call_hooks` |
| Normalizing payment args from other plugins | `ctx.register_middleware("tool_request", cb)` | `hermes_cli/middleware.py` (`tool_request`, `tool_execution`, `llm_request`, `llm_execution`) |
| CLI | `ctx.register_cli_command("wallet", ...)` | plugin guide |
| Desktop pane / chip / settings | unified package `plugins/agent-wallet/desktop/plugin.js` | Desktop Plugin SDK |
| Pane → backend | `plugin_api.py` at `/api/plugins/agent-wallet`, consumed by `ctx.rest` / `ctx.socket` | Desktop SDK |
| Secrets the plugin itself needs (e.g. RPC URL token) | `agent.secret_scope.get_secret(NAME)` | catalog rule 11 |
| Future: paid agent-to-agent channel | `ctx.register_platform(...)` | `hermes_cli/plugins.py:787` |

Nothing in this design patches core, imports undocumented core modules, or touches app markup.

## Repository layout

```
plugin.yaml                  # manifest: kind, capabilities, requires_env, config_schema
__init__.py                  # register(ctx)
plugin_api.py                # /api/plugins/agent-wallet backend, operator token lives here
wallet/                      # agent half
  client.py                  # UDS JSON-RPC client (agent scope)
  tools.py                   # schemas + handlers
  policy_mirror.py           # readable early failures (authority stays in walletd)
  doctor.py                  # `hermes wallet doctor`
walletd/                     # trusted half, zero Hermes imports
  __main__.py  rpc.py  keystore.py  policy.py  mandates.py  ledger.py
  chain/evm.py  chain/x402.py
desktop/plugin.js            # pane + chip + settings page (SDK only, no JSX)
skills/buy-from-402-api/SKILL.md
skills/shopping-checkout/SKILL.md
tests/                       # real keystore, real socket, real Sepolia tx (opt-in)
docs/
```

`walletd/` never imports from `wallet/`, and never imports Hermes. That is checkable in CI:
`grep -r "hermes" walletd/` must only hit comments.

## Failure semantics

| Failure | Behaviour |
|---|---|
| `walletd` not running | tools return `walletd_unavailable`, no fallback |
| Wallet locked | requests queue as `pending`; approval impossible until operator unlocks |
| No operator present (cron, gateway fire-and-forget) | deny synchronously + receipt `reason: unattended` |
| Chain RPC down | request stays `pending`; receipt `broadcast_failed`; no retry storm (max 2) |
| Approval expires (120 s) | request returns to `pending`, nonce burned |
| Policy mismatch | deny + receipt; the message names the exact rule and the numbers |
| Keystore corrupt / wrong passphrase | fail closed, no partial keys in memory, log only the failure code |

## Receipts

```json
{"seq": 41, "prev": "sha256:…", "ts": "2026-11-09T12:04:11Z",
 "kind": "transfer", "decision": "approved", "rule": "manual",
 "chain": "base-sepolia", "asset": "USDC", "amount": "0.05",
 "payee": "0x…", "tx_hash": "0x…", "request_id": "req_…",
 "sig": "ed25519:…"}
```

Signature covers the canonical JSON of every field except `sig`. `prev` chains to the previous
entry's signature. `hermes wallet verify` walks the file, re-verifies each signature and each
link, and reports the first break. Tampering with any field or reordering entries fails.
`verify_file(path, pubkey)` needs only the public key, so verification works offline, on another
machine, and after the wallet is rotated.

## Chain adapters

One interface, one policy engine, three implementations:

| Adapter | Key custody | Build → approve → broadcast |
|---|---|---|
| `chains/monero.py` | `monero-wallet-rpc` holds the encrypted wallet file; `walletd` holds its password (from the keystore) and the RPC credentials | `transfer` with `do_not_relay: true` returns the tx blob → approval → `relay_tx`; store the `tx_key` in the receipt for third-party verification without a view key |
| `chains/bitcoin.py` | `bitcoind` wallet | `walletcreatefundedpsbt` → approval → `walletprocesspsbt` + `sendrawtransaction`; addresses per counterparty from `getnewaddress` |
| `chains/evm.py` | key in the keystore (secp256k1) | EIP-1559 tx, or an EIP-3009 authorization for x402 payments (signed, submitted by a facilitator — no gas on our side) |

Monero is first because it is the request that started the project: a Monero transfer between
two people, where the agent arranges it, a human approves it, and the *recipient can prove* the
payment happened. Monero gives us that for free — the tx key is in the transaction — which no
EVM chain does without extra machinery.

Adapter rules: no adapter may read the keystore directly, and no adapter may sign without an
approval record or a live mandate. The adapter receives a validated `SpendRequest` and an
approval id; it returns a `tx_hash` and a receipt payload.

## The human gate

`human_gate: true` (default) means:

- unlocking needs the passphrase, typed in the Desktop pane → `plugin_api.py` → `unlock`;
- an approval above `approval_threshold` additionally requires **typing the amount**, which is
  checked inside `walletd`, not in the UI;
- disabling the gate requires operator scope **and** the passphrase again **and**
  `confirm: true`, and it writes a `config/changed` receipt.

The settings file is HMAC-signed with a key derived from the keystore master secret. If the file
is edited, or cannot be verified because the wallet is locked, `walletd` **discards it entirely**
and runs on restrictive defaults with `human_gate: true`. An agent with filesystem access can
therefore make the wallet stricter or broken, never looser.

## Desktop pane (the GUI inside Hermes)

Unified package — `plugins/agent-wallet/desktop/plugin.js` — loaded by the app's disk door and
hot-reloaded on save. SDK-only: `@hermes/plugin-sdk`, `react`, `react/jsx-runtime`; no JSX syntax
(loaded uncompiled), so `jsx()`/`jsxs()` calls; no DOM patching, no `window.hermesDesktop`.

| Area | Contribution |
|---|---|
| `SIDEBAR_NAV_AREA` | **Wallet** row in the top-left nav (like Kanban / Memory), `codicon: credit-card`, navigates to `/wallet` |
| `ROUTES_AREA` | the wallet page at `/wallet`: lock/unlock, approval cards with decoded intent and typed-amount confirm, mandates with Activate/Revoke, receipts with Verify |
| `PALETTE_AREA` | “Open Wallet” command for ⌘K |
| `STATUSBAR_AREAS.right` | chip that navigates to `/wallet` |
| `registerSettingsPage` | caps, threshold, chains, passphrase-prompt state |

Everything the page does is also a CLI verb (see “CLI parity” below), so the plugin never depends on
the desktop app being open.

Data path: page → `ctx.rest('/api/plugins/agent-wallet/…')` → `plugin_api.py` (holds the operator
token) → UDS → `walletd`. The renderer never sees a token, an address secret or a key.

Two platform details that cost a debugging round and are easy to get wrong again:

- the backend is discovered from **`dashboard/manifest.json`** (`{"api": "plugin_api.py"}`), not
  from the file existing on disk;
- the server imports that file **by path**, so the plugin root is not importable — the module adds
  its own root to `sys.path` before `from wallet import …`.

## CLI parity (no desktop required)

`hermes wallet` covers the whole operator lifecycle, so a headless box, an SSH session or a
terminal-only user loses nothing:

```
hermes wallet status                    # lock state, chains, caps, pending count
hermes wallet up | down                 # start / stop the signer daemon
hermes wallet unlock                    # passphrase on the TTY (operator scope)
hermes wallet requests                  # list with request_id + approval_id
hermes wallet approve <id> [--amount X] # decoded fields, y/N, typed amount above threshold
hermes wallet deny <id> --reason "..."
hermes wallet mandates [list|activate|revoke] --mandate-id <id>
hermes wallet ledger --limit 20
hermes wallet verify                    # hash-chain verification, exit 1 on a break
hermes wallet gate on|off               # passphrase prompt; operator + passphrase + confirm
```

Passphrases are read with `getpass`; the only non-interactive path is `--passphrase-stdin`
(scripts/tests). There is no `--passphrase VALUE` flag, ever — nothing lands in shell history or
`ps`. Approval above `approval_threshold` requires `--amount` as the typed confirmation, and
`--yes` only skips the y/N prompt, never the passphrase.
