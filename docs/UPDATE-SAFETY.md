# Surviving a Hermes upgrade

Hermes ships often. This plugin is written so an upgrade cannot take it offline, and so the parts
that *could* break are visible instead of silent. `requires_hermes: ">=0.21.0"` in `plugin.yaml` is a
floor only — an upper bound would refuse a version that works.

## What it touches, and why that is safe

| Surface | Used for | Public? |
|---|---|---|
| `ctx.register_tool(...)` | the 23 agent tools | documented plugin API |
| `ctx.register_hook("pre_tool_call", …)` | blocking foreign payment tools | documented hook |
| `ctx.register_cli_command("wallet", …)` | `hermes wallet …` | documented plugin API |
| `dashboard/manifest.json` + `plugin_api.py` | the Desktop pane's backend route | documented dashboard API |
| Desktop SDK: `registerMany` / `registerSettingsPage` + the area constants | the Wallet page, sidebar row, palette entry, settings page | documented Desktop SDK |
| `agent.secret_scope` / config | nothing — the plugin reads no Hermes secrets | — |

No core module is imported, no core function is rebound, no `sys.modules` surgery, no
`setattr` on a Hermes object. The only `setattr` in the tree is on this plugin's own `Settings`
dataclass. That is the rule the catalog's *no core override* check enforces, and it is why a Hermes
release cannot break the plugin through an internal rename — only through a documented API change,
which Hermes's compatibility contract says will be additive and announced.

## What happens if a surface disappears

Registration is defensive on both halves:

- **Python (`__init__.py`)**: every `ctx.register_*` is looked up first. A build without
  `register_hook` or `register_cli_command` still loads all 26 tools and prints which surface was
  skipped to stderr. A build without `register_tool` raises — there is nothing useful left to do, and
  a loud failure is better than a plugin that appears installed and does nothing.
- **Desktop (`desktop/plugin.js`)**: if `registerMany` is missing, the contributions are registered
  one by one through their individual registrars; if `registerSettingsPage` is missing, the wallet
  page still appears and the settings page is skipped. Each skip logs a `console.warn` naming the
  area.

Both are covered by tests: `tests/test_register.py` registers against a full context and against a
context missing the optional surfaces.

## What the daemon does *not* depend on

`walletd` is a separate process with a unix socket and its own JSON-RPC. It imports nothing from
Hermes, so agent upgrades, model changes, gateway restarts and profile switches do not touch it. The
plugin half and the daemon talk over a versioned payload of plain dictionaries; new fields are added,
never removed.

## Verifying after an upgrade

```bash
hermes plugins doctor   <plugin-dir>     # discovery, manifest, import, registration
hermes plugins validate <plugin-dir>     # 16 checks, includes the security scan
node desktop/render-check.mjs desktop/plugin.js   # the page still renders (no app needed)
hermes wallet doctor                    # keystore, signer, chains, receipts, anchor
python -m unittest discover -s tests -t .         # 227 unit tests, no network
```

`doctor` is the fastest signal: it reports registration drift (`adds tool 'x' not listed in
provides_tools`) before anything else notices.

## When Hermes *does* change a documented surface

The compatibility contract is additive: documented `PluginContext` methods are not removed or
renamed, new parameters are optional, hook payloads gain fields rather than change them. If a needed
seam is missing, the answer is an issue on `hermes-agent` asking for the hook — not a patch around
it, which is what breaks on the next release. `docs/CATALOG-COMPLIANCE.md` records the scanner
history: the `crypto_mining` caution is a false positive on the official Monero download host.
`hermes plugins validate` passes with that one caution, not as `safe`; admission needs a maintainer
allowlist entry for the host, and `docs/CATALOG-COMPLIANCE.md` rule 7a records the ask.

## Auto-lock and what is encrypted at rest

`autolock_minutes` (Settings → Auto-lock, or `hermes wallet autolock 5|30|off`) is enforced by a
watchdog thread in the daemon, not by the next request: when the window passes, `lock()` runs and the
XMR signer is terminated, the BTC/EVM keys are dropped from memory, and nothing but the encrypted
keystore remains readable.

At rest, after a lock:

| File | Contents | Protection |
|---|---|---|
| `keystore.json` | master key, receipt signing seed, BTC/EVM keys, **Monero wallet password** | scrypt (n=2¹⁵) + XChaCha20-Poly1305 |
| `monero/wallet` | the XMR keys themselves | encrypted by monero-wallet-rpc |
| `wallet.env` | only the signer's RPC credentials | 0600 |
| `ledger*.jsonl`, `contacts.json`, `mandates.json`, `requests.json` | addresses, caps, receipts | 0600 (public data, no secrets) |

`Clear history` (Receipts) archives the chain instead of deleting it, so it needs no passphrase —
it cannot destroy evidence, which is the only thing a secret would protect here.

The Monero wallet-file password used to sit in `wallet.env` in plaintext; it is now part of the
sealed payload and the plaintext copy is removed on the next unlock (`_strip_env_password`), reusing
the existing password so an older wallet still opens.

## What is in the keystore after the first unlock

| Secret | Where it lives | Protection |
|---|---|---|
| Monero spend/view keys | `<home>/monero/` wallet file | encrypted by `monero-wallet-rpc` with a 43-char password that itself lives inside the sealed payload |
| Bitcoin key (p2wpkh) | `keystore.json` -> `keys.btc` | scrypt n=2^15 + XChaCha20-Poly1305, file 0600 |
| EVM key (EIP-1559 / EIP-3009) | `keystore.json` -> `keys.evm` | same envelope |
| XRP key (XRPL classic address) | `keystore.json` -> `keys.xrp` | same envelope |

Every one of those is 32 bytes from `secrets.token_hex`, generated inside `walletd` on the first unlock
and never handed to the Hermes process, the pane, or an RPC caller: the daemon exposes *addresses* and
*balances*, never key material. Addresses are not stored at all - they are re-derived from the key each
time - so there is no second copy of anything to leak.

`hermes wallet backup` copies the sealed keystore (still encrypted, still passphrase-protected);
`hermes wallet seed` exports the XMR recovery words behind operator scope + passphrase + explicit
confirm, and records the export in the ledger without the words.

## Known gaps (stated, not implied)

- `hermes wallet verify` walks the **live** chain. Archived chains are recorded with the signature
  they ended on, but the archives themselves are not re-walked yet.
- A sandbox's profile allow-list is enforced against the profile name the caller declares. It stops a
  misconfigured agent; it does not stop a hostile process running as the same uid (see HARDENING.md -
  the OS user is the boundary).
- The signer's RPC password lives in `<wallet home>/signer.conf` (0600) and is passed as
  `--config-file`, so it is no longer in `/proc/<pid>/cmdline` — but it is still readable by
  any process running as this uid, and so is the signer. That is the open XMR item in
  `docs/THREAT-MODEL.md`; a second uid is the fix.

## After a Hermes update, run the check

```bash
hermes plugins validate ~/.hermes/plugins/agent-wallet     # still loadable by this version
~/.hermes/plugins/agent-wallet/scripts/update-check.sh      # the whole surface, read-only
```

`scripts/update-check.sh` answers the five things an app or gateway update can break, without
touching the wallet: does the plugin validate, is the pane the app loads still standalone (no package
marker, header present, *not* byte-identical to the package half — identical bytes get re-adopted as an
inert package half), does that pane still render in the Node harness, is the daemon healthy with an
intact receipt chain, and do the dashboard routes still import and answer. It exits non-zero and names
the failing line, so it can gate a wrapper script around `hermes update`.

Two things bit this plugin during the v0.22.5 → v0.23.0 update, both invisible to `validate`:
the package's `desktop/` half was months behind the copy the app was actually loading (a reinstall
would have silently downgraded the pane), and on one machine the loaded copy was byte-identical to
the package half, which is exactly how a pane gets re-adopted as disabled.
