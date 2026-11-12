# Publishing: the checklist that has to be true before the catalog PR

> **Status 2026-11-09 — on hold by the owner's decision: there is no public copy.**
> The GitHub mirror exists as a **private** repository (it was briefly public at an older version and
> has been made private), so the catalog's first requirement — a public `https` clone URL — is not
> met. Do not open the catalog PR until section 1 is green by the owner's explicit choice. Everything
> else in this file is already done and can be re-run at any time.

Every line is a command with an expected answer. If a line is unchecked, do not open the PR.

## 1. The repo is public and cloneable over https

```bash
git ls-remote https://github.com/BlackHelio/agent-wallet.git HEAD     # must print a SHA
```

- [ ] a public mirror exists at all, and its `main` **is** the commit the catalog entry pins (the
      mirror today is private and carries an older tree: 4f148cf, v0.3.0, 22 tools)
- [ ] `git clone` from that URL, then everything in section 2 passes **in the clone**, not in the
      development checkout

## 2. The tree passes its own gates

```bash
hermes plugins validate .        # expect: Validation passed.  ✓ security scan — caution
hermes plugins doctor .          # expect: 23 tool(s), 2 hook(s), OK
python3 scripts/spec_check.py    # expect: 21/21
python3 scripts/rehearse.py      # expect: 17/17
python3 scripts/pane_probe.py    # expect: 15/15
python3 scripts/qr_check.py      # expect: 49 checks, 0 mismatches
python3 -m unittest discover -s tests -t .   # expect: 628 tests OK
```

The scan is a **caution**, not `safe`: the keyword rule rates the coin's name beside an ordinary verb
as extraction work. `docs/CATALOG-COMPLIANCE.md` records it; the PR asks for nothing but review.

## 3. The manifest and the catalog entry agree

```bash
grep '^version' plugin.yaml plugin-catalog/agent-wallet.yaml      # same version
hermes plugins doctor . | head -1                                  # same version here too
grep -c '^    - wallet_' plugin-catalog/agent-wallet.yaml           # 23 (four-space list)
git rev-parse HEAD                                                 # == sha: in the catalog entry
```

- [ ] `sha:` in `plugin-catalog/agent-wallet.yaml` is the **40-hex commit** that section 2 passed at,
      and that commit is pushed
- [ ] `version:` is the same string in `plugin.yaml`, `walletd/__init__.py`, `dashboard/manifest.json`,
      `desktop/plugin.js` and the catalog entry (`tests/test_packaging.py` enforces this)
- [ ] `provides_tools` lists the same 23 names, in `TOOL_SPECS` order
- [ ] MIT licence present; `CHANGELOG.md` has an entry for this version

## 4. No secrets, no junk, no machine-specific paths

```bash
python3 scripts/secret_scan.py --history --env-file ~/.env    # expect: OK (tree and history)
git ls-files | wc -l                                          # 106 files, no scratch, no .env
```

`secret_scan.py` runs three passes: known secret shapes, high-entropy literals with address
alphabets/hashes allowlisted, and — with `--env-file` — a direct match of every live credential value
against every file. `--history` walks every blob this repo ever contained, because a secret deleted in
a later commit is still in the packfile.

- [ ] `secret_scan.py --history` is clean; if it reports a *path* to key material in an old blob, decide
      whether to rewrite history (`git filter-repo`) or accept it — rewriting changes every SHA, so the
      catalog `sha:` must be re-pinned afterwards
- [ ] the only absolute paths in the tree are inside the docs' examples
- [ ] `wallet.env`, `keystore.json`, tokens and ledgers are **not** tracked (`git ls-files` shows only
      source, docs, tests, scripts)

### 4b. Publish a snapshot, not the history

```bash
bash scripts/publish_snapshot.sh ../agent-wallet-public   # prints the sha to pin
```

An internal working history can carry machine-specific paths, private network addresses and the
location of local key files inside a sidecar doc that was deleted later. No *value* is exposed —
`--env-file` compares every live credential and finds nothing — but a deleted file is still in the
packfile, so a mirror with history publishes the layout. The snapshot is one
commit of the current tree; it runs the same scan (tree **and** history, with and without the live
`.env`) and refuses to print a sha if either fails.

The sha the catalog PR carries is the **snapshot's** commit, because that is the only one a reviewer
can fetch. The `sha:` in this repo's own copy of the catalog entry is internal bookkeeping and will
differ; do not paste it into the PR.

If you would rather publish the history, replace the strings first (`git filter-repo --replace-text`),
then re-pin the catalog: every commit id changes from the first rewritten one onward.

## 5. A stranger can install and run it

```bash
export HERMES_HOME=$(mktemp -d)                 # exported, or the next line runs against your real home
hermes plugins install file://$PWD --force --yes-deps
hermes plugins enable agent-wallet
printf 'pub-demo-passphrase\n' | hermes wallet demo --no-install --passphrase-stdin
```

- [ ] install prints `✓ Installed` (a bare path is parsed as `owner/repo`; `file://` is required)
- [ ] `demo` prints its own throwaway home and never touches `$HERMES_HOME/wallet`
- [ ] the Desktop page appears under **Capabilities → Plugins** after a disable/enable toggle, and the
      pane shows balances, the approval card and receipts (not "no wallet")

## 6. What only a human can do

- [ ] record the demo video (shot list: `docs/DEMO.md`, under 90 s) and post it on X tagged
      `@NousResearch`
- [ ] submit the Typeform entry
- [ ] open the catalog PR with `plugin-catalog/PR-BODY.md` — **only after** sections 1–5 are green on
      the public mirror

## Known limits, stated so nobody "discovers" them in review

- **Monero's signer shares the agent's uid.** `docs/THREAT-MODEL.md` marks it open; the PIN protects
  against an injected agent, not against a shell running as you. The fix is a second uid (root once).
- **Real mainnet payments have been broadcast from this tree.** One on Base mainnet:
  `0x2931f2fde073e76fbe5764715cd0148605c76efc1df1df142ef0cd12faf24c04` at block 56173713,
  0.00005 ETH to a second wallet, fee 0.000000126 ETH, signed by `walletd` after a PIN approval.
  Monero and Bitcoin mainnet sends are still exercised on stagenet/testnet only; `docs/LIVE-TEST.md`
  is the runbook for the first one on each.
- **XRP is receive-only.**
- **The pane and the CLI can both approve.** The pane is the default operator surface; it is not the
  only one, and `README` does not claim otherwise.
