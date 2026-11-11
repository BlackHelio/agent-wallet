# agent-wallet

**The agent can ask for money. It cannot approve it, and it never holds a key.**

Hermes plugin: the agent asks, a human types a six-digit PIN on a card that shows exactly where the
money goes, a capped budget can be delegated, and every decision — approved *or denied* — lands in a
tamper-evident receipt chain. Keys live in a separate daemon the agent cannot read, run or approve
through.

```
$ hermes wallet status
   ▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄
  █  ┌───────────┐  █   agent-wallet
  █  │ ▓▓▓▓▓▓▓▓▓ │  █   UNLOCKED
  █  └───────────┘  █   keys live in walletd only
   ▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀   XMR/BTC/XRP

  wallet     unlocked (locks in 15m)
  coins      XMR, BTC, XRP          chains  Monero, Bitcoin, XRP
  signer     ready                  gate    PIN on every payment
  caps       BTC 0.001, XMR 0.5, XRP 50
  pending    0   autopays 0   6 archive(s)
```

```
Agent (tools only) ──request──▶ walletd (keys · policy · receipts) ──plan/sign/relay──▶ public node
       │                                ▲        │
       └──── cannot reach: keystore, seed, approvals ────┘
                                        │
                       Human (6-digit PIN + click) ──approve──┘
                                        └──append──▶ Ed25519 hash chain
```

## Goals

1. **A model must never be able to move money on its own.** Every payment is either authorised by a
   human typing a six-digit PIN, or drawn from a budget that human already moved on-chain. There is no
   third path — including when the agent is prompt-injected.
2. **A grant is a grant: quantity *and* destination.** Caps bound the amount; the sandbox bounds where
   it can go (allowlisted addresses or contacts a human saved). Asking for a budget gives the agent
   nothing until the operator funds it.
3. **Every decision is provable afterwards.** Approved, denied, refunded or destroyed — each is a
   signed receipt in a hash chain that verifies offline and detects truncation. An audit is reading
   the ledger, not trusting the model's summary.
4. **No infrastructure to run.** Public nodes and a self-provisioned signer binary; durable state in
   one home directory; nothing in a container, no node, no database, no indexer.
5. **Boring to operate.** One command to demo, one GO/NO-GO check before real money, one script that
   says the plugin survived a Hermes update, and a pane that is honest when it is locked or offline.
6. **Say what it cannot do.** The limitations are listed below in the same detail as the features.

### Non-goals

- Custody, pooling or anything that resembles holding other people's funds.
- Being a mixer or a privacy tool beyond what the underlying chains already give you.
- A trading interface, a price oracle, or a DeFi router. It sends and receives; that is the whole job.
- Replacing a hardware wallet for large balances. If you are holding rather than paying, this is the
  wrong tool.

## Why this exists

Agents are starting to pay for things — API calls, data, compute, each other. Two failure modes
dominate: the agent holds a key and a prompt injection drains it, or the human approves so often that
approval becomes a rubber stamp. This plugin is built around one rule: **the model is never in the
payment path.** It can ask. A separate process with the key decides, and a human authorises anything
above a budget they granted.

## What it does

| Capability | Detail |
|---|---|
| **Human gate** | A six-digit PIN is typed in the Desktop page (or TTY): it unlocks the keystore *and* authorises each manual payment — an agent that finds the operator token on disk still cannot approve. The pane shows nothing but the unlock gate while locked. The passphrase is the stronger key and the recovery secret: the way in when the PIN is locked out or the wallet was restored on another machine, and the only way to change the PIN. |
| **Spend policy** | Per-asset per-tx and daily caps, payee allowlist, chain allowlist, approval threshold with typed-amount confirmation. Enforced in the signer, not in the plugin. |
| **Standing approvals ("auto-pay")** | Capped per payment, per interval and lifetime, expiring, payee-exact. The agent may propose, pause and revoke; only a human activates, and every payment inside the caps still comes from an agent request — there is no timer that spends on its own, so "pay rent monthly" means the agent asks each month and the caps decide. |
| **Sandboxes** | A real sub-account funded once with a budget a human approves. The agent can ask for one (`wallet_request_sandbox`): the sandbox is created **unfunded** and the funding transfer stays pending until the operator approves it, so asking never gives the agent spending room. By default it may spend inside it **only to a destination a human already blessed**, and every such payment still asks for the PIN. A human can also *hand the budget over* (`sandbox unattended`, or **Hand it over** in the pane): that costs the passphrase and the PIN once, and from then on the agent pays **any address** from that budget, without asking, until the money or the clock runs out — the ceiling is the containment, the main wallet is untouched, and asking for more means a fresh PIN. Narrowing `--pay-to` still wins over the hand-over. A sandbox auto-pays only addresses in its `allowed_payees` (`sandbox create --pay-to`) or saved as a contact *by a human*; anything else is filed for approval. An agent's own proposal cannot name payees or profiles (they are dropped and reported as `ignored`), `mode="pooled"` is refused on chains that can hold a real account (a pooled budget there would arm an account no sweep would empty), revoking cancels a pending funding request, a closed or expired budget cannot be re-armed by a late approval, and only an operator can sweep. It stops at `budget - spent - fee` (the fee is charged to the budget), at expiry, or on revoke, which returns the remainder. Status is one of `active`, `expired`, `unfunded`, `revoked` or `destroyed`; every row also carries `revoked_at`, `was_destroyed_with`, and the sub-account's real balance (`live_balance`, with `live_balance_state` = `live` / `locked` / `unavailable` / `unsupported` and `live_balance_error` saying why it could not be read). Optionally restricted to named Hermes profiles. `docs/SANDBOX-DESIGN.md` explains why this is the security story: on Monero the money physically left the main account, so a hijacked agent can spend a budget, not the wallet; on Bitcoin and the other account-less chains the sandbox is a *pooled* budget enforced against the main account (no transfer, no confirmation wait) — containment there is policy, not custody. |
| **Receipts** | Ed25519-signed, hash-chained, offline-verifiable (`hermes wallet verify`), and anchored so a truncated chain is detected. A denial is a receipt with the rule and the numbers that refused it. While the wallet is locked there is no ledger to append to, so the refusal is queued in memory and written on the next unlock marked `deferred` — it is delayed, never lost. |
| **Coin preference** | `prefer_monero` (**off by default**): turn it on and an *agent-filed* payment in another coin is refused with `prefer_monero_needs_reason` unless it states why — and that reason is stored on the request and in the receipt, so exceptions are visible rather than routine. Operator approvals, sweeps and sandbox funding are exempt; `pay_url` gets no free pass either. Toggle in Settings, or `hermes wallet config set --json '{"prefer_monero": true}'` (passphrase-protected). |
| **No nodes** | A registry of public endpoints (cakewallet, stackwallet, mempool.space, blockstream, base, publicnode) with health probes and failover. Chain data is never stored locally. |
| **One-time sandboxes** | `--one-time`: one agent payment, then a countdown (default 30 min, `--destruct-after` 60..86400 seconds, per-sandbox), then the remainder is swept home and the sandbox destroys itself. It never destroys while it holds funds — a blocked sweep keeps it alive with a reason. Receipted as `self_destruct_armed` / `swept` / `destroyed`. |
| **Recovery** | `wallet backup` writes one encrypted archive (keystore, settings, sandboxes, ledger, contacts, mandates, pending requests, idempotency keys); `wallet seed` exports the recovery words behind operator scope + passphrase + explicit confirm. |
| **Retries** | `idempotency_key` on a request (or `wallet pay-url`) makes a retry return the request that already exists, persisted across restarts — a timeout or a crash cannot turn one payment into two. |
| **Receiving** | `wallet receive --label "order 42"` opens a fresh Monero **account** per payment (a subaddress belongs to account 0, so it would share the main balance and could not be merged on its own) and returns an `ord_…` id. The pane has **no orders list**: money is tracked by the balance, which cannot disagree with the chain. `wallet_incoming` (CLI: `hermes wallet incoming`) still reports each order's age and `waiting` / `partial` / `paid` / `closed` status for whoever wants the per-address view, and `wallet proof <txid> --tx-key …` proves a payment arrived with the payer's transaction key. |
| **Simulation** | `sim-mainnet` keeps its own **play balances** in `<wallet home>/sim-balances.json`: account 0 is seeded with 1 XMR on first use, and funding, spending, sweeping and teardown all move that fake money the way a chain would. It broadcasts nothing and stamps `[SIMULATED]` on every receipt — so the demo shows a budget being spent and swept, not a number going up. |

## Install

```bash
# This tree is not published to a public mirror yet: install from a clone or a copy you already have.
cd agent-wallet

# Local checkouts need the `file://` form: a bare path is parsed as `owner/repo` and Hermes tries to
# clone it from a forge. The scan rates this tree `caution` (keyword false positives, see "Catalog
# status" below), so `--force` is what accepts it:
hermes plugins install file://$PWD --force --yes-deps
hermes plugins enable agent-wallet

hermes plugins validate .            # 16 checks; the security scan is the one that fails, see below
hermes wallet demo                   # keystore, signer, sim payment, receipts — one command
hermes wallet demo --with-402        # ...plus a real x402 quote, filed as pending
```

`hermes wallet demo` creates a keystore on a throwaway home, starts the signer, funds the simulator and
prints the six-digit demo PIN (`123456`) — the same digits that unlock the pane in a demo wallet. A
wallet you intend to use with real money should get its own PIN: `hermes wallet pin set`.

After a Hermes update, one script answers "is it still intact?" — `scripts/update-check.sh`
(validation, the pane the app actually loads, the daemon, the receipt chain, and the dashboard
routes). It is read-only and exits non-zero on a regression.

`wallet demo` does the whole first run — installs the signer binary, creates the wallet, files a
payment, approves it and prints the receipt — on the simulator chain, so it costs nothing (the
simulator seeds itself with 1 XMR of play money; no real funds are involved). `--with-402` turns on
USDC on Base on demand, starts the bundled x402 seller, and files its quote as a pending request.
It is the *offline* demo; the recording path when this wallet holds real value is **ETH on Base**
and starts `scripts/live_view.py` (see `docs/DEMO-PROMPTS.md`). Then:

```bash
hermes wallet service install     # one supervised daemon per wallet home
hermes wallet status              # banner, coins, caps, pending
hermes wallet coins --enable ETH  # add a chain, its address and a balance row in one step
```

Desktop: sidebar **Wallet** (opt-in under Capabilities → Plugins). Everything below works without it.

### Platforms

| | Daemon, CLI, pane | Simulator, BTC, EVM, XRP (receive) | Monero | Background service |
|---|---|---|---|---|
| Linux x86-64 | yes | yes | yes (bundled signer) | `systemd --user` |
| Linux ARM, macOS | yes | yes | no — the pinned signer is an x86-64 Linux binary | starts on demand |
| Windows (native) | yes | yes | no — same reason | starts on demand |

On Windows there are no Unix sockets, so the daemon listens on a loopback port it picks itself and
writes `tcp://127.0.0.1:<port>` into `<wallet home>/walletd.sock`; the same capability tokens
authenticate it, and the wallet home lives under `%LOCALAPPDATA%\hermes\wallet`. File privacy there
comes from the user profile's ACL rather than `chmod 0600`. `hermes wallet signer install` says
`unsupported_platform` instead of downloading a binary that cannot run.

## Two minutes, no money

```bash
hermes wallet coin XMR          # one coin, spelled out: art, balance, address, caps
hermes wallet balances XMR      # the same view, from the balances verb
hermes wallet gas               # gas coin per chain: held, the floor it keeps, how many transfers left
hermes wallet gas reserve base-mainnet 0x…   # where a human sends a refill (the wallet never spends it)
hermes wallet demo --with-402   # request → policy → approve → receipt → verify → x402 quote, no funds
hermes wallet receive --label "order 42"   # a fresh account and an ord_… id for one payment
hermes wallet incoming                      # that id, its age, received vs expected (CLI only — the pane has no orders list)
./scripts/demo.sh             # the same story, scripted, for a screen recording
hermes wallet status
hermes wallet nodes           # which public endpoints are healthy right now
hermes wallet verify          # receipt chain; exit 1 on tamper
```

`hermes wallet coin <ASSET>` prints the coin's **logo** — the CC0 `spothq/cryptocurrency-icons` set
rasterised into 20×10 braille pixels, in the coin's colour — above its balance and state, the network
and provider answering, the address, the caps that apply and the two commands you would actually run. Art appears only on a terminal: `--json`, a pipe, `NO_COLOR`, `WALLET_NO_ART=1` or
a narrow window all fall back to plain text, and `WALLET_ART=1` forces it (for `| less -R`).

A refusal is machine-readable: daemon-side refusals print `error: <code>: <message>` on stderr and
exit `2` (local argument mistakes exit `2` too, with a JSON `{"ok": false, "error": …}` object), so a
script branches on the code instead of matching prose.

With real funds, when you want them:

```bash
hermes wallet requests                        # what is waiting, with approval ids
hermes wallet approve req_… --amount 0.01     # decoded fields, y/N, then the six-digit PIN
hermes wallet deny req_…                      # a request id is enough; no approval token needed
hermes wallet pay-url https://api.example/report/42 --why "the report I asked for"
hermes wallet sandbox create --kind monero-mainnet --budget 0.05 --hours 24 --label "api budget"
hermes wallet sandbox create --kind monero-mainnet --budget 0.002 --one-time --destruct-after 900 \
  --label "one purchase"        # dies 15 min after the payment, after sweeping what is left
hermes wallet sandbox list      # status, live_balance, destroy_in, revoked_at
hermes wallet sandbox arm --sandbox-id sbx_…   # switch on a budget the agent asked for on BTC/EVM
```

## What you actually type

```console
$ hermes wallet demo
0. bootstrap  creating the keystore in a throwaway home (keys stay in the daemon)
   demo PIN   123456  (a throwaway home; the pane asks for it before each payment)
0. bootstrap  installing the Monero signer (official binary, SHA256-pinned)
1. request   0.001 XMR on sim-mainnet → 42h3Nh335aTjQmo8…
   policy    allow=True rule=manual_approval code=needs_operator
2. approve   request req_0500a8fc05e4 (operator)
   status    sent  simulated=True
   tx        simd1dcad99c0dbe0887febe…
3. receipt   chain ok=True entries=6
   #6   spend_request  approved     0.001 XMR  [simulated]

$ hermes wallet request 10$ XMR adrian --why "credits"
  request   req_cad2879c76f6  0.016364000000 XMR  monero-mainnet
  policy    allow=True rule=manual_approval code=needs_operator
  approve   hermes wallet approve req_cad2879c76f6 --amount 0.016364000000 --yes
```

## Your two secrets

One secret you type all day, one that is the key to the file:

| | what it does | how long it lasts |
|---|---|---|
| **PIN** (6 digits) | unlocks the wallet **and** authorises one payment | until the wallet auto-locks (15 min) |
| **passphrase** (8+ chars) | the stronger, portable key: the way in when there is no PIN yet, when the PIN is locked out, and the only way to change the PIN | until the wallet auto-locks |

So the everyday flow is one secret: unlock with the six digits, approve with the same six digits. The
passphrase is the recovery secret — it opens the file and is the only way in on a restored machine or
after the PIN has been locked out, but it is not what you type to pay.

```console
$ hermes wallet unlock --pin-stdin       # one line of stdin: the six digits
$ hermes wallet pin set                  # first PIN needs the passphrase; later changes take either
$ hermes wallet passphrase change        # current, then the new one twice
$ hermes wallet pin status               # set?, how many attempts left, locked until when
```

In the pane the unlock bar asks for the PIN when one is set — type the six digits there and it is sent
as a PIN, not as a passphrase — and a six-digit secret typed into a passphrase field opens the wallet
too, because a passphrase is eight characters or more so it can never be mistaken for one.
`PIN · change` / `passphrase · change` sit in the header.

**The pane is a PIN page until it is unlocked.** While locked it draws one field and one button —
nothing else, no greyed-out balances, no addresses, no pending cards — because a blurred figure still
tells whoever can see the screen how much is there, and making the PIN look optional is how a gate
becomes a formality. The passphrase is one link away ("forgot the PIN? use the passphrase") for the
operator whose PIN is locked out. A **first run** is a three-step setup card and nothing else: what
the passphrase is and that it has no recovery, then the six-digit PIN, then the addresses to fund from
a small amount. Creating the keystore unlocks it in the same click, so the same secret is never typed
twice.

**What protects a six-digit PIN**, in three layers, because six digits are a million guesses:

1. **It is bound to this machine.** The PIN does not decrypt the file with the digits alone — the key is
   `blake2b(scrypt(PIN), device_key)` where `device_key` is 32 random bytes kept *outside* the wallet
   directory (`.<wallet>-device.key`, 0600). Copy `keystore.json` anywhere else and the **correct** PIN
   does not open it: `pin_needs_device`. There is nothing to brute-force, because the half that is not in
   the file is 256 bits.
2. **The KDF is deliberately expensive**: `scrypt(n=2^19)`, 537 MB, ~1.2 s per guess — 14 days for a
   million guesses on one core. That is what an attacker with *this* machine (and therefore the device
   key) still has to pay.
3. **The online path is finite**: four guesses, then the fifth locks it for 30 s and every wrong PIN
   after that is a longer lockout (2 min, 8 min) — the third lockout closes the PIN path entirely until
   the passphrase is proved. That is **seven wrong guesses and the six digits are shut**, so a million
   guesses cannot be spread over an hour of retries.

The honest residual: someone who already has shell access on this machine has the device key and the
file, so the wall is layer 2 — and that attacker also owns the running daemon. The passphrase stays the
portable key, is **not** device-bound, and is the only way in on a restored machine or after lockouts.
Everything else is unchanged: the keys never leave the daemon, and no payment moves without the PIN.

## Swaps

A payment you cannot make is sometimes a conversion away: a chain with USDC and no ETH for the fee, a
bill in a coin you do not hold. A swap is a payment to a contract, so it is gated like one.

```console
$ hermes wallet swap quote 0.5 USDC ETH
0.5 USDC → 0.000238621596522 ETH
  route      BaseSwap V2 (0x327Df1E6de05895d2ab08513aaDD9313Fe505d86)
  minimum    0.000237428488539 ETH  (50 bps of slippage allowed)
  operators  baseswap-v2, sushiswap-v2
  nothing was filed: add `do` to ask a human to approve it

$ hermes wallet swap do 0.05 ETH USDC --why "gas for the USDC payout"
filed req_9f2c1a77d0e4 — 0.05 ETH → 0.103061 USDC (min 0.102545)
  route   BaseSwap V2 · fee 0.0000112 ETH
  approve with the PIN in the pane: approval apr_… expires 2026-11-11T09:41:07Z
```

- **The router is code.** Only the operators pinned in `walletd/chains/evm.py` can be used; the caller
  supplies a coin, an amount and a coin to buy — never `to`, never `data`. `settings.swap_routers` can
  narrow the list and can never add to it.
- **The minimum out is policy.** `swap_slippage_bps` (default 50 = 0.5%, clamped to 500) decides the
  worst rate; it goes into the calldata as `amountOutMin` and onto the card. The daemon re-quotes before
  signing and refuses if the route degraded past it — before any gas is spent.
- **Exact approval, then the swap.** An ERC-20 sale sends `approve(router, exact amount)`, waits for it
  to mine, and only then sends the swap. No unlimited allowances, ever.
- **The PIN converts.** `swap do` files a card; the agent may quote and may file, but filing only ever
  produces a card.
- **Two switches.** `swaps_enabled` off = the daemon refuses every swap. `suggest_swaps` off = the
  operator can still convert; the agent may not propose one.

## Deposit codes

Every wallet row in the pane has **deposit QR**: the chain's own URI (`monero:`, `bitcoin:`, `ripple:`)
with an amount if you type one, drawn as a code a phone can scan — or the bare address when the URI is
longer than the code can carry, with the pane saying which it drew. `scripts/qr_check.py` compares that
encoder against an independent implementation cell by cell, across every mask.

## Getting paid

```console
$ hermes wallet deposits create --label "order 42"
  send to: 41GxUtmLCNgWW2ATaJEHzU54HT2KpJLNkBgAEYuckkAH4aQk8kRWCT9u3d88KZ3M24t9314LK8yKud4rWUs3S3NCNv23aVM
  it merges into the main account automatically once the funds unlock.

$ hermes wallet deposits list
  dep_f1c19a4a  order 42  0.05 XMR (0.05 spendable)  auto

$ hermes wallet deposits merge --deposit-id dep_f1c19a4a
  "merged": "0.04997", "fee": "0.00003", "sent": true
```

Each deposit address is its own Monero **account**: money arrives there and the balance shows what is
spendable (incoming XMR unlocks ten blocks after it is mined), and the daemon merges it into your main
account on the reaper's clock — or immediately, when you run the command. Every merge writes a receipt
with the amount, the fee and the transaction. Dust that cannot pay a fee is skipped, and says so.

**The pane no longer lists them.** Deposit addresses and the contact book are off the page: an address
book and a list of unused addresses read as clutter on a wallet whose job is to show what you have and
what it is about to pay. Both are still in the CLI — `hermes wallet deposits` and `hermes wallet
contacts` — and an agent may ask *what arrived* (`wallet_deposits`); only you can create, arm or merge
one. The pane still draws a **deposit QR** per asset row (previous section), which is how you get paid
on screen.

### Operator cleanup ops

Four daemon methods take operator scope and exist because a wallet used for testing accumulates rows
that describe nothing. None of them can touch money:

| op | forgets | never touches |
|---|---|---|
| `orders_purge` | incoming orders that received **nothing** | an order paid even in part; the money in the account |
| `requests_purge` | denied / cancelled / expired requests with no tx hash | anything pending, approved, or carrying a `tx_hash` |
| `sandbox_purge` | sandboxes already destroyed, revoked or expired | a budget still holding money, or one with any history of spending |
| `ledger_archive` | — (moves the receipt chain aside so the view starts clean) | the old chain, which stays on disk; spend caps are not reset |

`orders_purge` exists because archiving the ledger does not forget orders — `receive_address` wrote a
receipt per address and the chain keeps it — so a test wallet listed phantom orders forever.

### Sandboxes cost what the chain charges

On Monero a sandbox is a real account: a transfer funds it, and it gets its own balance and address.
On Bitcoin that same shape is a bad deal — a fee that can eat a small budget and a 10–60 minute first
confirmation, i.e. longer than the task it is meant to protect. So on chains like that a sandbox is
**pooled**: the cap and the destination allowlist are enforced by the daemon against the main account,
nothing is moved, nothing is funded, nothing is swept. `hermes wallet sandbox create` prints which
mechanism it used, and the pane labels it.

## How a payment actually happens

```
you    send adrian 10$
agent  wallet_request_payment → daemon quotes it → pane draws the card

  ┌──────────────────────────────────────────────────────────────┐
  │ pay to         Adrian · 4A…9f                                │
  │ amount         0.016364 XMR                                  │
  │                ≈ 10 USD     (1 XMR = 611.09 USD)             │
  │ chain          Monero                                        │
  │ network fee    0.00012 XMR                                   │
  │ address from   openrouter.ai · Credits (invoice-link)         │
  │                https://openrouter.ai/credits                 │
  │ PIN [ •••••• ]                        [ Approve ]  [ Deny ]  │
  └──────────────────────────────────────────────────────────────┘

you    type the six digits → the daemon signs → receipt #412 (0.016364 XMR, fee 0.00012)
```

- **Every payment out of the wallet needs the PIN.** Not just large ones, not just the first: a
  budget and an autopay relax *who may decide the amount*, never the human's confirmation of the
  destination. There are exactly two exceptions, both a deliberate act by the operator, both costing
  the passphrase **and** the PIN, both red in the pane: an autopay marked `unattended`, and a budget
  handed over with `sandbox unattended`. The second one is the sharper of the two — inside it the
  agent may pay *any* address — so it is bounded by the money in the budget and by its expiry, it
  never touches the main account, and more money means another human decision.
- **The PIN authorises one card.** The pane sends the amount and payee it drew; the daemon compares
  them to the request and refuses the PIN if they moved (`card_changed`). A card that says one thing
  and a request that says another is the whole attack.
- **Wrong PINs are counted**: four guesses, then 30 s, 2 min, 8 min — and the third lockout closes the
  PIN path until the passphrase is proved, i.e. the seventh wrong PIN, not the fifteenth. Persisted, so
  a restart does not hand the attacker a fresh four.
- **The PIN opens the wallet; the passphrase still does more.** The PIN cannot export keys, change caps,
  add a contact or turn the gate off — those need the passphrase, which the agent never has. The
  passphrase is the recovery key: the only way in when the PIN is locked out or the wallet restored on
  another machine, and the only way to change the PIN.
- Set or change it in the pane (or `hermes wallet pin set`). There is no passphrase-only path for a
  wallet that has a PIN: `pin_every_payment` defaults on, and with no PIN yet the daemon refuses every
  approval with `pin_not_set` instead of quietly falling back — the way in is the setup card, which
  asks for the passphrase once and then the six digits. Turning the PIN gate off is itself a
  passphrase-level change, and the pane says so; nothing ever approves silently.

## Before you send real money

```bash
hermes wallet ready --amount 0.002        # GO/NO-GO, builds the transaction, sends nothing
```

One call checks the keystore, the signer, the node height, the balance, the fee against your cap,
the per-tx cap and the receipt chain — and plans the exact transaction it would send. It prints
`NO_GO` with a remedy line for anything blocking, so a live test does not discover a missing signer
after you have typed the passphrase. `--json` is the machine-readable form, and the agent can call
the same check as `wallet_readiness` before promising a payment will work.

## Check it yourself, without reading the source

Four scripts, each self-contained, each on a throwaway wallet (no funds, no network needed for three):

```bash
python3 scripts/spec_check.py     # 21 checks: "send adrian 10$" and "deposit 10$ into my vendor",
                                  #   including that the agent cannot price a private address
python3 scripts/rehearse.py       # 17 checks: bootstrap → receive → deny → approve → sandbox fund/
                                  #   spend/sweep → verify → backup → ready → live injection battery
python3 scripts/pane_probe.py     # 15 checks: the Desktop page's real HTTP API, card binding,
                                  #   PIN gates, balances, receipts
python3 scripts/qr_check.py       #  49 checks: the pane's inline QR encoder, cell by cell, against
                                  #   the `qrcode` package
python3 scripts/secret_scan.py    # patterns + entropy, and (--history) every blob ever committed
python3 scripts/demo_stage.py     # stages a wallet for recording: contact, USDC, a receipt, a live
                                  #   402 quote — and prints a recording sheet
python3 -m unittest discover -s tests -t .   # 542 tests
```

`scripts/spec_check.py` is the one to run first: it calls the same 23 tools the model calls, in the
order a model would, and prints what the approval card shows for a 10-dollar request — the coin amount
the wallet quoted, the fee, whether the payee is new — then proves nothing was sent until the PIN. The
`demo_stage.py` wallet is the *old* USDC-and-x402 staging; the demo path in `docs/DEMO-PROMPTS.md` is
ETH on Base.

## How a sandbox bounds a compromised agent

![How a sandbox bounds a spend: the human grants a capped budget, walletd checks every spend against
the sealed row in one place, and the agent can only spend inside it](docs/sandbox.png)

A sandbox is a **budget, not a permission**: the money is moved once and the agent may spend inside it
without a PIN, because the ceiling — not the destination — is what contains it. Every spend still passes
one ordered check in `walletd` (`_sandbox_decision` / `_sandbox_limits`), and the fields a forged
confirmation would want to change (address, arm, allowlist) are sealed: edit `sandboxes.json` by hand and
the row reads `tampered`, every spend is refused, and the leftover can still be swept home because that
destination comes from the seed rather than the row.

`docs/sandbox.svg` is generated by `scripts/make_sandbox_diagram.py`, so the labels track the error codes
in the code rather than a drawing made once.

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
- **A sandbox pays destinations, not just amounts.** The budget is a spending limit; the destination
  is the thing a prompt injection changes. A sandbox auto-pays only addresses the human allowlisted
  (`--pay-to`) or saved as a *contact by a human* — the agent's own address book does not count.
  Anything else is filed for approval, so the injected payment comes back to you instead of leaving.
  Contacts the agent saved are labelled as such, and the pane says every budget's rule on its card.
- **Monero is the default coin, but the preference is opt-in.** `prefer_asset` still defaults to XMR,
  so an agent that does not name a coin is pointed at Monero; turning on `prefer_monero` goes further
  and refuses an agent-filed payment in another coin unless it says *why* — the reason is carried in
  the request and in the receipt, so exceptions are visible instead of routine. Human-approved
  payments and sweeps are never blocked by it, and `Monero only` is still there as the hard version.

- **A sandbox cannot be blessed by the agent.** `wallet_request_sandbox` drops any
  `allowed_payees`/`allowed_profiles` an agent proposes and says so; only the operator sets them
  (with the passphrase). A late funding approval cannot re-arm a revoked or destroyed budget, and
  revoking cancels the funding request it had pending.
- **The budget is charged what it actually spends**, fee included, and every spend receipt names the
  `sandbox_id` that paid. A sweep reports the amount the chain accepted (`sent`), never
  `remaining − reserve` arithmetic: a refused sweep says `swept: 0` and leaves the balance visible.

What it does **not** protect against: a compromised host while the wallet is unlocked, a human
approving something they did not read, or a counterparty who takes the money and delivers nothing.
`docs/THREAT-MODEL.md` is explicit about each. One sandbox control is **advisory, not
enforced**: `allowed_profiles` restricts which Hermes profile may spend from a budget, but every
profile presents the same agent token, so a caller can claim any profile. Treat it as a guard rail
between cooperative profiles, not a security boundary.

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
The bundled demo seller quotes USDC on `base-mainnet`; run it through `hermes wallet demo --with-402`,
which enables USDC first, or the quote fails with `bad_url`/`chain_not_enabled`.

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

`0.9.2` — the pane is a PIN page until it is unlocked and a three-step setup card on a first run, the
Orders/Receiving and Deposit/Contacts sections are off it (the operator ops `orders_purge`,
`requests_purge` and `sandbox_purge` clean up what they left behind), `prefer_monero` ships off, and
`scripts/live_view.py` watches a payment land; `0.9.1` — the PIN is bound to the machine (scrypt(n=2^19)
+ a device key outside the wallet dir) and the online ladder shuts it after seven wrong guesses;
`0.9.0` — one secret you type: the PIN unlocks the wallet and authorises each payment;
`0.8.2` — the PIN and the passphrase are changeable from the pane; `0.8.1` — the pane on a phone, on
Windows, in a light theme; `0.8.0` — swaps, on the operator's terms; `0.7.3` — the wallet keeps a gas
floor (a payment cannot leave a chain unable to pay for the next one); `0.7.2` — an old copy of an authority file cannot put an arm back (the seal is genuine, the file is stale: the budget is closed); `0.7.1` — the authority files are sealed (a forged `unattended`, blessing or autopay is refused on unlock); `0.7.0` — a budget handed over (`sandbox unattended`), top-ups, and the model told the rule
straight; `0.6.0` — Windows (loopback transport, `msvcrt` lock, no `/proc`),
limits that are re-checked at the PIN instead of only at request time, per-coin daily caps, and 52
regression tests from a line-by-line reading of the payment path. `0.5.0` — Monero end to end (build → approve → relay → receipt with tx key), Bitcoin (P2WPKH,
signed in-process) and EVM (Base/Polygon, ETH/POL/USDC, signed in-process), 402 purchases settled
with an EIP-3009 authorisation, XRP receive-only, sandboxes with withdraw-on-revoke and a real
live-balance column, one-time sandboxes, incoming payments with `ord_…` ids and receipts on close,
autopays (create, pause, resume, edit the interval, delete; `proposed`/`active`/`paused`/`expired`/
`revoked` with `created_at` and `activated_at`, and an operator-only `unattended` waiver that costs the
passphrase *and* the PIN), a passphrase prompt the operator can turn off with an explicit warning,
auto-lock on a clock, per-order accounts with payment proofs, idempotent retries,
**23 agent tools** (one per distinct act; the list in `plugin.yaml`, asserted against registrations by
`tests/test_packaging.py`), a Desktop pane with a per-coin menu, a **Prefer Monero** toggle, a sandbox destination allowlist,
`hermes wallet ready` as the GO/NO-GO gate before real money, and an update check that keeps the
plugin honest across Hermes upgrades.

Tool payloads are sized for a model rather than a UI: `wallet_status` is the compact form (~750 B)
unless `full=True`, `wallet_balances` shortens addresses unless `full=True`, and `wallet_spend`
splits `real_totals` from `simulated_totals` so play money is never mistaken for money
(`0 XMR (+0.33 XMR simulated, not real money)`).

`docs/HARDENING.md` records every external review finding and how it was closed; `docs/UPDATE-SAFETY.md`
says which Hermes surfaces it uses and what happens when one changes; `docs/PUBLISHING.md` is the
release checklist (what has to be green on the public clone before the catalog PR).

MIT.

> **Catalog status — read this honestly:** `hermes plugins validate .` **passes** with two cautions —
> `proc_access` and `crypto_mining` (the latter lands on the compliance doc's own prose, which has to
> name the rule to disclose it). The `proc_access` one is a true positive: `walletd/compat.py` reads a
> process's argument vector from `/proc` where the platform has it, so `wallet down` can be sure the
> pid it is about to stop is *this* wallet home's daemon and not another profile's. It reads, never
> writes, and `ps`/`psutil` are the fallbacks on platforms without `/proc`. Documented in
> `docs/CATALOG-COMPLIANCE.md`. `hermes plugins install` still asks you to confirm a caution verdict — that is what
> `--force` is for. The suite is 522 tests, `render-check: OK`, `qr-check: 49 checks, 0 mismatches`,
> `scripts/spec_check.py` 21/21. **There is no public mirror right now**: the catalog entry is
> prepared and on hold by the owner's decision (`docs/PUBLISHING.md`), so install from a clone you
> already have.

### The one security caveat worth reading before you fund it

On Bitcoin, EVM and XRP the keys sit inside `walletd`'s sealed payload and are signed in-process: the
agent's shell cannot reach them. **Monero is different.** RingCT cannot be signed in-process, so the
spend key lives in a `monero-wallet-rpc` that runs as *your* user, and a shell with your uid can read
its credentials and call it directly — past the caps, past the PIN. The PIN protects you from an
injected or confused agent; it does not protect you from a process you already let run as yourself.
The fix is one `systemd` unit with `User=` pointing at a second account, which needs root once.
`docs/THREAT-MODEL.md` records this as an open item rather than a closed control — and a red team
walked it: read `wallet.env`, POST `query_key`, get the 25-word seed back.

The `pre_tool_call` guard (it blocks payment-shaped tools from other plugins, and shell commands that
drive the wallet CLI or the signer) is **advisory**, not a boundary: the boundary is the daemon, the
PIN and the receipt chain.

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
- **The 402 demo's loopback seller** needs the explicit flag: `hermes wallet pay-url
  http://127.0.0.1:38091/sku/42` is refused with `bad_url: … not a public address`, and the working
  command is the same URL plus `--allow-loopback` (the daemon honours it only for operator scope and
  only for loopback). `hermes wallet demo --with-402` does both steps for you. The quote itself is
  covered by `tests/test_pay402.py`.
- **Monero needs Linux x86-64.** The signer is the official `monero-wallet-rpc` binary, pinned by
  hash for that platform only. Elsewhere the wallet runs without XMR (see *Platforms*).
- **No hardware wallet, no multisig, no custody.** A compromised host while the wallet is unlocked is
  outside what this design can stop.

## Documents

| File | What it holds |
|---|---|
| [`docs/BUILD-LOG.md`](docs/BUILD-LOG.md) | What was built, what was verified, and every bug found along the way |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Components, trust boundaries, IPC contract, plugin surfaces used |
| [`docs/THREAT-MODEL.md`](docs/THREAT-MODEL.md) | Assets, adversaries, controls, explicit non-goals |
| [`docs/DEMO.md`](docs/DEMO.md) | The 90-second demo, exact commands, what to say |
| [`docs/LIVE-TEST.md`](docs/LIVE-TEST.md) | The real-money runbook: gate, approve, prove, and what each failure means |
| [`docs/DEV-SETUP.md`](docs/DEV-SETUP.md) | Public-node setup, no chain data anywhere |
| [`docs/RAILS-AND-STANDARDS.md`](docs/RAILS-AND-STANDARDS.md) | x402 / MPP / AP2 / ERC-8004, why rail-agnostic |
| [`docs/CATALOG-COMPLIANCE.md`](docs/CATALOG-COMPLIANCE.md) | Every admission rule mapped to a design decision |
| [`docs/UPDATE-SAFETY.md`](docs/UPDATE-SAFETY.md) | Which Hermes surfaces it uses, what breaks if one changes, and `scripts/update-check.sh` |
| [`docs/PERFORMANCE.md`](docs/PERFORMANCE.md) | Measured hot paths before/after, and why the slow parts were algorithmic (not a language problem) |
| [`docs/SANDBOX-DESIGN.md`](docs/SANDBOX-DESIGN.md) | The delegated-budget design, its teardown order, and the hardening round it survived |
| [`docs/HARDENING.md`](docs/HARDENING.md) | Every external review finding and how it was closed |
| [`CHANGELOG.md`](CHANGELOG.md) | What each release adds, hardens and still cannot do |
| [`PLAN.md`](PLAN.md) | Original scope, cut ladder, risk register |

## Not this

Not a custodial service, not a token, not a DEX, not a hardware wallet. If a design decision
requires holding someone else's funds, it is out of scope.
