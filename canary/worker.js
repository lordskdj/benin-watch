// Benin Watch canary: the FAST path. Every minute (Cron Trigger) it checks ONE page and
// alerts through ntfy when it changes, redirects, answers an unexpected HTTP status, or stays
// unreachable. Nothing else: no classification, reminders, daily report or social signals
// (all of that is the Python watcher's job, see watcher.py).
//
// No dependency, no import: the same file runs on Cloudflare Workers and under `node --test`.
//
// Secrets (Cloudflare secrets, never in code or wrangler.toml): NTFY_TOPIC, NTFY_TOKEN (optional),
// HEALTHCHECK_URL (optional but recommended). Binding: CANARY_STATE (Durable Object).

export const URL_CANONICAL = 'https://www.voyage.benin.bj/'; // the www host is the reference
export const NTFY_URL = 'https://ntfy.sh/'; // topic goes in the JSON body, never in a URL
const USER_AGENT = 'BeninWatch-Canary/1.0 (personal availability monitor)';

const SITE_TIMEOUT_MS = 8000;
const NTFY_TIMEOUT_MS = 8000;
const HEALTH_TIMEOUT_MS = 5000;
const RETRY_DELAY_MS = 1000;
export const FULL_FETCH_EVERY_MS = 30 * 60 * 1000; // unconditional GET at least every 30 min
export const OUTAGE_ALERT_AFTER_MS = 3 * 60 * 1000; // one alert if unreachable for 3 min
// Anti-spam for a page that changes on every request: at most 3 alerts of the same kind per
// 30 min (max ~150/day, below ntfy.sh free limits). The first 3 changes are ALWAYS alerted.
export const QUIET_WINDOW_MS = 30 * 60 * 1000;
export const MAX_ALERTS_PER_WINDOW = 3;
export const LEASE_MS = 55 * 1000; // a run never lasts longer (worst case ~40 s)
export const MAX_PAGE_BYTES = 2_000_000; // same limit as the Python watcher; never read more
const TRANSIENT = new Set([408, 425, 429, 500, 502, 503, 504]);
const PRIORITY = { urgent: 5, high: 4, default: 3 };

// ---------------------------------------------------------------- logging (redacted)

const SECRET_NAMES = ['NTFY_TOPIC', 'NTFY_TOKEN', 'HEALTHCHECK_URL'];

export function makeRedactor(env) {
  const values = SECRET_NAMES.map((name) => String(env?.[name] ?? '').trim()).filter((v) => v.length >= 4);
  return (text) => values.reduce((out, v) => out.split(v).join('***'), String(text));
}

export function makeLogger(env, sink = console.log) {
  const redact = makeRedactor(env);
  return (message) => sink(redact(`[CANARY] ${message}`));
}

// ---------------------------------------------------------------- configuration

export function readConfig(env, log) {
  const topic = String(env?.NTFY_TOPIC ?? '').trim();
  if (!topic) throw new Error('NTFY_TOPIC manquant : le canari ne peut pas alerter');
  const token = String(env?.NTFY_TOKEN ?? '').trim();
  let health = String(env?.HEALTHCHECK_URL ?? '').trim();
  if (health) {
    let ok = false;
    try {
      const u = new URL(health);
      ok = u.protocol === 'https:' && !u.username && !u.password;
    } catch { /* invalid */ }
    if (!ok) {
      log('HEALTHCHECK_URL invalide (https requis) : ping desactive');
      health = '';
    }
  } else {
    log('HEALTHCHECK_URL absent : aucun ping healthchecks');
  }
  return { topic, token, health: health.replace(/\/+$/, '') };
}

// ---------------------------------------------------------------- observation (one GET)

async function sha256Hex(buffer) {
  const digest = await crypto.subtle.digest('SHA-256', buffer);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, '0')).join('');
}

function cleanHost(host) {
  return String(host || '?').toLowerCase().replace(/[^a-z0-9.:-]/g, '').slice(0, 80) || '?';
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** Body as bytes, or null if it exceeds `max` (checked on Content-Length AND while streaming). */
async function readCapped(res, max) {
  const declared = Number(res.headers.get('content-length'));
  if (Number.isFinite(declared) && declared > max) {
    await res.body?.cancel?.();
    return null;
  }
  if (!res.body) return new Uint8Array(await res.arrayBuffer());
  const reader = res.body.getReader();
  const chunks = [];
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (total > max) {
      await reader.cancel();
      return null;
    }
    chunks.push(value);
  }
  const bytes = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return bytes;
}

/**
 * One check of the page, never following redirects. Never throws for a site problem.
 * Returns one of:
 *   { kind: 'same' }                                   304 to our conditional request
 *   { kind: 'page', sig, family, hash, etag, lastModified, full }
 *   { kind: 'redirect', sig, family, status, host, sameHost }
 *   { kind: 'http', sig, family, status }              unexpected, non-transient status
 *   { kind: 'too_large', status }                      body > MAX_PAGE_BYTES (never read further)
 *   { kind: 'down', detail }                           network error / timeout / transient 5xx
 */
export async function observe(prev, now, { fetchFn = fetch, wait = sleep } = {}) {
  const conditional = Boolean(
    prev?.sig?.startsWith('page:') && (prev.etag || prev.lastModified) &&
    prev.fullAt && now - Date.parse(prev.fullAt) < FULL_FETCH_EVERY_MS,
  );
  const headers = { 'User-Agent': USER_AGENT, Accept: 'text/html,*/*;q=0.5' };
  if (conditional && prev.etag) headers['If-None-Match'] = prev.etag;
  if (conditional && prev.lastModified) headers['If-Modified-Since'] = prev.lastModified;

  let detail = 'inconnu';
  for (let attempt = 1; attempt <= 2; attempt += 1) {
    if (attempt > 1) await wait(RETRY_DELAY_MS);
    let res;
    try {
      res = await fetchFn(URL_CANONICAL, {
        method: 'GET', redirect: 'manual', cache: 'no-store', headers,
        signal: AbortSignal.timeout(SITE_TIMEOUT_MS),
      });
    } catch (err) {
      detail = err?.name === 'TimeoutError' ? 'timeout' : `erreur reseau (${err?.name || 'Error'})`;
      continue;
    }
    const { status } = res;
    if (status === 304) {
      await res.body?.cancel?.();
      if (conditional) return { kind: 'same' };
      return { kind: 'http', sig: 'http:304', family: 'http:304', status };
    }
    if (status >= 300 && status < 400) {
      await res.body?.cancel?.();
      const location = res.headers.get('location');
      let destination = '(sans Location)';
      let host = '?';
      if (location) {
        try {
          const target = new URL(location, URL_CANONICAL);
          destination = target.href;
          host = cleanHost(target.host);
        } catch {
          destination = '(Location invalide)';
        }
      }
      const sameHost = host === new URL(URL_CANONICAL).host;
      return { kind: 'redirect', sig: `redirect:${status}:${destination}`, family: `redirect:${host}`, status, host, sameHost };
    }
    if (status >= 200 && status < 300) {
      const body = await readCapped(res, MAX_PAGE_BYTES);
      if (!body) return { kind: 'too_large', status };
      const hash = await sha256Hex(body);
      return {
        kind: 'page', sig: `page:${hash}`, family: 'page', hash,
        etag: res.headers.get('etag'), lastModified: res.headers.get('last-modified'), full: !conditional,
      };
    }
    await res.body?.cancel?.();
    if (TRANSIENT.has(status)) {
      detail = `HTTP ${status}`;
      continue;
    }
    return { kind: 'http', sig: `http:${status}`, family: `http:${status}`, status };
  }
  return { kind: 'down', detail };
}

// ---------------------------------------------------------------- decision (pure)

function describe(obs) {
  if (obs.kind === 'page') return `page (empreinte ${obs.hash.slice(0, 12)})`;
  if (obs.kind === 'redirect') {
    return `redirection HTTP ${obs.status} vers ${obs.host} (${obs.sameHost ? 'meme hote' : 'AUTRE HOTE'})`;
  }
  return `HTTP ${obs.status}`;
}

function changeAlert(prev, obs, quietCount) {
  const extra = quietCount ? ` (${quietCount} autre(s) changement(s) non notifie(s) depuis la derniere alerte : page instable)` : '';
  if (obs.kind === 'redirect') {
    return {
      title: 'BENIN WATCH CF - REDIRECTION DETECTEE', priority: 'urgent', tags: ['rotating_light'],
      message: `Le site officiel redirige : ${describe(obs)}. Verifie que le domaine est legitime avant tout paiement !${extra}`,
    };
  }
  if (obs.kind === 'http') {
    return {
      title: 'BENIN WATCH CF - ANOMALIE HTTP', priority: 'urgent', tags: ['warning'],
      message: `Le site repond ${describe(obs)} (avant : ${prev.sig.split(':')[0]}). Migration ou ouverture possible : verifie !${extra}`,
    };
  }
  return {
    title: 'BENIN WATCH CF - PAGE MODIFIEE', priority: 'urgent', tags: ['rotating_light'],
    message: `Le contenu de la page a change (${describe(obs)}). Ouverture possible, NON confirmee : verifie immediatement !${extra}`,
  };
}

function merge(alerts) {
  if (!alerts.length) return null;
  const top = alerts.reduce((a, b) => (PRIORITY[b.priority] > PRIORITY[a.priority] ? b : a));
  return { ...top, message: alerts.map((a) => a.message).join('\n') };
}

/**
 * Pure state machine. Returns { state, dirty, alert }.
 * The caller saves `state` (if dirty) ONLY after `alert` (if any) was accepted by ntfy.
 */
export function decide(prev, obs, now) {
  const state = {
    sig: null, family: null, etag: null, lastModified: null, fullAt: null,
    failSince: null, failAlerted: false, alertTimes: [], quiet: 0, tooLarge: false, ...(prev || {}),
  };
  const nowIso = new Date(now).toISOString();
  const alerts = [];
  let dirty = false;

  if (obs.kind === 'down') {
    if (!state.failSince) {
      state.failSince = nowIso; // outage onset: one write, no alert yet (avoid flapping)
      dirty = true;
    } else if (!state.failAlerted && now - Date.parse(state.failSince) >= OUTAGE_ALERT_AFTER_MS) {
      const minutes = Math.round((now - Date.parse(state.failSince)) / 60000);
      alerts.push({
        title: 'BENIN WATCH CF - SITE INJOIGNABLE', priority: 'high', tags: ['warning'],
        message: `Le site ne repond pas depuis ${minutes} min (${obs.detail}). Surveillance aveugle pendant la panne.`,
      });
      state.failAlerted = true;
      dirty = true;
    }
    return { state, dirty, alert: merge(alerts) };
  }

  if (state.failSince) { // the site answered again
    if (state.failAlerted) {
      alerts.push({ title: 'BENIN WATCH CF - SITE RETABLI', priority: 'default', tags: ['white_check_mark'], message: 'Le site repond de nouveau.' });
    }
    state.failSince = null;
    state.failAlerted = false;
    dirty = true;
  }

  if (obs.kind === 'too_large') {
    // The server answered (not an outage) but the content is not a valid state: the last valid
    // page (sig, validators, fullAt) is kept untouched. Alerted once until a normal answer returns.
    if (!state.tooLarge) {
      alerts.push({
        title: 'BENIN WATCH CF - PAGE TROP VOLUMINEUSE', priority: 'urgent', tags: ['rotating_light'],
        message: `Le site renvoie une reponse HTTP ${obs.status} de plus de ${MAX_PAGE_BYTES / 1_000_000} Mo `
          + '(non analysee). Changement majeur possible : verifie immediatement !',
      });
      state.tooLarge = true;
      dirty = true;
    }
    return { state, dirty, alert: merge(alerts) };
  }
  if (state.tooLarge) { // any normal answer ends the "too large" episode
    state.tooLarge = false;
    dirty = true;
  }

  if (obs.kind === 'same') return { state, dirty, alert: merge(alerts) }; // 304: never a change

  if (obs.kind === 'page') {
    if (obs.full) { state.fullAt = nowIso; dirty = true; }
    if (state.etag !== obs.etag || state.lastModified !== obs.lastModified) {
      state.etag = obs.etag;
      state.lastModified = obs.lastModified;
      dirty = true;
    }
  } else if (state.etag || state.lastModified) {
    state.etag = null; // not a page any more: next request is unconditional
    state.lastModified = null;
    dirty = true;
  }

  if (!state.sig) { // first successful observation: baseline (the notification proves the pipeline)
    state.sig = obs.sig;
    state.family = obs.family;
    alerts.push({
      title: 'BENIN WATCH CF - canari demarre', priority: obs.kind === 'page' ? 'default' : 'urgent', tags: ['eyes'],
      message: `Canari actif. Etat initial : ${describe(obs)}.`,
    });
    return { state, dirty: true, alert: merge(alerts) };
  }

  if (obs.sig !== state.sig) {
    const recent = (state.alertTimes || []).filter((t) => now - Date.parse(t) < QUIET_WINDOW_MS);
    const quiet = obs.family === state.family && recent.length >= MAX_ALERTS_PER_WINDOW;
    if (quiet) {
      state.quiet += 1; // recorded, reported with the next alert
      state.alertTimes = recent;
    } else {
      alerts.push(changeAlert(state, obs, state.quiet));
      state.alertTimes = [...recent, nowIso].slice(-MAX_ALERTS_PER_WINDOW);
      state.quiet = 0;
    }
    state.sig = obs.sig;
    state.family = obs.family;
    dirty = true;
  }
  return { state, dirty, alert: merge(alerts) };
}

// ---------------------------------------------------------------- delivery

export async function sendNtfy(alert, cfg, { fetchFn = fetch, wait = sleep, log } = {}) {
  const body = JSON.stringify({
    topic: cfg.topic, title: alert.title, message: alert.message,
    priority: PRIORITY[alert.priority], tags: alert.tags || [], click: URL_CANONICAL,
  });
  const headers = { 'Content-Type': 'application/json' };
  if (cfg.token) headers.Authorization = `Bearer ${cfg.token}`;
  for (let attempt = 1; attempt <= 2; attempt += 1) {
    if (attempt > 1) await wait(RETRY_DELAY_MS);
    try {
      const res = await fetchFn(NTFY_URL, { method: 'POST', headers, body, signal: AbortSignal.timeout(NTFY_TIMEOUT_MS) });
      await res.body?.cancel?.();
      if (res.ok) return true;
      log(`ntfy a refuse le message : HTTP ${res.status}`);
      if (!TRANSIENT.has(res.status)) return false; // 400/401/403/413: retrying will not help
    } catch (err) {
      log(`ntfy injoignable : ${err?.name || 'Error'}`);
    }
  }
  return false;
}

export async function pingHealth(cfg, outcome, detail, { fetchFn = fetch, log } = {}) {
  if (!cfg.health) return;
  const target = outcome === 'fail' ? `${cfg.health}/fail` : cfg.health;
  try {
    const res = await fetchFn(target, {
      method: 'POST', body: String(detail).slice(0, 200),
      headers: { 'User-Agent': USER_AGENT, 'Content-Type': 'text/plain; charset=utf-8' },
      signal: AbortSignal.timeout(HEALTH_TIMEOUT_MS),
    });
    await res.body?.cancel?.();
    if (!res.ok) log(`healthcheck : HTTP ${res.status}`);
  } catch (err) {
    log(`healthcheck injoignable : ${err?.name || 'Error'}`); // never fails the run
  }
}

// ---------------------------------------------------------------- one run

/**
 * Order matters:
 *   lease -> observe -> decide -> ntfy -> save state -> health ping (success)
 * - ntfy refused/unreachable: state NOT saved (the same alert is retried next minute), ping /fail.
 * - any exception (config, storage...): no ping at all -> healthchecks detects the silence.
 * - site down: that is a normal, completed run (the outage is reported through ntfy) -> success ping.
 */
export async function runCheck({ env, store, now = Date.now(), fetchFn = fetch, wait = sleep, log }) {
  const cfg = readConfig(env, log);
  const lease = await store.begin(now);
  if (lease.busy) {
    log('Execution precedente encore en cours : ignore');
    return { outcome: 'busy' };
  }
  let released = false;
  try {
    const obs = await observe(lease.state, now, { fetchFn, wait });
    const { state, dirty, alert } = decide(lease.state, obs, now);
    log(`Observation : ${obs.kind}${obs.status ? ` ${obs.status}` : ''}${obs.hash ? ` ${obs.hash.slice(0, 12)}` : ''}${alert ? ` -> alerte ${alert.priority}` : ''}`);
    if (alert && !(await sendNtfy(alert, cfg, { fetchFn, wait, log }))) {
      await pingHealth(cfg, 'fail', 'ntfy n a pas accepte l alerte', { fetchFn, log });
      return { outcome: 'delivery_failed', alert };
    }
    await store.end(lease.token, dirty ? state : null);
    released = true;
    await pingHealth(cfg, 'success', `obs=${obs.kind}`, { fetchFn, log });
    return { outcome: 'ok', alert, saved: dirty };
  } finally {
    if (!released) await store.end(lease.token, null).catch(() => {});
  }
}

// ---------------------------------------------------------------- state: one Durable Object

/** Strongly consistent single-instance store with a lease against overlapping runs. */
export class CanaryState {
  constructor(ctx) {
    this.storage = ctx.storage;
    this.lease = null; // { token, until } (in memory: one live instance per id, by design)
  }

  async fetch(request) {
    const { pathname } = new URL(request.url);
    const body = await request.json().catch(() => ({}));
    const now = Date.now();
    if (pathname === '/begin') {
      if (this.lease && this.lease.until > now) return Response.json({ busy: true });
      const token = crypto.randomUUID();
      this.lease = { token, until: now + LEASE_MS };
      return Response.json({ busy: false, token, state: (await this.storage.get('state')) ?? null });
    }
    if (pathname === '/end') {
      if (!this.lease || this.lease.token !== body.token) {
        return Response.json({ ok: false, error: 'lease lost' }, { status: 409 });
      }
      if (body.state) await this.storage.put('state', body.state); // single key: atomic
      this.lease = null;
      return Response.json({ ok: true });
    }
    return new Response('not found', { status: 404 });
  }
}

export function durableStore(namespace) {
  const stub = namespace.get(namespace.idFromName('benin-watch-canary'));
  const call = async (path, payload) => {
    const res = await stub.fetch(`https://canary-state${path}`, { method: 'POST', body: JSON.stringify(payload) });
    const data = await res.json();
    if (!res.ok) throw new Error(`state ${path}: ${data.error || res.status}`);
    return data;
  };
  return {
    begin: (now) => call('/begin', { now }),
    end: (token, state) => call('/end', { token, state }),
  };
}

// ---------------------------------------------------------------- entry point (cron only, no HTTP route)

export default {
  async scheduled(_controller, env) {
    const log = makeLogger(env);
    try {
      await runCheck({ env, store: durableStore(env.CANARY_STATE), log });
    } catch (err) {
      log(`ECHEC du canari : ${err?.name || 'Error'}: ${err?.message || ''}`);
      throw new Error('canary run failed'); // marks the cron run as failed, without any secret
    }
  },
};
