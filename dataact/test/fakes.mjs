import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { CLUSTERS, LIMITS, createRunner } from '../job.mjs';

export const EMAIL = 'owner@example.test';
export const PASSWORD = 'pa"ss\\w/rd&1';
export const T0 = Date.parse('2099-01-01T00:00:00Z');
export const ZIP_BYTES = Buffer.from('PK\x03\x04fake export');

export const record = (recordNo, status, extra = {}) => ({
  recordNo, status, timeRange: 30, dataClusters: [...CLUSTERS],
  createTime: '2098-12-31 23:00:00', expireTime: '2099-01-07 23:00:00', ...extra,
});

export function fakeClock() {
  const clock = { t: T0, now: () => clock.t, sleep: ms => { clock.t += ms; return new Promise(r => setImmediate(r)); } };
  return clock;
}

// `lists` are the answers of successive activities() calls; the last one repeats.
export function fakePortal({ vehicles = [{ vinToken: 'TESTVINTOKEN', userType: 1 }], lists = [[]], login } = {}) {
  const calls = { login: 0, submit: [], download: [], logout: 0, close: 0 };
  let n = 0;
  return {
    calls,
    solveCaptcha: async () => 'validate-token',
    login: async args => { calls.login++; if (login) await login(args); },
    vehicles: async () => vehicles,
    activities: async () => lists[Math.min(n++, lists.length - 1)],
    submit: async args => { calls.submit.push(args); return 'DA20990101000000BBBB'; },
    downloadZip: async (rec, dir) => {
      calls.download.push(rec.recordNo);
      writeFileSync(join(dir, `${rec.recordNo}.zip`), ZIP_BYTES);
    },
    logout: async () => { calls.logout++; },
    close: async () => { calls.close++; },
  };
}

export function fakeHistory({ imports = [], running = [false], uploadStatus = 200 } = {}) {
  const calls = { uploads: [] };
  let n = 0;
  return {
    calls,
    exports: async () => ({ status: 200, body: { ok: true, running: running[Math.min(n++, running.length - 1)], imports } }),
    upload: async (path, name) => { calls.uploads.push(name); return { status: uploadStatus }; },
  };
}

export function setup({ portal = fakePortal(), history = fakeHistory(), limits = {} } = {}) {
  const dataDir = mkdtempSync(join(tmpdir(), 'dataact-test-'));
  const logs = [];
  const clock = fakeClock();
  const runner = createRunner({
    dataDir, history, clock, createPortal: log => { portal.log = log; return portal; },
    limits: { ...LIMITS, ...limits }, log: line => logs.push(line),
  });
  return { dataDir, logs, clock, portal, history, runner };
}
