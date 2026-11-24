import assert from 'node:assert/strict';
import { existsSync, mkdtempSync, readdirSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { Readable } from 'node:stream';
import { test } from 'node:test';
import { CLUSTERS, HOST_REFUSED, JobError, LIMITS } from '../job.mjs';
import { EMAIL, PASSWORD, ZIP_BYTES, fakeHistory, fakePortal, record, setup } from './fakes.mjs';

const OLD = 'DA20990101000000AAAA';
const NEW = 'DA20990101000000BBBB';
// VIN-shaped: a real VIN has no I, O or Q, so the scrubber pattern does not match the plain TESTVIN.
const VIN = 'LTESTVEH000000001';

async function until(check) {
  while (!check()) await new Promise(r => setImmediate(r));
}

async function run(env) {
  const result = env.runner.request({ email: EMAIL, password: PASSWORD });
  await result.done;
  return env.runner.status();
}

test('reuses a matching recent record instead of submitting a new one', async () => {
  const env = setup({ portal: fakePortal({ lists: [[record(OLD, 'PENDING')], [record(OLD, 'AVAILABLE')]] }) });
  const status = await run(env);
  assert.equal(status.state, 'uploaded');
  assert.equal(status.record_no, OLD);
  assert.deepEqual(env.portal.calls.submit, []);
  assert.deepEqual(env.history.calls.uploads, [`${OLD}.zip`]);
});

test('submits a new 30-day request with all six categories when no record matches', async () => {
  const env = setup({ portal: fakePortal({ lists: [[], [record(NEW, 'AVAILABLE')]] }) });
  const status = await run(env);
  assert.equal(status.state, 'uploaded');
  assert.deepEqual(env.portal.calls.submit, [{ vinToken: 'TESTVINTOKEN', dataClusters: CLUSTERS, timeRange: 30 }]);
  assert.deepEqual(env.portal.calls.download, [NEW]);
});

test('submits a new request when the matching record is already imported', async () => {
  const env = setup({
    portal: fakePortal({ lists: [[record(OLD, 'AVAILABLE')], [record(OLD, 'DOWNLOADED'), record(NEW, 'AVAILABLE')]] }),
    history: fakeHistory({ imports: [{ filename: `${OLD}.zip` }] }),
  });
  const status = await run(env);
  assert.equal(status.record_no, NEW);
  assert.equal(env.portal.calls.submit.length, 1);
  assert.deepEqual(env.portal.calls.download, [NEW]);
});

test('does not reuse a 7-day record', async () => {
  const env = setup({
    portal: fakePortal({ lists: [[record(OLD, 'PENDING', { timeRange: 7 })], [record(NEW, 'AVAILABLE')]] }),
  });
  const status = await run(env);
  assert.equal(status.record_no, NEW);
  assert.equal(env.portal.calls.submit.length, 1);
});

test('fails at once when the record turns FAILED while polling', async () => {
  const env = setup({ portal: fakePortal({ lists: [[], [record(NEW, 'PENDING')], [record(NEW, 'FAILED')]] }) });
  const status = await run(env);
  assert.equal(status.state, 'failed');
  assert.equal(status.step, 'wait');
  assert.equal(status.message, 'wait for the export failed: the export is FAILED');
  assert.equal(env.clock.now() - Date.parse(status.started_at), LIMITS.pollMs);
  assert.deepEqual(env.portal.calls.download, []);
});

test('fails at step wait when the export is not ready within the poll cap', { timeout: 5000 }, async () => {
  const env = setup({ portal: fakePortal({ lists: [[], [record(NEW, 'GENERATING')]] }) });
  const status = await run(env);
  assert.equal(status.state, 'failed');
  assert.equal(status.step, 'wait');
  assert.equal(status.message, 'wait for the export failed: the export was not ready in time');
  assert.equal(env.portal.calls.logout, 1);
});

test('a deadline during a slow login holds the slot until the login returns, and the job never submits', { timeout: 5000 }, async () => {
  let loginSignal;
  let release;
  const env = setup({
    portal: fakePortal({ login: (_, signal) => { loginSignal = signal; return new Promise(r => { release = r; }); } }),
    limits: { deadlineMs: 20 },
  });
  const first = env.runner.request({ email: EMAIL, password: PASSWORD });
  await until(() => loginSignal?.aborted);
  assert.equal(env.runner.request({ email: EMAIL, password: PASSWORD }).reason, 'running');
  assert.equal(env.runner.status().state, 'running');
  release();
  await first.done;
  const status = env.runner.status();
  assert.equal(status.state, 'failed');
  assert.equal(status.step, 'login');
  assert.equal(status.message, 'XPENG login failed: the job passed its time limit');
  assert.deepEqual(env.portal.calls.submit, []);
  assert.equal(env.portal.calls.close, 1);
  assert.equal(env.runner.request({ email: EMAIL, password: PASSWORD }).reason, 'cooldown');
});

test('an upload that history accepts after the deadline ends as uploaded', { timeout: 5000 }, async () => {
  const env = setup({
    history: fakeHistory({ upload: signal => until(() => signal.aborted) }),
    limits: { deadlineMs: 20 },
  });
  writeFileSync(join(env.dataDir, `${OLD}.zip`), ZIP_BYTES);
  const status = await run(env);
  assert.equal(status.state, 'uploaded');
  assert.equal(existsSync(join(env.dataDir, `${OLD}.zip`)), false);
});

test('a failed login answer starts the cooldown even when the job fails at a later step', async () => {
  const env = setup({ portal: fakePortal({ loginSucceeded: false, vehicles: [] }) });
  const status = await run(env);
  assert.equal(status.step, 'vehicles');
  assert.equal(env.runner.request({ email: EMAIL, password: PASSWORD }).reason, 'cooldown');
});

test('a second job after a failed download submits a new record instead of reusing the old one', async () => {
  const env = setup({
    portal: fakePortal({
      lists: [[record(OLD, 'AVAILABLE')], [record(OLD, 'AVAILABLE')], [record(OLD, 'DOWNLOADED')],
        [record(OLD, 'DOWNLOADED'), record(NEW, 'AVAILABLE')]],
      download: no => (no === OLD ? Readable.from([Buffer.from('<html>expired</html>')]) : Readable.from([ZIP_BYTES])),
    }),
  });
  assert.equal((await run(env)).state, 'failed');
  const status = await run(env);
  assert.equal(status.state, 'uploaded');
  assert.equal(env.portal.calls.submit.length, 1);
  assert.deepEqual(env.portal.calls.download, [OLD, NEW]);
  assert.deepEqual(env.history.calls.uploads, [`${NEW}.zip`]);
});

test('an export over the size cap fails at step download and leaves no zip and no part file', async () => {
  const env = setup({
    portal: fakePortal({
      lists: [[record(OLD, 'AVAILABLE')]],
      download: () => Readable.from([ZIP_BYTES, Buffer.alloc(64)]),
    }),
    limits: { zipBytes: 32 },
  });
  const status = await run(env);
  assert.equal(status.state, 'failed');
  assert.equal(status.message, `export download failed: the export is larger than ${32 / 2 ** 20} MB`);
  assert.deepEqual(env.portal.calls.download, [OLD]);
  assert.deepEqual(readdirSync(env.dataDir), ['status.json']);
});

test('a download that is not a zip fails at step download and leaves no zip and no part file', async () => {
  const env = setup({
    portal: fakePortal({
      lists: [[record(OLD, 'AVAILABLE')]],
      download: () => Readable.from([Buffer.from('<html>login</html>')]),
    }),
  });
  const status = await run(env);
  assert.equal(status.message, 'export download failed: the download is not a zip');
  assert.deepEqual(env.portal.calls.download, [OLD]);
  assert.deepEqual(readdirSync(env.dataDir), ['status.json']);
});

test('polling survives three record list errors in a row', async () => {
  const error = new JobError({ detail: 'the portal gave an unexpected answer', status: 502 });
  const env = setup({ portal: fakePortal({ lists: [[], error, error, error, [record(NEW, 'AVAILABLE')]] }) });
  const status = await run(env);
  assert.equal(status.state, 'uploaded');
});

test('polling fails at the fourth record list error in a row', async () => {
  const error = new JobError({ detail: 'the portal gave an unexpected answer', status: 502 });
  const env = setup({ portal: fakePortal({ lists: [[], error, error, error, error, [record(NEW, 'AVAILABLE')]] }) });
  const status = await run(env);
  assert.equal(status.state, 'failed');
  assert.equal(status.message, 'wait for the export failed: the portal gave an unexpected answer (HTTP 502)');
});

test('a kept zip is uploaded during the login cooldown without a login', async () => {
  const env = setup({ portal: fakePortal({ login: async () => { throw new JobError({ detail: 'login refused' }); } }) });
  assert.equal((await run(env)).step, 'login');
  writeFileSync(join(env.dataDir, `${OLD}.zip`), ZIP_BYTES);
  const status = await run(env);
  assert.equal(status.state, 'uploaded');
  assert.equal(env.portal.calls.login, 1);
});

test('fails before the solver and the login when history could not list its imports', async () => {
  const env = setup({ history: fakeHistory({ error: 'connection refused' }) });
  const status = await run(env);
  assert.equal(status.message, 'history service check failed: history could not list its imports');
  assert.equal(env.portal.calls.captcha, 0);
});

test('fails before the solver and the login while history is busy', async () => {
  const env = setup({ history: fakeHistory({ busy: [true] }) });
  const status = await run(env);
  assert.equal(status.message, 'history service check failed: history is busy with a sync or an import');
  assert.equal(env.portal.calls.captcha, 0);
});

test('waits while history is busy before the upload', async () => {
  const env = setup({
    portal: fakePortal({ lists: [[record(OLD, 'AVAILABLE')]] }),
    history: fakeHistory({ busy: [false, true, false] }),
  });
  const status = await run(env);
  assert.equal(status.state, 'uploaded');
  assert.equal(env.history.calls.exports, 3);
});

test('does not reuse a ready record without a valid expiry time', async () => {
  const env = setup({
    portal: fakePortal({ lists: [[record(OLD, 'AVAILABLE', { expireTime: 'soon' })], [record(NEW, 'AVAILABLE')]] }),
  });
  const status = await run(env);
  assert.equal(status.record_no, NEW);
  assert.equal(env.portal.calls.submit.length, 1);
});

test('removes partial downloads when the runner starts', () => {
  const dataDir = mkdtempSync(join(tmpdir(), 'dataact-test-'));
  writeFileSync(join(dataDir, `${OLD}.zip.part`), ZIP_BYTES);
  const env = setup({ dataDir });
  assert.deepEqual(readdirSync(env.dataDir), []);
  assert.deepEqual(env.logs, [`removed the partial download ${OLD}.zip.part`]);
});

test('logs out after a failed login', async () => {
  const env = setup({ portal: fakePortal({ login: async () => { throw new JobError({ detail: 'login refused' }); } }) });
  await run(env);
  assert.equal(env.portal.calls.logout, 1);
});

test('uploads a kept local zip before any login', { timeout: 5000 }, async () => {
  const env = setup();
  writeFileSync(join(env.dataDir, `${OLD}.zip`), ZIP_BYTES);
  const status = await run(env);
  assert.equal(status.state, 'uploaded');
  assert.equal(status.record_no, OLD);
  assert.equal(env.portal.calls.login, 0);
  assert.deepEqual(env.history.calls.uploads, [`${OLD}.zip`]);
  assert.equal(existsSync(join(env.dataDir, `${OLD}.zip`)), false);
});

test('keeps the zip when history answers 409 on upload', async () => {
  const env = setup({
    portal: fakePortal({ lists: [[record(OLD, 'AVAILABLE')]] }),
    history: fakeHistory({ uploadStatus: 409 }),
  });
  const status = await run(env);
  assert.equal(status.state, 'failed');
  assert.equal(status.step, 'upload');
  assert.equal(status.message, 'upload to history failed: history refused the zip; the zip is kept (HTTP 409)');
  assert.deepEqual(readFileSync(join(env.dataDir, `${OLD}.zip`)), ZIP_BYTES);
});

test('fails before the solver and the login when history has no VIN set', async () => {
  const env = setup({ history: fakeHistory({ vinSet: false }) });
  const status = await run(env);
  assert.equal(status.state, 'failed');
  assert.equal(status.step, 'history');
  assert.equal(status.message, 'history service check failed: history has no VIN set');
  assert.equal(env.portal.calls.captcha, 0);
  assert.equal(env.portal.calls.login, 0);
});

test('fails with the reason when the account has more than one owner vehicle', async () => {
  const vehicles = [{ vinToken: 'TESTVIN1', userType: 1 }, { vinToken: 'TESTVIN2', userType: 1 }, { vinToken: 'TESTVIN3', userType: 2 }];
  const env = setup({ portal: fakePortal({ vehicles }) });
  const status = await run(env);
  assert.equal(status.state, 'failed');
  assert.equal(status.message, 'vehicle list failed: 2 owner vehicles on the account, expected 1');
  assert.deepEqual(env.portal.calls.submit, []);
});

test('a Playwright-shaped error logs its scrubbed first line and leaves no secret in the log or status.json', async () => {
  const signed = 'https://oss.example.test/export/x.zip?Expires=4070908800&OSSAccessKeyId=STS.KEYID&Signature=SECRETSIG&security-token=STSTOKEN';
  const env = setup({
    portal: fakePortal({
      login: () => {
        env.portal.log(`request body email=${encodeURIComponent(EMAIL)}&password=${encodeURIComponent(PASSWORD)} to ${signed}`);
        const e = new Error([
          `page.goto: net::ERR_ABORTED at ${signed} for ${EMAIL} ${PASSWORD} ${VIN}`,
          'Call log:',
          `  - fill(${JSON.stringify(PASSWORD)}) on input for ${EMAIL}`,
          `  - navigating to "${signed}", waiting until "load"`,
          `  - raw ${PASSWORD}`,
        ].join('\n'));
        e.name = 'TimeoutError';
        throw e;
      },
    }),
  });
  const status = await run(env);
  assert.equal(status.message, 'XPENG login failed');
  const logText = env.logs.join('\n');
  assert.match(logText, /oss\.example\.test\/export\/x\.zip/);
  assert.ok(env.logs.includes(
    'job failed at step login (Error): page.goto: net::ERR_ABORTED at oss.example.test/export/x.zip for <cut> <cut> <vin>',
  ), logText);
  assert.equal(logText.includes('Call log'), false);
  const stored = readFileSync(join(env.dataDir, 'status.json'), 'utf8');
  const leaks = [EMAIL, PASSWORD, encodeURIComponent(EMAIL), encodeURIComponent(PASSWORD),
    JSON.stringify(PASSWORD).slice(1, -1), 'SECRETSIG', 'STSTOKEN', 'STS.KEYID', 'Expires=', VIN];
  for (const leak of leaks) {
    assert.equal(logText.includes(leak), false, `log holds ${leak}`);
    assert.equal(stored.includes(leak), false, `status.json holds ${leak}`);
  }
});

function recordSleeps(env) {
  const sleeps = [];
  const sleep = env.clock.sleep;
  env.clock.sleep = (ms, signal) => { sleeps.push(ms); return sleep(ms, signal); };
  return sleeps;
}

const solverDown = attempt => { throw new JobError({ detail: 'the solver gave no answer', status: attempt === 1 ? 502 : 504 }); };

test('a captcha solver that fails twice and then answers leads to a login', async () => {
  const env = setup({
    portal: fakePortal({ lists: [[], [record(NEW, 'AVAILABLE')]], solveCaptcha: attempt => { if (attempt < 3) solverDown(attempt); } }),
  });
  const sleeps = recordSleeps(env);
  const status = await run(env);
  assert.equal(status.state, 'uploaded');
  assert.deepEqual(sleeps, [LIMITS.captchaRetryMs, LIMITS.captchaRetryMs]);
  assert.equal(env.portal.calls.captcha, 3);
  assert.equal(env.portal.calls.login, 1);
  assert.equal(env.logs.filter(l => l.startsWith('captcha solver failed')).length, 2);
});

test('a captcha solver that fails three times ends failed at step captcha with no login', async () => {
  const env = setup({ portal: fakePortal({ solveCaptcha: solverDown }) });
  const sleeps = recordSleeps(env);
  const status = await run(env);
  assert.deepEqual(sleeps, [LIMITS.captchaRetryMs, LIMITS.captchaRetryMs]);
  assert.equal(status.state, 'failed');
  assert.equal(status.step, 'captcha');
  assert.equal(status.message, 'captcha solver failed: the solver gave no answer (HTTP 504)');
  assert.equal(env.portal.calls.captcha, 3);
  assert.equal(env.portal.calls.login, 0);
  assert.equal(env.runner.request({ email: EMAIL, password: PASSWORD }).reason, undefined);
});

async function secondJobAfter(firstDownload, limits) {
  let downloads = 0;
  const env = setup({
    portal: fakePortal({
      lists: [[record(OLD, 'AVAILABLE')]],
      download: () => (downloads++ === 0 ? firstDownload() : Readable.from([ZIP_BYTES])),
    }),
    limits,
  });
  assert.equal((await run(env)).state, 'failed');
  const status = await run(env);
  return { env, status };
}

test('a deadline during the download leaves the record reusable by the next job', { timeout: 5000 }, async () => {
  const { env, status } = await secondJobAfter(() => new Readable({ read() {} }), { deadlineMs: 50 });
  assert.equal(status.state, 'uploaded');
  assert.deepEqual(env.portal.calls.submit, []);
  assert.deepEqual(env.portal.calls.download, [OLD, OLD]);
  assert.deepEqual(env.history.calls.uploads, [`${OLD}.zip`]);
});

test('a refused download host leaves the record reusable by the next job', async () => {
  const { env, status } = await secondJobAfter(() => { throw new JobError({ detail: HOST_REFUSED }); }, {});
  assert.equal(status.state, 'uploaded');
  assert.deepEqual(env.portal.calls.submit, []);
  assert.deepEqual(env.portal.calls.download, [OLD, OLD]);
});
