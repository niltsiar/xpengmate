// The XPENG Data Act portal driven through Playwright. The login steps and selectors are the ones that worked
// against the live portal; the nextApi calls reuse the headers the page itself sends.
import { createWriteStream } from 'node:fs';
import { rename, rm } from 'node:fs/promises';
import { join } from 'node:path';
import { Readable } from 'node:stream';
import { pipeline } from 'node:stream/promises';
import { chromium } from 'playwright-core';
import { JobError } from './job.mjs';

const PORTAL = 'https://www.xpeng.com/data-act/my-data';
const API = '/nextApi/dataAct/myData/';
// Requests to any other host are aborted. A blocked host is logged once by name.
// The captcha vendor hosts stay open as in the login run that worked; the initNECaptcha stub still answers the captcha.
const ALLOWED_HOSTS = [
  'www.xpeng.com', 'login.xpeng.com',
  'a-cdn.xpeng.com', 's-cdn.xpeng.com', 'static.xpeng.com', 'f.xiaopeng.com',
  'xp-static-eur.oss-eu-central-1.aliyuncs.com',
  'c.dun.163.com', 'ir-sdk.dun.163.com', 'cstaticdun.126.net', 'necaptcha.nosdn.127.net',
];
const WEB_CAPTCHA_ID = 'ee799838945f4771aa5d3dc9bea105d2';
const API_HEADERS = ['language', 'languagecode', 'timezone', 'client-type', 'country'];
// Only the slider gate code is known from the portal; every other code is reported as is.
const LOGIN_CODES = new Map([[12040101, 'captcha refused']]);
const MAX_ZIP_BYTES = 200 * 1024 * 1024;
const ZIP_MAGIC = Buffer.from([0x50, 0x4b, 0x03, 0x04]);

async function saveZip(stream, dir, recordNo) {
  const part = join(dir, `${recordNo}.zip.part`);
  let size = 0;
  let head = Buffer.alloc(0);
  try {
    await pipeline(stream, async function* (source) {
      for await (const chunk of source) {
        size += chunk.length;
        if (size > MAX_ZIP_BYTES) throw new JobError({ detail: 'the export is larger than 200 MB' });
        if (head.length < 4) head = Buffer.concat([head, chunk]).subarray(0, 4);
        yield chunk;
      }
    }, createWriteStream(part, { mode: 0o600 }));
    if (!head.equals(ZIP_MAGIC)) throw new JobError({ detail: 'the download is not a zip' });
    const target = join(dir, `${recordNo}.zip`);
    await rename(part, target);
    return target;
  } catch (e) {
    await rm(part, { force: true });
    throw e;
  }
}

export function createPortal({ log, solverUrl, solverToken }) {
  const allowed = new Set(ALLOWED_HOSTS);
  const blocked = new Set();
  let browser;
  let page;
  let apiHeaders;
  let frontRedirect;
  let loginAnswer;
  let logoutUrl;

  async function api(method, path, body) {
    if (!apiHeaders) throw new JobError({ detail: 'the portal request headers were not seen' });
    const query = method === 'GET' && frontRedirect != null ? `?frontRedirect=${encodeURIComponent(frontRedirect)}` : '';
    const payload = body && (frontRedirect != null ? { ...body, frontRedirect } : body);
    const res = await page.evaluate(async ({ url, method, headers, payload }) => {
      const r = await fetch(url, {
        method,
        credentials: 'same-origin',
        headers: payload ? { ...headers, 'content-type': 'application/json' } : headers,
        body: payload ? JSON.stringify(payload) : undefined,
        signal: AbortSignal.timeout(30_000),
      });
      let json = null;
      try { json = await r.json(); } catch {}
      return { status: r.status, json };
    }, { url: API + path + query, method, headers: apiHeaders, payload });
    if (res.status !== 200 || res.json?.data == null) {
      throw new JobError({ detail: 'the portal gave an unexpected answer', status: res.status, code: res.json?.code });
    }
    return res.json.data;
  }

  async function clickDownload(record, dir) {
    if (typeof record.createTime !== 'string') throw new JobError({ detail: 'the record has no creation time' });
    await page.goto(PORTAL, { waitUntil: 'networkidle' });
    const row = page.locator('tr, [class*=row], [class*=item]').filter({ hasText: record.createTime }).last();
    const [download] = await Promise.all([
      page.waitForEvent('download', { timeout: 120_000 }),
      row.getByText(/download/i).first().click(),
    ]);
    return saveZip(await download.createReadStream(), dir, record.recordNo);
  }

  return {
    async solveCaptcha() {
      if (!solverUrl) throw new JobError({ detail: 'the solver is not configured' });
      const r = await fetch(`${solverUrl.replace(/\/$/, '')}/solve`, {
        method: 'POST',
        headers: { 'content-type': 'application/json', authorization: `Bearer ${solverToken}` },
        body: JSON.stringify({ captchaId: WEB_CAPTCHA_ID }),
        signal: AbortSignal.timeout(120_000),
      });
      const j = r.ok ? await r.json().catch(() => null) : null;
      if (!j?.ok || typeof j.validate !== 'string') throw new JobError({ detail: 'the solver gave no answer', status: r.status });
      return j.validate;
    },

    async login({ email, password, validate }) {
      browser = await chromium.launch();
      const context = await browser.newContext({
        locale: 'en-US', timezoneId: 'UTC', acceptDownloads: true, serviceWorkers: 'block',
        viewport: { width: 1280, height: 1000 },
      });
      context.setDefaultTimeout(60_000);
      await context.route('**/*', route => {
        const url = new URL(route.request().url());
        if (!/^(https?|wss?):$/.test(url.protocol) || allowed.has(url.host)) return route.continue();
        if (!blocked.has(url.host)) {
          blocked.add(url.host);
          log(`blocked host ${url.host}`);
        }
        return route.abort('blockedbyclient');
      });
      await context.addInitScript(v => {
        const stub = function (cfg, onload) {
          onload({ popUp() { setTimeout(() => cfg.onVerify(null, { validate: v }), 100); }, refresh() {}, close() {}, destroy() {}, verify() {} });
        };
        Object.defineProperty(window, 'initNECaptcha', { value: stub, writable: false, configurable: false });
      }, validate);
      page = await context.newPage();
      page.on('request', q => {
        const url = new URL(q.url());
        if (url.host !== 'www.xpeng.com' || !url.pathname.startsWith(API)) return;
        if (!apiHeaders) {
          const h = q.headers();
          apiHeaders = Object.fromEntries(API_HEADERS.filter(k => h[k] != null).map(k => [k, h[k]]));
        }
        frontRedirect ??= url.searchParams.get('frontRedirect');
      });
      page.on('response', async r => {
        const url = new URL(r.url());
        if (url.host === 'login.xpeng.com' && url.pathname === '/api/web/v2/login') {
          const json = await r.json().catch(() => null);
          loginAnswer = { status: r.status(), code: json?.code };
        } else if (url.host === 'www.xpeng.com' && url.pathname.startsWith('/webAuthSdk/') && !logoutUrl) {
          const json = await r.json().catch(() => null);
          const found = json?.logoutUrl ?? json?.data?.logoutUrl;
          if (typeof found === 'string' && URL.canParse(found) && allowed.has(new URL(found).host)) logoutUrl = found;
        }
      });

      await page.goto(PORTAL, { waitUntil: 'networkidle' });
      await page.getByPlaceholder('Email address').fill(email);
      await page.locator('text=I accept XPENG').locator('xpath=preceding-sibling::*[1]').click();
      await page.getByRole('button', { name: 'Next' }).click();
      const field = page.locator('input[type=password]').first();
      await field.waitFor({ timeout: 30_000 });
      await field.fill(password);
      await page.getByRole('button', { name: /log ?in/i }).first().click();
      try {
        await page.waitForURL(/www\.xpeng\.com\/data-act\/my-data/);
      } catch {
        if (!loginAnswer) throw new JobError({ detail: 'the login page did not finish' });
        const code = Number.isInteger(loginAnswer.code) ? loginAnswer.code : undefined;
        throw new JobError({ detail: LOGIN_CODES.get(code) ?? 'login refused', status: loginAnswer.status, code });
      }
      await page.waitForLoadState('networkidle').catch(() => {});
    },

    vehicles: async () => {
      const data = await api('GET', 'download/vehicles');
      if (!Array.isArray(data.vehicles)) throw new JobError({ detail: 'the portal gave no vehicle list' });
      return data.vehicles;
    },

    activities: async () => {
      const data = await api('GET', 'download/recentActivities');
      if (!Array.isArray(data)) throw new JobError({ detail: 'the portal gave no record list' });
      return data;
    },

    submit: async ({ vinToken, dataClusters, timeRange }) => {
      const data = await api('POST', 'download/submit', { vin: vinToken, dataClusters, timeRange });
      return data.recordNo;
    },

    async downloadZip(record, dir) {
      try {
        const data = await api('GET', `download/url/${record.recordNo}`);
        const url = new URL(data.downloadUrl);
        if (url.protocol !== 'https:') throw new JobError({ detail: 'the download link is not https' });
        allowed.add(url.host);
        const r = await fetch(url, { signal: AbortSignal.timeout(5 * 60_000) });
        if (!r.ok || !r.body) throw new JobError({ detail: 'the download was refused', status: r.status });
        return await saveZip(Readable.fromWeb(r.body), dir, record.recordNo);
      } catch (e) {
        log(`direct download failed (${e?.constructor?.name}); trying the download button`);
        return clickDownload(record, dir);
      }
    },

    async logout() {
      if (!logoutUrl) {
        log('logout skipped: no logout link seen');
        return;
      }
      await page.goto(logoutUrl, { timeout: 15_000 });
      log('logged out');
    },

    async close() {
      await browser?.close();
    },
  };
}
