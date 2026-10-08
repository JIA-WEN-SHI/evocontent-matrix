const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');
const ts = require('typescript');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');

function render(status, metrics = {}, payload = {}) {
  const filename = resolve(__dirname, '../../apps/web/components/task-workflow.tsx');
  const code = ts.transpileModule(readFileSync(filename, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX },
  }).outputText;
  const sandbox = { exports: {}, require(name) {
    if (name.startsWith('@/lib/')) return {};
    return require(name);
  }};
  vm.runInNewContext(code, sandbox, { filename });
  return renderToStaticMarkup(React.createElement(sandbox.exports.TaskWorkflow, {
    task: { id: 'task', account_id: 'account', status, published_at: status === 'published' ? '2026-10-06T12:00:00Z' : null,
      payload_jsonb: payload, publish_jsonb: {}, metrics_jsonb: metrics }, onUpdated() {},
  }));
}

test('approved draft requires explicit ownership confirmation and no delegated publishing', () => {
  const html = render('approved');
  assert.match(html, /登记手动发布/);
  assert.match(html, /已自行发布/);
  assert.match(html, /发布链接/);
  assert.doesNotMatch(html, /自动发布|代发布/);
});
test('observer draft shows saved writing rules and project source', () => {
  const html = render('pending_review', {}, { analysis_jsonb: { content_branch: {
    id: 'project_observer', name: 'AI 项目观察·共情短文', version: '1.0', rules: '共情是核心目的',
    project_reference: { source_url: 'https://github.com/lllyasviel/Fooocus', checked_at: '2026-10-07T08:00:00Z' },
  }}});
  assert.match(html, /AI 项目观察·共情短文/);
  assert.match(html, /共情是核心目的/);
  assert.match(html, /https:\/\/github.com\/lllyasviel\/Fooocus/);
  assert.match(html, /设想画面/);
});
test('observer post detail does not imply a default CTA', () => {
  const source = readFileSync(resolve(__dirname, '../../apps/web/app/tasks/[id]/page.tsx'), 'utf8');
  assert.match(source, /isProjectObserver \? "无"/);
  assert.match(source, /grid gap-6 xl:grid-cols-\[1\.15fr_0\.85fr\][\s\S]*?min-w-0 space-y-6/);
});
test('unreviewed draft cannot register publication', () => {
  const html = render('pending_review');
  assert.doesNotMatch(html, /登记手动发布/);
  assert.match(html, /草稿配图/);
});
test('published metrics retain zero and show unavailable counters as unknown', () => {
  const html = render('published', { post_metrics: { likes: 0, collects: 3 } });
  assert.match(html, /点赞[^<]*<[^>]*>0</);
  assert.match(html, /曝光[^<]*<[^>]*>未知</);
  assert.match(html, /补录观测/);
  assert.match(html, /同步真实反馈/);
});
