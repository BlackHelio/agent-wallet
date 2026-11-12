# Hardening log — external review, 2026-11-05

An independent review of this tree (read-only, no execution) produced a ranked list of holes.
This file records what was found and what changed, so the next reviewer can see the delta rather
than re-derive it. Everything below is verified by a test or a live check named next to it.

## Closed — catalog review, 2026-11-12 (reviewed at `524e5872`)

| Finding | Fix | Proof |
|---|---|---|
| `wallet/daemon.py` spawned `walletd` with `dict(os.environ)`, so the daemon inherited every provider key the agent held | `wallet/daemon.py::child_env()` builds the child's environment by allow-list (PATH, HOME, PYTHONPATH, TMPDIR, HERMES_HOME/PROFILE, AGENT_WALLET_HOME, XDG_*, plus the wallet's own `AGENT_WALLET_*`/`WALLETD_*` settings); `WALLETD_PASSPHRASE`/`WALLETD_PIN` reach `init` only and are never inherited by a `serve` child | `tests/test_process_env.py` — a planted `OPENROUTER_API_KEY` is absent from both the `init` and the `serve` env, and `AGENT_WALLET_*` survives |
| The vendor binary `monero-wallet-rpc` was started with the inherited environment | `walletd/signer.py::_child_env()` — `PATH` and `HOME` only, used by `spawn()`, `preflight()` and `status()` | same test file; live check: `/proc/<pid>/environ` of a running signer holds exactly `HOME` and `PATH` |
| The signer's RPC login was `--rpc-login` in argv, visible in `/proc/<pid>/cmdline` to every local user | Written to `<wallet home>/signer.conf` (0600 from the first byte, atomic replace) and passed as `--config-file`; argv is `[binary, --config-file, path]` | live check against the real 0.18.5.4 binary: Digest auth 200 with the file's login, 401 with a wrong one; `argv` carries no credential |
| `plugin.yaml`/README said nothing is signed without a PIN | Both now state that an armed budget/autopay signs unattended (capped, receipted, no per-payment PIN) and that while unlocked a same-uid process — including the agent's terminal — can reach the Monero signer directly | this file, `README.md` intro + *The one security caveat*, `docs/THREAT-MODEL.md` non-controls |
| (recommended) the plaintext Monero node could spoof heights and balances | `cakewallet` is HTTPS; the signer's no-healthy-node fallback is the first registry entry instead of a hardcoded cleartext URL | `tests/test_chains.py::test_every_monero_endpoint_is_https` |

## Closed

| Finding | Fix | Proof |
|---|---|---|
| **Signer RPC had no auth** (`--disable-rpc-login`). Any same-uid process — including the agent's own shell — could POST `transfer` or `query_key {mnemonic}` to `127.0.0.1:38084`, bypassing every cap, approval and the human gate, and read the seed. | `--rpc-login` with credentials generated once into `wallet.env` (0600), handed to the binary as a 0600 `signer.conf` (`--config-file`) since 0.9.3 so the login is not in argv. `walletd/httpdigest.py` speaks epee's Digest auth. | `curl` without credentials → **401**; with them → 200. `wallet doctor` → signer reachable. |
| **Approve never re-evaluated policy.** Queue 3 × 0.4 XMR against a 1.0 cap, approve all three, 1.2 XMR moves. | `approve()` re-runs `evaluate()` with the human-present context and denies + receipts the attempt. | `test_queued_approvals_cannot_multiply_the_daily_cap` |
| **No lock on a threaded server.** Two concurrent calls read the same ledger `last()` → duplicate `seq`/`prev` → the hash chain is broken permanently; budgets double-spend. | `State.mutex` held around every dispatch; server backlog raised 5 → 128 so bursts stop failing with EAGAIN. | `test_parallel_requests_keep_the_chain_valid` (16 threads, chain verifies, seqs contiguous), `test_parallel_contacts_keep_the_chain_valid` |
| **Adapters read the wallet password from the environment only.** Ad-hoc start → empty password → "invalid password", every chain offline, silently. This was the root cause of the recurring "no chain is reachable". | One source of truth: `_signer_password()` (env → `wallet.env`). Adapter failures now carry the real error instead of "wallet not openable". | `test_adapters_use_the_stored_password_not_the_environment`; cold start → both chains live on the first unlock |
| **`daemon.start()` raised `NameError`** (`fcntl`/`lock_fd` orphaned from a removed starter lock) — every non-systemd start was broken. | Orphan block deleted: the daemon itself holds the lifetime flock and refuses a live socket, so the parent only spawns. | `hermes wallet up` on a clean host |
| **The pane's Approve always returned 400.** JS sent `passphrase`, `ApproveBody` did not declare it, Pydantic dropped it, the human gate demanded it. | Field declared and forwarded; the pane now shows a passphrase field while unlocked too. | `tests/smoke_mount.py` → `approve 200` through the exact route the pane calls |
| **`cli.py` KeyError on first run** (`values["AGENT_WALLET_MONERO_WALLET"]` when only the password was written). | The wallet file name is a signed setting, not an env var; the line is gone. | `hermes wallet config set` |
| **Sandbox ACL bypass:** `allowed_profiles=["wallet"]` plus an empty `profile` skipped the check. | Deny when the allowlist is set and the profile is empty or absent. | `test_sandbox_with_empty_profile_is_denied` |
| **Mandate/sandbox spend with no adapter** returned `approved, signed: false` — the agent was told it paid; nothing moved. | Deny with `chain_unavailable` + receipt before any approval is recorded. | covered by policy tests |
| **Activated mandates vanished on restart**, silently turning off auto-pay. | `<home>/mandates.json` (0600), written on propose/activate/revoke, loaded at unlock; malformed rows are skipped individually. | `test_mandates_survive_a_restart` |
| **`signer_start` always picked a mainnet node**, so a stagenet wallet opened against a mainnet daemon could not sync. | Node chosen from the wallet's own `monero-*` kind. | `wallet chains` |
| **Locked balances carried no address**, so funding required unlocking first. | Public addresses cached to `<home>/addresses.json` on unlock and shown while locked. | live: address visible before unlock |
| **EVM kind names mismatched** (`base-mainnet` etc. rejected as unknown by `payee_key`). | Names aligned with the node registry. | `payee_key('base-mainnet', …)` |
| **A stale signed port** (`monero_rpc_url` on an older default) made every probe look at a port the signer is never spawned on. | On unlock the URL is reconciled to `DEFAULT_PORT`, which is the only port the plugin spawns. | PC went from `doctor false` to live balances |
| **Unlock blocked on signer startup** (up to 20 s, or a 120 MB first-run download) and timed out at 60 s. | Signer bring-up runs in a background thread; unlock returns immediately and the next refresh finds live balances. | PC `unlock: True` instantly, chains live 3 s later |
| **No way to create a keystore from the pane** (`/wallet/init` died with `no_keystore`, and `python -m walletd init` used a different default home). | `create_keystore` RPC (operator scope) + `POST /keystore/create`. | route smoke |

## Closed in the second pass

| Finding | Fix | Proof |
|---|---|---|
| **Signer hash came from the vendor's `hashes.txt` over HTTPS with its GPG signature unchecked.** | The SHA256 of the exact release tarball is pinned in code; `install()` refuses any version that is not pinned (`release_not_pinned`). The hashes.txt fetch is gone. | `install(version="0.18.5.5")` → `release_not_pinned` |
| **Per-profile systemd units:** one shared unit baked in whichever profile installed it, so profile B's start targeted A's home and timed out. | `service.unit_name(home)` — the default home keeps `agent-wallet.service`, every other home gets `agent-wallet-<hash>.service`, and `managed_by_service()` requires the unit's `--home` to be *this* home. | `<HERMES_HOME>/wallet` → `agent-wallet.service`; `<HERMES_HOME>/profiles/wallet/wallet` → `agent-wallet-5f8b0640.service` |
| **Approving a request on a chain with no adapter** recorded `approved` while nothing was signed. | `approve()` refuses with `chain_unavailable` and receipts the denial. | live: a 0.025 USDC 402 request → denied, nothing sent |
| **"Buy me an X" was prose.** The agent supplied price and payee, so a wrong or injected number reached the human as an approval. | `walletd/pay402.py` fetches the URL, reads the *server's* 402 demand (x402 header or body, atomic units, network→chain mapping), and opens a normal request from that quote. The demand's digest is bound into the request. `wallet_pay_url` tool + `hermes wallet pay-url`. L402/Lightning and unknown networks are refused with a named reason instead of guessed. | `tests/test_pay402.py` (9 tests, real HTTP server): atomic-unit parsing, header form, L402 refusal, unsupported network, `the_caller_cannot_set_the_price`, digest changes with price |

## Third pass — the other chains

| Change | Detail | Proof |
|---|---|---|
| **EVM signing in-process** (`walletd/chains/evm.py`) | EIP-1559 transactions and ERC-20 `transfer` signed with `eth-account` using a key from the sealed keystore. Base, Base Sepolia, Polygon, Polygon Amoy. Public RPCs are walked with failover, because `polygon-rpc.com` now answers "API key disabled" for anonymous callers. | `tests/test_chains.py`: recovered sender == our address; USDC calldata decodes to `transfer(payee, 25000)`; empty balance refused before signing |
| **Bitcoin signing in-process** (`walletd/chains/bitcoin.py`) | P2WPKH, inputs selected largest-first, fee from `/fee-estimates`, BIP143 sighash, witness assembled by hand. | witness verifies against the sighash of the parsed transaction; change returns to the wallet; no UTXOs → `insufficient_funds` |
| **Refuse doomed EVM transfers** | A token transfer with no token balance, or no native coin for gas, is denied before a signature exists — otherwise the wallet pays gas to revert. | live: 0.025 USDC 402 attempt → `insufficient_funds: need 0.025 USDC, have 0` |
| **Defaults are real chains** | XMR/BTC/XRP by default, ETH/USDC/POL one toggle away; the simulator is opt-in. The stale `38083` RPC default is gone (that was the PC's "offline" bug). | fresh install shows three live addresses |
| **Coins toggle** | Desktop Settings gains a per-chain switch grid writing `chains_enabled`; the colour follows the coin. | render harness + seeded dump |

## Open, known

- **Live broadcast is unproven**: the wallet holds 0 XMR, so approvals end at `signed` (simulated)
  rather than a confirmed transaction. Every other link (plan, policy, approval, receipt, verify) is
  exercised; the last hop to a real chain is not.
- **XRP is receive-only.** `xrp.py` derives and validates addresses and reads balances; it refuses to
  sign a transfer with a named reason rather than pretending.
- **Merchants that are not 402** (plain HTML shops): the agent still reads the page and quotes it itself. Only 402 endpoints get a server-verified price.
- **The catalog scanner finding is open, and disclosed.** `hermes plugins validate` passes with one
  `crypto_mining` **caution** — a false positive on the official Monero download host (the rule keys
  on the coin/host vocabulary). No mining code ships and the signer tarball is SHA256-pinned; the
  ask is a maintainer allowlist entry for that host. See `CATALOG-COMPLIANCE.md` rule 7a.

## Standing invariants (unchanged, now enforced harder)

1. Keys exist only inside the signer; the plugin process and the agent never see them.
2. The agent can request, never approve.
3. The human gate needs a passphrase for every manual payment, and no agent-facing call can disable it.
4. Caps are enforced at request **and** at approval time.
5. The receipt ledger is an append-only Ed25519 hash chain, and every mutation is serialised.

## "No passphrase, no transaction" — and where it is enforced

When the passphrase prompt is **on** (`human_gate`), a transaction reaches a network only if all of
these hold. Each one is a single check in the code, not a convention:

1. **The daemon holds keys.** `_broadcast` refuses unless the keystore is unlocked, so a locked
   wallet has nothing to sign with - no adapter, no signer, no key in memory. Locking also kills the
   Monero signer process.
2. **The request carries an authorisation a human produced.** `SpendRequest.authorised_by` is set in
   exactly three places: "passphrase" in `approve()` after the passphrase verified against the
   keystore envelope, and "mandate" / "sandbox" for standing authority the human created with the
   passphrase. `_broadcast` refuses anything else with `not_authorised` - including a request that was
   never approved at all.
3. **Manual approval needs the secret at the moment of approval.** `approve()` re-opens the keystore
   with the supplied passphrase; the approval token is bound to a digest of the request (excluding
   bookkeeping fields), is single-use, and expires.
4. **Standing authority still needs an unlocked wallet.** A mandate or sandbox spend while locked is
   refused by check 1. Standing authority is a *permission*, never a key.

Verified live (simulated chain, real daemon):

```
approve with no passphrase                -> passphrase_required
approve with a wrong passphrase           -> passphrase_required
approve while locked, correct passphrase  -> locked
request status afterwards                 -> pending, tx_hash "", authorised_by ""
approve unlocked, correct passphrase      -> sent, authorised_by "passphrase"
```

and in `tests/test_state.py`: `test_a_locked_wallet_cannot_sign_anything`,
`test_approval_without_the_passphrase_never_reaches_the_signer`,
`test_broadcast_refuses_a_request_nobody_authorised`,
`test_a_mandate_spend_still_needs_an_unlocked_wallet`,
`test_the_gate_off_still_requires_an_unlocked_wallet`.

Turning the prompt **off** removes step 3, never steps 1, 2 or 4: with the gate off a human still
has to unlock with the passphrase for anything to be signed.
