const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const { test } = require('node:test');
const source = readFileSync(resolve(__dirname, '../../apps/web/components/accounts/account-editors.tsx'), 'utf8');

test('account editor offers both writing branches and preserves existing prompt overrides', () => {
  assert.match(source, /value="tutorial"/);
  assert.match(source, /value="project_observer"/);
  assert.match(source, /AI 项目观察·共情短文/);
  assert.match(source, /\.\.\.profile\.prompt_overrides/);
  assert.match(source, /content_branch: contentBranch/);
});

test('project fields are available only for observer branch with explicit source confirmation', () => {
  assert.match(source, /contentBranch === "project_observer"/);
  for (const label of ['项目名称', '所属行业', '官方来源', '技术与用途', '项目优势', '输入材料', '输出结果', '技术关键词', '受影响的职业', '设想的职业画面']) {
    assert.ok(source.includes(label), label);
  }
  assert.match(source, /我已核对官方来源中的项目能力/);
  assert.match(source, /project_reference:/);
});
