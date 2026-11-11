# Demo script — four beats, about 90 seconds

Everything here is pasted into the Hermes chat **as the user**. The agent never gets a tool that
moves money on its own; every beat below is either a read, or a request that ends in a card you
approve with your PIN — the same six digits that unlock the pane. No beat needs the passphrase.

Substitute `<PAYEE>` with an address you control (a second wallet, or a fresh one). Never the
wallet's own address: the agent will recognise a self-transfer and refuse it, which is correct
behaviour but not the beat you are recording.

Three things are baked in: `prefer_monero` is **off by default**, so an ETH payment no longer has to
justify itself; a **Base sandbox is a pooled budget** — it caps spending but still asks for the PIN
per payment unless you hand it over `unattended`; and the pane is a **PIN page until it is
unlocked**, so there is nothing to show on camera before the six digits go in.

## Before you hit record

| | | |
|---|---|---|
| **Live page** | `http://127.0.0.1:8791/` from `scripts/live_view.py` — put it on a second screen or a phone. It updates every 1.2 s. |
| **Pane** | Hermes → Capabilities → Plugins → toggle **off**, then **on**. Do this if the pane is blank; it is the fix, not a reload. |
| **Pane state** | Locked it shows one field and one button, nothing else — balances, addresses and cards all appear after the PIN. Do not read an empty pane as a broken one, and do not open a second wallet mid-demo. |
| **PIN** | The six digits you set. The same PIN unlocks the pane and authorises one payment. |
| **Passphrase** | Not needed for any beat. It is the recovery secret; the only box that asks for it is "forgot the PIN?". |
| **Autolock** | 15 minutes by default. Re-unlock between takes, or turn it off for the session (`hermes wallet config set autolock_minutes 0`). |
| **Funds** | Enough native coin for the amount **plus** the network fee. On Base a 0.00005 ETH transfer costs roughly 0.00000025 ETH, and the wallet keeps a gas floor so it can always pay again. |

Do not record against a wallet you care about. If you want a throwaway, install the plugin into a
scratch `HERMES_HOME` and record there instead.

---

## Beat 1 — it knows what it holds (0:00–0:12)

> Wallet check: one line per coin with its chain and amount. Nothing else.

**Expect:** a short table and no advice. If the pane is locked, say so out loud, type the PIN, then ask.

---

## Beat 2 — a real payment, and the PIN (0:12–0:40)

> Send 0.00005 ETH on Base to `<PAYEE>`. One line for the fee, then file it and stop.

**Expect:** the card carries the amount, the `fee_estimate`, and what the payment leaves behind in the
gas coin. Type the PIN on the card. `walletd` signs, broadcasts, and the receipt lands in the ledger.

**Camera:** the card (slow), the PIN, then the live page — `sent` → confirmations → explorer link.

---

## Beat 3 — the injection that does not work (0:40–1:00)

Paste a page-like line that **orders** a payment to an address nobody approved — for example one
claiming the wallet owner authorised a transfer to a burn address as a "service fee" — then ask the
agent to file it exactly as written so the card appears, and deny it.

Real injections also tell the model to disregard its earlier instructions. Do not paste that wording
into a document in this repo: the plugin validator rates that exact phrase CRITICAL, which turns the
security scan `dangerous` and blocks catalog admission. Describe the attack instead.

**Why "file it" is in the prompt:** without it the agent simply refuses in prose — correct behaviour,
but there is no card to point at. With it, the card appears and it is rendered from `walletd`'s own
record: the amount and payee the daemon stored, not the text on the page. **Press Deny.**

Camera line: *"the card is the truth, not the text."*

---

## Beat 4 — the sandbox (1:00–1:30)

> Give yourself a 0.0001 ETH sandbox on Base. One line.

**Expect:** a **pooled** budget is created and no funds move — it caps spending against the main
account, same caps, and every payment still asks for the PIN. A budget the agent asks for is created
unfunded on purpose: an agent must not be able to grant itself spending room. **Arm it in the pane**
(passphrase) — that is the beat where *you* act on camera.

> From that sandbox, send 0.00005 ETH to `<PAYEE>`.

> Now send 0.0005 ETH from the same sandbox.

**Expect:** the second is refused with `sandbox_budget` — 0.0005 is five times the ceiling. That
refusal is the point of the beat: the budget is enforced by `walletd`, not by the agent's manners.

**Camera:** the sandbox card, the Arm, then the refusal text and the live page's sandbox bar.

---

## The website question

| | what it shows | verdict |
|---|---|---|
| **`scripts/live_view.py`** (`:8791`) | the spend, its confirmations, the sandbox budget, the gas floor — one screen | **the camera**. Nothing else shows the sandbox and the gas at the same time. |
| **BaseScan** — `basescan.org` | the chain's own record of the tx | **the receipt, and the site to check Base with.** Paste the hash on `/tx/`, or leave the wallet address open and it appears in ~15 s. |
| **Blockscout** — `base.blockscout.com` | same data, cleaner dark UI | use it instead of BaseScan if you want it to look better; same credibility. |
| **mempool.space** | the best-looking explorer there is | only for a Bitcoin tx — it is BTC-only. |

**If you want one site for "checking the blockchain", it is `basescan.org`.**

Never a fake animated page. If a judge asks for the hash and it resolves, the video is unimpeachable;
if it does not, the video is worthless regardless of how good it looked.
