/**
 * Click-through for the two credential controls: change PIN, change passphrase.
 *
 * Rendered in Chromium against the app's compiled CSS with a stubbed backend that records every call,
 * so this asserts the wire payload a person's click actually produces — not that a button exists.
 */
import { chromium } from 'playwright'

const browser = await chromium.launch({ executablePath: process.env.CHROME })
const page = await browser.newPage({ viewport: { width: 1280, height: 900 } })
const errors = []
page.on('pageerror', (e) => errors.push(String(e)))

const posts = (path) => page.evaluate((p) => (window.__calls || []).filter((c) => c.path === p), path)
const card = (title) => page.getByText(title, { exact: true })

await page.goto('http://127.0.0.1:8899/index.html?state=loaded', { waitUntil: 'load' })
await page.waitForFunction(() => window.__ready === true, { timeout: 20000 })

const results = []
const check = (name, ok, detail = '') => {
  results.push({ name, ok })
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  — ' + detail : ''}`)
}

// ── change PIN ───────────────────────────────────────────────────────────────
const pinChip = page.getByRole('button', { name: /PIN · change/i })
await pinChip.waitFor({ timeout: 5000 })
const box = await pinChip.boundingBox()
check('PIN chip is on the page', true, `${Math.round(box.width)}x${Math.round(box.height)}`)

await pinChip.click()
await card('Change the payment PIN').waitFor({ timeout: 5000 })
check('PIN card opens', true)

await page.getByPlaceholder('PIN · 6 digits').first().fill('654321')
await page.getByPlaceholder('again').first().fill('654321')
await page.getByPlaceholder('wallet passphrase').first().fill('demo-stage-passphrase')
await page.getByRole('button', { name: /^Change PIN$/ }).first().click()
await page.waitForTimeout(300)
let sent = await posts('/pin/set')
check('POST /pin/set carries pin + confirm + passphrase',
  sent.length === 1 && sent[0].body.pin === '654321' && sent[0].body.confirm === '654321'
  && sent[0].body.passphrase === 'demo-stage-passphrase', JSON.stringify(sent.map((c) => c.body)))
check('PIN card closes on success',
  !(await card('Change the payment PIN').isVisible().catch(() => false)))

// two different PINs never reach the daemon
await pinChip.click()
await card('Change the payment PIN').waitFor({ timeout: 5000 })
await page.getByPlaceholder('PIN · 6 digits').first().fill('111111')
await page.getByPlaceholder('again').first().fill('222222')
await page.getByPlaceholder('wallet passphrase').first().fill('x')
await page.getByRole('button', { name: /^Change PIN$/ }).first().click()
await page.waitForTimeout(250)
check('mismatched PINs are refused in the pane',
  (await posts('/pin/set')).length === 1
  && (await page.getByText(/the two PINs are not the same/i).isVisible().catch(() => false)))

await page.getByRole('button', { name: /^Cancel$/ }).first().click()

// ── change passphrase ────────────────────────────────────────────────────────
const passChip = page.getByRole('button', { name: /passphrase · change/i })
await passChip.waitFor({ timeout: 5000 })
check('passphrase chip is on the page', true)

await passChip.click()
await card('Change the wallet passphrase').waitFor({ timeout: 5000 })
check('passphrase card opens', true)

await page.getByPlaceholder('current passphrase').fill('demo-stage-passphrase')
await page.getByPlaceholder('new passphrase (8+ chars)').fill('a-longer-demo-passphrase')
await page.getByPlaceholder('new again').fill('a-longer-demo-passphrase')
await page.getByRole('button', { name: /^Change passphrase$/ }).click()
await page.waitForTimeout(300)
sent = await posts('/passphrase/change')
check('POST /passphrase/change carries current + new + confirm',
  sent.length === 1 && sent[0].body.passphrase === 'demo-stage-passphrase'
  && sent[0].body.new_passphrase === 'a-longer-demo-passphrase'
  && sent[0].body.confirm === 'a-longer-demo-passphrase', JSON.stringify(sent.map((c) => c.body)))
check('passphrase card closes on success',
  !(await card('Change the wallet passphrase').isVisible().catch(() => false)))

// six characters is the PIN, not a passphrase — refused before any request
await passChip.click()
await card('Change the wallet passphrase').waitFor({ timeout: 5000 })
await page.getByPlaceholder('current passphrase').fill('demo-stage-passphrase')
await page.getByPlaceholder('new passphrase (8+ chars)').fill('123456')
await page.getByPlaceholder('new again').fill('123456')
await page.getByRole('button', { name: /^Change passphrase$/ }).click()
await page.waitForTimeout(250)
check('a 6-digit passphrase is refused in the pane',
  (await posts('/passphrase/change')).length === 1
  && (await page.getByText(/at least 8 characters/i).isVisible().catch(() => false)))

await page.screenshot({ path: 'shots/credentials.png', fullPage: false })
console.log('page errors:', errors.length ? errors : 'none')
await browser.close()

const failed = results.filter((r) => !r.ok)
console.log(`\n${results.length - failed.length}/${results.length} checks passed`)
process.exit(failed.length ? 1 : 0)
