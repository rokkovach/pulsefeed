/* Capture existing local pages only. This script never submits a feed or changes policy.
 * Session exports belong in an ignored private directory, never beside these sources.
 */
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

async function main() {
  const [sessionFile, executablePath] = process.argv.slice(2);
  if (!sessionFile || !executablePath) throw new Error('Usage: capture_report.cjs PRIVATE_SESSION_JSON CHROMIUM_PATH');
  const session = JSON.parse(fs.readFileSync(sessionFile, 'utf8'));
  if (session.base !== 'https://localhost:8020') throw new Error('Capture is restricted to the local review server');
  const output = path.resolve(__dirname, 'assets');
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ headless: true, executablePath });
  try {
    for (const role of ['reader', 'admin']) {
      const context = await browser.newContext({ viewport: { width: 1280, height: 1040 },
        deviceScaleFactor: 1.5, colorScheme: 'light', reducedMotion: 'reduce', ignoreHTTPSErrors: true });
      await context.addCookies(session[role]);
      const page = await context.newPage();
      const url = role === 'reader' ? `${session.base}/feeds/${session.view}` : `${session.base}/admin`;
      await page.goto(url, { waitUntil: 'networkidle' });
      await page.evaluate(() => document.fonts.ready);
      if (role === 'reader') {
        await page.waitForFunction(() => document.querySelectorAll('.reader-card img').length >= 3
          && [...document.querySelectorAll('.reader-card img')].slice(0, 3).every(image => image.complete && image.naturalWidth > 0), null, { timeout: 60000 });
        await page.screenshot({ path: path.join(output, 'reader.png'), clip: { x: 0, y: 0, width: 1280, height: 940 } });
      } else {
        await page.locator('#admin-dashboard').waitFor();
        await page.locator('.usage-overview').screenshot({ path: path.join(output, 'admin-overview.png') });
        const usage = page.locator('#model-usage-section');
        await usage.locator('summary').first().click();
        await page.waitForFunction(() => document.querySelector('#qwen-usage-chart svg'));
        await usage.scrollIntoViewIfNeeded();
        await page.locator('.usage-model-report').first().screenshot({ path: path.join(output, 'admin-models.png') });
      }
      await context.close();
    }
  } finally { await browser.close(); }
  console.log('Captured reader, admin overview and aggregate model usage');
}
main().catch(error => { console.error(error.message); process.exitCode = 1; });
