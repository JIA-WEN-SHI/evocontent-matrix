const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');
const ts = require('typescript');

const home = 'apps/web/app/page.tsx';
const capture = 'apps/web/components/accounts/BrowserCaptureDetails.tsx';
const detail = 'apps/web/app/tasks/[id]/page.tsx';

function operation(file, name, bindings = {}) {
  const filename = resolve(__dirname, '../..', file);
  const source = ts.createSourceFile(filename, readFileSync(filename, 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let node;
  function visit(current) {
    if (ts.isFunctionDeclaration(current) && current.name?.text === name) node = current;
    if (ts.isVariableDeclaration(current) && current.name.getText(source) === name && ts.isCallExpression(current.initializer)) node = current.initializer.arguments[0];
    ts.forEachChild(current, visit);
  }
  visit(source);
  assert.ok(node, `missing operation ${name}`);
  const code = ts.transpileModule(`exports.operation = ${node.getText(source)};`, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const sandbox = { exports: {}, Error, ...bindings };
  vm.runInNewContext(code, sandbox, { filename });
  return sandbox.exports.operation;
}

function metricHelpers() {
  return { readRecord: operation(detail, 'readRecord'), readNumber: operation(detail, 'readNumber') };
}

test('task result cards read nested real metrics and preserve zero without inventing missing counts', () => {
  const result = operation(detail, 'metricCards', metricHelpers())({ post_metrics: { likes: 0, collects: 8, comments_count: null } }, null);
  assert.equal(result.find(item => item.label === '点赞').value, 0);
  assert.equal(result.find(item => item.label === '收藏').value, 8);
  assert.equal(result.find(item => item.label === '评论').value, '未知');
  assert.equal(result.find(item => item.label === '分享').value, '未知');
});

test('a partial current observation never fills its absent metrics from an older observation', () => {
  const result = operation(detail, 'metricCards', metricHelpers())({ post_metrics: { likes: 10, shares: 100 } }, { feedback_analysis: { current_metrics: { likes: 0 } } });
  assert.equal(result.find(item => item.label === '点赞').value, 0);
  assert.equal(result.find(item => item.label === '分享').value, '未知');
});

test('invalid, negative and fractional engagement counters stay unknown', () => {
  const result = operation(detail, 'metricCards', metricHelpers())({ likes: false, collects: -2, comments_count: 1.5, shares: 'bad' }, null);
  assert.ok(result.every(item => item.value === '未知'));
});

test('HOLD and insufficient evidence suggestions cannot be applied even with generic proposals', () => {
  const allowed = operation(home, 'canApplyStrategy');
  const proposal = { target: 'collection_plan' };
  assert.equal(allowed({ title: '标题优化', content: '{}', proposed_changes: [proposal] }), true);
  for (const item of [
    { title: 'HOLD', content: '{}' },
    { title: '建议', content: '{"executable":false}' },
    { title: '建议', content: '{"has_own_account_evidence":false}' },
    { title: '建议', content: '证据不足，请等待真实反馈' },
    { title: '建议', content: '{}', tags: ['competitor_only'] },
  ]) assert.equal(allowed({ ...item, proposed_changes: [proposal] }), false);
});

test('a same-tick double click sends only one live operation and refreshes once', async () => {
  let calls = 0, refreshed = 0, release;
  const gate = new Promise(resolve => { release = resolve; });
  const run = operation(home, 'runLiveAction', {
    actionBusy: '', sending: false, loading: false, actionLock: { current: false },
    setActionBusy() {}, setError() {}, setNotice() {},
    refresh: async () => { refreshed++; }, toUserFacingError: err => err.message,
  });
  const action = async () => { calls++; await gate; };
  const first = run('generate', action);
  const second = run('generate', action);
  release();
  await Promise.all([first, second]);
  assert.equal(calls, 1);
  assert.equal(refreshed, 1);
});

function generationBindings(response) {
  const state = { notice: '', selection: '', focus: '', calls: 0 };
  const bindings = {
    demoMode: false, accountId: 'own', loadedAccountId: { current: 'own' }, DOMAIN_SLUG: 'ai_content',
    runLiveAction: async (_name, action) => action(),
    runAccountLoopDaily: async (accountId, input) => {
      assert.equal(accountId, 'own'); assert.equal(input.domain_slug, 'ai_content');
      assert.notEqual(input.force, true); state.calls++;
      return response;
    },
    setNotice: value => { state.notice = value; },
    setSelectedTaskId: value => { state.selection = value; },
    wakeUp: pane => { state.focus = pane; },
  };
  return { state, bindings };
}

test('generation opens the returned draft only after refresh and reports its real review state', async () => {
  const { state, bindings } = generationBindings({ status: 'pending_review', result: { pipeline_task_id: 'draft' } });
  await operation(home, 'generateDraft', bindings)();
  assert.equal(state.calls, 1);
  assert.equal(state.selection, 'draft');
  assert.equal(state.focus, 'publish');
  assert.match(state.notice, /审核/);
});

test('blocked generation retains the real reason and never claims a draft was generated', async () => {
  const { state, bindings } = generationBindings({ status: 'blocked', blocked_reason: '今日案例不足', result: { pipeline_task_id: 'reserved-not-created' } });
  await operation(home, 'generateDraft', bindings)();
  assert.match(state.notice, /今日案例不足/);
  assert.equal(state.selection, '');
  assert.doesNotMatch(state.notice, /生成完成/);
});

test('task loading retains approved manual-publication drafts and in-progress drafts, scoped to the active account', async () => {
  let tasks = [];
  const noop = () => {};
  const empty = async () => ({});
  const bindings = {
    shouldUseDemoFallback: () => false, DOMAIN_SLUG: 'ai_content', loadedAccountId: { current: 'own' },
    selectTaskForEditing: noop, setSelectedSourceId: noop,
    getRuntimeContext: async () => ({ publish_account: { account_id: 'own' } }),
    getPipelineTasks: async () => [
      { id: 'approved', account_id: 'own', status: 'approved' },
      { id: 'drafting', account_id: 'own', status: 'drafting' },
      { id: 'foreign', account_id: 'other', status: 'approved' },
    ],
    setPendingTasks: value => { tasks = value; }, toUserFacingError: err => err.message,
  };
  for (const name of ['setLoading', 'setError', 'setRuntime', 'setOverviewGlobal', 'setCollectionMode', 'setOverviewAccount', 'setPublishSummary', 'setPendingStrategy', 'setSopCurrent', 'setSopSnapshots', 'setCoachBrief', 'setCoachActions']) bindings[name] = noop;
  for (const name of ['getOpsOverview', 'getCoachBrief', 'getAccountSopCurrent', 'getAccountSopSnapshots', 'getChannelAccountStrategy', 'getPublishFeedbackSummary', 'getPendingStrategyItems']) bindings[name] = empty;
  await operation(home, 'refresh', bindings)();
  assert.deepEqual(Array.from(tasks, task => task.id), ['approved', 'drafting']);
});

test('an uncertain generation response points to status recovery instead of claiming completion', async () => {
  const { state, bindings } = generationBindings({ status: 'awaiting_status', result: { pipeline_task_id: 'draft' } });
  await operation(home, 'generateDraft', bindings)();
  assert.match(state.notice, /状态|刷新/);
  assert.doesNotMatch(state.notice, /生成完成/);
});

test('generation returning after an account switch cannot select the old account draft', async () => {
  const { state, bindings } = generationBindings({ status: 'pending_review', result: { pipeline_task_id: 'draft' } });
  bindings.runAccountLoopDaily = async () => { bindings.loadedAccountId.current = 'other'; return { status: 'pending_review', result: { pipeline_task_id: 'old' } }; };
  await operation(home, 'generateDraft', bindings)();
  assert.equal(state.selection, '');
  assert.equal(state.notice, '');
});

test('comment-only retry keeps explicit scope, partial status and refreshes the existing capture', async () => {
  let notice = '', revision = 0, calls = 0;
  const retryComments = operation(capture, 'retryComments', {
    accountId: 'own', itemId: 'note', retryLock: { current: false }, retryScope: { current: 'own:note' },
    retryCaptureComments: async (accountId, itemId) => {
      assert.equal(accountId, 'own'); assert.equal(itemId, 'note'); calls++;
      return { status: 'partial', comments_status: 'partial', warnings: ['评论只获取了部分内容'] };
    },
    setRetryBusy() {}, setRetryError() {}, setRetryNotice: value => { notice = value; },
    setRevision: update => { revision = update(revision); },
  });
  await retryComments();
  assert.equal(calls, 1);
  assert.equal(revision, 1);
  assert.match(notice, /部分/);
  assert.doesNotMatch(notice, /全部/);
});

test('comment retry errors remain visible and do not clear previously captured data', async () => {
  let error = '', revisions = 0;
  const retryComments = operation(capture, 'retryComments', {
    accountId: 'own', itemId: 'note', retryLock: { current: false }, retryScope: { current: 'own:note' },
    retryCaptureComments: async () => { throw new Error('小红书评论暂时不可读取'); },
    setRetryBusy() {}, setRetryNotice() {}, setRetryError: value => { error = value; },
    setRevision: () => { revisions++; },
  });
  await retryComments();
  assert.match(error, /暂时不可读取/);
  assert.equal(revisions, 0);
});

test('comment retry ignores responses after the visible account changes', async () => {
  let notice = '', revisions = 0;
  const scope = { current: 'own:note' };
  await operation(capture, 'retryComments', {
    accountId: 'own', itemId: 'note', retryLock: { current: false }, retryScope: scope,
    retryCaptureComments: async () => { scope.current = 'other:note'; return { comments_status: 'complete' }; },
    setRetryBusy() {}, setRetryError() {}, setRetryNotice: value => { notice = value; },
    setRevision: () => { revisions++; },
  })();
  assert.equal(notice, '');
  assert.equal(revisions, 0);
});
