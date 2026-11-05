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
  backgroundColor: 'var(--card, #23262c)',
  borderRadius: '8px',
  padding: '7px 9px',
  fontSize: '13px',
  lineHeight: '1.2',
  color: 'inherit',
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

const SELECT_FACE = 'var(--card, #23262c)'
const SELECT_INK = 'var(--foreground, #eef1f6)'
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
  'sim-mainnet': 'Simulator (no funds)'
}

function chainName(kind) {
  return CHAIN_NAME[kind] || String(kind).replace(/-mainnet$/, '').replace(/-/g, ' ')
}

/* ── formatting ─────────────────────────────────────────────────────────── */

function fmt(value, asset) {
  const spec = COIN[asset] || { decimals: 6 }
  const n = Number(value)
  if (!Number.isFinite(n)) return '0'
  const digits = n === 0 ? 0 : Math.min(spec.decimals, 8)
  return n.toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: digits })
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

function btn(label, onClick, tone) {
  const tones = {
    good: 'border-emerald-500/50 text-emerald-300 hover:bg-emerald-500/10',
    bad: 'border-red-500/50 text-red-300 hover:bg-red-500/10',
    plain: 'border-(--ui-border) hover:bg-(--ui-bg-secondary)'
  }
  return h('button', {
    type: 'button',
    className: 'rounded-md border-[2px] px-3 py-1.5 text-[0.8125rem] font-medium transition-colors ' + (tones[tone] || tones.plain),
    onClick,
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
    className: 'flex flex-col gap-3 rounded-xl border-[3px] border-(--ui-border) p-3',
    style: { background: 'var(--wallet-surface)' }
  }, [
    h('div', { key: 'h', className: 'flex flex-wrap items-center gap-2 border-b-2 border-(--ui-border) pb-2' }, [
      h('h2', {
        key: 't',
        className: 'text-[0.8125rem] font-semibold uppercase tracking-[0.10em] text-(--ui-text-secondary)',
        children: title
      }),
      h('span', { key: 'sp', className: 'flex-1' }),
      right ? h('div', { key: 'r', className: 'flex flex-wrap items-center gap-2' }, right) : null
    ]),
    h('div', { key: 'b', className: 'flex flex-col gap-3' }, children)
  ])
}

/* ── page ───────────────────────────────────────────────────────────────── */

function WalletPage({ ctx }) {
  const [state, setState] = useState({
    status: null, accounts: [], requests: [], mandates: [], sandboxes: [], contacts: [], ledger: [],
    spend: null, incoming: null,
    // true until the first /status + /balances settle: the pane used to claim "no balances yet"
    // while it was still loading, which reads as an empty wallet.
    loading: true,
    accountsStale: false,
    error: null, note: null, bridge: false, busy: false
  })
  const [passphrase, setPassphrase] = useState('')
  const [passphrase2, setPassphrase2] = useState('')      // confirmation on the first-run form
  const [confirmAmount, setConfirmAmount] = useState({})
  // Accepting a payment is a two-step thing when the passphrase prompt is on: press Accept, then type
  // the passphrase in the row itself. There is no page-wide passphrase field any more - it made it
  // look like the passphrase was a property of the section instead of a per-payment authorisation.
  // A receive address is shown only while the operator asks for it: one reveal per row, cleared when
  // this page goes away or the window is left (a beholder of the screen should not inherit it).
  const [revealed, setRevealed] = useState({})
  const [copied, setCopied] = useState('')
  const [accepting, setAccepting] = useState(null)
  const [acceptPass, setAcceptPass] = useState('')

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
  const [contactPass, setContactPass] = useState('')      // only needed to change an address / remove
  const [confirmContact, setConfirmContact] = useState(null)
  const [promptAmount, setPromptAmount] = useState('')
  const [mandateDraft, setMandateDraft] = useState({ payee: '', per_tx: '', period_cap: '',
                                                     interval: '1d', label: '', kind: '' })
  const [draft, setDraft] = useState({ kind: '', asset: '', amount: '', payee: '', why: '' })
  const [showAllReceipts, setShowAllReceipts] = useState(false)
  const [sandboxDraft, setSandboxDraft] = useState({ kind: 'monero-mainnet', asset: 'XMR', budget: '', hours: '24', label: '', profiles: [], oneTime: false, destruct: '1800' })
  const [profiles, setProfiles] = useState([])          // profiles that actually exist on this machine
  const [hiddenAssets, setHiddenAssets] = useState([])
  const [showCoins, setShowCoins] = useState(false)   // the wallet-wide coin menu
  const [profileDraft, setProfileDraft] = useState({})
  const [contactDraft, setContactDraft] = useState({ name: '', kind: 'monero-mainnet', address: '', notes: '' })
  const [showReceive, setShowReceive] = useState(false)   // the receive card, closed by default
  const [receiveLabel, setReceiveLabel] = useState('')
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

  const refresh = useCallback(async () => {
    try {
      const status = await send('/status')
      const [accountsRes, requests, mandates, sandboxes, contacts, ledger, profs, spend, incoming] = await Promise.all([
        // null, not an empty list: a failed read must not be painted as "this wallet holds nothing"
        send('/balances').catch(() => null),
        send('/requests').catch(() => ({ requests: [] })),
        send('/mandates').catch(() => ({ mandates: [] })),
        send('/sandboxes').catch(() => ({ sandboxes: [] })),
        send('/contacts').catch(() => ({ contacts: [] })),
        send('/ledger?limit=40').catch(() => ({ entries: [] })),
        send('/profiles').catch(() => ({ profiles: [] })),
        // Added after the first release: on a backend that predates them these 404 and the pane
        // simply shows nothing, never an error banner about a route the operator cannot fix.
        send('/spend').catch(() => null),
        send('/incoming').catch(() => null)
      ])
      // /sandboxes carries the profile list. The dashboard imports a plugin's routes once at
      // startup, so a route added later 404s until the backend restarts - and then the picker had
      // nothing to show. This way the list arrives on a route that predates it.
      // the daemon nests everything under wallet; and it is read from /status first because that
      // route exists on every backend version, while /profiles only exists after a restart
      setProfiles((status.wallet && status.wallet.profiles) || sandboxes.profiles || profs.profiles || [])
      // Shape-check before trusting either one: a half-implemented route must not paint a section.
      const spendOk = spend && typeof spend.assets === 'object' && spend.assets !== null
      const incomingOk = incoming && Array.isArray(incoming.orders)
      const accountsOk = accountsRes && Array.isArray(accountsRes.accounts)
      setState((s) => ({
        ...s, status, bridge: false, loading: false,  // an error is cleared by the next action, not by a poll

        accounts: accountsOk ? accountsRes.accounts : s.accounts,
        accountsStale: !accountsOk,
        requests: requests.requests || [],
        mandates: mandates.mandates || [], sandboxes: sandboxes.sandboxes || [],
        contacts: contacts.contacts || [], ledger: ledger.entries || [],
        spend: spendOk ? spend : null,
        incoming: incomingOk ? incoming.orders : null
      }))
    } catch (err) {
      const message = String((err && err.message) || err)
      const missing = /\b404\b|not found/i.test(message)
      // loading is cleared even on failure: a spinner that never stops is a worse lie than an error
      setState((s) => ({ ...s, bridge: missing, loading: false, error: missing ? null : message }))
    }
  }, [send])

  useEffect(() => {
    refresh()
    ctx.setInterval(refresh, 3000)
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
      await send(path, body || {})
      setPassphrase('')
      setState((s) => ({ ...s, note: note || null, error: null, busy: false }))
      await refresh()
    } catch (err) {
      setState((s) => ({ ...s, error: String((err && err.message) || err), note: null, busy: false }))
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
    h('div', { key: 'coins', className: 'relative' }, [
      h('button', {
        key: 'btn', type: 'button',
        className: 'flex items-center gap-1 rounded-full border-[2px] border-(--ui-border) px-2 py-0.5 text-[0.6875rem]',
        onClick: () => setShowCoins((v) => !v),
        title: 'which coins this wallet holds',
        children: [
          h('span', { key: 'l', children: `coins ${(wallet.assets || []).length}` }),
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
                  const next = on ? current.filter((a) => a !== asset) : [...current, asset]
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
    btn('Refresh', refresh),
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
    state.sandboxes.length
      ? h('div', { key: 'sb', className: 'flex flex-col gap-0.5' }, [
          h('span', { key: 'l', className: 'text-[0.6875rem] uppercase tracking-wide text-(--ui-text-tertiary)', children: 'from sandbox' }),
          h('select', { style: fieldProps({ paddingRight: '1.6rem' }),
            key: 's', value: draft.sandbox_id || '', style: fieldProps(),
            onChange: (e) => setDraft((d) => ({ ...d, sandbox_id: e.target.value }))
          }, [
            opt('', 'my wallet (needs approval)'),
            ...state.sandboxes.map((s) => opt(
              s.sandbox_id,
              `${s.label || s.sandbox_id} · ${fmt(s.remaining, s.asset)} ${s.asset} left${s.enabled ? '' : ' (unfunded)'}`))
          ])
        ])
      : null,
    h('div', { key: 'p', className: 'flex flex-col gap-0.5 flex-1 min-w-[16rem]' }, [
      h('span', { key: 'l', className: 'text-[0.6875rem] uppercase tracking-wide text-(--ui-text-tertiary)', children: 'payee' }),
      h('input', {
        key: 'i', value: draft.payee, placeholder: 'address',
        style: fieldProps({ fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace' }),
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

  const firstRun = running && state.status && state.status.wallet && state.status.wallet.initialized === false
  const unlockBar = firstRun
    ? h('div', { key: 'first-run', className: 'rounded-lg border-[3px] border-amber-500/40 p-3 flex flex-col gap-2' }, [
        h('div', { key: 'h', className: 'text-xs font-medium', children: 'Set a passphrase — this wallet has no keystore yet' }),
        h('div', { key: 'n', className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
                   children: 'It encrypts the keys and authorises every payment. There is no recovery: write it down.' }),
        h('div', { key: 'r', className: 'flex flex-wrap items-center gap-2' }, [
          h('input', {
            key: 'pw', type: 'password', value: passphrase, placeholder: 'new passphrase (min 8)',
            style: fieldProps({ width: '14rem' }),
            onChange: (e) => setPassphrase(e.target.value)
          }),
          h('input', {
            key: 'pw2', type: 'password', value: passphrase2, placeholder: 'type it again',
            style: fieldProps({ width: '14rem' }),
            onChange: (e) => setPassphrase2(e.target.value)
          }),
          btn('Create wallet', () => {
            if (!passphrase || passphrase.length < 8) {
              setState((s) => ({ ...s, error: 'a passphrase of at least 8 characters is required' }))
              return
            }
            if (passphrase !== passphrase2) {
              setState((s) => ({ ...s, error: 'the two passphrases do not match' }))
              return
            }
            act('/keystore/create', { passphrase }, 'keystore created — unlock to continue')
          }, 'good')
        ])
      ])
    : locked && running
    ? h('div', { key: 'unlock', className: 'rounded-lg border-[3px] border-(--ui-border) p-3 flex flex-wrap items-center gap-2' }, [
        h('span', { key: 'l', className: 'text-xs text-(--ui-text-tertiary)', children: 'Passphrase — typed here, never sent to the agent' }),
        h('div', { key: 'sp', className: 'flex-1' }),
        h('input', {
          key: 'pw', type: 'password', value: passphrase, placeholder: 'keystore passphrase',
          style: fieldProps({ width: '14rem' }),
          onChange: (e) => setPassphrase(e.target.value),
          onKeyDown: (e) => { if (e.key === 'Enter') act('/unlock', { passphrase }, 'wallet unlocked') }
        }),
        btn('Unlock', () => act('/unlock', { passphrase }, 'wallet unlocked'), 'good')
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
                  style: { background: 'var(--card, #23262c)' },
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

  // The agent cannot ask for a passphrase in chat, so it raises a prompt here.
  const prompt = state.status && state.status.prompt
  const promptRequest = prompt && prompt.request_id
    ? state.requests.find((r) => r.request_id === prompt.request_id)
    : null
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

  const promptCard = prompt
    ? h('div', {
        key: 'operator-prompt', className: 'rounded-xl border-[3px] p-3 flex flex-col gap-2',
        style: {
          borderColor: '#f59e0b',
          background: 'color-mix(in srgb, #f59e0b 10%, transparent)'
        }
      }, [
        h('div', { key: 'h', className: 'text-[0.75rem] font-semibold uppercase tracking-[0.08em]',
                   children: prompt.kind === 'unlock' ? 'the agent is asking you to unlock' : 'the agent is waiting for your approval' }),
        h('div', { key: 'm', className: 'text-sm', children: prompt.message }),
        promptRequest
          ? h('div', { key: 'd', className: 'font-mono text-[0.6875rem] text-(--ui-text-tertiary)',
                       children: `${fmt(promptRequest.amount, promptRequest.asset)} ${promptRequest.asset} · ${shortAddr(promptRequest.payee)} · ${chainName(promptRequest.kind)}` })
          : null,
        h('div', { key: 'r', className: 'flex flex-wrap items-center gap-2' }, [
          promptRequest
            ? h('input', {
                key: 'amt', value: promptAmount, inputMode: 'decimal',
                placeholder: `type ${promptRequest.amount} to confirm`,
                style: fieldProps({ width: '10rem' }),
                onChange: (e) => setPromptAmount(e.target.value)
              })
            : null,
          h('input', {
            key: 'pw', type: 'password', value: passphrase, placeholder: 'passphrase — typed here, never sent to the agent',
            autoFocus: !promptRequest,
            style: fieldProps({ width: '16rem' }),
            onChange: (e) => setPassphrase(e.target.value),
            onKeyDown: (e) => { if (e.key === 'Enter') act('/unlock', { passphrase }, 'wallet unlocked') }
          }),
          promptRequest && promptRequest.approval_id
            ? btn('Approve', () => {
                act('/approve', {
                  request_id: promptRequest.request_id, approval_id: promptRequest.approval_id,
                  confirm_amount: promptAmount || null, passphrase
                }, 'payment approved')
                setPromptAmount('')
              }, 'good')
            : btn('Unlock', () => act('/unlock', { passphrase }, 'wallet unlocked'), 'good'),
          promptRequest
            ? btn('Deny', () => act('/deny', {
                request_id: promptRequest.request_id, approval_id: promptRequest.approval_id || '',
                reason: 'denied from the prompt'
              }, 'payment denied'))
            : null,
          btn('Dismiss', () => act('/prompt/clear', {}, null))
        ])
      ])
    : null

  const assetRows = state.accounts.length
    ? visibleAccounts.map((account) => {
        const spec = COIN[account.asset] || { name: account.asset }
        const color = (COIN[account.asset] || { color: '#8b93a7' }).color
        const shown = Boolean(revealed[account.kind])
        const isCopied = copied === account.kind
        return h('div', {
          key: account.kind,
          className: 'flex flex-col gap-2 rounded-xl border-[3px] px-3 py-2.5',
          style: {
            background: 'var(--wallet-surface)',
            borderColor: `color-mix(in srgb, ${color} 55%, var(--ui-border))`,
            borderLeft: `6px solid ${color}`
          }
        }, [
          h('div', { key: 'top', className: 'flex items-center gap-3' }, [
            CoinIcon({ asset: account.asset, size: 30 }),
            // The amount belongs next to the coin it is denominated in, not pinned to the far edge of
            // a wide row where the eye has to travel to connect the two.
            h('div', { key: 'm', className: 'flex min-w-0 flex-1 flex-wrap items-baseline gap-x-3 gap-y-0.5' }, [
              h('span', { key: 'a', className: 'text-[0.9375rem] font-medium whitespace-nowrap',
                          children: account.asset + ' · ' + (spec.name || '') }),
              h('span', { key: 'b', className: 'font-mono text-[1rem] font-semibold tabular-nums whitespace-nowrap',
                          style: { color: color || undefined },
                          children: account.balance == null
                            ? (account.state === 'locked' ? 'locked' : 'offline')
                            : fmt(account.balance, account.asset) + ' ' + account.asset }),
              h('span', { key: 'c', className: 'text-[0.6875rem] text-(--ui-text-tertiary) truncate',
                          title: account.error || '',
                          children: [chainName(account.kind), account.provider,
                                     account.address ? 'address ready'
                                       : account.state === 'locked'
                                         ? 'locked — unlock to show the address'
                                         : 'address unavailable right now',
                                     account.error ? String(account.error).slice(0, 60) : '']
                            .filter(Boolean).join(' · ') })
            ]),
            // A row with an error is not live whatever `available` claims: "live" beside "rpc
            // unreachable" was the lie that made a dead node look healthy.
            account.error ? pill('error', 'bad')
              : account.available ? pill('live', 'good')
              : pill(account.state === 'locked' ? 'locked' : 'offline', account.state === 'locked' ? 'flat' : 'bad')
          ]),
          account.address
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
                    onClick: () => setRevealed((m) => ({ ...m, [account.kind]: !m[account.kind] })),
                    children: shown ? 'hide address' : 'show address'
                  }),
                  shown
                    ? h('button', {
                        key: 'copy', type: 'button',
                        className: 'rounded-md border-[2px] border-(--ui-border) px-2 py-1 text-[0.6875rem]',
                        onClick: () => copy(account.address, account.kind),
                        children: isCopied ? 'copied ✓' : 'copy'
                      })
                    : null,
                  shown
                    ? h('span', { key: 'n', className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
                                  children: 'anyone can send to this address; it is not a secret, it is just not left on screen' })
                    : null
                ]),
                shown
                  ? h('div', {
                      key: 'full', className: 'select-all break-all rounded-md border-[2px] border-(--ui-border) px-2 py-1 font-mono text-[0.6875rem]',
                      style: { background: 'var(--card, #23262c)' },
                      onClick: () => copy(account.address, account.kind),
                      title: 'click to copy',
                      children: account.address
                    })
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
        const expired = r.approval_expires_at && Date.parse(r.approval_expires_at) < Date.now()
        return h('div', {
        key: r.request_id,
        className: 'rounded-lg border-[3px] border-amber-500/70 p-3 flex flex-col gap-2',
        style: { background: `color-mix(in srgb, ${(COIN[r.asset] || { color: '#f59e0b' }).color} 6%, transparent)` }
      }, [
        h('div', { key: 'top', className: 'flex items-center gap-2' }, [
          CoinIcon({ asset: r.asset, size: 26 }),
          h('span', { key: 'amt', className: 'font-mono text-[1.875rem] font-semibold tabular-nums',
                      style: { color: (COIN[r.asset] || {}).color || undefined },
                      children: `${fmt(r.amount, r.asset)} ${r.asset}` }),
          h('span', { key: 'sp', className: 'flex-1' }),
          r.sandbox_id ? pill('sandbox', 'flat') : null,
          pill(chainName(r.kind), 'flat')
        ]),
        r.funding_for
          ? h('div', { key: 'arm', className: 'rounded-md border-[2px] border-amber-500/50 px-2 py-1 text-[0.6875rem] text-amber-300',
                       children: 'This is funding a sandbox. Approving it hands that budget to the agent, which can then spend it without asking you again.' })
          : null,
        r.sweep_of
          ? h('div', { key: 'sweep', className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
                       children: 'A sweep: this returns a sandbox balance to your own address, so it does not need your approval on the payment itself.' })
          : null,
        h('div', { key: 'who', className: 'flex flex-col gap-0.5' }, [
          h('div', { key: 'to', className: 'text-[0.6875rem] uppercase tracking-wide text-(--ui-text-tertiary)',
                     children: 'pay to' }),
          h('div', { key: 'p', className: 'font-mono text-[0.75rem]', title: r.payee,
                     children: shortAddr(r.payee) }),
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
          h('div', { key: 'fee', className: 'flex flex-wrap items-center gap-3 text-[0.6875rem] text-(--ui-text-tertiary)' }, [
            h('span', { key: 'f', children: r.settles === 'x402'
              ? 'no fee — the seller settles this authorisation on-chain'
              : (r.fee ? `network fee ${fmt(r.fee, r.asset)} ${r.asset}` : 'network fee added when it is signed') }),
            r.approval_expires_at
              ? h('span', { key: 'e', children: 'offer expires ' + String(r.approval_expires_at).slice(11, 16) + ' UTC' })
              : null
          ])
        ]),
        null,
        h('div', { key: 'row', className: 'flex flex-wrap items-center gap-2' }, [
          expired
            ? h('span', { key: 'exp', className: 'text-xs text-(--ui-text-tertiary)',
                          children: 'This offer expired — ask the agent to request it again.' })
            : null,
          expired ? null : h('input', {
            key: 'cf', placeholder: 'type the amount to confirm', title: 'this is the guard: type the number yourself',
            style: fieldProps({ width: '12rem' }),
            value: confirmAmount[r.request_id] || '',
            onChange: (e) => setConfirmAmount((m) => ({ ...m, [r.request_id]: e.target.value }))
          }),
          expired ? null : (accepting === r.request_id
            ? [
                h('input', {
                  key: 'ap', type: 'password', placeholder: 'wallet passphrase',
                  style: fieldProps({ width: '15rem' }),
                  value: acceptPass, autoFocus: true,
                  onChange: (e) => setAcceptPass(e.target.value)
                }),
                btn('Confirm payment', async () => {
                  if (!acceptPass) return
                  await act('/approve', {
                    request_id: r.request_id,
                    approval_id: r.approval_id,
                    confirm_amount: confirmAmount[r.request_id] || null,
                    passphrase: acceptPass
                  }, 'payment approved')
                  setAccepting(null); setAcceptPass('')
                }, 'good'),
                btn('Cancel', () => { setAccepting(null); setAcceptPass('') }, 'flat')
              ]
            : btn('Accept', () => {
                if (wallet.human_gate) { setAccepting(r.request_id); setAcceptPass(''); return }
                act('/approve', {
                  request_id: r.request_id,
                  approval_id: r.approval_id,
                  confirm_amount: confirmAmount[r.request_id] || null
                }, 'payment approved')
              }, 'good')),
          btn('Decline', () => act('/deny', {
            request_id: r.request_id, approval_id: r.approval_id, reason: 'denied in UI'
          }, 'payment denied'), 'bad')
        ])
      ])
      })
    : [h('div', {
        key: 'none',
        className: 'rounded-lg border-[3px] border-dashed border-(--ui-border) p-4 text-center text-xs text-(--ui-text-tertiary)',
        children: 'Nothing waiting for approval.'
      })]

  // Incoming payments: one row per address the operator asked for. Absent route (older backend) or
  // an empty list hides the whole section — an empty "Orders" box would just be noise.
  const orders = Array.isArray(state.incoming) ? state.incoming : []
  const orderCards = orders.map((o, i) => {
    const status = String(o.status || 'waiting')
    const label = o.label || `order ${i + 1}`
    const key = o.address ? `incoming:${o.address}` : `incoming:${i}`
    const expected = Number(o.expected || 0)
    const received = Number(o.received || 0)
    return h('div', {
      key,
      className: 'flex flex-col gap-1.5 rounded-lg border-[3px] p-3',
      style: {
        borderColor: status === 'paid'
          ? 'color-mix(in srgb, #22c55e 45%, var(--ui-border))'
          : status === 'partial'
            ? 'color-mix(in srgb, #f59e0b 40%, var(--ui-border))'
            : 'var(--ui-border)'
      }
    }, [
      h('div', { key: 'top', className: 'flex flex-wrap items-center gap-2' }, [
        CoinIcon({ asset: o.asset, size: 22 }),
        h('span', { key: 'l', className: 'min-w-0 truncate text-sm', title: label, children: label }),
        h('span', { key: 'sp', className: 'flex-1' }),
        h('span', { key: 'a', className: 'whitespace-nowrap font-mono text-xs',
                    children: `${fmt(received, o.asset)} / ${fmt(expected, o.asset)} ${o.asset}` }),
        pill(status, status === 'paid' ? 'good' : status === 'partial' ? 'warn' : 'flat')
      ]),
      h('div', { key: 'meta', className: 'flex flex-wrap items-center gap-2 text-[0.6875rem] text-(--ui-text-tertiary)' }, [
        o.address
          ? h('span', { key: 'addr', className: 'break-all font-mono', title: o.address, children: shortAddr(o.address) })
          : null,
        h('span', { key: 'k', children: chainName(o.kind)
          + (o.account_index != null ? ` · account #${o.account_index}` : '') }),
        o.address
          ? btn(copied === key ? 'copied ✓' : 'copy address', () => copy(o.address, key))
          : null,
        expected > 0 && received < expected
          ? h('span', { key: 'left', children:
              `${fmt(expected - received, o.asset)} ${o.asset} still expected` })
          : null
      ])
    ])
  })

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
          m.status === 'active'
            ? btn('Revoke', () => act('/mandates/revoke', { mandate_id: m.mandate_id }, 'mandate revoked'), 'bad')
            : null,
          h('select', { style: fieldProps({ paddingRight: '1.6rem' }),
            key: 'iv', value: '', title: 'change how often the period cap resets',
            style: fieldProps({ padding: '4px 6px', fontSize: '12px', minHeight: '26px' }),
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
               children: 'A standing permission: when the agent asks to pay this payee, it goes through without asking you again — up to the caps, until it expires or you pause it. Nothing is paid on a timer.' }),
    h('div', { key: 'row1', className: 'flex flex-wrap items-end gap-2' }, [
      h('input', { key: 'payee', value: mandateDraft.payee, placeholder: 'payee address or contact name',
                   style: fieldProps({ flex: '1 1 auto', minWidth: '18rem', fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace' }),
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
        h('div', { key: 'g', className: 'flex items-end gap-1' }, [
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
        h('div', { key: 'g', className: 'flex items-end gap-1' }, [
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
                  children: 'needs the passphrase: this authorises spending without asking again' })
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
            pill(sb.status, sb.status === 'active' ? 'good' : (sb.status === 'expired' || sb.status === 'destroyed') ? 'bad' : 'warn')
          ]),
          h('div', { key: 'nums', className: 'flex items-baseline justify-between font-mono text-xs' }, [
            h('span', { key: 'r', children: `${fmt(sb.remaining, sb.asset)} left` }),
            h('span', { key: 'b', className: 'text-(--ui-text-tertiary)', children: `of ${fmt(sb.budget, sb.asset)}` })
          ]),
          h('div', { key: 'bar', className: 'h-1.5 w-full rounded-full', style: { background: 'rgba(127,127,127,0.2)' } },
            h('div', { className: 'h-1.5 rounded-full', style: { width: pct + '%', background: (COIN[sb.asset] || { color: '#f26822' }).color } })),
          h('div', { key: 'meta', className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
                     children: `account #${sb.account_index} · expires ${sb.expires_at}` }),
          sb.mode === 'one_time'
            ? h('div', { key: 'sd', className: 'text-[0.6875rem]',
                style: { color: sb.destroy_blocked ? '#f59e0b' : 'var(--ui-text-tertiary)' },
                children: sb.destroyed_at
                  ? `destroyed ${sb.destroyed_at} — its key material is gone`
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
          sb.status === 'active'
            ? h('div', { key: 'acts', className: 'flex gap-2' }, [
                Number(sb.remaining) > 0
                  ? btn('Withdraw left', () => act('/sandbox/sweep', { sandbox_id: sb.sandbox_id },
                        'sweeping the remaining budget back to the main account'), 'plain')
                  : null,
                btn('Revoke', () => act('/sandbox/revoke', { sandbox_id: sb.sandbox_id }, 'sandbox revoked'), 'bad')
              ])
            : null
        ])
      })
    : [h('div', { key: 'none', className: 'rounded-xl border-[3px] border-dashed border-(--ui-border) p-4 text-center text-xs text-(--ui-text-tertiary)',
                  children: 'No sandboxes. Carve one out and the agent can spend inside it without asking you every time.' })]

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
    btn('Create sandbox', async () => {
      if (!sandboxDraft.budget) { setState((s) => ({ ...s, error: 'budget is required' })); return }
      const asset = sandboxDraft.asset
      await act('/sandbox/create', {
        kind: sandboxDraft.kind, asset, budget: sandboxDraft.budget,
        lifetime_hours: Number(sandboxDraft.hours) || 24, label: sandboxDraft.label,
        allowed_profiles: sandboxDraft.profiles || [],
        mode: sandboxDraft.oneTime ? 'one_time' : 'budget',
        self_destruct_seconds: Number(sandboxDraft.destruct) || 1800
      }, sandboxDraft.oneTime
        ? 'sandbox created — one payment, then it destroys itself'
        : 'sandbox created — fund it from pending approvals')
      setSandboxDraft({ kind: sandboxDraft.kind, budget: '', hours: '24', label: '', profiles: [],
                        oneTime: false, destruct: '1800' })
    }, 'good')
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
                  children: 'No contacts yet. Say “send Aiden 5” and the agent will ask for the address once, then remember it.' })]

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
        style: fieldProps({ fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace' }),
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
      className: 'grid grid-cols-[3rem_1fr_auto_5rem_auto] items-center gap-2 border-b border-(--ui-border) py-1 text-[0.6875rem]'
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
    '--ui-text-tertiary': 'var(--muted-foreground, rgba(222,228,238,0.92))',
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

  return h('div', { style: pageStyle, className: 'flex h-full w-full flex-col gap-4 overflow-auto p-4 text-[0.9375rem] leading-relaxed' }, [
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
    promptCard,
    receiveCard,
    unlockBar,
    locked && running && state.accounts.length && !state.accounts.some((a) => a.available)
      ? h('div', { key: 'locked-bal', className: 'rounded border-[3px] border-(--ui-border) p-2 text-xs text-(--ui-text-tertiary)',
          children: 'Balances load after you unlock: the signer needs the passphrase to open the wallet and ask the node.' })
      : null,
    h('div', { key: 'init', className: 'flex flex-wrap items-center gap-2' }, [
      h('span', { key: 'l', className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
                  children: 'No wallet for a chain? Create one — keys are generated inside the signer, never here.' }),
      btn('Create Monero wallet', () => act('/wallet/init', { kind: 'monero-mainnet' }, 'wallet created'), 'plain')
    ]),
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
              borderColor: hidden ? 'var(--ui-border)' : ((COIN[asset] || {}).color || 'var(--ui-border)'),
              color: hidden ? 'var(--ui-text-tertiary)' : ((COIN[asset] || {}).color || 'var(--foreground)'),
              opacity: hidden ? 0.45 : 1
            },
            children: `${(COIN[asset] || {}).glyph ? (COIN[asset] || {}).glyph + ' ' : ''}${asset}`
          })
        }).concat([h('span', { key: 'live', className: 'ml-2 shrink-0 text-[0.6875rem] text-(--ui-text-tertiary)',
                               children: state.loading ? 'loading…'
                                 : `${state.accounts.filter((a) => a.available && !a.error).length} live` })])),
      spendLine,
      h('div', { key: 'grid', className: 'flex flex-col gap-2' }, assetRows)
    ]),
    section('Pending approvals', null, [
      wallet.human_gate && !locked && pending.length
        ? h('div', { key: 'hint', className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
                     children: 'Accept asks for your passphrase on the payment itself; Decline needs nothing.' })
        : null,
      h('div', { key: 'cards', className: 'flex flex-col gap-3' }, approvalCards)
    ]),
    // nothing to show unless /incoming exists and returned at least one address
    orders.length
      ? section(`Orders · receiving (${orders.filter((o) => String(o.status || 'waiting') !== 'paid').length} open)`, null,
          h('div', { key: 'orders', className: 'flex flex-col gap-2' }, orderCards))
      : null,
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
                    'needed to widen who may spend a sandbox; narrowing is free'),
        h('div', { key: 'grid', className: 'grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3' }, sandboxCards),
        sandboxForm
      ])),
    section(`Contacts (${state.contacts.length})`, null, [
      sectionPass('contact-pass', contactPass, setContactPass,
                  'only needed to change an address or remove a contact'),
      h('div', { key: 'body', className: 'flex flex-col gap-2' }, [
        h('div', { key: 'grid', className: 'flex flex-col gap-3' }, contactCards),
        contactForm
      ])
    ]),
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
          : btn('Clear history', () => setConfirmClear(true))
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
  ['sim-mainnet', 'Simulator (no funds)', 'XMR']
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
        style: fieldProps(),
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
      h('select', { style: fieldProps({ paddingRight: '1.6rem' }),
        key: 'pref', value: s.prefer_asset || 'XMR',
        style: fieldProps(),
        onChange: (e) => save({ prefer_asset: e.target.value }, 'preferred coin saved')
      }, (s.assets_enabled || ['XMR']).map((a) => opt(a, `default ${a}`))),
      h('label', { key: 'mono', className: 'flex items-center gap-2 text-xs' }, [
        h('input', {
          key: 'c', type: 'checkbox', checked: Boolean(s.monero_only),
          onChange: (e) => save({ monero_only: e.target.checked }, e.target.checked ? 'Monero only: on' : 'Monero only: off')
        }),
        h('span', { key: 'l', children: 'Monero only' })
      ])
    ]),
    h('div', { key: 'mono-warn', className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
               children: 'Heads up: Monero is great for privacy but many merchants do not accept XMR. With this on, any payment in another coin is refused — shops that only take BTC/USDC will not work until you switch it off.' }),

    h('div', { key: 'rpc', className: 'flex flex-wrap items-center gap-2 pt-1' }, [
      h('input', {
        key: 'i', value: rpc, placeholder: 'http://127.0.0.1:38084/json_rpc',
        style: fieldProps({ flex: '1 1 auto', minWidth: '18rem', fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace' }),
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
  version: '0.3.0',
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
export { ProfilePicker, qrEncode }
