# Changelog

## 0.9.2 — a page you can leave open

The pane is the operator's page; this release is about what it does *not* show, plus a way to watch a
payment land without the pane at all.

- **The Orders · receiving section is gone**, and with it the "expected" amount on a new receiving
  address. Two ways to see incoming money — a balance and an order list — meant the list was the only
  thing that could disagree with the chain, and it did. `wallet_receive_address` still hands out a
  fresh address, `wallet_incoming` still reports what arrived per address if a caller asks for it, and
  `wallet_close_order` still closes a settled one with a receipt; what is gone is the page that drew
  them. The balance is the record.
- **`prefer_monero` is off by default.** It was the loudest thing a new operator hit: fund the wallet
  with ETH, ask it to pay, and be told to justify itself in Monero. The rule is unchanged and one
  toggle away (`config set prefer_monero=true`), because it is a good rule for an XMR-first operator;
  it is just no longer the price of admission. `prefer_asset` still defaults to XMR, so an agent that
  does not name a coin is still pointed at Monero.
- **`orders_purge`** (operator): forget incoming orders that never received anything. Archiving the
  ledger does not forget them — `receive_address` wrote a receipt per address and the chain keeps it —
  so a wallet used for testing listed 11 phantom orders forever, and the old pane kept drawing an
  "Orders · receiving (11 open)" section above everything else. The Orders section is gone from the
  pane entirely; a row that received even part of its amount is kept.
- **Deposit addresses and Contacts are off the page.** An address book and a list of unused
  receiving addresses read as clutter on a wallet whose job is to show what you have and what it is
  about to pay; both stay reachable from the CLI (`hermes wallet deposits|contacts`). `requests_purge`
  (operator) forgets denied/cancelled requests that never reached a chain — pending and sent rows are
  kept, so a purge cannot erase a payment.
- **The pane is a PIN page until it is unlocked.** Balances, addresses, budgets, pending cards and
  history all describe money the keystore has not opened; drawing them greyed out told whoever could
  see the screen how much was there and made the PIN look optional. While locked, the pane is one
  field and one button, with the passphrase behind a "forgot the PIN?" link. `pin_every_payment`
  defaults on and there is no passphrase-only fallback in the card: a wallet with no PIN yet answers
  `pin_not_set` rather than inviting a secret into an approval that cannot work.
- **A first run is a three-step setup card and nothing else.** It used to show an empty Assets panel
  and an empty approvals list next to "create your wallet", which reads as a wallet that is already
  broken. The card now says what the passphrase is and that it has no recovery, says the six digits
  come next and unlock the pane *and* authorise each payment, and says to fund the addresses it shows
  with a small amount first. Creating the keystore unlocks it in the same click instead of asking for
  the same secret twice.
- **A six-digit secret typed into the passphrase field unlocks the wallet.** Older desktop bundles put
  whatever the operator typed into the passphrase field, and the digits came back as "passphrase does
  not match the keystore" — a wallet that could not be opened from the pane at all. A passphrase is
  eight characters or more, so six digits can only be a PIN: `unlock` now tries the PIN slot instead
  of refusing.
- **`sandbox_purge`** (operator): forget sandboxes that are already destroyed, revoked or expired.
  Only rows with no live account and nothing ever spent are dropped, so a purge can never hide a
  budget that still holds money or a history of spending.
- **An Assets row no longer says the coin twice.** The line under `BTC · Bitcoin` read
  `Bitcoin · electrum · …` — the coin's name again, and never the network it is on. It now says
  `Bitcoin mainnet · electrum · …` (the second line comes from its own `CHAIN_NETWORK` table; every
  chain the balance tables ship with has an entry, and a test network says it is a test network).
- **`desktop/ui-contract.mjs`**: the page rendered in Node against chosen daemon answers, in all
  three states, and checked for what it must not say — no Orders/receiving section or order count on
  a loaded wallet, no coin named twice in a row, nothing but the gate when locked, nothing but the
  setup card on a first run. Wired into `scripts/check.sh`.
- **`scripts/live_view.py`**: a read-only page (default `http://127.0.0.1:8791/`) that shows one spend as
  it happens — amount, confirmations counting up, the tx hash linking to the explorer — next to the
  sandbox budget it came out of and the gas the chain still holds above its floor. It reads the
  daemon over its own socket with the operator token and can move no money. A background thread polls
  the chain; every HTTP response is served from the last good snapshot, so a slow RPC never stalls
  the page (an inline `gas` read took 5.5 s and hung every poll).
- The simulator is **not** part of the default chain set and stays opt-in (`simulated: false`); the
  live wallet no longer carries it, so receipts contain only money that moved.

## 0.9.1 — a six-digit PIN that is not a six-digit problem

0.9.0 made the PIN open the wallet and protected it with an expensive KDF. That is not the same as
"cannot be brute-forced": a million guesses at 0.3 s is three days on one core, and less on a GPU. Three
layers now, and the first is the one that matters.

- **The PIN is bound to the machine that set it.** The slot's key is `blake2b(scrypt(PIN), device_key)`,
  with `device_key` = 32 random bytes in `.<wallet>-device.key` **outside** the wallet directory (0600).
  Copy `keystore.json` anywhere else and the correct PIN returns `pin_needs_device` — there is no
  offline guess to make, because the missing half is 256 bits. The passphrase is deliberately *not*
  bound, so a restored wallet on a new machine still opens with it.
- **`scrypt(n=2^19)`**: 537 MB and 1.2 s per guess on this host (measured, not assumed) — 14 days per
  million guesses on one core, and that is the price for an attacker who already has the machine.
- **The online path is finite**: the existing ladder now also counts lockouts, and three of them disable
  the PIN until the passphrase is proved. The ladder keys off the total fail count, so the shape is
  sharper than it reads: four guesses, then 30 s / 2 min / 8 min, and the **seventh** wrong PIN shuts the
  PIN path — not the fifteenth. A million guesses cannot be spread over an hour of retries.
  (`tests/test_credentials.py::test_the_ladder_closes_the_pin_after_seven_guesses` drives the real ladder
  rather than seeding `disabled`, because a seeded flag proves only that the flag is read.)
- `doctor` grew a **`pin binding`** line: it says when a PIN still opens the file with six digits alone
  (a slot written by 0.9.0), and when the PIN belongs to another machine. Both come with a remedy.
- Unpeppered 0.9.0 slots still open, so the upgrade is not a lockout; setting the PIN again binds it.
- **The locked pane was blank and the audit said "0 findings".** A helper declared below the unlock bar
  (`const pinSet`) was read inside that branch, threw in the temporal dead zone, and took the whole pane
  with it — for every *locked* wallet. The layout audit measured overflow, clipping, targets and
  contrast, all of which are zero on an empty page, so 18 locked renders passed. `audit.mjs` now records
  `blank` (no `#root` children, no text) and fails on it, the helpers moved above their first use, and
  `scripts/gui-audit/unlock-clickthrough.mjs` proves the locked bar asks for the PIN and posts `{pin}`.

Verified: 22 keystore tests (peppered slot round-trip, correct PIN without the device key refused, a
different device key reads as a wrong PIN, the passphrase still opens it, unpeppered slots still open,
n ≥ 2^19); 30 credential tests (the device key is outside the wallet dir and 0600, a *copied* keystore
refuses the correct PIN with `pin_needs_device`, the passphrase opens the copy, three lockouts disable
the PIN, proving the passphrase re-arms it); and the same on the live wallet: migrated, re-bound, PIN
opens it, and a copy of its `keystore.json` in another directory refuses the correct PIN.

## 0.9.0 — one secret you type: the PIN unlocks the wallet

The operator's complaint, verbatim: *"unlocking my page inside of the wallet gui and confirming stuff
should be the same pin."* He was right, and the old answer — "the PIN authorises a payment, the
passphrase opens the file" — was a design choice made for security and defended in prose instead of
being fixed.

- **The keystore now has one payload and two ways in.** `keystore.json` v3 holds the payload under a
  random 32-byte **data key**; the passphrase and the PIN each wrap that key in their own slot
  (`unlocks.passphrase`, `unlocks.pin`). Either secret opens the same wallet. Rotating one re-wraps 32
  bytes and never touches the payload, so a PIN change cannot invalidate the passphrase and a payload
  write cannot go stale against either.
- **The PIN slot is deliberately expensive:** `scrypt(n=2^17)`, ~128 MB and about a second per guess —
  sixteen times the work of the passphrase slot, because six digits are a million possibilities. That
  is the honest cost of a secret you can type from memory, and `docs/GOALS.md` no longer claims a stolen
  keystore is useless offline.
- **`hermes wallet unlock --pin-stdin`**, `POST /unlock {pin}`, and the pane's unlock bar now asks for
  the PIN when one is set (the passphrase always works too).
- **Unlock attempts get their own counter** (`pin-unlock-attempts.json`): an agent burning payment
  attempts must not be able to lock the operator out of their own wallet, and vice versa. It is plain
  JSON because it is read while locked, when there is no master key to MAC it with — it is a speed
  bump, and the scrypt cost is the wall.
- **Changing the PIN takes the passphrase *or* the current PIN.** Once a six-digit secret opens the
  wallet it is a credential in its own right, so demanding a passphrase the operator may not remember
  would just trap him. The first PIN still needs the passphrase.
- **v2 keystores are read and upgraded on unlock**, with a `keystore_upgraded` receipt. The PIN cannot be
  carried over (a hash cannot be turned back into a PIN), so a wallet that had one must set it once more.
- `lock` now drops the data key with the master key, and the `unlocked` receipt records **which** secret
  opened the wallet (`how: pin|passphrase`) — a question the ledger could not previously answer.

Verified: 15 keystore tests (either secret opens the same payload, wrong PIN is its own refusal, no PIN
slot, rotating either secret leaves the other working, re-seal keeps both wrappers, the PIN slot is
expensive, the wrappers share no salt or nonce), 19 credential tests (PIN unlocks, wrong PIN counts,
lockout refuses even the right PIN, no-PIN says so, a later PIN makes unlock available, PIN survives a
passphrase change and vice versa, the receipt says `how: pin`, locking forgets the data key), and a live
migration of the real wallet from v2 to v3.

## 0.8.2 — the two secrets, changeable from the pane

The daemon has always accepted a new PIN (`set_pin`) and re-sealing the keystore; the pane only ever
offered the *first-run* PIN form, so a PIN could be set once and never changed without a terminal —
and the passphrase could not be changed at all, from anywhere. That is the kind of gap a person only
discovers when they are trying to get into their own wallet, which is the worst moment.

- **Change PIN** — a `PIN · change` chip in the header, next to the coin menu, opens the same card the
  first run uses in a `change` mode: new PIN, again, passphrase. The old PIN stops working the moment
  it is saved, including on a card that is already open. Hidden while locked, because `set_pin`
  requires an unlocked keystore and a button that can only fail is worse than no button.
- **Change passphrase** — `passphrase · change`, next to it, and new: `change_passphrase` in the
  daemon, `POST /passphrase/change`, `hermes wallet passphrase change`. It re-seals `keystore.json`
  under the new passphrase and touches nothing else — the master key, the ledger seed, the chain keys
  and the Monero wallet password all live *inside* the sealed payload, which is why the wallet keeps
  its addresses and its PIN. Refusals: wrong current passphrase, fewer than 8 characters (the exact
  reason `123456` — the PIN — can never unlock a wallet), a mismatch, or the same passphrase again.
  The receipt records the length, never the secret, because a receipt is readable while locked.
- Both are operator-scope methods: an agent token cannot re-key a wallet or change what authorises a
  payment. `tests/test_credentials.py` opens a *fresh* state after each change, because the in-memory
  object cannot prove what landed on disk.

Verified in Chromium (`scripts/gui-audit/credentials-clickthrough.mjs`, 10/10): the chips render, the
cards open, the payloads carry exactly what `set_pin`/`change_passphrase` expect, mismatches never
reach the daemon, and a six-digit passphrase is refused before any request. The 36-render layout audit
stays at 0 findings with the two new controls.

## 0.8.1 — the pane on a phone, on Windows, in a light theme

Three defects that every other test missed, found by rendering the pane in Chromium against the
desktop app's own compiled CSS at nine viewport widths and in two themes:

- **A `<select>` was invisible in a light theme.** The face came from `var(--card, #23262c)`, and
  `--card` is not a token this app defines — so the dark fallback was the real value in *every*
  theme. Measured 1.09:1. The face is now `--ui-bg-elevated` (opaque, flips with the theme:
  #fdfdfd light / #37373a dark) and the ink is the page's own `--foreground`. `Canvas`/`CanvasText`
  looked like the obvious answer and was wrong: the app pins `color-scheme: light`, so they stay
  white on a dark page.
- **Text fields painted no ink of their own** (`color: 'inherit'` on that same dark face): in a light
  theme the value was near-black on #23262c — a field that looks empty. Six address/code blocks had
  the same face.
- **A 320 px window overflowed by 9 px.** The mandate "at most, every" group could not wrap, and the
  payee field carried a hard 18 rem floor. Both now shrink; nothing scrolls sideways from 320 to
  1920 px.
- Also: three selects lost the padding that keeps the dropdown arrow off their text (a second
  `style:` key in one object literal silently wins), the monospace stack had no Windows face
  (Consolas/Cascadia), brand colours used as 11 px text measured 3.87:1 (USDC) and are now mixed
  toward the theme ink, and every control gets a 24 px target (WCAG 2.2 AA 2.5.8).

`scripts/gui-audit/` reproduces it: 36 renders (2 states × 2 themes × 9 widths), checking horizontal
overflow, clipped and squashed boxes, target size, and contrast including ink-vs-face on fields.
Result on this build: **0 findings**. `tests/test_pane_tokens.py` locks the decisions so they cannot
silently regress.

## 0.8.0 — swaps, on the operator's terms

A conversion is a payment to a contract, so it is gated like one. `hermes wallet swap quote 0.5 USDC ETH`
asks the pool what you would get; `swap do` files a card, and the PIN converts. The agent may quote and
may file — filing only ever produces a card.

- **The router is code, not configuration.** Two Uniswap-V2 operators are pinned on Base
  (`sushiswap-v2`, `baseswap-v2`). The caller never supplies `to` or `data`, only "0.05 ETH → USDC, at
  least X". `settings.swap_routers` can narrow the list, never add to it. Polygon is deliberately absent:
  neither V2 router quoted its native USDC when probed, and an unverified route is worse than none.
- **Slippage is policy.** `swap_slippage_bps` (default 50 = 0.5%), clamped to 500 in the daemon. The card
  shows the quoted out and the minimum out, and the minimum goes into the calldata as `amountOutMin`.
- **The daemon re-quotes before signing** and refuses if the route degraded below the approved minimum —
  without spending the gas.
- **Exact-amount approval.** An ERC-20 sale sends `approve(router, exact amount)`, waits for it to mine,
  and only then sends the swap. A router compromised later can only take what that one swap was about to
  spend, never an unlimited allowance.
- **The gas floor applies.** Selling the gas coin counts the amount sold, so a swap cannot strand the
  wallet.
- **Two switches.** `swaps_enabled` off makes the daemon refuse every swap; `suggest_swaps` off lets the
  operator convert while the agent may not propose one.
- The receipt is a `swap`, not a `spend_request`: `spend` totals do not pretend a conversion was money
  given away, and `verify` covers both hashes.
- 16 new tests (522 total), including a live Base quote in both directions and keccak checks on every
  selector.

## 0.7.3 — the wallet will not spend its last gas

A payment is not the only thing a transfer costs: on an EVM chain it also spends the coin that pays
for *every* transfer, and the balance that pays for the next one is the same balance. So a payment of
"everything" is not a payment — it is the last one that chain can make. Nothing errored when the
wallet reached zero: the transfer succeeded, the receipt said `sent`, and from then on every payment
on that chain failed at signing until a human noticed and funded the address. An agent that can be
told to "send it all" could brick a chain without ever doing anything that looked like a mistake.

- **A gas floor, measured in transfers.** `gas_floor_transfers` (default 4) keeps enough native coin
  for four more transfers at the *current* fee ceiling, so the floor tunes itself to the chain instead
  of stranding a fixed amount on a cheap one and being useless on an expensive one. An explicit
  `gas_floor` (per native coin) overrides it; `0` disables it.
- **Enforced where every other limit is enforced, twice.** The check runs when the request is planned
  (so the refusal is a receipt, not an approval card) and again at broadcast, because the fee is
  re-read between the human's PIN and the send. `gas_floor` is *retryable*, like `insufficient_funds`:
  a top-up makes the same request payable, so it is not recorded as a human saying no.
- **A 402 authorisation is exempt.** It costs no gas — the seller's facilitator submits it.
- **`hermes wallet gas`**: per chain, the gas coin, what is held, the floor, how many transfers are
  left, and — when low — the shortfall and the address to send it to. A chain that holds nothing at
  all reads `unused` rather than `LOW`, so a chain nobody funds cannot make a real warning look like
  noise.
- **A refill address kept separately.** `gas_reserve` holds one address per native coin — a bigger
  gas wallet, a hardware wallet, an exchange — and the wallet never spends from it. Only the address
  is stored, so a compromised agent cannot reach the reserve, and refilling stays a human act:
  `hermes wallet gas reserve base-mainnet 0x…` (passphrase-level, because where a human is told to
  send money is policy). `wallet gas` and `doctor` print it with the shortfall.
- **The card says what is left behind.** `gas after 0.000183 ETH · keeps 0.00000312` sits next to the
  network fee, so the PIN approves a payment that leaves the chain able to pay again.
- `doctor` gains a `gas floor` check; the refusal, the card line and the report all name the same
  numbers, from one place (`_gas_view`).
- `tests/test_gas.py`: 14 tests — the floor, the exact-floor boundary, a native payment counting its
  amount as well as its fee, the explicit override, the 402 exemption, a chain with no separate gas
  coin, the report shape, the locked case, the retryable status, the card line, and the settings
  surviving a signed round trip (the `to_dict` trap that silently loosened `fee_cap` once).

## 0.7.2 — an old confirmation is not a confirmation

0.7.1 stopped a *forged* record. A red team that re-read the tree found the row beside it: a **genuine
old** one. Same-uid access is enough to copy `sandboxes.json` while a budget is armed, and if the
operator later takes the arm back (or revokes the budget), restoring the copy put the arm back — every
seal still verified, because it was a real confirmation, just not the newest one. A replayed approval
is the same attack with a different file.

- **Every authority file now carries a digest, and the ledger remembers it.** A seal proves who wrote a
  row; it cannot prove the row is current. Each save appends `authority_state {what, digest}` to the
  hash chain, and `unlock` compares the file it just read against the last one the chain recorded. A
  file that disagrees is *stale*: its budgets are closed (`revoked_at`), not merely un-armed, and an
  `authority_dropped` receipt says `stale_copy`. The leftover is still sweepable home, because a sweep
  destination comes from the seed rather than from the row.
- **The chain's own memory is checked before it is rewritten.** `_write_anchor` used to pin the newest
  receipt at unlock, so truncating the ledger reset the anchor and erased the `authority_state` history
  with it. The anchor the previous process left behind is now read first; a shorter chain is a
  `ledger_rewound` failure, the authority is dropped, and `doctor` says the receipts were deleted.
- **`allowed_profiles` is authority.** It was outside `_SEALED_FIELDS`, so editing one line widened
  which profiles may spend a handed-over budget and the change survived an unlock. It is sealed now.
- **A save during a load cannot bless a file nobody checked.** `_load_requests` reconciles in-flight
  sandbox spends and writes `sandboxes.json`; doing that before verification re-sealed a restored stale
  file and made it look newest. Verification now runs before anything can write, and `_sealed_file`
  refuses to mint new seals until the rows have been checked in this process.
- `tests/test_seals.py` gains four: the replayed arm, the resurrected revoked budget, the on-disk
  profile widening, and a truncated chain.

## 0.7.1 — a forged record cannot spend your money

- **The authority files are sealed.** `sandboxes.json`, `contacts.json` and `mandates.json` are 0600
  and owned by the agent's uid, so editing one is a shell away. An edit that sets `unattended: true`,
  `created_by: "operator"`, `enabled: true` or rewrites an address is a confirmation the human never
  gave, and no card design stops it. Every row now carries an HMAC of its authority fields keyed by the
  master key; `unlock` verifies them, drops what does not verify, writes an `authority_dropped` receipt,
  and marks the sandbox `tampered` — refused for spends, funding and arming, sweeps still allowed (they
  can only return money to this wallet's own account 0). `sandbox list` prints `TAMPERED` in place of a
  budget and the pane shows a red chip. Files from before this release are re-sealed without their
  authority: one re-bless / re-arm / re-activate per row, deliberately, because an unsigned file cannot
  prove who wrote it.
- `tests/test_seals.py` forges each file and asserts the refusal, plus the two card defences that
  already existed and are now pinned: a request edited after the card was drawn cannot be approved
  (`approval_digest_mismatch`), and a card that disagrees with the request is refused (`card_changed`).

## 0.7.0 — a budget the agent can actually spend

- **The hand-over.** `sandbox unattended --sandbox-id sbx_…` (or **Hand it over** in the pane) makes
  one budget spendable by the agent *without a per-payment PIN*, to any address, until the money or
  the clock runs out. The operator pays for it once with the passphrase **and** the PIN; the agent
  cannot arm a budget, cannot arm a proposal, and disarming is free. Revoking a budget clears the arm
  with it, expiry ends it, and a narrowed `--pay-to` list still escalates anything outside it.
- **Asking for more is a request, not a way up.** `wallet_request_sandbox(sandbox_id=…)` files a
  pending funding request for an existing budget; the ceiling does not move until the operator
  approves it with the PIN, and a redirected funding transfer cannot raise a budget it was not paid to
  (the approval is bound to the request digest, and the funding path re-checks the sandbox address).
- **Top-ups.** A first funding must be the whole budget; a later one may be any amount and only
  raises the ceiling — it never counts as spending.
- The model-facing section now states the rule honestly: money leaving the wallet needs the human's
  PIN, and inside a budget they handed over the agent spends on its own and asks for more when it runs
  out. `docs/GOALS.md` gains **The hand-over**; `README.md` and the quickstart say what the PIN does
  and does not cover.

## 0.6.0 — it runs on Windows, and the limits hold when a human says yes

A read-through of the whole tree, on a Windows machine with no WSL. The first finding was that the
plugin could not be imported there at all; the rest came from reading the payment path line by line.

**Platform**

- **Native Windows.** `fcntl`, Unix sockets, `/proc` and directory `fsync` were used unconditionally,
  so `import` failed before a tool was registered. `walletd/compat.py` now holds every platform
  difference: where there are no Unix sockets the daemon listens on a loopback port of its own choosing
  and writes `tcp://127.0.0.1:<port>` into `walletd.sock`, the daemon lock uses `msvcrt`, and
  `wallet down` identifies the daemon without `/proc`. The capability tokens authenticate both
  transports; on loopback a peer whose first line is not this protocol is dropped.
- **The Monero signer is refused up front where it cannot run.** The pinned release is the Linux
  x86-64 binary; on Windows, macOS and ARM the installer used to download 120 MB and then fail at
  start. The simulator, Bitcoin and the EVM chains sign in-process and work everywhere.

**Limits that were not limits**

- **A new destination skipped the budget.** A sandbox payment to an address nobody had blessed was
  escalated to the human *before* the budget, expiry, one-time and coin checks ran, and none of them
  ran again at approval. They now run first, and again when the PIN is typed — so five requests that
  each fit an untouched budget no longer all fit it, and a revoked or expired budget cannot be spent
  by a card still on screen.
- **The daily cap was per chain.** USDC on Base and USDC on Polygon each got a full day's cap. It is
  now summed per coin across every chain of the same class (real, testnet, simulator).
- **The payee allowlist never matched a case-sensitive address.** The list was lower-cased and
  compared with a Monero/XRP/legacy-Bitcoin payee that keeps its case, so every allowlisted payment
  was refused.
- **`NaN` and `Infinity` were amounts.** One crashed the request path; the other could be an
  autopay's cap.
- **An agent's contact could be promoted to a blessed one** by re-saving it unchanged on the operator
  token, with no passphrase.
- **`--one-time` was dropped on Bitcoin/EVM budgets**, which then took every payment that fitted. And
  a budget the *agent* proposed on those chains could never be switched on: there is no funding
  transfer to approve. `hermes wallet sandbox arm` (and the pane's **Arm this budget**) is the
  operator's yes, and costs the passphrase.

**Autopays**

- `unattended` and `revoked_at` were written to disk and not read back: after a restart a revoked
  autopay was merely "paused" (and resumable) and an unattended one asked for the PIN again.
- Revoked is now terminal on every path; ids are random rather than one per second (two proposals in
  the same second overwrote each other); the usage the pane shows is the usage policy enforces.

**Chains and quotes**

- **Every on-chain Base transfer would have been rejected**: the tip had a 1 gwei floor, which is
  above the fee ceiling at Base's gas prices. The node's own tip suggestion is used, clamped.
- **A quote for Ethereum mainnet or Arbitrum was signed for Base.** Those networks are refused by
  name. Only the `exact` scheme is priced, the authorisation echoes the seller's own network id
  (`eip155:8453`), and a malformed `extra` block no longer raises out of the parser.
- **Bitcoin addresses**: every taproot and P2WSH address was refused (program length compared in
  5-bit groups against limits in bytes), and legacy/script addresses were sent to the bech32 check.
- A Bitcoin rebroadcast that the second endpoint already knows is a success, not a failed payment.
- The SSRF guard also refuses carrier-grade NAT and IPv4-mapped loopback.

**The agent's tools**

- The payment guard blocked Hermes's own `send_message`, `send_dm`, `send_voice`… and `show_tip`.
- `wallet_pay_url` never passed the `amount` its schema documents; `wallet_request_payment` with a
  chain and no coin forced XMR onto that chain.
- `10 euros` did not parse (the code was cut out of the word); `€10` and `£7.50` now do.

**Plumbing**

- Requests, idempotency keys, autopays, deposits, contacts and sandboxes are written
  temp-file-then-rename with an fsync. They were truncated in place, and an unparseable file reads as
  "nothing here".
- Paid orders were starved by four unpaid ones ahead of them, and a dust account added a signed
  receipt to the chain every five minutes for ever.
- `walletd verify` crashed on a deleted ledger; the pane's "Reap expired now" failed as soon as there
  was something to reap; `wallet down` could stop another profile's daemon; `--passphrase-stdin` was
  unusable without a TTY; `scripts/secret_scan.py` printed OK after scanning zero files outside a git
  checkout; one unit test downloaded the signer.

Not changed: the Monero signer still runs as the agent's uid (see `docs/THREAT-MODEL.md`), XRP is
still receive-only, and a real-funds broadcast is still unproven — the EVM fee fix in particular has
been exercised against a stub, not a node. `hermes plugins validate` also still reports one caution,
`proc_access`: the daemon reads a process's argv to be sure the pid it stops is its own. Read-only, and
the alternative is stopping another profile's wallet.

## 0.5.1 — what three red-teamers actually got through

A same-uid attacker, an attacker on the agent/HTTP surfaces, and a cryptographer went at this tree
with permissions to break it. Two things were real:

- **A forged destination blessing.** A contact saved *as the operator* is what lets a sandbox spend
  without asking, and creating one cost nothing — so an agent holding the operator token (a 0600 file
  on the same uid) could mint a blessing to its own address and a funded sandbox auto-paid it. Creating
  an operator contact now needs the passphrase, and an unknown scope fails closed to `agent`.
- **The guard was seven hardcoded tool names.** `xmr_send`, a new plugin's `tip`, an MCP tool — all
  allowed. It now matches the intent in the name, plus shell-class tools whose arguments drive the
  wallet CLI or the signer's RPC. Still advisory; the boundary is the daemon.

Also fixed: the standalone `walletd verify` ignored the truncation anchor (a deleted tail verified as
`ok`), and the PIN counter and anchor were written non-atomically, so a crash during a lockout save
could read back as `fails: 0`.

Confirmed and *not* fixed (it needs root, not code): the Monero signer runs as the agent's uid, so a
shell reads `wallet.env`, POSTs `query_key` and gets the seed — and a direct signer transfer leaves no
receipt. `docs/THREAT-MODEL.md` says so in those words.

What the red team could **not** break: the PIN gate and its lockout (12 and 200 concurrent guesses
counted exactly), the operator token alone (needs the PIN *and* the passphrase), the keystore
(scrypt n=2^15, fresh XChaCha20 nonce per seal, no key reuse), the Ed25519 chain (forge, reorder, splice
all detected), EIP-3009 authorisation replay across chains or tokens, and the Decimal money path.


## 0.5.0 — the review round: money that reconciles, and a demo that cannot touch your wallet

Six independent reviewers went through this tree (product, security, first-run, tool surface,
accounting, claims). What they reproduced, closed:

- **A pooled sandbox on an account chain stranded real money.** `mode="pooled"` was accepted on
  Monero, created a real sub-account with a funding request, and every sweep was a no-op because the
  sweep keyed on the mode string; destroy then refused to close a sandbox that still held funds.
  Pooled is now refused where the chain can hold an account, and the sweep keys on `account_index < 0`.
- **`_from_pay_url` was read out of RPC params**, so an agent could attach an x402 challenge of its own
  and have a signed authorisation POSTed to its host, with the response returned. It is a Python kwarg
  only `pay_url` can pass — the RPC cannot reach it.
- **x402 settlements inside a sandbox/autopay were never delivered**: only `approve` submitted. The
  submit is now one helper both paths call.
- **`unattended` was a dead field.** The one way past the PIN is now real: `hermes wallet mandates
  unattended --mandate-id …` costs the passphrase *and* the PIN, is receipted, is refused in
  `mandates_create` so nothing can smuggle it, and is cleared by revoke.
- **Reports lied about money.** `wallet spend` ignored fees (5 payments: reported 0.0055, actually
  left 0.00565); the CSV had no fee column; both totalled a chain they never verified (a forged line
  reported 9.0035 XMR while `verify` said `bad signature at seq 9`). Fees are now in the totals, the
  CSV and the pane; a chain that does not verify refuses to be totalled; verification works while
  locked, from the public key.
- **`wallet backup` left `deposits.json` out**, so a restore came back with the deposits gone, the sim
  balance reseeded (fees reappearing as money) and nothing knowing which accounts held what.
- **One order merge wrote two receipts**, so anything summing the ledger counted the transfer twice.
- **`hermes wallet demo` bootstrapped the real home** with the published PIN `123456` and called it
  throwaway. It runs on its own scratch home now (`--home` to override).
- **Paused autopays reported `revoked`**; revoked is now its own terminal state.
- **The first-run text was a dead end**: `no_keystore` named a python module instead of a command,
  "not enough money" carried no balance and no way to fix it, a chain refusal told you to add a coin
  that was already on, and a successful approval printed 40 lines of JSON instead of `sent 0.001 XMR`.
- **The per-turn brief never fired for a waiting payment** unless the user's word looked like money;
  it now also fires when a wallet call recently showed something pending.
- Docs: tool count (27), test count, routes (52), the scan verdict, the sandbox and PIN story,
  subaddress vs account, and the `--force` claim are all corrected. The catalog entry is re-pinned to
  a commit where those numbers are true.
- **`scripts/spec_check.py`**: the two sentences this product exists for, exercised through the same
  27 tools a model calls — a contact saved by name, a "10$" request the *wallet* converts to a coin
  amount with a fee on the card, nothing sent until the PIN, an HTTP 402 seller priced by the page and
  refused with the command that enables its coin, a budget that stays unfunded until a human funds it,
  and receipts that verify. 21 checks.
- `wallet_request_payment` accepts a call without `kind`/`asset` (the schema always said optional; the
  handler raised a `TypeError`), and `hermes wallet coins --enable` no longer crashes on a missing
  `--json` attribute — both found by writing that script.
- `docs/PUBLISHING.md`: the checklist a release has to pass on the *public* clone, plus the limits to
  state in review rather than let someone discover.

## 0.4.2 — unreleased polish

- A token row's deposit QR is a real EIP-681 transfer (USDC contract + base units), not a native send.
- Pane hierarchy: 1px section borders, quieter labels, stacked card facts, the amount as the hero.
- `feeText()` never rounds a real fee to "0"; the list and the card read the same fee field; an order
  with no status shows `—` instead of "waiting".
- Install instructions match a current Hermes (`--force --yes-deps`, `plugins enable`).

## 0.4.1 — "buy this": pay an invoice, not a directory of shops

- `walletd/invoice.py`: reads a checkout page (SSRF-guarded, no cross-host redirects, 512 KB) and finds
  the payment target it states — `monero:`/`bitcoin:`/`ripple:`/`ethereum:` URIs or a bare address of a
  known shape. No target → `no_target`; two addresses of one coin → `ambiguous_target`, the human picks.
- `pay_url` falls back to that path when the URL is not a 402 endpoint, so "buy this" no longer needs a
  vendor list — nor a `wallet_pay_vendor` tool that would have to know every shop.
- `SpendRequest.provenance` (`url`, `host`, `title`, `method`, `extracted_at`) travels from the page to
  the card and the receipt: the human sees *where* the address came from.
- `request_payment` accepts the same provenance via `source_url`.

## 0.4.0 — fiat amounts, and the card the PIN authorises

- **"10$" works.** `walletd/fx.py` quotes the coin (cached, deviation-checked, refuses rather than
  guesses), `request_tx` stores the coin amount plus the fiat pair, and the card shows both.
- **The card shows the network fee** (`fee_estimate` on the request) — the human was typing the PIN on
  a card that hid one of the five facts the spec requires.
- **A contact saved in the pane is now a human's contact** (`_op` instead of `_ag`): the GUI and the
  CLI no longer disagree about whether a saved name is trusted.
- `approve --pin-stdin` reads the PIN first, then the passphrase, matching `pin set` (the two commands
  used to read the same pipe in opposite orders).
- The typed-amount field appears only when policy demands it: the PIN is already bound to the amount.
- Approving a sandbox funding transfer says so on the prompt card, not only in the list.

## 0.3.x — hardening, coins and the sandbox rules (released)

- **CLI**: `hermes wallet coin <ASSET>` (and `balances <ASSET>`) print the coin's logo — braille pixels
  rasterised from the CC0 `spothq/cryptocurrency-icons` set — next to its balance, state, network,
  address and caps; the balance table leads each row with a coloured
  emblem and `status` shows the coin list with emblems plus the active coin preference. Art is
  terminal-only — `--json`, pipes, `NO_COLOR`, `WALLET_NO_ART=1` and narrow terminals get plain text.

- **Prefer Monero** (default on): an agent-filed payment in another coin must carry a `why`, which
  travels into the receipt. Operator approvals and sweeps are exempt. Toggle in Settings, or
  `hermes wallet config set --json '{"prefer_monero": false}'`.
- **Sandbox destination allowlist**: `sandbox create --pay-to <address>` (repeatable), `allowed_payees`
  in the API/tool, and a field in the pane's create form. Without it a sandbox auto-pays
  human-saved contacts only; every other destination needs an approval. Contacts now record who
  wrote them, and only a human's entry counts as permission.

## 0.3.0 — submission release

The release that makes the wallet installable from the catalog and honest about what it can do.

### Added

- **XRP**: derivation, address validation and balances, plus reveal/copy of receiving addresses in
  the pane. Receive-only by design — this release does not sign XRP payments.
- **Coins, not chains, as the mental model**: the wallet ships with XMR, BTC and XRP; a `coins ▾`
  menu in the pane (and `hermes wallet coins --enable/--disable`) adds or removes a coin, its chain,
  its address and its balance row in one step. Removing a coin cancels requests that were waiting on
  it and cannot strand a funded sandbox.
- **One wallet home, one resolver** (`walletd/home.py`): CLI, daemon, unit file, dashboard and
  profile list all resolve `<HERMES_HOME>/wallet` the same way, with `AGENT_WALLET_HOME` as an
  override and XDG as a last resort. A second keystore elsewhere is reported, not silently ignored.
- **HTTP-402 purchases end to end**: a seller's demand is read off the wire, hashed into the request,
  and settled with an EIP-3009 authorisation when funds exist (`tests/test_pay402.py` covers
  quote → approve → `X-PAYMENT` → 200 offline).
- **A CLI that reads like a wallet**: `status`, `balances`, `coins`, `requests`, `approve`, `verify`,
  `ledger`, `receive`, `incoming`, `spend`, `proof` print decoded fields rather than JSON dumps;
  amounts sit beside their coin, and simulated spend is called out
  (`0 XMR (+0.33 XMR simulated, not real money)`).
- **Autopay is inspectable**: mandates carry `created_at` and `activated_at`, and their status is
  `proposed` / `active` / `expired` / `revoked`; per-period and lifetime caps apply only to that
  mandate's own spending, and every receipt it authorises carries its `mandate_id`.
- **Agent tools for the whole flow (25 in total)**: `wallet_incoming` was joined by `wallet_orders`
  (order id, `opened_at`, `age_seconds`, `closed_at`, `waiting`/`partial`/`paid`/`closed`, counts),
  `wallet_close_order` (receipted bookkeeping; no money moves), `wallet_request_sandbox` (the agent
  asks for a budget; the sandbox is created unfunded and the funding stays a pending request), plus
  `wallet_cancel` and `wallet_spend`. `wallet_spend` reports `real_totals` and `simulated_totals`
  separately, so play money is never added to money.
- **Incoming orders have ids.** `receive` opens an `ord_…` order, `incoming` reports each order's age
  and received-vs-expected status, and closing one writes an `order/closed` receipt.
- **Sandbox lifecycle is visible**: status `active` / `expired` / `unfunded` / `revoked` /
  `destroyed`, with `revoked_at`, `was_destroyed_with`, `destroy_in`, and the sub-account's real
  `live_balance` (`live_balance_state` = live / locked / unavailable / unsupported, plus
  `live_balance_error`). One-time sandboxes take `self_destruct_seconds` in 60..86400.
- **The simulator keeps real balances.** `sim-mainnet` seeds account 0 with 1 XMR of play money on
  first use and writes `<wallet home>/sim-balances.json`; funding, spending, sweeping and teardown
  move it exactly as a chain would, while every receipt stays marked simulated.
- **`hermes wallet demo --with-402`**: enables USDC on Base on demand, starts the bundled x402 seller
  (which quotes `base-mainnet`), and files the quote as a pending request — the whole demo, one
  command, no funds.
- **Refusals are scriptable**: daemon refusals print `error: <code>: <message>` on stderr and exit
  `2`; `deny` works from a request id alone, and a disabled-chain refusal names the
  `hermes wallet coins --enable <ASSET>` fix.
- **Payloads sized for a model**: `wallet_status` is a compact (~750 B) form unless `full=True`, and
  `wallet_balances` shortens addresses unless `full=True`.
- **Three other agent tools from the first cut**: `wallet_incoming` (each labelled subaddress:
  expected, received, waiting/partial/paid), `wallet_cancel` (withdraw your own pending request,
  receipted) and `wallet_spend` (totals per asset and payee over `30d`/`12h`/ISO windows, read from
  the same approved receipts the caps use).
- **CI** (`.github/workflows/ci.yml`): Python 3.12 and 3.13, the offline unittest suite, `node
  --check` plus the render harness, and `hermes plugins validate` where Hermes is installed.

### Security and hardening

- Two external review rounds (17 and 12 findings) closed, including an EVM address-derivation bug
  that would have sent funds to the wrong address, and a signed SSRF: an agent-supplied URL could
  reach loopback, the dashboard or cloud metadata. 402 quoting now refuses every non-public address
  unless the operator explicitly allows loopback for the bundled demo seller.
- **One choke point for the human gate**: every send path passes the passphrase check; a locked
  wallet denies and records instead of queueing forever.
- **Archiving history no longer resets spend caps**; a cap is re-checked at approval time, not only
  at request time, so a queue of pendings cannot multiply a daily limit.
- **Bitcoin**: a testnet wallet that could not pay, and a single HTTP 429 that took a chain offline —
  both fixed (fee/UTXO handling, endpoint failover).
- Monero wallet password moved out of `wallet.env` into the sealed keystore; the plaintext copy is
  stripped on next unlock.
- Sandbox profile lists survive a stale backend and are enforced against the declared profile name
  (cooperative: the OS user remains the boundary).

### Submission

- Public GitHub mirror published and pinned by 40-character SHA in the catalog entry
  (`87964b43c8cf193dc8aeccdde1b4059433174b4d`).
- `status.version` now comes from the package (`walletd.__version__`, `0.3.0`) instead of a
  hardcoded `0.2.0`, so the pane, the CLI and the daemon cannot disagree about the version.
- The catalog security scan is **not** clean: `hermes plugins validate` passes with a
  `crypto_mining` **caution** pointed at the prose in `docs/CATALOG-COMPLIANCE.md` that names the
  official Monero download host. No extraction code ships; the ask is a maintainer allowlist entry for
  that host. This replaces the earlier claim that validation reported `safe`.
- `docs/UPDATE-SAFETY.md` rewritten around the surfaces actually used; `docs/DEMO.md` is a timed,
  recordable shot list that needs no funded wallet; `scripts/demo.sh` runs `demo --with-402` plus the
  order, spend and verify steps.
- Suite: 227 offline tests; `node desktop/render-check.mjs desktop/plugin.js` prints
  `render-check: OK`.
