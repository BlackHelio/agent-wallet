# Changelog

## 0.3.0 — submission release

The release that makes the wallet installable from the catalog and honest about what it can do.

### Added

- **XRP**: derivation, address validation and balances, plus reveal/copy of receiving addresses in
  the pane. Receive-only by design — this release does not sign XRP payments.
- **Coins, not chains, as the mental model**: the wallet ships with XMR, BTC and XRP; a `coins ▾`
  menu in the pane (and `hermes wallet coins --enable/--disable`) adds or removes a coin, its chain,
  its address and its balance row in one step. Removing a coin cancels requests that were waiting on
  it and cannot strand a funded sandbox.
- **One wallet home, one resolver** (`walletd/home.py`): CLI, daemon, unit file, dashboard and
  profile list all resolve `<HERMES_HOME>/wallet` the same way, with `AGENT_WALLET_HOME` as an
  override and XDG as a last resort. A second keystore elsewhere is reported, not silently ignored.
- **HTTP-402 purchases end to end**: a seller's demand is read off the wire, hashed into the request,
  and settled with an EIP-3009 authorisation when funds exist (`tests/test_pay402.py` covers
  quote → approve → `X-PAYMENT` → 200 offline).
- **A CLI that reads like a wallet**: `status`, `balances`, `coins`, `requests`, `approve`, `verify`,
  `ledger`, `receive`, `proof` print decoded fields rather than JSON dumps; amounts sit beside their
  coin.
- **Three new agent tools**: `wallet_incoming` (each labelled subaddress: expected, received,
  waiting/partial/paid), `wallet_cancel` (withdraw your own pending request, receipted) and
  `wallet_spend` (totals per asset and payee over `30d`/`12h`/ISO windows, read from the same
  approved receipts the caps use).
- **CI** (`.github/workflows/ci.yml`): Python 3.12 and 3.13, the offline unittest suite, `node
  --check` plus the render harness, and `hermes plugins validate` where Hermes is installed.

### Security and hardening

- Two external review rounds (17 and 12 findings) closed, including an EVM address-derivation bug
  that would have sent funds to the wrong address, and a signed SSRF: an agent-supplied URL could
  reach loopback, the dashboard or cloud metadata. 402 quoting now refuses every non-public address
  unless the operator explicitly allows loopback for the bundled demo seller.
- **One choke point for the human gate**: every send path passes the passphrase check; a locked
  wallet denies and records instead of queueing forever.
- **Archiving history no longer resets spend caps**; a cap is re-checked at approval time, not only
  at request time, so a queue of pendings cannot multiply a daily limit.
- **Bitcoin**: a testnet wallet that could not pay, and a single HTTP 429 that took a chain offline —
  both fixed (fee/UTXO handling, endpoint failover).
- Monero wallet password moved out of `wallet.env` into the sealed keystore; the plaintext copy is
  stripped on next unlock.
- Sandbox profile lists survive a stale backend and are enforced against the declared profile name
  (cooperative: the OS user remains the boundary).

### Submission

- Public GitHub mirror published and pinned by 40-character SHA in the catalog entry.
- The catalog security scan reports `safe`: the `crypto_mining` hit was a false positive on the
  official Monero download host, and validation now passes end to end.
- `docs/UPDATE-SAFETY.md` rewritten around the surfaces actually used; `docs/DEMO.md` is a timed,
  recordable shot list that needs no funded wallet.
