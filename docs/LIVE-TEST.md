# Live test runbook (real money, small)

The goal is one real payment: an agent files it, a human approves it with the passphrase, a real
transaction lands on a real chain, and the receipt chain proves it afterwards. Everything below is
the smallest version of that.

## 0. Prerequisites

- ~0.002 XMR on the wallet's main address (`hermes wallet balances` → the Monero row), or ~2000 sats
  on the Bitcoin address. The fee for a 1-input XMR transfer is around 0.00003–0.0001 XMR.
- `hermes wallet unlock` (or the pane) so the daemon has the key.
- The signer running: `hermes wallet signer status`.

## 1. Gate: is it ready?

```bash
hermes wallet ready --amount 0.001
```

`GO` means every piece lines up and the transaction it would send has already been built once
(without broadcasting). `NO_GO` prints a remedy line per blocking check. Do not skip this — it is the
only step that catches a missing signer, a dead node or a fee above the cap *before* the passphrase.

## 2. The agent asks

```bash
hermes wallet request 0.001 XMR <destination> --why "live test"
# or, in a chat: the agent calls wallet_request_payment(...)
```

What should happen: `status pending`, an `approval_id`, `confirm_amount_required: true` (0.001 is
under the 0.02 threshold, so this is `false`; use `--amount` anyway if you want the typed guard).

## 3. The human approves

```bash
hermes wallet approve <request_id> --amount 0.001 --yes   # PIN first, then the passphrase
```

or in the pane: type the PIN in the card / type the amount when it asks.

What should happen: `status sent`, a `tx_hash`, and three receipts — `pending`, `broadcasting`,
`approved`. If the chain refuses, the request becomes `failed` **with a receipt saying why** (that is
new; before, a refusal left `approved` and no receipt).

## 4. Prove it

```bash
hermes wallet verify                 # receipt chain still verifies
hermes wallet ledger --limit 6       # the three entries above, with the tx hash
hermes wallet balances               # balance dropped by amount + fee
hermes wallet proof <txid> <tx_key> <your address>    # only if the payer gave you a tx key
```

Independent check: paste the txid into `https://xmrchain.net/tx/<txid>` (or any explorer) — the
amount and destination should match what you approved.

## 5. If it goes wrong

| Symptom | What it means | Do |
|---|---|---|
| `ready` says `FAIL enough money` | nothing arrived yet, or on the wrong chain | wait, or check the address in `balances` |
| `passphrase_required` on approve | the gate is on and the passphrase was wrong/missing | re-approve with the passphrase |
| `fee_too_high` | network fee above `fee_cap` | wait for a quieter chain, or raise `fee_cap` deliberately |
| `locked` | auto-lock fired mid-flow | `hermes wallet unlock`, then approve again (the request is still `pending`) |
| request `failed` | the chain refused; nothing was sent | read the receipt's `code`/`reason`, re-file |
| `broadcast_failed` | the broadcast call errored after signing | check the ledger; if a `tx_hash` exists it is on-chain — do not re-send |

## 6. Afterwards

```bash
hermes wallet backup --out ~/wallet-after-live-test.json   # now includes monero/*.keys
hermes wallet spend --since 1d                             # the real number, split from simulated
```

The fee actually paid is the number that should replace the shipped `fee_cap` guess:
`hermes wallet ledger --limit 3` shows it on the approved entry.
