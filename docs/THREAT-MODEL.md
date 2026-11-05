# Threat model

Written before the code, because the whole entry is a security claim. If a control here is not
in v0.1, it says so.

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
| T1 — agent creates itself an unlimited mandate | `mandates_propose` only; `mandates_activate` is operator-only; every mandate has absolute cap + expiry | v0.1 |
| T1 — agent exfiltrates the key | No agent-scope method returns secret material; response scan test rejects long hex/word runs; `walletd` redacts logs; `export_seed` has no agent route at all | v0.1 |
| T1 — agent bypasses policy by calling the chain directly | It has no key. The only signer is `walletd` | v0.1 (structural) |
| T1 — agent pays through a *different* payment plugin (`pay`, `mpp_fetch`, `mm_transfer`) | `pre_tool_call` denies any payment-shaped tool that has no corresponding approved request in this session; the deny is a receipt | v0.1 |
| T2 — another plugin steals the agent token | Token file `0600` in the runtime dir; a foreign process still only gets `agent` scope, which cannot approve or unlock | v0.1 |
| T2 — another plugin calls `approve` | No operator token outside `plugin_api.py`; approval also requires an unlocked wallet | v0.1 |
| T3 — malicious seller returns a mismatched price | The 402 challenge is parsed and re-shown in the pane; approval binds the *verified* amount, not the seller's claim; amount mismatch after signing = refuse to broadcast | v0.1 |
| T3 — malicious seller in a mandate path | Mandate match includes exact payee address; a new address never inherits an existing mandate | v0.1 |
| T4 — local user reads the keystore | scrypt (n=2^15) + XChaCha20-Poly1305; `0600`; no key in the plugin dir, no key in `.env`, no key in git | v0.1 |
| T4 — local user connects to the socket | `0600` socket + peer-uid check; tokens per boot | v0.1 |
| T5 — replay of an approval | Single-use nonce, TTL, hash binding | v0.1 |
| T5 — MITM on chain RPC | HTTPS only, two pinned RPC endpoints, tx hash re-read from the RPC before reporting success | v0.1 |
| T5 — tampered ledger | Hash chain + Ed25519; verify is offline and fails at the first break | v0.1 |
| T6 — dependency turns hostile | Two crypto deps, both floors+upper bounds, `uv --exclude-newer` in CI, no vendored binaries (catalog rule 17) | v0.1 |
| T6 — plugin self-updates into something else | No self-updater. Updates only via catalog SHA bump | v0.1 (absence is the control) |

## Non-controls we will state in the README rather than pretend

- If the host is compromised, the wallet is compromised. The mitigation is v0.2: hardware wallet
  or a remote signer over an authenticated channel.
- A human can be socially engineered into approving. The mitigation is the decoded summary, the
  typed-amount confirmation above a threshold, and receipts that survive the mistake.
- The ledger proves *what this wallet did*, not that the counterparty is honest. Reputation and
  escrow are a different product.

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
