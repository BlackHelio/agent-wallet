/**
 * agent-wallet — Desktop UI: full-page wallet (balances, approvals, mandates, receipts).
 *
 * SDK-only: imports @hermes/plugin-sdk, react, react/jsx-runtime. No JSX syntax (loaded
 * uncompiled), no DOM queries into app markup, no raw bridge calls. Everything privileged goes
 * through /api/plugins/agent-wallet, which holds the operator token server-side.
 */

import { PALETTE_AREA, ROUTES_AREA, SIDEBAR_NAV_AREA, host } from '@hermes/plugin-sdk'
import { createElement as h, useCallback, useEffect, useMemo, useState } from 'react'

const ID = 'agent-wallet'
const ROUTE = '/wallet'

const COIN = {
  XMR: { color: '#f26822', name: 'Monero', glyph: 'M', decimals: 6 },
  BTC: { color: '#f7931a', name: 'Bitcoin', glyph: 'B', decimals: 6 },
  ETH: { color: '#627eea', name: 'Ethereum', glyph: 'E', decimals: 6 },
  POL: { color: '#8247e5', name: 'Polygon', glyph: 'P', decimals: 6 },
  USDC: { color: '#2775ca', name: 'USD Coin', glyph: '$', decimals: 2 }
}

// Native <select> popups are painted by the platform, not by Tailwind classes: with a transparent
// background the option list renders white-on-white in the app's dark theme. Give the control and
// every option an explicit face, taken from the app's own tokens so light themes still work.
// --card / --foreground are the app's own tokens (opaque surfaces); the earlier
// --ui-bg-secondary fallback is an 8%-alpha tint, which is why the popup read white-on-white.
// One place for field styling: every input/select in the page gets an outline, and a focus ring so
// it is obvious which field has the cursor.
/**
 * Field styling lives in one place, as inline styles.
 *
 * It used to be Tailwind arbitrary values (`border-[color:var(--ui-border)]`), which only exist if
 * the app's build ever compiled that exact class — it didn't, so the fields had no border at all.
 * Inline CSS always applies.
 */
const FIELD_STYLE = {
  border: '2px solid var(--ui-border, #6b7280)',
  backgroundColor: 'var(--ui-bg-elevated, var(--ui-bg-secondary, transparent))',
  borderRadius: '8px',
  padding: '7px 9px',
  fontSize: '13px',
  lineHeight: '1.2',
  // `inherit` meant the page ink on a hardcoded dark face: in a light theme that is near-black text
  // on #23262c, i.e. a field that looks empty. The ink now comes from the same token as the page.
  color: 'var(--foreground, currentColor)',
  outline: 'none',
  boxShadow: 'none',
  minHeight: '34px'
}

const FIELD_FOCUS = 'var(--accent, #6366f1)'

/** Style props for a field: the shared look plus the caller's width/font tweaks. */
function fieldProps(extra) {
  return { ...FIELD_STYLE, ...(extra || {}) }
}

/** Convenience for call sites that only need a className-less element with the field look. */
function field() { return '' }

// `--card` does not exist in this app, so the old fallback (#23262c) was the *real* face in every
// theme, and in a light theme that put near-black text on it — measured 1.09:1, i.e. an invisible
// dropdown. `--ui-bg-elevated` is the app's own opaque surface token and flips with the theme
// (measured #fdfdfd light / #37373a dark). System colours (Canvas/CanvasText) would have been the
// obvious answer, but the app pins `color-scheme: light` and never sets it to dark, so they would
// have stayed white on a dark page — which is exactly what the dark pass caught.
// A monospace that exists on all three: SF Mono/Menlo on macOS, Consolas/Cascadia on Windows,
// whatever the desktop calls monospace on Linux.
const MONO = 'ui-monospace, SFMono-Regular, Menlo, Consolas, "Cascadia Mono", "DejaVu Sans Mono", monospace'
const SELECT_FACE = 'var(--ui-bg-elevated, var(--ui-bg-secondary, transparent))'
const SELECT_INK = 'var(--foreground, currentColor)'
// Brand colours are picked to look right as a dot, not as 11 px text: USDC's #2775ca measured
// 3.87:1 on a light surface. Mixing toward the theme's own ink keeps the hue and follows the theme,
// so one expression is legible on a light page and on a dark one.
const coinInk = (asset) => {
  const c = (COIN[asset] || {}).color
  return c ? `color-mix(in srgb, ${c} 55%, var(--foreground, currentColor))` : 'var(--foreground, currentColor)'
}
const SELECT_STYLE = { backgroundColor: SELECT_FACE, color: SELECT_INK }
const OPTION_STYLE = { backgroundColor: SELECT_FACE, color: SELECT_INK }

function opt(value, label, key) {
  return h('option', { key: key || String(value), value, style: OPTION_STYLE, children: label })
}

const MANDATE_ASSET = {
  'monero-mainnet': 'XMR', 'monero-stagenet': 'XMR', 'monero-testnet': 'XMR',
  'bitcoin-mainnet': 'BTC', 'bitcoin-testnet': 'BTC',
  'xrp-mainnet': 'XRP', 'xrp-testnet': 'XRP',
  'base-mainnet': 'ETH', 'base-sepolia': 'ETH',
  'polygon-mainnet': 'POL', 'polygon-amoy': 'POL', 'sim-mainnet': 'XMR'
}

const CHAIN_ASSETS = {
  'monero-mainnet': ['XMR'], 'monero-stagenet': ['XMR'], 'monero-testnet': ['XMR'],
  'bitcoin-mainnet': ['BTC'], 'bitcoin-testnet': ['BTC'],
  'xrp-mainnet': ['XRP'], 'xrp-testnet': ['XRP'],
  'base-mainnet': ['ETH', 'USDC'], 'base-sepolia': ['ETH', 'USDC'],
  'polygon-mainnet': ['POL', 'USDC'], 'polygon-amoy': ['POL', 'USDC'],
  'arbitrum-mainnet': ['ETH', 'USDC'], 'sim-mainnet': ['XMR']
}

// What a human should read in a dropdown: the coin, not the internal chain id. The value keeps
// the id — only the label is friendly.
const CHAIN_NAME = {
  'monero-mainnet': 'Monero',
  'monero-stagenet': 'Monero · stagenet',
  'monero-testnet': 'Monero · testnet',
  'bitcoin-mainnet': 'Bitcoin',
  'bitcoin-testnet': 'Bitcoin · testnet',
  'base-mainnet': 'Base · ETH + USDC',
  'base-sepolia': 'Base · Sepolia (test)',
  'polygon-mainnet': 'Polygon · POL + USDC',
  'polygon-amoy': 'Polygon · Amoy (test)',
  'sim-mainnet': 'Simulator (play money)'
}

function chainName(kind) {
  return CHAIN_NAME[kind] || String(kind).replace(/-mainnet$/, '').replace(/-/g, ' ')
}

// The network a coin sits on, for the line *under* an Assets row. It cannot be CHAIN_NAME: the row
// already says 'BTC · Bitcoin' on its head, so a subtitle of 'Bitcoin · electrum · …' says the coin
// twice and never says which network it is on. This names the network, and says when it is a test
// one (the row reads 'Bitcoin · testnet · electrum · …').
const CHAIN_NETWORK = {
  'monero-mainnet': 'Monero mainnet',
  'monero-stagenet': 'Monero · stagenet',
  'monero-testnet': 'Monero · testnet',
  'bitcoin-mainnet': 'Bitcoin mainnet',
  'bitcoin-testnet': 'Bitcoin · testnet',
  'base-mainnet': 'Base mainnet',
  'base-sepolia': 'Base Sepolia (test)',
  'polygon-mainnet': 'Polygon mainnet',
  'polygon-amoy': 'Polygon Amoy (test)',
  'arbitrum-mainnet': 'Arbitrum mainnet',
  'xrp-mainnet': 'XRP Ledger mainnet',
  'xrp-testnet': 'XRP Ledger · testnet',
  'sim-mainnet': 'Simulator (play money)'
}

function networkName(kind) {
  return CHAIN_NETWORK[kind] || String(kind).replace(/-mainnet$/, '')
}

/* ── formatting ─────────────────────────────────────────────────────────── */

function fmt(value, asset) {
  const spec = COIN[asset] || { decimals: 6 }
  const n = Number(value)
  if (!Number.isFinite(n)) return '0'
  if (n === 0) return '0'
  const digits = Math.min(Math.max(spec.decimals, 2), 8)
  const text = n.toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: digits })
  // Rounding is fine for money until it erases it: 0.000193371234095216 ETH printed as "0", which
  // reads as an empty wallet and invites someone to fund a row that already has gas. A non-zero
  // balance that falls under the display precision is shown with just enough digits to stay
  // non-zero (trailing zeros trimmed), never "0" and never a made-up number.
  if (Number(text.replace(/,/g, '')) === 0) return underPrecision(value, n)
  return text
}

/** A real amount too small for `digits` places: '0.00019337' instead of '0'. */
function underPrecision(value, n) {
  // Exact-ish digits from the string the daemon sent: toFixed() on a float drops the tail.
  const s = String(value).trim()
  const m = /^(\d*)\.(\d+)$/.exec(s)
  const frac = m ? m[2].replace(/0+$/, '') : ''
  if (frac) return '0.' + frac.slice(0, Math.max(4, Math.min(frac.length, 12)))
  // a float that arrived already in exponent form: 1e-7 is still a balance, not a zero
  return /e-/i.test(s) ? n.toFixed(Math.min(12, Math.max(1, -Math.floor(Math.log10(n)) + 1))) : s
}

/**
 * A fee that is real but smaller than the asset's display precision. `fmt` rounds 0.00004 USDC to
 * "0", and "network fee 0 USDC" reads as free — the one number on the card a person will check.
 */
function feeText(value, asset) {
  const n = Number(value)
  if (!Number.isFinite(n) || n <= 0) return '0'
  const spec = COIN[asset] || { decimals: 6 }
  if (n < Math.pow(10, -spec.decimals)) return `<0.${'0'.repeat(Math.max(0, spec.decimals - 1))}1`
  // fees need precision, not pretty thousands separators
  return String(Number(n.toFixed(Math.min(spec.decimals + 2, 12))))
}

function shortAddr(address) {
  const s = String(address || '')
  return s.length <= 22 ? s : `${s.slice(0, 10)}…${s.slice(-8)}`
}

function ago(ts) {
  if (!ts) return ''
  const ms = Date.now() - Date.parse(ts)
  if (!Number.isFinite(ms) || ms < 0) return ts
  const min = Math.round(ms / 60000)
  if (min < 1) return 'just now'
  if (min < 60) return `${min}m ago`
  const hours = Math.round(min / 60)
  if (hours < 24) return `${hours}h ago`
  return `${Math.round(hours / 24)}d ago`
}

/** "18:55:19" from an ISO stamp, in UTC: the same clock the receipts table prints. */
function clockTime(ts) {
  const text = String(ts || '')
  return text.length >= 19 ? text.slice(11, 19) : text
}

/**
 * What a destroyed sandbox left behind: how much was swept home, and when it died.
 *
 * Two sources, on purpose. The daemon grew `was_destroyed_with` after this pane shipped, so a
 * field that is absent means "old backend", not "no sweep" — the fallback is the signed
 * `sandbox/destroyed` receipt we already fetched for the Receipts list. Neither is invented: an
 * amount nobody reported comes back empty and the card says so, because a destroyed sandbox
 * showing a leftover "0.003" is the one lie this page must not tell.
 */
function destroyedFacts(sb, receipts) {
  const box = sb || {}
  let swept = ''
  let at = String(box.destroyed_at || '')
  const raw = box.was_destroyed_with
  if (raw && typeof raw === 'object') {
    const value = raw.swept !== undefined && raw.swept !== null ? raw.swept : raw.amount
    swept = value === undefined || value === null ? '' : String(value)
    at = String(raw.at || raw.ts || raw.destroyed_at || at)
  } else if (raw !== undefined && raw !== null && String(raw).trim() !== '') {
    swept = String(raw)
  }
  if (!swept && Array.isArray(receipts)) {
    const hit = receipts.find((e) => e && e.kind === 'sandbox' && e.decision === 'destroyed'
      && e.details && String(e.details.sandbox_id || '') === String(box.sandbox_id || ''))
    if (hit) {
      const value = hit.details.swept !== undefined && hit.details.swept !== null
        ? hit.details.swept : hit.details.amount
      if (value !== undefined && value !== null) swept = String(value)
      at = at || String(hit.ts || '')
    }
  }
  const n = Number(swept)
  return { swept: swept === '' ? null : swept, zero: swept !== '' && Number.isFinite(n) && n === 0, at }
}


/* ── QR encoder ───────────────────────────────────────────────────────────
 * A real QR encoder, inline: byte mode, error correction L, versions 1–6 (up to 41×41). No CDN and
 * no dependency — a receiving address is the one thing on this page a phone has to read, and a
 * pretty block-grid would scan as nothing at all. Versions stop at 6 on purpose: from version 7 a
 * QR carries a second 18-bit version block, and every address this wallet hands out (Monero 95–106
 * chars, bech32/hex 42) fits in version 5.
 */
const QR_TABLE = [
  null,
  [7, 1, 19, 0, 0], [10, 1, 34, 0, 0], [15, 1, 55, 0, 0],
  [20, 1, 80, 0, 0], [26, 1, 108, 0, 0], [18, 2, 68, 0, 0]
]
const QR_ALIGN = [[], [], [6, 18], [6, 22], [6, 26], [6, 30], [6, 34]]

// GF(256) with the QR polynomial 0x11d, as log/antilog tables.
const QR_EXP = new Array(512)
const QR_LOG = new Array(256)
{
  let x = 1
  for (let i = 0; i < 255; i++) {
    QR_EXP[i] = x
    QR_LOG[x] = i
    x <<= 1
    if (x & 0x100) x ^= 0x11d
  }
  for (let i = 255; i < 512; i++) QR_EXP[i] = QR_EXP[i - 255]
}

function qrMul(a, b) {
  return a && b ? QR_EXP[QR_LOG[a] + QR_LOG[b]] : 0
}

/** Reed–Solomon error-correction codewords for one block. */
function qrRemainder(data, ecLen) {
  let gen = [1]
  for (let i = 0; i < ecLen; i++) {
    const next = new Array(gen.length + 1).fill(0)
    for (let j = 0; j < gen.length; j++) {
      next[j] ^= gen[j]
      next[j + 1] ^= qrMul(gen[j], QR_EXP[i])
    }
    gen = next
  }
  const res = data.concat(new Array(ecLen).fill(0))
  for (let i = 0; i < data.length; i++) {
    const factor = res[i]
    if (!factor) continue
    for (let j = 0; j < gen.length; j++) res[i + j] ^= qrMul(gen[j], factor)
  }
  return res.slice(data.length)
}

/** 15-bit format word: ECC level L (01), the given mask, BCH(15,5) and the fixed XOR mask. */
function qrFormatBits(mask) {
  const data = (1 << 3) | mask
  let rem = data
  for (let i = 0; i < 10; i++) rem = (rem << 1) ^ ((rem >>> 9) * 0x537)
  return ((data << 10) | rem) ^ 0x5412
}

/**
 * Encode text as a QR matrix. Returns an array of rows of 0/1, or null when the payload is longer
 * than version 6 at ECC L can carry — the caller then shows the address without a code rather than
 * a code that does not decode.
 */
function qrEncode(text, forceMask) {
  const raw = String(text == null ? '' : text)
  if (!raw) return null
  const bytes = []
  for (const ch of raw) {
    const cp = ch.codePointAt(0)
    if (cp < 0x80) bytes.push(cp)
    else if (cp < 0x800) bytes.push(0xc0 | (cp >> 6), 0x80 | (cp & 63))
    else if (cp < 0x10000) bytes.push(0xe0 | (cp >> 12), 0x80 | ((cp >> 6) & 63), 0x80 | (cp & 63))
    else bytes.push(0xf0 | (cp >> 18), 0x80 | ((cp >> 12) & 63), 0x80 | ((cp >> 6) & 63), 0x80 | (cp & 63))
  }
  let version = 0
  for (let v = 1; v <= 6; v++) {
    const t = QR_TABLE[v]
    if (12 + bytes.length * 8 <= (t[1] * t[2] + t[3] * t[4]) * 8) { version = v; break }
  }
  if (!version) return null
  const [ecLen, g1, d1, g2, d2] = QR_TABLE[version]
  const totalData = g1 * d1 + g2 * d2

  const bits = []
  const push = (val, len) => { for (let i = len - 1; i >= 0; i--) bits.push((val >> i) & 1) }
  push(4, 4)                       // byte mode
  push(bytes.length, 8)            // count: 8 bits for versions 1–9
  bytes.forEach((b) => push(b, 8))
  const cap = totalData * 8
  for (let i = 0; i < 4 && bits.length < cap; i++) bits.push(0)   // terminator
  while (bits.length % 8) bits.push(0)
  for (let pad = 0xec; bits.length < cap; pad ^= 0xfd) push(pad, 8)

  const dataCw = []
  for (let i = 0; i < bits.length; i += 8) {
    let b = 0
    for (let j = 0; j < 8; j++) b = (b << 1) | bits[i + j]
    dataCw.push(b)
  }
  const blocks = []
  let at = 0
  for (let i = 0; i < g1; i++) { blocks.push(dataCw.slice(at, at + d1)); at += d1 }
  for (let i = 0; i < g2; i++) { blocks.push(dataCw.slice(at, at + d2)); at += d2 }
  const ecs = blocks.map((b) => qrRemainder(b, ecLen))
  const codewords = []
  const longest = Math.max(...blocks.map((b) => b.length))
  for (let i = 0; i < longest; i++) for (const b of blocks) if (i < b.length) codewords.push(b[i])
  for (let i = 0; i < ecLen; i++) for (const e of ecs) codewords.push(e[i])

  const size = 17 + 4 * version
  const grid = Array.from({ length: size }, () => new Array(size).fill(0))
  const fixed = Array.from({ length: size }, () => new Array(size).fill(false))
  const set = (x, y, v) => {
    if (x < 0 || y < 0 || x >= size || y >= size) return
    grid[y][x] = v ? 1 : 0
    fixed[y][x] = true
  }
  // finders with their separators, then the timing patterns through the gaps
  for (const [cx, cy] of [[3, 3], [size - 4, 3], [3, size - 4]]) {
    for (let dy = -4; dy <= 4; dy++) {
      for (let dx = -4; dx <= 4; dx++) {
        const d = Math.max(Math.abs(dx), Math.abs(dy))
        set(cx + dx, cy + dy, d !== 2 && d !== 4)
      }
    }
  }
  for (let i = 0; i < size; i++) {
    if (!fixed[6][i]) set(i, 6, i % 2 === 0)
    if (!fixed[i][6]) set(6, i, i % 2 === 0)
  }
  for (const cy of QR_ALIGN[version]) {
    for (const cx of QR_ALIGN[version]) {
      if (fixed[cy][cx]) continue
      for (let dy = -2; dy <= 2; dy++) {
        for (let dx = -2; dx <= 2; dx++) {
          set(cx + dx, cy + dy, Math.max(Math.abs(dx), Math.abs(dy)) !== 1)
        }
      }
    }
  }
  // format strips (both copies) plus the always-dark module, as explicit coordinates
  const formatSpots = []
  for (let i = 0; i <= 5; i++) formatSpots.push([8, i, i])
  formatSpots.push([8, 7, 6], [8, 8, 7], [7, 8, 8])
  for (let i = 9; i < 15; i++) formatSpots.push([14 - i, 8, i])
  for (let i = 0; i < 8; i++) formatSpots.push([size - 1 - i, 8, i])
  for (let i = 8; i < 15; i++) formatSpots.push([8, size - 15 + i, i])
  formatSpots.push([8, size - 8, -1])
  formatSpots.forEach(([x, y]) => { fixed[y][x] = true })

  // data in the standard two-column zigzag, skipping function modules
  let bi = 0
  for (let right = size - 1; right >= 1; right -= 2) {
    if (right === 6) right = 5
    for (let vert = 0; vert < size; vert++) {
      for (let j = 0; j < 2; j++) {
        const x = right - j
        const upward = ((right + 1) & 2) === 0
        const y = upward ? size - 1 - vert : vert
        if (fixed[y][x]) continue
        grid[y][x] = bi < codewords.length * 8
          ? (codewords[bi >>> 3] >> (7 - (bi & 7))) & 1
          : 0
        bi += 1
      }
    }
  }

  const maskFn = (m, x, y) => {
    switch (m) {
      case 0: return (x + y) % 2 === 0
      case 1: return y % 2 === 0
      case 2: return x % 3 === 0
      case 3: return (x + y) % 3 === 0
      case 4: return (Math.floor(y / 2) + Math.floor(x / 3)) % 2 === 0
      case 5: return ((x * y) % 2) + ((x * y) % 3) === 0
      case 6: return (((x * y) % 2) + ((x * y) % 3)) % 2 === 0
      default: return (((x + y) % 2) + ((x * y) % 3)) % 2 === 0
    }
  }
  const penalty = (m) => {
    let score = 0
    const run = (get) => {
      let len = 1
      for (let i = 1; i < size; i++) {
        if (get(i) === get(i - 1)) {
          len += 1
          if (len === 5) score += 3
          else if (len > 5) score += 1
        } else len = 1
      }
    }
    for (let y = 0; y < size; y++) run((i) => m[y][i])
    for (let x = 0; x < size; x++) run((i) => m[i][x])
    // finder-like patterns with four light modules beside them (rule N3)
    const near = ['10111010000', '00001011101']
    const count = (line) => near.reduce((n, pat) => n + line.split(pat).length - 1, 0)
    for (let y = 0; y < size; y++) score += 40 * count(m[y].join(''))
    for (let x = 0; x < size; x++) score += 40 * count(m.map((row) => row[x]).join(''))
    let dark = 0
    for (let y = 0; y < size; y++) {
      for (let x = 0; x < size; x++) {
        dark += m[y][x]
        if (y < size - 1 && x < size - 1) {
          const v = m[y][x]
          if (v === m[y][x + 1] && v === m[y + 1][x] && v === m[y + 1][x + 1]) score += 3
        }
      }
    }
    score += Math.floor(Math.abs((dark / (size * size)) * 100 - 50) / 5) * 10
    return score
  }
  let best = null
  let bestScore = Infinity
  for (let m = 0; m < 8; m++) {
    if (forceMask != null && m !== forceMask) continue
    const cand = grid.map((row) => row.slice())
    for (let y = 0; y < size; y++) {
      for (let x = 0; x < size; x++) {
        if (!fixed[y][x] && maskFn(m, x, y)) cand[y][x] ^= 1
      }
    }
    const fbits = qrFormatBits(m)
    formatSpots.forEach(([x, y, bit]) => { cand[y][x] = bit < 0 ? 1 : (fbits >> bit) & 1 })
    const score = penalty(cand)
    if (score < bestScore) { bestScore = score; best = cand }
  }
  return best
}

/** The code as inline SVG: one path, no image, no third-party service. */
function QrCode({ text, size = 168 }) {
  const matrix = qrEncode(text)
  if (!matrix) {
    return h('div', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
      children: 'this address is too long to draw as a QR — copy it instead' })
  }
  const n = matrix.length
  const quiet = 4
  const dim = n + quiet * 2
  let d = ''
  for (let y = 0; y < n; y++) {
    for (let x = 0; x < n; x++) {
      if (matrix[y][x]) d += `M${x + quiet} ${y + quiet}h1v1h-1z`
    }
  }
  return h('svg', {
    width: size, height: size, viewBox: `0 0 ${dim} ${dim}`, role: 'img',
    'aria-label': 'QR code for this receiving address', shapeRendering: 'crispEdges'
  }, [
    h('rect', { key: 'bg', width: dim, height: dim, fill: '#ffffff' }),
    h('path', { key: 'fg', d, fill: '#000000' })
  ])
}

/* ── primitives ─────────────────────────────────────────────────────────── */

function CoinIcon({ asset, size = 34 }) {
  const spec = COIN[asset] || { color: '#6b7280', glyph: '·' }
  const c = size / 2
  const glyph = asset === 'ETH'
    ? h('polygon', {
        points: `${c},${size * 0.22} ${size * 0.8},${c} ${c},${size * 0.72} ${size * 0.2},${c}`,
        fill: '#fff'
      })
    : h('text', {
        x: c, y: size * 0.69, textAnchor: 'middle', fill: '#fff',
        fontSize: size * 0.5, fontWeight: 700, fontFamily: 'ui-sans-serif, system-ui'
      }, spec.glyph)

  return h('svg', { width: size, height: size, viewBox: `0 0 ${size} ${size}`, 'aria-hidden': 'true' },
    h('circle', { cx: c, cy: c, r: c - 1, fill: spec.color }),
    glyph
  )
}

function pill(text, tone) {
  const tones = {
    good: 'border-emerald-500/40 text-emerald-400',
    warn: 'border-amber-500/40 text-amber-400',
    bad: 'border-red-500/40 text-red-400',
    flat: 'border-(--ui-border) text-(--ui-text-tertiary)'
  }
  return h('span', {
    className: 'rounded-full border-2 px-2.5 py-0.5 text-[0.6875rem] font-medium uppercase tracking-wide ' + (tones[tone] || tones.flat),
    children: text
  })
}

function btn(label, onClick, tone, disabled, title) {
  const tones = {
    good: 'border-emerald-500/50 text-emerald-300 hover:bg-emerald-500/10',
    bad: 'border-red-500/50 text-red-300 hover:bg-red-500/10',
    plain: 'border-(--ui-border) hover:bg-(--ui-bg-secondary)'
  }
  // `disabled` and `title` are optional and appended last, so every existing 3-argument call keeps its
  // behaviour. A disabled control is better than a click that explains itself afterwards: the reason
  // is on the button before the user tries it.
  return h('button', {
    type: 'button',
    disabled: !!disabled,
    title: title || undefined,
    className: 'rounded-md border-[2px] px-3 py-1.5 text-[0.8125rem] font-medium transition-colors '
      + (tones[tone] || tones.plain) + (disabled ? ' opacity-50 cursor-not-allowed' : ''),
    onClick: disabled ? undefined : onClick,
    children: label
  })
}

/**
 * One card per section: a header strip, a rule under it, and the body. Sections used to run into
 * each other with only a heading, which made a long page hard to scan.
 */
function section(title, right, children, options) {
  const opts = options || {}
  return h('section', {
    key: opts.key || title,
    // 1px: the approval card and the balance rows keep their weight, everything else recedes. With
    // every box at 3px nothing had hierarchy — seven sections all shouted at once.
    className: 'flex flex-col gap-3 rounded-xl border border-(--ui-border) p-3',
    style: { background: 'var(--wallet-surface)' }
  }, [
    h('div', { key: 'h', className: 'flex flex-wrap items-center gap-2 border-b-2 border-(--ui-border) pb-2' }, [
      h('h2', {
        key: 't',
        // No `flex-1`: a title that takes the free width pushes its buttons to the far edge and wraps
        // the heading between its own words ("… (1" / "active)") as soon as the row is tight. Natural
        // width, and the spacer below pushes the actions right when there is room.
        className: 'text-[0.8125rem] font-semibold uppercase tracking-[0.10em] text-(--ui-text-secondary)',
        style: { maxWidth: '100%', overflowWrap: 'anywhere' },
        children: title
      }),
      h('span', { key: 'sp', className: 'flex-1' }),
      right ? h('div', { key: 'r', className: 'flex flex-wrap items-center gap-2' }, right) : null
    ]),
    h('div', { key: 'b', className: 'flex flex-col gap-3' }, children)
  ])
}

/* ── approval ───────────────────────────────────────────────────────────── */

/**
 * The contact whose address book holds this payee, or '' — read from the pane's own list, never
 * from anything the agent said. Exact match on purpose: an address that differs by one character
 * is a different payee, and "known contact" on a lookalike is the one wrong label this card cannot
 * afford to print.
 */
function knownPayee(contacts, payee) {
  const want = String(payee || '').trim()
  if (!want) return ''
  for (const contact of contacts || []) {
    const addresses = (contact && contact.addresses) || {}
    for (const kind of Object.keys(addresses)) {
      if (String(addresses[kind] || '').trim() === want) return String(contact.name || '')
    }
  }
  return ''
}

/**
 * The one approval control. The agent's prompt card and the pending list both render this.
 *
 * There used to be two: 'Approve' in the prompt card, with no check that a passphrase had been
 * typed, and 'Accept' → 'Confirm payment' in the list, which did check. Same decision, two
 * spellings, two behaviours — so the label never told you what would happen. One component, one
 * pair of labels (Approve / Deny, deny in the destructive colour), one guard, one field order.
 *
 * It holds its own passphrase state, so it must stay a module-level component: defining it inside
 * WalletPage would change its identity on every 3s poll and wipe whatever was being typed.
 */
function ApprovalActions({ request, humanGate, pinRequired, alias, locked, pinSet, facts, autoFocus,
                          onApprove, onDeny, onError }) {
  const [amount, setAmount] = useState('')   // the number the human types to prove they read it
  const [pin, setPin] = useState('')         // the six digits; never leaves this call
  const [pass, setPass] = useState('')       // only for a wallet that still runs without a PIN
  const expired = Boolean(request.approval_expires_at)
    && Date.parse(request.approval_expires_at) < Date.now()
  // Approving needs the keystore open; offering a PIN on a locked wallet invites a secret into a
  // flow that cannot work. Deny stays available — refusing removes authority and needs no unlock.
  // No PIN yet → the daemon refuses every approval (`pin_not_set`). Say what to do instead of
  // collecting six digits that cannot be checked against anything.
  const pinMissing = pinSet === false
  const canApprove = Boolean(request.approval_id) && !expired && !locked && !pinMissing
  // the daemon decides whether this amount must be typed (per asset, never above that coin's cap);
  // older daemons do not send the flag, so an amount field is shown and the server still enforces
  const mustType = request.confirm_amount_required !== false
  const needsPin = pinRequired !== false
  const fiat = request.fiat && request.fiat.amount
    ? `${request.fiat.amount} ${request.fiat.currency || ''}`.trim()
    : ''

  const approve = () => {
    if (!request.approval_id) {
      onError('this request has no approval id yet — the wallet has not opened it for signing')
      return
    }
    if (expired) {
      onError('this offer expired — ask the agent to request it again')
      return
    }
    if (needsPin && !/^\d{6}$/.test(pin)) {
      onError('the PIN is six digits — that is what makes this your payment')
      return
    }
    if (!needsPin && humanGate && !pass) {
      onError('type your passphrase — that is what makes this your approval')
      return
    }
    if (mustType && !amount) {
      onError(`type ${request.amount} ${request.asset || ''} to confirm you read it`.trim())
      return
    }
    // `expect_*` is the card: the daemon refuses the PIN if the request moved after these numbers
    // were drawn, so what is on screen is what gets signed.
    const body = {
      confirm_amount: amount || null,
      expect_amount: request.amount,
      expect_payee: request.payee
    }
    if (needsPin) body.pin = pin
    else if (pass) body.passphrase = pass
    onApprove(body)
  }

  // Label above value, left-aligned: side-by-side with a right-aligned address turns a 95-character
  // payee into six ragged lines on a narrow pane, and the eye cannot compare one row to the next.
  const line = (label, value, mono) => h('div', {
    key: label, className: 'flex flex-col gap-0.5'
  }, [
    h('span', { key: 'l', className: 'text-[0.625rem] uppercase tracking-[0.08em] text-(--ui-text-tertiary)',
                children: label }),
    h('span', { key: 'v', className: (mono ? 'font-mono ' : '') + 'min-w-0 break-all text-left text-[0.8125rem]',
                children: value })
  ])

  return h('div', { className: 'flex flex-col gap-2 rounded-lg border-[2px] border-(--ui-border) p-2.5' }, [
    // ── the card: everything the human needs to see before the PIN ──────────────────────────
    h('div', { key: 'card', className: 'flex flex-col gap-1' }, [
      // amount and fee first: those are what the PIN commits to, and they must sit next to the PIN
      // rather than eight lines below a 95-character address
      h('div', { key: 'amount', className: 'flex flex-col gap-0.5' }, [
        h('span', { key: 'l', className: 'text-[0.625rem] uppercase tracking-[0.08em] text-(--ui-text-tertiary)',
                    children: 'amount' }),
        h('span', { key: 'v', className: 'font-mono text-[1.0625rem] font-semibold tabular-nums',
                    children: `${fmt(request.amount, request.asset)} ${request.asset || ''}`.trim() }),
        fiat
          ? h('span', { key: 'f', className: 'text-[0.75rem] text-(--ui-text-secondary)',
                        title: request.fiat && request.fiat.rate
                          ? `1 ${request.asset} = ${request.fiat.rate} ${request.fiat.currency}` : '',
                        children: `≈ ${fiat}` })
          : null
      ]),
      request.fee_estimate && Number(request.fee_estimate) > 0
        ? line('network fee', `${feeText(request.fee_estimate, request.asset)} ${request.asset || ''}`.trim())
        : null,
      // What the payment leaves in the gas coin, next to the fee it costs. The floor is the reason
      // the wallet will not spend its last ETH: this line is where a human sees that.
      request.gas && request.gas.native
        ? line('gas after', `${feeText(request.gas.after, request.gas.native)} ${request.gas.native}`
             + ` · keeps ${feeText(request.gas.floor, request.gas.native)}`)
        : null,
      // A conversion is not a payment: the destination is a router pinned in code, so the card says
      // what is being bought and the worst rate the operator allowed, not "pay to 0x…".
      request.swap && request.swap.asset_out
        ? line('swap for', `${fmt(request.swap.quoted_out, request.swap.asset_out)} ${request.swap.asset_out}`
             + ` (min ${fmt(request.swap.min_out, request.swap.asset_out)})`)
        : null,
      request.swap && request.swap.operator
        ? line('route', `${request.swap.label || request.swap.operator}`
             + ` · ${(Number(request.swap.slippage_bps) || 0) / 100}% slippage allowed`)
        : null,
      facts === false || (request.swap && request.swap.asset_out)
        ? null
        : line('pay to', alias ? `${alias} · ${request.payee}` : request.payee, true),
      facts === false ? null : (!alias ? h('div', { key: 'new', className: 'text-[0.6875rem]',
                         style: { color: 'var(--ui-text-tertiary)' },
                         children: 'not a saved contact — check every character before you approve' })
        : null),
      line('chain', chainName(request.kind)),
      request.provenance && request.provenance.host
        ? (request.provenance.verified === 'true'
            ? line('address read from', `${request.provenance.host}`
                + (request.provenance.title ? ` · ${request.provenance.title}` : '')
                + (request.provenance.method ? ` (${request.provenance.method})` : ''))
            // the wallet did NOT read that page: the agent typed the host, so say so
            : line('agent says', `${request.provenance.host} — not checked by the wallet`))
        : null,
      request.provenance && request.provenance.url
        ? h('div', { key: 'prov', className: 'break-all font-mono text-[0.625rem] text-(--ui-text-tertiary)',
                     children: request.provenance.url })
        : null,
      request.memo ? line('memo', request.memo) : null,
      request.why ? line('why', request.why) : null
    ]),
    expired
      ? h('div', { key: 'expired', className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
                   children: 'This offer expired — it cannot be approved. Deny closes it, or ask the '
                             + 'agent to request it again and a fresh card appears.' })
      : null,
    locked || pinMissing
      ? h('div', { key: 'locked', className: 'rounded-md border-[2px] px-2 py-1.5 text-[0.6875rem]',
                   style: { borderColor: 'var(--ui-border)' },
                   children: pinMissing
                     ? (locked
                         ? 'Unlock the wallet with your passphrase below, then set a PIN — approving asks for it every time.'
                         : 'Set a PIN first (the card above) — every payment asks for it, and without one the wallet refuses to approve.')
                     : 'Unlock the wallet with your passphrase below to approve this payment.' })
      : null,
    h('div', { key: 'row', className: 'flex flex-wrap items-center gap-2' }, [
      // Two typing boxes on one card is what a person stumbles on. The PIN is already bound to the
      // amount (`expect_amount`), so the typed amount only appears when the policy demands it.
      locked || pinMissing || expired || !(mustType && !needsPin) ? null : h('input', {
        key: 'amt', value: amount, inputMode: 'decimal',
        placeholder: `type ${fmt(request.amount, request.asset)} to confirm`,
        title: 'this is the guard: type the number yourself, do not paste it',
        style: fieldProps({ width: '11rem' }),
        onChange: (e) => setAmount(e.target.value),
        onKeyDown: (e) => { if (e.key === 'Enter') approve() }
      }),
      locked || pinMissing || expired ? null : (needsPin
        ? h('input', {
            key: 'pin', type: 'password', value: pin, inputMode: 'numeric', maxLength: 6,
            autoComplete: 'off', autoFocus: Boolean(autoFocus),
            placeholder: 'PIN · 6 digits',
            title: 'the PIN you set for this wallet; it is checked in the daemon and never sent to the agent',
            style: fieldProps({ width: '9rem', letterSpacing: '0.3em' }),
            onChange: (e) => setPin(e.target.value.replace(/\D/g, '').slice(0, 6)),
            onKeyDown: (e) => { if (e.key === 'Enter') approve() }
          })
        : h('input', {
            key: 'pw', type: 'password', value: pass, autoFocus: Boolean(autoFocus),
            placeholder: 'wallet passphrase — never sent to the agent',
            style: fieldProps({ width: '16rem' }),
            onChange: (e) => setPass(e.target.value),
            onKeyDown: (e) => { if (e.key === 'Enter') approve() }
          })),
      canApprove ? btn('Approve', approve, 'good') : null,
      // Deny is available even on an expired request: that is the only way left to close it.
      btn('Deny', onDeny, 'bad')
    ])
  ])
}

/**
 * First run: the wallet has no PIN, so it cannot approve anything. Say so, and let the human set one
 * here — the passphrase is required because the PIN is the *lower* secret and must not be settable
 * by anything holding only a token.
 */
function PinSetupCard({ busy, onSet, onError, mode = 'set', onCancel }) {
  const change = mode === 'change'
  const [pin, setPin] = useState('')
  const [again, setAgain] = useState('')
  const [pass, setPass] = useState('')
  const submit = () => {
    if (!/^\d{6}$/.test(pin)) { onError('the PIN is exactly six digits'); return }
    if (pin !== again) { onError('the two PINs are not the same'); return }
    if (!pass) {
      onError(change ? 'type the wallet passphrase or your current PIN' : 'type the wallet passphrase — it is what makes this your decision')
      return
    }
    // Once a PIN exists it opens the wallet, so it is a credential in its own right: accept it in place
    // of the passphrase rather than trapping whoever only remembers the six digits.
    const asPin = change && /^\d{6}$/.test(pass.trim())
    onSet(asPin ? { pin, confirm: again, current_pin: pass.trim() }
                : { pin, confirm: again, passphrase: pass })
  }
  return h('div', { className: 'rounded-xl border-[3px] p-3 flex flex-col gap-2', style: { borderColor: 'var(--ui-border)' } }, [
    h('div', { className: 'text-[0.875rem] font-medium',
               children: change ? 'Change the payment PIN' : 'Set a PIN for payments' }),
    h('div', { className: 'text-[0.75rem] text-(--ui-text-tertiary)', children: change
      ? 'The old PIN stops working the moment this one is saved — including on a card that is already open. '
        + 'It unlocks the wallet and authorises each payment, so changing it costs the passphrase or the current PIN.'
      : 'Six digits: this unlocks the wallet and authorises each payment. It cannot change policy — that is what the passphrase is for.' }),
    // The two PIN boxes are a pair, so they wrap together; the action sits on its own line when they
    // do, so a narrow pane never leaves one input behind beside the button.
    h('div', { className: 'flex flex-wrap items-center gap-2' }, [
      h('div', { key: 'pins', className: 'flex items-center gap-2' }, [
        h('input', { key: 'pin', type: 'password', value: pin, inputMode: 'numeric', maxLength: 6,
                     placeholder: 'PIN · 6 digits', autoComplete: 'off',
                     style: fieldProps({ width: '9rem', letterSpacing: '0.3em' }),
                     onChange: (e) => setPin(e.target.value.replace(/\D/g, '').slice(0, 6)) }),
        h('input', { key: 'again', type: 'password', value: again, inputMode: 'numeric', maxLength: 6,
                     placeholder: 'again', autoComplete: 'off',
                     style: fieldProps({ width: '7rem', letterSpacing: '0.3em' }),
                     onChange: (e) => setAgain(e.target.value.replace(/\D/g, '').slice(0, 6)) })
      ]),
      h('input', { key: 'pass', type: 'password', value: pass, autoComplete: 'off',
                   placeholder: change ? 'passphrase or current PIN' : 'wallet passphrase',
                   style: fieldProps({ width: '16rem' }),
                   onChange: (e) => setPass(e.target.value),
                   onKeyDown: (e) => { if (e.key === 'Enter') submit() } }),
      btn(busy ? (change ? 'Changing…' : 'Setting…') : (change ? 'Change PIN' : 'Set PIN'), submit, 'good'),
      change && onCancel ? btn('Cancel', onCancel, 'plain') : null
    ])
  ])
}

/**
 * The passphrase is the key to the keystore, so changing it re-seals the file and nothing else: the
 * master key, the ledger seed, the chain keys and the Monero wallet password all live inside the
 * sealed payload. Typed twice because a typo here is an unrecoverable wallet — the seed is the only
 * backup, and saying so on the card is cheaper than saying it in a support thread.
 */
function PassphraseCard({ busy, onSubmit, onError, onCancel }) {
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [again, setAgain] = useState('')
  const submit = () => {
    if (!current) { onError('type the current passphrase'); return }
    if (next.length < 8) { onError('a passphrase is at least 8 characters'); return }
    if (next !== again) { onError('the two new passphrases are not the same'); return }
    onSubmit({ passphrase: current, new_passphrase: next, confirm: again })
  }
  return h('div', { className: 'rounded-xl border-[3px] p-3 flex flex-col gap-2', style: { borderColor: 'var(--ui-border)' } }, [
    h('div', { className: 'text-[0.875rem] font-medium', children: 'Change the wallet passphrase' }),
    h('div', { className: 'text-[0.75rem] text-(--ui-text-tertiary)', children:
      'This is the key that decrypts the wallet file. The wallet, its keys and its receipts do not change — '
      + 'only what opens them. If you lose the new passphrase the money is only recoverable from your seed, '
      + 'so write it down before you press the button.' }),
    // The inputs wrap as one group, and the actions keep together after them: the fields are a single
    // thought and the buttons act on all of them.
    h('div', { className: 'flex flex-wrap items-center gap-2' }, [
      h('div', { key: 'fields', className: 'flex flex-wrap items-center gap-2' }, [
        h('input', { key: 'cur', type: 'password', value: current, autoComplete: 'off',
                     placeholder: 'current passphrase',
                     style: fieldProps({ width: '14rem' }),
                     onChange: (e) => setCurrent(e.target.value) }),
        h('input', { key: 'new', type: 'password', value: next, autoComplete: 'new-password',
                     placeholder: 'new passphrase (8+ chars)',
                     style: fieldProps({ width: '16rem' }),
                     onChange: (e) => setNext(e.target.value) }),
        h('input', { key: 'again', type: 'password', value: again, autoComplete: 'new-password',
                     placeholder: 'new again',
                     style: fieldProps({ width: '12rem' }),
                     onChange: (e) => setAgain(e.target.value),
                     onKeyDown: (e) => { if (e.key === 'Enter') submit() } })
      ]),
      h('div', { key: 'acts', className: 'flex items-center gap-2' }, [
        btn(busy ? 'Changing…' : 'Change passphrase', submit, 'bad'),
        btn('Cancel', onCancel, 'plain')
      ])
    ])
  ])
}

/* ── deposit QR ──────────────────────────────────────────────────────────── */

// The encoder tops out at version 6, error correction L. Rather than hard-code a byte limit that can
// drift from the encoder's own tables (a wrong constant silently downgraded a URL to a bare address),
// ask the encoder: `qrEncode` returns null when the text does not fit.

/**
 * Shift a decimal string by `decimals` without floating point: `atomic('0.01', 6)` → `10000`.
 * `Number('0.07') * 1e6` is 70000.00000000001, and that lands in the QR as 70000000 units.
 */
function atomic(value, decimals) {
  const text = String(value == null ? '' : value).trim()
  if (!/^\d*(\.\d*)?$/.test(text) || !text || text === '.') return ''
  const [whole, frac = ''] = text.split('.')
  const padded = (frac + '0'.repeat(decimals)).slice(0, decimals)
  if (frac.length > decimals && /[1-9]/.test(frac.slice(decimals))) return ''   // would round
  const digits = (whole + padded).replace(/^0+(?=\d)/, '')
  return digits || '0'
}

function paymentUri(account, amount) {
  const kind = String(account.kind || '')
  const addr = String(account.address || '')
  const amt = String(amount || '').trim()
  const clean = amt && /^\d*\.?\d+$/.test(amt) ? amt : ''
  if (!addr) return ''
  if (kind.startsWith('monero')) {
    // Monero URI (Monero Project): tx_amount in XMR
    return clean ? `monero:${addr}?tx_amount=${clean}` : `monero:${addr}`
  }
  if (kind.startsWith('bitcoin')) {
    // BIP21
    return clean ? `bitcoin:${addr}?amount=${clean}` : `bitcoin:${addr}`
  }
  if (kind.startsWith('xrp')) {
    // XRPL URI: the amount is in drops (1 XRP = 1,000,000 drops)
    const drops = clean ? String(Math.round(Number(clean) * 1e6)) : ''
    return drops ? `ripple:${addr}?amount=${drops}` : `ripple:${addr}`
  }
  if (kind.startsWith('base') || kind.startsWith('polygon')) {
    // EIP-681. The distinction matters: `ethereum:<wallet>` is a *native* transfer, so a USDC row
    // drawn that way funds the wallet with ETH and deposits the wrong asset entirely. A token row
    // needs its contract in the path — the daemon sends it per chain — and without it we draw nothing
    // rather than something wrong.
    const token = (account.tokens || {})[String(account.asset || '').toUpperCase()] || null
    const chain = account.chain_id ? `@${account.chain_id}` : ''
    if (token && token.address) {
      const units = clean ? atomic(clean, Number(token.decimals || 0)) : ''
      return `ethereum:${token.address}${chain}/transfer?address=${addr}`
        + (units ? `&uint256=${units}` : '')
    }
    if (String(account.asset || '').toUpperCase() === String(account.native || '').toUpperCase()
        || ['ETH', 'POL', 'MATIC'].includes(String(account.asset || '').toUpperCase())) {
      const wei = clean ? atomic(clean, 18) : ''
      return `ethereum:${addr}${chain}` + (wei ? `?value=${wei}` : '')
    }
    return ''
  }
  return addr
}

/**
 * Depositing to a main wallet: the address as a code to scan, with an optional amount that goes into
 * the chain's own URI. Deliberately reads nothing and sends nothing — this is an inbound address.
 */
function DepositPanel({ account }) {
  const [amount, setAmount] = useState('')
  const uri = paymentUri(account, amount)
  const fits = Boolean(uri) && qrEncode(uri) !== null
  const text = fits ? uri : String(account.address || '')
  return h('div', { className: 'flex flex-col gap-2 rounded-lg border-[2px] border-(--ui-border) p-2.5' }, [
    h('div', { className: 'flex flex-wrap items-start gap-3' }, [
      h('div', { key: 'q', className: 'rounded-lg p-1', style: { background: '#ffffff' } },
        h(QrCode, { text, size: 148 })),
      h('div', { key: 'c', className: 'flex min-w-0 flex-1 flex-col gap-1.5' }, [
        h('div', { className: 'text-[0.6875rem] uppercase tracking-wide text-(--ui-text-tertiary)',
                   children: fits ? 'scan to deposit' : 'scan the address (this URI is too long for the code)' }),
        h('div', { className: 'select-all break-all rounded-md border-[2px] border-(--ui-border) px-2 py-1 font-mono text-[0.6875rem]',
                   style: { background: 'var(--ui-bg-elevated, var(--ui-bg-secondary, transparent))' }, title: 'the text inside the code',
                   children: text }),
        h('div', { className: 'flex flex-wrap items-center gap-2' }, [
          h('input', {
            key: 'amt', value: amount, inputMode: 'decimal',
            placeholder: `ask for an amount (${account.asset || ''})`,
            title: 'optional: put an amount inside the code so the sender does not have to type it',
            style: fieldProps({ width: '11rem' }),
            onChange: (e) => setAmount(e.target.value)
          }),
          h('button', {
            key: 'cp', type: 'button',
            className: 'rounded-md border-[2px] border-(--ui-border) px-2 py-1 text-[0.6875rem]',
            onClick: () => copy(text, 'deposit-' + account.kind),
            children: 'copy what the code says'
          })
        ]),
        h('div', { className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children:
          'This is a receiving address: showing it cannot move anything. Anyone with it can send to you.' })
      ])
    ])
  ])
}

/* ── page ───────────────────────────────────────────────────────────────── */

function WalletPage({ ctx }) {
  const [state, setState] = useState({
    status: null, accounts: [], requests: [], mandates: [], sandboxes: [], contacts: [], ledger: [],
    spend: null, deposits: [],
    walletsKnown: null,   // null until /status lands; the setup card waits for the answer
    // true until the first /status + /balances settle: the pane used to claim "no balances yet"
    // while it was still loading, which reads as an empty wallet.
    loading: true,
    accountsStale: false,
    error: null, note: null, bridge: false, busy: false
  })
  const [passphrase, setPassphrase] = useState('')
  const [passphrase2, setPassphrase2] = useState('')      // confirmation on the first-run form
  // The locked gate: one field, nothing else on screen. `gatePass` reveals the passphrase fallback
  // for someone who has forgotten the six digits (or whose PIN is locked out).
  const [gatePass, setGatePass] = useState(false)
  const [gatePassphrase, setGatePassphrase] = useState('')
  // A payment is approved by the shared ApprovalActions component, in the prompt card if the agent
  // raised one and in the pending list otherwise. There is no page-wide passphrase field and no
  // per-row accept state: the passphrase belongs to the payment being approved, not to the section.
  // A receive address is shown only while the operator asks for it: one reveal per row, cleared when
  // this page goes away or the window is left (a beholder of the screen should not inherit it).
  const [revealed, setRevealed] = useState({})
  // Deposit codes are per row and collapse with the page, like the addresses they show.
  const [depositOpen, setDeposit] = useState({})
  const [copied, setCopied] = useState('')

  // A labelled column for forms: the label says what the number means, which "per tx" never did.
  const labelled = (key, text, control) => h('div', { key, className: 'flex flex-col gap-0.5' }, [
    h('span', { key: 'l', className: 'text-[0.6875rem] uppercase tracking-wide text-(--ui-text-tertiary)',
                children: text }),
    control
  ])
  const [confirmDelete, setConfirmDelete] = useState(null)
  const [confirmClear, setConfirmClear] = useState(false)
  const [mandatePass, setMandatePass] = useState('')
  const [sandboxPass, setSandboxPass] = useState('')
  const [sandboxPin, setSandboxPin] = useState('')
  const [payeeDraft, setPayeeDraft] = useState({})
  const [depositDraft, setDepositDraft] = useState('')
  const [contactPass, setContactPass] = useState('')      // saving here blesses the address: the daemon asks for it
  const [confirmContact, setConfirmContact] = useState(null)
  const [mandateDraft, setMandateDraft] = useState({ payee: '', per_tx: '', period_cap: '',
                                                     interval: '1d', label: '', kind: '' })
  const [draft, setDraft] = useState({ kind: '', asset: '', amount: '', payee: '', why: '' })
  const [showAllReceipts, setShowAllReceipts] = useState(false)
  const [sandboxDraft, setSandboxDraft] = useState({ kind: 'monero-mainnet', asset: 'XMR', budget: '', hours: '24', label: '', profiles: [], payees: '', oneTime: false, destruct: '1800' })
  const [profiles, setProfiles] = useState([])          // profiles that actually exist on this machine
  const [hiddenAssets, setHiddenAssets] = useState([])
  const [showCoins, setShowCoins] = useState(false)   // the wallet-wide coin menu
  const [showPinChange, setShowPinChange] = useState(false)  // the change-PIN card, closed by default
  const [showPassChange, setShowPassChange] = useState(false) // the change-passphrase card, closed by default
  const [profileDraft, setProfileDraft] = useState({})
  const [contactDraft, setContactDraft] = useState({ name: '', kind: 'monero-mainnet', address: '', notes: '' })
  const [showReceive, setShowReceive] = useState(false)   // the receive card, closed by default
  const [receiveLabel, setReceiveLabel] = useState('')
  // The amount the order is for. It was only settable from the agent's tool call, so an order
  // opened from this pane could never say "paid" or "partial" — it could only ever say "waiting"
  // with no target. Optional: an open-ended order is a legitimate thing to want.
  const [receiveAddr, setReceiveAddr] = useState(null)    // { address, label } from POST /receive

  useEffect(() => {
    try {
      Promise.resolve(ctx.storage.get('wallet.hiddenAssets'))
        .then((v) => { if (Array.isArray(v)) setHiddenAssets(v) })
        .catch(() => {})
    } catch (err) { /* storage is optional */ }
  }, [ctx])

  const toggleAsset = (asset) => {
    setHiddenAssets((prev) => {
      const next = prev.includes(asset) ? prev.filter((a) => a !== asset) : [...prev, asset]
      try { ctx.storage.set('wallet.hiddenAssets', next) } catch (err) { /* optional */ }
      return next
    })
  }

  // Derived BEFORE any effect/closure reads them: a later `const` is in the temporal dead zone
  // inside an effect's dependency array, which throws "cannot access before initialization".
  const chains = (state.status && state.status.wallet && state.status.wallet.chains) || []
  const assets = (state.status && state.status.wallet && state.status.wallet.assets) || []
  const wallet = (state.status && state.status.wallet) || {}
  // Unlocking takes the same six digits that authorise a payment; the passphrase still works, and is
  // the only way in when no PIN is set (or the PIN is locked out). These live here, above every use:
  // declared next to the unlock bar they were in the temporal dead zone for a *locked* wallet and threw
  // `Cannot access 'pinSet' before initialization` — an empty pane, which the layout audit scored as
  // "no findings" because an empty page has no overflow.
  // `unlocks` is whether the PIN actually opens the file; `set` only says a PIN exists. They differ
  // for one release after a v2 keystore upgrade. A null answer (an older daemon, or a keystore read
  // that failed) must mean "ask for the PIN" when one is required — treating it as "no PIN" is what
  // sent six digits out as a passphrase and made the PIN look broken after an auto-lock.
  const pinAnswered = wallet.pin
    ? (wallet.pin.unlocks === undefined ? wallet.pin.set : wallet.pin.unlocks)
    : null
  const pinSet = Boolean(wallet.pin && ((pinAnswered === null || pinAnswered === undefined)
    ? wallet.pin.required !== false
    : pinAnswered))
  const unlockWith = (value) => act('/unlock',
    (pinSet && /^\d{6}$/.test(String(value || '').trim())) ? { pin: String(value).trim() }
      : { passphrase: value },
    'wallet unlocked')
  // The daemon decides which coins a chain holds and which one it exists for; the local tables are
  // only a fallback for the first render before /status lands. This is why picking XRP no longer
  // leaves the asset on XMR.
  const chainAssets = (kind) => (wallet.chain_assets && wallet.chain_assets[kind])
    || CHAIN_ASSETS[kind] || []
  const nativeAsset = (kind) => (wallet.native_asset && wallet.native_asset[kind])
    || chainAssets(kind)[0] || 'XMR'
  // coin -> its default network, and every network that holds it (only the enabled ones)
  const defaultChain = (asset) => (wallet.default_chain && wallet.default_chain[asset])
    || (wallet.asset_chains && wallet.asset_chains[asset] || [])[0] || ''
  const chainsFor = (asset) => ((wallet.asset_chains && wallet.asset_chains[asset]) || [])
    .filter((c) => chains.includes(c))

  // The spend figure is read at render (spendAssets, below), from the poll that stores it, so the
  // daily-cap row reflects the current state rather than the one it had at mount.
  const send = useCallback(async (path, body, method) => {
    const verb = method || (body === undefined ? 'GET' : 'POST')
    if (verb === 'GET') return ctx.rest(path)
    try {
      return await ctx.rest(path, { method: verb, body })
    } catch (err) {
      // The JSON.stringify retry exists for one failure: the transport did not accept an object
      // body. Retrying anything else risks filing the same payment twice - a timeout means the
      // request may well have gone through.
      const text = String((err && err.message) || err)
      // only the transport refusing an object body is safe to retry: a timeout means the request may
      // already have been acted on, and a 200-with-HTML bridge page is not a shape problem at all
      const shape = /\b(422|unprocessable entity)\b/i.test(text)
      if (!shape) throw err
      return await ctx.rest(path, { method: verb, body: JSON.stringify(body) })
    }
  }, [ctx])

  const copy = useCallback(async (text, key) => {
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text)
      } else {
        const area = document.createElement('textarea')
        area.value = text; document.body.appendChild(area); area.select()
        document.execCommand('copy'); document.body.removeChild(area)
      }
      setCopied(key)
      setTimeout(() => setCopied((cur) => (cur === key ? '' : cur)), 1600)
    } catch (err) {
      setState((s) => ({ ...s, error: 'could not copy: ' + String((err && err.message) || err) }))
    }
  }, [])

  useEffect(() => {
    // leaving the GUI (tab hidden, app backgrounded) hides every address again
    if (typeof document === 'undefined' || !document.addEventListener) return undefined
    const hide = () => { if (document.visibilityState === 'hidden') { setRevealed({}); setCopied('') } }
    document.addEventListener('visibilitychange', hide)
    return () => document.removeEventListener('visibilitychange', hide)
  }, [])

  // `refreshOnce({fresh:true})` is the human pressing Refresh: skip the balance cache. The polled
  // `refresh()` reads the cache so a tick never pays the per-chain network cost.
  const refreshOnce = useCallback(async (opts) => {
    const fresh = Boolean(opts && opts.fresh)
    try {
      const status = await send('/status')
      // /incoming and /spend read the ledger, which a locked wallet does not have open. Asking for
      // them every 3s produced a 503 per poll in the gateway log for as long as the wallet sat
      // locked; the pane showed nothing anyway. Ask only when there is something to read.
      const lockedNow = Boolean(status && status.wallet && status.wallet.locked)
      // walletsKnown is read at render to decide the first run; leaving it out of state meant the
      // setup card never went away for a wallet created from this very pane.
      const walletsKnown = Boolean(status && status.wallet && status.wallet.wallets)
      const [accountsRes, requests, mandates, sandboxes, contacts, ledger, profs, spend, deposits] = await Promise.all([
        // null, not an empty list: a failed read must not be painted as "this wallet holds nothing"
        send(fresh ? '/balances?fresh=1' : '/balances').catch(() => null),
        send('/requests').catch(() => ({ requests: [] })),
        send('/mandates').catch(() => ({ mandates: [] })),
        send('/sandboxes').catch(() => ({ sandboxes: [] })),
        send('/contacts').catch(() => ({ contacts: [] })),
        send('/ledger?limit=40').catch(() => ({ entries: [] })),
        send('/profiles').catch(() => ({ profiles: [] })),
        // Added after the first release: on a backend that predates them these 404 and the pane
        // simply shows nothing, never an error banner about a route the operator cannot fix.
        lockedNow ? null : send('/spend').catch(() => null),
        // a route that a pre-0.4 backend does not have: 404 → the section simply does not render
        send('/deposits').catch(() => null)
      ])
      // /sandboxes carries the profile list. The dashboard imports a plugin's routes once at
      // startup, so a route added later 404s until the backend restarts - and then the picker had
      // nothing to show. This way the list arrives on a route that predates it.
      // the daemon nests everything under wallet; and it is read from /status first because that
      // route exists on every backend version, while /profiles only exists after a restart
      setProfiles((status.wallet && status.wallet.profiles) || sandboxes.profiles || profs.profiles || [])
      // Shape-check before trusting either one: a half-implemented route must not paint a section.
      const spendOk = spend && typeof spend.assets === 'object' && spend.assets !== null
      const accountsOk = accountsRes && Array.isArray(accountsRes.accounts)
      setState((s) => ({
        ...s, status, bridge: false, loading: false,  // an error is cleared by the next action, not by a poll
        walletsKnown,
        accounts: accountsOk ? accountsRes.accounts : s.accounts,
        accountsStale: !accountsOk,
        requests: requests.requests || [],
        mandates: mandates.mandates || [], sandboxes: sandboxes.sandboxes || [],
        contacts: contacts.contacts || [], ledger: ledger.entries || [],
        // deposits was destructured and never stored: the deposit list could not render, and the
        // ?.catch(() => null) above lost the "not loaded" case to an empty array it then showed as
        // "No deposit addresses" — a lie about a route that simply failed.
        deposits: deposits && Array.isArray(deposits.deposits) ? deposits.deposits : s.deposits,
        spend: spendOk ? spend : null
      }))
    } catch (err) {
      const message = String((err && err.message) || err)
      const missing = /\b404\b|not found/i.test(message)
      // loading is cleared even on failure: a spinner that never stops is a worse lie than an error
      setState((s) => ({ ...s, bridge: missing, loading: false, error: missing ? null : message }))
    }
  }, [send])

  // the polled read: same path, cache allowed
  const refresh = useCallback(() => refreshOnce({ fresh: false }), [refreshOnce])

  useEffect(() => {
    refresh()
    // Polling is the pane's whole cost: every tick is 6 HTTP round trips. 5s instead of 3s is still
    // faster than a human notices, and a background window asks for nothing at all (the app hides the
    // page when it is not visible), which is the difference between idle CPU and none.
    const tick = () => {
      if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return
      refresh()
    }
    ctx.setInterval(tick, 5000)
  }, [refresh])

  useEffect(() => {
    if (chains.length && !mandateDraft.kind) {
      const first = chains.find((c) => c.startsWith('monero')) || chains[0]
      setMandateDraft((d) => ({ ...d, kind: first }))
    }
  }, [chains.join(','), mandateDraft.kind])

  // Seed the request form's selects once the wallet reports its chains (never during render).
  useEffect(() => {
    if (chains.length && !draft.kind) {
      setDraft((d) => ({ ...d, kind: chains[0], asset: assets[0] || 'XMR' }))
    }
  }, [chains.join(','), assets.join(','), draft.kind])

  const act = async (path, body, note) => {
    setState((s) => ({ ...s, busy: true }))
    try {
      const result = await send(path, body || {})
      setPassphrase('')
      // A refusal is a 200 with decision.allow=false: reporting "created" for one sends the viewer to
      // an empty pending list that says the opposite of what just happened.
      const decision = (result && result.decision) || {}
      if (decision.allow === false) {
        setState((s) => ({ ...s, error: String(decision.reason || decision.code || 'refused'),
                           note: null, busy: false }))
        await refresh()
        return result
      }
      setState((s) => ({ ...s, note: note || null, error: null, busy: false }))
      await refresh()
      return result
    } catch (err) {
      setState((s) => ({ ...s, error: String((err && err.message) || err), note: null, busy: false }))
      return null
    }
  }

  // A fresh receiving address: an explicit action with its own error, kept on screen until the next
  // one — the panel must not look like it worked when the backend refused.
  const newAddress = async () => {
    setState((s) => ({ ...s, busy: true, error: null, note: null }))
    try {
      const res = await send('/receive', { label: receiveLabel })
      if (!res || !res.address) throw new Error('the wallet did not return an address')
      setReceiveAddr({ address: res.address, label: res.label || receiveLabel })
      setState((s) => ({ ...s, busy: false, note: 'a fresh receiving address is ready', error: null }))
      await refresh()
    } catch (err) {
      setState((s) => ({ ...s, busy: false, note: null, error: String((err && err.message) || err) }))
    }
  }

  const running = Boolean(state.status && state.status.running)
  const locked = wallet.locked !== false
  const pending = state.requests.filter((r) => r.status === 'pending')
  const activeMandates = state.mandates.filter((m) => m.status === 'active')

  const visibleAccounts = state.accounts.filter((a) => !hiddenAssets.includes(a.asset))

  const header = h('div', { key: 'head', className: 'flex flex-wrap items-center gap-2 border-b border-(--ui-border) pb-3' }, [
    h('div', { key: 'title', className: 'flex items-center gap-2' }, [
      h('span', { key: 'g', className: 'text-[1.125rem] leading-none text-(--ui-text-secondary)',
                  children: '\u25c8' }),
      h('span', { key: 't', className: 'text-base font-medium', children: 'Wallet' })
    ]),
    // Before /status lands the pane knows nothing: it used to assert "locked · signer offline ·
    // passphrase off" from defaults, which is a verdict on a wallet it had not looked at yet.
    pill(state.loading ? 'checking…' : locked ? 'locked' : 'unlocked',
         state.loading ? 'flat' : locked ? 'warn' : 'good'),
    !state.loading && !locked && wallet.unlock_expires_in != null
      ? pill(`locks in ${Math.max(1, Math.round(wallet.unlock_expires_in / 60))}m`, 'flat')
      : null,
    state.loading ? null : pill(running ? 'signer ready' : 'signer offline', running ? 'good' : 'bad'),
    !locked && wallet.autolock_minutes != null
      ? h('span', { key: 'al', className: 'flex items-center gap-1 text-[0.6875rem] text-(--ui-text-tertiary)' }, [
          h('span', { key: 'l', children: 'auto-lock' }),
          h('select', {
            key: 's', value: String(wallet.autolock_minutes),
            style: fieldProps({ padding: '2px 6px', fontSize: '11px', minHeight: '0' }),
            title: 'how long the wallet stays unlocked without you',
            onChange: (e) => act('/config', { autolock_minutes: Number(e.target.value), passphrase },
                                 `auto-lock ${Number(e.target.value) ? Number(e.target.value) + ' min' : 'off'}`)
          }, [['1', '1m'], ['5', '5m'], ['15', '15m'], ['30', '30m'], ['60', '1h'], ['0', 'off']]
              .map(([v, label]) => opt(v, label)))
        ])
      : null,
    // The PIN is the one secret a person has to remember and cannot look up, so the control to change
    // it belongs on the page rather than in a CLI. Hidden while locked: the daemon refuses `set_pin`
    // without an unlocked keystore, and a button that always fails is worse than no button.
    !locked && !state.loading && wallet.pin
      ? h('button', {
          key: 'pin-btn', type: 'button',
          className: 'flex items-center gap-1 rounded-full border-[2px] border-(--ui-border) px-2 py-0.5 text-[0.6875rem]',
          style: { minHeight: '28px' },
          title: wallet.pin.set === false
            ? 'no PIN is set — payments cannot be approved until one is'
            : 'change the six-digit PIN that authorises a payment',
          onClick: () => setShowPinChange((v) => !v),
          children: wallet.pin.set === false ? 'PIN · not set' : 'PIN · change'
        })
      : null,
    !locked && !state.loading
      ? h('button', {
          key: 'pass-btn', type: 'button',
          className: 'flex items-center gap-1 rounded-full border-[2px] border-(--ui-border) px-2 py-0.5 text-[0.6875rem]',
          style: { minHeight: '28px' },
          title: 'change the passphrase that decrypts this wallet',
          onClick: () => setShowPassChange((v) => !v),
          children: 'passphrase · change'
        })
      : null,
    h('div', { key: 'coins', className: 'relative' }, [
      h('button', {
        key: 'btn', type: 'button',
        className: 'flex items-center gap-1 rounded-full border-[2px] border-(--ui-border) px-2 py-0.5 text-[0.6875rem]',
        style: { minHeight: '28px' },
        onClick: () => setShowCoins((v) => !v),
        title: 'which coins this wallet holds',
        children: [
          // count what is actually listed: the daemon's enabled assets and the rows can disagree
          // while a balance fetch is in flight, and "coins 3" above two rows reads as a bug
          h('span', { key: 'l',
                      children: `coins ${new Set(state.accounts.map((a) => a.asset)).size
                        || (wallet.assets || []).length}` }),
          h('span', { key: 'c', children: showCoins ? '▴' : '▾' })
        ]
      }),
      showCoins
        ? h('div', {
            key: 'panel',
            className: 'absolute right-0 top-full z-30 mt-1 flex w-64 flex-col gap-1 rounded-lg border-[2px] border-(--ui-border) p-2 shadow-xl',
            style: { backgroundColor: SELECT_FACE, color: SELECT_INK }
          }, [
            h('div', { key: 'h', className: 'text-[0.6875rem] uppercase tracking-wide text-(--ui-text-tertiary)',
                       children: 'this wallet holds' }),
            ...(wallet.available_assets || wallet.assets || []).map((asset) => {
              const on = (wallet.assets || []).includes(asset)
              const chains = (wallet.asset_chains || {})[asset] || []
              return h('button', {
                key: asset, type: 'button',
                className: 'flex items-center gap-2 rounded-md px-2 py-1 text-left text-[0.8125rem]',
                style: {
                  border: `2px solid ${on ? (COIN[asset] || {}).color || 'var(--accent, #6366f1)' : 'transparent'}`,
                  background: on ? `color-mix(in srgb, ${(COIN[asset] || {}).color || '#6366f1'} 14%, transparent)` : 'transparent',
                  color: on ? 'var(--foreground, #eef1f6)' : 'var(--ui-text-tertiary)'
                },
                onClick: async () => {
                  const current = wallet.assets || []
                  // A stale poll can make `on` wrong for one frame; the daemon dedupes, but send a
                  // set anyway so the request never carries the same coin twice.
                  const next = on
                    ? current.filter((a) => a !== asset)
                    : Array.from(new Set([...current, asset]))
                  if (!next.length) {
                    setState((s) => ({ ...s, error: 'keep at least one coin enabled' }))
                    return
                  }
                  await act('/config', { assets_enabled: next },
                            `${asset} ${on ? 'removed from' : 'added to'} this wallet`)
                  // the menu is a picker, not a dashboard: close it once the choice is made
                  setShowCoins(false)
                },
                children: [
                  CoinIcon({ key: 'i', asset, size: 16 }),
                  h('span', { key: 'n', className: 'flex-1', children: asset }),
                  null,
                  h('span', { key: 's', children: on ? '✓' : '+' })
                ]
              })
            }),
            h('div', { key: 'n', className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
                       children: 'Adding a coin adds its chain, an address and a balance row; removing one drops them and cancels anything waiting on it.' })
          ])
        : null
    ]),
    state.loading ? null : pill(wallet.human_gate ? 'passphrase on' : 'passphrase off',
                                wallet.human_gate ? 'flat' : 'warn'),
    pending.length ? pill(`${pending.length} pending`, 'warn') : null,
    h('div', { key: 'sp', className: 'flex-1' }),
    state.busy ? h('span', { key: 'b', className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: 'working…' }) : null,
    btn(showReceive ? 'Close receive' : 'Receive', () => setShowReceive((v) => !v), 'good'),
    btn('Refresh', () => refreshOnce({ fresh: true })),
    btn('Verify receipts', async () => {
      try {
        const res = await send('/verify', {})
        setState((s) => ({ ...s, note: res.ok ? `receipt chain ok · ${res.entries} entries` : `VERIFY FAILED: ${res.message}` }))
      } catch (err) {
        setState((s) => ({ ...s, error: String((err && err.message) || err) }))
      }
    })
  ])

  const requestForm = h('div', { key: 'new', className: 'rounded-lg border-[3px] border-(--ui-border) p-3 flex flex-wrap items-end gap-2' }, [
    h('div', { key: 'a', className: 'flex flex-col gap-0.5' }, [
      h('span', { key: 'l', className: 'text-[0.6875rem] uppercase tracking-wide text-(--ui-text-tertiary)', children: 'coin' }),
      h('select', {
        key: 's', value: draft.asset, style: fieldProps(),
        onChange: (e) => {
          const asset = e.target.value
          // a coin arrives with its default network; the network can be changed next to it
          setDraft((d) => ({ ...d, asset, kind: defaultChain(asset) || d.kind }))
        }
      }, (assets.length ? assets : Object.keys(wallet.asset_chains || {}))
        .map((a) => opt(a, a)))
    ]),
    h('div', { key: 'k', className: 'flex flex-col gap-0.5' }, [
      h('span', { key: 'l', className: 'text-[0.6875rem] uppercase tracking-wide text-(--ui-text-tertiary)', children: 'network' }),
      h('select', {
        key: 's', value: draft.kind, style: fieldProps(),
        onChange: (e) => setDraft((d) => ({ ...d, kind: e.target.value }))
      }, (chainsFor(draft.asset).length ? chainsFor(draft.asset) : [draft.kind])
        .map((c) => opt(c, chainName(c))))
    ]),
    h('div', { key: 'amt', className: 'flex flex-col gap-0.5' }, [
      h('span', { key: 'l', className: 'text-[0.6875rem] uppercase tracking-wide text-(--ui-text-tertiary)', children: 'amount' }),
      h('input', {
        key: 'i', value: draft.amount, placeholder: '0.01', inputMode: 'decimal',
        style: fieldProps({ width: '6rem' }),
        onChange: (e) => setDraft((d) => ({ ...d, amount: e.target.value }))
      })
    ]),
    (state.sandboxes.filter((s) => s.status !== 'destroyed').length
      ? h('div', { key: 'sb', className: 'flex flex-col gap-0.5' }, [
          h('span', { key: 'l', className: 'text-[0.6875rem] uppercase tracking-wide text-(--ui-text-tertiary)', children: 'from sandbox' }),
          h('select', { key: 's', value: draft.sandbox_id || '',
            style: fieldProps({ paddingRight: '1.6rem' }),
            onChange: (e) => setDraft((d) => ({ ...d, sandbox_id: e.target.value }))
          }, [
            opt('', 'my wallet (needs approval)'),
            // A destroyed sandbox cannot spend: offering one here would only produce a refused
            // request, so it is left out of the menu rather than labelled.
            ...state.sandboxes.filter((s) => s.status !== 'destroyed').map((s) => opt(
              s.sandbox_id,
              `${s.label || s.sandbox_id} · ${fmt(s.remaining, s.asset)} ${s.asset} left${s.enabled ? '' : ' (unfunded)'}`))
          ])
        ])
      : null),
    h('div', { key: 'p', className: 'flex flex-col gap-0.5 flex-1 min-w-[16rem]' }, [
      h('span', { key: 'l', className: 'text-[0.6875rem] uppercase tracking-wide text-(--ui-text-tertiary)', children: 'payee' }),
      h('input', {
        key: 'i', value: draft.payee, placeholder: 'address',
        style: fieldProps({ fontFamily: MONO }),
        onChange: (e) => setDraft((d) => ({ ...d, payee: e.target.value }))
      })
    ]),
    h('div', { key: 'w', className: 'flex flex-col gap-0.5 flex-1 min-w-[12rem]' }, [
      h('span', { key: 'l', className: 'text-[0.6875rem] uppercase tracking-wide text-(--ui-text-tertiary)', children: 'why' }),
      h('input', {
        key: 'i', value: draft.why, placeholder: 'what is this for',
        style: fieldProps(),
        onChange: (e) => setDraft((d) => ({ ...d, why: e.target.value }))
      })
    ]),
    btn('Request payment', async () => {
      if (!draft.amount || !draft.payee) {
        setState((s) => ({ ...s, error: 'amount and payee are required' }))
        return
      }
      // one key per submission: a transport retry reuses this exact body, a deliberate second
      // payment is a new submission with a new key
      const key = (crypto && crypto.randomUUID) ? crypto.randomUUID() : String(Date.now()) + Math.random()
      await act('/request', { ...draft, idempotency_key: key },
                'request created — it is waiting in pending approvals')
      setDraft((d) => ({ ...d, amount: '', payee: '', why: '' }))
    }, 'good')
  ])

  // A wallet exists once an address was ever created for that chain (the daemon reports it in
  // status.wallets, which survives a lock). Older daemons have no such key: fall back to the address
  // on the balance row, and prefer showing nothing over nagging someone who already has a wallet.
  // Hide the offer as soon as *any* wallet exists: telling someone with a funded BTC wallet to
  // "create a wallet" is the same mistake as telling someone with Monero.
  const noWalletAtAll = state.walletsKnown
    ? !Object.values(state.status.wallet.wallets || {}).some(Boolean)
    : !state.accounts.some((a) => a.address)
  // `running` is false precisely when there is no keystore, so requiring it hid the form that creates
  // one: a fresh profile's pane said "start the daemon" under a no_keystore banner with no way in.
  const firstRun = Boolean(state.status) && state.status.wallet
    ? state.status.wallet.initialized === false
    : false
  // Creating the keystore also unlocks it: asking for the same secret twice in a row is how a
  // first run gets abandoned. `act` clears the field after a call, so the value is captured first.
  const createWallet = async () => {
    if (!passphrase || passphrase.length < 8) {
      setState((s) => ({ ...s, error: 'a passphrase of at least 8 characters is required' }))
      return
    }
    if (passphrase !== passphrase2) {
      setState((s) => ({ ...s, error: 'the two passphrases do not match' }))
      return
    }
    const secret = passphrase
    const made = await act('/keystore/create', { passphrase: secret }, 'wallet created — unlocking')
    if (made) await act('/unlock', { passphrase: secret }, 'wallet ready — now set a PIN')
  }
  // First run is a three-step setup, not a lone field: the passphrase is unrecoverable, so the
  // screen has to say what it is for before asking for it, and the PIN that gates every payment
  // comes straight after.
  const unlockBar = firstRun
    ? h('div', { key: 'first-run', className: 'rounded-xl border-[3px] border-(--ui-border) p-4 flex flex-col gap-3' }, [
        h('div', { key: 'h', className: 'text-[0.9375rem] font-semibold', children: 'Set up your agent wallet' }),
        h('div', { key: 'n', className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
                   children: 'The agent can ask to spend; it can never approve or hold a key. You hold two secrets.' }),
        h('ol', { key: 's', className: 'text-[0.6875rem] text-(--ui-text-tertiary) flex flex-col gap-1',
                  style: { listStyle: 'decimal', paddingLeft: '1.1rem' } }, [
          h('li', { key: '1', children: 'A passphrase encrypts the keys. There is no recovery — write it down now.' }),
          h('li', { key: '2', children: 'Next you set a 6-digit PIN — it unlocks the pane and authorises each payment.' }),
          h('li', { key: '3', children: 'Fund the addresses it shows. Start with a small amount.' })
        ]),
        h('div', { key: 'r', className: 'flex flex-wrap items-center gap-2' }, [
          h('input', {
            key: 'pw', type: 'password', value: passphrase, placeholder: 'new passphrase (min 8)',
            autoComplete: 'new-password', style: fieldProps({ width: '14rem' }),
            onChange: (e) => setPassphrase(e.target.value)
          }),
          h('input', {
            key: 'pw2', type: 'password', value: passphrase2, placeholder: 'type it again',
            autoComplete: 'new-password', style: fieldProps({ width: '14rem' }),
            onChange: (e) => setPassphrase2(e.target.value),
            onKeyDown: (e) => { if (e.key === 'Enter') createWallet() }
          }),
          btn('Create wallet', createWallet, 'good')
        ])
      ])
    : locked && running
    ? h('div', { key: 'unlock', className: 'rounded-lg border-[3px] border-(--ui-border) p-3 flex flex-wrap items-center gap-2' }, [
        h('span', { key: 'l', className: 'text-xs text-(--ui-text-tertiary)', children:
          pinSet ? 'PIN — typed here, never sent to the agent' : 'Passphrase — typed here, never sent to the agent' }),
        h('div', { key: 'sp', className: 'flex-1' }),
        h('input', {
          key: 'pw', type: 'password', value: passphrase,
          placeholder: pinSet ? '6-digit PIN' : 'keystore passphrase',
          inputMode: pinSet ? 'numeric' : 'text',
          autoComplete: 'off',
          style: fieldProps({ width: '14rem' }),
          onChange: (e) => setPassphrase(e.target.value),
          onKeyDown: (e) => { if (e.key === 'Enter') unlockWith(passphrase) }
        }),
        btn('Unlock', () => unlockWith(passphrase), 'good')
      ])
    : null

  // Receive: one address at a time, on screen only while asked for. The QR is drawn from this file
  // (see qrEncode) so nothing about an address leaves the app to be rendered.
  const receiveCard = showReceive
    ? h('div', {
        key: 'receive',
        className: 'rounded-xl border-[3px] border-(--ui-border) p-3 flex flex-col gap-3',
        style: { background: 'var(--wallet-surface)' }
      }, [
        h('div', { key: 'h', className: 'flex flex-col gap-0.5' }, [
          h('span', { key: 't', className: 'text-[0.75rem] font-semibold uppercase tracking-[0.08em]',
                      children: 'Receive' }),
          h('span', { key: 'n', className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
                      children: 'Each request gets its own address (a new account index), so a payment can be matched to what it was for.' })
        ]),
        h('div', { key: 'row', className: 'flex flex-wrap items-end gap-2' }, [
          h('div', { key: 'l', className: 'flex flex-1 min-w-[12rem] flex-col gap-0.5' }, [
            h('span', { key: 'k', className: 'text-[0.6875rem] uppercase tracking-wide text-(--ui-text-tertiary)',
                        children: 'label' }),
            h('input', {
              key: 'i', value: receiveLabel, placeholder: 'who or what this is for',
              style: fieldProps(),
              onChange: (e) => setReceiveLabel(e.target.value),
              onKeyDown: (e) => { if (e.key === 'Enter') newAddress() }
            })
          ]),
          btn(state.busy ? 'working…' : 'new address', newAddress, 'good')
        ]),
        receiveAddr
          ? h('div', { key: 'out', className: 'flex flex-col gap-3 sm:flex-row sm:items-start' }, [
              h('div', { key: 'col', className: 'flex min-w-0 flex-1 flex-col gap-1' }, [
                receiveAddr.label
                  ? h('div', { key: 'lb', className: 'text-[0.6875rem] uppercase tracking-wide text-(--ui-text-tertiary)',
                               children: receiveAddr.label })
                  : null,
                h('div', {
                  key: 'a', className: 'select-all break-all rounded-md border-[2px] border-(--ui-border) px-2 py-1 font-mono text-[0.6875rem]',
                  style: { background: 'var(--ui-bg-elevated, var(--ui-bg-secondary, transparent))' },
                  title: 'click to copy',
                  onClick: () => copy(receiveAddr.address, 'receive'),
                  children: receiveAddr.address
                }),
                h('div', { key: 'c', className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
                           children: copied === 'receive'
                             ? 'copied ✓'
                             : 'click the address to copy it — the code is the same address' })
              ]),
              h('div', { key: 'q', className: 'rounded-lg p-1', style: { background: '#ffffff' } },
                h(QrCode, { text: receiveAddr.address, size: 156 }))
            ])
          : h('div', { key: 'empty', className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
                       children: 'Nothing is shown until you ask for an address — then it appears here with a code to scan.' })
      ])
    : null

  // The agent cannot ask for a passphrase in chat, so it raises a prompt here. The daemon nests it
  // inside `wallet` (walletd/state.py status() → "prompt"), and this read used to look only at the
  // top level of /status — which is {ok, wallet, running}, so the card never appeared at all and a
  // pending payment had no prompt to be approved from. Keep the top-level read as a fallback for
  // any older shape.
  const prompt = (state.status && (state.status.prompt || wallet.prompt)) || null
  const promptRequest = prompt && prompt.request_id
    ? state.requests.find((r) => r.request_id === prompt.request_id)
    : null
  // Read from the contacts the pane already loaded: an agent-supplied payee that is not in the book
  // is exactly the case a human should be told about before approving, not after.
  const promptPayeeName = promptRequest ? knownPayee(state.contacts, promptRequest.payee) : ''
  // an offer past its window is still drawn — but it must not read as something you can approve
  const promptExpired = Boolean(promptRequest && promptRequest.approval_expires_at
    && Date.parse(promptRequest.approval_expires_at) < Date.now())
  // The PIN is the gate on every payment. `wallet.pin` is absent on an older daemon, and a missing
  // answer must not silently fall back to the weaker passphrase path: default to asking for the PIN.
  const pinRequired = () => !(wallet.pin && wallet.pin.required === false)
  // One unlock from the prompt, with the same empty-field guard as an approval: pressing Unlock on
  // an empty field used to send an empty string and paint the daemon's refusal over the card.
  const unlockFromPrompt = () => {
    if (!passphrase) {
      setState((s) => ({ ...s, error: 'type the passphrase first — nothing was sent', note: null }))
      return
    }
    act('/unlock', { passphrase }, 'wallet unlocked')
  }
  const signerMissing = state.status && state.status.wallet && state.status.wallet.initialized
    && state.status.wallet.signer_installed === false
  const signerCard = signerMissing
    ? h('div', {
        key: 'signer-missing',
        className: 'rounded-xl border-[3px] border-(--ui-border) p-3 flex flex-col gap-2',
        style: { background: 'var(--wallet-surface)' }
      }, [
        h('div', { key: 'h', className: 'text-[0.75rem] font-semibold uppercase tracking-[0.08em]',
                   children: 'the Monero signer is not installed' }),
        h('div', { key: 'n', className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
                   children: 'Without it there is no XMR address and no signing. It is one binary (~30 MB), downloaded from the Monero project and verified against a SHA256 pinned in the plugin.' }),
        h('div', { key: 'b', className: 'flex gap-2' }, [
          btn('Install signer', () => act('/signer/install', {}, 'signer installed — unlock to load the wallet'), 'good')
        ])
      ])
    : null

  // No PIN yet (a wallet created before the gate, or a first run): approving is impossible until one
  // exists, so the pane puts the form in front of the human instead of failing at the click.
  // Two reasons this form is on screen: no PIN exists yet (approving is impossible until one does, so
  // the pane puts the form in front of the human rather than failing at the click), or the operator
  // asked to change it. The daemon has always supported the second — `set_pin` replaces the hash and
  // writes a `pin_changed` receipt — but the pane only ever offered the first, so a PIN could be set
  // once and never changed from the UI.
  const pinSetupCard = (wallet.pin && wallet.pin.set === false && !wallet.locked)
    ? h(PinSetupCard, {
        busy: state.busy,
        onSet: (body) => act('/pin/set', body, 'PIN set — every payment will ask for it'),
        onError: (message) => setState((s) => ({ ...s, error: message, note: null }))
      })
    : (showPinChange && !wallet.locked)
      ? h(PinSetupCard, {
          mode: 'change',
          busy: state.busy,
          onCancel: () => setShowPinChange(false),
          onSet: async (body) => {
            const result = await act('/pin/set', body, 'PIN changed — the old one no longer works')
            if (result) setShowPinChange(false)
          },
          onError: (message) => setState((s) => ({ ...s, error: message, note: null }))
        })
      : null

  const passphraseCard = (showPassChange && !wallet.locked)
    ? h(PassphraseCard, {
        busy: state.busy,
        onCancel: () => setShowPassChange(false),
        onSubmit: async (body) => {
          const result = await act('/passphrase/change', body, 'passphrase changed — the old one no longer opens this wallet')
          if (result) setShowPassChange(false)
        },
        onError: (message) => setState((s) => ({ ...s, error: message, note: null }))
      })
    : null

  const promptCard = prompt
    ? h('div', {
        key: 'operator-prompt', className: 'rounded-xl border-[3px] p-3 flex flex-col gap-2',
        style: {
          borderColor: '#f59e0b',
          background: 'color-mix(in srgb, #f59e0b 10%, transparent)'
        }
      }, [
        // The system line: what is being asked, in the app's voice, built from the request record
        // and never from anything the agent typed. This is the line to read first.
        h('div', {
          key: 'h',
          className: 'text-[0.9375rem] font-semibold',
          children: promptRequest && prompt.kind !== 'unlock' && prompt.kind !== 'note'
            ? (promptExpired ? 'Expired — ' : '')
              + `Approve ${fmt(promptRequest.amount, promptRequest.asset)} `
              + `${promptRequest.asset} to ${shortAddr(promptRequest.payee)}`
            : prompt.kind === 'note' ? 'The agent left you a note'
              : prompt.kind === 'unlock' ? 'Unlock the wallet' : 'The agent is waiting for you'
        }),
        // Quarantined and attributed. This string is the one field a prompt-injected agent controls,
        // and it used to be rendered as a headline in the app's own voice — exactly the voice an
        // injection wants to borrow. Now it is a labelled quote, muted, italic, and subordinate.
        h('div', {
          key: 'm', className: 'flex flex-col gap-0.5 rounded-md py-1 pl-2',
          style: {
            borderLeft: '3px solid var(--border, rgba(186,196,212,0.34))',
            background: 'color-mix(in srgb, var(--foreground, #e8ecf2) 4%, transparent)'
          }
        }, [
          h('span', { key: 'tag', className: 'text-[0.625rem] uppercase tracking-[0.12em] text-(--ui-text-tertiary)',
                      children: 'agent message — the agent wrote this, not the wallet' }),
          h('span', {
            key: 'body', className: 'text-[0.8125rem] italic break-words',
            style: { color: 'var(--muted-foreground, #e8ecf2)', opacity: 0.85 },
            children: String(prompt.message || '').trim() || '(the agent sent no message)'
          })
        ]),
        // The facts the agent does not get to write: full payee, whether it is a known contact, the
        // chain, the amount, when the offer lapses.
        promptRequest
          ? h('div', { key: 'facts', className: 'flex flex-col gap-1' }, [
              // The address itself is printed once, in the payment card below: it was here *and*
              // there, 60px apart, which is how a person stops reading either.
              h('div', { key: 'payee', className: 'flex flex-wrap items-center gap-2' }, [
                promptPayeeName
                  ? h('span', { key: 'k', className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
                                children: `known contact: ${promptPayeeName}` })
                  : pill('new payee', 'warn')
              ]),
              h('div', { key: 'meta', className: 'flex flex-wrap items-center gap-3 text-[0.6875rem] text-(--ui-text-tertiary)' }, [
                // chain and amount are already in the headline and in the card below; only the
                // expiry is unique here
                promptRequest.approval_expires_at
                  ? h('span', { key: 'e', children: 'offer expires '
                      + String(promptRequest.approval_expires_at).slice(11, 16) + ' UTC' })
                  : null
              ]),
              // The pending list already said this; the card the agent *raised* did not — and that is
              // the surface a person actually reads. Approving a funding transfer is the moment a
              // budget changes hands, so it belongs on the card.
              promptRequest.funding_for
                ? h('div', { key: 'fund', className: 'rounded-md border-[2px] px-2 py-1 text-[0.6875rem]',
                             style: { borderColor: 'var(--ui-border)' },
                             children: `This funds a sandbox: approving hands ${fmt(promptRequest.amount, promptRequest.asset)} `
                               + 'to the agent to spend inside that budget. The PIN is still asked for every payment.' })
                : null,
            ])
          : null,
        promptRequest && prompt.kind !== 'unlock' && prompt.kind !== 'note'
          ? h(ApprovalActions, {
              key: 'decide', request: promptRequest, humanGate: Boolean(wallet.human_gate),
              pinRequired: pinRequired(), alias: promptPayeeName, autoFocus: true,
              locked: Boolean(wallet.locked), pinSet: pinSet,
              onApprove: (body) => act('/approve', {
                request_id: promptRequest.request_id,
                approval_id: promptRequest.approval_id,
                ...body
              }, 'payment approved'),
              onDeny: () => act('/deny', {
                request_id: promptRequest.request_id,
                approval_id: promptRequest.approval_id || '',
                reason: 'denied from the prompt'
              }, 'payment denied'),
              onError: (message) => setState((s) => ({ ...s, error: message, note: null }))
            })
          // A note carries no action; an unlock prompt (or an approval prompt whose request has not
          // landed in the list yet) can only be answered by unlocking. Same empty-field guard as an
          // approval — pressing Unlock on an empty field used to send an empty string.
          : prompt.kind === 'note'
            ? null
            : h('div', { key: 'unlock-row', className: 'flex flex-wrap items-center gap-2' }, [
                h('input', {
                  key: 'pw', type: 'password', value: passphrase, autoFocus: true,
                  placeholder: 'passphrase — typed here, never sent to the agent',
                  style: fieldProps({ width: '16rem' }),
                  onChange: (e) => setPassphrase(e.target.value),
                  onKeyDown: (e) => { if (e.key === 'Enter') unlockFromPrompt() }
                }),
                btn('Unlock', unlockFromPrompt, 'good')
              ]),
        h('div', { key: 'foot', className: 'flex flex-wrap items-center gap-2 border-t-2 border-(--ui-border) pt-2' }, [
          // 'Dismiss' was a lie: it closes this card and leaves the request pending for the rest of
          // its window. The label says what it does; the line says what it does not.
          btn('Hide for now', () => act('/prompt/clear', {}, null), 'plain'),
          promptRequest
            ? btn('Withdraw request', () => act('/cancel', {
                request_id: promptRequest.request_id, reason: 'withdrawn from the prompt'
              }, 'request withdrawn — the agent cannot pay it now'), 'plain')
            : null,
          h('span', { key: 'n', className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
                      children: 'Hiding closes this card only — the request stays pending until it expires and the agent can raise the prompt again. Withdraw cancels the request itself.' })
        ])
      ])
    : null

  // ── Assets rows ─────────────────────────────────────────────────────────
  // One row = one name. The old row said "BTC · Bitcoin", then "Bitcoin · address ready" in its
  // meta line — the coin name twice, in two sizes. The ticker is the identity (it is what the amount
  // is denominated in, and it is on the coin icon); the meta line carries only facts the ticker does
  // not: the chain it is held on (when that differs from the ticker's own name), the provider, the
  // address state.
  //
  // A chain that holds more than one coin is folded into ONE row: Base showing "ETH · Ethereum" and
  // "USDC · USD Coin" as two unrelated rows hid that they are the same account (one address, one
  // chain), and gave the reader two numbers with no way to see what that one account holds. Chains
  // are grouped in the order they first appear in `state.accounts`, coins within a chain in the
  // order the daemon listed them.
  const rowGroups = []
  const groupAt = {}
  visibleAccounts.forEach((account) => {
    const kind = String(account.kind || '')
    if (!groupAt[kind]) {
      groupAt[kind] = { kind, accounts: [] }
      rowGroups.push(groupAt[kind])
    }
    groupAt[kind].accounts.push(account)
  })

  // The chain's own words, only when the ticker does not already say it: "Base", not "Base · ETH".
  const chainLabel = (kind, accounts) => {
    const alone = chainAssets(kind).length <= 1 || accounts.length === 1
    return alone ? chainName(kind) : String(kind).replace(/-mainnet$/, '').replace(/-sepolia$/, ' Sepolia')
      .replace(/-/g, ' ').replace(/^\w/, (c) => c.toUpperCase())
  }

  const assetRows = state.accounts.length
    ? rowGroups.map((group) => {
        const items = group.accounts
        const first = items[0]
        const multi = items.length > 1
        const spec = COIN[first.asset] || { name: first.asset }
        const color = coinInk(first.asset)
        const rowKey = multi ? group.kind : first.kind + ':' + first.asset
        const shown = Boolean(revealed[rowKey])
        const isCopied = copied === rowKey
        // A chain row is live only if every coin on it is: "live" beside a coin the node could not
        // be asked about is the same lie as "live" beside "rpc unreachable".
        const errored = items.filter((a) => a.error)
        const offline = items.filter((a) => !a.error && !a.available)
        const lockedRows = items.filter((a) => a.state === 'locked')
        const address = items.map((a) => a.address).find(Boolean) || ''
        const providers = Array.from(new Set(items.map((a) => a.provider).filter(Boolean)))
        // A balance nobody could read is said in words, never as 0: "0 BTC" beside "esplora
        // unreachable" is a wallet claiming to be empty on the strength of a failed lookup.
        const amount = first.balance == null
          ? (first.state === 'locked' ? 'locked' : 'offline')
          : fmt(first.balance, first.asset) + ' ' + first.asset
        return h('div', {
          key: rowKey,
          className: 'flex flex-col gap-2 rounded-xl border-[3px] px-3 py-2.5',
          style: {
            background: 'var(--wallet-surface)',
            borderColor: `color-mix(in srgb, ${color} 55%, var(--ui-border))`,
            borderLeft: `6px solid ${color}`
          }
        }, [
          h('div', { key: 'top', className: 'flex items-start gap-3' }, [
            // one icon per coin on a chain row: the row is about the chain, and the coins are
            // named on the lines below it
            multi
              ? h('span', { key: 'ic', className: 'flex items-center -space-x-2 pt-1' },
                  items.map((a) => CoinIcon({ key: a.asset, asset: a.asset, size: 24 })))
              : CoinIcon({ asset: first.asset, size: 30 }),
            h('div', { key: 'm', className: 'flex min-w-0 flex-1 flex-col gap-0.5' }, [
              h('div', { key: 'l1', className: 'flex flex-wrap items-baseline gap-x-3 gap-y-0.5' }, [
                // the single name for this row
                h('span', { key: 'a', className: 'text-[0.9375rem] font-medium whitespace-nowrap',
                            children: multi ? chainLabel(group.kind, items) : first.asset }),
                // The amount belongs next to the coin it is denominated in, not pinned to the far edge
                // of a wide row. A chain row has several amounts instead, printed on the lines below.
                multi ? null
                  : h('span', { key: 'b', className: 'font-mono text-[1rem] font-semibold tabular-nums whitespace-nowrap',
                                style: { color: color || undefined },
                                children: amount })
              ]),
              // What the chain holds, coin by coin: the only total that can be shown without a fiat
              // rate is "this many ETH and this many USDC", so it is printed as exactly that. One
              // line per coin (not "A ETH + B USDC" on one line) keeps each amount whole in a
              // 320 px window.
              multi
                ? h('div', { key: 'total', className: 'flex flex-col gap-0.5' },
                    items.map((account) => h('div', { key: 'p' + account.asset, className: 'flex items-baseline gap-1.5 flex-wrap' }, [
                      account.balance == null
                        ? h('span', { key: 'v', className: 'font-mono text-[1rem] tabular-nums text-(--ui-text-tertiary)',
                                      children: account.error || 'not read' })
                        : h('span', { key: 'v', className: 'font-mono text-[1rem] font-semibold tabular-nums',
                                      style: { color: coinInk(account.asset) },
                                      children: fmt(account.balance, account.asset) }),
                      h('span', { key: 'u', className: 'text-[0.8125rem] font-medium',
                                  style: { color: coinInk(account.asset) }, children: account.asset })
                    ])))
                : null,
              h('div', { key: 'l2', className: 'text-[0.6875rem] text-(--ui-text-tertiary) truncate',
                          title: errored.length ? String(errored[0].error || '') : '',
                          children: [
                            multi ? `${items.length} coins` : (networkName(first.kind) === (spec.name || first.asset) ? '' : networkName(first.kind)),
                            providers.join(', '),
                            address ? 'address ready'
                              : lockedRows.length === items.length
                                ? 'locked — unlock to show the address'
                                : 'address unavailable right now',
                            errored.length ? String(errored[0].error || '').slice(0, 60) : ''
                          ].filter(Boolean).join(' · ') })
            ]),
            // A row with an error is not live whatever `available` claims: "live" beside "rpc
            // unreachable" was the lie that made a dead node look healthy.
            errored.length ? pill('error', 'bad')
              : offline.length ? pill(offline[0].state === 'locked' ? 'locked' : 'offline',
                                      offline[0].state === 'locked' ? 'flat' : 'bad')
              : pill('live', 'good')
          ]),
          address
            ? h('div', { key: 'addr', className: 'flex flex-col gap-1' }, [
                h('div', { key: 'row', className: 'flex flex-wrap items-center gap-2' }, [
                  h('button', {
                    key: 'show', type: 'button',
                    className: 'rounded-md border-[2px] px-2 py-1 text-[0.6875rem]',
                    style: {
                      borderColor: shown ? 'var(--accent, #6366f1)' : 'var(--ui-border)',
                      background: shown ? 'color-mix(in srgb, var(--accent, #6366f1) 18%, transparent)' : 'transparent',
                      color: shown ? 'var(--foreground, #eef1f6)' : 'var(--ui-text-tertiary)'
                    },
                    onClick: () => setRevealed((m) => ({ ...m, [rowKey]: !m[rowKey] })),
                    children: shown ? 'hide address' : 'show address'
                  }),
                  shown
                    ? h('button', {
                        key: 'copy', type: 'button',
                        className: 'rounded-md border-[2px] border-(--ui-border) px-2 py-1 text-[0.6875rem]',
                        onClick: () => copy(address, rowKey),
                        children: isCopied ? 'copied ✓' : 'copy'
                      })
                    : null,
                  // Deposit: the address as a code, for the main wallet of this chain. Available
                  // whether or not the address is revealed — it is a receiving address, not a secret.
                  h('button', {
                    key: 'dep', type: 'button',
                    className: 'rounded-md border-[2px] border-(--ui-border) px-2 py-1 text-[0.6875rem]',
                    title: 'a QR code of this address, so a phone can scan it',
                    onClick: () => setDeposit((m) => ({ ...m, [rowKey]: !m[rowKey] })),
                    children: depositOpen[rowKey] ? 'hide deposit code' : 'deposit QR'
                  }),
                  shown
                    ? h('span', { key: 'n', className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
                                  children: 'anyone can send to this address; it is not a secret, it is just not left on screen' })
                    : null
                ]),
                shown
                  ? h('div', {
                      key: 'full', className: 'select-all break-all rounded-md border-[2px] border-(--ui-border) px-2 py-1 font-mono text-[0.6875rem]',
                      style: { background: 'var(--ui-bg-elevated, var(--ui-bg-secondary, transparent))' },
                      onClick: () => copy(address, rowKey),
                      title: 'click to copy',
                      children: address
                    })
                  : null,
                depositOpen[rowKey]
                  ? h(DepositPanel, { key: 'deposit', account: first })
                  : null
              ])
            : null
        ])
      })
    : [h('div', {
        key: 'none',
        className: 'rounded-xl border-[3px] border-dashed border-(--ui-border) p-6 text-center text-xs text-(--ui-text-tertiary)',
        // Three different situations, three different sentences. "No balances yet" while the first
        // fetch was still in flight, or while the wallet was locked, is simply false.
        children: state.loading
          ? 'Loading balances…'
          : locked
            ? (running
                ? 'Locked — the signer needs the passphrase above before it can ask a node for a balance.'
                : 'The signer is offline — start the wallet daemon, then unlock.')
            : state.accountsStale
              ? 'Balances could not be read just now — press Refresh.'
              : 'No balances yet. Create the chain wallet below to open one.'
      })]

  if (state.accounts.length && !visibleAccounts.length) {
    assetRows.push(h('div', {
      key: 'hidden-all',
      className: 'rounded-xl border-[3px] border-dashed border-(--ui-border) p-4 text-center text-xs text-(--ui-text-tertiary)',
      children: 'Every coin is hidden. Turn one back on with the coin toggles above.'
    }))
  }

  // Spent today against the daily cap — the number that actually stops a runaway agent. Built from
  // /spend and painted only if that route exists and answered in the documented shape.
  const spendAssets = (state.spend && state.spend.assets) || {}
  const spendItems = Object.keys(spendAssets)
    .filter((asset) => !assets.length || assets.includes(asset))
    .map((asset) => {
      const row = spendAssets[asset] || {}
      const cap = ((wallet.caps || {}).daily || {})[asset]
      const received = Number(row.received || 0)
      const spent = Number(row.spent || 0)
      // a cap that has been reached is the one thing here worth colouring
      const hit = cap != null && spent >= Number(cap) && Number(cap) > 0
      return h('span', {
        key: asset, className: 'flex items-center gap-1 whitespace-nowrap font-mono',
        style: hit ? { color: '#f59e0b' } : undefined
      }, [
        h('span', { key: 'l', children: 'spent today:' }),
        h('span', {
          key: 'v',
          children: `${fmt(spent, asset)}${cap != null ? ' / ' + fmt(cap, asset) : ''} ${asset}`
        }),
        received > 0 ? h('span', { key: 'r', children: `· received ${fmt(received, asset)}` }) : null,
        hit ? h('span', { key: 'h', children: '· daily cap reached' }) : null
      ])
    })
  const spendLine = spendItems.length
    ? h('div', {
        key: 'spend',
        className: 'flex flex-wrap items-center gap-x-4 gap-y-1 text-[0.6875rem] text-(--ui-text-tertiary)'
      }, spendItems)
    : null

  const approvalCards = pending.length
    ? pending.map((r) => {
        // The request the agent raised a prompt for is decided in that one card at the top of the
        // page. Rendering a second set of buttons here meant the same payment had two live forms,
        // each with its own passphrase field, and typing in one did nothing to the other.
        const prompted = Boolean(prompt && prompt.request_id === r.request_id)
        const payeeName = knownPayee(state.contacts, r.payee)
        return h('div', {
        key: r.request_id,
        className: 'rounded-lg border-[3px] border-amber-500/70 p-3 flex flex-col gap-2',
        style: { background: `color-mix(in srgb, ${(COIN[r.asset] || { color: '#f59e0b' }).color} 6%, transparent)` }
      }, [
        h('div', { key: 'top', className: 'flex items-center gap-2' }, [
          CoinIcon({ asset: r.asset, size: 26 }),
          h('span', { key: 'amt', className: 'font-mono text-[1.875rem] font-semibold tabular-nums',
                      style: { color: coinInk(r.asset) },
                      children: `${fmt(r.amount, r.asset)} ${r.asset}` }),
          h('span', { key: 'sp', className: 'flex-1' }),
          r.sandbox_id ? pill('sandbox', 'flat') : null,
          pill(chainName(r.kind), 'flat')
        ]),
        r.funding_for
          ? h('div', { key: 'arm', className: 'rounded-md border-[2px] border-amber-500/50 px-2 py-1 text-[0.6875rem] text-amber-300',
                       children: 'This funds a sandbox. It bounds what the agent can lose on one task — every payment from it still asks for your PIN, and it can only pay the destinations you allow.' })
          : null,
        r.sweep_of
          ? h('div', { key: 'sweep', className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
                       children: 'A sweep returns a sandbox balance to your own address: it moves your money between your own accounts, so it needs no PIN — and it still writes a receipt.' })
          : null,
        h('div', { key: 'who', className: 'flex flex-col gap-0.5' }, [
          // The whole payee, click-to-copy, not an abbreviation: the abbreviation is fine in a
          // headline, but it is the last thing to check before the money leaves, and a truncated
          // base58 string cannot be compared by eye or pasted into a block explorer.
          h('div', { key: 'to', className: 'flex flex-wrap items-center gap-2' }, [
            h('span', { key: 'l', className: 'text-[0.6875rem] uppercase tracking-wide text-(--ui-text-tertiary)',
                        children: 'pay to' }),
            h('button', {
              key: 'p', type: 'button', title: `click to copy ${r.payee}`,
              onClick: () => copy(r.payee, 'payee:' + r.request_id),
              className: 'max-w-full break-all rounded-md border-[2px] border-(--ui-border) px-1.5 py-0.5 text-left font-mono text-[0.6875rem]',
              style: { background: 'var(--ui-bg-elevated, var(--ui-bg-secondary, transparent))' },
              children: copied === 'payee:' + r.request_id ? `copied ✓ ${r.payee}` : r.payee
            }),
            payeeName
              ? h('span', { key: 'k', className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
                            children: `known contact: ${payeeName}` })
              : pill('new payee', 'warn')
          ]),
          r.why ? h('div', { key: 'w', className: 'text-xs text-(--ui-text-secondary)', children: r.why }) : null,
          r.memo ? h('div', { key: 'm', className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: r.memo }) : null,
          r.challenge && r.challenge.description
            ? h('div', { key: 'buys', className: 'mt-1 rounded-md border-[2px] border-(--ui-border) px-2 py-1' }, [
                  h('div', { key: 'h', className: 'text-[0.6875rem] uppercase tracking-wider text-(--ui-text-tertiary)',
                             children: 'this is what the seller is selling' }),
                  h('div', { key: 'd', className: 'text-xs', children: r.challenge.description }),
                  h('div', { key: 'u', className: 'text-[0.6875rem] font-mono break-all text-(--ui-text-tertiary)',
                             children: r.challenge.resource || r.challenge.url || '' })
                ])
              : null,
            // One row when it fits, two balanced columns when it does not. As one flex-wrap row, the fee
            // label + expiry jumped between a single line and a stranded second one as the pane was
            // resized, because neither span could shrink.
            h('div', { key: 'fee', className: 'grid grid-cols-[auto_minmax(0,1fr)] gap-x-6 gap-y-0.5 text-[0.6875rem] text-(--ui-text-tertiary)' }, [
              h('span', { key: 'f', className: 'whitespace-nowrap border-b border-(--ui-border) pb-0.5 sm:border-b-0 sm:pb-0',
                children: r.settles === 'x402'
                  ? 'no fee — the seller settles this authorisation on-chain'
                  : (r.fee_estimate && Number(r.fee_estimate) > 0
                      ? `network fee ${feeText(r.fee_estimate, r.asset)} ${r.asset}`
                      : 'network fee quoted when it is approved') }),
              r.approval_expires_at
                ? h('span', { key: 'e', className: 'sm:text-right', children: 'offer expires ' + String(r.approval_expires_at).slice(11, 16) + ' UTC' })
                : null
            ])
        ]),
        prompted
          ? h('div', { key: 'here', className: 'rounded-md border-[2px] border-(--ui-border) px-2 py-1 text-[0.6875rem] text-(--ui-text-tertiary)',
                       children: 'The agent is asking for this one in the card at the top of the page — approve or deny it there.' })
          : h(ApprovalActions, {
              key: 'decide', request: r, humanGate: Boolean(wallet.human_gate),
              pinRequired: pinRequired(), alias: knownPayee(state.contacts, r.payee), facts: false,
              locked: Boolean(wallet.locked), pinSet: pinSet,
              onApprove: (body) => act('/approve', {
                request_id: r.request_id, approval_id: r.approval_id, ...body
              }, 'payment approved'),
              onDeny: () => act('/deny', {
                request_id: r.request_id, approval_id: r.approval_id || '',
                reason: 'denied in the wallet pane'
              }, 'payment denied'),
              onError: (message) => setState((s) => ({ ...s, error: message, note: null }))
            })
      ])
      })
    : [h('div', {
        key: 'none',
        className: 'rounded-lg border-[3px] border-dashed border-(--ui-border) p-4 text-center text-xs text-(--ui-text-tertiary)',
        children: 'Nothing waiting for approval.'
      })]

  const periodLabel = (hours) => {
    const n = Number(hours || 0)
    if (n >= 168 && n % 168 === 0) return `${n / 168} week${n === 168 ? '' : 's'}`
    if (n >= 24 && n % 24 === 0) return `${n / 24} day${n === 24 ? '' : 's'}`
    return `${n} hour${n === 1 ? '' : 's'}`
  }

  const mandateCards = state.mandates.length
    ? state.mandates.map((m) => {
        const usage = m.usage || {}
        return h('div', { key: m.mandate_id, className: 'rounded-lg border-[3px] border-(--ui-border) p-3 flex flex-col gap-1.5' }, [
        h('div', { key: 'top', className: 'flex items-center gap-2' }, [
          CoinIcon({ asset: m.asset, size: 22 }),
          h('span', { key: 'l', className: 'text-sm truncate', children: m.label || m.mandate_id }),
          h('span', { key: 'sp', className: 'flex-1' }),
          m.unattended ? pill('no PIN', 'bad') : null,
          pill(m.status, m.status === 'active' ? 'good' : m.status === 'revoked' ? 'bad' : 'flat')
        ]),
        h('div', { key: 'caps', className: 'flex flex-col gap-0.5 text-[0.6875rem] text-(--ui-text-secondary)' }, [
          h('div', { key: 'a', className: 'font-mono',
                     children: `≤ ${fmt(m.per_tx_cap, m.asset)} ${m.asset} per payment` }),
          h('div', { key: 'b', className: 'font-mono', children:
            `≤ ${fmt(m.period_cap, m.asset)} ${m.asset} every ${periodLabel(m.period_hours)}`
            + (usage.period_left ? ` — ${fmt(usage.period_left, m.asset)} left in this window` : '') }),
          h('div', { key: 'c', className: 'font-mono', children:
            `${fmt(m.absolute_cap, m.asset)} ${m.asset} in total`
            + (usage.lifetime_left ? ` — ${fmt(usage.lifetime_left, m.asset)} left` : '') })
        ]),
        h('div', { key: 'payee', className: 'font-mono text-[0.6875rem] text-(--ui-text-tertiary) truncate', children: shortAddr(m.payee) }),
        h('div', { key: 'exp', className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: `expires ${m.expires_at}` }),
        h('div', { key: 'btns', className: 'flex flex-wrap items-center gap-2 mt-1' }, [
          m.status === 'active'
            ? btn('Pause', () => act('/mandates/enable', { mandate_id: m.mandate_id, enabled: false },
                                     'autopay paused'), 'flat')
            : btn('Resume', () => act('/mandates/enable', {
                mandate_id: m.mandate_id, enabled: true, passphrase: mandatePass
              }, 'autopay resumed'), 'good'),
          m.unattended
            // turning the waiver OFF needs nothing: removing authority never does. Turning it ON
            // needs the passphrase and the PIN, so it stays a deliberate CLI step
            ? btn('Ask for the PIN again', () => act('/mandates/unattended',
                { mandate_id: m.mandate_id, unattended: false }, 'the PIN is required again'), 'flat')
            : null,
          m.status === 'active'
            ? btn('Revoke', () => act('/mandates/revoke', { mandate_id: m.mandate_id }, 'mandate revoked'), 'bad')
            : null,
          h('select', { key: 'iv', value: '', title: 'change how often the period cap resets',
            style: fieldProps({ padding: '4px 6px', fontSize: '12px', minHeight: '26px',
                                paddingRight: '1.6rem' }),
            onChange: (e) => {
              const interval = e.target.value
              if (interval) {
                act('/mandates/update', { mandate_id: m.mandate_id, interval, passphrase: mandatePass },
                    `interval set to ${interval}`)
              }
            }
          }, [
            opt('', 'change window…'),
            opt('1h', 'every hour'), opt('6h', 'every 6 hours'), opt('12h', 'every 12 hours'),
            opt('1d', 'every day'), opt('3d', 'every 3 days'), opt('7d', 'every week')
          ]),
          confirmDelete === m.mandate_id
            ? btn('Confirm delete', () => { setConfirmDelete(null); act('/mandates/delete', { mandate_id: m.mandate_id, passphrase: mandatePass }, 'autopay deleted') }, 'bad')
            : btn('Delete', () => setConfirmDelete(m.mandate_id), 'flat')
        ])
        ])
      })
    : [h('div', {
        key: 'none',
        className: 'rounded-lg border-[3px] border-dashed border-(--ui-border) p-4 text-center text-xs text-(--ui-text-tertiary)',
        children: 'No autopays yet — use the form above, or let the agent propose one and activate it.'
      })]

  const mandateForm = h('div', { key: 'mandate-form', className: 'rounded-xl border-[3px] border-(--ui-border) p-3 flex flex-col gap-2' }, [
    h('div', { key: 'h', className: 'text-[0.75rem] font-semibold uppercase tracking-[0.08em] text-(--ui-text-secondary)',
               children: 'New standing approval (auto-pay)' }),
    h('div', { key: 'n', className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
               children: (wallet.pin && wallet.pin.required === false
                 ? 'A standing permission: when the agent asks to pay this payee it goes through without '
                   + 'asking you — up to the caps, until it expires or you pause it. Nothing is paid on a timer.'
                 : 'A standing permission up to these caps, until it expires or you pause it. Every '
                   + 'payment still asks for your PIN, so an autopay raises the ceiling — it does not '
                   + 'remove the gate.') }),
    h('div', { key: 'row1', className: 'flex flex-wrap items-end gap-2' }, [
      // `1 1 18rem` let the address field take every spare pixel: at 1280 it held ~68% of the row and
      // pushed the coin and network menus to the far edge. The address is long; the field does not
      // need to be the width of the page to hold it.
      h('input', { key: 'payee', value: mandateDraft.payee, placeholder: 'payee address or contact name',
                   style: fieldProps({ flex: '1 1 18rem', maxWidth: '30rem', minWidth: '0', fontFamily: MONO }),
                   onChange: (e) => setMandateDraft((d) => ({ ...d, payee: e.target.value })) }),
      h('select', { key: 'coin', value: mandateDraft.asset, style: fieldProps({ paddingRight: '1.6rem' }),
                    onChange: (e) => {
                      const asset = e.target.value
                      setMandateDraft((d) => ({ ...d, asset, kind: defaultChain(asset) || d.kind }))
                    } },
        (assets.length ? assets : ['XMR']).map((a) => opt(a, a))),
      h('select', { key: 'kind', value: mandateDraft.kind, style: fieldProps({ paddingRight: '1.6rem' }),
                    onChange: (e) => setMandateDraft((d) => ({ ...d, kind: e.target.value })) },
        (chainsFor(mandateDraft.asset).length ? chainsFor(mandateDraft.asset) : [mandateDraft.kind])
          .filter((c) => !c.startsWith('sim-')).map((c) => opt(c, chainName(c))))
    ]),
    h('div', { key: 'row2', className: 'flex flex-wrap items-end gap-3' }, [
      labelled('per', 'max per payment',
        h('input', { key: 'i', value: mandateDraft.per_tx, placeholder: '0.005', inputMode: 'decimal',
          style: fieldProps({ width: '7rem' }),
          onChange: (e) => setMandateDraft((d) => ({ ...d, per_tx: e.target.value })) })),
      labelled('pc', 'at most, every',
        h('div', { key: 'g', className: 'flex flex-wrap items-end gap-1' }, [
          h('input', { key: 'i', value: mandateDraft.period_cap, placeholder: '0.02', inputMode: 'decimal',
            style: fieldProps({ width: '7rem' }),
            onChange: (e) => setMandateDraft((d) => ({ ...d, period_cap: e.target.value })) }),
          h('span', { key: 'slash', className: 'pb-2 text-[0.75rem] text-(--ui-text-tertiary)', children: '/' }),
          h('input', { key: 'n', value: mandateDraft.period_n, inputMode: 'numeric',
            style: fieldProps({ width: '3.5rem' }),
            onChange: (e) => setMandateDraft((d) => ({ ...d, period_n: e.target.value })) }),
          h('select', { key: 'u', value: mandateDraft.unit, style: fieldProps({ paddingRight: '1.6rem' }),
            onChange: (e) => setMandateDraft((d) => ({ ...d, unit: e.target.value })) },
            opt('h', 'hours'), opt('d', 'days'), opt('w', 'weeks'))
        ])),
      labelled('tot', 'in total',
        h('input', { key: 'i', value: mandateDraft.total, placeholder: '0.05', inputMode: 'decimal',
          style: fieldProps({ width: '7rem' }),
          onChange: (e) => setMandateDraft((d) => ({ ...d, total: e.target.value })) })),
      labelled('exp', 'expires in',
        h('div', { key: 'g', className: 'flex flex-wrap items-end gap-1' }, [
          h('input', { key: 'i', value: mandateDraft.expire_days, inputMode: 'numeric',
            style: fieldProps({ width: '4rem' }),
            onChange: (e) => setMandateDraft((d) => ({ ...d, expire_days: e.target.value })) }),
          h('span', { key: 'u', className: 'pb-2 text-[0.75rem] text-(--ui-text-tertiary)', children: 'days' })
        ])),
      labelled('lb', 'label',
        h('input', { key: 'i', value: mandateDraft.label, placeholder: 'e.g. VPS bill',
          style: fieldProps({ flex: '1 1 auto', minWidth: '10rem' }),
          onChange: (e) => setMandateDraft((d) => ({ ...d, label: e.target.value })) }))
    ]),
    h('div', { key: 'explain', className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
               children: 'max per payment = one transaction. "at most, every" is a rolling window: the last N hours/days/weeks to this payee, not a calendar day. "in total" caps everything this autopay ever pays. Nothing is sent on a timer — the agent still has to ask each time.' }),
    h('div', { key: 'row3', className: 'flex flex-wrap items-center gap-2' }, [
      h('input', { key: 'pw', type: 'password', value: mandatePass, placeholder: 'passphrase',
                   style: fieldProps({ width: '14rem' }),
                   onChange: (e) => setMandatePass(e.target.value) }),
      btn('Create autopay', () => {
        if (!mandateDraft.payee || !mandateDraft.per_tx || !mandateDraft.period_cap) {
          setState((s) => ({ ...s, error: 'payee, max per payment and the period cap are required' }))
          return
        }
        const days = Math.max(1, Math.min(7, Number(mandateDraft.expire_days) || 7))
        act('/mandates/create', {
          kind: mandateDraft.kind, asset: mandateDraft.asset,
          payee: mandateDraft.payee, per_tx_cap: mandateDraft.per_tx,
          period_cap: mandateDraft.period_cap,
          absolute_cap: mandateDraft.total || mandateDraft.period_cap,
          interval: `${Number(mandateDraft.period_n) || 24}${mandateDraft.unit || 'h'}`,
          expires_at: new Date(Date.now() + days * 86400000).toISOString().replace(/\.\d+Z$/, 'Z'),
          label: mandateDraft.label, activate: true, passphrase: mandatePass
        }, 'autopay created')
      }, 'good'),
      h('span', { key: 'n', className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
                  children: 'needs the passphrase: it authorises this autopay’s caps' })
    ])
  ])

  const sandboxCards = state.sandboxes.length
    ? state.sandboxes.map((sb) => {
        const budget = Number(sb.budget || 0)
        const spent = Number(sb.spent || 0)
        const pct = budget > 0 ? Math.min(100, Math.round((spent / budget) * 100)) : 0
        // A closed sandbox is history: dim it and break the border so it cannot be mistaken for a
        // live budget the agent can still draw on.
        const live = sb.status === 'active'
        const dead = sb.status === 'destroyed'
        // A destroyed row keeps `remaining` from before the fix (0.003) — showing it would say the
        // money is still in an account whose key is gone. Show what was swept and when instead.
        const gone = dead ? destroyedFacts(sb, state.ledger) : null
        // `live_balance` is the true on-chain balance, which the budget arithmetic cannot see: a
        // spend made outside the pane, a sweep that has not confirmed, an unswept remainder. The
        // daemon sends null when it cannot read the chain, so the card says "unverified" rather
        // than repeating the budget as if it were the chain. An older backend omits the key
        // entirely; then the line is simply absent, not falsely "unverified".
        const hasLive = sb.live_balance !== undefined
        const liveNum = Number(sb.live_balance)
        const liveText = hasLive && sb.live_balance !== null && Number.isFinite(liveNum)
          ? `live ${fmt(sb.live_balance, sb.asset)} / budget ${fmt(sb.budget, sb.asset)}`
          : null
        return h('div', { key: sb.sandbox_id, className: 'rounded-xl border-[3px] border-(--ui-border) p-3 flex flex-col gap-2',
                          style: {
                            background: 'var(--wallet-surface)',
                            borderStyle: live ? 'solid' : 'dashed',
                            opacity: live ? 1 : 0.6
                          } }, [
          h('div', { key: 'top', className: 'flex items-center gap-2' }, [
            CoinIcon({ asset: sb.asset, size: 22 }),
            h('span', { key: 'l', className: 'text-sm truncate', children: sb.label || sb.sandbox_id }),
            h('span', { key: 'sp', className: 'flex-1' }),
            sb.mode === 'one_time' ? pill('one-time', 'warn') : null,
            sb.unattended ? pill('agent spends here', 'warn') : null,
            sb.tampered ? pill('record edited — revoke it', 'bad') : null,
            pill(sb.status, sb.status === 'active' ? 'good' : (sb.status === 'expired' || sb.status === 'destroyed') ? 'bad' : 'warn')
          ]),
          dead
            ? h('div', { key: 'nums', className: 'flex flex-col gap-0.5 font-mono text-xs' }, [
                h('span', { key: 'when', children: `destroyed ${clockTime(gone.at)}` }),
                h('span', { key: 'sw', className: 'text-(--ui-text-tertiary)', children:
                  gone.swept === null
                    ? 'no sweep was recorded — the receipt is not in this ledger'
                    : gone.zero
                      ? 'nothing was left to sweep'
                      : `swept ${fmt(gone.swept, sb.asset)} ${sb.asset} home · budget was ${fmt(sb.budget, sb.asset)}` })
              ])
            : h('div', { key: 'nums', className: 'flex items-baseline justify-between font-mono text-xs' }, [
                h('span', { key: 'r', children: `${fmt(sb.remaining, sb.asset)} left` }),
                h('span', { key: 'b', className: 'text-(--ui-text-tertiary)', children: `of ${fmt(sb.budget, sb.asset)}` })
              ]),
          // The bar is a budget gauge: on a destroyed row it would draw progress towards money that
          // has already been sent home.
          dead
            ? null
            : h('div', { key: 'bar', className: 'h-1.5 w-full rounded-full', style: { background: 'rgba(127,127,127,0.2)' } },
                h('div', { className: 'h-1.5 rounded-full', style: { width: pct + '%', background: (COIN[sb.asset] || { color: '#f26822' }).color } })),
          dead
            ? null
            : h('div', { key: 'chain', className: 'flex flex-wrap items-center gap-2 text-[0.6875rem] text-(--ui-text-tertiary)' }, [
                liveText
                  ? h('span', { key: 'live', className: 'font-mono', title: 'the balance the chain reports for this account, next to the budget the pane tracks',
                                children: liveText })
                  : hasLive && sb.live_balance === null
                    ? h('span', { key: 'unv', className: 'rounded border border-(--ui-border) px-1',
                                  title: 'the chain could not be read, so the budget above is what the pane believes, not what the chain holds',
                                  children: 'unverified' })
                    : null
              ]),
          h('div', { key: 'meta', className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
                     children: `account #${sb.account_index ?? 0} · expires ${sb.expires_at}` }),
          // The destination rule is the security boundary a human should be able to read at a glance:
          // a sandbox pays saved contacts on its own, and anything else waits for an approval.
          h('div', { key: 'payees', className: 'text-[0.6875rem]', style: sb.unattended ? { color: '#f59e0b' } : undefined },
            sb.unattended
              ? (sb.allowed_payees && sb.allowed_payees.length
                  ? `HANDED OVER: the agent pays any of the ${sb.allowed_payees.length} allowed address${sb.allowed_payees.length === 1 ? '' : 'es'} without asking — other destinations still need you`
                  : 'HANDED OVER: the agent pays any address from this budget without asking, until it runs out')
              : (sb.allowed_payees && sb.allowed_payees.length)
                ? `auto-pays ${sb.allowed_payees.length} allowed address${sb.allowed_payees.length === 1 ? '' : 'es'} + your saved contacts`
                : 'auto-pays your saved contacts only — any other destination needs your approval'),
          sb.mode === 'one_time'
            ? h('div', { key: 'sd', className: 'text-[0.6875rem]',
                style: { color: sb.destroy_blocked ? '#f59e0b' : 'var(--ui-text-tertiary)' },
                children: sb.destroyed_at
                  ? 'its key material is gone — nothing can spend from this account'
                  : sb.destroy_at
                    ? (sb.destroy_blocked
                        ? `holding off: ${sb.destroy_blocked} — the money is still in the sandbox`
                        : `destroys itself in ${Math.max(0, Math.floor((sb.destroy_in || 0) / 60))}m, after sweeping what is left home`)
                    : 'waiting for its one payment; the clock starts when that settles' })
            : null,
          h('div', { key: 'acl', className: 'flex flex-wrap items-end gap-2 text-[0.6875rem] text-(--ui-text-tertiary)' }, [
            ProfilePicker({
              key: 'p', profiles, disabled: !live,
              selected: profileDraft[sb.sandbox_id] ?? (sb.allowed_profiles || []),
              onChange: (next) => setProfileDraft((m) => ({ ...m, [sb.sandbox_id]: next }))
            }),
            h('button', {
              key: 's', type: 'button', disabled: !live,
              className: 'rounded-md border-[2px] border-(--ui-border) px-2 py-1 text-[0.6875rem] hover:bg-(--wallet-surface-strong)',
              style: live ? undefined : { opacity: 0.5, cursor: 'not-allowed' },
              onClick: () => {
                if (!live) return
                act('/sandbox/profiles', {
                  sandbox_id: sb.sandbox_id,
                  allowed_profiles: profileDraft[sb.sandbox_id] ?? (sb.allowed_profiles || []),
                  passphrase: sandboxPass
                }, 'sandbox access updated')
              },
              children: 'save access'
            })
          ]),
          !live
            ? h('div', { key: 'closed', className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
                         children: 'This sandbox is closed — nothing can spend from it, and its access list no longer matters.' })
            : null,
          // Which addresses this budget may pay unattended. The CLI has had this since the
          // destination rule landed; the pane could only set it at creation.
          live
            ? h('div', { key: 'payees', className: 'flex flex-wrap items-end gap-2' }, [
                h('div', { key: 'f', className: 'flex flex-col gap-0.5 flex-1 min-w-[14rem]' }, [
                  h('span', { key: 'l', className: 'text-[0.625rem] uppercase tracking-[0.08em] text-(--ui-text-tertiary)',
                              children: 'extra addresses it may pay (optional)' }),
                  h('input', {
                    key: 'i',
                    value: payeeDraft[sb.sandbox_id] ?? (sb.allowed_payees || []).join(', '),
                    placeholder: 'addresses, comma separated — blank = saved contacts only',
                    style: fieldProps({ fontFamily: MONO }),
                    onChange: (e) => setPayeeDraft((m) => ({ ...m, [sb.sandbox_id]: e.target.value }))
                  })
                ]),
                h('button', {
                  key: 's', type: 'button',
                  className: 'rounded-md border-[2px] border-(--ui-border) px-2 py-1 text-[0.6875rem] hover:bg-(--wallet-surface-strong)',
                  onClick: () => act('/sandbox/payees', {
                    sandbox_id: sb.sandbox_id,
                    allowed_payees: String(payeeDraft[sb.sandbox_id] ?? (sb.allowed_payees || []).join(', '))
                      .split(/[\s,]+/).map((a) => a.trim()).filter(Boolean),
                    passphrase: sandboxPass
                  }, 'destinations updated — adding one needs the passphrase, removing is free'),
                  children: 'save destinations'
                })
              ])
            : null,
          (live && !sb.tampered)
            ? h('div', { key: 'unattended', className: 'flex flex-wrap items-end gap-2 rounded-lg p-2',
                         style: { border: '2px solid var(--ui-border)' } }, [
                h('div', { key: 't', className: 'flex-1 min-w-[13rem] flex flex-col gap-0.5' }, [
                  h('span', { key: 'l', className: 'text-[0.625rem] uppercase tracking-[0.08em] text-(--ui-text-tertiary)',
                              children: sb.unattended ? 'the agent can spend this budget on its own' : 'hand this budget to the agent' }),
                  h('span', { key: 'd', className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
                              children: sb.unattended
                                ? 'Every payment it makes from here is signed without your PIN, up to the money left. Revoke the budget to stop it at once.'
                                : 'The agent then pays from this budget without asking — until the money runs out or the budget expires. Your main wallet is untouched, and it has to ask you for more.' })
                ]),
                h('input', {
                  key: 'pin', type: 'password', inputMode: 'numeric', maxLength: 6, placeholder: 'PIN',
                  value: sandboxPin, style: fieldProps({ width: '5.5rem', fontFamily: MONO }),
                  onChange: (e) => setSandboxPin(e.target.value.replace(/[^0-9]/g, '').slice(0, 6))
                }),
                btn(sb.unattended ? 'Take it back' : 'Hand it over', () => {
                  if (sb.unattended) {
                    act('/sandbox/unattended', { sandbox_id: sb.sandbox_id, on: false },
                        'gate back on — every payment from this budget asks for your PIN again')
                    return
                  }
                  act('/sandbox/unattended', { sandbox_id: sb.sandbox_id, on: true,
                                               passphrase: sandboxPass, pin: sandboxPin },
                      'handed over — the agent spends from this budget without asking')
                }, sb.unattended ? 'plain' : 'bad',
                   sb.unattended ? undefined
                     : (sandboxPass && sandboxPin.length === 6 ? undefined
                        : 'needs the passphrase above and your six-digit PIN'))
              ])
            : null,
          (sb.pooled || sb.mode === 'pooled')
            ? h('div', { key: 'pooled', className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
                         children: 'Policy budget: this chain cannot hold a separate sandbox account, so '
                           + 'the cap is enforced against the main balance. Nothing was moved, so there '
                           + 'is nothing to fund or sweep; every payment still asks for your PIN.' })
            : null,
          // A budget the agent asked for on a chain with nothing to fund has no funding request to
          // approve, so this button is the operator's yes (it costs the passphrase above).
          ((sb.pooled || sb.mode === 'pooled') && sb.status === 'unfunded')
            ? h('div', { key: 'arm', className: 'flex gap-2' }, [
                btn('Arm this budget', () => act('/sandbox/arm',
                      { sandbox_id: sb.sandbox_id, passphrase: sandboxPass },
                      'budget armed — every payment from it still asks for your PIN'), 'plain'),
                btn('Revoke', () => act('/sandbox/revoke', { sandbox_id: sb.sandbox_id }, 'sandbox revoked'), 'bad')
              ])
            : null,
          sb.status === 'active'
            ? h('div', { key: 'acts', className: 'flex gap-2' }, [
                (sb.pooled || sb.mode === 'pooled')
                  ? null
                  : (Number(sb.remaining) > 0
                      ? btn('Withdraw left', () => act('/sandbox/sweep', { sandbox_id: sb.sandbox_id },
                            'sweeping the remaining budget back to the main account'), 'plain')
                      : null),
                btn('Revoke', () => act('/sandbox/revoke', { sandbox_id: sb.sandbox_id }, 'sandbox revoked'), 'bad')
              ])
            : null
        ])
      })
    : [h('div', { key: 'none', className: 'rounded-xl border-[3px] border-dashed border-(--ui-border) p-4 text-center text-xs text-(--ui-text-tertiary)',
                  children: 'No sandboxes. A sandbox bounds what the agent can lose on one task — every payment from it still asks for your PIN.' })]

  const sectionPass = (key, value, onChange, hint) => h('div', {
    key, className: 'flex flex-wrap items-center gap-2'
  }, [
    h('span', { key: 'l', className: 'text-[0.6875rem] uppercase tracking-wide text-(--ui-text-tertiary)',
                children: 'passphrase' }),
    h('input', { key: 'i', type: 'password', value, placeholder: 'needed for the actions below',
      style: fieldProps({ width: '14rem' }), onChange: (e) => onChange(e.target.value) }),
    h('span', { key: 'n', className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: hint })
  ])

  const sandboxForm = h('div', { key: 'new-sandbox', className: 'rounded-lg border-[3px] border-(--ui-border) p-3 flex flex-wrap items-end gap-2' }, [
    h('div', { key: 'a', className: 'flex flex-col gap-0.5' }, [
      h('span', { key: 'l', className: 'text-[0.6875rem] uppercase tracking-wide text-(--ui-text-tertiary)', children: 'coin' }),
      h('select', { key: 's', value: sandboxDraft.asset, style: fieldProps(),
        onChange: (e) => {
          const asset = e.target.value
          setSandboxDraft((d) => ({ ...d, asset, kind: defaultChain(asset) || d.kind }))
        } },
        (assets.length ? assets : ['XMR']).map((a) => opt(a, a)))
    ]),
    h('div', { key: 'k', className: 'flex flex-col gap-0.5' }, [
      h('span', { key: 'l', className: 'text-[0.6875rem] uppercase tracking-wide text-(--ui-text-tertiary)', children: 'network' }),
      h('select', { key: 's', value: sandboxDraft.kind, style: fieldProps(),
        onChange: (e) => setSandboxDraft((d) => ({ ...d, kind: e.target.value })) },
        (chainsFor(sandboxDraft.asset).length ? chainsFor(sandboxDraft.asset) : [sandboxDraft.kind])
          .map((c) => opt(c, chainName(c))))
    ]),
    h('div', { key: 'b', className: 'flex flex-col gap-0.5' }, [
      h('span', { key: 'l', className: 'text-[0.6875rem] uppercase tracking-wide text-(--ui-text-tertiary)', children: 'budget' }),
      h('input', { key: 'i', value: sandboxDraft.budget, placeholder: '0.05', inputMode: 'decimal',
        style: fieldProps({ width: '6rem' }),
        onChange: (e) => setSandboxDraft((d) => ({ ...d, budget: e.target.value })) })
    ]),
    h('div', { key: 'h', className: 'flex flex-col gap-0.5' }, [
      h('span', { key: 'l', className: 'text-[0.6875rem] uppercase tracking-wide text-(--ui-text-tertiary)', children: 'hours' }),
      h('input', { key: 'i', value: sandboxDraft.hours, inputMode: 'numeric',
        style: fieldProps({ width: '5rem' }),
        onChange: (e) => setSandboxDraft((d) => ({ ...d, hours: e.target.value })) })
    ]),
    h('label', { key: 'ot', className: 'flex items-center gap-2 text-[0.6875rem]' }, [
      h('input', { key: 'c', type: 'checkbox', checked: sandboxDraft.oneTime,
        onChange: (e) => setSandboxDraft((d) => ({ ...d, oneTime: e.target.checked })) }),
      h('span', { key: 'l', children: 'one payment, then self-destruct' })
    ]),
    sandboxDraft.oneTime
      ? h('div', { key: 'dw', className: 'flex flex-col gap-0.5' }, [
          h('span', { key: 'l', className: 'text-[0.6875rem] uppercase tracking-wide text-(--ui-text-tertiary)', children: 'destroy after' }),
          h('select', { key: 's', value: sandboxDraft.destruct, style: fieldProps({ paddingRight: '1.6rem' }),
            onChange: (e) => setSandboxDraft((d) => ({ ...d, destruct: e.target.value })) },
            opt('300', '5 minutes'), opt('900', '15 minutes'), opt('1800', '30 minutes'),
            opt('3600', '1 hour'), opt('21600', '6 hours'))
        ])
      : null,
    ProfilePicker({
      key: 'pr', profiles,
      selected: sandboxDraft.profiles,
      onChange: (next) => setSandboxDraft((d) => ({ ...d, profiles: next }))
    }),
    h('div', { key: 'lb', className: 'flex flex-col gap-0.5 flex-1 min-w-[10rem]' }, [
      h('span', { key: 'l', className: 'text-[0.6875rem] uppercase tracking-wide text-(--ui-text-tertiary)', children: 'label' }),
      h('input', { key: 'i', value: sandboxDraft.label, placeholder: 'what it is for',
        style: fieldProps(),
        onChange: (e) => setSandboxDraft((d) => ({ ...d, label: e.target.value })) })
    ]),
    // The one rule that stops a prompt-injected agent: a budget with no allowlist can only pay the
    // addresses you already saved, so a swapped destination always comes back to you.
    h('div', { key: 'pt', className: 'flex flex-col gap-0.5 flex-1 min-w-[14rem]' }, [
      h('span', { key: 'l', className: 'text-[0.6875rem] uppercase tracking-wide text-(--ui-text-tertiary)',
                  children: 'extra addresses it may pay (optional)' }),
      h('input', { key: 'i', value: sandboxDraft.payees,
        placeholder: 'addresses, comma separated — blank = saved contacts only',
        style: fieldProps({ fontFamily: MONO }),
        onChange: (e) => setSandboxDraft((d) => ({ ...d, payees: e.target.value })) })
    ]),
    // `mt-auto` pins the primary action to the form's bottom line, so it stands beside the last row of
    // fields instead of trailing the wide address row along the far right edge.
    h('div', { key: 'go', className: 'mt-auto' }, [
      btn('Create sandbox', async () => {
      if (!sandboxDraft.budget) { setState((s) => ({ ...s, error: 'budget is required' })); return }
      const asset = sandboxDraft.asset
      await act('/sandbox/create', {
        kind: sandboxDraft.kind, asset, budget: sandboxDraft.budget,
        lifetime_hours: Number(sandboxDraft.hours) || 24, label: sandboxDraft.label,
        allowed_profiles: sandboxDraft.profiles || [],
        allowed_payees: String(sandboxDraft.payees || '')
          .split(/[\s,]+/).map((a) => a.trim()).filter(Boolean),
        mode: sandboxDraft.oneTime ? 'one_time' : 'budget',
        self_destruct_seconds: Number(sandboxDraft.destruct) || 1800
      }, sandboxDraft.oneTime
        ? 'sandbox created — one payment, then it destroys itself'
        : 'sandbox created — fund it from pending approvals')
      setSandboxDraft({ kind: sandboxDraft.kind, budget: '', hours: '24', label: '', profiles: [],
                        oneTime: false, destruct: '1800' })
      }, 'good')
    ])
  ])

  const contactCards = state.contacts.length
    ? state.contacts.map((c) => h('div', { key: c.contact_id, className: 'rounded-xl border-[3px] border-(--ui-border) p-3 flex flex-col gap-1',
                                          style: { background: 'var(--wallet-surface)' } }, [
        h('div', { key: 'top', className: 'flex items-center gap-2' }, [
          h('span', {
            key: 'av', className: 'grid h-7 w-7 place-items-center rounded-full text-[0.8125rem] font-semibold',
            style: { background: 'var(--wallet-surface-strong)', color: 'var(--ui-text-primary)' },
            children: (c.name || '?').slice(0, 1).toUpperCase()
          }),
          h('span', { key: 'n', className: 'text-[0.9375rem] font-medium', children: c.name }),
          h('span', { key: 'sp', className: 'flex-1' }),
          confirmContact === c.name
            ? h('button', {
                key: 'del', type: 'button', className: 'text-[0.6875rem] text-red-400 hover:underline',
                onClick: () => {
                  setConfirmContact(null)
                  act('/contact/delete', { name: c.name, passphrase: contactPass },
                      'contact removed')
                },
                children: 'yes, remove'
              })
            : h('button', {
                key: 'del', type: 'button', className: 'text-[0.6875rem] text-red-400 hover:underline',
                onClick: () => setConfirmContact(c.name),
                children: 'remove'
              }),
          // "fix Adrian's address" used to mean re-saving by name and hoping it merged. Load the
          // contact into the form instead: the daemon asks for the passphrase when an address changes.
          h('button', {
            key: 'ed', type: 'button',
            className: 'text-[0.6875rem] text-(--ui-text-tertiary) hover:text-(--ui-text-primary)',
            onClick: () => {
              const chainsKnown = Object.keys(c.addresses || {})
              const kind = chainsKnown[0] || contactDraft.kind
              setContactDraft({ name: c.name, kind, address: (c.addresses || {})[kind] || '',
                                notes: c.notes || '' })
              setState((st) => ({ ...st, note: `editing ${c.name} — change the address and press Save contact`, error: null }))
            },
            children: 'edit'
          })
        ]),
        ...Object.entries(c.addresses || {}).map(([kind, address]) => h('button', {
          key: kind, type: 'button',
          className: 'text-left font-mono text-[0.6875rem] text-(--ui-text-tertiary) hover:text-(--ui-text-primary)',
          title: address, onClick: () => copy(address),
          children: `${chainName(kind)}: ${shortAddr(address)}`
        })),
        c.notes ? h('div', { key: 'note', className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: c.notes }) : null
      ]))
    : [h('div', { key: 'none', className: 'rounded-xl border-[3px] border-dashed border-(--ui-border) p-4 text-center text-xs text-(--ui-text-tertiary)',
                  children: 'No contacts yet. Say “send Aiden 5”: the wallet shows you the address to confirm, and only then saves it.' })]

  const contactForm = h('div', { key: 'new-contact', className: 'rounded-lg border-[3px] border-(--ui-border) p-3 flex flex-wrap items-end gap-2' }, [
    h('div', { key: 'n', className: 'flex flex-col gap-0.5' }, [
      h('span', { key: 'l', className: 'text-[0.6875rem] uppercase tracking-wide text-(--ui-text-tertiary)', children: 'name' }),
      h('input', { key: 'i', value: contactDraft.name, placeholder: 'Aiden',
        style: fieldProps({ width: '8rem' }),
        onChange: (e) => setContactDraft((d) => ({ ...d, name: e.target.value })) })
    ]),
    h('div', { key: 'k', className: 'flex flex-col gap-0.5' }, [
      h('span', { key: 'l', className: 'text-[0.6875rem] uppercase tracking-wide text-(--ui-text-tertiary)', children: 'chain' }),
      h('select', { key: 's', value: contactDraft.kind, style: fieldProps(),
        onChange: (e) => setContactDraft((d) => ({ ...d, kind: e.target.value })) },
        chains.map((c) => opt(c, chainName(c))))
    ]),
    h('div', { key: 'a', className: 'flex flex-col gap-0.5 flex-1 min-w-[16rem]' }, [
      h('span', { key: 'l', className: 'text-[0.6875rem] uppercase tracking-wide text-(--ui-text-tertiary)', children: 'address' }),
      h('input', { key: 'i', value: contactDraft.address, placeholder: 'paste the address',
        style: fieldProps({ fontFamily: MONO }),
        onChange: (e) => setContactDraft((d) => ({ ...d, address: e.target.value })) })
    ]),
    btn('Save contact', async () => {
      if (!contactDraft.name || !contactDraft.address) { setState((s) => ({ ...s, error: 'name and address are required' })); return }
      await act('/contact/save', { name: contactDraft.name, kind: contactDraft.kind,
                                   address: contactDraft.address, notes: contactDraft.notes,
                                   passphrase: contactPass }, 'contact saved')
      setContactDraft((d) => ({ ...d, name: '', address: '', notes: '' }))
    }, 'good')
  ])

  // ---- deposit addresses: receive here, merged home ----
  const depositCards = (state.deposits || []).length
    ? state.deposits.map((d) => h('div', {
        key: d.deposit_id,
        className: 'rounded-xl border-[3px] border-(--ui-border) p-3 flex flex-col gap-1.5',
        style: { background: 'var(--wallet-surface)' }
      }, [
        h('div', { key: 'top', className: 'flex flex-wrap items-center gap-2' }, [
          h('span', { key: 'l', className: 'text-[0.9375rem] font-medium',
                      children: d.label || '(no label)' }),
          h('span', { key: 'sp', className: 'flex-1' }),
          h('span', { key: 'b', className: 'font-mono text-[0.8125rem] tabular-nums',
                      children: d.balance == null ? 'offline' : `${fmt(d.balance, 'XMR')} XMR` }),
          h('span', { key: 'u', className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
                      children: d.unlocked && Number(d.unlocked) > 0
                        ? `${fmt(d.unlocked, 'XMR')} spendable`
                        : 'nothing spendable yet' })
        ]),
        h('div', { key: 'a', className: 'flex flex-wrap items-center gap-2' }, [
          h('button', {
            key: 'cp', type: 'button', title: 'click to copy',
            className: 'max-w-full break-all rounded-md border-[2px] border-(--ui-border) px-1.5 py-0.5 text-left font-mono text-[0.6875rem]',
            style: { background: 'var(--ui-bg-elevated, var(--ui-bg-secondary, transparent))' },
            onClick: () => copy(d.address, 'dep-' + d.deposit_id),
            children: copied === 'dep-' + d.deposit_id ? 'copied ✓' : d.address
          })
        ]),
        h('div', { key: 'acts', className: 'flex flex-wrap items-center gap-2' }, [
          btn('Merge now', () => act('/deposits/merge', { deposit_id: d.deposit_id },
              'deposit merged into the main account'), 'plain'),
          h('button', {
            key: 'auto', type: 'button',
            className: 'rounded-md border-[2px] px-2 py-1 text-[0.6875rem]',
            style: {
              borderColor: d.auto_merge ? 'var(--accent, #6366f1)' : 'var(--ui-border)',
              background: d.auto_merge ? 'color-mix(in srgb, var(--accent, #6366f1) 18%, transparent)' : 'transparent',
              color: d.auto_merge ? 'var(--foreground, #eef1f6)' : 'var(--ui-text-tertiary)'
            },
            onClick: () => act('/deposits/auto', { deposit_id: d.deposit_id, auto_merge: !d.auto_merge },
              d.auto_merge ? 'auto-merge off for this address' : 'auto-merge on for this address'),
            children: d.auto_merge ? 'auto-merge on' : 'auto-merge off'
          }),
          d.merged_total && d.merged_total !== '0'
            ? h('span', { key: 'm', className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
                          children: `merged ${fmt(d.merged_total, 'XMR')} XMR total` })
            : null,
          d.last_error
            ? h('span', { key: 'e', className: 'text-[0.6875rem] text-red-400',
                          children: String(d.last_error).slice(0, 80) })
            : null
        ])
      ]))
    : [h('div', { key: 'none', className: 'rounded-xl border-[3px] border-dashed border-(--ui-border) p-4 text-center text-xs text-(--ui-text-tertiary)',
                  children: 'No deposit addresses. Create one and give it to the person paying you: the money lands in its own account and merges into your balance.' })]

  const depositForm = h('div', { key: 'new-deposit', className: 'rounded-lg border-[3px] border-(--ui-border) p-3 flex flex-wrap items-end gap-2' }, [
    h('div', { key: 'l', className: 'flex flex-1 min-w-[12rem] flex-col gap-0.5' }, [
      h('span', { key: 'k', className: 'text-[0.6875rem] uppercase tracking-wide text-(--ui-text-tertiary)',
                  children: 'what is it for' }),
      h('input', { key: 'i', value: depositDraft, placeholder: 'order 42 · shop · invoice 118',
        style: fieldProps(), onChange: (e) => setDepositDraft(e.target.value) })
    ]),
    btn('New deposit address', () => act('/deposits/create', { label: depositDraft, auto_merge: true },
      'deposit address created — give out the address; it merges home when the funds unlock'), 'good')
  ])

  const receiptLimit = showAllReceipts ? 40 : 6
  // "Receipts" is what the human thinks it is: payments. Wallet/config/mandate events used to sit in
  // the same list, which is why clearing history still left a row behind (the archive marker).
  const paymentEvents = state.ledger.filter((e) => e.kind === 'spend_request')
  const otherEvents = state.ledger.filter((e) => e.kind !== 'spend_request')
  const lastArchive = state.ledger.find((e) => e.kind === 'ledger' && e.decision === 'archived')
  const listSource = showAllReceipts ? state.ledger : paymentEvents
  const receiptRows = listSource.slice().reverse().slice(0, receiptLimit).map((e) => {
    const d = e.details || {}
    const tone = e.decision === 'approved' ? 'text-emerald-400' : e.decision === 'denied' ? 'text-red-400' : 'text-(--ui-text-tertiary)'
    const coin = (COIN[d.asset] || {}).color
    return h('div', {
      key: e.seq,
      // An arbitrary-value grid class is only a column layout if the app's Tailwind build emitted it:
      // `grid-cols-[3rem_1fr_auto_5rem_auto]` was never compiled, so every receipt stacked all five
      // cells in one column — `#3`, kind, amount, "5m ago" and `again` each on its own line, filling
      // the full row width. The one class here is emitted; the columns are inline so they cannot go
      // missing again. minmax(0,1fr) lets the kind truncate instead of widening the row.
      className: 'grid items-center gap-2 border-b border-(--ui-border) py-1 text-[0.6875rem]',
      style: { gridTemplateColumns: '3rem minmax(0,1fr) minmax(0,auto) 5rem auto' }
    }, [
      h('span', { key: 's', className: 'font-mono text-(--ui-text-tertiary)', children: `#${e.seq}` }),
      h('span', { key: 'k', className: 'truncate', children: `${e.kind}${d.asset ? ' · ' + d.asset : ''}` }),
      h('span', { key: 'a', className: 'font-mono text-[0.8125rem] ' + tone,
                  style: coin ? { color: coin } : undefined,
                  children: d.amount ? `${fmt(d.amount, d.asset)} ${d.asset}` : e.decision }),
      h('span', { key: 't', className: 'text-right text-(--ui-text-tertiary)' },
        d.explorer
          ? h('a', { href: d.explorer, target: '_blank', rel: 'noreferrer',
                     className: 'underline hover:text-(--ui-text-primary)', children: ago(e.ts) })
          : ago(e.ts)),
      // "again": refile the same payment as a new request. It still needs approval — this only
      // saves typing, it never sends on its own.
      d.payee && d.amount && d.kind
        ? h('button', {
            key: 'again', type: 'button', title: `request ${d.amount} ${d.asset} to ${d.payee} again`,
            className: 'rounded-md border-[2px] border-(--ui-border) px-2 py-0.5 text-[0.6875rem] hover:bg-(--wallet-surface-strong)',
            onClick: () => {
              setDraft((cur) => ({ ...cur, kind: d.kind, asset: d.asset,
                                   amount: String(d.amount), payee: d.payee, sandbox_id: '',
                                   memo: `repeat of #${e.seq}`, why: `repeat of #${e.seq}` }))
              setState((s) => ({ ...s, error: null, note: `payment #${e.seq} loaded into the form above — check it and press Request payment` }))
            },
            children: 'again'
          })
        : h('span', { key: 'again-empty' })
    ])
    if (!showAllReceipts || (!d.tx_hash && !d.tx_key)) return row
    // The proof, when there is one: a block link where the chain allows it, and for Monero the
    // tx hash + tx key (a public explorer there would deanonymise the spenders).
    return [row, h('div', {
      key: `proof-${e.seq}`,
      className: 'flex flex-wrap items-center gap-3 border-b border-(--ui-border) pb-1 pl-[3.25rem] text-[0.6875rem] font-mono text-(--ui-text-tertiary)'
    }, [
      d.tx_hash ? h('span', { key: 'h', title: d.tx_hash, children: 'tx ' + String(d.tx_hash).slice(0, 18) + '…' }) : null,
      d.tx_key ? h('span', { key: 'k', title: d.tx_key, children: 'key ' + String(d.tx_key).slice(0, 14) + '… (proves the payment)' }) : null
    ])]
  }).flatMap((row) => (Array.isArray(row) ? row : [row]))

  // Token fallbacks: the app defines its own --ui-* variables; these chains keep the page legible
  // in a theme that does not (and never hard-code a colour the app cannot override).
  const accent = 'var(--accent, #6366f1)'
  const themeVars = {
    '--ui-border': 'var(--ui-stroke-secondary, rgba(186,196,212,0.34))',
    '--ui-text-primary': 'var(--foreground, currentColor)',
    '--ui-text-secondary': 'var(--muted-foreground, rgba(232,236,242,0.95))',
    // tertiary used to *be* muted-foreground, i.e. as loud as the values — a label could not be told
    // from the thing it labelled. A 55% mix makes the hierarchy visible without adding a colour.
    '--ui-text-tertiary': 'color-mix(in srgb, var(--muted-foreground, rgba(222,228,238,0.92)) 55%, transparent)',
    '--ui-bg-secondary': 'rgba(127,127,127,0.08)',
    '--ui-accent': accent,
    // Wallet surfaces: a touch of the accent so the page reads as its own thing on a black desktop.
    // No accent tint: the page sat on a blue wash that fought the app's own background. A few
    // percent of the foreground is enough to lift a card off black without changing its hue.
    '--wallet-surface': 'color-mix(in srgb, var(--foreground, #e8ecf2) 4%, transparent)',
    '--wallet-surface-strong': 'color-mix(in srgb, var(--foreground, #e8ecf2) 9%, transparent)',
    '--wallet-ring': `color-mix(in srgb, ${accent} 45%, transparent)`
  }
  // No page-wide tint: a radial accent wash over a black desktop reads as "blue theme". Cards lift
  // themselves off the background by a few percent of the foreground instead.
  const pageStyle = { ...themeVars }

  // WCAG 2.2 AA wants a 24 px target: several inline buttons here are py-0.5 (22 px) and the raw
  // checkboxes are 13 px, both under it. One scoped rule lifts every control, present and future,
  // without touching the density of the text around it.
  const hitAreas = h('style', { key: 'hit-areas', children:
    '.wallet-pane button, .wallet-pane select { min-height: 24px }'
    + '.wallet-pane label { min-height: 24px }'
    + '.wallet-pane input[type="checkbox"] { width: 16px; height: 16px; margin: 0 2px }' })

  // A first run is the setup card and nothing else: showing an empty Assets panel, an empty
  // approvals list and a request form beside "create your wallet" is how a new operator concludes
  // the wallet is already half-broken.
  if (firstRun && running && !state.loading) {
    return h('div', { style: pageStyle, className: 'wallet-pane flex h-full w-full flex-col items-center justify-center p-6 text-[0.9375rem] leading-relaxed' }, [
      h('div', { key: 'card', style: { width: '100%', maxWidth: '34rem' } }, [unlockBar])
    ])
  }

  // A locked wallet shows nothing but the way in. Balances, addresses, budgets, history and
  // pending cards all describe money the keystore has not opened: drawing them greyed-out tells
  // whoever is looking at the screen how much is there, and it makes the PIN look optional.
  if (locked && running && !state.loading && !firstRun) {
    return h('div', { style: pageStyle, className: 'wallet-pane flex h-full w-full flex-col items-center justify-center gap-3 p-6 text-[0.9375rem] leading-relaxed' }, [
      h('div', { key: 'k', className: 'text-[0.6875rem] font-semibold uppercase tracking-[0.14em] text-(--ui-text-tertiary)',
                 children: 'agent wallet' }),
      h('div', { key: 't', className: 'text-[1.25rem] font-semibold',
                 children: pinSet ? 'Enter your PIN' : 'Enter your passphrase' }),
      h('div', { key: 'n', className: 'text-[0.6875rem] text-(--ui-text-tertiary)', children: pinSet
        ? 'Six digits. It unlocks the wallet and authorises each payment.'
        : 'The passphrase opens the keystore.' }),
      h('div', { key: 'r', className: 'flex items-center gap-2' }, [
        h('input', {
          key: 'pw', type: 'password', value: passphrase,
          placeholder: pinSet ? '••••••' : 'keystore passphrase',
          inputMode: pinSet ? 'numeric' : 'text', autoComplete: 'off', autoFocus: true,
          style: fieldProps({ width: '13rem', textAlign: 'center',
                              letterSpacing: pinSet ? '0.35em' : 'normal' }),
          onChange: (e) => setPassphrase(e.target.value),
          onKeyDown: (e) => { if (e.key === 'Enter') unlockWith(passphrase) }
        }),
        btn('Unlock', () => unlockWith(passphrase), 'good')
      ]),
      state.error
        ? h('div', { key: 'e', className: 'text-[0.75rem]', style: { color: '#f87171' }, children: state.error })
        : null,
      pinSet ? (gatePass
        ? h('div', { key: 'pp', className: 'flex items-center gap-2' }, [
            h('input', {
              key: 'i', type: 'password', value: gatePassphrase, placeholder: 'keystore passphrase',
              autoComplete: 'off', style: fieldProps({ width: '13rem' }),
              onChange: (e) => setGatePassphrase(e.target.value),
              onKeyDown: (e) => { if (e.key === 'Enter') act('/unlock', { passphrase: gatePassphrase }, 'wallet unlocked') }
            }),
            btn('Unlock', () => act('/unlock', { passphrase: gatePassphrase }, 'wallet unlocked'), 'plain')
          ])
        : h('button', { key: 'b', className: 'text-[0.6875rem] underline text-(--ui-text-tertiary)',
                        onClick: () => setGatePass(true),
                        children: 'forgot the PIN? use the passphrase' })
      ) : null
    ])
  }

  return h('div', { style: pageStyle, className: 'wallet-pane flex h-full w-full flex-col gap-4 overflow-auto p-4 text-[0.9375rem] leading-relaxed' }, [
    hitAreas,
    header,
    state.bridge
      ? h('div', { key: 'bridge', className: 'rounded border border-amber-500/40 bg-amber-500/10 p-3 text-xs text-amber-300',
          children: 'The wallet bridge is not loaded in this Hermes backend yet (its API route mounts at backend start). Restart the backend once, then reopen this page. `hermes wallet status` works from a terminal meanwhile.' })
      : null,
    wallet.config_tampered
      ? h('div', { key: 'tamper', className: 'rounded border border-red-500/40 bg-red-500/10 p-3 text-xs text-red-300',
          children: 'Settings failed signature verification. Defaults restored and the passphrase prompt forced on.' })
      : null,
    state.error ? h('div', { key: 'err', className: 'rounded border border-red-500/40 p-2 text-xs text-red-300', children: state.error }) : null,
    state.note ? h('div', { key: 'note', className: 'rounded border-[3px] border-(--ui-border) p-2 text-xs text-(--ui-text-tertiary)', children: state.note }) : null,
    signerCard,
    pinSetupCard,
    passphraseCard,
    promptCard,
    receiveCard,
    unlockBar,
    locked && running && state.accounts.length && !state.accounts.some((a) => a.available)
      ? h('div', { key: 'locked-bal', className: 'rounded border-[3px] border-(--ui-border) p-2 text-xs text-(--ui-text-tertiary)',
          children: 'Balances load after you unlock: the signer needs the passphrase to open the wallet and ask the node.' })
      : null,
    // Only when there is no wallet at all. It used to sit above the Assets card on every visit,
    // telling a funded wallet to create one.
    noWalletAtAll
      ? h('div', { key: 'init', className: 'flex flex-wrap items-center gap-2' }, [
          h('span', { key: 'l', className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
                      children: 'No Monero wallet yet — create one (keys are generated inside the signer, never here).' }),
          btn('Create Monero wallet', () => act('/wallet/init', { kind: 'monero-mainnet' }, 'wallet created'), 'plain')
        ])
      : null,
    section('Assets', null, [
      // These pills used to sit in the section header's right-hand slot, where the layout squeezed
      // them into an unreadable blob next to the title. They are a control, so they live in the body
      // with the rows they control, and each pill refuses to shrink.
      h('div', { key: 'pills', className: 'flex flex-wrap items-center gap-1' },
        Array.from(new Set(state.accounts.map((a) => a.asset))).map((asset) => {
          const hidden = hiddenAssets.includes(asset)
          return h('button', {
            key: asset, type: 'button', onClick: () => toggleAsset(asset),
            title: hidden ? 'show in the wallet' : 'hide from the wallet',
            className: 'shrink-0 whitespace-nowrap rounded-full border px-2 py-0.5 text-[0.6875rem]',
            style: {
              minHeight: '28px',
              borderColor: hidden ? 'var(--ui-border)' : ((COIN[asset] || {}).color || 'var(--ui-border)'),
              color: hidden ? 'var(--ui-text-tertiary)' : coinInk(asset),
              opacity: hidden ? 0.45 : 1
            },
            children: `${(COIN[asset] || {}).glyph ? (COIN[asset] || {}).glyph + ' ' : ''}${asset}`
          })
        }).concat([h('span', { key: 'live', className: 'ml-2 shrink-0 text-[0.6875rem] text-(--ui-text-tertiary)',
                               title: 'coins whose balance was read from a node and came back without an error',
                               children: state.loading ? 'loading…'
                                 : `${state.accounts.filter((a) => a.available && !a.error).length} live` })])),
      spendLine,
      h('div', { key: 'grid', className: 'flex flex-col gap-2' }, assetRows)
    ]),
    section('Pending approvals', null, [
      wallet.human_gate && !locked && pending.length
        ? h('div', { key: 'hint', className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
                     children: 'Approve asks for the six-digit PIN, on the card that shows the destination and the amount; Deny needs nothing. A request the agent raised a prompt for is approved in the card at the top of the page.' })
        : null,
      h('div', { key: 'cards', className: 'flex flex-col gap-3' }, approvalCards)
    ]),
    section('New request', null, requestForm),
    section(`Auto-pay mandates (${activeMandates.length} active)`, null,
      h('div', { className: 'flex flex-col gap-3' }, [
        sectionPass('auto-pass', mandatePass, setMandatePass,
                    'pausing is free; resuming, editing and deleting need it'),
        mandateForm,
        h('div', { key: 'grid', className: 'flex flex-col gap-3' }, mandateCards)])),
    section(`Sandboxes (${state.sandboxes.filter((s) => s.status === 'active').length} active)`, null,
      h('div', { className: 'flex flex-col gap-2' }, [
        sectionPass('sandbox-pass', sandboxPass, setSandboxPass,
                    'needed to widen who may spend a sandbox, or to hand one to the agent; narrowing and taking it back are free'),
        h('div', { key: 'reap', className: 'flex items-center gap-2' }, [
          btn('Reap expired now', () => act('/sandbox/reap', {},
              'reaper ran — expired budgets swept and one-time sandboxes destroyed'), 'plain'),
          h('span', { key: 'n', className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
                      children: 'the daemon also runs this on a timer; nothing is swept without a receipt' })
        ]),
        h('div', { key: 'grid', className: 'grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3' }, sandboxCards),
        sandboxForm
      ])),
    // Contacts are not on the page: an address book read as clutter on a wallet whose job is to show
    // what you have and what it is about to pay. Still reachable from the CLI (`hermes wallet contacts`);
    // the pane keeps the save/delete calls so a future section needs no backend change.
    section('Receipts', null, [
      h('div', { key: 'hd', className: 'flex flex-wrap items-center gap-2' }, [
        paymentEvents.length > 6
          ? btn(showAllReceipts ? 'Payments only' : `Show all payments (${paymentEvents.length})`,
                () => setShowAllReceipts((v) => !v))
          : null,
        !showAllReceipts && otherEvents.length
          ? btn(`Show ${otherEvents.length} other event${otherEvents.length === 1 ? '' : 's'}`,
                () => setShowAllReceipts(true))
          : null,
        confirmClear
          ? h('div', { key: 'confirm', className: 'flex flex-wrap items-center gap-2' }, [
              btn('Yes, clear the list', () => {
                setConfirmClear(false)
                act('/ledger/archive', {}, 'history archived — the old chain is kept on disk')
              }, 'bad'),
              btn('Cancel', () => setConfirmClear(false))
            ])
          : btn('Clear history', () => setConfirmClear(true)),
        // A plain link, not a fetch: the browser's download handles it, so nothing here needs the
        // passphrase and a big ledger never has to pass through this page's state. The route reads
        // the same signed chain the list below does.
        h('a', {
          key: 'csv',
          href: '/api/plugins/agent-wallet/ledger.csv',
          download: `wallet-receipts-${new Date().toISOString().slice(0, 10)}.csv`,
          className: 'rounded-md border-[2px] border-(--ui-border) px-3 py-1.5 text-[0.8125rem] font-medium transition-colors hover:bg-(--ui-bg-secondary)',
          style: { color: 'inherit', textDecoration: 'none' },
          title: 'every receipt, as CSV — no passphrase needed; the server streams the same signed ledger',
          children: 'Download CSV'
        })
      ]),
      confirmClear
        ? h('div', { key: 'warn', className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
                     children: 'Clearing moves this chain to a timestamped file and starts a new one — nothing is deleted, so the record stays provable.' })
        : null,
      h('div', { key: 'list', className: 'rounded-lg border-[2px] border-(--ui-border) p-2 flex flex-col' },
        receiptRows.length
          ? receiptRows
          : h('div', { key: 'none', className: 'p-4 text-center text-xs text-(--ui-text-tertiary)',
                       children: showAllReceipts ? 'No events yet.' : 'No payments yet.' })),
      lastArchive && !showAllReceipts
        ? h('div', { key: 'arch', className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
                     children: `earlier history archived: ${(lastArchive.details && lastArchive.details.entries) || '?'} receipts in ${(lastArchive.details && lastArchive.details.file) || 'a file'}` })
        : null
    ])
  ])
}

/* ── settings page ──────────────────────────────────────────────────────── */

const KNOWN_CHAINS = [
  ['monero-mainnet', 'Monero (XMR)', 'XMR'],
  ['bitcoin-mainnet', 'Bitcoin (BTC)', 'BTC'],
  ['base-mainnet', 'Base — ETH + USDC', 'ETH'],
  ['polygon-mainnet', 'Polygon — POL + USDC', 'POL'],
  ['bitcoin-testnet', 'Bitcoin testnet', 'BTC'],
  ['base-sepolia', 'Base Sepolia (test)', 'ETH'],
  ['sim-mainnet', 'Simulator (play money)', 'XMR']
]

/**
 * Who may spend from this sandbox: a checklist of the profiles that exist, inside a dropdown.
 * Typing names invited typos that silently became an ACL nobody could satisfy.
 */
function ProfilePicker({ profiles, selected, onChange, disabled }) {
  // Chips, not a popover of checkboxes. The popover relied on an absolutely-positioned panel and on
  // checkbox change events and in the real pane picking a profile did nothing. A row of buttons with
  // an onClick is the interaction that is proven to work here, and the whole list stays visible, so
  // "which profiles may spend this budget" needs no second click to answer.
  const chosen = selected || []
  const chip = (key, label, active, onClick, hint) => h('button', {
    key, type: 'button', onClick, disabled: Boolean(disabled), title: hint || '',
    className: 'rounded-full border-[2px] px-2 py-0.5 text-[0.6875rem]',
    style: {
      minHeight: '28px',
      borderColor: active ? 'var(--accent, #6366f1)' : 'var(--ui-border)',
      backgroundColor: active ? 'color-mix(in srgb, var(--accent, #6366f1) 22%, transparent)' : 'transparent',
      color: active ? 'var(--foreground, #eef1f6)' : 'var(--ui-text-tertiary)',
      fontWeight: active ? 600 : 400,
      opacity: disabled ? 0.5 : 1
    },
    children: label
  })
  return h('div', { className: 'flex flex-col gap-1' }, [
    h('div', { key: 'l', className: 'text-[0.6875rem] uppercase tracking-wide text-(--ui-text-tertiary)',
               children: 'profiles that may spend' }),
    h('div', { key: 'row', className: 'flex flex-wrap items-center gap-1' }, [
      chip('__all', 'all profiles', chosen.length === 0, () => onChange([]),
           'every Hermes profile may spend from this budget'),
      ...profiles.map((name) => chip(name, name, chosen.includes(name), () => {
        const next = chosen.includes(name) ? chosen.filter((x) => x !== name) : [...chosen, name]
        onChange(next)
      })),
      profiles.length ? null : h('span', { key: 'none', className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
                                          children: 'no profiles found — the wallet daemon reports the ones installed' })
    ])
  ])
}

function SettingsPage({ ctx }) {
  const [info, setInfo] = useState(null)
  const [nodes, setNodes] = useState([])
  const [rpc, setRpc] = useState('')
  const [saving, setSaving] = useState(false)
  const [note, setNote] = useState(null)
  const [pass, setPass] = useState('')      // needed only for policy changes (caps, allowlists, mandates)
  const [gateArmed, setGateArmed] = useState(false)   // the "turn the prompt off" panel is open
  const [gateAck, setGateAck] = useState(false)       // "I understand what this allows"
  const [gatePass, setGatePass] = useState('')        // re-typed passphrase, verified by walletd

  const load = useCallback(() => {
    Promise.all([ctx.rest('/config'), ctx.rest('/nodes').catch(() => ({ nodes: [] }))])
      .then(([c, n]) => {
        setInfo(c)
        setNodes(n.nodes || [])
        setRpc(((c.settings || {}).monero_rpc_url) || '')
      })
      .catch(() => setInfo(null))
  }, [ctx])

  useEffect(() => { load() }, [load])

  if (!info) return h('div', { className: 'p-3 text-sm text-(--ui-text-tertiary)', children: 'wallet not reachable' })
  const s = info.settings || {}

  const save = async (body, message) => {
    setSaving(true)
    try {
      await ctx.rest('/config', { method: 'POST', body: pass ? { ...body, passphrase: pass } : body })
      setNote(message)
      load()
    } catch (err) {
      setNote(String((err && err.message) || err))
    } finally {
      setSaving(false)
    }
  }

  const rows = [
    ['passphrase prompt', s.human_gate ? 'required' : 'disabled by operator'],
    ['per-tx cap', JSON.stringify(s.per_tx_cap)],
    ['daily cap', JSON.stringify(s.daily_cap)],
    ['approval threshold', s.approval_threshold],
    ['chains', (s.chains_enabled || []).join(', ')],
    ['wallet provider', s.monero_provider || 'custom']
  ]

  const xmrNodes = nodes.filter((n) => (n.kind || '').startsWith('monero'))

  return h('div', { className: 'flex flex-col gap-3 p-3 text-sm' }, [
    h('div', { key: 'h', className: 'text-[0.6875rem] uppercase tracking-wider text-(--ui-text-tertiary)', children: 'Settings' }),
    h('div', { key: 'pw', className: 'flex flex-wrap items-center gap-2' }, [
      h('input', {
        key: 'i', type: 'password', value: pass, placeholder: 'passphrase (for caps, allowlists, Monero-only)',
        style: fieldProps({ flex: '1 1 auto', minWidth: '16rem' }),
        onChange: (e) => setPass(e.target.value)
      }),
      h('span', { key: 'n', className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
                  children: 'policy changes need it; toggling a coin does not' })
    ]),
    ...rows.map(([k, v]) => h('div', {
      key: k, className: 'flex justify-between gap-3 border-b border-(--ui-border) py-1 text-xs'
    }, [
      h('span', { key: 'k', className: 'text-(--ui-text-tertiary)', children: k }),
      h('span', { key: 'v', className: 'font-mono', children: String(v) })
    ])),

    h('div', { key: 'nodes-h', className: 'pt-2 text-[0.6875rem] uppercase tracking-wider text-(--ui-text-tertiary)',
               children: 'Wallet node (RPC the signer talks to)' }),
    h('div', { key: 'nodes', className: 'flex flex-col gap-1' },
      xmrNodes.length
        ? xmrNodes.map((n) => h('div', { key: n.label + n.url, className: 'flex items-center gap-2 text-xs' }, [
            pill(n.label, n.ok ? 'good' : 'bad'),
            h('span', { key: 'u', className: 'font-mono text-[0.6875rem] text-(--ui-text-tertiary) truncate', children: n.url }),
            h('span', { key: 'h2', className: 'ml-auto text-[0.6875rem] text-(--ui-text-tertiary)',
                        children: n.ok ? String(n.height) : (n.error || 'down') })
          ]))
        : h('div', { key: 'none', className: 'text-xs text-(--ui-text-tertiary)', children: 'no public endpoints known for this chain' })),

    h('div', { key: 'coins-h', className: 'pt-2 text-[0.6875rem] uppercase tracking-wider text-(--ui-text-tertiary)',
               children: 'Coins this wallet accepts' }),
    h('div', { key: 'coin-grid', className: 'grid grid-cols-2 gap-2' },
      KNOWN_CHAINS.map(([kind, label, hint]) => {
        const on = (s.chains_enabled || []).includes(kind)
        return h('label', {
          key: kind,
          className: 'flex items-start gap-2 rounded-md border-[2px] px-2 py-1.5 text-xs cursor-pointer',
          style: {
            borderColor: on
              ? `color-mix(in srgb, ${(COIN[hint] || { color: '#8b93a7' }).color} 60%, var(--ui-border))`
              : 'var(--ui-border)',
            background: on ? `color-mix(in srgb, ${(COIN[hint] || { color: '#8b93a7' }).color} 8%, transparent)` : 'transparent'
          }
        }, [
          h('input', {
            key: 'c', type: 'checkbox', checked: on, disabled: saving,
            onChange: (e) => {
              const next = (s.chains_enabled || []).filter((c) => c !== kind)
              if (e.target.checked) next.push(kind)
              save({ chains_enabled: next }, `${label} ${e.target.checked ? 'on' : 'off'}`)
            }
          }),
          h('span', { key: 'l', className: 'flex flex-col' }, [
            h('span', { key: 'n', className: 'font-medium', children: label }),
            h('span', { key: 't', className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
                        children: on ? 'on' : 'off' })
          ])
        ])
      })),
    h('div', { key: 'coins-note', className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
               children: 'Turning a coin on adds its chain to every balance, request and approval. XMR and USDC sign locally; Monero uses the bundled signer. Settings are keystore-signed, so this is the only way to change them.' }),

    h('div', { key: 'gate-h', className: 'pt-2 text-[0.6875rem] uppercase tracking-wider text-(--ui-text-tertiary)',
               children: 'Passphrase prompt' }),
    h('div', {
      key: 'gate', className: 'flex flex-col gap-2 rounded-lg border-[3px] p-3',
      style: {
        borderColor: s.human_gate ? 'color-mix(in srgb, #22c55e 45%, var(--ui-border))' : '#ef4444',
        background: s.human_gate ? 'transparent' : 'color-mix(in srgb, #ef4444 8%, transparent)'
      }
    }, [
      h('div', { key: 'row', className: 'flex flex-wrap items-center gap-2' }, [
        h('span', { key: 'v', className: 'text-sm font-medium',
                    children: s.human_gate ? 'On — every manual payment asks for your passphrase' : 'OFF — payments are approved by a click' }),
        h('span', { key: 'sp', className: 'flex-1' }),
        s.human_gate
          ? btn(gateArmed ? 'Cancel' : 'Turn off…', () => {
              setGateArmed((v) => !v); setGateAck(false); setGatePass('')
            }, 'bad')
          : btn('Turn it back on', async () => {
              setNote(null)
              try {
                await ctx.rest('/human-gate', { method: 'POST', body: { enabled: true, confirm: true } })
                setNote('passphrase prompt back on')
                load()
              } catch (err) {
                setNote(String((err && err.message) || err))
              }
            }, 'good')
      ]),
      !s.human_gate
        ? h('div', { key: 'warn', className: 'text-[0.6875rem] text-red-300',
                     children: 'With the prompt off, holding the operator token is enough to approve a payment — and that token is a file on this disk, readable by anything running as this user. Only leave it off for a wallet with no funds, or on a machine nobody else can touch.' })
        : null,
      gateArmed && s.human_gate
        ? h('div', { key: 'panel', className: 'flex flex-col gap-2 rounded-md border-[2px] border-red-500/50 p-2' }, [
            h('div', { key: 'w', className: 'text-[0.75rem] text-red-300',
                       children: 'What this changes: payments will be approved by clicking Approve, with no passphrase. Anything running as your user — including the shell the agent can run — can read the operator token and approve a payment it asked for.' }),
            h('label', { key: 'ack', className: 'flex items-start gap-2 text-[0.75rem]' }, [
              h('input', { key: 'c', type: 'checkbox', checked: gateAck,
                           onChange: (e) => setGateAck(e.target.checked) }),
              h('span', { key: 'l', children: 'I understand, and I want the prompt off for this wallet.' })
            ]),
            h('div', { key: 'pw', className: 'flex flex-wrap items-center gap-2' }, [
              h('input', {
                key: 'i', type: 'password', value: gatePass, placeholder: 'passphrase (verified, not cached)',
                style: fieldProps({ width: '14rem' }),
                onChange: (e) => setGatePass(e.target.value)
              }),
              btn('Turn off the prompt', async () => {
                const typed = gatePass || pass
                if (!gateAck) { setNote('tick "I understand" first'); return }
                if (!typed) { setNote('type your passphrase — the field above this card or the one here'); return }
                setNote(null)
                try {
                  await ctx.rest('/human-gate', { method: 'POST',
                    body: { enabled: false, passphrase: typed, confirm: true } })
                  setNote('prompt off — every payment is now approved by a click')
                  setGateArmed(false); setGateAck(false); setGatePass('')
                  load()
                } catch (err) {
                  setNote('could not turn it off: ' + String((err && err.message) || err))
                }
              }, 'bad')
            ])
          ])
        : null
    ]),

    h('div', { key: 'autolock-h', className: 'pt-2 text-[0.6875rem] uppercase tracking-wider text-(--ui-text-tertiary)',
               children: 'Auto-lock' }),
    h('div', { key: 'autolock', className: 'flex flex-wrap items-center gap-3' }, [
      h('select', {
        key: 'sel', value: String(s.autolock_minutes ?? 15),
        style: fieldProps({ paddingRight: '1.6rem' }),
        onChange: (e) => save({ autolock_minutes: Number(e.target.value) },
                              Number(e.target.value) === 0
                                ? 'auto-lock off — the wallet stays unlocked until you lock it'
                                : `auto-lock after ${e.target.value} min`)
      }, [['1', '1 min'], ['5', '5 min'], ['15', '15 min'], ['30', '30 min'], ['60', '1 hour'],
          ['0', 'off (never)']].map(([v, label]) => opt(v, label))),
      h('span', { key: 'n', className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
                  children: 'when the window passes the wallet locks itself: the signer is stopped, the adapter keys are dropped, and the encrypted keystore is the only thing left' })
    ]),

    h('div', { key: 'coins-pref', className: 'pt-1 text-[0.6875rem] uppercase tracking-wider text-(--ui-text-tertiary)',
               children: 'Default coin' }),
    h('div', { key: 'coins', className: 'flex flex-wrap items-center gap-3' }, [
      h('select', {
        key: 'pref', value: s.prefer_asset || 'XMR',
        style: fieldProps({ paddingRight: '1.6rem' }),
        onChange: (e) => save({ prefer_asset: e.target.value }, 'preferred coin saved')
      }, (s.assets_enabled || ['XMR']).map((a) => opt(a, `default ${a}`))),
      h('label', { key: 'prefer', className: 'flex items-center gap-2 text-xs' }, [
        h('input', {
          key: 'c', type: 'checkbox', checked: s.prefer_monero !== false,
          onChange: (e) => save({ prefer_monero: e.target.checked },
                                e.target.checked ? 'Prefer Monero: on' : 'Prefer Monero: off')
        }),
        h('span', { key: 'l', children: 'Prefer Monero' })
      ]),
      h('label', { key: 'mono', className: 'flex items-center gap-2 text-xs' }, [
        h('input', {
          key: 'c', type: 'checkbox', checked: Boolean(s.monero_only),
          onChange: (e) => save({ monero_only: e.target.checked }, e.target.checked ? 'Monero only: on' : 'Monero only: off')
        }),
        h('span', { key: 'l', children: 'Monero only' })
      ])
    ]),
    h('div', { key: 'prefer-note', className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
               children: 'Prefer Monero: the agent is told to pay in XMR, and an agent-filed payment in another coin has to say why — the reason is recorded with the receipt. Your own approvals are never blocked by it.' }),
    h('div', { key: 'mono-warn', className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
               children: 'Heads up: Monero is great for privacy but many merchants do not accept XMR. With “Monero only” on, any payment in another coin is refused — shops that only take BTC/USDC will not work until you switch it off.' }),

    h('div', { key: 'rpc', className: 'flex flex-wrap items-center gap-2 pt-1' }, [
      h('input', {
        key: 'i', value: rpc, placeholder: 'http://127.0.0.1:38084/json_rpc',
        style: fieldProps({ flex: '1 1 18rem', minWidth: '0', fontFamily: MONO }),
        onChange: (e) => setRpc(e.target.value)
      }),
      btn(saving ? 'Saving…' : 'Save node', () => save({ monero_rpc_url: rpc }, 'node saved'), 'good')
    ]),
    h('div', { key: 'hint', className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
               children: 'The signer talks to a wallet RPC you control (loopback or a tunnel). Point that RPC at any public node — nothing here runs a chain.' }),

    note ? h('div', { key: 'note', className: 'rounded border-[3px] border-(--ui-border) p-2 text-xs text-(--ui-text-tertiary)', children: note }) : null,
    h('div', { key: 'caps', className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
               children: 'Caps live in a keystore-signed file. Editing config.json by hand is detected and ignored.' })
  ])
}

const CONTRIBUTIONS = [
  { id: 'wallet-route', area: ROUTES_AREA, data: { path: ROUTE }, render: (ctx) => h(WalletPage, { ctx }) },
  { id: 'wallet-nav', area: SIDEBAR_NAV_AREA, data: { path: ROUTE, label: 'Wallet', codicon: 'credit-card' } },
  {
    id: 'wallet-open',
    area: PALETTE_AREA,
    data: {
      id: 'wallet.open',
      label: 'Open Wallet',
      keywords: ['wallet', 'crypto', 'approve', 'payments'],
      run: () => (host && typeof host.navigate === 'function' ? host.navigate(ROUTE) : null)
    }
  }
]

export default {
  id: ID,
  name: 'Agent Wallet',
  version: '0.9.2',
  defaultEnabled: true,
  /**
   * Register defensively. The Desktop SDK is the one surface that can gain an optional helper
   * (`registerMany`) between app versions; if it is absent we register one by one instead of
   * throwing, which the loader would report as the whole plugin failing to load.
   */
  register(ctx) {
    const contributions = CONTRIBUTIONS.map((c) => ({ ...c, render: c.render && (() => c.render(ctx)) }))
    if (typeof ctx.registerMany === 'function') {
      ctx.registerMany(contributions)
    } else {
      for (const c of contributions) {
        const fn = { [ROUTES_AREA]: 'registerRoute', [SIDEBAR_NAV_AREA]: 'registerSidebarNavPage',
                     [PALETTE_AREA]: 'registerPaletteCommand' }[c.area]
        if (fn && typeof ctx[fn] === 'function') ctx[fn](c)
        else console.warn(`[agent-wallet] this app build has no registrar for ${c.area}; skipping ${c.id}`)
      }
    }
    if (typeof ctx.registerSettingsPage === 'function') {
      ctx.registerSettingsPage({
        id: 'wallet-settings',
        title: 'Agent Wallet',
        render: () => h(SettingsPage, { ctx })
      })
    } else {
      console.warn('[agent-wallet] this app build has no settings pages; the wallet page still works')
    }
  }
}

// Exported for the render check: the profile chips are a pure function of (profiles, selected),
// so the toggle behaviour can be exercised in Node without the app. The QR encoder is exported for
// the same reason: its matrices are compared against a reference encoder, mask by mask.
export { ProfilePicker, qrEncode, paymentUri }
