const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');
const ts = require('typescript');
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');

function landingOperation(name, bindings) {
  const filename = resolve(__dirname, '../../apps/web/app/page.tsx');
  const source = ts.createSourceFile(filename, readFileSync(filename, 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let operation;
  function visit(node) {
    if (ts.isFunctionDeclaration(node) && node.name?.text === name) operation = node;
    if (ts.isVariableDeclaration(node) && node.name.getText(source) === name && ts.isCallExpression(node.initializer)) {
      operation = node.initializer.arguments[0];
    }
    ts.forEachChild(node, visit);
  }
  visit(source);
  assert.ok(operation, `missing operation ${name}`);
  const code = ts.transpileModule(`exports.operation = ${operation.getText(source)};`, {
    compilerOptions: { module: ts.ModuleKind.CommonJS },
  }).outputText;
  const sandbox = { exports: {}, ...bindings };
  vm.runInNewContext(code, sandbox, { filename });
  return sandbox.exports.operation;
}

test('account source fallback never includes another account or public inputs', () => {
  const result = landingOperation('accountCards', {
    accountId: 'a', overviewAccount: null,
    overviewGlobal: { reference_items: [{ id: 'foreign', account_id: 'b' }, { id: 'public', account_id: null }, { id: 'own', account_id: 'a' }] },
  })();
  assert.equal(result.length, 1);
  assert.equal(result[0].id, 'own');
});

test('a stale selected task cannot mount an editor for a different account', () => {
  const result = landingOperation('selectedTask', {
    selectedTaskId: 'old', accountId: 'b', demoMode: false,
    pendingTasks: [{ id: 'old', account_id: 'a' }],
  })();
  assert.equal(result, null);
});

test('failed task loading invalidates old tasks instead of retaining actionable drafts', async () => {
  let tasks = [{ id: 'old', account_id: 'a' }];
  const noop = () => {};
  const empty = async () => ({});
  const bindings = {
    shouldUseDemoFallback: () => false, DOMAIN_SLUG: 'ai_content',
    loadedAccountId: { current: 'a' }, selectTaskForEditing: noop, setSelectedSourceId: noop,
    setPendingTasks: value => { tasks = typeof value === 'function' ? value(tasks) : value; },
    getRuntimeContext: async () => ({ publish_account: { account_id: 'b' } }),
    getPipelineTasks: async () => { throw new Error('unavailable'); },
    toUserFacingError: err => err.message,
  };
  for (const name of ['setLoading', 'setError', 'setRuntime', 'setOverviewGlobal', 'setCollectionMode', 'setOverviewAccount', 'setPublishSummary', 'setPendingStrategy', 'setSopCurrent', 'setSopSnapshots', 'setCoachBrief', 'setCoachActions']) bindings[name] = noop;
  for (const name of ['getOpsOverview', 'getCoachBrief', 'getAccountSopCurrent', 'getAccountSopSnapshots', 'getChannelAccountStrategy', 'getPublishFeedbackSummary', 'getPendingStrategyItems']) bindings[name] = empty;
  await landingOperation('refresh', bindings)();
  assert.equal(tasks.length, 0);
});

test('approving an unselected demo draft never replaces the current editor contents', () => {
  let title = 'edited A';
  let body = 'body A';
  let tasks = [{ id: 'a', payload_jsonb: {} }, { id: 'b', payload_jsonb: { title: 'B', body: 'body B' } }];
  landingOperation('approveDemoDraft', {
    demoMode: true, sending: false, selectedTaskId: 'a', pendingTasks: tasks,
    draftTitle: title, draftBody: body,
    taskTags: () => [], ensureBodyHasTags: value => value,
    setPendingTasks: update => { tasks = update(tasks); }, setMessages: () => {},
    setDraftTitle: value => { title = value; }, setDraftBody: value => { body = value; },
  })('b');
  assert.equal(tasks[1].status, 'approved');
  assert.equal(title, 'edited A');
  assert.equal(body, 'body A');
});

function renderLanding(clockStep = 0) {
  const filename = resolve(__dirname, '../../apps/web/app/page.tsx');
  const code = ts.transpileModule(readFileSync(filename, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX },
  }).outputText;
  let clock = 0;
  class Clock extends Date { static now() { clock += clockStep; return clock; } }
  const sandbox = { exports: {}, Date: Clock, process: { env: {} }, require(name) {
    if (name === 'next/link') return { default: ({ children, ...props }) => React.createElement('a', props, children) };
    if (name === '@/lib/utils') return { cn: (...values) => values.filter(Boolean).join(' ') };
    if (name === '@/lib/api') return { DEFAULT_DOMAIN_SLUG: 'ai_content' };
    if (name.startsWith('@/')) return {};
    if (name === 'react/jsx-runtime') {
      const runtime = require(name);
      const wrap = (render) => (type, props, key) => {
        if (type === 'style') {
          const { jsx, global, ...rest } = props;
          return render(type, rest, key);
        }
        return render(type, props, key);
      };
      return { ...runtime, jsx: wrap(runtime.jsx), jsxs: wrap(runtime.jsxs) };
    }
    return require(name);
  }};
  vm.runInNewContext(code, sandbox, { filename });
  return renderToStaticMarkup(React.createElement(sandbox.exports.default));
}

test('all four workspace areas remain expanded after the idle deadline', () => {
  const html = renderLanding(60000);
  for (const heading of ['今日公共数据', '本账号数据', '今日草稿任务', '今日沉淀SOP']) {
    assert.match(html, new RegExp(`<h2[^>]*>${heading}</h2>`));
  }
  assert.doesNotMatch(html, /writing-mode:vertical-rl/);
});

test('the existing animated decorations remain available', () => {
  const html = renderLanding();
  for (const className of ['character-core', 'character-wave', 'character-ring', 'character-ring-2']) {
    assert.ok(html.includes(className));
  }
  assert.match(html, /prefers-reduced-motion/);
});

test('workspace view switches use keyboard-accessible pressed buttons', () => {
  const html = renderLanding();
  assert.match(html, /role="group" aria-label="内容视图"/);
  assert.match(html, /aria-pressed="true"[^>]*>公共素材<\/button>/);
  assert.doesNotMatch(html, /role="tablist"/);
});

test('capture details can start expanded without changing the default collapsed view', () => {
  const filename = resolve(__dirname, '../../apps/web/components/accounts/BrowserCaptureDetails.tsx');
  const code = ts.transpileModule(readFileSync(filename, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX },
  }).outputText;
  const sandbox = { exports: {}, require(name) {
    if (name === '@/lib/api') return {};
    return require(name);
  }};
  vm.runInNewContext(code, sandbox, { filename });
  const render = (expanded) => renderToStaticMarkup(React.createElement(sandbox.exports.BrowserCaptureDetails, {
    accountId: 'account', itemId: 'item', expanded,
  }));
  assert.match(render(false), /查看采集资料/);
  assert.doesNotMatch(render(false), /读取资料中/);
  assert.match(render(true), /读取资料中/);
  assert.doesNotMatch(render(true), /查看采集资料/);
});

test('collection is a visible main action rather than hidden inside a data pane', () => {
  const html = renderLanding();
  const header = html.match(/<header[\s\S]*?<\/header>/)?.[0] || '';
  assert.match(header, /开始采集/);
  assert.equal((html.match(/href="\/legacy-flow"/g) || []).length, 1);
});

test('side regions use flexible desktop columns and viewport-bounded source lists', () => {
  const html = renderLanding();
  assert.equal((html.match(/class="workspace-side /g) || []).length, 2);
  assert.match(html, /@media \(min-width: 1200px\)/);
  assert.ok(html.includes('minmax(240px, 1fr) minmax(460px, 2.2fr) minmax(280px, 1.1fr)'));
  assert.ok(html.includes('max-height: clamp(220px, calc(50dvh - 200px), 420px)'));
  assert.ok(html.includes('scrollbar-gutter: stable'));
  assert.ok(html.includes('grid-auto-rows: max-content'));
  assert.ok(html.includes('align-content: start'));
});
