# Rails and standards

## The map (as of Nov 2026)

Agent payments split into three layers. Confusing them is how products end up claiming a "new
protocol" that is really one layer wearing a coat.

| Layer | Question | Who is there |
|---|---|---|
| **Identity** | Who is this agent, who authorized it | ERC-8004 (draft EIP; registries on many L2s), Skyfire KYA, Catena ACK-ID (DID+VC), World ID + AgentKit, Visa Trusted Agent Protocol |
| **Protocol** | How does it ask to pay | **x402** (HTTP 402), **MPP** (Tempo), **AP2** (Google, rail-agnostic, mandate-shaped), Stripe ACP, ACK-Pay |
| **Settlement** | Where the money moves | USDC on Base / Solana / Polygon / Arbitrum, card rails |

Reported traction for x402 alone: **165M+ payments, ~$50M cumulative agent-to-agent USDC volume
by April 2026**. The field guide's own conclusion, and the reason this project exists: the hardest
unsolved problem is not *can* the agent pay — it is *should* it, and the first major exploit will
be prompt injection in a tool the agent treats as authoritative.

## What Hermes already has (verified in `plugin-catalog`)

| Entry | Owner | What it is | Middleware |
|---|---|---|---|
| `pay`, `pay-cli` | solana-foundation (1.8k★, active) | x402 / MPP / AP2 client, hosted MCP or local CLI, `--sandbox` needs no funds | none |
| `hermes-mpp` | tempoxyz | Tempo 402 auto-pay, `TEMPO_PRIVATE_KEY` in env | none |
| `agentwalletapi` | community | custodial wallets, transfers with no confirmation prompt | none |
| `metamask-wallet` | community | keys in MetaMask, approval-gated, 11 tools | none |
| `vet402` | community | payee lookup before paying, **fails open** | none |

Every one declares `provides_middleware: []`, and none of them proves what was spent. That is the
hole this plugin fills — it is a *policy and evidence* layer, not a rail.

## Design consequences

1. **Do not build a rail.** Buy through x402/MPP via whatever client the user has. Our value is
   the approval, the cap, and the receipt around it.
2. **x402 is the sweet spot for us** because payment is an **EIP-3009
   `transferWithAuthorization`**: the wallet signs an off-chain authorization and a facilitator
   broadcasts it. No gas on the buyer side, no nonce race, no stuck transactions — just a
   signature that a human approved.
3. **Rail-agnostic interface, one implementation.** `walletd` exposes `request_tx` with a `kind`
   (chain id) field, and one policy engine covers every chain. Shipped adapters: Monero, Bitcoin,
   XRP (receive-only), Base/Polygon EVM (including Base Sepolia and Polygon Amoy) and the simulator.
   A new chain is an adapter, not a policy, ledger, IPC or UI change. No swap adapter shipped in
   this release.
4. **Swaps were cut, not faked.** A swap would be execution, and execution belongs behind the same
   approval gate and receipts. Since no swap adapter ships, there is no `request_swap` method or
   tool to claim otherwise; the agent can price an x402 purchase and request a payment.
5. **Identity is deferred, not faked.** ERC-8004 registration is a v0.2 feature behind a flag. We
   do not pretend a local keystore is an identity system; the receipt key is a start, not a
   passport.

## Chain choice

| Option | Why | Why not |
|---|---|---|
| **Base Sepolia** (chosen) | one signing path, USDC contract known, cheap faucet, x402 default rail, same code as Base mainnet | testnet only, needs a faucet on day 1 |
| Solana devnet | `pay --sandbox` lives there; Solana Foundation tooling is the strongest client | second chain, second signing lib, second tx model — a day we do not have |
| Local anvil | zero flake | not a real tx in the demo video, and the entry is a trust claim |

Decision belongs to Helio (§8 of PLAN.md). Default: Base Sepolia.

## Dependencies (catalog rule 10 aware)

| Need | Package | Bound |
|---|---|---|
| XChaCha20-Poly1305, Ed25519 | `pynacl` | `>=1.5,<2` |
| keystore KDF | stdlib `hashlib.scrypt` | — |
| EVM build/sign/broadcast | `eth-account` | `>=0.13,<0.14` |
| RPC/HTTP | stdlib `urllib` (no `requests`, no `httpx` in `walletd`) | — |

No vendored binaries, no compiled artifacts, no `.pyc` in the tree (rule 17). CI runs
`uv --exclude-newer` like the guide recommends.

## Explicit non-goals

- No token, no points, no airdrop, no "AI2" branding that implies a coin.
- No custody for other people, no escrow, no dispute resolution.
- No reputation system in v0.1 — `vet402` already exists and we would rather compose with it than
  reimplement it badly.
