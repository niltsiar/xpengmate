// The export job: record choice, polling, download and upload to history, with the portal, the history
// client, the clock and the data directory injected.
import { readdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import { rm } from 'node:fs/promises';
import { join } from 'node:path';

export const LIMITS = {
  deadlineMs: 25 * 60_000,
  pollMs: 20_000,
  pollCapMs: 15 * 60_000,
  uploadWaitMs: 10 * 60_000,
  cooldownMs: 15 * 60_000,
};

export const CLUSTERS = [
  'vehicle:dynamics', 'vehicle:driver_input', 'vehicle:powertrain',
  'vehicle:battery', 'vehicle:access', 'vehicle:health',
];
const RECORD_NO = /^[A-Z0-9]{10,40}$/;
const IN_FLIGHT = new Set(['PENDING', 'GENERATING', 'PROCESSING']);
const READY = new Set(['AVAILABLE', 'DOWNLOADED']);
const DEAD = new Set(['FAILED', 'EXPIRED']);
const ZIP_NAME = /^([A-Z0-9]{10,40})\.zip$/;

const STEP_TEXT = {
  history: 'history service check',
  captcha: 'captcha solver',
  login: 'XPENG login',
  vehicles: 'vehicle list',
  record: 'export request',
  wait: 'wait for the export',
  download: 'export download',
  upload: 'upload to history',
};

// `detail` is always a fixed text from this code base, never text from an error or a response.
export class JobError extends Error {
  constructor({ detail, status, code } = {}) {
    super(detail ?? 'job error');
    this.detail = detail;
    this.status = Number.isInteger(status) ? status : undefined;
    this.code = Number.isInteger(code) ? code : undefined;
  }
}

class DeadlineError extends Error {}

function makeScrubber(secrets) {
  const forms = secrets.filter(Boolean).flatMap(s => [s, encodeURIComponent(s), JSON.stringify(s).slice(1, -1)]);
  const unique = [...new Set(forms)].sort((a, b) => b.length - a.length);
  return text => {
    let out = String(text);
    for (const s of unique) out = out.split(s).join('<cut>');
    return out
      .replace(/\b[a-z][a-z0-9+.-]*:\/\/[^\s"'<>`]+/gi, u => {
        try { const x = new URL(u); return x.host + x.pathname; } catch { return '<url>'; }
      })
      .replace(/L[A-HJ-NPR-Z0-9]{16}/g, '<vin>');
  };
}

const errorClass = e => (e && typeof e === 'object' && e.constructor?.name) || typeof e;

function failureMessage(step, err) {
  let text = `${STEP_TEXT[step] ?? 'job'} failed`;
  if (err instanceof DeadlineError) return `${text}: the job passed its time limit`;
  if (!(err instanceof JobError)) return text;
  if (err.detail) text += `: ${err.detail}`;
  if (err.status !== undefined) text += ` (HTTP ${err.status})`;
  if (err.code !== undefined) text += ` (code ${err.code})`;
  return text;
}

const timeOf = v => (typeof v === 'number' ? v : Date.parse(String(v)));

function chooseRecord(activities, importedNames, now) {
  const usable = activities.filter(a =>
    RECORD_NO.test(a.recordNo) &&
    Number(a.timeRange) === 30 &&
    Array.isArray(a.dataClusters) &&
    a.dataClusters.length === CLUSTERS.length &&
    CLUSTERS.every(c => a.dataClusters.includes(c)) &&
    (IN_FLIGHT.has(a.status) ||
      (READY.has(a.status) && !(timeOf(a.expireTime) <= now) && !importedNames.has(`${a.recordNo}.zip`))));
  const created = a => timeOf(a.createTime) || 0;
  return usable.sort((a, b) => created(b) - created(a))[0];
}

async function runJob({ email, password, portal, history, clock, dataDir, limits, signal, setStep, setRecord, log }) {
  const pending = readdirSync(dataDir).map(n => ZIP_NAME.exec(n)?.[1]).filter(Boolean).sort()[0];
  if (pending) {
    setRecord(pending);
    setStep('upload');
    log(`uploading the kept zip ${pending}.zip before any login`);
    return upload(pending);
  }

  setStep('history');
  const pre = await history.exports();
  if (pre.status !== 200 || !pre.body?.ok) throw new JobError({ detail: 'history is not ready', status: pre.status });
  if (pre.body.running) throw new JobError({ detail: 'a history import is running' });
  const imported = new Set((pre.body.imports ?? []).map(i => i.filename));

  setStep('captcha');
  const validate = await portal.solveCaptcha();

  let loggedIn = false;
  let recordNo;
  try {
    setStep('login');
    await portal.login({ email, password, validate });
    loggedIn = true;

    setStep('vehicles');
    const owned = (await portal.vehicles()).filter(v => v.userType === 1);
    if (owned.length !== 1) throw new JobError({ detail: `${owned.length} owner vehicles on the account, expected 1` });

    setStep('record');
    const reuse = chooseRecord(await portal.activities(), imported, clock.now());
    if (reuse) {
      recordNo = reuse.recordNo;
      log(`reusing record ${recordNo} (${reuse.status})`);
    } else {
      recordNo = await portal.submit({ vinToken: owned[0].vinToken, dataClusters: CLUSTERS, timeRange: 30 });
      if (!RECORD_NO.test(recordNo)) throw new JobError({ detail: 'the portal gave an unexpected record number' });
      log(`submitted record ${recordNo}`);
    }
    setRecord(recordNo);

    setStep('wait');
    const waitStart = clock.now();
    let record;
    for (;;) {
      signal.throwIfAborted();
      record = (await portal.activities()).find(a => a.recordNo === recordNo);
      if (record && READY.has(record.status)) break;
      if (record && DEAD.has(record.status)) throw new JobError({ detail: `the export is ${record.status}` });
      if (clock.now() - waitStart >= limits.pollCapMs) throw new JobError({ detail: 'the export was not ready in time' });
      await clock.sleep(limits.pollMs);
    }

    setStep('download');
    await portal.downloadZip(record, dataDir);
  } finally {
    if (loggedIn) await portal.logout().catch(e => log(`logout failed (${errorClass(e)})`));
    await portal.close().catch(() => {});
  }

  setStep('upload');
  return upload(recordNo);

  async function upload(no) {
    const path = join(dataDir, `${no}.zip`);
    const waitStart = clock.now();
    for (;;) {
      signal.throwIfAborted();
      const st = await history.exports();
      if (st.status !== 200) throw new JobError({ detail: 'history is not ready; the zip is kept', status: st.status });
      if (!st.body?.running) break;
      if (clock.now() - waitStart >= limits.uploadWaitMs) throw new JobError({ detail: 'a history import is still running; the zip is kept' });
      await clock.sleep(limits.pollMs);
    }
    const res = await history.upload(path, `${no}.zip`);
    if (res.status !== 200) throw new JobError({ detail: 'history refused the zip; the zip is kept', status: res.status });
    await rm(path);
    return no;
  }
}

export function createRunner({ dataDir, history, createPortal, clock, limits = LIMITS, log }) {
  const statusPath = join(dataDir, 'status.json');
  let state = { state: 'idle', step: null, record_no: null, started_at: null, finished_at: null, message: null };
  try {
    const stored = JSON.parse(readFileSync(statusPath, 'utf8'));
    state = { ...state, ...stored };
    if (state.state === 'running') {
      set({ state: 'interrupted', finished_at: iso(), message: 'the service restarted during the job' });
    }
  } catch (e) {
    if (e.code !== 'ENOENT') log(`status.json not read (${errorClass(e)})`);
  }
  let current = null;
  let cooldownUntil = 0;

  function iso() { return new Date(clock.now()).toISOString(); }

  function set(change) {
    state = { ...state, ...change };
    try {
      writeFileSync(`${statusPath}.tmp`, JSON.stringify(state), { mode: 0o600 });
      renameSync(`${statusPath}.tmp`, statusPath);
    } catch (e) {
      log(`status.json not written (${errorClass(e)})`);
    }
  }

  function finish(job, change) {
    if (current !== job) return;
    current = null;
    clearTimeout(job.timer);
    job.controller.abort();
    set({ ...change, finished_at: iso() });
  }

  function fail(job, err) {
    if (current !== job) return;
    const step = state.step;
    job.log(`job failed at step ${step} (${errorClass(err)})`);
    if (step === 'login') cooldownUntil = clock.now() + limits.cooldownMs;
    job.portal.close().catch(() => {});
    finish(job, { state: 'failed', message: failureMessage(step, err) });
  }

  function start(email, password) {
    const scrub = makeScrubber([email, password]);
    const job = { log: line => log(scrub(line)), controller: new AbortController() };
    current = job;
    set({ state: 'running', step: null, record_no: null, started_at: iso(), finished_at: null, message: 'export request started' });
    job.log('job started');
    job.portal = createPortal(job.log);
    const deadline = new Promise((_, reject) => {
      job.timer = setTimeout(() => reject(new DeadlineError()), limits.deadlineMs);
    });
    const work = runJob({
      email, password, portal: job.portal, history, clock, dataDir, limits,
      signal: job.controller.signal,
      setStep: step => { if (current === job) { set({ step }); job.log(`step ${step}`); } },
      setRecord: no => { if (current === job) set({ record_no: no }); },
      log: job.log,
    });
    return Promise.race([work, deadline]).then(
      no => {
        job.log(`job done, ${no}.zip uploaded`);
        finish(job, { state: 'uploaded', message: `export ${no} uploaded to history` });
      },
      err => fail(job, err),
    );
  }

  return {
    status: () => ({ ...state }),
    request({ email, password }) {
      if (current) return { conflict: 'an export request is already running' };
      if (clock.now() < cooldownUntil) {
        return { conflict: `a login failed; try again after ${new Date(cooldownUntil).toISOString()}` };
      }
      return { done: start(email, password) };
    },
    crash(err) {
      if (current) fail(current, err);
      else log(`unexpected error outside a job (${errorClass(err)})`);
    },
  };
}
