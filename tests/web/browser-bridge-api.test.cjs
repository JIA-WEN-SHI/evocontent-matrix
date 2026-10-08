const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');
const { test } = require('node:test');

function load(fetch) {
  const file = path.resolve(__dirname, '../../apps/web/lib/api.ts');
  const code = ts.transpileModule(fs.readFileSync(file, 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } }).outputText;
  const sandbox = { exports: {}, fetch, process: { env: {} }, URL, URLSearchParams, AbortController, setTimeout, clearTimeout };
  vm.runInNewContext(code, sandbox);
  return sandbox.exports;
}

test('Browser controls keep account and domain scope and do not replay failed writes', async () => {
  const calls = [];
  const api = load(async (url, init) => { calls.push([url, init]); return new Response(JSON.stringify({ status: 'queued', id: 'job' })); });
  const result = await api.startBrowserCollection('account', 'ai_content');
  assert.equal(result.status, 'queued');
  assert.equal(calls[0][0], 'http://127.0.0.1:8000/api/browser-bridge/accounts/account/jobs');
  assert.deepEqual(JSON.parse(calls[0][1].body), { domain_slug: 'ai_content', source_kind: 'hotspot' });
  await api.createBrowserPairing('account', 'ai_content');
  await api.controlBrowserJob('account', 'job', 'stop');
  await api.disconnectBrowser('account');
  assert.equal(calls[2][0].endsWith('/accounts/account/jobs/job/stop'), true);
  let count = 0;
  const failed = load(async () => { count++; return new Response(JSON.stringify({ detail: '已有采集任务' }), { status: 409 }); });
  await assert.rejects(failed.startBrowserCollection('account', 'ai_content'), /已有采集任务/);
  assert.equal(count, 1);
});

test('Browser state distinguishes active, paused, unavailable and partial jobs', () => {
  const file = path.resolve(__dirname, '../../apps/web/lib/browser-bridge.ts');
  const code = ts.transpileModule(fs.readFileSync(file, 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText;
  const sandbox = { exports: {} }; vm.runInNewContext(code, sandbox);
  const model = sandbox.exports;
  assert.equal(model.isBrowserJobActive({ status: 'awaiting_user' }), true);
  assert.equal(model.isBrowserJobActive({ status: 'model_unavailable' }), false);
  assert.notEqual(model.browserJobLabel('partial'), model.browserJobLabel('completed'));
  assert.notEqual(model.browserJobLabel('model_unavailable'), model.browserJobLabel('running'));
});

test('pairing binds background mode and media reads use account auth headers', async () => {
  const calls=[];
  const api=load(async (url,init)=>{calls.push([url,init]);return new Response(url.includes('/media/')?new Uint8Array([1,2]):'{}',{headers:{'Content-Type':url.includes('/media/')?'image/png':'application/json'}});});
  await api.createBrowserPairing('a','ai_content','background_text');
  assert.equal(JSON.parse(calls[0][1].body).mode,'background_text');
  await api.getBrowserCapture('a','i');
  assert.equal(calls[1][0].endsWith('/accounts/a/items/i/capture'),true);
  const blob=await api.getBrowserMedia('a','i','m');
  assert.equal(blob.size,2);
  assert.equal(calls[2][1].headers['x-user-role'],'admin');
  assert.equal(calls[2][0].endsWith('/accounts/a/items/i/media/m'),true);
});
