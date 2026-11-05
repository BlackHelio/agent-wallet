# The 90-second demo

Ten shots, 1:29 on the clock. Everything runs with **no money and no funded wallet**:
`sim-mainnet` builds real plans through the real policy engine and "broadcasts" nothing, and the
refusals are the feature. Every shot has a fallback line, so a failed command never kills the take.

## Prep (not recorded, run once)

```bash
hermes wallet demo                       # installs the pinned signer, creates the keystore + sim wallet
hermes wallet status                     # confirm: unlocked or locked, 3 coins, signer ready
python3 scripts/pay402_demo_server.py 38091 &   # the seller for shot 5
```

Open the Desktop on **Wallet** (sidebar, credit-card icon) and put the terminal beside it at a
readable font size. `./scripts/demo.sh` replays shots 5–7 in one go if you would rather not type
live; the shot list below is the timed version.

## Shot list

**0:00 – 0:07 · the locked pane**
On screen: Wallet page, hero card `locked`, three asset rows (XMR, BTC, XRP).
Say: *"This is an agent wallet. The agent can spend — it never holds a key."*
If it fails (pane blank / `failed to load`), say: *"The pane is one ESM file on the documented SDK;
the terminal is the same product."* and cut to the terminal.

**0:07 – 0:16 · the human gate opens it**
Do: type the passphrase in the pane. On screen: `unlocked`, balances load.
Say: *"The passphrase decrypts the keystore and authorises every payment — the agent can read the
operator token off disk and still not approve."*
If it fails (wrong passphrase / daemon down), say: *"The wallet stays locked and every tool returns
`walletd_unavailable`; there is no degraded mode that signs."*

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
   policy    allow=True rule=manual_approval code=ok
   status    sent  simulated=True
3. receipt   chain ok=True entries=…
```
Say: *"Policy is evaluated in the signer, and the receipt says out loud that this one was
simulated."*
If it fails (no `simulated=True`), say: *"Anything not stamped simulated came off a real chain — the
stamp is the promise."*

**0:33 – 0:45 · the 402 purchase**
```bash
curl -i http://127.0.0.1:38091/sku/42
hermes wallet pay-url http://127.0.0.1:38091/sku/42 --allow-loopback --why "buy an ACME widget"
```
On screen: `402 Payment Required`, then the wallet's own reading —
```
  quoted   0.025 USDC on base-sepolia
  pay to   0x1234567890abcdef1234567890abcdef12345678
  for      ACME Widget, 1 unit
  refused  chain_not_enabled: chain base-sepolia is not enabled
```
Say: *"The agent never states the price or the address — both come off the wire, and a SHA256 of
the seller's terms is bound into the request. This wallet has that network switched off, so it
refuses: nothing signed, and the refusal is recorded."*
If it fails with `bad_url: 127.0.0.1 … not a public address`, say: *"The CLI is presenting the agent
token, and the agent may only quote public URLs — that refusal is the SSRF guard."* Then show the
same quote captured beforehand (`docs/BUILD-LOG.md` has the run) and move on. **This is the known
one-line bug: `cmd_pay_url` does not pass operator scope when `--allow-loopback` is given.** With a
funded testnet wallet the same request settles as an EIP-3009 authorisation
(`tests/test_pay402.py` drives quote → approve → `X-PAYMENT` → 200 offline).

**0:45 – 0:53 · the agent asks for too much**
Do: in chat, ask the agent to send 5 XMR to a vendor. On screen: `denied`, `per_tx_cap`,
`amount 5.000000000000 exceeds per-tx cap 0.5 XMR`.
Say: *"It can only ask. The cap lives in the signer, so the ask dies before a key is touched — and
the denial is a receipt."*
If it fails (agent refuses to even call the tool), say: *"The tool description tells the model the
cap; either way the signer is the thing that says no."*

**0:53 – 1:03 · no passphrase, no payment**
```bash
hermes wallet requests                      # note the req_… id
hermes wallet approve req_… --amount 0.01   # press Enter at the prompt, leave it blank
```
On screen: `bad_passphrase` / `passphrase does not match the keystore`.
Say: *"The wallet is already unlocked, and approval still needs the passphrase. An agent that steals
the operator token gets this far and no further."*
If it fails (prompt not shown), say: *"Without a TTY it refuses to read a passphrase at all — the gate
fails closed."* (`refusing to read a passphrase without a TTY`.)

**1:03 – 1:11 · denial is evidence**
```bash
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
On screen: the empty list (or an existing sandbox with `remaining`).
Say: *"For real spending you carve out a sandbox, fund it once with a passphrase, and the agent
spends inside it without asking — it stops at the budget, at expiry, or when I revoke."*
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

## If you have 0.01 XMR to spare

The one thing simulation cannot show is a real transaction. Send 0.01 XMR to the wallet address,
then `hermes wallet requests` → `hermes wallet approve req_… --amount 0.01`, and the ledger carries
the tx hash and tx key. Do not put this in the 90-second cut: a real confirmation can take longer
than the whole video, and the honest version of this demo is the one that never needed funds.
