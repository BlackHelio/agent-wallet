# The 85-second cut — record this one

**Use a real seller, not the bundled one.** Verified live on 2026-11-10: `GET
https://x402.tbs-digital.com/weather/current` answers a genuine HTTP 402 quoting **0.001 USDC on Base**
(`eip155:8453`, USDC contract `0x8335…2913`, payee `0xd6fCE15298Fc9D60f867d9455D497177Da9BFB0a`). The
wallet prices it in one call, files a card with `verified` provenance, and after payment the seller
returns the weather JSON — a real internet purchase for a tenth of a cent, no account, no signup.
BlockRun (`POST https://blockrun.ai/api/v1/chat/completions`) is the same shape at $0.006 if you would
rather show an LLM call than a forecast (`pay-url` is a GET, so the weather endpoint is the one to use).

One task, start to finish, with the card as the star. No feature tour: five beats, each ~15 s, cut
between takes rather than performed live. The whole thing is about one sentence a viewer should be able
to repeat afterwards: **the agent can ask, the card decides, and every decision is signed.**

Prep (not recorded, once): `python3 scripts/demo_stage.py` prints a recording sheet and stages exactly
this state — a scratch wallet, `adrian` in contacts, USDC enabled, one old receipt, the live seller's
quote already filed. It needs a scratch wallet with `adrian` saved as a contact, **USDC enabled**, **~1
USDC on Base funded** (the whole take spends about 1.5 cents), the pane open, terminal at 16 pt, one old
receipt in the list so the ledger is not empty. Run `hermes wallet demo` twice off camera: the cold run
derives addresses for four chains and leaves ten seconds of dead air.

| Time | On screen | Say (≈ words that fit the time) |
|---|---|---|
| 0:00–0:10 | Chat: `send adrian 10 dollars` → the agent answers with the price it got: `$10 ≈ 0.0167 XMR, fee 0.00003` | "An agent that can pay. Watch what it actually gets to do." |
| 0:10–0:28 | The card, full-screen: **full address**, amount, fee, fiat, `new payee`, and the line *"agent message — the agent wrote this, not the wallet"*. Type the PIN → `sent` → receipt appears | "It can file a payment. It cannot approve one. The card shows the real destination and exactly what leaves — and the agent's own words are labelled as its words." |
| 0:28–0:42 | Paste a hostile line into chat, as if a web page had written it: an order to send a payment to an address you have never used. The agent files it. The card shows that address, flagged as a new payee. Click **Deny** | "Prompt injection still gets a card — somebody else's address, flagged as a new payee. Deny, and the refusal is a receipt." |
| 0:42–0:58 | Pane: **Sandboxes** → the budget, `Hand it over` (passphrase + PIN, once) → chat: `buy the two parts from the shops in the notes` → two payments leave with **no PIN**, `authorised_by sandbox`, the budget's remaining ticking down → chat: `I need 0.01 more` → a card with a PIN pad | "This is the part that makes it an agent. A budget is money that already left my wallet: inside it it pays whoever it needs, no PIN, until it runs out. The moment it wants more, it's my key again." |
| 0:58–1:10 | `hermes wallet pay-url https://x402.tbs-digital.com/weather/current --why "one weather call"` → card: `0.001 USDC on Base`, the seller's own payee, `provenance verified` → PIN → the signed authorisation goes back and the forecast appears | "It can buy from a machine that prices itself — a real HTTP 402 seller on Base. The price is the seller's, not the model's, and it costs a tenth of a cent." |
| 1:10–1:19 | `wallet verify` → `ok`; `wallet spend` → `fees 0.00006 · verified true`; tamper one line → `bad signature at seq 9` | "Everything above is in a signed, hash-chained ledger — including the refusals. Break one line and it says which." |
| 1:19–1:25 | End card: `522 tests · 21 spec checks · keys never enter the agent process` | "The keys never enter the agent's process. Every promise here is a test in the repo." |

Rules for the take: never show a number you cannot reproduce; when something is the simulator, say
"simulator" (the receipts are stamped). Cut on action, no transitions. Two attempts per beat maximum —
the honest fallback for a failed beat is a sentence, not a different feature.

## If you want the sandbox in the same take (adds ~25 s)

The sandbox is the second half of the argument — *delegation with a wall* — and `scripts/demo_beats.py`
rehearses it end to end on a scratch wallet (send · injection-denied · sandbox · ledger):

```
sandbox   sbx_… 0.02 XMR budget          # real money moved into its own account
funding   req_… pending → approve        # a human moves it, with the PIN
live      0.02 XMR in the budget         # the agent can spend this, and only this
card      0.002 XMR → a blessed address  # policy says yes, the PIN still asks
verdict   pin/needs_pin
3b        same budget, an address nobody blessed → escalates to a human card
3c        revoke → sweeps 0.01794 home, fee 0.00003, receipt written
```

Say: *"A budget is money I moved out of the wallet. It can spend that and nothing else — a different
address gets a card, and giving the rest back is one click."* Then cut to the ledger.

What not to record: the coin menu, the QR codes, backup/restore, the ASCII banner. They are good, and
they are not the argument.

# The 90-second demo (the long cut, if you have time)

Ten shots, 1:29 on the clock. Everything runs with **no real money and no funded wallet**:
`sim-mainnet` builds real plans through the real policy engine, broadcasts nothing, and keeps its own
play balances (1 XMR seeded on first use, moved by funding, spending, sweeping and teardown) — so the
refusals and the budget arithmetic are the feature. Every shot has a fallback line, so a failed
command never kills the take.

## Prep (not recorded, run once)

```bash
hermes wallet demo --with-402            # installs the pinned signer, creates the keystore + sim
                                         # wallet, then prices the bundled x402 seller
hermes wallet status                     # confirm: unlocked or locked, 3 coins, signer ready
```

`--with-402` starts the bundled seller itself and files its quote as a pending request, so there is
no second terminal to babysit. If you want to run the seller by hand, start
`python3 scripts/pay402_demo_server.py 38091 &` and enable the coin it quotes first
(`hermes wallet coins --enable USDC`), then use `--allow-loopback` on `pay-url`.

Open the Desktop on **Wallet** (sidebar, credit-card icon) and put the terminal beside it at a
readable font size. `./scripts/demo.sh` replays the 402 quote and the simulated payment in one go if
you would rather not type live; the shot list below is the timed version.

## Shot list

**0:00 – 0:07 · the locked pane**
On screen: Wallet page, hero card `locked`, three asset rows (XMR, BTC, XRP).
Say: *"This is an agent wallet. The agent can spend — it never holds a key."*
If it fails (pane blank / `failed to load`), say: *"The pane is one ESM file on the documented SDK;
the terminal is the same product."* and cut to the terminal.

**0:07 – 0:16 · the human gate opens it**
Do: type the six-digit PIN in the pane (the same digits that approve a payment). On screen: `unlocked`, balances load. Say once, early: *"that is the only secret I type — it opens the wallet and it signs off each payment."*
Say: *"The passphrase decrypts the keystore. Payments themselves are authorised by a six-digit PIN — the agent can read the
operator token off disk and still not approve."*
If it fails (wrong passphrase / daemon down), say: *"The wallet stays locked and every tool fails
closed; there is no degraded mode that signs."*

**0:16 – 0:25 · one command, whole flow**
```bash
hermes wallet demo
```
Say: *"Request, policy, approval, receipt, verification — on a simulator chain, so it costs
nothing."*
If it fails (signer download blocked), say: *"The demo needs the pinned signer once; the flow is the
same code path with or without it."* then run `hermes wallet status`.

**0:25 – 0:33 · policy and receipt are visible**
On screen, from the same output:
```
   policy    allow=True rule=manual_approval code=needs_operator
   status    sent  simulated=True
3. receipt   chain ok=True entries=…
```
Say: *"Policy is evaluated in the signer, and the receipt says out loud that this one was
simulated."*
If it fails (no `simulated=True`), say: *"Anything not stamped simulated came off a real chain — the
stamp is the promise."*

**0:33 – 0:45 · the 402 purchase**
```bash
hermes wallet demo --with-402
```
On screen: the seller's own banner, then the wallet's reading of its demand —
```
   seller    402 demo seller on http://127.0.0.1:43711/sku/42 quoting 25000 atomic USDC on base-mainnet
   quote     0.025 USDC on base-mainnet → 0x1234567890ab…
   filed     request req_… status=pending
   next      approve it in the Wallet pane; with no USDC funded it will refuse at the chain, not before the quote
```
Say: *"The agent never states the price or the address — both come off the wire, and a SHA256 of the
seller's terms is bound into the request. This wallet holds no USDC, so approving it stops at the
chain: nothing signed, and the refusal is recorded."*

If you would rather show the refusal explicitly, in a second terminal:
```bash
curl -i http://127.0.0.1:38091/sku/42
hermes wallet pay-url http://127.0.0.1:38091/sku/42 --allow-loopback --why "buy an ACME widget"
```
`--allow-loopback` is required for the bundled seller; without it the daemon refuses with
`bad_url: … not a public address`, which is the SSRF guard doing its job. With it, the quote is filed
exactly as above. The settlement path is driven offline by `tests/test_pay402.py`
(quote → approve → `X-PAYMENT` → 200).

**0:45 – 0:53 · the agent asks for too much**
Do: in chat, ask the agent to send 5 XMR to a vendor. On screen: `denied`, `per_tx_cap`,
`amount 5.000000000000 exceeds per-tx cap 0.5 XMR`.
Say: *"It can only ask. The cap lives in the signer, so the ask dies before a key is touched — and
the denial is a receipt."*
If it fails (agent refuses to even call the tool), say: *"The tool description tells the model the
cap; either way the signer is the thing that says no."*

**0:53 – 1:03 · no PIN, no payment**
```bash
hermes wallet requests                      # note the req_… id
hermes wallet approve req_… --amount 0.01   # press Enter at the prompt, leave it blank
```
On screen: `pin_required`, then `bad_pin (4 attempts left)` with a wrong one.
Say: *"The wallet is already unlocked, and approval still needs the PIN the human types. An agent that steals
the operator token gets this far and no further."*
If it fails (prompt not shown), say: *"Without a TTY it refuses to read a PIN at all — the gate fails
closed."* (`refusing to read a PIN without a TTY`.)

**1:03 – 1:11 · denial is evidence**
```bash
hermes wallet requests                      # note the req_… id
hermes wallet deny req_… --reason "not this vendor"
hermes wallet ledger --limit 3
```
On screen: a `denied` row with the reason, next to the approved simulated one.
Say: *"Denials are signed into the same chain as approvals — a refusal you can prove later."*
If it fails (request already expired), say: *"Requests expire after the approval window; the expired
one is still in the ledger."*

**1:11 – 1:21 · the budget story, in one line**
```bash
hermes wallet sandbox list
```
On screen: the empty list (or a sandbox row with `status`, `remaining`, `live_balance`, `destroy_in`).
Say: *"For real spending you carve out a sandbox, fund it once with a passphrase, and the agent
spends inside it without asking — it stops at the budget, at expiry, or when I revoke. The agent can
ask for one, but asking creates it unfunded: the funding transfer is a request I approve."*
If it fails (`chain_unavailable`), say: *"Sandboxes need a live chain and a funded budget; this
machine has neither, and I will not demo a payment that did not happen."*

**1:21 – 1:29 · the ledger is provable**
```bash
hermes wallet verify; echo "exit=$?"
sed -i 's/"amount":"0.001"/"amount":"9.9"/' "$HERMES_HOME/wallet/ledger.jsonl"
hermes wallet verify; echo "exit=$?"
```
On screen: `ok` / `exit=0`, then `bad signature at seq 2` / `exit=1`.
Say: *"Ed25519, hash-chained, anchored. Flip one byte and it fails — including on the denial."*
If it fails (no `ledger.jsonl` at that path), say: *"`hermes wallet doctor` prints the resolved home;
the ledger lives beside the keystore."*

Close on the hero card: *"The agent never had a key."*

## Shots added in 0.4 (insert if the take needs to show the product sentences)

**"send adrian 10$"** — with a contact saved and the network up:
`hermes wallet request 10$ XMR adrian --why "credits"` → the card reads
`0.0163 XMR ≈ 10 USD (1 XMR = 611.09 USD)` and asks for the PIN. Pre-flight the rate:
`curl -s 'https://api.coingecko.com/api/v3/simple/price?ids=monero&vs_currencies=usd'`.
If the rate is unavailable the command refuses (`fx_unavailable`) — that is the honest fallback line,
not a reason to skip the shot.

**"buy this" (invoice page)** — any checkout that states an address:
`hermes wallet pay-url https://<shop>/checkout --why "credits"`. The card shows
`address read from <host> · <title>`; a page with no address returns `no_target` and files nothing.

**Deposit QR** — the pane's balance row → *deposit QR* → the chain's own URI with an optional amount,
plus the exact text inside the code. Motion is one click; say *"receiving cannot move anything"*.

**PIN card** — `hermes wallet approve <id> --amount <n>` with a blank PIN: `pin_required`. Then the
pane popup: alias, full address, amount ≈ fiat, fee, chain, provenance, PIN pad.

## If you have 0.01 XMR to spare

The one thing simulation cannot show is a real transaction. Send 0.01 XMR to the wallet address,
then `hermes wallet requests` → `hermes wallet approve req_… --amount 0.01`, and the ledger carries
the tx hash and tx key. Do not put this in the 90-second cut: a real confirmation can take longer
than the whole video, and the honest version of this demo is the one that never needed funds.
