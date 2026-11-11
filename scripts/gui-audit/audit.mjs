/**
 * Measure the agent-wallet pane in a real Chromium, at real sizes, in the app's real themes.
 *
 * What it checks, per (state x viewport x theme):
 *   overflow  — the page scrolls sideways (the classic "works on my 1440p" bug)
 *   clipped   — an element's box extends past the viewport
 *   squashed  — an element's content is wider than its box and nothing scrolls or ellipsizes
 *   targets   — a control smaller than a finger
 *   contrast  — text under 4.5:1 against its effective background
 */
import { mkdirSync, writeFileSync } from 'node:fs'
import { chromium } from 'playwright'

const BASE = process.env.BASE || 'http://127.0.0.1:8899/index.html'
const OUT = 'shots'
mkdirSync(OUT, { recursive: true })

const VIEWPORTS = [
  [320, 640, 'phone-narrow'], [375, 667, 'phone'], [414, 896, 'phone-large'],
  [600, 800, 'tablet-narrow'], [768, 1024, 'tablet'], [1024, 768, 'laptop-small'],
  [1280, 800, 'laptop'], [1440, 900, 'desktop'], [1920, 1080, 'desktop-large']
]
const THEMES = ['light', 'dark']
const STATES = ['locked', 'loaded']

const AUDIT = () => {
  const out = { blank: false, overflow: 0, clipped: [], squashed: [], targets: [], contrast: [] }
  // A pane that failed to render has no overflow, no contrast failure and no small targets — it
  // scored a perfect 0 findings while the locked state was throwing in the temporal dead zone.
  // Measure the render itself, or the audit is scoring an empty page.
  const root = document.getElementById('root')
  out.blank = !root || root.children.length === 0 || (root.innerText || '').trim().length === 0
  const de = document.scrollingElement || document.documentElement
  out.overflow = Math.max(0, de.scrollWidth - window.innerWidth)
  const vw = window.innerWidth
  const label = (el) => {
    const t = (el.textContent || '').trim().slice(0, 28).replace(/\s+/g, ' ')
    return `${el.tagName.toLowerCase()}${el.className ? '.' + String(el.className).split(/\s+/).slice(0, 2).join('.') : ''}${t ? ` "${t}"` : ''}`
  }
  const visible = (el) => {
    const r = el.getBoundingClientRect()
    const cs = getComputedStyle(el)
    return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none' && cs.opacity !== '0'
  }
  const parse = (c) => {
    const m = String(c).match(/rgba?\(([^)]+)\)/)
    if (!m) return null
    const p = m[1].split(',').map((x) => parseFloat(x))
    return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 }
  }
  const lum = (c) => {
    const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4) }
    return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b)
  }
  const ratio = (a, b) => {
    const l1 = lum(a), l2 = lum(b)
    return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05)
  }
  const bgOf = (el) => {
    let node = el
    while (node && node !== document.documentElement) {
      const c = parse(getComputedStyle(node).backgroundColor)
      if (c && c.a > 0.9) return c
      node = node.parentElement
    }
    const body = parse(getComputedStyle(document.body).backgroundColor)
    return body && body.a > 0.9 ? body : { r: 20, g: 22, b: 26, a: 1 }
  }
  for (const el of document.querySelectorAll('#root *')) {
    if (!visible(el)) continue
    const r = el.getBoundingClientRect()
    if (r.right > vw + 1 || r.left < -1) out.clipped.push({ el: label(el), left: Math.round(r.left), right: Math.round(r.right) })
    const cs = getComputedStyle(el)
    const scrolls = /auto|scroll/.test(cs.overflowX)
    const ellipsis = cs.textOverflow === 'ellipsis' || cs.textOverflow === 'clip'
    if (!scrolls && !ellipsis && el.scrollWidth > el.clientWidth + 2 && el.clientWidth > 0) {
      out.squashed.push({ el: label(el), scroll: el.scrollWidth, client: el.clientWidth })
    }
    if (['button', 'select', 'input', 'a'].includes(el.tagName.toLowerCase())) {
      // a checkbox inside a <label> is clicked through the label: the label is the target
      const box = el.closest('label')
      const rr = box && el.type === 'checkbox' ? box.getBoundingClientRect() : r
      if (rr.width < 24 || rr.height < 24) out.targets.push({ el: label(el), w: Math.round(rr.width), h: Math.round(rr.height) })
    }
    // fields carry their value, not a text node: check the pair the browser actually paints
    if (['input', 'textarea', 'select'].includes(el.tagName.toLowerCase())) {
      const fg = parse(cs.color), bg = parse(cs.backgroundColor)
      if (fg && bg && bg.a > 0.9) {
        const cr = ratio(fg, bg)
        if (cr < 4.5) out.contrast.push({ el: 'FIELD ' + label(el), ratio: Math.round(cr * 100) / 100,
                                         need: 4.5, fg: cs.color, size: Math.round(parseFloat(cs.fontSize)) })
      }
    }
    // contrast only for elements that paint their own text
    const own = [...el.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim().length > 1)
    if (own) {
      const fg = parse(cs.color)
      if (fg) {
        const size = parseFloat(cs.fontSize)
        const bold = parseInt(cs.fontWeight, 10) >= 600
        const large = size >= 24 || (bold && size >= 18.66)
        const need = large ? 3 : 4.5
        const cr = ratio(fg, bgOf(el))
        if (cr < need) out.contrast.push({ el: label(el), ratio: Math.round(cr * 100) / 100, need,
                                           fg: cs.color, size: Math.round(size) })
      }
    }
  }
  // dedupe: one row per distinct problem shape keeps the report readable
  const uniq = (rows, key) => {
    const seen = new Set()
    return rows.filter((r) => { const k = key(r); if (seen.has(k)) return false; seen.add(k); return true })
  }
  out.clipped = uniq(out.clipped, (r) => r.el).slice(0, 6)
  out.squashed = uniq(out.squashed, (r) => r.el).slice(0, 6)
  out.targets = uniq(out.targets, (r) => r.el).slice(0, 6)
  out.contrast = uniq(out.contrast, (r) => r.el + r.ratio).slice(0, 8)
  return out
}

// CHROME points at any Chromium/Electron binary when Playwright's own download is unavailable.
const browser = await chromium.launch({ executablePath: process.env.CHROME || undefined })
const report = []
for (const state of STATES) {
  for (const theme of THEMES) {
    for (const [w, h, name] of VIEWPORTS) {
      const ctx = await browser.newContext({ viewport: { width: w, height: h }, deviceScaleFactor: 1 })
      const page = await ctx.newPage()
      await page.goto(`${BASE}?state=${state}`, { waitUntil: 'networkidle' })
      if (theme === 'dark') {
        // The app sets its ink tokens from JS when a theme is chosen, so a class alone cannot
        // reproduce one. This is a plausible dark palette, applied the same way the app does it.
        await page.evaluate(() => {
          const r = document.documentElement
          r.classList.add('dark')
          r.style.setProperty('--ui-base', '#e9e9ec')
          r.style.setProperty('--foreground', 'color-mix(in srgb, #e9e9ec 94%, transparent)')
          r.style.setProperty('--muted-foreground', 'color-mix(in srgb, #e9e9ec 62%, transparent)')
          r.style.setProperty('--ui-text-primary', '#e9e9ec')
          r.style.setProperty('--ui-text-secondary', 'rgba(233,233,236,0.78)')
          r.style.setProperty('--ui-text-tertiary', 'rgba(233,233,236,0.55)')
          r.style.setProperty('--ui-stroke-secondary', 'rgba(255,255,255,0.20)')
          r.style.setProperty('--ui-bg-primary', '#141416')
          document.body.style.background = '#141416'
        })
      }
      await page.waitForFunction(() => window.__ready === true, { timeout: 15000 }).catch(() => {})
      await page.waitForTimeout(150)
      const row = await page.evaluate(AUDIT)
      report.push({ state, theme, size: `${w}x${h}`, name, ...row })
      if ([375, 768, 1440].includes(w)) {
        await page.screenshot({ path: `${OUT}/${state}-${theme}-${w}.png`, fullPage: true })
      }
      await ctx.close()
    }
  }
}
await browser.close()

const bad = report.filter((r) => r.blank || r.overflow || r.clipped.length || r.squashed.length || r.targets.length || r.contrast.length)
writeFileSync('audit.json', JSON.stringify(report, null, 2))
console.log(`${report.length} renders, ${bad.length} with at least one finding\n`)
for (const r of report) {
  const flags = []
  if (r.blank) flags.push('blank render')
  if (r.overflow) flags.push(`overflow +${r.overflow}px`)
  if (r.clipped.length) flags.push(`clipped ${r.clipped.length}`)
  if (r.squashed.length) flags.push(`squashed ${r.squashed.length}`)
  if (r.targets.length) flags.push(`small-targets ${r.targets.length}`)
  if (r.contrast.length) flags.push(`contrast ${r.contrast.length}`)
  if (flags.length) console.log(`${r.state.padEnd(6)} ${r.theme.padEnd(5)} ${r.size.padEnd(10)} ${flags.join(', ')}`)
}
console.log('\n--- distinct problems ---')
const seen = new Set()
for (const r of bad) {
  for (const [kind, rows] of [['clipped', r.clipped], ['squashed', r.squashed], ['small', r.targets], ['contrast', r.contrast]]) {
    for (const row of rows) {
      const key = kind + row.el + (row.ratio || '')
      if (seen.has(key)) continue
      seen.add(key)
      const extra = row.ratio ? `${row.ratio}:1 (need ${row.need}) ${row.fg} @${row.size}px` : (row.right ? `right=${row.right}` : `${row.w}x${row.h}`)
      console.log(`${kind.padEnd(9)} ${r.state}/${r.theme}/${r.size}  ${row.el}  ${extra}`)
    }
  }
}
