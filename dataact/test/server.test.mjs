import assert from 'node:assert/strict';
import { once } from 'node:events';
import { test } from 'node:test';
import { JobError } from '../job.mjs';
import { createServer } from '../server.mjs';
import { EMAIL, PASSWORD, fakePortal, setup } from './fakes.mjs';

const TOKEN = 'test-token';

async function serve(env, t) {
  const server = createServer({ token: TOKEN, runner: env.runner, log: line => env.logs.push(line) });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  t.after(() => server.close());
  const base = `http://127.0.0.1:${server.address().port}`;
  return (path, { token = TOKEN, body, method = body === undefined ? 'GET' : 'POST' } = {}) =>
    fetch(base + path, {
      method,
      headers: token ? { authorization: `Bearer ${token}` } : {},
      body: typeof body === 'string' ? body : body && JSON.stringify(body),
    }).then(async r => ({ status: r.status, json: await r.json() }));
}

async function until(check) {
  while (!check()) await new Promise(r => setImmediate(r));
}

test('answers 409 while a job runs', async t => {
  let release;
  const env = setup({ portal: fakePortal({ login: () => new Promise(r => { release = r; }) }) });
  const call = await serve(env, t);
  assert.equal((await call('/request', { body: { email: EMAIL, password: PASSWORD } })).status, 202);
  const second = await call('/request', { body: { email: EMAIL, password: PASSWORD } });
  assert.deepEqual(second, { status: 409, json: { error: 'an export request is already running' } });
  await until(() => release);
  release();
  await until(() => env.runner.status().state !== 'running');
});

test('answers 409 during the cooldown after a failed login', async t => {
  const env = setup({ portal: fakePortal({ login: async () => { throw new JobError({ detail: 'login refused', code: 99 }); } }) });
  const call = await serve(env, t);
  assert.equal((await call('/request', { body: { email: EMAIL, password: PASSWORD } })).status, 202);
  await until(() => env.runner.status().state !== 'running');
  assert.equal(env.runner.status().message, 'XPENG login failed: login refused (code 99)');
  const second = await call('/request', { body: { email: EMAIL, password: PASSWORD } });
  assert.equal(second.status, 409);
  assert.match(second.json.error, /^a login failed; try again after 2099-01-01T00:15:00\.000Z$/);
  assert.equal(env.portal.calls.login, 1);
});

test('answers 401 without the token or with a wrong one', async t => {
  const env = setup();
  const call = await serve(env, t);
  for (const token of [null, 'wrong-token']) {
    assert.deepEqual(await call('/status', { token }), { status: 401, json: { error: 'unauthorized' } });
    const post = await call('/request', { token, body: { email: EMAIL, password: PASSWORD } });
    assert.equal(post.status, 401);
  }
  assert.equal(env.portal.calls.login, 0);
  assert.equal(env.runner.status().state, 'idle');
});

test('a bad JSON body gives a fixed 400 and the log holds no part of the body', async t => {
  const env = setup();
  const call = await serve(env, t);
  const reply = await call('/request', { body: `{"email":"${EMAIL}","password":"hunter2" x}` });
  assert.deepEqual(reply, { status: 400, json: { error: 'bad request' } });
  assert.deepEqual(env.logs, ['bad request body (SyntaxError)']);
  assert.equal(env.runner.status().state, 'idle');
});
