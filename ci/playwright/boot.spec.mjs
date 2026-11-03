// CI stage 3: browser check against a freshly booted Raspberry Pi OS image.
// Plain Node script (no test runner): node boot.spec.mjs [BASE_URL]
//
// 1. the page loads and shows the status view
// 2. switching the language to German in Settings shows German text
// 3. login is switched on through the API (PUT /api/v1/settings)
// 4. log out, then log in again through the login form
import { chromium } from 'playwright';

const BASE = (process.argv[2] || process.env.BASE_URL || 'http://127.0.0.1:8080').replace(/\/$/, '');
const PASSWORD = 'ci-' + Math.random().toString(36).slice(2) + '-Pw1';
const TIMEOUT = 60_000; // the emulated Pi is slow

function check(cond, message) {
  if (!cond) throw new Error(message);
  console.log('ok -', message);
}

async function api(request, method, path, body) {
  const session = await (await request.get(BASE + '/api/v1/session')).json();
  const res = await request.fetch(BASE + path, {
    method,
    headers: { 'X-CSRF-Token': session.csrf_token || '', 'Content-Type': 'application/json' },
    data: body === undefined ? undefined : JSON.stringify(body),
  });
  return res;
}

const browser = await chromium.launch();
let failed = false;
const context = await browser.newContext({ locale: 'en-GB' });
const page = await context.newPage();
page.setDefaultTimeout(TIMEOUT);
page.on('pageerror', (err) => console.log('page error:', err.message));
try {
  const de = await (await context.request.get(BASE + '/api/v1/i18n/de')).json();
  const en = await (await context.request.get(BASE + '/api/v1/i18n/en')).json();
  check(de['nav.settings'] && de['nav.settings'] !== en['nav.settings'], 'German catalogue is served');

  // 1. page loads
  await page.goto(BASE + '/', { waitUntil: 'domcontentloaded' });
  await page.locator('#nav').waitFor({ state: 'visible' });
  check((await page.title()).includes('Footage Offload Station'), 'page loads with its title');

  // 2. German via Settings
  await page.goto(BASE + '/#settings');
  const select = page.locator('#f-language');
  await select.waitFor({ state: 'visible' });
  await select.selectOption('de');
  await page.locator('#view form button[type=submit]').last().click();
  await page.locator('#nav a[data-route=settings]', { hasText: de['nav.settings'] })
    .waitFor({ state: 'visible' });
  check(await page.evaluate(() => document.documentElement.lang) === 'de', 'UI switched to German');
  check((await page.locator('#nav').innerText()).includes(de['nav.settings']),
    `German navigation text "${de['nav.settings']}" is shown`);

  // 3. switch login on through the API (same browser cookies)
  let res = await api(context.request, 'PUT', '/api/v1/settings',
    { password: PASSWORD, login_enabled: true });
  check(res.ok(), `PUT /api/v1/settings enables login (HTTP ${res.status()})`);
  let session = await (await context.request.get(BASE + '/api/v1/session')).json();
  check(session.login_enabled === true, 'session reports login_enabled');

  // 4. log out, then log in through the form
  res = await api(context.request, 'POST', '/api/v1/logout');
  check(res.ok(), `POST /api/v1/logout (HTTP ${res.status()})`);
  session = await (await context.request.get(BASE + '/api/v1/session')).json();
  check(session.logged_in === false, 'logged out');
  res = await context.request.get(BASE + '/api/v1/status');
  check(res.status() === 401, `status needs a login now (HTTP ${res.status()})`);

  await page.goto(BASE + '/');
  await page.reload();
  const pw = page.locator('#lp');
  await pw.waitFor({ state: 'visible' });
  check(true, 'login form is shown');
  await pw.fill('wrong-password-123');
  await page.locator('form.login button[type=submit]').click();
  await page.locator('form.login [role=alert]').filter({ hasText: /\S/ }).first()
    .waitFor({ state: 'visible' }).catch(() => {});
  session = await (await context.request.get(BASE + '/api/v1/session')).json();
  check(session.logged_in === false, 'a wrong password is rejected');
  await pw.fill(PASSWORD);
  await page.locator('form.login button[type=submit]').click();
  await page.locator('#nav').waitFor({ state: 'visible' });
  session = await (await context.request.get(BASE + '/api/v1/session')).json();
  check(session.logged_in === true, 'logged in through the form');
  check(!(await page.locator('#logout').isHidden()), 'logout button is shown');
} catch (err) {
  failed = true;
  console.error('FAILED:', err.message);
  await page.screenshot({ path: 'boot-failure.png', fullPage: true }).catch(() => {});
} finally {
  await browser.close();
}
process.exit(failed ? 1 : 0);
