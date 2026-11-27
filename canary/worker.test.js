// Tests of the canary: node --test worker.test.js (no dependency, no real network call).
// The real CanaryState Durable Object class is used, over an in-memory storage.
import { test } from 'node:test';
import assert from 'node:assert/strict';

import worker, {
  CanaryState, durableStore, runCheck, makeLogger, URL_CANONICAL, NTFY_URL,
  OUTAGE_ALERT_AFTER_MS, FULL_FETCH_EVERY_MS, MAX_PAGE_BYTES,
} from './worker.js';

// Fake secrets: they must never appear in any log line or URL.
const HC = 'https://hc-ping.example.org/secret-check-0001';
const ENV = { NTFY_TOPIC: 'secret-topic-abc123', NTFY_TOKEN: 'tk_fake_token_value_42', HEALTHCHECK_URL: HC };
const T0 = Date.parse('2026-11-27T10:00:00Z');
const MIN = 60 * 1000;

const PAGE = '<html><body><h1>Votre plateforme sera bientot disponible</h1></body></html>';
const page = (body = PAGE, headers = { etag: '"v1"', 'last-modified': 'Fri, 07 Aug 2026 17:12:31 GMT' }) =>
  new Response(body, { status: 200, headers: { 'content-type': 'text/html', ...headers } });
const status = (code, headers = {}) => new Response(null, { status: code, headers });
const TOO_BIG = 'a'.repeat(MAX_PAGE_BYTES + 1);
const huge = () => new Response(TOO_BIG, {
  status: 200, headers: { 'content-type': 'text/html', 'content-length': String(TOO_BIG.length) },
});
/** Chunked body WITHOUT Content-Length; records how many chunks were actually pulled. */
function streamed(chunkCount, chunkSize, counter) {
  const chunk = new Uint8Array(chunkSize).fill(97);
  let sent = 0;
  const body = new ReadableStream({
    pull(controller) {
      if (sent >= chunkCount) return controller.close();
      sent += 1;
      counter.pulled = sent;
      controller.enqueue(chunk);
    },
  });
  return new Response(body, { status: 200, headers: { 'content-type': 'text/html' } });
}

function memoryStorage() {
  const map = new Map();
  return {
    map,
    get: async (key) => structuredClone(map.get(key)),
    put: async (key, value) => { map.set(key, structuredClone(value)); },
  };
}

function namespaceFor(object) {
  return {
    idFromName: (name) => name,
    get: () => ({ fetch: (url, init) => object.fetch(new Request(url, init)) }),
  };
}

/** One environment shared by successive runs: same storage, same Durable Object instance. */
function setup({ env = ENV } = {}) {
  const storage = memoryStorage();
  const object = new CanaryState({ storage });
  const store = durableStore(namespaceFor(object));
  const logs = [];
  const log = makeLogger(env, (line) => logs.push(line));
  const calls = [];

  async function run(now, { site = [], ntfy = [], health = [] } = {}) {
    const fetchFn = async (url, init = {}) => {
      calls.push({ url: String(url), init, now });
      if (url === URL_CANONICAL) {
        const next = site.shift();
        if (next === undefined) throw new Error('no site response scripted');
        if (next instanceof Error) throw next;
        return next;
      }
      if (url === NTFY_URL) {
        const next = ntfy.shift() ?? 200;
        if (next instanceof Error) throw next;
        return new Response('{}', { status: next });
      }
      if (String(url).startsWith(HC)) {
        const next = health.shift() ?? 200;
        if (next instanceof Error) throw next;
        return new Response('OK', { status: next });
      }
      throw new Error('unexpected request');
    };
    const before = calls.length;
    const result = await runCheck({ env, store, now, fetchFn, wait: async () => {}, log });
    return { result, calls: calls.slice(before) };
  }

  const only = (list, pred) => list.filter(pred);
  const ntfyCalls = (list) => only(list, (c) => c.url === NTFY_URL).map((c) => JSON.parse(c.init.body));
  const siteCalls = (list) => only(list, (c) => c.url === URL_CANONICAL);
  const healthCalls = (list) => only(list, (c) => c.url.startsWith(HC)).map((c) => c.url);
  const saved = () => storage.map.get('state');
  return { run, storage, object, store, logs, calls, ntfyCalls, siteCalls, healthCalls, saved };
}

async function baseline(t, body = PAGE, headers) {
  await t.run(T0, { site: [page(body, headers)] });
}

// ------------------------------------------------------------------ first run / existing state

test('first run without state: baseline notification, state saved, health success', async () => {
  const t = setup();
  const { result, calls } = await t.run(T0, { site: [page()] });
  assert.equal(result.outcome, 'ok');
  const [msg] = t.ntfyCalls(calls);
  assert.match(msg.title, /canari demarre/);
  assert.equal(msg.priority, 3);
  assert.match(t.saved().sig, /^page:[0-9a-f]{64}$/);
  assert.deepEqual(t.healthCalls(calls), [HC]);
});

test('existing state, 200 with identical content: no alert, health success', async () => {
  const t = setup();
  await baseline(t, PAGE, {}); // no validators -> next request is a full GET
  const before = t.saved();
  const { result, calls } = await t.run(T0 + MIN, { site: [page(PAGE, {})] });
  assert.equal(result.outcome, 'ok');
  assert.equal(t.ntfyCalls(calls).length, 0);
  assert.equal(t.saved().sig, before.sig);
  assert.deepEqual(t.healthCalls(calls), [HC]);
});

test('304 Not Modified is never a change, and conditional headers are sent', async () => {
  const t = setup();
  await baseline(t);
  const before = structuredClone(t.saved());
  const { result, calls } = await t.run(T0 + MIN, { site: [status(304)] });
  const [req] = t.siteCalls(calls);
  assert.equal(req.init.headers['If-None-Match'], '"v1"');
  assert.equal(req.init.headers['If-Modified-Since'], 'Fri, 07 Aug 2026 17:12:31 GMT');
  assert.equal(result.outcome, 'ok');
  assert.equal(t.ntfyCalls(calls).length, 0);
  assert.deepEqual(t.saved(), before); // nothing written
});

test('an unconditional GET is forced every 30 minutes', async () => {
  const t = setup();
  await baseline(t);
  const { calls } = await t.run(T0 + FULL_FETCH_EVERY_MS + MIN, { site: [page()] });
  assert.equal(t.siteCalls(calls)[0].init.headers['If-None-Match'], undefined);
});

test('304 without a conditional request is an HTTP anomaly', async () => {
  const t = setup();
  await baseline(t, PAGE, {});
  const { calls } = await t.run(T0 + MIN, { site: [status(304)] });
  assert.match(t.ntfyCalls(calls)[0].title, /ANOMALIE HTTP/);
});

// ------------------------------------------------------------------ changes

test('content change: one urgent alert, then no duplicate for the same content', async () => {
  const t = setup();
  await baseline(t);
  const changed = PAGE.replace('bientot disponible', 'Reservez votre vol');
  const first = await t.run(T0 + MIN, { site: [page(changed, { etag: '"v2"' })] });
  const [msg] = t.ntfyCalls(first.calls);
  assert.match(msg.title, /PAGE MODIFIEE/);
  assert.equal(msg.priority, 5);
  assert.equal(msg.click, URL_CANONICAL);
  const second = await t.run(T0 + 2 * MIN, { site: [status(304)] });
  const third = await t.run(T0 + FULL_FETCH_EVERY_MS + 3 * MIN, { site: [page(changed, { etag: '"v2"' })] });
  assert.equal(t.ntfyCalls(second.calls).length + t.ntfyCalls(third.calls).length, 0);
});

test('change invisible in the rendered page (HTML comment) is detected', async () => {
  const t = setup();
  await baseline(t, PAGE, {});
  const { calls } = await t.run(T0 + MIN, { site: [page(PAGE.replace('</body>', '<!-- v2 --></body>'), {})] });
  assert.match(t.ntfyCalls(calls)[0].title, /PAGE MODIFIEE/);
});

test('a page changing on every request is capped (3 alerts / 30 min), the count is reported later', async () => {
  const t = setup();
  await baseline(t, PAGE, {});
  let alerts = 0;
  for (let i = 1; i <= 6; i += 1) {
    const { calls } = await t.run(T0 + i * MIN, { site: [page(`${PAGE}<!-- ${i} -->`, {})] });
    alerts += t.ntfyCalls(calls).length;
  }
  assert.equal(alerts, 3); // the first 3 changes are always alerted
  const later = await t.run(T0 + 40 * MIN, { site: [page(`${PAGE}<!-- 7 -->`, {})] });
  assert.match(t.ntfyCalls(later.calls)[0].message, /3 autre\(s\) changement\(s\) non notifie/);
});

// ------------------------------------------------------------------ redirects

test('redirect www -> bare domain: alerted with the host, never followed', async () => {
  const t = setup();
  await baseline(t);
  const { calls } = await t.run(T0 + MIN, { site: [status(301, { location: 'https://voyage.benin.bj/' })] });
  const [req] = t.siteCalls(calls);
  assert.equal(req.init.redirect, 'manual');
  assert.equal(t.siteCalls(calls).length, 1); // the destination is never requested
  assert.ok(calls.every((c) => c.url !== 'https://voyage.benin.bj/'));
  const [msg] = t.ntfyCalls(calls);
  assert.match(msg.title, /REDIRECTION DETECTEE/);
  assert.equal(msg.priority, 5);
  assert.match(msg.message, /vers voyage\.benin\.bj \(AUTRE HOTE\)/);
  assert.equal(msg.click, URL_CANONICAL); // never the redirect target
});

test('relative redirect on the same host is reported as same host', async () => {
  const t = setup();
  await baseline(t);
  const { calls } = await t.run(T0 + MIN, { site: [status(302, { location: '/reservation' })] });
  assert.match(t.ntfyCalls(calls)[0].message, /vers www\.voyage\.benin\.bj \(meme hote\)/);
});

// ------------------------------------------------------------------ HTTP anomalies and outages

test('HTTP 404 (non transient): urgent anomaly, no retry', async () => {
  const t = setup();
  await baseline(t);
  const { calls } = await t.run(T0 + MIN, { site: [status(404)] });
  assert.equal(t.siteCalls(calls).length, 1);
  const [msg] = t.ntfyCalls(calls);
  assert.match(msg.title, /ANOMALIE HTTP/);
  assert.match(msg.message, /HTTP 404/);
});

test('HTTP 503: retried, outage alerted once after 3 min, then recovery (no false change)', async () => {
  const t = setup();
  await baseline(t);
  const first = await t.run(T0 + MIN, { site: [status(503), status(503)] });
  assert.equal(t.siteCalls(first.calls).length, 2); // one retry
  assert.equal(t.ntfyCalls(first.calls).length, 0); // no alert on the first failure
  assert.deepEqual(t.healthCalls(first.calls), [HC]); // site down != canary down
  const second = await t.run(T0 + MIN + OUTAGE_ALERT_AFTER_MS, { site: [status(503), status(503)] });
  const [outage] = t.ntfyCalls(second.calls);
  assert.match(outage.title, /SITE INJOIGNABLE/);
  assert.equal(outage.priority, 4);
  const third = await t.run(T0 + 5 * MIN, { site: [status(503), status(503)] });
  assert.equal(t.ntfyCalls(third.calls).length, 0); // alerted once only
  const back = await t.run(T0 + 6 * MIN, { site: [status(304)] });
  const msgs = t.ntfyCalls(back.calls);
  assert.equal(msgs.length, 1);
  assert.match(msgs[0].title, /SITE RETABLI/);
});

test('network error: retried, treated as an outage, not as a change', async () => {
  const t = setup();
  await baseline(t);
  const { result, calls } = await t.run(T0 + MIN, { site: [new TypeError('fetch failed'), new TypeError('fetch failed')] });
  assert.equal(result.outcome, 'ok');
  assert.equal(t.siteCalls(calls).length, 2);
  assert.equal(t.ntfyCalls(calls).length, 0);
  assert.ok(t.saved().failSince);
});

// ------------------------------------------------------------------ page too large (> 2 MB)

test('page > 2 MB (Content-Length): urgent PAGE TROP VOLUMINEUSE, last valid state kept', async () => {
  const t = setup();
  await baseline(t);
  const before = structuredClone(t.saved());
  const { result, calls } = await t.run(T0 + MIN, { site: [huge()] });
  assert.equal(result.outcome, 'ok');
  assert.equal(t.siteCalls(calls).length, 1); // not retried: not a network outage
  const [msg] = t.ntfyCalls(calls);
  assert.match(msg.title, /PAGE TROP VOLUMINEUSE/);
  assert.equal(msg.priority, 5);
  const after = t.saved();
  for (const key of ['sig', 'family', 'etag', 'lastModified', 'fullAt']) assert.equal(after[key], before[key]);
  assert.equal(after.failSince, null); // not an outage
  assert.equal(after.tooLarge, true);
  assert.deepEqual(t.healthCalls(calls), [HC]); // completed run
});

test('page > 2 MB without Content-Length: detected while streaming, reading stops early', async () => {
  const t = setup();
  await baseline(t, PAGE, {});
  const counter = { pulled: 0 };
  const chunkSize = 500_000;
  const { calls } = await t.run(T0 + MIN, { site: [streamed(20, chunkSize, counter)] });
  assert.match(t.ntfyCalls(calls)[0].title, /PAGE TROP VOLUMINEUSE/);
  assert.ok(counter.pulled <= Math.ceil(MAX_PAGE_BYTES / chunkSize) + 2, `pulled ${counter.pulled} chunks`);
});

test('Content-Length > 2 MB: the body is not read at all', async () => {
  const t = setup();
  await baseline(t, PAGE, {});
  const counter = { pulled: 0 };
  const res = streamed(20, 500_000, counter);
  res.headers.set('content-length', String(20 * 500_000));
  const { calls } = await t.run(T0 + MIN, { site: [res] });
  assert.match(t.ntfyCalls(calls)[0].title, /PAGE TROP VOLUMINEUSE/);
  assert.ok(counter.pulled <= 1, `pulled ${counter.pulled} chunks`); // at most the stream's own pre-fill
});

test('a page of exactly 2 MB is still a valid page', async () => {
  const t = setup();
  await baseline(t, PAGE, {});
  const { calls } = await t.run(T0 + MIN, { site: [page('b'.repeat(MAX_PAGE_BYTES), {})] });
  assert.match(t.ntfyCalls(calls)[0].title, /PAGE MODIFIEE/);
  assert.equal(t.saved().tooLarge, false);
});

test('page too large: alerted once per episode, no duplicate, cleared by a normal answer', async () => {
  const t = setup();
  await baseline(t);
  const first = await t.run(T0 + MIN, { site: [huge()] });
  const again = await t.run(T0 + 2 * MIN, { site: [huge()] });
  assert.equal(t.ntfyCalls(first.calls).length, 1);
  assert.equal(t.ntfyCalls(again.calls).length, 0); // no duplicate
  const back = await t.run(T0 + 3 * MIN, { site: [status(304)] }); // old page again
  assert.equal(t.ntfyCalls(back.calls).length, 0); // same as the last valid page: nothing to say
  assert.equal(t.saved().tooLarge, false);
  const newEpisode = await t.run(T0 + 4 * MIN, { site: [huge()] });
  assert.equal(t.ntfyCalls(newEpisode.calls).length, 1);
});

test('page too large, then a different valid page: PAGE MODIFIEE against the last valid page', async () => {
  const t = setup();
  await baseline(t);
  await t.run(T0 + MIN, { site: [huge()] });
  const { calls } = await t.run(T0 + 2 * MIN, { site: [page(`${PAGE}<!-- v2 -->`, { etag: '"v2"' })] });
  assert.match(t.ntfyCalls(calls)[0].title, /PAGE MODIFIEE/);
  assert.equal(t.saved().tooLarge, false);
});

test('page too large ends an outage (the server answered): recovery + urgent alert', async () => {
  const t = setup();
  await baseline(t);
  await t.run(T0 + MIN, { site: [status(503), status(503)] });
  await t.run(T0 + MIN + OUTAGE_ALERT_AFTER_MS, { site: [status(503), status(503)] }); // outage alerted
  const { calls } = await t.run(T0 + 6 * MIN, { site: [huge()] });
  const [msg] = t.ntfyCalls(calls);
  assert.match(msg.title, /PAGE TROP VOLUMINEUSE/); // the most urgent title wins
  assert.match(msg.message, /repond de nouveau/);
  assert.equal(t.saved().failSince, null);
});

test('page too large + ntfy failure: nothing saved, /fail ping, alert re-sent once next run', async () => {
  const t = setup();
  await baseline(t);
  const before = structuredClone(t.saved());
  const failed = await t.run(T0 + MIN, { site: [huge()], ntfy: [503, 503] });
  assert.equal(failed.result.outcome, 'delivery_failed');
  assert.deepEqual(t.saved(), before);
  assert.deepEqual(t.healthCalls(failed.calls), [`${HC}/fail`]);
  const retried = await t.run(T0 + 2 * MIN, { site: [huge()] });
  assert.equal(t.ntfyCalls(retried.calls).length, 1);
  const after = await t.run(T0 + 3 * MIN, { site: [huge()] });
  assert.equal(t.ntfyCalls(after.calls).length, 0);
});

// ------------------------------------------------------------------ ntfy delivery

test('ntfy success: request format (topic in body, token in header, never in the URL)', async () => {
  const t = setup();
  const { calls } = await t.run(T0, { site: [page()] });
  const req = calls.find((c) => c.url === NTFY_URL);
  assert.equal(req.url, 'https://ntfy.sh/');
  assert.equal(JSON.parse(req.init.body).topic, ENV.NTFY_TOPIC);
  assert.equal(req.init.headers.Authorization, `Bearer ${ENV.NTFY_TOKEN}`);
  assert.ok(calls.every((c) => !c.url.includes(ENV.NTFY_TOPIC) && !c.url.includes(ENV.NTFY_TOKEN)));
});

test('ntfy failure: state NOT saved, /fail ping, alert re-sent next run exactly once', async () => {
  const t = setup();
  await baseline(t);
  const before = structuredClone(t.saved());
  const changed = `${PAGE}<!-- new -->`;
  const failed = await t.run(T0 + MIN, { site: [page(changed, { etag: '"v2"' })], ntfy: [500, 503] });
  assert.equal(failed.result.outcome, 'delivery_failed');
  assert.equal(t.ntfyCalls(failed.calls).length, 2); // transient: one retry
  assert.deepEqual(t.saved(), before); // the change is not marked as handled
  assert.deepEqual(t.healthCalls(failed.calls), [`${HC}/fail`]);
  // Old validators were kept, so the server answers 200 with the new page again.
  const retried = await t.run(T0 + 2 * MIN, { site: [page(changed, { etag: '"v2"' })] });
  assert.equal(t.siteCalls(retried.calls)[0].init.headers['If-None-Match'], '"v1"');
  const [msg] = t.ntfyCalls(retried.calls);
  assert.match(msg.title, /PAGE MODIFIEE/); // delivered now
  const after = await t.run(T0 + 3 * MIN, { site: [status(304)] });
  assert.equal(t.siteCalls(after.calls)[0].init.headers['If-None-Match'], '"v2"');
  assert.equal(t.ntfyCalls(after.calls).length, 0); // and only once
});

test('ntfy permanent refusal (403): no retry, /fail ping', async () => {
  const t = setup();
  const { result, calls } = await t.run(T0, { site: [page()], ntfy: [403] });
  assert.equal(result.outcome, 'delivery_failed');
  assert.equal(t.ntfyCalls(calls).length, 1);
  assert.deepEqual(t.healthCalls(calls), [`${HC}/fail`]);
  assert.equal(t.saved(), undefined);
});

// ------------------------------------------------------------------ healthcheck semantics

test('healthcheck is pinged only after a completed run, after the state was saved', async () => {
  const t = setup();
  const { calls } = await t.run(T0, { site: [page()] });
  const order = calls.map((c) => (c.url === URL_CANONICAL ? 'site' : c.url === NTFY_URL ? 'ntfy' : 'health'));
  assert.deepEqual(order, ['site', 'ntfy', 'health']);
  assert.ok(t.saved()); // saved before the ping (the ping is the last request)
});

test('crash (storage failure): no healthcheck ping at all', async () => {
  const t = setup();
  t.storage.put = async () => { throw new Error('storage unavailable'); };
  await assert.rejects(t.run(T0, { site: [page()] }));
  assert.equal(t.healthCalls(t.calls).length, 0);
});

test('missing NTFY_TOPIC is fatal: no request, no healthcheck ping', async () => {
  const t = setup({ env: { HEALTHCHECK_URL: HC } });
  await assert.rejects(t.run(T0, { site: [page()] }), /NTFY_TOPIC/);
  assert.equal(t.calls.length, 0);
});

test('a failing healthcheck never fails the run', async () => {
  const t = setup();
  const { result } = await t.run(T0, { site: [page()], health: [new Error(`connect failed ${HC}`)] });
  assert.equal(result.outcome, 'ok');
});

// ------------------------------------------------------------------ secrets never logged

test('no secret in any log line, whatever happens', async () => {
  const t = setup();
  await t.run(T0, { site: [page()], health: [new Error(`ECONNREFUSED ${HC}`)] });
  await t.run(T0 + MIN, { site: [page(`${PAGE}x`, {})], ntfy: [new Error(`boom ${ENV.NTFY_TOPIC}`), 401] });
  await t.run(T0 + 2 * MIN, { site: [new TypeError('fetch failed'), status(500)] });
  await t.run(T0 + 3 * MIN, { site: [huge()], ntfy: [new Error(`refused ${ENV.NTFY_TOKEN}`), 500] });
  const all = t.logs.join('\n');
  assert.ok(t.logs.length > 3);
  for (const secret of [ENV.NTFY_TOPIC, ENV.NTFY_TOKEN, HC, 'secret-check-0001']) {
    assert.ok(!all.includes(secret), 'a secret leaked into the logs');
  }
});

test('the logger redacts secret values even if a message contains them', () => {
  const lines = [];
  const log = makeLogger(ENV, (l) => lines.push(l));
  log(`topic=${ENV.NTFY_TOPIC} token=${ENV.NTFY_TOKEN} url=${HC}`);
  assert.equal(lines[0], '[CANARY] topic=*** token=*** url=***');
});

test('scheduled(): a crash is reported without secret and fails the cron run', async () => {
  const lines = [];
  const original = console.log;
  console.log = (l) => lines.push(l);
  try {
    const env = { ...ENV, CANARY_STATE: { idFromName: () => 'x', get: () => ({ fetch: async () => { throw new Error(`down ${HC}`); } }) } };
    await assert.rejects(worker.scheduled({}, env), /canary run failed/);
  } finally {
    console.log = original;
  }
  assert.match(lines.join('\n'), /ECHEC du canari/);
  assert.ok(!lines.join('\n').includes(HC));
});

// ------------------------------------------------------------------ Durable Object state

test('Durable Object: lease prevents overlapping runs, stale token cannot write', async () => {
  const storage = memoryStorage();
  const store = durableStore(namespaceFor(new CanaryState({ storage })));
  const a = await store.begin(T0);
  assert.equal(a.busy, false);
  assert.equal((await store.begin(T0)).busy, true);
  await assert.rejects(store.end('wrong-token', { sig: 'x' }), /lease lost/);
  assert.equal(storage.map.get('state'), undefined);
  await store.end(a.token, { sig: 'page:abc' });
  assert.equal((await store.begin(T0)).busy, false); // released
});

test('Durable Object: state survives a new instance (eviction / restart)', async () => {
  const storage = memoryStorage();
  const first = durableStore(namespaceFor(new CanaryState({ storage })));
  const lease = await first.begin(T0);
  await first.end(lease.token, { sig: 'page:abc', etag: '"v1"' });
  const second = durableStore(namespaceFor(new CanaryState({ storage })));
  assert.deepEqual((await second.begin(T0)).state, { sig: 'page:abc', etag: '"v1"' });
});

test('busy lease: the run is skipped without alert nor healthcheck', async () => {
  const t = setup();
  await t.store.begin(T0); // another run holds the lease
  const { result, calls } = await t.run(T0, { site: [page()] });
  assert.equal(result.outcome, 'busy');
  assert.equal(calls.length, 0);
});
