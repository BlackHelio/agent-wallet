---
name: send-xmr
description: Send Monero from the agent wallet with human approval.
version: 0.3.0
author: BlackHelio
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [wallet, monero, payments]
    category: wallet
---

# Send XMR Skill

How to pay someone in Monero through `agent-wallet`. The agent never holds a key; every transfer
is a request that a human approves in the Wallet pane, and the receipt carries the Monero tx key so
the recipient can prove payment.

## When to Use
- The user asks to send XMR to an address, or to pay for something priced in XMR.
- A workflow needs an XMR payment and the user has granted a mandate.

## Procedure

1. **Confirm the amount and address with the user in plain text.** Read the address back in full;
   Monero addresses are 95 characters and one wrong character loses the money.
2. **Call `wallet_request_payment`** with `kind: monero-stagenet` (or `monero-mainnet` when the
   operator enabled it), `asset: XMR`, the amount as a string, the payee, and a `why`.
3. **Read the decision.** `decision.allow: false` means policy refused; quote the `code` and
   `reason` to the user and stop — do not retry with a different amount unless they ask.
4. **If approved**, you get `request.status: approved` and a `tx_hash` once the signer broadcasts.
   Report the txid and the receipt `seq` from `wallet_ledger`.
5. **Verification.** Tell the recipient they can verify with the tx key in the receipt
   (`wallet_ledger` → `details.tx_key`) against the txid, without either side sharing a view key.

## Pitfalls

- **Never** ask the user for a seed, private key or view key. If they paste one, refuse and tell
  them to delete it — the wallet never needs it.
- A locked wallet denies with `unattended_no_operator`. Call `wallet_request_unlock`, tell the user
  to open the Wallet pane, and wait. Do not loop.
- Subaddresses: prefer a fresh subaddress per counterparty (`accounts` shows them) so one payment
  cannot be correlated with the rest of the wallet's history.
