# Catalog compliance

Every admission rule mapped to a decision, written now so the PR is not a surprise later. Rule
numbers follow `plugin-catalog/README.md` in `NousResearch/hermes-agent` at the time of writing.

| Rule | Requirement | Our answer |
|---|---|---|
| 1 | Public `https`-cloneable GitHub/GitLab repo | Forgejo is the working source of truth; a public GitHub mirror is pushed before submission and the catalog pins that repo |
| 2 | Full 40-char SHA pin | `plugin-catalog/agent-wallet.yaml` pins the full 40-hex commit of the public mirror that these docs and the 617-test suite were verified at; `version` is `0.9.2` and matches `plugin.yaml` |
| 3 | No self-updating code | No updater, no "check for updates", no remote code loading. The pane shows the installed version as read-only text |
| 4 | SHA bumps are new PRs | Documented in the PR body: a new SHA is a new PR, never an amend |
| 5 | Owner or major contributor submits | BlackHelio, sole author |
| 6 | Declared capabilities match reality | 23 tools and 2 hooks, listed in `plugin.yaml` and copied into the catalog entry by a script — the two files are compared by `tests/test_packaging.py`, so the catalog cannot drift from the registrations again. No middleware. `requires_env`: none; the RPC endpoints are setting| 7a | Scanner: the signer download | `wallet signer install` fetches the official `monero-wallet-rpc` release from the project's own release host, with the tarball SHA256 pinned in code (`PINNED_TARBALLS`); an unpinned version is refused, and nothing is bundled. No extraction code is present in this tree. The `crypto_mining` keyword rule no longer fires on this tree. |
 a clean scan. |
| 7 | Install scanner at admission | No `eval`, no `exec`, no shell, no `subprocess` of a shell; `walletd` is spawned as an argv list, disclosed. Current result: `hermes plugins validate .` passes with **one** caution — `proc_access` at `compat.py:267`, the process-existence probe the CLI uses to decide whether `walletd` is up — and no `dangerous` finding. The probe reads `/proc` for a pid it wrote itself; it never enumerates other processes. A `dangerous` finding would be a redesign, not a note. |
| 8 | Desktop stays inside the SDK | Single ESM file; imports only `@hermes/plugin-sdk`, `react`, `react/jsx-runtime`; no JSX; no prototype patching; no `eval`/`new Function`/`import()`; no script tags; no `document.querySelector` of app markup; no `MutationObserver` on `body`; no raw `window.hermesDesktop` calls — pane data comes from `ctx.rest('/api/plugins/agent-wallet/…')` |
| 9 | No runtime overrides of core | No `setattr` on anything imported from Hermes, no `sys.modules` writes, no private method calls on core objects. Only `ctx.register_*`, `pre_tool_call`, and the documented ABCs |
| 10 | Dependency policy | `pynacl`, `eth-account` with floors **and** upper bounds; no newest-release floors; `uv --exclude-newer` in CI |
| 11 | Credentials only through Hermes | The plugin reads nothing from `.env`, `auth.json`, keychains or other wallets. An RPC URL that contains a token is read with `agent.secret_scope.get_secret`. The keystore is the wallet's own encrypted data, opened only by `walletd` on operator passphrase — it is not a credential store being scraped |
| 12 | Approvals and unattended runs respected | No auto-approve; no YOLO inheritance; no spawning Hermes children. Unattended = synchronous deny + receipt |
| 13 | Risky behaviour disclosed | README disclosure block covers: chain RPC calls (which providers), the HTTP fetch of a seller's 402 terms, the `walletd` subprocess, key storage location and encryption, no telemetry, no third-party analytics |
| 14 | Truthful compat metadata | `requires_hermes` a SemVer floor no newer than the release we test against; `version` matches the pin; deps resolve under core constraints (`validate --install-deps`) |
| 15 | No skins or forks of bundled plugins | Original plugin, own name |
| 16 | One listing per lineage | Original, no fork lineage |
| 17 | No unscannable executable payloads | Pure Python source + one ESM file. No wheels committed, no `.pyc`, no archives, no WASM |
| 18 | Password-manager login backends | N/A — no `browser_vault` backend |

## Disclosure (this is the README's block, mirrored here)

> **Network:** `walletd` talks to public chain endpoints to read balances and broadcast approved
> transactions — a Monero node for XMR, an Esplora API for BTC, XRPL JSON-RPC for XRP, and an EVM RPC
> for Base/Polygon when those coins are enabled. The endpoint list is in `walletd/chains/nodes.py`
> and is configurable. A request to an HTTP-402 (x402) seller fetches that seller's own price page.
> No telemetry, no analytics, no account.
>
> **Process:** the plugin starts `walletd`, a local Python process, on demand. It holds the encrypted
> keystore and is the only component that can sign. On first unlock it may download the official
> Monero command-line wallet binary from the project's own download page, SHA256-pinned in code — or
> you can install it yourself with `wallet signer install --from-path` and it never fetches anything.
>
> **Keys:** the keystore lives at `<HERMES_HOME>/wallet/keystore.json` (0600), encrypted with
> scrypt (n=2^15) + XChaCha20-Poly1305 and unlocked by a passphrase typed in the Desktop pane. The
> agent has no route to the key, the Monero wallet file, or the recovery words.
>
> **Money:** the default coins are mainnet XMR, BTC and XRP, so this plugin moves real funds.
> Nothing is spent without a passphrase approval, or inside a mandate/sandbox a human created with
> that passphrase.

## Pre-submission checks

```bash
hermes plugins validate ./agent-wallet --install-deps        # catalog CI gate
hermes plugins validate ./agent-wallet --json | jq '.warnings, .capabilities'
grep -rn "eval(\|exec(\|os.system\|shell=True" --include=*.py .
grep -rn "hermes" walletd/ | grep -v "^walletd/.*:.*#"       # trusted half stays Hermes-free
```

Expect `warnings: ["security scan caution: proc_access (walletd/compat.py:N)"]`. It is a true
positive and it is the one concession in this tree: `process_cmdline()` reads
`/proc/<pid>/cmdline` (or `ps -ww -o args=`, or `psutil`) to check that the process `wallet down`
is about to stop was started for *this* home — the alternative is killing another profile's daemon.
Read-only, no writes, no shelling out with untrusted input. A `dangerous` result, or any code other
than this one, is a stop.
