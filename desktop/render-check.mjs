/**
 * Render harness: executes a desktop plugin's page component in Node with a stubbed SDK + React.
 * Catches the class of bug that only shows up at render time (TDZ, bad hook use, undefined refs),
 * which the app otherwise reports as "failed to load".
 */
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'

const pluginPath = process.argv[2]
if (!pluginPath) {
  console.error('usage: node render-check.mjs <plugin.js>')
  process.exit(2)
}

// The plugin imports the app's SDK and react, which do not exist outside the app. Rather than ship a
// node_modules stub, copy the source with those three imports rewritten to local stubs — so this
// runs from a fresh clone with nothing installed.
const SDK_STUB = `export const PALETTE_AREA = 'palette'
export const ROUTES_AREA = 'routes'
export const SIDEBAR_NAV_AREA = 'sidebar.nav'
export const STATUSBAR_AREAS = { left: 'statusBar.left', right: 'statusBar.right' }
export const TITLEBAR_AREAS = { left: 'titleBar.left', center: 'titleBar.center', right: 'titleBar.right' }
export const host = { notify: () => {}, navigate: () => {}, request: async () => ({}), state: { gateway: { get: () => 'open' } } }
export const useValue = (atom) => (atom && typeof atom.get === 'function' ? atom.get() : atom)
`

const REACT_STUB = `export function createElement(type, props, ...children) {
  const all = children.length ? children : (props && props.children !== undefined ? [props.children] : [])
  const merged = { ...(props || {}), children: all.length === 1 ? all[0] : all }
  if (typeof type === 'function') return type(merged)
  return { type, props: merged }
}
let slots = []
export function useState(v) { return [typeof v === 'function' ? v() : v, () => {}] }
export function useEffect() {}
export function useCallback(fn) { return fn }
export function useMemo(fn) { return fn() }
export function useRef(v) { return { current: v } }
export default { createElement }
`

const dir = mkdtempSync(join(tmpdir(), 'hermes-render-'))
writeFileSync(join(dir, 'sdk.mjs'), SDK_STUB)
writeFileSync(join(dir, 'react.mjs'), REACT_STUB)
const patched = readFileSync(pluginPath, 'utf8')
  .replace("from '@hermes/plugin-sdk'", `from '${pathToFileURL(join(dir, 'sdk.mjs')).href}'`)
  .replace("from 'react/jsx-runtime'", `from '${pathToFileURL(join(dir, 'react.mjs')).href}'`)
  .replace("from 'react'", `from '${pathToFileURL(join(dir, 'react.mjs')).href}'`)
writeFileSync(join(dir, 'plugin.mjs'), patched)

const { default: plugin } = await import(pathToFileURL(join(dir, 'plugin.mjs')).href)

const contributions = []
const settingsPages = []

const fakeResponses = {
  '/status': { ok: true, running: true, wallet: { locked: false, human_gate: true, config_signed: true, config_tampered: false, chains: ['monero-mainnet', 'base-sepolia'], assets: ['XMR', 'ETH', 'USDC'], pending_requests: 1, active_mandates: 1, ledger_pubkey: 'ab'.repeat(32),
    prompt: { kind: 'approval', request_id: 'req_1', message: 'Approve 0.05 XMR' } } },
  '/balances': { accounts: [
    { kind: 'monero-mainnet', asset: 'XMR', address: '43s4SdoS9vwGuVtaQPU6ik2kpLpZT1LNnDQMvUCQzjDYWcDcmXDhHBtT8hJJuwDWccRkikymJN36dZLiFf7ejoxH72sHNXN', balance: '1.234567890123', available: true, provider: 'cakewallet' },
    { kind: 'base-sepolia', asset: 'USDC', balance: '0.000000000000', available: false, error: 'rpc unreachable' }
  ] },
  '/requests': { requests: [
    { request_id: 'req_1', approval_id: 'apr_1', status: 'pending', amount: '0.05', asset: 'XMR', kind: 'monero-mainnet', payee: '43s4SdoS9vwGuVtaQPU6ik2kpLpZT1LNnDQMvUCQzjDYWcDcmXDhHBtT8hJJuwDWccRkikymJN36dZLiFf7ejoxH72sHNXN', why: 'buy an image', memo: 'test' }
  ] },
  '/mandates': { mandates: [
    { mandate_id: 'mnd_1', asset: 'XMR', label: 'vendor', status: 'active', per_tx_cap: '0.02', period_cap: '0.1', absolute_cap: '0.3', period_hours: 24, payee: '43s4SdoS9vwGuVtaQPU6ik2kpLpZT1LNnDQMvUCQzjDYWcDcmXDhHBtT8hJJuwDWccRkikymJN36dZLiFf7ejoxH72sHNXN', expires_at: '2026-11-07T00:00:00Z' }
  ] },
  '/ledger?limit=40': { entries: [
    { seq: 3, kind: 'spend_request', decision: 'approved', ts: new Date().toISOString(),
      details: { asset: 'XMR', amount: '0.05', kind: 'monero-mainnet',
                 payee: '4ABeR5e9JgvCEj9xbNw9py1KjBHhyVEmri3apehMS4chBiVTF6BXNY7iZxL6cjn2mPUwLmBmS4DJL6s3bZyQS1ssFUw6MzR',
                 tx_hash: 'aa'.repeat(32), tx_key: 'bb'.repeat(32) } }
  ] },
  '/profiles': { profiles: ['default', 'wallet', 'work'], current: 'default' },
  '/config': { settings: { human_gate: true, per_tx_cap: { XMR: '0.5' }, daily_cap: { XMR: '1' }, approval_threshold: '0.02', chains_enabled: ['monero-mainnet'], monero_rpc_url: 'http://127.0.0.1:38084/json_rpc' } }
}

const ctx = {
  source: 'plugin:agent-wallet',
  register: (c) => { contributions.push(c); return () => {} },
  registerMany: (cs) => { cs.forEach((c) => contributions.push(c)); return () => {} },
  registerSettingsPage: (p) => { settingsPages.push(p); return () => {} },
  rest: async (path) => {
    if (path in fakeResponses) return fakeResponses[path]
    throw new Error('404 not found: ' + path)
  },
  socket: () => () => {},
  onEvent: () => () => {},
  onDispose: () => {},
  setTimeout: () => () => {},
  setInterval: () => () => {},
  addEventListener: () => () => {},
  storage: { get: () => undefined, set: () => {}, remove: () => {} },
  os: {},
  notify: () => {},
  pet: {},
  preview: {}
}

plugin.register(ctx)
console.log('contributions:', contributions.map((c) => `${c.area || c.id}:${c.id}`).join(', '))

let rendered = 0
for (const contribution of contributions) {
  if (typeof contribution.render !== 'function') continue
  const tree = contribution.render()
  rendered += 1
  // Count text nodes so a blank render is visible rather than silent.
  let texts = 0
  const walk = (node) => {
    if (node == null) return
    if (Array.isArray(node)) return node.forEach(walk)
    if (typeof node === 'string' || typeof node === 'number') { texts += 1; return }
    if (typeof node === 'object') walk(node.props && node.props.children)
  }
  walk(tree)
  // Structural guard: a section header holds a title and, at most, pills of status. Controls that
  // need space (buttons, selects, inputs) belong in the body — one of them sat in the header's
  // right-hand slot and the layout squeezed it into an unreadable blob.
  if (contribution.id === 'wallet-route') {
    const headers = []
    const findHeaders = (node) => {
      if (node == null || typeof node !== 'object') return
      if (Array.isArray(node)) { node.forEach(findHeaders); return }
      if (node.type === 'section') {
        const kids = (node.props && node.props.children) || []
        if (Array.isArray(kids) && kids[0]) headers.push(kids[0])
      }
      findHeaders(node.props && node.props.children)
    }
    findHeaders(tree)
    // Data-independent version of the same rule: in the source, `section(title, right, body)` must
    // pass null for the header slot. The rendered check above cannot see a control that only exists
    // once state has loaded, and that was exactly this bug.
    const source = readFileSync(pluginPath, 'utf8')
    const headerArgs = [...source.matchAll(/section\(\s*(?:'[^']*'|`[^`]*`)\s*,\s*([^,\n]+)/g)]
      .map((m) => m[1].trim())
      .filter((arg) => arg !== 'null')
    if (headerArgs.length) {
      console.error(`FAIL: ${headerArgs.length} section() call(s) put something in the header slot: `
        + `[${headerArgs.slice(0, 3).join(' | ')}] — controls there get squished`)
      process.exitCode = 1
    }
    const controls = new Set(['button', 'select', 'input', 'textarea'])
    let bad = 0
    const scan = (node) => {
      if (node == null || typeof node !== 'object') return
      if (Array.isArray(node)) { node.forEach(scan); return }
      if (controls.has(node.type)) bad += 1
      scan(node.props && node.props.children)
    }
    headers.forEach(scan)
    if (bad) {
      console.error(`FAIL: ${bad} control(s) sit in a section header — they get squished there`)
      process.exitCode = 1
    } else {
      console.log(`  section headers: ${headers.length} checked, no controls in any of them`)
    }
  }
  // A <select> whose popup has no background reads white-on-white in a dark theme.
  let selects = 0
  let unstyled = 0
  const styleWalk = (node) => {
    if (node == null) return
    if (Array.isArray(node)) return node.forEach(styleWalk)
    if (typeof node !== 'object') return
    if (node.type === 'select' || node.type === 'option') {
      selects += 1
      const style = node.props && node.props.style
      if (!style || !style.backgroundColor) unstyled += 1
    }
    styleWalk(node.props && node.props.children)
  }
  styleWalk(tree)
  if (selects) {
    console.log(`  ${selects} select/option node(s), ${unstyled} without a background`)
    if (unstyled) { console.error('FAIL: a select popup would render white-on-white'); process.exit(1) }
  }
  console.log(`rendered ${contribution.id}: ${texts} text nodes`)
}
for (const page of settingsPages) {
  const tree = page.render()
  let selects = 0
  let unstyled = 0
  const styleWalk = (node) => {
    if (node == null) return
    if (Array.isArray(node)) return node.forEach(styleWalk)
    if (typeof node !== 'object') return
    if (node.type === 'select' || node.type === 'option') {
      selects += 1
      const style = node.props && node.props.style
      if (!style || !style.backgroundColor) unstyled += 1
    }
    styleWalk(node.props && node.props.children)
  }
  styleWalk(tree)
  if (selects && unstyled) { console.error('FAIL: settings select without a background'); process.exit(1) }
  console.log(`rendered settings ${page.id}: ${tree ? 'ok' : 'null'}`)
}
console.log('render-check: OK')

/* ── profile chips ───────────────────────────────────────────────────────────
 * The sandbox ACL was a popover of checkboxes and, in the real pane, clicking a profile did
 * nothing. It is now a row of buttons, so the toggle is testable: render it, press a chip, look at
 * what onChange was handed. A control nobody can press is worse than no control at all.
 */
{
  const mod = await import(pathToFileURL(join(dir, 'plugin.mjs')).href)
  const picker = mod.ProfilePicker
  if (typeof picker !== 'function') {
    console.error('FAIL: ProfilePicker is not exported, the chip test cannot run')
    process.exit(1)
  }
  const press = (tree, label) => {
    const stack = [tree]
    while (stack.length) {
      const node = stack.pop()
      if (!node) continue
      if (Array.isArray(node)) { stack.push(...node); continue }
      if (typeof node !== 'object' || !node.props) continue
      if (node.type === 'button' && node.props.children === label) return node.props.onClick
      stack.push(node.props.children)
    }
    return null
  }
  let seen = null
  const tree = picker({ profiles: ['default', 'wallet', 'work'], selected: [], onChange: (next) => { seen = next } })
  press(tree, 'wallet')()
  const afterWallet = seen
  press(picker({ profiles: ['default', 'wallet', 'work'], selected: ['wallet'], onChange: (n) => { seen = n } }), 'wallet')()
  const afterSecondPress = seen
  press(picker({ profiles: ['default', 'wallet'], selected: ['wallet'], onChange: (n) => { seen = n } }), 'all profiles')()
  const afterClear = seen
  console.log('chips: press wallet ->', JSON.stringify(afterWallet),
              '| press again ->', JSON.stringify(afterSecondPress),
              '| all profiles ->', JSON.stringify(afterClear))
  if (JSON.stringify(afterWallet) !== '["wallet"]' || JSON.stringify(afterSecondPress) !== '[]'
      || JSON.stringify(afterClear) !== '[]') {
    console.error('FAIL: the profile chips do not toggle what they say they toggle')
    process.exit(1)
  }
  const empty = picker({ profiles: [], selected: [], onChange: () => {} })
  const flat = JSON.stringify(empty)
  if (!flat.includes('no profiles found')) {
    console.error('FAIL: with no profiles the picker says nothing')
    process.exit(1)
  }
  console.log('render-check: OK (profile chips toggle)')
}
