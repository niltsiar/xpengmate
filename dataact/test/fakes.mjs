import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { Readable } from 'node:stream';
import { CLUSTERS, LIMITS, createRunner } from '../job.mjs';

export const EMAIL = 'owner@example.test';
export const PASSWORD = 'pa"ss\\w/rd&1';
const T0 = Date.parse('2099-01-01T00:00:00Z');
export const ZIP_BYTES = Buffer.from('PK\x03\x04fake export');

export const record = (recordNo, status, extra = {}) => ({
  recordNo, status, timeRange: 30, dataClusters: [...CLUSTERS],
  createTime: '2098-12-31 23:00:00', expireTime: '2099-01-07 23:00:00', ...extra,
});

function fakeClock() {
  const clock = {
    t: T0,
    now: () => clock.t,
    sleep: async (ms, signal) => {
      signal.throwIfAborted();
      clock.t += ms;
      await new Promise(r => setImmediate(r));
    },
  };
  return clock;
}

// `lists` are the answers of successive activities() calls; the last one repeats, an Error entry is thrown.
// `solveCaptcha(attempt, signal)` may throw to fail that attempt.
// `download` returns the zip stream for a record number; the default streams ZIP_BYTES.
export function fakePortal({
  vehicles = [{ vinToken: 'TESTVINTOKEN', userType: 1 }], lists = [[]], login, loginSucceeded = true,
  download = () => Readable.from([ZIP_BYTES]), solveCaptcha,
} = {}) {
  const calls = { captcha: 0, login: 0, submit: [], download: [], logout: 0, close: 0 };
  let n = 0;
  return {
    calls,
    solveCaptcha: async signal => { calls.captcha++; if (solveCaptcha) await solveCaptcha(calls.captcha, signal); return 'validate-token'; },
    login: async (args, signal) => { calls.login++; if (login) await login(args, signal); },
    loginSucceeded: () => loginSucceeded,
    vehicles: async () => vehicles,
    activities: async () => {
      const list = lists[Math.min(n++, lists.length - 1)];
      if (list instanceof Error) throw list;
      return list;
    },
    submit: async args => { calls.submit.push(args); return 'DA20990101000000BBBB'; },
    download: async recordNo => { calls.download.push(recordNo); return download(recordNo); },
    logout: async () => { calls.logout++; },
    close: async () => { calls.close++; },
  };
}

// `busy` are the answers of successive exports() calls; the last one repeats.
export function fakeHistory({ imports = [], busy = [false], vinSet = true, error, uploadStatus = 200, upload } = {}) {
  const calls = { exports: 0, uploads: [] };
  return {
    calls,
    exports: async () => {
      const body = { ok: true, busy: busy[Math.min(calls.exports++, busy.length - 1)], vin_set: vinSet, imports };
      return { status: 200, body: error ? { ...body, error } : body };
    },
    upload: async (path, name, signal) => {
      calls.uploads.push(name);
      if (upload) await upload(signal);
      return { status: uploadStatus };
    },
  };
}

export function setup({ portal = fakePortal(), history = fakeHistory(), limits = {}, dataDir = mkdtempSync(join(tmpdir(), 'dataact-test-')) } = {}) {
  const logs = [];
  const clock = fakeClock();
  const runner = createRunner({
    dataDir, history, clock, createPortal: log => { portal.log = log; return portal; },
    limits: { ...LIMITS, ...limits }, log: line => logs.push(line),
  });
  return { dataDir, logs, clock, portal, history, runner };
}
