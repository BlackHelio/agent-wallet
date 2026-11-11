/**
 * Render the agent-wallet pane in a real browser, with the app's real compiled CSS, so layout can be
 * measured at real sizes and in real themes.
 *
 * The plugin is copied with its two bare imports rewritten to a local SDK stub and to the react in
 * node_modules — the same trick render-check.mjs uses, so nothing about the plugin itself changes.
 */
import { copyFileSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const pluginSrc = resolve(process.argv[2] || join(here, '..', '..', 'desktop', 'plugin.js'))
const out = join(here, 'page')
mkdirSync(out, { recursive: true })

const patched = readFileSync(pluginSrc, 'utf8')
  .replace("from '@hermes/plugin-sdk'", "from './sdk.mjs'")
  .replace("from 'react/jsx-runtime'", "from 'react/jsx-runtime'")
  .replace("from 'react'", "from 'react'")
writeFileSync(join(out, 'plugin.mjs'), patched)
copyFileSync(join(here, 'sdk.mjs'), join(out, 'sdk.mjs'))

// ---- fixtures: one per state we care about, shaped like the daemon's answers -------------------
const ADDR_XMR = '44AFFq5kSiGBoZ4NMDwYtN18obc8AemS33DBLWs3H7otXft3XjrpDtQGv7SqSsaBYBb98uNbr2VBBEt7f2wfn3RVGQBEP3A'
const ADDR_ETH = '0x0000000000000000000000000000000000000001'   // fixture only, never a real wallet

const fixtures = {
  locked: {
    '/status': { ok: true, running: true, wallet: { locked: true, human_gate: true, config_signed: true,
      config_tampered: false, chains: ['monero-mainnet', 'base-mainnet'], assets: ['XMR', 'ETH', 'USDC'],
      pending_requests: 0, active_mandates: 0,
      // what `pin_status()` answers while locked: `unlocks` is whether the six digits open the file
      pin: { set: true, unlocks: true, required: true, attempts_left: null } } },
    '/balances': { accounts: [] },
    '/requests': { requests: [] },
    '/mandates': { mandates: [] },
    '/ledger?limit=40': { entries: [] },
    '/profiles': { profiles: ['default'], current: 'default' },
    '/config': { settings: { human_gate: true, per_tx_cap: { XMR: '0.5' }, daily_cap: { XMR: '1' },
      approval_threshold: '0.02', chains_enabled: ['monero-mainnet', 'base-mainnet'] } }
  },
  loaded: {
    '/status': { ok: true, running: true, wallet: { locked: false, human_gate: true, config_signed: true,
      config_tampered: false, chains: ['monero-mainnet', 'bitcoin-mainnet', 'base-mainnet'],
      assets: ['XMR', 'BTC', 'ETH', 'USDC'], pending_requests: 1, active_mandates: 1,
      pin: { set: true, updated_at: '2026-11-11T09:00:00Z' },
      ledger_pubkey: 'ab'.repeat(32) } },
    '/balances': { accounts: [
      { kind: 'monero-mainnet', asset: 'XMR', address: ADDR_XMR, balance: '1.234567890123',
        available: true, provider: 'cakewallet' },
      { kind: 'base-mainnet', asset: 'ETH', address: ADDR_ETH, balance: '0.000193371',
        available: true, provider: 'base.publicnode.com' },
      { kind: 'base-mainnet', asset: 'USDC', address: ADDR_ETH, balance: '0.000000000000',
        available: true, provider: 'base.publicnode.com' },
      { kind: 'bitcoin-mainnet', asset: 'BTC', address: 'bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4',
        balance: '0.000000000000', available: false, error: 'esplora unreachable' }
    ] },
    '/requests': { requests: [
      { request_id: 'req_1', approval_id: 'apr_1', status: 'pending', amount: '0.05', asset: 'XMR',
        kind: 'monero-mainnet', payee: ADDR_XMR, why: 'buy an image', memo: 'test',
        confirm_amount_required: true,
        fiat: { amount: '28.35', currency: 'USD' },
        gas: { native: 'ETH', after: '0.000140883', floor: '0.00000312' },
        swap: { asset_out: 'USDC', quoted_out: '0.102609', min_out: '0.102095955',
                label: 'BaseSwap V2', operator: 'baseswap-v2', slippage_bps: 50 } },
      { request_id: 'req_2', approval_id: 'apr_2', status: 'pending', amount: '0.00005', asset: 'ETH',
        kind: 'base-mainnet', payee: '0x327Df1E6de05895d2ab08513aaDD9313Fe505d86',
        why: 'swap for gas', memo: '',
        swap: { asset_out: 'USDC', quoted_out: '0.102609', min_out: '0.102095955',
                label: 'BaseSwap V2', operator: 'baseswap-v2', slippage_bps: 50 },
        gas: { native: 'ETH', after: '0.000140883', floor: '0.00000312' } }
    ] },
    '/mandates': { mandates: [
      { mandate_id: 'mnd_1', asset: 'XMR', label: 'vendor', status: 'active', per_tx_cap: '0.02',
        period_cap: '0.1', absolute_cap: '0.3', period_hours: 24, payee: ADDR_XMR,
        expires_at: '2026-11-12T00:00:00Z' } ] },
    '/ledger?limit=40': { entries: [
      { seq: 3, kind: 'spend_request', decision: 'approved', ts: new Date().toISOString(),
        details: { asset: 'XMR', amount: '0.05', kind: 'monero-mainnet', payee: ADDR_XMR,
                   tx_hash: 'aa'.repeat(32) } },
      { seq: 4, kind: 'swap', decision: 'approved', ts: new Date().toISOString(),
        details: { asset: 'ETH', amount: '0.00005', asset_out: 'USDC', quoted_out: '0.102609',
                   tx_hash: 'bb'.repeat(32), approve_hash: 'cc'.repeat(32) } } ] },
    '/profiles': { profiles: ['default', 'wallet', 'work'], current: 'default' },
    '/config': { settings: { human_gate: true, per_tx_cap: { XMR: '0.5' }, daily_cap: { XMR: '1' },
      approval_threshold: '0.02', chains_enabled: ['monero-mainnet', 'base-mainnet'],
      swaps_enabled: true, suggest_swaps: true, swap_slippage_bps: 50,
      monero_rpc_url: 'http://127.0.0.1:38084/json_rpc' } }
  }
}
writeFileSync(join(out, 'fixtures.json'), JSON.stringify(fixtures))

const harness = `import { createRoot } from 'react-dom/client'
import { createElement } from 'react'
import plugin from './plugin.mjs'

const state = new URLSearchParams(location.search).get('state') || 'loaded'
const fixtures = await (await fetch('./fixtures.json')).json()
const data = fixtures[state] || fixtures.loaded

const contributions = []
const ctx = {
  source: 'plugin:agent-wallet',
  register: (c) => { contributions.push(c); return () => {} },
  registerMany: (cs) => { cs.forEach((c) => contributions.push(c)); return () => {} },
  registerSettingsPage: () => () => {},
  rest: async (path, opts) => {
    window.__calls = window.__calls || []
    window.__calls.push({ path, method: (opts && opts.method) || 'GET', body: (opts && opts.body) || null })
    const hit = Object.keys(data).find((k) => k === path || k.split('?')[0] === path.split('?')[0])
    await new Promise((r) => setTimeout(r, 15))
    if (opts && opts.method && opts.method !== 'GET') return { ok: true, decision: { allow: true } }
    if (hit) return structuredClone(data[hit])
    throw new Error('404 ' + path)
  },
  socket: () => () => {},
  onEvent: () => () => {},
  onDispose: () => {},
  setTimeout: (fn, ms) => setTimeout(fn, ms),
  setInterval: (fn, ms) => setInterval(fn, ms),
  addEventListener: (t, fn) => { window.addEventListener(t, fn); return () => window.removeEventListener(t, fn) },
  storage: { get: () => undefined, set: () => {}, remove: () => {} },
  os: {}, notify: () => {}, pet: {}, preview: {}
}

plugin.register(ctx)
const page = contributions.find((c) => typeof c.render === 'function')
if (!page) throw new Error('no renderable contribution')
const root = createRoot(document.getElementById('root'))
root.render(createElement(page.render))
// data settles after two fetches; give it a beat, then let the audit measure a stable frame
setTimeout(() => { window.__ready = true }, 600)
`
writeFileSync(join(out, 'harness.mjs'), harness)

// The app's own compiled CSS is what the pane is really rendered against — utilities and tokens.
// The pane is rendered against the app's own compiled CSS — utilities and tokens — so a class that
// the host's build never emitted shows up here as a missing style, not as a surprise on macOS.
const appCssPath = process.env.HERMES_APP_CSS
  || '/opt/hermes/apps/desktop/dist-webapp/assets/main-D01CRUNp.css'
const appCss = readFileSync(appCssPath, 'utf8')
writeFileSync(join(out, 'app.css'), appCss)

const html = `<!doctype html>
<html><head><meta charset="utf-8">
<link rel="stylesheet" href="./app.css">
<style>
  html, body { margin: 0; padding: 0; }
  body { background: var(--ui-bg-primary, #14161a); color: var(--ui-text-primary, #eef1f6);
         font: 14px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
  /* the pane is mounted where the app puts a route: a scroll container, full width */
  #root { width: 100%; min-height: 100vh; box-sizing: border-box; }
  #root * { box-sizing: border-box; }
</style>
</head>
<body><div id="root"></div>
<script>window.process = { env: { NODE_ENV: "production" } };</script>
<script type="module" src="./bundle.js"></script>
</body></html>`
writeFileSync(join(out, 'index.html'), html)
console.log('wrote', out)
