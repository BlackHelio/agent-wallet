# PLAN — agent-wallet v0.1 (hackathon slice)

Deadline: **2026-11-11 17:00 EST** = 23:00 CET. Build days: Nov 6, 7, 8, 9, 10; submit Nov 11.

This is a three-week product. What follows is the six-day *slice* that is demo-able, honest, and
catalog-valid — not the whole thing. The cut ladder below is part of the plan, not a failure mode.

---

## 1. What ships

A Hermes plugin that gives an agent a wallet it cannot rob:

- **Desktop pane** (`Wallet`): unlock, balances, pending approvals with decoded intent, active
  mandates with Revoke, receipt ledger.
- **Agent tools**: request payment, request swap quote, request swap, propose/cancel mandate,
  read status, verify receipt. None of them can sign or approve.
- **`walletd`**: separate process holding an encrypted keystore; evaluates policy; signs only on
  operator approval or inside a live mandate; writes signed receipts.
- **Bundled skills**: how to buy from an x402-priced API, how to run a checkout flow, how to
  swap, and the rule that every spend goes through a request tool.

### The 90-second demo (this is the contract — if it cannot be filmed, the build is wrong)

| t | What the camera sees |
|---|---|
| 0:00 | Desktop pane open, wallet locked, two accounts listed (addresses only) |
| 0:08 | Passphrase typed in the pane → balances appear (Base Sepolia) |
| 0:15 | Chat: "buy the image from the seller at :8188, keep it under 0.10" |
| 0:22 | Pane lights up: **0.05 USDC → 0xSELLER…, Base Sepolia, image, requested by agent** |
| 0:28 | Click **Approve** → tx hash → receipt row appears → image lands in chat |
| 0:35 | Agent tries 0.25 for a second item → **blocked inline**: "per-call cap 0.10" + DENY receipt |
| 0:45 | Agent proposes a mandate: "0.02/task to seller, max 0.20/day, 24h" → pane shows it → approve |
| 0:55 | Second purchase settles with **no prompt** (inside mandate) |
| 1:05 | Click **Revoke** → same purchase now asks for approval again |
| 1:12 | `hermes wallet verify` → chain OK; edit one byte → **FAIL** |
| 1:20 | Close on: *"the agent never had a key."* |

---

## 2. Design invariants (do not trade these)

1. No key material in the Hermes process; no agent-scope method returns secrets.
2. Two scopes: `agent` requests, `operator` approves/unlocks. Renderer never holds a token — the
   pane talks to `plugin_api.py`, which holds it.
3. Policy lives in `walletd`.
4. Mandates: payee + absolute cap + period + expiry + revoke; agent can revoke, cannot create.
5. Signed hash-chained receipts, offline verify.
6. Unattended = deny + record. Never block a cron run waiting for a human.

---

## 3. Scope

### In v0.1 — "all crypto, focused on the big ones", XMR first
- **Monero (`monero-stagenet`, then mainnet behind `enabled: true`)** — the flagship chain:
  one subaddress per counterparty, `transfer` built with `do_not_relay` → operator approval →
  `relay_tx`, and the tx key stored in the receipt so the payee can verify payment without the
  sender's view key. Optional view-key verification helper.
- **Bitcoin (`bitcoin-testnet`)** — `bitcoind` wallet RPC, `getnewaddress`, PSBT built and
  approved before broadcast.
- **EVM + stablecoins (`base-sepolia`)** — EIP-1559 transfers and EIP-3009
  `transferWithAuthorization` for x402/MPP-style payments (no gas on the buyer side).
- **Policy:** per-tx cap, daily budget, payee allowlist, chain allowlist, mandate matching —
  chain-agnostic, one implementation for all three chains.
- **Receipts + ledger + offline `verify`.**
- **Human gate:** passphrase prompt before unlock/approval; disabling it needs operator scope +
  the passphrase + confirmation, and the settings file is HMAC-signed so a direct edit is detected.
- **Desktop pane** (unlock, approvals with decoded intent, mandates + Revoke, ledger) + settings
  page for caps; status-bar chip showing locked/unlocked and pending count.
- **Skills:** pay-a-402-API, checkout/shopping, and send-XMR (subaddress + proof flow).

### Out (explicitly, in the README too)
- Solana (v0.2; the rail abstraction is built so it slots in).
- Hardware wallets / remote signer / MPC.
- Multi-account HD wallets, fiat on-ramp, portfolio, tax export.
- Custody for third parties, escrow, dispute resolution, reputation.
- Any token of our own.

---

## 4. Day by day (exit criteria, not intentions)

### D1 — Thu Nov 6 · `walletd` core
- Keystore: `hashlib.scrypt` (n=2^15) + XChaCha20-Poly1305 (PyNaCl `SecretBox`), newline-free
  versioned envelope, atomic write, `0600`.
- UDS JSON-RPC v1 at `$XDG_RUNTIME_DIR/agent-wallet/walletd.sock`, `0600`, peer-uid check.
- Token mint: `agent` + `operator`, file-backed, rotated per start.
- Methods: `status`, `accounts`, `balances`, `chain_info`.
- **Exit:** keystore round-trips; wrong passphrase fails closed; socket rejects a foreign uid;
  `accounts` response contains no field longer than 64 hex chars (secret-leak test).

### D2 — Fri Nov 7 · signing + approvals + receipts
- `request_tx`, `approve`, `deny` with nonce + 120 s TTL bound to the exact tx hash.
- EIP-1559 build/sign/broadcast on Base Sepolia (faucet-funded test key).
- Receipt: Ed25519 over `{ts, kind, chain, asset, amount, payee, tx_hash, decision, why}` + hash
  chain to previous entry; JSONL ledger; `verify`.
- **Exit:** one real Sepolia transfer end-to-end; a replayed approval is refused; ledger verify
  passes and fails on a single-byte edit.

### D3 — Sat Nov 8 · Hermes plugin half
- `plugin.yaml` + `__init__.py`: tools via `ctx.register_tool`, `pre_tool_call` deny hook,
  `hermes wallet` CLI subcommand via `ctx.register_cli_command`.
- `plugin_api.py`: `/api/plugins/agent-wallet/*` proxy to the UDS with the operator token held
  server-side (never in the renderer).
- Policy mirror for early, readable failures; authority stays in `walletd`.
- **Exit:** `hermes -z "pay 0.01 to <test addr>"` produces a pending request that a script can
  approve; over-cap call is denied with a human message; cron-path call denies and records.

### D4 — Sun Nov 9 · Desktop pane
- Unified package: `plugins/agent-wallet/desktop/plugin.js`, SDK-only (`@hermes/plugin-sdk`,
  `react`, `react/jsx-runtime`; no JSX), pane + status-bar chip + settings page.
- Approve/deny, mandate list with Revoke, ledger view, unlock prompt.
- **Exit:** the whole demo flow up to 0:55 works by hand in the real desktop app, hot-reload
  included; nothing in the pane touches `window.hermesDesktop` or app markup.

### D5 — Mon Nov 10 · swaps, mandates, skills, polish
- Mandate create/revoke with operator approval; auto-pay evaluation path.
- Swap quote + execute (or quote + deep link if the aggregator is flaky).
- Two bundled skills; README screenshots; `hermes wallet doctor`.
- **Exit:** demo script filmed end-to-end once, unedited, under 90 s.

### D6 — Tue Nov 11 · validate + submit
- `hermes plugins validate --install-deps` clean; every warning read and answered.
- README disclosure section (network calls, subprocess, key storage, no telemetry).
- Public GitHub repo + `plugin-catalog/agent-wallet.yaml` PR (40-char SHA pin).
- X demo video + typeform. **Submit by 20:00 CET, not 23:00** — leave margin.

---

## 5. Cut ladder (drop in this order when a day slips)

1. Swap execution → quote + "open in DEX" deep link (half day back).
2. Status-bar chip → pane only.
3. Second bundled skill → one skill.
4. Mandates → keep create/cancel but demo only manual approval (auto-pay shown as a recorded
   earlier run, not live).
5. USDC → native ETH only (drops ERC-20 + EIP-3009 paths).
6. Desktop pane → **stop**. Without the pane there is no human approval surface, and the entry
   stops being defensible. If D4 fails, the answer is not "ship a CLI", it is "slide the demo to
   the approval-in-CLI path and say so" — but that is a losing entry.

---

## 6. Risk register

| Risk | Tripwire | Mitigation |
|---|---|---|
| Key handling fails review | reviewer asks "where is the key" and the answer sounds hand-wavy | `walletd` as its own process, no agent-scope secret methods, secret-leak test in CI, threat model doc |
| Install scanner flags `dangerous` | `hermes plugins validate` output | no network egress beyond the chain RPC, no self-updater, no shell exec, disclose the subprocess; if `caution`, document it in the PR |
| Time: 6 days for a 3-week product | D3 exit criteria missed | cut ladder above; the demo is the contract, not the feature list |
| Desktop SDK surprise | pane cannot call the backend | `plugin_api.py` path is the documented bridge (`/api/plugins/<id>`); fall back to CLI approval only as a last resort, and only for the video |
| Testnet flakiness (faucet, RPC) | no funds / RPC down | pin two RPC endpoints, fund the test key on D1 not D2, cache a funded key |
| Approvals vs rule 12 | a payment waits forever under cron | unattended path denies synchronously with a receipt; tested on D3 |

---

## 7. Definition of done

- [ ] The 90-second script films without a retry, against a real testnet tx.
- [ ] No secret material in any tool result (test), log (grep), or plugin file.
- [ ] `hermes wallet verify` passes, then fails after a one-byte edit.
- [ ] Over-cap, over-budget, and non-allowlisted payees are denied with receipts.
- [ ] Revoked mandate reverts to approval-required within the same session.
- [ ] `hermes plugins validate --install-deps` clean.
- [ ] README disclosure lines written before the PR, not after.
- [ ] Catalog PR open with a full 40-char SHA; form + X video submitted.

---

## 8. Decisions I need from Helio

1. **Demo chain:** Base Sepolia (chosen) — or Solana devnet because `pay --sandbox` lives there?
   Base Sepolia keeps one signing path and pairs with a local 402 seller we control.
2. **Unlock model:** passphrase typed in the pane (chosen) vs OS keychain-held key. Passphrase is
   more honest for a security demo.
3. **Who signs receipts:** the wallet's own Ed25519 receipt key, separate from the spending key
   (chosen) — keeps receipts verifiable even after rotating the wallet.
4. **Public repo at submission:** mirror this repo to GitHub, since the catalog requires a
   public GitHub/GitLab clone URL. Forgejo stays the working source of truth.
