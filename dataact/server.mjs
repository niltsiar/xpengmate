// HTTP front of the export job: GET /healthz, and POST /request, GET /status behind a bearer token.
import { createHash, timingSafeEqual } from 'node:crypto';
import { openAsBlob } from 'node:fs';
import http from 'node:http';
import { setTimeout as sleep } from 'node:timers/promises';
import { fileURLToPath } from 'node:url';
import { JobError, createRunner } from './job.mjs';
import { createPortal } from './portal.mjs';

const BODY_LIMIT = 4096;
const digest = s => createHash('sha256').update(s).digest();

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on('data', chunk => {
      size += chunk.length;
      if (size > BODY_LIMIT) reject(new RangeError('body too large'));
      else chunks.push(chunk);
    });
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
    req.on('error', reject);
  });
}

export function createServer({ token, runner, log }) {
  const expected = digest(token);
  return http.createServer(async (req, res) => {
    const send = (status, body) => {
      res.writeHead(status, { 'content-type': 'application/json', 'cache-control': 'no-store' });
      res.end(JSON.stringify(body));
    };
    const route = `${req.method} ${req.url}`;
    if (route === 'GET /healthz') return send(200, { ok: true });

    const [scheme, presented = ''] = (req.headers.authorization ?? '').split(' ');
    if (scheme?.toLowerCase() !== 'bearer' || !timingSafeEqual(digest(presented), expected)) {
      return send(401, { error: 'unauthorized' });
    }
    if (route === 'GET /status') return send(200, runner.status());
    if (route !== 'POST /request') return send(404, { error: 'not found' });

    let input;
    try {
      input = JSON.parse(await readBody(req));
    } catch (e) {
      if (e instanceof RangeError) {
        res.setHeader('connection', 'close');
        return send(413, { error: 'body too large' });
      }
      log(`bad request body (${e?.constructor?.name})`);
      return send(400, { error: 'bad request' });
    }
    const { email, password } = input ?? {};
    if (typeof email !== 'string' || typeof password !== 'string') return send(400, { error: 'bad request' });
    if (!email.trim() || !password) return send(400, { error: 'email and password are required' });

    const result = runner.request({ email: email.trim(), password });
    if (result.conflict) return send(409, { error: result.conflict });
    return send(202, runner.status());
  });
}

function historyClient(baseUrl, token) {
  const headers = { authorization: `Bearer ${token}` };
  const base = (baseUrl ?? '').replace(/\/$/, '');
  return {
    async exports() {
      if (!base) throw new JobError({ detail: 'the history URL is not configured' });
      const r = await fetch(`${base}/exports`, { headers, signal: AbortSignal.timeout(30_000) });
      return { status: r.status, body: r.ok ? await r.json() : null };
    },
    async upload(path, name) {
      const form = new FormData();
      form.append('file', await openAsBlob(path, { type: 'application/zip' }), name);
      const r = await fetch(`${base}/exports`, {
        method: 'POST', headers, body: form, signal: AbortSignal.timeout(10 * 60_000),
      });
      await r.body?.cancel();
      return { status: r.status };
    },
  };
}

function main() {
  const env = process.env;
  const token = env.XPENGMATE_DATAACT_TOKEN ?? '';
  if (!token) {
    console.error('XPENGMATE_DATAACT_TOKEN is empty; the service does not start without it');
    process.exit(1);
  }
  const log = line => console.log(`${new Date().toISOString()} ${line}`);
  const runner = createRunner({
    dataDir: env.DATA_DIR || '/data',
    history: historyClient(env.XPENGMATE_HISTORY_URL, env.XPENGMATE_HISTORY_UPLOAD_TOKEN ?? ''),
    createPortal: jobLog => createPortal({ log: jobLog, solverUrl: env.SOLVER_URL, solverToken: env.SOLVER_TOKEN ?? '' }),
    clock: { now: Date.now, sleep },
    log,
  });
  process.on('uncaughtException', e => runner.crash(e));
  process.on('unhandledRejection', e => runner.crash(e));
  const port = Number(env.PORT || 8300);
  createServer({ token, runner, log }).listen(port, () => log(`listening on ${port}`));
}

if (process.argv[1] === fileURLToPath(import.meta.url)) main();
