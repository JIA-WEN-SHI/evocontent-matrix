const assert = require("node:assert/strict");
const { readFileSync, existsSync } = require("node:fs");
const { resolve, dirname, extname } = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");
const ts = require("typescript");
const { webcrypto } = require("node:crypto");

const webRoot = resolve(__dirname, "../../apps/web");
function harness() {
  const requests = [];
  const cache = new Map();
  let rejectNext = false;
  function load(path) {
    let filename = path;
    if (!extname(filename)) filename = [".ts", ".tsx"].map(ext => path + ext).find(existsSync);
    if (cache.has(filename)) return cache.get(filename);
    const exports = {};
    cache.set(filename, exports);
    const compiled = ts.transpileModule(readFileSync(filename, "utf8"), {
      compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true },
    }).outputText;
    vm.runInNewContext(compiled, {
      exports, process: { env: {} }, URL, URLSearchParams, TextEncoder, crypto: webcrypto,
      setTimeout, clearTimeout, AbortController,
      require(name) {
        if (name.startsWith("@/")) return load(resolve(webRoot, name.slice(2)));
        if (name.startsWith(".")) return load(resolve(dirname(filename), name));
        return require(name);
      },
      fetch: async (url, init) => {
        const body = init?.body ? JSON.parse(init.body) : undefined;
        requests.push({ url, method: init?.method || "GET", body });
        if (rejectNext) { rejectNext = false; return new Response('{"detail":"模拟失败"}', { status: 422 }); }
        return new Response(JSON.stringify({ id: "record-12345678", ...body, deleted_at: body?.deleted ? "2026-09-18" : null }));
      },
    }, { filename });
    return exports;
  }
  return { model: load(resolve(webRoot, "components/knowledge/workspace-model.ts")), load, requests, failNext() { rejectNext = true; } };
}
const scope = { domain_slug: "japan_immigration", account_id: "account-12345678" };
const samples = {
  case: { title: "案例标题", content: "案例正文", author: "作者", url: "https://example.com/post" },
  asset: { content: "素材正文", summary: "素材摘要", is_verified: "true", source: "官方网站" },
  user_need: { original_text: "用户问题", real_problem: "真实需求", emotion: "焦虑" },
  topic: { title: "选题标题", status: "archived", target_user: "目标用户" },
  review: { summary_text: "复盘结论", topic_id: "topic-12345678", improvement: "下一步改进" },
};
const backendNames = { case: "Case", asset: "Asset", user_need: "UserNeed", topic: "Topic", review: "Review" };
const routes = { case: "cases", asset: "assets", user_need: "user-needs", topic: "topics", review: "reviews" };
const models = readFileSync(resolve(__dirname, "../../services/api/app/models.py"), "utf8");
function allowedFields(kind, operation) {
  const body = models.split(`class Kb${backendNames[kind]}${operation}Request(BaseModel):`)[1].split(/\r?\nclass /)[0];
  return new Set([...body.matchAll(/^    (\w+):/gm)].map(match => match[1]));
}

for (const kind of Object.keys(samples)) {
  test(`${kind}: create/edit/soft-delete use the existing backend contract and scope`, async () => {
    const { model, requests } = harness();
    const draft = { ...model.makeDraft(kind), ...samples[kind] };
    const created = await model.saveRow(kind, scope, draft);
    const create = requests[0];
    assert.equal(create.method, "POST");
    assert.equal(new URL(create.url).pathname, `/api/kb/${routes[kind]}`);
    assert.equal(create.body.account_id, scope.account_id);
    assert.equal(create.body.domain_slug, scope.domain_slug);
    for (const key of Object.keys(create.body)) assert.ok(allowedFields(kind, "Create").has(key), `unsupported create field: ${key}`);
    for (const [key, value] of Object.entries(samples[kind])) assert.equal(create.body[key], key === "is_verified" ? true : value);
    const existing = { ...created, source_ref: "preserve-source", source_type: "octopus", pipeline_task_id: "keep-task" };
    await model.saveRow(kind, scope, draft, existing);
    assert.equal(requests[1].method, "PATCH");
    assert.ok(requests[1].url.endsWith(`/api/kb/${routes[kind]}/${created.id}`));
    assert.ok(!("account_id" in requests[1].body));
    assert.ok(!("source_ref" in requests[1].body));
    assert.ok(!("pipeline_task_id" in requests[1].body));
    for (const key of Object.keys(requests[1].body)) assert.ok(allowedFields(kind, "Patch").has(key), `unsupported patch field: ${key}`);
    await model.patchRow[kind](created.id, { deleted: true });
    assert.deepEqual(requests[2].body, { deleted: true });
    assert.ok(allowedFields(kind, "Patch").has("deleted"));
  });
}

test("all required form fields reject whitespace before sending requests", async () => {
  const { model, requests } = harness();
  for (const kind of model.kinds) {
    const draft = model.makeDraft(kind);
    for (const field of model.fields[kind]) if (field.required) draft[field.key] = " \n ";
    await assert.rejects(model.saveRow(kind, scope, draft), /请填写/);
  }
  assert.equal(requests.length, 0);
});

test("metrics preserve unknown keys, clear only blank exposed keys, and reject invalid counts", () => {
  const { model } = harness();
  const row = { ...samples.review, metrics: { custom_rate: 0.52, likes: 0, comments: 23 } };
  const draft = model.makeDraft("review", row);
  assert.equal(draft.metric_likes, "0");
  draft.metric_comments = "";
  draft.metric_views = "100";
  const result = model.makePayload("review", draft, row);
  assert.equal(result.metrics.custom_rate, 0.52);
  assert.equal(result.metrics.likes, 0);
  assert.equal(result.metrics.views, 100);
  assert.ok(!("comments" in result.metrics));
  for (const value of ["-1", "0.5", "NaN", "9007199254740992"]) {
    assert.throws(() => model.makePayload("review", { ...draft, metric_likes: value }, row), /非负整数/);
  }
});

test("form contracts reject long titles, unsafe URLs, and invalid topic status", () => {
  const { model } = harness();
  assert.throws(() => model.makePayload("case", { ...model.makeDraft("case"), title: "长".repeat(501) }), /不能超过/);
  assert.throws(() => model.makePayload("case", { ...model.makeDraft("case"), title: "标题", url: "javascript:alert(1)" }), /HTTP/);
  for (const status of ["publishedx", "toString"]) assert.throws(() => model.makePayload("topic", { ...model.makeDraft("topic"), title: "标题", status }), /有效/);
});

test("API failure propagates; a retry sends one fresh request and does not claim success", async () => {
  const { model, requests, failNext } = harness();
  const draft = { ...model.makeDraft("case"), ...samples.case };
  failNext();
  await assert.rejects(model.saveRow("case", scope, draft), /模拟失败/);
  await model.saveRow("case", scope, draft);
  assert.equal(requests.length, 2);
});

test("Octopus accepts arrays/envelopes/BOM and preserves supported metrics, tags, and raw data", () => {
  const { model } = harness();
  const items = [{ title: "案例", content: "正文", tags: ["留学"], metrics: { likes: 4 }, raw: { original: "原始数据" } }];
  for (const text of [JSON.stringify(items), JSON.stringify({ items }), "\ufeff" + JSON.stringify(items)]) {
    assert.equal(JSON.stringify(model.parseOctopusJson(text)), JSON.stringify(items));
  }
  assert.equal(model.parseOctopusJson(JSON.stringify(Array.from({ length: 500 }, () => ({ title: "样本" })))).length, 500);
});

test("Octopus rejects invalid shape, blank content, overflow, and malformed fields before import", () => {
  const { model } = harness();
  const invalid = ["not json", "null", "{}", "[]", "[null]", '[{"title":" "}]', '[{"title":1}]',
    JSON.stringify([{ title: "x".repeat(501) }]), JSON.stringify(Array.from({ length: 501 }, () => ({ title: "样本" }))),
    '[{"title":"案例","metrics":[]}]', '[{"title":"案例","raw":null}]', '[{"title":"案例","tags":[1]}]',
    '[{"title":"案例","tags":[""]}]', JSON.stringify([{ title: "案例", tags: Array(31).fill("标签") }]),
    '[{"标题":"中文导出字段"}]', '[{"title":"案例","unsupported":"value"}]'];
  for (const text of invalid) assert.throws(() => model.parseOctopusJson(text), undefined, text.slice(0, 80));
});

test("source-less import identities are stable, content-specific, account-specific, and preserve explicit sources", async () => {
  const { model } = harness();
  const first = await model.prepareImportItems([{ title: "第一批", content: "样本正文" }], scope);
  const repeat = await model.prepareImportItems([{ content: "样本正文", title: "第一批" }], scope);
  const second = await model.prepareImportItems([{ title: "第二批", content: "样本正文" }], scope);
  const otherAccount = await model.prepareImportItems([{ title: "第一批", content: "样本正文" }], { ...scope, account_id: "other-account" });
  assert.equal(first[0].source_ref, repeat[0].source_ref);
  assert.notEqual(first[0].source_ref, second[0].source_ref);
  assert.notEqual(first[0].source_ref, otherAccount[0].source_ref);
  const explicit = [{ title: "案例", source_ref: "provided" }, { title: "案例", url: "https://example.com" }];
  assert.equal(JSON.stringify(await model.prepareImportItems(explicit, scope)), JSON.stringify(explicit));
});

test("record summaries are human content, never database IDs", () => {
  const { model } = harness();
  for (const kind of model.kinds) {
    const row = { ...samples[kind], id: "secret-row-id", domain_id: "secret-domain-id" };
    assert.ok(model.rowTitle(row));
    assert.ok(!model.rowTitle(row).includes("secret"));
  }
});

test("workspace exports an embeddable client component with the default domain", () => {
  const { load } = harness();
  const { KnowledgeWorkspace } = load(resolve(webRoot, "components/KnowledgeWorkspace.tsx"));
  const element = KnowledgeWorkspace({ accountId: "account-one" });
  assert.equal(element.props.scope.account_id, "account-one");
  assert.equal(element.props.scope.domain_slug, "japan_immigration");
  const changed = KnowledgeWorkspace({ accountId: "account-two" });
  assert.notEqual(element.key, changed.key, "account switch must remount and reset pending UI state");
  const otherDomain = KnowledgeWorkspace({ accountId: "account-one", domainSlug: "other-domain" });
  assert.notEqual(element.key, otherDomain.key);
});
