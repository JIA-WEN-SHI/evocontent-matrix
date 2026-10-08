const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const policy = require('../../apps/chrome-bridge/page-policy.js');
const code = fs.readFileSync('apps/chrome-bridge/service-worker.js', 'utf8');

function worker() {
  const listeners = {}, calls = [];
  const event = name => ({ addListener: fn => { listeners[name] = fn; } });
  const session = { token: 'test-token', tab_id: 7 };
  let stored = session, captureCount = 0;
  const data = { kind: 'results', version: 'v', results: [], crop: { x: 0, y: 0, width: 80, height: 80, viewport_width: 100 }, capture_guard: { revision: 0, version: 'v', href: 'public' } };
  const tab = { id: 7, windowId: 2, url: 'https://www.xiaohongshu.com/explore', status: 'complete' };
  const context = { EvoPagePolicy: policy, importScripts() {}, AbortSignal, Uint8Array, OffscreenCanvas: class {
    getContext() { return { drawImage() {} }; } async convertToBlob() { return { arrayBuffer: async () => new Uint8Array([1]).buffer }; }
  }, btoa: input => Buffer.from(input, 'binary').toString('base64'),
    createImageBitmap: async () => ({ width: 100, close() {} }), setTimeout, clearTimeout,
    fetch: async url => { calls.push(url); return { ok: true, json: async () => ({}), blob: async () => ({}) }; },
    chrome: { runtime: { id: 'a'.repeat(32), getURL: () => 'chrome-extension://' + 'a'.repeat(32) + '/', onMessage: event('message') },
      windows: { onFocusChanged: event('focus') },
      storage: { session: { get: async () => ({ bridgeSession: stored }), remove: async () => { stored = null; }, set: async value => { stored = value.bridgeSession; } } },
      scripting: { executeScript: async details => details.files ? [] : [{ result: typeof details.args?.[0] === 'string' ? { ok: true, data: { ...data } } : true }] },
      tabs: { onActivated: event('activated'), onUpdated: event('updated'), onRemoved: event('removed'), get: async () => tab,
        query: async () => [tab], captureVisibleTab: async () => { captureCount++; listeners.activated?.({ tabId: 8, windowId: 2 }); listeners.activated?.({ tabId: 7, windowId: 2 }); return 'data:image/jpeg;base64,AA=='; } } } };
  vm.runInNewContext(code, context);
  context.session = session;
  return { context, data, listeners, calls, captureCount: () => captureCount, stored: () => stored };
}

test('intervening tab activation discards captured pixels even after A-B-A switching', async () => {
  const w = worker();
  const result = await vm.runInNewContext('execute(session, {operation: "observe", arguments: {}})', w.context);
  assert.equal(result.ok, false);
  assert.equal(result.paused, true);
  assert.ok(!result.data?.screenshot);
});

test('missing public crop pauses instead of accepting an imageless observation', async () => {
  const w = worker();
  delete w.data.crop;
  const result = await vm.runInNewContext('execute(session, {operation: "observe", arguments: {}})', w.context);
  assert.equal(result.paused, true);
  assert.equal(w.captureCount(), 0);
});

test('closure or private navigation revokes local session and server pairing', async () => {
  for (const event of ['removed', 'updated']) {
    const w = worker();
    assert.equal(typeof w.listeners[event], 'function');
    await w.listeners[event](7, event === 'updated' ? { url: 'https://www.xiaohongshu.com/message' } : {});
    assert.equal(w.stored(), null);
    assert.ok(w.calls.some(url => url.endsWith('/extension/disconnect')));
  }
});

test('hung page execution has a bounded response and never replays the operation', { timeout: 250 }, async () => {
  const w = worker();
  let executions = 0;
  w.context.setTimeout = (fn, ms) => setTimeout(fn, Math.min(ms, 15));
  w.context.clearTimeout = clearTimeout;
  w.context.chrome.scripting.executeScript = async details => {
    if (details.files) return [];
    executions++;
    return new Promise(() => {});
  };
  const result = await vm.runInNewContext('execute(session, {operation: "open_note", arguments: {}}).catch(e => ({ok:false,paused:true,message:e.message}))', w.context);
  assert.equal(result.paused, true);
  assert.match(result.message, /超时/);
  assert.equal(executions, 1);
});

test('one fresh observation recovers a changing public crop without replaying read', async () => {
  const w = worker(), operations = [];
  let checks = 0;
  w.data.kind = 'note';
  w.data.note = { source_url: 'https://www.xiaohongshu.com/explore/6a089a9d0000000038021ad6', title: 'AI 方法', text: '实际公开正文内容' };
  w.context.chrome.scripting.executeScript = async details => {
    if (details.files) return [];
    if (typeof details.args[0] === 'string') { operations.push(details.args[0]); return [{ result: { ok: true, data: { ...w.data } } }]; }
    return [{ result: ++checks > 1 }];
  };
  w.context.chrome.tabs.captureVisibleTab = async () => 'data:image/jpeg;base64,AA==';
  const result = await vm.runInNewContext('execute(session, {operation: "read_note", arguments: {version:"v"}})', w.context);
  assert.equal(result.ok, true);
  assert.ok(result.data.screenshot);
  assert.deepEqual(operations, ['read_note', 'observe']);
});

test('tab switching is reported distinctly and never retried as a render change', async () => {
  const w = worker();
  const result = await vm.runInNewContext('execute(session, {operation: "observe", arguments: {}})', w.context);
  assert.match(result.message, /截图期间标签页发生切换/);
  assert.equal(w.captureCount(), 1);
});

test('background uses paired tab without capturing or activating another active tab', async () => {
  const w = worker();
  w.context.session.mode = 'background_text';
  w.context.chrome.tabs.query = async () => [{ id: 99 }];
  const result = await vm.runInNewContext('execute(session, {operation:"observe",arguments:{},mode:"background_text",protocol_version:2})', w.context);
  assert.equal(result.ok, true);
  assert.equal(w.captureCount(), 0);
  assert.equal(result.data.screenshot, undefined);
  assert.equal(result.data.crop, undefined);
});

test('background frozen tab pauses rather than forcing activation', async () => {
  const w = worker();
  w.context.session.mode = 'background_text';
  w.context.chrome.tabs.get = async () => ({ id: 7, windowId: 2, frozen: true, url: 'https://www.xiaohongshu.com/explore' });
  const result = await vm.runInNewContext('execute(session, {operation:"observe",arguments:{},mode:"background_text"})', w.context);
  assert.equal(result.paused, true);
  assert.match(result.message, /冻结/);
  assert.equal(w.captureCount(), 0);
});

test('mode cannot be widened by a command', async () => {
  const w = worker();
  const result = await vm.runInNewContext('execute(session, {operation:"observe",arguments:{},mode:"background_text"})', w.context);
  assert.equal(result.ok, false);
  assert.equal(w.captureCount(), 0);
});

test('freeze during background read discards the result', async () => {
  const w = worker();
  w.context.session.mode = 'background_text';
  const original = w.context.chrome.scripting.executeScript;
  w.context.chrome.scripting.executeScript = async details => {
    const result = await original(details);
    if (!details.files) w.context.chrome.tabs.get = async () => ({id:7,url:'https://www.xiaohongshu.com/explore',frozen:true});
    return result;
  };
  const result = await vm.runInNewContext('execute(session,{operation:"read_note",arguments:{}})',w.context);
  assert.equal(result.ok,false);
  assert.equal(result.paused,true);
});

test('freeze notification pauses the server while no page command is pending', async () => {
  const w = worker();
  w.context.session.mode = 'background_text';
  await w.listeners.updated(7,{frozen:true});
  assert.ok(w.calls.some(url=>url.endsWith('/extension/pause')));
  assert.notEqual(w.stored(),null);
});
