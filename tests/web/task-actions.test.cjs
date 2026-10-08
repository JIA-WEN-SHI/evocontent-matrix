const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');
const ts = require('typescript');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');

function renderTask(status) {
  const filename = resolve(__dirname, '../../apps/web/components/task-actions.tsx');
  const code = ts.transpileModule(readFileSync(filename, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX },
  }).outputText;
  const sandbox = { exports: {}, require(name) {
    if (name === '@/components/task-workflow') return { TaskWorkflow: () => null };
    if (name === '@/components/ui/button') return { Button: ({ children, ...props }) => React.createElement('button', props, children) };
    if (name.startsWith('@/lib/')) return {};
    return require(name);
  }};
  vm.runInNewContext(code, sandbox, { filename });
  return renderToStaticMarkup(React.createElement(sandbox.exports.TaskActions, {
    task: { id: 'task', status, payload_jsonb: { title: 'Draft', body: 'Body' } },
  }));
}

for (const status of ['pending_review', 'approved', 'publish_failed', 'queued']) {
  test(`task ${status} exposes drafts but no delegated publishing`, () => {
    const html = renderTask(status);
    assert.match(html, /保存草稿/);
    assert.doesNotMatch(html, /提交发布/);
    if (status === 'pending_review') assert.match(html, /审核通过/);
    if (status === 'queued') assert.match(html, /生成草稿/);
    else assert.doesNotMatch(html, /生成草稿/);
  });
}
