const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');
const ts = require('typescript');
const { File } = require('node:buffer');

function load(fetch, globals = {}) {
  const filename = resolve(__dirname, '../../apps/web/lib/api.ts');
  const compiled = ts.transpileModule(readFileSync(filename, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
  }).outputText;
  const sandbox = { exports: {}, fetch, process: { env: {} }, URL, URLSearchParams,
    AbortController, setTimeout, clearTimeout, ...globals };
  vm.runInNewContext(compiled, sandbox, { filename });
  return sandbox.exports;
}

const scope = { account_id: 'account', domain_slug: 'ai_content' };
const publication = { ...scope, published_url: 'https://www.xiaohongshu.com/explore/6a6191a4000000000103392a',
  published_at: '2026-10-07T01:30:00Z', confirmed: true };

test('publication registration and corrections keep confirmation, time and scoped identity', async () => {
  const calls = [];
  const api = load(async (url, init) => { calls.push({ url, init }); return new Response('{"id":"task","status":"published"}'); });
  const result = await api.registerManualPublication('task /?', publication);
  assert.equal(result.status, 'published');
  assert.equal(calls[0].url, 'http://127.0.0.1:8000/api/pipeline/tasks/task%20%2F%3F/manual-publication');
  assert.equal(calls[0].init.method, 'POST');
  assert.deepEqual(JSON.parse(calls[0].init.body), publication);
  const correction = { ...publication, correction_reason: 'Corrected publication time' };
  await api.registerManualPublication('task', correction, true);
  assert.equal(calls[1].init.method, 'PATCH');
  assert.deepEqual(JSON.parse(calls[1].init.body), correction);
});

test('feedback sync preserves its account/domain and returns partial-operation warnings', async () => {
  let request;
  const api = load(async (url, init) => { request = { url, init }; return new Response('{"id":"task","operation_warning":"Review not finished"}'); });
  const result = await api.syncTaskFeedback('task', 'account', 'ai_content');
  assert.equal(request.url, 'http://127.0.0.1:8000/api/pipeline/tasks/task/feedback/sync');
  assert.equal(request.init.method, 'POST');
  assert.deepEqual(JSON.parse(request.init.body), scope);
  assert.equal(result.operation_warning, 'Review not finished');
});

test('manual observations preserve unknown counters instead of inventing zeros', async () => {
  let request;
  const api = load(async (url, init) => { request = { url, init }; return new Response('{"id":"task"}'); });
  const input = { ...scope, values: { likes: 0, views: null, followers_delta: -2 },
    observed_at: '2026-10-07T02:00:00Z', provenance: 'Creator dashboard' };
  await api.saveTaskObservation('task', input);
  assert.equal(request.url, 'http://127.0.0.1:8000/api/pipeline/tasks/task/feedback/observations');
  assert.equal(request.init.method, 'POST');
  assert.deepEqual(JSON.parse(request.init.body), input);
  assert.equal('shares' in JSON.parse(request.init.body).values, false);
});

test('knowledge reconciliation can target selected captures or let the backend choose scoped captures', async () => {
  const calls = [];
  const api = load(async (url, init) => { calls.push({ url, init }); return new Response('{"status":"partial","linked":1,"items":[],"failures":[{"item_id":"bad","message":"Not linked"}]}'); });
  const result = await api.reconcileCaptureKnowledge('account /?', 'ai_content', ['item1', 'item2']);
  assert.equal(calls[0].url, 'http://127.0.0.1:8000/api/ops/accounts/account%20%2F%3F/collection/reconcile-knowledge');
  assert.equal(calls[0].init.method, 'POST');
  assert.deepEqual(JSON.parse(calls[0].init.body), { domain_slug: 'ai_content', item_ids: ['item1', 'item2'] });
  assert.equal(result.failures[0].item_id, 'bad');
  await api.reconcileCaptureKnowledge('account', 'ai_content');
  assert.deepEqual(JSON.parse(calls[1].init.body), { domain_slug: 'ai_content' });
});

test('comment retry only calls the scoped capture endpoint and preserves partial receipts', async () => {
  let request;
  const api = load(async (url, init) => { request = { url, init }; return new Response('{"status":"partial","comments_status":"partial","local_saved":true,"database_status":"saved","warnings":["Page limit"]}'); });
  const result = await api.retryCaptureComments('account /?', 'item /?');
  assert.equal(request.url, 'http://127.0.0.1:8000/api/ops/accounts/account%20%2F%3F/browser/captures/item%20%2F%3F/retry-comments');
  assert.equal(request.init.method, 'POST');
  assert.equal(result.local_saved, true);
  assert.equal(result.comments_status, 'partial');
  assert.deepEqual(Array.from(result.warnings), ['Page limit']);
});

test('image uploads send the exact File as raw binary with authentication and encoded account scope', async () => {
  let request;
  const api = load(async (url, init) => { request = { url, init }; return new Response('{"id":"task","status":"pending_review"}'); });
  const file = new File([new Uint8Array([1, 2, 3])], 'draft.png', { type: 'image/png' });
  const result = await api.uploadDraftImage('task /?', 'account /?', file);
  assert.equal(result.status, 'pending_review');
  assert.equal(request.url, 'http://127.0.0.1:8000/api/pipeline/tasks/task%20%2F%3F/images?account_id=account%20%2F%3F');
  assert.equal(request.init.method, 'POST');
  assert.equal(request.init.body, file);
  assert.equal(request.init.headers['Content-Type'], 'image/png');
  assert.equal(request.init.headers['x-user-id'], 'demo-reviewer');
  assert.equal(request.init.headers['x-user-role'], 'admin');
});

test('image upload rejects empty and oversized files before sending and accepts the 10MiB boundary', async () => {
  let count = 0;
  const api = load(async () => { count++; return new Response('{"id":"task"}'); });
  await assert.rejects(api.uploadDraftImage('task', 'account', new File([], 'empty.png')), /图片|image/i);
  await assert.rejects(api.uploadDraftImage('task', 'account', new File([new Uint8Array(10 * 1024 * 1024 + 1)], 'large.png')), /10.*MiB/);
  assert.equal(count, 0);
  await api.uploadDraftImage('task', 'account', new File([new Uint8Array(10 * 1024 * 1024)], 'boundary.png'));
  assert.equal(count, 1);
});

test('image upload with unknown MIME does not send JSON content type', async () => {
  let headers;
  const api = load(async (_url, init) => { headers = init.headers; return new Response('{}'); });
  await api.uploadDraftImage('task', 'account', new File([new Uint8Array([1])], 'draft'));
  assert.equal(headers['Content-Type'], 'application/octet-stream');
});

test('image detach is a scoped DELETE returning the updated task', async () => {
  let request;
  const api = load(async (url, init) => { request = { url, init }; return new Response('{"id":"task","payload_jsonb":{"draft_media":{"images":[]}}}'); });
  const result = await api.detachDraftImage('task /?', 'account /?', 'image /?');
  assert.equal(request.url, 'http://127.0.0.1:8000/api/pipeline/tasks/task%20%2F%3F/images/image%20%2F%3F?account_id=account%20%2F%3F');
  assert.equal(request.init.method, 'DELETE');
  assert.equal(result.payload_jsonb.draft_media.images.length, 0);
});

test('draft image reads authenticate, avoid caches and release their timer and abort listener', async () => {
  let request, listener, removed, cleared = false;
  const signal = { aborted: false, addEventListener: (_type, fn) => { listener = fn; },
    removeEventListener: (_type, fn) => { removed = fn; } };
  const api = load(async (url, init) => { request = { url, init }; return new Response(new Uint8Array([1, 2]), { headers: { 'Content-Type': 'image/webp' } }); }, {
    setTimeout: (_fn, ms) => { assert.equal(ms, 20000); return 1; },
    clearTimeout: () => { cleared = true; },
  });
  const result = await api.getDraftImage('task /?', 'account /?', 'image /?', signal);
  assert.equal(result.type, 'image/webp');
  assert.equal(result.size, 2);
  assert.equal(request.url, 'http://127.0.0.1:8000/api/pipeline/tasks/task%20%2F%3F/images/image%20%2F%3F?account_id=account%20%2F%3F');
  assert.equal(request.init.headers['x-user-role'], 'admin');
  assert.equal(request.init.cache, 'no-store');
  assert.equal(listener, removed);
  assert.ok(cleared);
});

for (const [label, response] of [
  ['non-image bodies', () => new Response('{}', { headers: { 'Content-Type': 'application/json' } })],
  ['oversized decoded bodies', () => ({ ok: true, blob: async () => new Blob([new Uint8Array(10 * 1024 * 1024 + 1)], { type: 'image/png' }) })],
  ['empty image bodies', () => new Response(null, { headers: { 'Content-Type': 'image/png' } })],
]) {
  test(`draft image reads reject ${label} and clean up after failure`, async () => {
    let cleared = false;
    const api = load(async () => response(), { setTimeout: () => 1, clearTimeout: () => { cleared = true; } });
    await assert.rejects(api.getDraftImage('task', 'account', 'image'), /图片|image/i);
    assert.ok(cleared);
  });
}

test('draft image backend errors are readable and do not retry', async () => {
  let count = 0, cleared = false;
  const api = load(async () => { count++; return new Response('{"detail":"Image does not belong to this account"}', { status: 404 }); }, {
    setTimeout: () => 1, clearTimeout: () => { cleared = true; },
  });
  await assert.rejects(api.getDraftImage('task', 'account', 'image'), /does not belong/);
  assert.equal(count, 1);
  assert.ok(cleared);
});

test('draft image cancellation propagates during body decoding and cleans up', async () => {
  const caller = new AbortController();
  let requestSignal, cleared = false;
  let beginDecode;
  const decoding = new Promise(resolve => { beginDecode = resolve; });
  const api = load(async (_url, init) => { requestSignal = init.signal; return {
    ok: true, blob: () => new Promise((_resolve, reject) => {
      init.signal.addEventListener('abort', () => reject(new Error('Aborted')), { once: true });
      beginDecode();
    }),
  }; }, { setTimeout: () => 1, clearTimeout: () => { cleared = true; } });
  const pending = api.getDraftImage('task', 'account', 'image', caller.signal);
  await decoding;
  const rejected = assert.rejects(pending, /abort/i);
  caller.abort();
  await rejected;
  assert.equal(requestSignal.aborted, true);
  assert.ok(cleared);
});

test('already cancelled draft image reads pass an aborted signal and remove the caller listener', async () => {
  let requestSignal, removed = false;
  const signal = { aborted: true, addEventListener: () => {}, removeEventListener: () => { removed = true; } };
  const api = load(async (_url, init) => { requestSignal = init.signal; throw new Error('Aborted'); });
  await assert.rejects(api.getDraftImage('task', 'account', 'image', signal), /abort/i);
  assert.equal(requestSignal.aborted, true);
  assert.ok(removed);
});

test('draft image deadline covers a stalled body even when the reader ignores cancellation', async () => {
  let expire, requestSignal, cleared = false;
  const api = load(async (_url, init) => { requestSignal = init.signal; return { ok: true, blob: () => new Promise(() => {}) }; }, {
    setTimeout: (fn, ms) => { expire = fn; assert.equal(ms, 20000); return 1; },
    clearTimeout: () => { cleared = true; },
  });
  const pending = api.getDraftImage('task', 'account', 'image');
  await Promise.resolve();
  const rejected = assert.rejects(pending, /请求超时/);
  expire();
  await rejected;
  assert.equal(requestSignal.aborted, true);
  assert.ok(cleared);
});

test('publication write timeout never retries an uncertain registration', async () => {
  let expire, count = 0;
  const api = load(async () => { count++; return new Promise(() => {}); }, {
    setTimeout: (fn, ms) => { expire = fn; assert.equal(ms, 240000); return 1; }, clearTimeout: () => {},
  });
  const pending = api.registerManualPublication('task', publication);
  const rejected = assert.rejects(pending, /结果尚未确认.*勿重复提交/);
  expire();
  await rejected;
  assert.equal(count, 1);
});
