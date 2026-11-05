# Sandboxed spending — the design, and why it is the security story

An agent that can pay is an agent that can be prompt-injected into paying. The wallet's answer is
not "ask a human every time" (that makes the agent useless) and not "give it the keys" (that makes
the wallet pointless). It is this: **the agent spends from a budgeted sub-account that a human funded
once, and the money physically left the main account before the agent ever touched it.**

## The model

```
main wallet  ── one approved transfer ──▶  sandbox account  ── agent spends ──▶  payee
(keys, all funds)                          (own address, own balance,     (no approval inside
                                            own cap, own expiry)           the cap)
```

1. **The agent asks for a budget.** `wallet sandbox create --budget 0.05 --hours 24 --label "api"`
   → walletd creates a real sub-account (a new Monero account, i.e. its own address) and files a
   **funding transfer** for the operator.
2. **A human approves that one transfer.** The passphrase, the policy engine, the receipt — the same
   gate as any other payment. This is the only moment a human is involved.
3. **The agent spends inside the sandbox without asking.** `request_tx` with `sandbox_id` is allowed
   when `amount + fee_reserve ≤ remaining`, the profile is allowed, the chain is enabled, and the
   sandbox has not expired.
4. **Revoking returns the rest.** `Revoke & withdraw` sweeps `remaining - fee` home through the
   normal operator path; nothing is stranded.

## Why it is safe

- **The money is really gone from the main account.** A sandbox is not a number in a database that a
  bug could inflate; it is a sub-account with its own balance on-chain. The worst a hijacked agent
  can do inside one is spend what the operator put there.
- **The cap is enforced where the keys are.** The decision is made in `walletd`, next to the signing
  key, not in the plugin the agent talks to. A compromised Hermes process cannot talk its way past it.
- **No key ever moves.** The agent holds no key at any point; there is no seed to exfiltrate, no
  "unlock the wallet for me" step it can trigger.
- **Every move is receipted.** Funding, each spend, the sweep and the revocation are entries in the
  same Ed25519 hash chain, anchored so truncation is detected.
- **It expires.** A sandbox has a deadline and a lifetime ceiling, so a forgotten budget cannot become
  a permanent allowance.
- **It can be scoped to Hermes profiles.** `allowed_profiles` means a sandbox opened for the
  `research` profile cannot be spent from `default`.

## Fees are part of the budget, not an afterthought

A spend from the sandbox pays its own network fee out of the same balance, so the last payment must
leave room for it:

- every sandbox decision requires `amount + fee_cap[asset] ≤ remaining`
- a sweep sends `remaining - fee_cap[asset]`, because a transfer of the *entire* balance cannot be
  signed — there is nothing left to pay the fee with
- the funding transfer's fee is paid from the main account (it is a normal transfer), and the
  sandbox receives exactly the budget

## Per-chain reality

| Chain | Sub-account | Status |
|---|---|---|
| Monero | native: each account is a real subaddress, keys stay in the signer | **implemented** (fund, spend, ACL, sweep, fee reserve) |
| Bitcoin | no accounts, but a derived child key gives a distinct address; UTXOs are tracked per address | **not implemented** — same design, needs derivation index in the keystore |
| EVM (Base, Polygon) | no native sub-accounts. Options: a **separate key** (simplest, needs a little ETH/POL for gas), **EIP-3009 authorizations** (gasless, per-payment, no new key), or an **ERC-4337 smart account with a session key** (strongest: on-chain policy, revocable) | **not implemented** |

The EVM case is where "fast finality" matters: funding a sandbox should wait for the funding
transfer to confirm before the agent is given the budget, otherwise a reorg could leave the sandbox
short. Spending speed itself is unaffected.

## What this is not

- Not a way to hide spending from the operator: every sandbox and every receipt is visible in the
  pane.
- Not custody-free delegation: the operator still decides the size and lifetime of each budget.
- Not a defence against a compromised *host while unlocked* — that boundary is the OS user, and it is
  stated in `docs/THREAT-MODEL.md`.

## The pitch, in one sentence

> The agent never holds a key, a human funds a capped sub-account once, and everything the agent does
> inside it is a signed receipt — so a prompt injection can spend a budget, not a wallet.

## One-time sandboxes destroy themselves

A one-time sandbox exists for **one payment by the agent** and then takes itself apart. It is the
safest shape for an agent that is buying something once: after the payment there is no standing
budget left to misuse, and no sub-account for anyone to forget about.

    operator: create one_time sandbox (budget, teardown window)
    operator: approve one funding transfer        -> sandbox armed
    agent:    one payment, no approval            -> clock starts
    ... window elapses (default 30 min) ...
    walletd:  read live balance -> sweep remainder home -> read again -> destroy

Rules, in order of importance:

1. **No destruction while money is in it.** The balance is read from the chain, the remainder is
   swept home, and the balance is read *again*. Only when nothing spendable is left does the sandbox
   record die. If the node is down, the sweep is deferred, or the sweep has not settled yet, the
   destroy is *blocked* and retried on the next tick: the sandbox keeps a `destroy_blocked` reason
   and the pane shows "holding off: <reason> — the money is still in the sandbox".
2. **The clock starts at settlement, not at creation.** A slow funding approval must not eat the
   window, and a denied or failed payment never starts it.
3. **Only one-time sandboxes do this.** Budget sandboxes have no clock at all; they end by expiry or
   by an operator revoke, and a revoke sweeps first for the same reason.
4. **Locked wallet means no reaping.** A sweep needs the signer, so the reaper does nothing while
   the wallet is locked - waiting is always cheaper than stranding funds.
5. **Funded but never used?** When a one-time sandbox passes its lifetime with zero spends, the
   reaper sweeps it home and destroys it (`lifetime_elapsed`), so money cannot sit in an abandoned
   sub-account.
6. **Sweeps are exempt from the one-payment rule.** The transfer that empties the sandbox is not a
   second payment; blocking it would make a safe teardown impossible.

Everything is receipted: `sandbox/self_destruct_armed`, `sandbox/swept`, `sandbox/destroyed` with
`left`, `swept`, `reason` and a `keys` line describing exactly what stopped existing.

**On unrecoverable funds.** Destroying key material is irreversible, so the wallet only destroys
after the balance is provably zero. Monero sub-accounts are derived from the main wallet's keys, so
"destroy" here means the sandbox is closed and forgotten - any dust left behind is still yours, in
an account you can recover from the wallet. If a later adapter gives a sandbox its own key (EVM
session keys), the same order holds: money out, then keys gone. `hermes wallet sandbox destroy`
takes the identical path, and `--force` only skips the "is it one-time" check, never the fund
checks.
