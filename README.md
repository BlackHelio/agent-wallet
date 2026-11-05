# agent-wallet

**The agent can ask for money. It cannot approve it, and it never holds a key.**

Hermes plugin: the agent asks, a human types the passphrase, a budget can be delegated, and every
decision — approved *or denied* — lands in a tamper-evident receipt chain. Keys live in a separate
signer process the agent cannot read, cannot run, and cannot approve through.

```
$ hermes wallet status
   ▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄
  █  ┌───────────┐  █   agent-wallet
  █  │ ▓▓▓▓▓▓▓▓▓ │  █   UNLOCKED
  █  └───────────┘  █   keys live in walletd only
   ▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀   XMR/BTC/XRP

  wallet     unlocked (locks in 15m)
  coins      XMR, BTC, XRP          chains  Monero, Bitcoin, XRP
  signer     ready                  gate    passphrase prompt on
  caps       BTC 0.001, XMR 0.5, XRP 50
  pending    0   autopays 0   receipts 6 archive(s)
```

```
Agent (tools only) ──request──▶ walletd (keys · policy · receipts) ──plan/sign/relay──▶ public node
       │                                ▲        │
       └──── cannot reach: keystore, seed, approvals ────┘
                                        │
                       Human (passphrase + click) ──approve──┘
                                        └──append──▶ Ed25519 hash chain
```

## Why this exists

Agents are starting to pay for things — API calls, data, compute, each other. Two failure modes
dominate: the agent holds a key and a prompt injection drains it, or the human approves so often that
approval becomes a rubber stamp. This plugin is built around one rule: **the model is never in the
payment path.** It can ask. A separate process with the key decides, and a human authorises anything
above a budget they granted.

## What it does

| Capability | Detail |
|---|---|
| **Human gate** | The passphrase is typed in the Desktop page (or TTY). It unlocks the keystore *and* authorises each manual payment — an agent that finds the operator token on disk still cannot approve. |
| **Spend policy** | Per-asset per-tx and daily caps, payee allowlist, chain allowlist, approval threshold with typed-amount confirmation. Enforced in the signer, not in the plugin. |
| **Standing approvals ("auto-pay")** | Capped per payment, per interval and lifetime, expiring, payee-exact. The agent may propose, pause and revoke; only a human activates, and every payment inside the caps still comes from an agent request — there is no timer that spends on its own, so "pay rent monthly" means the agent asks each month and the caps decide. |
| **Sandboxes** | A real sub-account funded once with a budget a human approves. Inside it the agent spends freely — that is what a delegated budget means — and it stops at `budget - spent - fee`, at expiry, or on revoke, which returns the remainder. Optionally restricted to named Hermes profiles. `docs/SANDBOX-DESIGN.md` explains why this is the security story: the money physically left the main account, so a hijacked agent can spend a budget, not the wallet. |
| **Receipts** | Ed25519-signed, hash-chained, offline-verifiable (`hermes wallet verify`), and anchored so a truncated chain is detected. A denial is a receipt with the rule and the numbers that refused it — except while the wallet is locked, where the ledger's signing key is unavailable: the refusal is returned to the agent but not written. |
| **No nodes** | A registry of public endpoints (cakewallet, stackwallet, mempool.space, blockstream, base, publicnode) with health probes and failover. Chain data is never stored locally. |
| **One-time sandboxes** | `--one-time`: one agent payment, then a countdown (default 30 min, per-sandbox), then the remainder is swept home and the sandbox destroys itself. It never destroys while it holds funds — a blocked sweep keeps it alive with a reason. Receipted as `self_destruct_armed` / `swept` / `destroyed`. |
| **Recovery** | `wallet backup` writes one encrypted archive (keystore, settings, sandboxes, ledger, contacts, mandates, pending requests, idempotency keys); `wallet seed` exports the recovery words behind operator scope + passphrase + explicit confirm. |
| **Retries** | `idempotency_key` on a request (or `wallet pay-url`) makes a retry return the request that already exists, persisted across restarts — a timeout or a crash cannot turn one payment into two. |
| **Receiving** | `wallet receive --label "order 42"` returns a fresh Monero subaddress per order, `wallet_incoming` says whether each order is waiting, partial or paid, and `wallet proof <txid> --tx-key …` proves a payment arrived with the payer's transaction key. |
| **Simulation** | `sim-mainnet` runs the whole flow — plan, approve, broadcast, receipt — with no transaction behind it, `[SIMULATED]` stamped on every receipt. |

## Install

```bash
hermes plugins install BlackHelio/agent-wallet --enable       # from the catalog
hermes plugins validate ./agent-wallet --install-deps          # or from a checkout
hermes wallet demo                                             # signer, wallet, sim payment, receipts
```

`wallet demo` does the whole first run — installs the signer binary, creates the wallet, files a
payment, approves it and prints the receipt — on the simulator chain, so it costs nothing. Then:

```bash
hermes wallet service install     # one supervised daemon per wallet home
hermes wallet status              # banner, coins, caps, pending
hermes wallet coins --enable ETH  # add a chain, its address and a balance row in one step
```

Desktop: sidebar **Wallet** (opt-in under Capabilities → Plugins). Everything below works without it.

## Two minutes, no money

```bash
hermes wallet demo            # request → policy → approve → receipt → verify, on sim-mainnet
hermes wallet receive --label "order 42"   # a fresh subaddress for one incoming payment
./scripts/demo.sh             # the same story plus a 402 quote, for a screen recording
hermes wallet status
hermes wallet nodes           # which public endpoints are healthy right now
hermes wallet verify          # receipt chain; exit 1 on tamper
```

With real funds, when you want them:

```bash
hermes wallet requests                        # what is waiting, with approval ids
hermes wallet approve req_… --amount 0.01     # decoded fields, y/N, then the passphrase
hermes wallet pay-url https://api.example/report/42 --why "the report I asked for"
hermes wallet sandbox create --kind monero-mainnet --budget 0.05 --hours 24 --label "api budget"
hermes wallet sandbox create --kind monero-mainnet --budget 0.002 --one-time --destruct-after 900 \
  --label "one purchase"        # dies 15 min after the payment, after sweeping what is left
hermes wallet sandbox list
```

## The security claim, precisely

- **Keys**: scrypt (n=2¹⁵) + XChaCha20-Poly1305 at rest, decrypted only inside `walletd` and only
  after an operator passphrase. The agent's tool surface has no method returning key material, and a
  test asserts every agent-visible response is free of key-shaped data.
- **Approval**: two token scopes — `agent` may request, `operator` may approve/unlock/export. On a
  shared uid those token files are readable, which is exactly why approval *also* requires the
  passphrase.
- **Policy**: evaluated inside the signer, so it survives a compromised plugin, gateway or prompt.
- **Fail closed**: a locked wallet behaves as unattended; unattended payments are denied and
  recorded, never queued forever.
- **Settings**: HMAC-signed with a keystore-derived key. Hand-editing `config.json` is detected, and
  the wallet falls back to restrictive defaults with the human gate forced on.
- **In-process keys are still sealed.** The BTC/EVM secrets live in `keystore.json` under the same
  scrypt + XChaCha20-Poly1305 envelope, are added on the first unlock of an older keystore, and are
  only readable by the daemon — no RPC method returns them.
- **The signer binary is pinned.** The installer accepts exactly the release whose SHA256 is in
  `PINNED_TARBALLS`; a version that is not pinned is refused rather than fetched.
- **The signer itself is authenticated.** `monero-wallet-rpc` is started with HTTP Digest auth
  (credentials generated once into `wallet.env`, 0600), so a casual request to `127.0.0.1:38084` is
  refused — `curl` without credentials gets a 401. Be precise about the boundary: those credentials
  are a file this user can read, so a same-uid shell *could* find them and talk to the signer
  directly. The boundary is the OS user, not the process. What the plugin guarantees is that *its*
  agent surface cannot approve, and that every payment it makes goes through policy and a receipt.
- **Caps hold at approval time**, not only at request time: a queued set of pendings cannot multiply
  a daily cap. Every mutation is serialised, so the receipt chain cannot be forked by two
  concurrent tool calls.

What it does **not** protect against: a compromised host while the wallet is unlocked, a human
approving something they did not read, or a counterparty who takes the money and delivers nothing.
`docs/THREAT-MODEL.md` is explicit about each.

## Chains and signing

| Chain | Address | Signing | Broadcast |
|---|---|---|---|
| Monero | `4…` | bundled `monero-wallet-rpc` (keys never leave it) | public node |
| Bitcoin | `bc1q…` | in-process, P2WPKH witness (`embit`) | Esplora |
| XRP | `r…` | — **receive-only** (`plan_transfer` refuses with a named reason) | XRPL JSON-RPC for balances |
| Base / Polygon | `0x…` | in-process, EIP-1559 (`eth-account`) | public RPC |

XMR needs the reference wallet code — there is no safe pure-Python RingCT signer, and custody was
not an option. BTC and EVM sign inside `walletd` with a 32-byte key that lives in the same sealed
keystore as everything else, so there is no extra daemon for them. EVM chains share one key (one
address on Base and Polygon); tokens (USDC) are supported per chain. XRP is the exception: the
address, balance and destination-tag plumbing is real, but this release does not sign XRP payments.

Which coins a wallet holds is a setting. It ships with **XMR, BTC and XRP** — three addresses, three
balance rows, nothing to explain. Everything else (ETH, USDC, POL, the simulator) is one click away
in the pane's header **coins ▾** menu, which writes `assets_enabled` and derives `chains_enabled` from
it: enabling a coin adds its chain, address and balance row, and disabling one removes them and
cancels any pending request that was waiting on it. Per-chain control still exists in
**Settings → Coins** and the CLI (`hermes wallet config set`).

## Buying something

```bash
hermes wallet pay-url https://api.example.com/report/42 --why "the report I asked for"
```

The URL is fetched, its `402` demand is read off the wire, and the price, asset, chain and payee come
from *that response* — not from the agent. The result is an ordinary pending request: a human approves
these exact terms, and a SHA256 of the demand is stored with it so the approval cannot be retargeted.
L402/Lightning and networks without an adapter are refused with a named reason instead of guessed at.

## Disclosure (what runs, what talks to the network)

- `walletd` is a local Python process holding the encrypted keystore, spawned as an argv list (never
  a shell); installed as a `systemd --user` unit with `Restart=always` and `NoNewPrivileges`.
- Network: a Monero wallet RPC on loopback (`monero-wallet-rpc`), which talks to a **public node**
  from the registry. No telemetry, no analytics, no remote code loading, no self-updater.

## Removing the plugin never touches your wallet

The keystore, the wallets, the receipts and every state file live in `<HERMES_HOME>/wallet`, outside
the plugin directory — `hermes plugins remove agent-wallet` deletes code, not money. Back up with
`hermes wallet backup` before moving machines.

## Status

`0.3.0` — Monero end to end (build → approve → relay → receipt with tx key), Bitcoin (P2WPKH,
signed in-process) and EVM (Base/Polygon, ETH/POL/USDC, signed in-process), 402 purchases settled
with an EIP-3009 authorisation, XRP receive-only, sandboxes with withdraw-on-revoke, autopays
(create, pause, resume, edit the interval, delete), a passphrase prompt the operator can turn off
with an explicit warning, auto-lock on a clock, per-order subaddresses with payment proofs,
idempotent retries, 22 agent tools (the list in `plugin.yaml`, asserted against registrations by
`tests/test_packaging.py`), and a Desktop pane with a per-coin menu.

`docs/HARDENING.md` records every external review finding and how it was closed; `docs/UPDATE-SAFETY.md`
says which Hermes surfaces it uses and what happens when one changes.

MIT.

> **Catalog status:** `hermes plugins validate .` passes, including the security scan (`safe`). The
> earlier `crypto_mining` finding was a false positive on the official Monero download host and no
> longer trips admission. The public mirror is
> [`BlackHelio/agent-wallet`](https://github.com/BlackHelio/agent-wallet), pinned at a 40-character
> SHA in the catalog entry.

## What it cannot do yet

Stated plainly, because a wallet that overstates itself is the one you should not trust:

- **Real-funds broadcast is unproven.** Every step is exercised — plan, policy, approval, signing,
  receipt, verification — but the last hop has not carried a real transaction, because the wallet
  holds no funds. Treat a mainnet send as untested until you have watched one confirm.
- **XRP is receive-only.** Addresses, balances and the XRP ledger adapter are real; `plan_transfer`
  raises `XRP can receive and hold funds, but this wallet does not sign XRP payments`. There is no
  testnet rehearsal for it either: this release simply has no XRP signer.
- **The sandbox profile allow-list is cooperative.** It compares the profile name the caller
  declares, which stops a misconfigured agent but not a hostile process running as the same uid. The
  OS user is the real boundary — see `docs/THREAT-MODEL.md`.
- **The 402 demo's loopback seller** needs operator scope: `hermes wallet pay-url … --allow-loopback`
  is refused with `not a public address` until the CLI presents the operator token on that path. The
  quote itself is covered by `tests/test_pay402.py`.
- **No hardware wallet, no multisig, no custody.** A compromised host while the wallet is unlocked is
  outside what this design can stop.

## Documents

| File | What it holds |
|---|---|
| [`docs/BUILD-LOG.md`](docs/BUILD-LOG.md) | What was built, what was verified, and every bug found along the way |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Components, trust boundaries, IPC contract, plugin surfaces used |
| [`docs/THREAT-MODEL.md`](docs/THREAT-MODEL.md) | Assets, adversaries, controls, explicit non-goals |
| [`docs/DEMO.md`](docs/DEMO.md) | The 90-second demo, exact commands, what to say |
| [`docs/DEV-SETUP.md`](docs/DEV-SETUP.md) | Public-node setup, no chain data anywhere |
| [`docs/RAILS-AND-STANDARDS.md`](docs/RAILS-AND-STANDARDS.md) | x402 / MPP / AP2 / ERC-8004, why rail-agnostic |
| [`docs/CATALOG-COMPLIANCE.md`](docs/CATALOG-COMPLIANCE.md) | Every admission rule mapped to a design decision |
| [`docs/UPDATE-SAFETY.md`](docs/UPDATE-SAFETY.md) | Which Hermes surfaces it uses, and what breaks if one changes |
| [`CHANGELOG.md`](CHANGELOG.md) | What 0.3.0 adds, hardens and still cannot do |
| [`PLAN.md`](PLAN.md) | Original scope, cut ladder, risk register |

## Not this

Not a custodial service, not a token, not a DEX, not a hardware wallet. If a design decision
requires holding someone else's funds, it is out of scope.
