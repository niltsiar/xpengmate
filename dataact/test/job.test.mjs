import assert from 'node:assert/strict';
import { existsSync, readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { test } from 'node:test';
import { CLUSTERS, LIMITS } from '../job.mjs';
import { EMAIL, PASSWORD, ZIP_BYTES, fakeHistory, fakePortal, record, setup } from './fakes.mjs';

const OLD = 'DA20990101000000AAAA';
const NEW = 'DA20990101000000BBBB';

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

test('fails with the current step when the job deadline passes', { timeout: 5000 }, async () => {
  const env = setup({ portal: fakePortal({ login: () => new Promise(() => {}) }), limits: { deadlineMs: 50 } });
  const status = await run(env);
  assert.equal(status.state, 'failed');
  assert.equal(status.step, 'login');
  assert.equal(status.message, 'XPENG login failed: the job passed its time limit');
  assert.equal(env.portal.calls.close, 1);
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

test('a Playwright-shaped error with credentials and a signed URL leaves no trace in the log or status.json', async () => {
  const signed = 'https://oss.example.test/export/x.zip?Expires=4070908800&OSSAccessKeyId=STS.KEYID&Signature=SECRETSIG&security-token=STSTOKEN';
  const env = setup({
    portal: fakePortal({
      login: () => {
        env.portal.log(`request body email=${encodeURIComponent(EMAIL)}&password=${encodeURIComponent(PASSWORD)} to ${signed}`);
        const e = new Error([
          'locator.fill: Timeout 30000ms exceeded.',
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
  assert.match(logText, /job failed at step login \(Error\)/);
  const stored = readFileSync(join(env.dataDir, 'status.json'), 'utf8');
  const leaks = [EMAIL, PASSWORD, encodeURIComponent(EMAIL), encodeURIComponent(PASSWORD),
    JSON.stringify(PASSWORD).slice(1, -1), 'SECRETSIG', 'STSTOKEN', 'STS.KEYID', 'Expires='];
  for (const leak of leaks) {
    assert.equal(logText.includes(leak), false, `log holds ${leak}`);
    assert.equal(stored.includes(leak), false, `status.json holds ${leak}`);
  }
});
