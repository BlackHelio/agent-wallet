#!/usr/bin/env node
/**
 * ui-contract.mjs — what the pane must and must not say, in each state it can be in.
 *
 * Every check here is a screen the operator actually got, and each one was wrong once:
 *
 *   1. A loaded wallet drew an 'Orders / receiving' section with a count, over rows that were never
 *      written to. The wallet's job is what it holds and what it is about to pay; receiving is a
 *      fresh address on request (the Receive card), not a list that lives on the page.
 *   2. An Assets row read 'BTC · Bitcoin' and then 'Bitcoin · electrum · …' on the next line: the
 *      coin's name, twice. The second line names the network it is on, so the pair reads
 *      'BTC · Bitcoin' / 'Bitcoin mainnet · electrum …'.
 *   3. A locked wallet drew the whole dashboard under a banner — balances, pending cards, budgets.
 *      Anyone at the screen could read how much was there and the PIN looked optional. A locked
 *      pane is the gate and nothing else.
 *   4. A first run drew the same empty dashboard next to 'create your wallet', which reads as a
 *      wallet that is already half-broken. A first run is the setup card alone.
 *
 * Renders the page in Node against chosen daemon answers (see ui-render.mjs). No daemon, no network,
 * no browser.
 *
 *     node desktop/ui-contract.mjs [desktop/plugin.js]
 */
import { readFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { loadPlugin, renderRoute } from './ui-render.mjs'

const here = dirname(fileURLToPath(import.meta.url))
const pluginPath = resolve(process.argv[2] || join(here, 'plugin.js'))

const failures = []
let passed = 0
const check = (name, ok, detail = '') => {
  if (ok) { passed += 1; console.log(`  PASS  ${name}`) }
  else { failures.push(name); console.log(`  FAIL  ${name}${detail ? `   ${detail}` : ''}`) }
}
/** Fails when any text node contains `needle`. */
const absent = (screen, needle, name) => {
  const hits = screen.texts.filter((t) => t.toLowerCase().includes(needle.toLowerCase()))
  check(name, hits.length === 0, hits.length ? `found in: ${JSON.stringify(hits.slice(0, 3))}` : '')
}
/** Fails unless some text node contains `needle`. */
const present = (screen, needle, name) => {
  const hits = screen.texts.filter((t) => t.toLowerCase().includes(needle.toLowerCase()))
  check(name, hits.length > 0)
}

// A funded wallet, unlocked, with one of every row the page can draw.
const LOADED = {
  '/status': { ok: true, running: true, wallet: {
    locked: false, initialized: true, human_gate: true, config_signed: true, config_tampered: false,
    chains: ['bitcoin-mainnet', 'monero-mainnet', 'base-sepolia'],
    assets: ['BTC', 'XMR', 'USDC'],
    native_asset: { 'bitcoin-mainnet': 'BTC', 'monero-mainnet': 'XMR', 'base-sepolia': 'ETH' },
    chain_assets: { 'bitcoin-mainnet': ['BTC'], 'monero-mainnet': ['XMR'], 'base-sepolia': ['ETH', 'USDC'] },
    pending_requests: 1, active_mandates: 1,
    pin: { set: true, unlocks: true, required: true }
  } },
  '/balances': { accounts: [
    { kind: 'bitcoin-mainnet', asset: 'BTC', address: 'bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq',
      balance: '0.01234567', available: true, provider: 'electrum' },
    { kind: 'monero-mainnet', asset: 'XMR',
      address: '44AFFq5kSiGBoZ4NMDwYtN18obc8AemS33DBLWs3H7otXft3XjrpDtQGv7SqSsaBYBb98uNbr2VBBEt7f2wfn3RVGQBEP3A',
      balance: '1.234567890123', available: true, provider: 'cakewallet' },
    { kind: 'base-sepolia', asset: 'USDC', balance: '0.000000000000', available: false, error: 'rpc unreachable' }
  ] },
  '/requests': { requests: [
    { request_id: 'req_1', approval_id: 'apr_1', status: 'pending', amount: '0.05', asset: 'XMR',
      kind: 'monero-mainnet', payee: '44AFFq5kSiGBoZ4NMDwYtN18obc8AemS33DBLWs3H7otXft3XjrpDtQGv7SqSsaBYBb98uNbr2VBBEt7f2wfn3RVGQBEP3A',
      why: 'buy an image', memo: 'test' }
  ] },
  '/mandates': { mandates: [
    { mandate_id: 'mnd_1', asset: 'XMR', label: 'vendor', status: 'active', per_tx_cap: '0.02',
      period_cap: '0.1', absolute_cap: '0.3', period_hours: 24,
      payee: '44AFFq5kSiGBoZ4NMDwYtN18obc8AemS33DBLWs3H7otXft3XjrpDtQGv7SqSsaBYBb98uNbr2VBBEt7f2wfn3RVGQBEP3A',
      expires_at: '2026-12-01T00:00:00Z' }
  ] },
  '/sandboxes': { sandboxes: [
    { sandbox_id: 'sbx_1', kind: 'monero-mainnet', asset: 'XMR', budget: '0.1', spent: '0.02',
      status: 'active', label: 'image job', expires_at: '2026-12-01T00:00:00Z', profiles: [] }
  ], profiles: ['default'] },
  '/contacts': { contacts: [] },
  '/ledger?limit=40': { entries: [
    { seq: 3, kind: 'spend_request', decision: 'approved', ts: '2026-11-11T12:00:00Z',
      details: { asset: 'XMR', amount: '0.05', kind: 'monero-mainnet',
                 payee: '4ABeR5e9JgvCEj9xbNw9py1KjBHhyVEmri3apehMS4chBiVTF6BXNY7iZxL6cjn2mPUwLmBmS4DJL6s3bZyQS1ssFUw6MzR',
                 tx_hash: 'aa'.repeat(32), tx_key: 'bb'.repeat(32) } }
  ] },
  '/profiles': { profiles: ['default', 'wallet', 'work'], current: 'default' },
  '/spend': { assets: { XMR: { spent: '0.05', received: '0', cap: '1' } } },
  // The section that must not exist. The old pane drew 'Orders · receiving (N open)' from this route;
  // a bundle that still does is caught here rather than on someone's screen.
  '/incoming': { orders: [
    { order_id: 'ord_legacy_monero-mainnet_1', status: 'waiting', kind: 'monero-mainnet', asset: 'XMR',
      address: '44AFFq5kSiGBoZ4NMDwYtN18obc8AemS33DBLWs3H7otXft3XjrpDtQGv7SqSsaBYBb98uNbr2VBBEt7f2wfn3RVGQBEP3A',
      expected: '0.1', received: '0', tx_hash: 'cc'.repeat(32) }
  ] },
  '/deposits': { deposits: [
    { address: '4AdUndXHHZ6cfufTMvppY6JwXNouMBzSkbLYfpAV5Usx3skxNgYeYTRj5UA3jBjYx1T9fWLDDCsCbvKz1TMdA4XJufzJ1e3', label: 'old', status: 'unused' }
  ] }
}

const LOCKED = {
  '/status': { ok: true, running: true, wallet: {
    locked: true, initialized: true, human_gate: true, config_signed: true, config_tampered: false,
    chains: ['monero-mainnet'], assets: ['XMR'],
    // Money is there and the daemon can see it is there — the whole point of the check is that the
    // page must not draw it.
    pending_requests: 1, active_mandates: 1,
    pin: { set: true, unlocks: true, required: true }
  } },
  '/balances': { accounts: [
    { kind: 'monero-mainnet', asset: 'XMR',
      address: '44AFFq5kSiGBoZ4NMDwYtN18obc8AemS33DBLWs3H7otXft3XjrpDtQGv7SqSsaBYBb98uNbr2VBBEt7f2wfn3RVGQBEP3A',
      balance: '1.234567890123', available: false, state: 'locked' }
  ] },
  '/requests': { requests: [
    { request_id: 'req_1', status: 'pending', amount: '0.05', asset: 'XMR', kind: 'monero-mainnet',
      payee: '44AFFq5kSiGBoZ4NMDwYtN18obc8AemS33DBLWs3H7otXft3XjrpDtQGv7SqSsaBYBb98uNbr2VBBEt7f2wfn3RVGQBEP3A', why: 'buy' }
  ] },
  '/incoming': { orders: [
    { order_id: 'ord_legacy_monero-mainnet_2', status: 'waiting', kind: 'monero-mainnet', asset: 'XMR',
      address: '44AFFq5kSiGBoZ4NMDwYtN18obc8AemS33DBLWs3H7otXft3XjrpDtQGv7SqSsaBYBb98uNbr2VBBEt7f2wfn3RVGQBEP3A',
      expected: '0.1', received: '0', tx_hash: 'cc'.repeat(32) }
  ] },
  '/sandboxes': { sandboxes: [{ sandbox_id: 'sbx_1', account_index: 1, asset: 'XMR', budget: '0.1', spent: '0.02', status: 'active', label: 'job' }] }
}

const FIRST_RUN = {
  '/status': { ok: true, running: true, wallet: {
    locked: true, initialized: false, human_gate: true, config_signed: true, config_tampered: false,
    chains: [], assets: []
  } },
  '/balances': { accounts: [] },
  '/requests': { requests: [] },
  '/sandboxes': { sandboxes: [] }
}

const loaded = await loadPlugin(pluginPath)

/* 1 ── a loaded wallet draws what it holds, and nothing about orders or receiving ─────────────── */
console.log('== loaded wallet')
const main = await renderRoute(loaded, LOADED)
check('the page rendered (more than a stylesheet and a title)', main.texts.length > 20,
      `${main.texts.length} text nodes`)
absent(main, 'order', 'no "Orders" section, no order count, no order row')
absent(main, 'receiving', 'no "receiving" section (addresses arrive from the Receive card, on request)')
absent(main, 'deposit addresses', 'no deposit-address list on the page')
present(main, 'Assets', 'the Assets section is there')
// The count that was wrong was a section title carrying a list length ("Orders (11)"). A section
// title never carries a count of rows — only "(N active)", which is a fact about state.
const sectionTitles = main.elements
  .filter((n) => n.type === 'section')
  .map((n) => n.props && n.props.children && n.props.children[0])
  .filter((c) => typeof c === 'string')
const counted = sectionTitles.filter((t) => /\(\s*\d+\s*\)/.test(t) && !/\bactive\)/.test(t))
check('no section title counts rows', counted.length === 0, JSON.stringify(counted))

/* 2 ── an Assets row names the coin once, and the network under it ───────────────────────────── */
console.log('== Assets rows')
// A row is: head ('BTC · Bitcoin'), balance ('0.012346 BTC'), then the line that says where the coin
// is — '<network> · <provider> · <address state>'. The network cell used to be the head's own name,
// so every row read 'BTC · Bitcoin' and then 'Bitcoin · electrum …'.
const ASSETS = main.elements.filter((n) => n.type === 'div'
  && typeof (n.props && n.props.className) === 'string'
  && n.props.className.includes('rounded-xl border-[3px]'))
check('the Assets rows rendered', ASSETS.length >= 2, `${ASSETS.length} row(s)`)
// One name per row: the head is the ticker alone ('BTC'), the amount sits next to it, and the line
// under it names the *network* ('Bitcoin mainnet · electrum · address ready'). The row this
// replaced read 'BTC · Bitcoin' and then 'Bitcoin · electrum …' — the coin named twice.
const NETWORK_LABEL = { BTC: /^Bitcoin (mainnet|·)/, XMR: /^Monero (mainnet|·)/, ETH: /^Base /,
                        USDC: /^(Base|Polygon) /, POL: /^Polygon / }
const COIN_NAME = { BTC: 'Bitcoin', XMR: 'Monero', ETH: 'Ether', USDC: 'USD Coin', POL: 'Polygon' }
const rowTexts = (row) => {
  const out = []
  const walk = (n) => {
    if (n == null || typeof n !== 'object') { if (typeof n === 'string') out.push(n); return }
    if (Array.isArray(n)) { n.forEach(walk); return }
    walk(n.props && n.props.children)
  }
  walk(row.props && row.props.children)
  return out
}
// An Assets row is the one that prints a network label; the autopay and sandbox cards share the
// same card class, so the class alone is not enough to pick them out.
const isNetworkLine = (t) => Object.values(NETWORK_LABEL).some((re) => re.test(t))
let seenCoin = 0
for (const row of ASSETS) {
  const texts = rowTexts(row)
  const under = texts.find(isNetworkLine)
  if (!under) continue
  const ticker = texts.slice(0, 4).find((t) => Object.prototype.hasOwnProperty.call(NETWORK_LABEL, t))
  if (!ticker) continue
  seenCoin += 1
  const name = COIN_NAME[ticker]
  const repeats = texts.filter((t) => t === name).length
  check(`row ${ticker}: the coin is named once`, repeats === 0,
        `the name "${name}" appears ${repeats}× on top of the ticker`)
  check(`row ${ticker}: the line under it names the network`,
        NETWORK_LABEL[ticker].test(under), `under it: ${JSON.stringify(under)}`)
}
check('the funded coins were checked', seenCoin >= 2, `${seenCoin} coin row(s)`)
// Data-independent, both ways: the row subtitle has to come from the network table (not the
// coin-name table, which is what made 'BTC · Bitcoin' / 'Bitcoin …'), and every chain either table
// ships with has to have a label — and a test network has to say it is one.
const source = readFileSync(pluginPath, 'utf8')
const rowSubtitle = /(\w+)\((?:first|account)\.kind\)/.exec(source)
check('an Assets row subtitle is named by the network table',
      Boolean(rowSubtitle) && rowSubtitle[1] === 'networkName',
      rowSubtitle ? `it uses ${rowSubtitle[1]}(…kind)` : 'no asset-row subtitle found')
const tableOf = (name) => {
  const found = new RegExp(`const ${name} = \\{([\\s\\S]*?)\\n\\}`).exec(source)
  if (!found) return null
  return [...found[1].matchAll(/'([a-z0-9-]+)':\s*'([^']*)'/g)].map(([, id, label]) => ({ id, label }))
}
const names = tableOf('CHAIN_NAME')
const networks = tableOf('CHAIN_NETWORK')
check('CHAIN_NAME and CHAIN_NETWORK are both in the bundle', Boolean(names && networks))
if (names && networks) {
  check('CHAIN_NAME has entries', names.length >= 10, `${names.length}`)
  const blank = [...names, ...networks].filter((e) => !e.label.trim()).map((e) => e.id)
  check('every chain has a label', blank.length === 0, JSON.stringify(blank))
  // Every chain the balance tables ship with gets a network label, or its row falls back to the raw
  // chain id ('bitcoin-mainnet') — the complaint the chain-name table was introduced to stop.
  const chains = [...new Set(['MANDATE_ASSET', 'CHAIN_ASSETS'].flatMap((table) =>
    [...(tableOf(table) || [])]))]
  const labelled = new Set(networks.map((e) => e.id))
  const missing = chains.filter(({ id }) => !labelled.has(id)).map(({ id }) => id)
  check('every chain that can hold a balance has a network label',
        chains.length >= 10 && missing.length === 0, `${chains.length} chains, missing ${JSON.stringify(missing)}`)
  const testnet = (e) => /test|sepolia|amoy|stagenet/.test(e.id)
  const unmarked = networks.filter((e) => testnet(e) && !/test|sepolia|amoy|stagenet/i.test(e.label))
  check('a test network says it is one', unmarked.length === 0, JSON.stringify(unmarked.map((e) => e.id)))
  // The defect this table was introduced for: a mainnet label that is just the coin's name, which
  // the row's head already says.
  const coinOf = (id) => ({ 'monero-mainnet': 'Monero', 'bitcoin-mainnet': 'Bitcoin', 'base-mainnet': 'Base',
                            'polygon-mainnet': 'Polygon', 'arbitrum-mainnet': 'Arbitrum',
                            'xrp-mainnet': 'XRP' }[id])
  const repeats = networks.filter((e) => coinOf(e.id) && e.label.trim() === coinOf(e.id))
  const bare = networks.filter((e) => !/mainnet|testnet|stagenet|test|sepolia|amoy|simulator/i.test(e.label))
  check('a network label names a network, not just a coin name', repeats.length + bare.length === 0,
        JSON.stringify([...repeats, ...bare].map((e) => `${e.id} -> "${e.label}"`)))
}

/* 3 ── a locked wallet is the gate, and only the gate ────────────────────────────────────────── */
console.log('== locked wallet')
const gate = await renderRoute(loaded, LOCKED)
present(gate, 'Enter your PIN', 'the gate asks for the PIN')
check('the gate has a single field and a single button',
      gate.tags.filter((t) => t === 'input').length === 1 && gate.tags.filter((t) => t === 'button').length <= 2,
      `${gate.tags.filter((t) => t === 'input').length} input(s), ${gate.tags.filter((t) => t === 'button').length} button(s)`)
absent(gate, 'Assets', 'no Assets section')
absent(gate, 'Pending approvals', 'no Pending approvals section')
absent(gate, 'Sandbox', 'no Sandbox section')
absent(gate, 'Auto-pay', 'no Auto-pay section')
absent(gate, 'New request', 'no New request form')
absent(gate, 'Receipt', 'no Receipts section')
absent(gate, '1.23', 'no balance is drawn')
absent(gate, '0.05', 'no pending amount is drawn')
absent(gate, '44AFFq5k', 'no address is drawn')
absent(gate, 'wallet-pane flex h-full w-full flex-col gap-4', 'not the dashboard layout')
check('the gate is short — a handful of lines, not a page',
      gate.texts.filter((t) => t.length > 1).length <= 8, `${gate.texts.length} text nodes`)

/* 4 ── a first run is the setup card, and only the setup card ─────────────────────────────────── */
console.log('== first run')
const firstRun = await renderRoute(loaded, FIRST_RUN)
present(firstRun, 'Set up your agent wallet', 'the setup card is there')
present(firstRun, 'Create wallet', 'it has the create action')
absent(firstRun, 'Assets', 'no Assets section')
absent(firstRun, 'Pending approvals', 'no Pending approvals section')
absent(firstRun, 'Sandbox', 'no Sandbox section')
absent(firstRun, 'Auto-pay', 'no Auto-pay section')
absent(firstRun, 'New request', 'no New request form')
absent(firstRun, 'Receipt', 'no Receipts section')
absent(firstRun, 'Enter your PIN', 'not the unlock gate — there is no keystore to open')
absent(firstRun, 'order', 'nothing about orders')
check('the setup card is short — a handful of lines, not a page',
      firstRun.texts.filter((t) => t.length > 1).length <= 10, `${firstRun.texts.length} text nodes`)
// The gate and the setup card share the login layout; neither may reach the dashboard.
for (const [name, screen] of [['locked', gate], ['first run', firstRun]]) {
  check(`${name}: no section elements at all`,
        screen.elements.filter((n) => n.type === 'section').length === 0,
        `${screen.elements.filter((n) => n.type === 'section').length} section(s)`)
}

loaded.cleanup()

console.log()
if (failures.length) {
  console.error(`ui-contract: ${failures.length} FAILED, ${passed} passed`)
  for (const f of failures) console.error(`  - ${f}`)
  process.exit(1)
}
console.log(`ui-contract: OK — ${passed} checks passed (loaded, locked, first run)`)
