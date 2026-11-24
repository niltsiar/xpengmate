// The XPENG Data Act portal driven through Playwright. The nextApi calls reuse the headers the page itself sends.
import { Readable } from 'node:stream';
import { chromium } from 'playwright-core';
import { HOST_REFUSED, JobError, errorClass } from './job.mjs';

const PORTAL = 'https://www.xpeng.com/data-act/my-data';
const API = '/nextApi/dataAct/myData/';
// Requests to any other host are aborted. A blocked host is logged once by name.
const ALLOWED_SUFFIXES = ['.xpeng.com', '.xiaopeng.com', '.oss-eu-central-1.aliyuncs.com'];
// The captcha vendor hosts stay open as in the login run that worked; the initNECaptcha stub still answers the captcha.
const ALLOWED_HOSTS = ['c.dun.163.com', 'ir-sdk.dun.163.com', 'cstaticdun.126.net', 'necaptcha.nosdn.127.net'];
const allowedHost = host => ALLOWED_HOSTS.includes(host) || ALLOWED_SUFFIXES.some(s => host.endsWith(s));
const WEB_CAPTCHA_ID = 'ee799838945f4771aa5d3dc9bea105d2';
const API_HEADERS = ['language', 'languagecode', 'timezone', 'client-type', 'country'];
// Only the slider gate code is known from the portal; every other code is reported as is.
// It arrives on the email check or the slider check, before the v2/login answer exists.
const LOGIN_CODES = new Map([[12040101, 'captcha refused']]);
const LOGIN_OK = 200;
const API_OK = 2000;

const typeName = v => (v === null ? 'null' : Array.isArray(v) ? 'array' : typeof v);
// Key names and value types only: the values hold account data.
const shape = v => (typeName(v) === 'object'
  ? `{${Object.entries(v).map(([k, x]) => `${k}: ${typeName(x)}`).join(', ')}}`
  : typeName(v));
const codeText = code => (Number.isInteger(code) ? code : typeName(code));

export function createPortal({ log, solverUrl, solverToken }) {
  const blocked = new Set();
  let browser;
  let page;
  let apiHeaders;
  let frontRedirect;
  let loginAnswer;
  let gateAnswer;
  let logoutUrl;

  function unexpected(what, data) {
    log(`unexpected ${what}: data ${shape(data)}`);
    return new JobError({ detail: `the portal gave no ${what}` });
  }

  async function api(signal, method, path, body) {
    signal.throwIfAborted();
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
    const code = res.json?.code;
    if (res.status !== 200 || code !== API_OK || res.json?.data == null) {
      log(`unexpected answer to ${path}: HTTP ${res.status}, code ${codeText(code)}, data ${shape(res.json?.data)}`);
      throw new JobError({ detail: 'the portal gave an unexpected answer', status: res.status, code });
    }
    return res.json.data;
  }

  async function activities(signal) {
    const data = await api(signal, 'GET', 'download/recentActivities');
    if (!Array.isArray(data)) throw unexpected('record list', data);
    return data;
  }

  async function clickDownload(recordNo, signal) {
    const record = (await activities(signal)).find(a => a.recordNo === recordNo);
    if (typeof record?.createTime !== 'string') throw new JobError({ detail: 'the record has no creation time' });
    await page.goto(PORTAL, { waitUntil: 'networkidle' });
    const row = page.locator('tr, [class*=row], [class*=item]').filter({ hasText: record.createTime }).last();
    const [download] = await Promise.all([
      page.waitForEvent('download', { timeout: 120_000 }),
      row.getByText(/download/i).first().click(),
    ]);
    return download.createReadStream();
  }

  async function directDownload(recordNo, signal) {
    let data;
    try {
      data = await api(signal, 'GET', `download/url/${recordNo}`);
    } catch (e) {
      if (signal.aborted) throw e;
      log(`download link failed (${errorClass(e)})`);
      return null;
    }
    if (typeof data.downloadUrl !== 'string' || !URL.canParse(data.downloadUrl)) {
      log(`no download link: data ${shape(data)}`);
      return null;
    }
    const url = new URL(data.downloadUrl);
    if (url.protocol !== 'https:' || !allowedHost(url.hostname)) {
      log(`download link refused: ${url.protocol}//${url.hostname}`);
      throw new JobError({ detail: HOST_REFUSED });
    }
    try {
      const r = await fetch(url, { redirect: 'error', signal: AbortSignal.any([signal, AbortSignal.timeout(5 * 60_000)]) });
      if (r.ok && r.body) return Readable.fromWeb(r.body);
      log(`direct download refused (HTTP ${r.status})`);
      await r.body?.cancel();
    } catch (e) {
      if (signal.aborted) throw e;
      log(`direct download failed (${errorClass(e)})`);
    }
    return null;
  }

  return {
    async solveCaptcha(signal) {
      if (!solverUrl) throw new JobError({ detail: 'the solver is not configured' });
      const r = await fetch(`${solverUrl.replace(/\/$/, '')}/solve`, {
        method: 'POST',
        headers: { 'content-type': 'application/json', authorization: `Bearer ${solverToken}` },
        body: JSON.stringify({ captchaId: WEB_CAPTCHA_ID }),
        signal: AbortSignal.any([signal, AbortSignal.timeout(120_000)]),
      });
      const j = r.ok ? await r.json().catch(() => null) : null;
      if (!j?.ok || typeof j.validate !== 'string') throw new JobError({ detail: 'the solver gave no answer', status: r.status });
      return j.validate;
    },

    async login({ email, password, validate }, signal) {
      signal.addEventListener('abort', () => browser?.close().catch(() => {}), { once: true });
      // The browser gets no service token from the environment.
      browser = await chromium.launch({ env: { HOME: process.env.HOME, PATH: process.env.PATH } });
      signal.throwIfAborted();
      const context = await browser.newContext({
        locale: 'en-US', timezoneId: 'UTC', acceptDownloads: true, serviceWorkers: 'block',
        viewport: { width: 1280, height: 1000 },
      });
      context.setDefaultTimeout(60_000);
      await context.route('**/*', route => {
        const url = new URL(route.request().url());
        if (!/^(https?|wss?):$/.test(url.protocol) || allowedHost(url.hostname)) return route.continue();
        if (!blocked.has(url.hostname)) {
          blocked.add(url.hostname);
          log(`blocked host ${url.hostname}`);
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
          log(`login answer: HTTP ${loginAnswer.status}, code ${codeText(json?.code)}`);
        } else if (url.host.endsWith('.xpeng.com') && (url.pathname === '/api/web/v3/emailLogin/account' || url.pathname.includes('/drag/validate'))) {
          const json = await r.json().catch(() => null);
          const what = url.pathname.includes('/drag/validate') ? 'slider check' : 'email check';
          log(`${what} answer: HTTP ${r.status()}, code ${codeText(json?.code)}`);
          if (LOGIN_CODES.has(json?.code)) gateAnswer = { status: r.status(), code: json.code };
        } else if (url.host === 'www.xpeng.com' && url.pathname.startsWith('/webAuthSdk/') && !logoutUrl) {
          const json = await r.json().catch(() => null);
          const found = json?.logoutUrl ?? json?.data?.logoutUrl;
          if (typeof found === 'string' && URL.canParse(found) && allowedHost(new URL(found).hostname)) logoutUrl = found;
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
        await page.waitForURL(u => u.hostname === 'www.xpeng.com' && u.pathname.startsWith('/data-act/my-data'));
      } catch {
        signal.throwIfAborted();
        const answer = loginAnswer ?? gateAnswer;
        if (!answer) throw new JobError({ detail: 'the login page did not finish' });
        const { code, status } = answer;
        throw new JobError({ detail: LOGIN_CODES.get(code) ?? 'login refused', status, code });
      }
      await page.waitForLoadState('networkidle').catch(() => {});
    },

    loginSucceeded: () => loginAnswer?.status === 200 && loginAnswer.code === LOGIN_OK,

    vehicles: async signal => {
      const data = await api(signal, 'GET', 'download/vehicles');
      if (!Array.isArray(data.vehicles)) throw unexpected('vehicle list', data);
      return data.vehicles;
    },

    activities,

    submit: async ({ vinToken, dataClusters, timeRange }, signal) => {
      const data = await api(signal, 'POST', 'download/submit', { vin: vinToken, dataClusters, timeRange });
      if (typeof data.recordNo !== 'string') throw unexpected('record number', data);
      return data.recordNo;
    },

    // A readable stream of the zip. The button is the fallback only when the link or its fetch is unavailable.
    async download(recordNo, signal) {
      const direct = await directDownload(recordNo, signal);
      if (direct) return direct;
      log('trying the download button');
      return clickDownload(recordNo, signal);
    },

    async logout() {
      if (!logoutUrl) {
        log('logout skipped: no logout link seen');
        return;
      }
      await page.goto(logoutUrl, { timeout: 10_000 });
      log('logged out');
    },

    async close() {
      await browser?.close();
    },
  };
}
