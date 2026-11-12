# Threat model

Written before the code, because the whole entry is a security claim. If a control here is not
in v0.1, it says so.

### Swaps: the destination is a contract

A swap sends money to a contract and gets an amount back that is not known until the pool answers, so
every rule that protects a payment has to hold *and* two more.

- **The router is not a parameter.** It comes from `SWAP_ROUTERS` in `walletd/chains/evm.py`, is pinned
  per chain, and `settings.swap_routers` can only narrow the list. An agent, a prompt, a card or a config
  file cannot name a contract to send money to.
- **The caller cannot set the price.** `amountOutMin` is computed from `swap_slippage_bps` (default 50,
  clamped to 500 in the daemon) against a quote the daemon took itself. A caller-supplied `min_out` is
  ignored. The daemon re-quotes immediately before signing and refuses if the route fell below the
  approved minimum, so a slow human cannot be turned into a bad fill.
- **The allowance is exact.** An ERC-20 sale sends `approve(router, amount)` and waits for it to mine
  before sending the swap. There is no `approve(max)`, so a router compromised after the fact can only
  take what the swap was already about to spend.
- **A swap is not a payment.** It is recorded as `swap`, not `spend_request`: it does not inflate (or
  excuse) spend totals, and `verify` covers both the approval and the swap hash.
- **The gas floor still applies**, and when the coin being sold *is* the gas coin the amount sold counts
  against the floor, so a swap cannot leave the wallet unable to pay for its next transfer.

## Assets

| # | Asset | Where it lives | Loss means |
|---|---|---|---|
| A1 | Spending key (secp256k1) | `walletd` memory only, encrypted at rest | funds gone, silently |
| A2 | Recovery phrase | shown once at creation, in the Desktop pane only | total compromise if leaked |
| A3 | Approval integrity | `walletd` nonce/TTL state | agent spends without a human |
| A4 | Ledger integrity | Ed25519 receipts + hash chain | no way to prove what happened |
| A5 | User intent | the decoded summary in the pane | approval of something else than what is shown |

## Adversaries

| # | Adversary | Capability assumed |
|---|---|---|
| T1 | **Prompt-injected agent** | Full control of tool arguments, can call any tool, can write files, can run shell |
| T2 | Malicious plugin / MCP server in the same Hermes | Registers tools, sees the agent's context, can call UDS methods if it finds the token |
| T3 | Malicious payee / 402 seller | Controls the request body, prices, and the response |
| T4 | Local unprivileged user | Can read files it owns, connect to sockets it can reach |
| T5 | Network attacker | Can MITM plaintext, replay, delay |
| T6 | Supply chain | A dependency or a later plugin update turns hostile |

Explicitly **out of scope for v0.1**: root/OS compromise, physical access to the disk, a
malicious `walletd` binary installed after review, chain-level protocol attacks, MEV, and any
legal/KYC question. Saying so is part of being honest, not a gap.

## Controls

| Threat | Control | Status |
|---|---|---|
| T1 — agent sends funds to attacker | Signing requires an `operator` approval bound to the exact tx hash, nonce-burned, 120 s TTL. The pane shows decoded payee/amount/asset/chain, never a raw blob | v0.1 |
| T1 — a funded sandbox is redirected by a forged "blessing" | A contact saved *as the operator* is what blesses a destination, and creating one used to cost nothing — so an agent holding the operator token could mint a blessing to its own address and a funded sandbox would auto-pay it. Creating an operator contact now requires the passphrase, and an unknown scope fails closed to `agent`. Reproduced and closed | v0.5.1 |
| T1 — agent creates itself an unlimited mandate | `mandates_propose` only; `mandates_activate` is operator-only; every mandate has absolute cap + expiry | v0.1 |
| T1 — agent exfiltrates the key | No agent-scope method returns secret material; response scan test rejects long hex/word runs; `walletd` redacts logs; `export_seed` has no agent route at all | v0.1 |
| T1 — agent bypasses policy by calling the chain directly | **Bitcoin/EVM:** it has no key; keys live in `walletd` and are signed in-process. **Monero: not closed.** The signer is a `monero-wallet-rpc` running as the *same uid* as the agent, so a shell can read `signer.conf` (0600, same user) and POST `transfer` / `query_key` to loopback — past the policy engine and the PIN. Verified against the real binary: `--restricted-rpc` blocks `transfer` too, so it is not an option. The fix is a second uid (systemd `User=` under root), which the plugin cannot do for itself | **open on XMR** |
| T1 — agent pays through a *different* payment plugin | `pre_tool_call` blocks (a) known foreign pay tools, (b) **any tool whose name says pay/send/transfer/spend/checkout…**, and (c) shell-class tools whose arguments drive `hermes wallet approve/request/pay-url/sandbox` or the signer's `/json_rpc`. It is **advisory**: the boundary is the daemon + PIN, and a same-uid shell can still reach the signer (see the row below) | v0.5.1 |
| T2 — another plugin steals the agent token | Token file `0600` in the runtime dir; a foreign process still only gets `agent` scope, which cannot approve or unlock | v0.1 |
| T2 — another plugin calls `approve` | No operator token outside `plugin_api.py`; approval also requires an unlocked wallet | v0.1 |
| T3 — malicious seller returns a mismatched price | The 402 challenge is parsed and re-shown in the pane; approval binds the *verified* amount, not the seller's claim; amount mismatch after signing = refuse to broadcast | v0.1 |
| T3 — malicious seller in a mandate path | Mandate match includes exact payee address; a new address never inherits an existing mandate | v0.1 |
| T4 — local user reads the keystore | scrypt (n=2^15) + XChaCha20-Poly1305; `0600`; no key in the plugin dir, no key in `.env`, no key in git | v0.1 |
| T4 — local user connects to the socket | `0600` socket + peer-uid check; tokens per boot | v0.1 |
| T5 — replay of an approval | Single-use nonce, TTL, hash binding | v0.1 |
| T5 — MITM on chain RPC | Every Monero endpoint is TLS (`stackwallet`, `cakewallet`) and there is no cleartext fallback anywhere — not in the registry, not in the signer's node choice: a plaintext node lets a path observer see which outputs you queried, and a hostile one can lie about heights and balances; tx hash re-read from the RPC before reporting success | v0.9.3 (was: one plaintext fallback) |
| T5 — tampered ledger | Hash chain + Ed25519; verify fails at the first break. **Truncation** is caught by the anchor — including in the standalone `walletd verify`, which ignored it until a reviewer showed a wiped tail passing as `ok` | v0.5.1 |
| T6 — dependency turns hostile | Two crypto deps, both floors+upper bounds, `uv --exclude-newer` in CI, no vendored binaries (catalog rule 17) | v0.1 |
| T6 — plugin self-updates into something else | No self-updater. Updates only via catalog SHA bump | v0.1 (absence is the control) |

## Non-controls we will state in the README rather than pretend

- If the host is compromised, the wallet is compromised. The mitigation is v0.2: hardware wallet
  or a remote signer over an authenticated channel.
- **Monero's signer shares the agent's uid — this is the one hole a red team actually walked
  through.** Every other chain's key is inside `walletd`'s sealed payload; XMR's spend key is in
  `monero-wallet-rpc`'s own wallet file, reachable over loopback with a password the same user can
  read (`signer.conf`, 0600, passed as `--config-file` so it is not in `ps` — but 0600 to the
  same uid is still readable by that uid). Verified end to end: read the credentials,
  POST `query_key` and get the **25-word seed** back, and POST `transfer` to reach the signer's spend
  path with no PIN, no passphrase and no policy. Nothing in `walletd` can close this; only a second
  uid can (`systemd` `User=`), and a direct signer transfer leaves **no receipt** — the ledger proves
  what `walletd` did, not what the signer did. The PIN is a control against a
  confused or injected *agent*, not against a shell on this host — say that plainly instead of
  implying the key is unreachable.
- **An armed budget or autopay signs without a per-payment PIN.** Handing a sandbox over
  (`sandbox unattended`) or waiving the PIN on an autopay is a deliberate trade: one
  passphrase+PIN act, then capped, receipted spending with no human in the loop. It is not
  "nothing is signed without a PIN" — the PIN gates *manual* payments.
- A human can be socially engineered into approving. The mitigation is the decoded summary, the
  typed-amount confirmation above a threshold, and receipts that survive the mistake.
- The ledger proves *what this wallet did*, not that the counterparty is honest. Reputation and
  escrow are a different product.

- **A forged file cannot grant authority.** `sandboxes.json`, `contacts.json` and `mandates.json` are
  0600 and owned by the same uid as the agent, so a shell can edit them — and a hand-written
  `unattended: true`, `created_by: "operator"` or `enabled: true` is a *confirmation the human never
  gave*. Each row is sealed with an HMAC of its authority fields keyed by the master key (the passphrase
  is the key material), checked on every unlock: a mismatch drops the authority, writes an
  `authority_dropped` receipt, and for a sandbox marks the row `tampered` — refused for spends, funding
  and arming (a sweep still works, because it can only go to this wallet's own account 0), until it is
  revoked. Files written before the seal existed are re-sealed *without* their authority, so an upgrade
  costs the operator one re-bless / re-arm / re-activate per row rather than trusting an unsigned file.
  `tests/test_seals.py` forges each one and asserts the refusal.

- **An old confirmation is still a confirmation, so the file carries its own age.** A seal proves who
  wrote a row, not that it is the newest one: with the same uid an agent can copy `sandboxes.json`
  while a budget is armed and put it back after the operator takes the arm away — a replay of a real
  confirmation. Each authority file therefore carries a digest of its seal map, every save appends
  `authority_state {what, digest}` to the hash chain, and `unlock` compares what it read with what the
  chain last recorded. A file that disagrees is stale: its budgets are closed (`revoked_at`), the
  leftover still sweeps home, and `authority_dropped` records `stale_copy`. The anchor is read before
  it is rewritten, so truncating the chain to erase that memory shows up as `ledger_rewound` in `doctor`
  instead of passing. Verification also runs before `_load_requests` reconciles in-flight spends —
  that write used to re-seal a restored stale file and make it look newest.

- **An armed budget is a deliberate hole, and it is bounded.** `sandbox unattended` lets the agent pay
  *any* address from that budget without a PIN. So an injected agent with an armed budget can empty
  that budget into its own account — by design, and that is the whole reason the money lives in a
  separate account on Monero. What it still cannot do: arm a budget (that is `sandbox_set_unattended`,
  passphrase + PIN), arm a proposal (`unattended_needs_operator`), raise its ceiling (a top-up is a
  pending funding request), survive expiry or revoke (both end the arm), or touch the main wallet (the
  arm lives on the sandbox row, and every other payment still stops at the PIN). On a **pooled** budget
  the coins never left the main account, so arming one is policy-only containment — the daemon returns
  `custody: "pooled"` and a warning saying so, rather than letting the word "sandbox" imply custody.

- **A payment can strand a chain, so the wallet keeps a gas floor.** On Base and Polygon the coin that
  pays for a transfer is a different coin from the one being sent, and the balance that pays for the
  *next* transfer is the same one. Spending it all is not a payment, it is the last one that chain can
  make — and nothing errors when it happens: the transfer succeeds and every later payment fails at
  signing until a human funds the address. The floor is `gas_floor_transfers` (default 4) × the current
  fee ceiling, checked at planning *and* at broadcast (the fee is re-read between the PIN and the send),
  refused as `gas_floor` — retryable, because a top-up makes the same request payable. A 402 is exempt:
  the seller submits it and pays the gas. `gas_reserve` holds a refill address per native coin and the
  wallet never spends from it, so the reserve cannot be reached by the agent at all.

## Test plan for the claims above

1. `test_no_secret_in_responses` — call every agent-scope method, fail on any 64-hex run or
   mnemonic-shaped token in the payload.
2. `test_agent_scope_cannot_approve|unlock|activate|export` — each returns `forbidden`.
3. `test_replayed_approval_refused` — same nonce twice.
4. `test_approval_hash_mismatch_refused` — approve req A with tx hash from req B.
5. `test_mandate_exact_payee` — a lookalike address does not match.
6. `test_unattended_denies` — no operator, request denies within 2 s with a receipt.
7. `test_ledger_tamper_fails` — flip one byte, `verify` exits non-zero.
8. `test_foreign_uid_refused` — connect to the socket as another uid (needs a second user; mark
   opt-in when unavailable).
