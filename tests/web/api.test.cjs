const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { resolve } = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");
const ts = require("typescript");

function apiWithFetch(fetch, globals = {}) {
  const filename = resolve(__dirname, "../../apps/web/lib/api.ts");
  const compiled = ts.transpileModule(readFileSync(filename, "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
  }).outputText;
  const sandbox = { exports: {}, fetch, process: { env: {} }, URL, URLSearchParams,
    AbortController, setTimeout, clearTimeout, ...globals };
  vm.runInNewContext(compiled, sandbox, { filename });
  return sandbox.exports;
}

test('collection receipts show partial success without hiding local saves or failed comments', () => {
  const api = apiWithFetch(() => { throw new Error('No request expected'); });
  assert.equal(typeof api.collectionResultNotice, 'function');
  const text = api.collectionResultNotice({status:'partial',collected:3,inserted:2,message:'本地保存 3 篇。评论读取失败。',warnings:['数据库保存未确认']});
  assert.match(text, /本地保存 3 篇/);
  assert.match(text, /评论读取失败/);
  assert.match(text, /数据库保存未确认/);
  assert.throws(() => api.collectionResultNotice({status:'error'}), /采集未完成/);
});

test("structured backend errors show their message rather than a raw JSON envelope", async () => {
  const api = apiWithFetch(async () => new Response(JSON.stringify({
    status: "error", error: { message: "Please refresh and try again", code: "http_409" },
  }), { status: 409 }));
  await assert.rejects(api.getPipelineTask("task"), { message: "Please refresh and try again" });
});

test("draft edits use PATCH and preserve Unicode", async () => {
  let request;
  const api = apiWithFetch(async (url, init) => {
    request = { url, init };
    return new Response(JSON.stringify({ id: "task", status: "pending_review" }));
  });
  const input = { title: "中文标题", body: "中文问题?" };
  await api.updatePipelineTask("task", input);
  assert.equal(request.init.method, "PATCH");
  assert.deepEqual(JSON.parse(request.init.body), input);
  assert.equal(request.url, "http://127.0.0.1:8000/api/pipeline/tasks/task");
});

test("confirmation text is correctly encoded", async () => {
  let payload;
  const api = apiWithFetch(async (_url, init) => {
    payload = JSON.parse(init.body);
    return new Response("{}");
  });
  await api.confirmTaskPlan({ task_plan: {} });
  assert.equal(payload.confirm_text, "确认执行");
});

test("configured domain is used by default without changing explicit historical scope", async () => {
  const urls = [];
  const api = apiWithFetch(async (url) => {
    urls.push(url);
    return new Response("{}");
  }, { process: { env: { NEXT_PUBLIC_DEFAULT_DOMAIN_SLUG: "ai_content" } } });
  await api.getDomain();
  await api.getRuntimeContext();
  await api.getDomain("japan_immigration");
  assert.match(urls[0], /\/domains\/ai_content$/);
  assert.match(urls[1], /domain_slug=ai_content/);
  assert.match(urls[2], /\/domains\/japan_immigration$/);
});

test("stalled reads abort with a readable timeout and always clear their timer", async () => {
  let expire;
  let cleared = false;
  let calls = 0;
  const api = apiWithFetch(async (_url, init) => {
    calls += 1;
    return new Promise((_resolve, reject) => {
      init.signal?.addEventListener("abort", () => reject(new Error("aborted")), { once: true });
    });
  }, {
    setTimeout: (callback, ms) => { expire = callback; assert.equal(ms, 20000); return 1; },
    clearTimeout: () => { cleared = true; },
  });
  const pending = api.getPipelineTask("task");
  assert.equal(typeof expire, "function", "Reads must have a deadline");
  const rejected = assert.rejects(pending, /请求超时/);
  expire();
  await rejected;
  assert.equal(calls, 1);
  assert.ok(cleared);
});

test("write timeout reports uncertain outcome without replaying the write", async () => {
  let expire;
  let calls = 0;
  const api = apiWithFetch(async (_url, init) => {
    calls += 1;
    return new Promise((_resolve, reject) => {
      init.signal?.addEventListener("abort", () => reject(new Error("aborted")), { once: true });
    });
  }, {
    setTimeout: (callback, ms) => { expire = callback; assert.equal(ms, 240000); return 1; },
    clearTimeout: () => {},
  });
  const pending = api.runPipelineTask("task");
  assert.equal(typeof expire, "function", "Writes must have a deadline");
  const rejected = assert.rejects(pending, /结果尚未确认.*勿重复提交/);
  expire();
  await rejected;
  assert.equal(calls, 1);
});

test("response decoding stays inside the deadline and successful requests clear it", async () => {
  let cleared = false;
  const api = apiWithFetch(async () => ({ ok: true, json: async () => {
    assert.equal(cleared, false, "Timer must cover response body reading");
    return { id: "task" };
  } }), { setTimeout: () => 1, clearTimeout: () => { cleared = true; } });
  assert.equal((await api.getPipelineTask("task")).id, "task");
  assert.ok(cleared);
});
