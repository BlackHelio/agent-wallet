# Setup — the plugin is self-contained

Nothing runs alongside it: no node, no Docker, no SSH tunnel, no second service you install
yourself.

```
┌─────────────────────────────────────────────────────────────────┐
│ Hermes + this plugin (one Python environment)                   │
│                                                                 │
│   walletd ── keys, policy, contacts, sandboxes, receipts         │
│      │                                                          │
│      └─ supervises ── monero-wallet-rpc                         │
│                        binary this plugin downloaded and         │
│                        SHA256-verified into <wallet home>/bin    │
└─────────────────────────────────┬───────────────────────────────┘
                                  │ outbound HTTPS
                                  ▼
                   a public node from the registry
                   (cakewallet → stackwallet, …)
```

## First run

```bash
hermes plugins install BlackHelio/agent-wallet --enable   # or a checkout + `hermes plugins enable`
hermes wallet service install     # optional: one supervised owner for walletd (systemd --user)
hermes wallet init                # ← the whole thing: provisions the signer and the wallet
```

`wallet init` does, in order:

1. probe the configured RPC; if nothing answers → download the official `monero-wallet-rpc` release,
   verify its SHA256 against the Monero project's signed hash list, extract **only** that binary into
   `<wallet home>/bin/`;
2. start it on `127.0.0.1:38084` against the first healthy public node, with the wallet directory
   inside the plugin's own home;
3. generate a wallet-file password into `<wallet home>/wallet.env` (0600, never printed);
4. create the wallet *inside* the signer and return only the address.

Idempotent — run it again and it opens the existing wallet (`created: false`).

## What the plugin needs from the machine

| Requirement | Why | Provided by |
|---|---|---|
| Python + the Hermes runtime | the plugin itself | you already have it |
| ~120 MB in the wallet home | signer binary + wallet file | `wallet init` |
| outbound HTTPS | download the binary once, reach a public node | your network |

That is the whole list: no chain data (a public node serves it), no root, no published ports, no
containers.

## Where things live

```
<wallet home>/             default ~/.hermes/wallet (or $HERMES_HOME/wallet)
├── keystore.json          encrypted (scrypt + XChaCha20-Poly1305)
├── config.json            signed settings (caps, chains, node URL)
├── contacts.json          the address book
├── sandboxes.json         sub-wallet budgets
├── ledger.jsonl(.pub)     receipt chain + public key
├── wallet.env             signer password, 0600
├── bin/monero-wallet-rpc  the signer binary (downloaded + verified)
└── monero/agent-wallet    the wallet file, created by the signer
```

## Operations

```bash
hermes wallet signer status      # installed / reachable / height / owned
hermes wallet signer stop        # stop it (walletd owns the process)
hermes wallet signer start       # start it on loopback against a registry node
hermes wallet chains             # what is live right now
hermes wallet backup             # one encrypted archive of everything above
hermes wallet seed --confirm     # 25 words: operator + passphrase + explicit confirm
```

## Moving machines

`hermes wallet backup` writes one sealed archive; `hermes wallet restore --archive …` puts it back
(refusing to overwrite). The Monero wallet itself comes from `wallet seed` inside the signer — the
archive never carries key material in the clear.

## Providers

`walletd/chains/nodes.py` is the endpoint registry, probed at runtime with skip-on-failure:

| Chain | Endpoints |
|---|---|
| Monero mainnet | cakewallet, stackwallet |
| Monero stagenet / testnet | rino community nodes |
| Bitcoin mainnet / testnet | mempool.space, blockstream |
| Base / Base Sepolia | base.org, publicnode |
| Polygon / Arbitrum | polygon-rpc.com, arb1.arbitrum.io |

`hermes wallet nodes` shows what is healthy now; the Settings page lists the same and lets you point
the signer at a different one. Bitcoin and EVM have endpoints but no signing adapter yet — they
report unavailable rather than pretending.
