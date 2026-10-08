const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');

test('Chrome bridge permits public XHS pages only and strips source tokens', () => {
  const policy = require('../../apps/chrome-bridge/page-policy.js');
  assert.equal(policy.isPublicPage('https://www.xiaohongshu.com/search_result?keyword=AI'), true);
  for (const url of ['http://www.xiaohongshu.com/', 'https://www.xiaohongshu.com.evil.test/', 'https://www.xiaohongshu.com/user/profile/a', 'https://www.xiaohongshu.com/message']) {
    assert.equal(policy.isPublicPage(url), false);
  }
  assert.equal(policy.noteUrl('https://www.xiaohongshu.com/explore/6a089a9d0000000038021ad6?xsec_token=secret'), 'https://www.xiaohongshu.com/explore/6a089a9d0000000038021ad6');
  assert.equal(policy.noteUrl('https://www.xiaohongshu.com/explore/not-a-note'), null);
  assert.equal(policy.noteUrl('https://www.xiaohongshu.com/search_result/6a089a9d0000000038021ad6?xsec_token=secret'), 'https://www.xiaohongshu.com/explore/6a089a9d0000000038021ad6');
});

test('Chrome bridge rejects stale targets and arbitrary UI execution', () => {
  const policy = require('../../apps/chrome-bridge/page-policy.js');
  assert.throws(() => policy.validate('click', { x: 1, y: 2 }));
  assert.throws(() => policy.validate('search', { query: 'AI', script: 'alert(1)', version: 'v' }));
  assert.throws(() => policy.validate('open_note', { target_id: 'unknown', version: 'old' }, { version: 'new', targets: {} }));
  assert.doesNotThrow(() => policy.validate('open_note', { target_id: 'note1', version: 'v' }, { version: 'v', targets: { note1: {} } }));
  assert.throws(() => policy.validate('search', { query: ' ', version: 'v' }));
});

test('Chrome bridge requests no cookie, debugging, history or all-site access', () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(__dirname, '../../apps/chrome-bridge/manifest.json'), 'utf8'));
  assert.equal(manifest.manifest_version, 3);
  assert.deepEqual([...manifest.permissions].sort(), ['activeTab', 'scripting', 'storage']);
  assert.deepEqual(manifest.host_permissions, ['http://127.0.0.1:8000/*']);
});
