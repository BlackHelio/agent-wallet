/**
 * The unlock bar, on a wallet that has a PIN: it must ask for the six digits and post `{pin}` — not
 * quietly fall back to the passphrase field, which is what it did while `pin_status()` answered `null`
 * for a locked wallet.
 */
import { chromium } from 'playwright'

const browser = await chromium.launch({ executablePath: process.env.CHROME })
const page = await browser.newPage({ viewport: { width: 1280, height: 900 } })
const errors = []
page.on('pageerror', (e) => errors.push(String(e)))

const results = []
const check = (name, ok, detail = '') => {
  results.push({ name, ok })
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '  — ' + detail : ''}`)
}

await page.goto('http://127.0.0.1:8899/index.html?state=locked', { waitUntil: 'load' })
await page.waitForFunction(() => window.__ready === true, { timeout: 20000 })

const field = page.getByPlaceholder('6-digit PIN')
check('the unlock bar asks for the PIN', await field.isVisible().catch(() => false))
check('it does not ask for the passphrase instead',
  !(await page.getByPlaceholder('keystore passphrase').isVisible().catch(() => false)))

await field.fill('123456')
await page.getByRole('button', { name: /^Unlock$/ }).first().click()
await page.waitForTimeout(300)
const calls = await page.evaluate(() => (window.__calls || []).filter((c) => c.path === '/unlock'))
check('the pane posts the PIN, not a passphrase',
  calls.length === 1 && calls[0].body.pin === '123456' && !calls[0].body.passphrase,
  JSON.stringify(calls.map((c) => c.body)))

// a passphrase still has to work: same field, anything that is not six digits
await page.evaluate(() => { window.__calls = [] })
await page.getByPlaceholder('6-digit PIN').fill('a-long-passphrase')
await page.getByRole('button', { name: /^Unlock$/ }).first().click()
await page.waitForTimeout(300)
const second = await page.evaluate(() => (window.__calls || []).filter((c) => c.path === '/unlock'))
check('a longer secret is sent as the passphrase',
  second.length === 1 && second[0].body.passphrase === 'a-long-passphrase' && !second[0].body.pin,
  JSON.stringify(second.map((c) => c.body)))

console.log('page errors:', errors.length ? errors : 'none')
await page.screenshot({ path: 'shots/unlock-pin.png' })
await browser.close()
const failed = results.filter((r) => !r.ok)
console.log(`\n${results.length - failed.length}/${results.length} checks passed`)
process.exit(failed.length ? 1 : 0)
