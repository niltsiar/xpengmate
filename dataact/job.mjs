// The export job: record choice, polling, download and upload to history, with the portal, the history
// client, the clock and the data directory injected.
import { createWriteStream, readdirSync, readFileSync, renameSync, rmSync, writeFileSync } from 'node:fs';
import { rename, rm } from 'node:fs/promises';
import { join } from 'node:path';
import { pipeline } from 'node:stream/promises';

export const LIMITS = {
  deadlineMs: 25 * 60_000,
  pollMs: 20_000,
  pollCapMs: 15 * 60_000,
  uploadWaitMs: 10 * 60_000,
  cooldownMs: 15 * 60_000,
  zipBytes: 200 * 1024 * 1024,
  captchaAttempts: 3,
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
const ZIP_MAGIC = Buffer.from([0x50, 0x4b, 0x03, 0x04]);
const POLL_ERRORS = 3;

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

class DeadlineError extends Error {
  constructor() {
    super('the job passed its time limit');
  }
}

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

export const errorClass = e => (e && typeof e === 'object' && e.constructor?.name) || typeof e;
// Only for the scrubbed log: a Playwright message holds its call log, with typed values, after the first line.
const firstLine = e => String(e?.message ?? e).split('\n', 1)[0];

function failureMessage(step, err) {
  let text = `${STEP_TEXT[step] ?? 'job'} failed`;
  if (err instanceof DeadlineError) return `${text}: ${err.message}`;
  if (!(err instanceof JobError)) return text;
  if (err.detail) text += `: ${err.detail}`;
  if (err.status !== undefined) text += ` (HTTP ${err.status})`;
  if (err.code !== undefined) text += ` (code ${err.code})`;
  return text;
}

const timeOf = v => (typeof v === 'number' ? v : Date.parse(String(v)));

function chooseRecord(activities, importedNames, failedDownloads, now) {
  const usable = activities.filter(a =>
    RECORD_NO.test(a.recordNo) &&
    !failedDownloads.has(a.recordNo) &&
    Number(a.timeRange) === 30 &&
    Array.isArray(a.dataClusters) &&
    a.dataClusters.length === CLUSTERS.length &&
    CLUSTERS.every(c => a.dataClusters.includes(c)) &&
    (IN_FLIGHT.has(a.status) ||
      (READY.has(a.status) && timeOf(a.expireTime) > now && !importedNames.has(`${a.recordNo}.zip`))));
  const created = a => timeOf(a.createTime) || 0;
  return usable.sort((a, b) => created(b) - created(a))[0];
}

const keptZip = dataDir => readdirSync(dataDir).map(n => ZIP_NAME.exec(n)?.[1]).filter(Boolean).sort()[0];

async function saveZip(stream, dir, recordNo, maxBytes, signal) {
  const part = join(dir, `${recordNo}.zip.part`);
  let size = 0;
  let head = Buffer.alloc(0);
  try {
    await pipeline(stream, async function* (source) {
      for await (const chunk of source) {
        size += chunk.length;
        if (size > maxBytes) throw new JobError({ detail: `the export is larger than ${maxBytes / 2 ** 20} MB` });
        if (head.length < 4) head = Buffer.concat([head, chunk]).subarray(0, 4);
        yield chunk;
      }
    }, createWriteStream(part, { mode: 0o600 }), { signal });
    if (!head.equals(ZIP_MAGIC)) throw new JobError({ detail: 'the download is not a zip' });
    await rename(part, join(dir, `${recordNo}.zip`));
  } catch (e) {
    await rm(part, { force: true });
    throw e;
  }
}

async function runJob({ email, password, portal, history, clock, dataDir, limits, signal, failedDownloads, setStep, setRecord, log }) {
  const kept = keptZip(dataDir);
  if (kept) {
    setRecord(kept);
    setStep('upload');
    log(`uploading the kept zip ${kept}.zip before any login`);
    return upload(kept);
  }

  setStep('history');
  const pre = await history.exports(signal);
  if (pre.status !== 200 || !pre.body?.ok) throw new JobError({ detail: 'history is not ready', status: pre.status });
  if (pre.body.error) throw new JobError({ detail: 'history could not list its imports' });
  if (pre.body.busy) throw new JobError({ detail: 'history is busy with a sync or an import' });
  if (!pre.body.vin_set) throw new JobError({ detail: 'history has no VIN set' });
  const imported = new Set((pre.body.imports ?? []).map(i => i.filename));

  setStep('captcha');
  let validate;
  for (let attempt = 1; ; attempt++) {
    try {
      validate = await portal.solveCaptcha(signal);
      break;
    } catch (e) {
      if (signal.aborted || attempt >= limits.captchaAttempts) throw e;
      log(`captcha solver failed (${errorClass(e)}), attempt ${attempt} of ${limits.captchaAttempts}: ${firstLine(e)}`);
    }
  }

  let recordNo;
  try {
    setStep('login');
    await portal.login({ email, password, validate }, signal);

    setStep('vehicles');
    const owned = (await portal.vehicles(signal)).filter(v => v.userType === 1);
    if (owned.length !== 1) throw new JobError({ detail: `${owned.length} owner vehicles on the account, expected 1` });

    setStep('record');
    const reuse = chooseRecord(await portal.activities(signal), imported, failedDownloads, clock.now());
    if (reuse) {
      recordNo = reuse.recordNo;
      log(`reusing record ${recordNo} (${reuse.status})`);
    } else {
      recordNo = await portal.submit({ vinToken: owned[0].vinToken, dataClusters: CLUSTERS, timeRange: 30 }, signal);
      if (!RECORD_NO.test(recordNo)) throw new JobError({ detail: 'the portal gave an unexpected record number' });
      log(`submitted record ${recordNo}`);
    }
    setRecord(recordNo);

    setStep('wait');
    const waitStart = clock.now();
    let errors = 0;
    for (;;) {
      let list = [];
      try {
        list = await portal.activities(signal);
        errors = 0;
      } catch (e) {
        if (signal.aborted || ++errors > POLL_ERRORS) throw e;
        log(`record list failed (${errorClass(e)}), error ${errors} of ${POLL_ERRORS} in a row: ${firstLine(e)}`);
      }
      const record = list.find(a => a.recordNo === recordNo);
      if (record && READY.has(record.status)) break;
      if (record && DEAD.has(record.status)) throw new JobError({ detail: `the export is ${record.status}` });
      if (clock.now() - waitStart >= limits.pollCapMs) throw new JobError({ detail: 'the export was not ready in time' });
      await clock.sleep(limits.pollMs, signal);
    }

    setStep('download');
    try {
      await saveZip(await portal.download(recordNo, signal), dataDir, recordNo, limits.zipBytes, signal);
    } catch (e) {
      failedDownloads.add(recordNo);
      throw e;
    }
  } finally {
    await portal.logout().catch(e => log(`logout failed (${errorClass(e)})`));
    await portal.close().catch(() => {});
  }

  setStep('upload');
  return upload(recordNo);

  async function upload(no) {
    const path = join(dataDir, `${no}.zip`);
    const waitStart = clock.now();
    for (;;) {
      const st = await history.exports(signal);
      if (st.status !== 200) throw new JobError({ detail: 'history is not ready; the zip is kept', status: st.status });
      if (!st.body?.busy) break;
      if (clock.now() - waitStart >= limits.uploadWaitMs) throw new JobError({ detail: 'history is still busy; the zip is kept' });
      await clock.sleep(limits.pollMs, signal);
    }
    const res = await history.upload(path, `${no}.zip`, signal);
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
  for (const name of readdirSync(dataDir).filter(n => n.endsWith('.zip.part'))) {
    rmSync(join(dataDir, name), { force: true });
    log(`removed the partial download ${name}`);
  }
  let current = null;
  let cooldownUntil = 0;
  const failedDownloads = new Set();

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

  function fail(job, err) {
    const { signal } = job.controller;
    const step = state.step;
    job.log(`job failed at step ${step} (${errorClass(err)}): ${firstLine(err)}`);
    if (job.loginTried && (step === 'login' || !job.portal.loginSucceeded())) {
      cooldownUntil = clock.now() + limits.cooldownMs;
    }
    set({ state: 'failed', finished_at: iso(), message: failureMessage(step, signal.aborted ? signal.reason : err) });
  }

  function start(email, password) {
    const scrub = makeScrubber([email, password]);
    const controller = new AbortController();
    const { signal } = controller;
    const job = { log: line => log(scrub(line)), controller, loginTried: false };
    current = job;
    set({ state: 'running', step: null, record_no: null, started_at: iso(), finished_at: null, message: 'export request started' });
    job.log('job started');
    job.portal = createPortal(job.log);
    const timer = setTimeout(() => controller.abort(new DeadlineError()), limits.deadlineMs);
    return runJob({
      email, password, portal: job.portal, history, clock, dataDir, limits, signal, failedDownloads,
      setStep: step => {
        signal.throwIfAborted();
        if (step === 'login') job.loginTried = true;
        set({ step });
        job.log(`step ${step}`);
      },
      setRecord: no => set({ record_no: no }),
      log: job.log,
    }).then(
      no => {
        job.log(`job done, ${no}.zip uploaded`);
        set({ state: 'uploaded', finished_at: iso(), message: `export ${no} uploaded to history` });
      },
      err => fail(job, err),
    ).finally(() => {
      clearTimeout(timer);
      if (current === job) current = null;
    });
  }

  return {
    status: () => ({ ...state }),
    request({ email, password }) {
      if (current) return { reason: 'running', conflict: 'an export request is already running' };
      if (clock.now() < cooldownUntil && !keptZip(dataDir)) {
        return { reason: 'cooldown', conflict: `a login failed; try again after ${new Date(cooldownUntil).toISOString()}` };
      }
      return { done: start(email, password) };
    },
    crash(err) {
      if (!current) return log(`unexpected error outside a job (${errorClass(err)})`);
      current.log(`unexpected error during the job (${errorClass(err)}): ${firstLine(err)}`);
      current.controller.abort(err);
    },
  };
}
