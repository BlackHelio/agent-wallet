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

1. **The agent asks for a budget.** `wallet_request_sandbox(kind, asset, budget, …)` creates the
   sub-account (a new Monero account, i.e. its own address) **unfunded** and files a **funding
   transfer** as a pending request; the operator's `hermes wallet sandbox create --budget 0.05
   --hours 24 --label "api"` is the same object created directly. Asking never gives the agent money.
2. **A human approves that one transfer.** The passphrase, the policy engine, the receipt — the same
   gate as any other payment. This is the only moment a human is involved. Until it settles the row
   reads `unfunded`.
3. **The agent spends inside the sandbox only to a blessed destination, and the PIN still gates it.**
   Unless the operator has marked that autopay `unattended` (passphrase + PIN, receipted), a spend
   inside a budget is filed as `needs_pin` like every other payment. `request_tx`
   with `sandbox_id` is auto-approved only when **all** of these hold:
   `amount + fee_reserve ≤ remaining`; the payee is in the sandbox's `allowed_payees` **or** is a
   contact a *human* saved (`created_by: operator`); the sandbox is funded, unexpired and not
   revoked/destroyed; the chain is enabled and the request is within the global caps.
   Any other destination returns `needs_operator`: the payment is filed for approval instead of being
   broadcast. That is the rule the whole design exists for — the budget bounds the *amount*, the
   allowlist bounds the *destination*, and a prompt-injected agent can change only the former.
4. **Revoking returns the rest.** `Revoke & withdraw` sweeps `remaining - fee` home through the
   normal operator path. If the sweep cannot run (chain down, signer stopped) the sandbox stays
   `revoked` with the balance still visible and the reason on the row — the money is not stranded
   silently, it is stranded *visibly*, and `sandbox sweep` can retry once the chain is back.

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
- **It can be scoped to Hermes profiles — advisory, not a boundary.** `allowed_profiles` compares the
  profile name the *caller declares*, and every profile presents the same agent token, so it is a
  guard rail between cooperative profiles. The enforced boundary is the destination allowlist above
  plus the OS user.

## The record is sealed, because the file is writable

A sandbox is only as strong as the row that says what it is. `sandboxes.json` sits in the wallet home,
mode 0600, owned by the same uid that runs the agent — so a shell (or a subagent, or a tool that writes
files) can rewrite the arm, the address, the allowlist or the ceiling. That is the one attack a
confirmation card cannot stop, because it never goes near a card: the injection edits the *record* and
lets the next restart believe it.

So every authority row carries an HMAC over the fields that could spend money (the destination, the
budget, the arm, the allowlist, the caps), keyed by the master key — which lives behind the passphrase.
`unlock` verifies each row before anything can spend:

| what happened | what the daemon does |
|---|---|
| seal verifies | nothing; the row is what it says |
| seal missing or wrong | drops the authority, writes `authority_dropped`, and (sandbox) marks it `tampered` |
| a file from before the seal existed | re-seals it **without** the authority: unarmed, unblessed, stopped |
| **an old copy put back** (the seals are genuine) | closes the budget (`revoked_at`), `authority_dropped` with `stale_copy` |

That last row is why the file also carries a digest: each save appends `authority_state {what, digest}`
to the hash chain, so `unlock` can tell a file the wallet wrote from a file it wrote *earlier*. Copying
`sandboxes.json` while a budget is armed, waiting for the operator to take the arm back, and restoring
the copy is otherwise a perfect forgery — the seal is real, it is just old. The anchor is checked
before it is rewritten for the same reason: a truncated chain would erase that memory.

A `tampered` budget refuses spends, funding, arming and new allowlisted addresses. It still sweeps, and
that is deliberate: a sweep can only send money to *this wallet's* account 0, derived from the seed, so
revoking a tampered budget is how the operator gets the money back. `sandbox list` prints `TAMPERED`
where the budget would be, and the pane shows a red chip instead of the hand-over control.

The same seal covers `contacts.json` (`created_by: "operator"` is a blessing) and `mandates.json` (an
active, unattended autopay is spending authority). `tests/test_seals.py` is the proof: it forges each
file the way a shell would and asserts the refusal, including the exact shape of the worry — a record
that says $5 to X while the money would go to $100 to Y.

## Handing a budget over

A sandbox that asks for the PIN on every payment is a limit on *quantity*, not a budget the agent can
use. For an errand — "buy this from whoever sells it" — the destination is unknown until the agent
gets there, so a blessing list turns every step back into a checkout form, and the human stops reading
the cards and starts clicking Approve.

So a budget has two states:

| state | the agent may pay | costs a human |
|---|---|---|
| funded | blessed destinations only (contacts a human saved, or `--pay-to`), each payment with the PIN | a PIN per payment |
| **handed over** | **any destination**, until `budget - spent - fee` or expiry | the passphrase **and** the PIN, once |

The hand-over is the containment model doing its job. On Monero the money is physically in a separate
account, so the worst a hijacked agent can do is empty *this* budget: it cannot reach the main wallet,
it cannot raise its own ceiling, and when the money is gone the only thing it can do is ask — which is
a card in the pane with a PIN pad. On the pooled chains (Bitcoin and the rest without accounts) the
same hand-over exists, but containment there is policy, not custody; that distinction is why the pane
says which kind of budget you are looking at.

Rules that keep the hand-over from becoming a hole:

- **The agent cannot arm anything.** `sandbox_create(unattended=True)` from an agent scope is refused;
  a *proposal* carrying `unattended` is refused with `unattended_needs_operator`. Arming is
  `sandbox_set_unattended`, which verifies the passphrase and the PIN. Disarming needs neither.
- **Revoke clears it.** `sandbox_revoke` sets `unattended = False` and empties `armed_at`, and the
  ledger entry records `was_unattended`, so a revoked budget cannot be re-armed by a late approval.
- **Expiry ends it.** An expired budget refuses spends (`sandbox_expired`) and refuses to be armed.
- **A narrowed list still wins.** With `--pay-to` set, an address outside the list escalates to a
  human even on a handed-over budget.
- **The main wallet is not affected.** The flag lives on the sandbox row; every other payment still
  stops at the PIN, and a wallet with an armed budget still asks for the PIN for its own transfers.
- **More money is another human decision.** Asking goes through `wallet_request_sandbox`, which files
  a pending funding request; nothing moves without the operator's PIN.

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

    operator: create one_time sandbox (budget, teardown window 60..86400 s)
    operator: approve one funding transfer        -> sandbox armed
    agent:    one payment (PIN unless unattended)  -> clock starts
    ... window elapses (default 30 min = 1800 s) ...
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

## What a sandbox row carries

`wallet sandbox list` and the pane show one row per sandbox:

| Field | Meaning |
|---|---|
| `status` | `active`, `expired`, `unfunded` (created, funding not settled), `revoked` (operator closed it; it may still hold a remainder) or `destroyed` |
| `budget` / `spent` / `remaining` | the bookkeeping the policy engine enforces |
| `live_balance` | what the sub-account actually holds on chain; `null` when it could not be read |
| `live_balance_state` | `live`, `locked` (unlock to read), `unavailable` (chain down) or `unsupported` (that chain cannot report a sub-account balance) |
| `live_balance_error` | the reason behind a non-`live` state, in one line |
| `revoked_at` / `was_destroyed_with` | when an operator revoked it, and what it held when it was destroyed |
| `destroy_in` / `destroy_at` | seconds until a one-time sandbox tears itself down, and the deadline |
| `destroy_blocked` | why a teardown is holding off (for example `sweep_failed: …`) |

The bookkeeping and the chain number are deliberately separate: after a sweep `remaining` can be `0`
while dust is still in the account, which is why the sweep reads `live_balance` before deciding.

**On unrecoverable funds.** Destroying key material is irreversible, so the wallet only destroys
after the balance is provably zero. Monero sub-accounts are derived from the main wallet's keys, so
"destroy" here means the sandbox is closed and forgotten - any dust left behind is still yours, in
an account you can recover from the wallet. If a later adapter gives a sandbox its own key (EVM
session keys), the same order holds: money out, then keys gone. `hermes wallet sandbox destroy`
takes the identical path, and `--force` only skips the "is it one-time" check, never the fund checks.

`--destruct-after` takes 60..86400 seconds and defaults to 1800. The same clock is exposed to the
agent as `self_destruct_seconds` on `wallet_request_sandbox`.

## Hardening against an injected agent (from a blunt external review)

The review's verdict: the core idea is sound, the containment was not. Each finding and its fix:

| Finding | Fix |
|---|---|
| An agent could name its own `allowed_payees` in `wallet_request_sandbox`, so one funding approval handed over the budget payable to the attacker | proposals drop `allowed_payees`/`allowed_profiles` and report `ignored`; only the operator sets them |
| A late funding approval re-armed a revoked sandbox | `_broadcast` refuses to arm a sandbox with `revoked_at`/`destroyed_at`; revoke cancels the pending funding request and burns its approval |
| A revoked sandbox reported `sandbox_unfunded` while a spend returned `sandbox_ok` | explicit `sandbox_closed` / `sandbox_destroyed` refusals, with the sweep still allowed so money is recoverable |
| `sandbox_sweep` returned `remaining − reserve` whether or not the transfer happened | `swept`/`sent` come from the transfer's tx hash; a refusal says `swept: 0` and names the reason |
| Every real-Monero teardown stranded up to `fee_cap` (0.0005 XMR) because the adapter has no static fee estimate | the sweep plans the transfer once and reserves the *planned* fee |
| `spent` excluded the network fee, so `remaining` over-reported | the budget is charged `amount + fee`, and receipts carry `sandbox_id` |
| A request left `approved` with no tx hash survived a restart unexamined | reconciled to `failed` on load |
| An agent could sweep its own budget shut via `sweep_of` | sweeps are operator-scope only (`sweep_operator_only`) |

Not fixed, and documented as advisory: `allowed_profiles`. All profiles present the same agent
token, so the profile in a request is self-asserted; the field is a guard rail between cooperative
profiles, not a boundary.
