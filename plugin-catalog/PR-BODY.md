> **HOLD (2026-11-09):** not for submission yet — the mirror is private by the owner's choice, and this PR needs a public clone URL. Prepared, reviewed, and waiting on that one decision.

# Catalog PR — text to submit

The entry lives beside this file as `agent-wallet.yaml`. Everything below is what the maintainer reads;
it is written to be true of the commit the `sha:` field names, and nothing else.

## Where to send it

The Hermes plugin catalog is a repo of `*.yaml` entries. Until the maintainer publishes the exact
path, the flow is: fork the catalog repo → drop `plugin-catalog/agent-wallet.yaml` into the entries
directory → open a PR with the body below. Keep the file name and the `name:` field identical.

```bash
# one-time: fork via the web UI, then
git clone https://github.com/<you>/<catalog-repo>
cp agent-wallet.yaml <catalog-repo>/plugins/            # the entries directory the repo documents
cd <catalog-repo>
git checkout -b add-agent-wallet
git add plugins/agent-wallet.yaml
git commit -m "agent-wallet: approval-gated multi-chain wallet for agents"
git push -u origin add-agent-wallet
```

Then open the PR from the fork.

## PR title

```
agent-wallet: approval-gated wallet (XMR/BTC/XRP) where manual payments need the human's PIN
```

## PR body

```markdown
### What it is

An agent can ask, a human must authorise. `walletd` holds the keys and enforces the policy; the plugin
process never sees key material. Coins: XMR, BTC, XRP by default, ETH/POL/USDC one toggle away.

The two sentences it exists for:

- `send adrian 10$` → the daemon quotes the rate, files the payment, and the pane draws a card with the
  alias, the full destination, the coin amount, the fiat it came from, the fee, and where the address
  was read from.
- `buy this` → `wallet_pay_url` pays a checkout: an x402 endpoint quotes itself; any other page is read
  for the address it *states* (a `monero:`/`bitcoin:`/`ripple:`/`ethereum:` URI). No address on the
  page → nothing is filed. Two addresses → a question, not a choice.

### The gate

Every **manual** payment asks for a six-digit PIN, typed in the pane, on the card that states the
amount and the payee. The PIN is scrypt-hashed inside the sealed keystore payload, verified only in
the daemon, bound to the card (`expect_amount`/`expect_payee`), counted and locked out (5 → 30 s →
2 min → 10 min → 1 h, persisted, MAC'd so a reset reads as a lockout). Two deliberate exceptions, so
the description is not broader than the protection: a sandbox budget a human **hands over**
(`sandbox unattended`) and an autopay an operator marks `unattended` each cost the passphrase *and*
the PIN once, and then spend inside their caps with no per-payment PIN — capped and receipted, not
PIN-confirmed. And because the Monero signer runs as the same OS user, a process running as that user
(the agent's own terminal included) can reach it directly while the wallet is unlocked; that is the
open XMR item in `docs/THREAT-MODEL.md`, not something the PIN can close.

### Try it

```bash
hermes plugins install https://github.com/BlackHelio/agent-wallet --force --yes-deps
hermes plugins enable agent-wallet
hermes wallet demo            # keystore, signer download, simulated payment, signed receipts
```

`hermes plugins validate .` passes. The one **caution** is `crypto_mining`, raised by the string
`monero-wallet-rpc` and the official Monero download host in the signer bootstrap — see the note below.

### Repository layout

- `walletd/` — daemon: keystore (scrypt + sealed payload), policy engine, chains, ledger (Ed25519,
  hash-chained), fiat quotes, invoice reading, addresses/checksums.
- `wallet/` — the plugin half: 26 tools, `hermes wallet …` CLI, thin UDS client.
- `desktop/plugin.js` — the pane (payment card, deposit QR, contacts, sandboxes, receipts).
- `dashboard/plugin_api.py` — the pane's backend, 52 routes, operator token held server-side.
- `tests/` — 628 tests, no network: real policy engine, adversarial battery, PIN gate, invoice parsing,
  QR cross-check against an independent encoder.

### Checklist

- [x] `hermes plugins validate .` — passes (`✓ security scan — caution: proc_access`: the daemon reads
      a process's argv to identify its own instance before stopping it; read-only, documented in
      `docs/CATALOG-COMPLIANCE.md`)
- [x] `hermes plugins doctor .` — declared tools == registered tools (27)
- [x] every config key is a setting, no secrets in the manifest
- [x] MIT licence, CHANGELOG, README with install + first run
- [x] no network in the test suite; the demo needs no funds
- [x] `scripts/spec_check.py` — 21 checks that the two product sentences work through the agent tools
      (including that an agent cannot price a private address), runnable on a throwaway wallet
- [x] the pane loads from the plugin (registered page, one sidebar entry)

### On the `crypto_mining` caution

The bootstrap fetches the official CLI release from the project's own release host so it can run
`monero-wallet-rpc` (RingCT cannot be signed in-process and custody services are out of scope);
explicitly out of scope. The download is SHA256-pinned per release, and no extraction
code ships. If the catalog can allowlist the official Monero download host, the caution drops to a
clean pass; otherwise this entry ships with the caution disclosed in the README.
```

## What the maintainer will check, and where it is

| Check | Where |
|---|---|
| declared tools == registered | `plugin.yaml` + `tests/test_packaging.py` |
| one config key style, no stored secrets | `plugin.yaml` `settings:`, README "Settings" |
| desktop surface inside the SDK | `desktop/plugin.js` imports only `@hermes/plugin-sdk` + react |
| dashboard routes through the host | `dashboard/plugin_api.py` uses the plugin route decorator |
| the `sha:` names a real commit in the public repo | `plugin-catalog/agent-wallet.yaml` |
| the README's install steps work | verified from a fresh anonymous clone into an empty `HERMES_HOME` |
