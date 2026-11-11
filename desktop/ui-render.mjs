/**
 * Render a desktop plugin's page component in Node, against *chosen* daemon answers, and hand back
 * the text it actually put on screen.
 *
 * `render-check.mjs` runs the page against one fixed response map and only counts text nodes — which
 * catches a crash, not a wrong screen. The checks here need the opposite: the same page rendered as
 * a *locked* wallet, as a *first run*, as a funded one — because the three states used to paint the
 * same dashboard, and that is what the operator saw.
 *
 * Two harness rules make that possible without the app or a browser:
 *   - `useState` really holds state and `useEffect` really runs (the page fills its state from
 *     `ctx.rest` in an effect), so the tree can be read after the fetch settles.
 *   - react's `createElement` has the real signature, `createElement(type, props, ...children)`. The
 *     stub in render-check.mjs reads `props.children` instead, which is why it cannot see text the
 *     page passes as children arguments — and why it reported 414 text nodes for a page that says
 *     far more than that.
 */
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'

const SDK_STUB = `export const PALETTE_AREA = 'palette'
export const ROUTES_AREA = 'routes'
export const SIDEBAR_NAV_AREA = 'sidebar.nav'
export const STATUSBAR_AREAS = { left: 'statusBar.left', right: 'statusBar.right' }
export const TITLEBAR_AREAS = { left: 'titleBar.left', center: 'titleBar.center', right: 'titleBar.right' }
export const host = { notify: () => {}, navigate: () => {}, request: async () => ({}), state: { gateway: { get: () => 'open' } } }
export const useValue = (atom) => (atom && typeof atom.get === 'function' ? atom.get() : atom)
`

// Mirrors the app's runtime closely enough for a page component: hooks keep their state between
// renders, effects run after render (and their cleanups are dropped), and element creation follows
// React's own argument order.
const REACT_STUB = `const RENDER = globalThis.__render
const HOOKS = []
let cursor = 0
function hook(init) {
  const i = cursor++
  if (!(i in HOOKS)) HOOKS[i] = { value: typeof init === 'function' ? init() : init }
  return HOOKS[i]
}
RENDER.begin = () => { cursor = 0; RENDER.effects.length = 0 }
RENDER.renders = 0
export function createElement(type, props, ...children) {
  const flat = children.length ? children : (props && props.children !== undefined ? [props.children] : [])
  const merged = { ...(props || {}) }
  if (flat.length) merged.children = flat.length === 1 ? flat[0] : flat
  if (typeof type === 'function') return type(merged)
  return { type, props: merged }
}
export function useState(init) {
  const cell = hook(init)
  return [cell.value, (next) => { cell.value = typeof next === 'function' ? next(cell.value) : next }]
}
export function useRef(init) { return hook({ current: init }) }
export function useMemo(fn) { const cell = hook({}); if (!cell.done) { cell.value = fn(); cell.done = true } return cell.value }
export function useCallback(fn) { return fn }
export function useEffect(fn) { RENDER.effects.push(fn) }
export default { createElement }
`

const CONTEXT_STUB = `// The page calls ctx.storage.get() synchronously in an effect; the app's may be async. Either way the
// hidden-coin list is not what these checks are about.
export const storage = { get: () => undefined, set: () => {}, remove: () => {} }
`

const settle = async () => {
  for (let i = 0; i < 8; i += 1) await new Promise((resolve) => setImmediate(resolve))
}

/**
 * Import a plugin with the SDK and React stubbed. Returns the module; call `renderPage` to draw a
 * page from it.
 */
export async function loadPlugin(pluginPath) {
  const render = { begin: () => {}, effects: [], renders: 0 }
  globalThis.__render = render
  const dir = mkdtempSync(join(tmpdir(), 'hermes-ui-'))
  writeFileSync(join(dir, 'sdk.mjs'), SDK_STUB)
  writeFileSync(join(dir, 'react.mjs'), REACT_STUB)
  writeFileSync(join(dir, 'storage.mjs'), CONTEXT_STUB)
  const local = (name) => pathToFileURL(join(dir, name)).href
  const patched = readFileSync(pluginPath, 'utf8')
    .replace("from '@hermes/plugin-sdk'", `from '${local('sdk.mjs')}'`)
    .replace("from 'react/jsx-runtime'", `from '${local('react.mjs')}'`)
    .replace("from 'react'", `from '${local('react.mjs')}'`)
  writeFileSync(join(dir, 'plugin.mjs'), patched)
  const module = await import(local('plugin.mjs') + '?n=' + Math.random())
  return {
    module,
    render,
    cleanup: () => rmSync(dir, { recursive: true, force: true })
  }
}

/**
 * Draw the plugin's wallet route against `responses` (a map of REST path -> body, exactly what the
 * daemon would answer). Unknown paths throw, like the real bridge does.
 *
 * Returns the page's element tree and a `texts` list in document order. Give `treeOf` to get the
 * tree the page would draw on its next render, and `textsOf` to flatten any tree.
 */
export async function renderRoute(loaded, responses, options = {}) {
  const ctx = {
    source: 'plugin:agent-wallet',
    rest: async (path) => {
      if (path in responses) return structuredClone(responses[path])
      throw new Error('404 not found: ' + path)
    },
    socket: () => () => {},
    onEvent: () => () => {},
    onDispose: () => {},
    setTimeout: () => () => {},
    setInterval: () => () => {},
    addEventListener: () => () => {},
    storage: { get: () => options.storageGet ? options.storageGet() : undefined, set: () => {}, remove: () => {} },
    os: {},
    notify: () => {},
    pet: {},
    preview: {}
  }
  const contributions = []
  loaded.module.default.register({
    ...ctx,
    register: (c) => { contributions.push(c); return () => {} },
    registerMany: (cs) => { cs.forEach((c) => contributions.push(c)); return () => {} },
    registerSettingsPage: () => () => {}
  })
  const route = contributions.find((c) => c.id === 'wallet-route')
  if (!route) throw new Error('the plugin registered no wallet route')

  loaded.render.begin()
  let tree = route.render()
  loaded.render.renders = 1
  // Effects are queued per render; run them until the page stops queueing new work (the first effect
  // fetches /status, which sets state, which may queue more). A capped loop, because a page that
  // never settles is itself a finding.
  // Each pass: flush the queued effects, let their promises land, then draw again. The render is
  // unconditional — the page's effects queue more than one render (seed the selects, then the fetch
  // lands), and a pass that settled synchronously still leaves the tree one render behind.
  for (let pass = 0; pass < 6; pass += 1) {
    loaded.render.begin()
    tree = route.render()
    loaded.render.renders += 1
    if (!loaded.render.effects.length) break
    const effects = loaded.render.effects.slice()
    loaded.render.effects.length = 0
    for (const effect of effects) {
      const cleanup = effect()
      if (typeof cleanup === 'function' && options.runCleanup) cleanup()
    }
    await settle()
  }
  return makeScreen(tree)
}

/** Flatten a tree to the strings a person would read, in order, and the element types in order. */
export function makeScreen(tree) {
  const texts = []
  const tags = []
  const elements = []
  const walk = (node) => {
    if (node == null || node === false || node === true) return
    if (Array.isArray(node)) { node.forEach(walk); return }
    if (typeof node === 'string' || typeof node === 'number') { texts.push(String(node)); return }
    if (typeof node !== 'object') return
    elements.push(node)
    if (typeof node.type === 'string') tags.push(node.type)
    walk(node.props && node.props.children)
  }
  walk(tree)
  return { tree, texts, tags, elements, text: texts.join(' ') }
}

/** Every text node that contains `needle` (case-insensitive unless `exact`). */
export function findText(screen, needle, options = {}) {
  const haystacks = screen.texts.map((t) => (options.caseSensitive ? t : t.toLowerCase()))
  const goal = options.caseSensitive ? needle : needle.toLowerCase()
  return screen.texts.filter((t, i) => haystacks[i].includes(goal))
}
