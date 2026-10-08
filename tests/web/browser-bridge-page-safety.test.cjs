const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const crypto = require('node:crypto');
const policy = require('../../apps/chrome-bridge/page-policy.js');
const source = fs.readFileSync('apps/chrome-bridge/page-tools.js', 'utf8');
const sourceUrl = 'https://www.xiaohongshu.com/explore/6a089a9d0000000038021ad6';

function page() {
  const elements = new Map();
  let clicks = 0, mutation;
  const rect = { left: 10, top: 10, right: 300, bottom: 200, width: 290, height: 190 };
  const el = (text = '') => ({ innerText: text, textContent: text, isConnected: true, getBoundingClientRect: () => rect,
    getAttribute: () => '', closest: () => null, parentElement: null, contains: () => true, querySelectorAll: () => [], click: () => { clicks++; } });
  const field = el();
  elements.set('input[placeholder*="搜索"]', [field]);
  let top = field;
  const context = { EvoPagePolicy: policy, crypto, URL, location: { href: 'https://www.xiaohongshu.com/search_result?keyword=AI' },
    innerHeight: 800, innerWidth: 1200, setTimeout: fn => { fn(); }, Date, NodeFilter: { SHOW_TEXT: 4 },
    getComputedStyle: () => ({ display: 'block', visibility: 'visible', opacity: '1', overflow: 'visible' }),
    MutationObserver: class { constructor(callback) { mutation = callback; } observe() {} },
    document: { documentElement: el(), querySelectorAll: selector => elements.get(selector) || [], elementFromPoint: () => top,
      createTreeWalker: element => { let read = false; return { currentNode: { textContent: element.textContent, parentElement: element }, nextNode: () => !read && (read = true) }; },
      createRange: () => ({ selectNodeContents() {}, getClientRects: () => [rect] }) } };
  vm.runInNewContext(source, context);
  return { context, elements, el, setTop: value => { top = value; }, clicks: () => clicks, mutate: () => mutation?.(), execute: (op, args = {}) => context.EvoPublicPage.execute(op, args) };
}

test('all visible auth containers block, including textless captcha and later dialogs', async () => {
  for (const selector of ['iframe[src*="captcha"]', '[role="dialog"]', '.login-modal']) {
    const p = page();
    p.elements.set(selector, [p.el(), p.el('登录')]);
    assert.equal((await p.execute('observe')).paused, true);
  }
});

test('observed target href must remain the same public note at dispatch', async () => {
  const p = page(), link = p.el(), title = p.el('公开笔记'), card = p.el();
  link.href = sourceUrl;
  card.parentElement = card;
  card.querySelectorAll = selector => selector === 'a[href]' ? [link] : [title];
  p.elements.set('section.note-item', [card]);
  const observed = await p.execute('observe');
  link.href = 'https://www.xiaohongshu.com/message';
  const result = await p.execute('open_note', { version: observed.data.version, target_id: observed.data.results[0].target_id });
  assert.equal(result.ok, false);
  assert.equal(p.clicks(), 0);
});

test('source URL transition cannot attach old rendered body to a new note', async () => {
  const p = page();
  p.context.location.href = sourceUrl;
  p.elements.set('#detail-title', [p.el('旧笔记')]);
  p.elements.set('#detail-desc', [p.el('上一篇笔记的正文仍然显示在页面上')]);
  p.setTop(p.elements.get('#detail-desc')[0]);
  const first = await p.execute('observe');
  assert.equal(first.ok, true);
  p.context.location.href = 'https://www.xiaohongshu.com/explore/6a352608000000001503f510';
  const second = await p.execute('read_note', { version: first.data.version });
  assert.equal(second.ok, false);
});

test('capture ticket rejects intervening DOM changes and auth overlays', async () => {
  const p = page();
  const observed = await p.execute('observe');
  assert.ok(observed.data.crop && observed.data.capture_guard);
  p.mutate();
  assert.equal(p.context.EvoPublicPage.verifyCapture(observed.data.capture_guard, observed.data.crop), false);
});

test('current XHS textarea search field is recognized without legacy input', async () => {
  const p = page(), field = p.el();
  p.elements.delete('input[placeholder*="搜索"]');
  p.elements.set('textarea[placeholder*="搜索"]', [field]);
  p.setTop(field);
  const observed = await p.execute('observe');
  assert.equal(observed.ok, true);
  const searched = await p.execute('search', { version: observed.data.version, query: 'AI 工作流' });
  assert.equal(searched.search_url, 'https://www.xiaohongshu.com/search_result?keyword=AI%20%E5%B7%A5%E4%BD%9C%E6%B5%81');
});

test('result screenshot uses one public card rather than a container with foreign widgets', async () => {
  const p = page(), card = p.el(), link = p.el(), title = p.el('AI 方法');
  link.href = sourceUrl;
  const parent = p.el();
  parent.getBoundingClientRect = () => ({ left: 0, top: 0, right: 1200, bottom: 800, width: 1200, height: 800 });
  card.parentElement = parent;
  card.querySelectorAll = selector => selector === 'a[href]' ? [link] : [title];
  p.elements.set('section.note-item', [card]);
  p.setTop(card);
  const observed = await p.execute('observe');
  assert.equal(observed.data.crop.width, 290);
});

test('background results still validate public page but do not require a capture ticket', async () => {
  const p = page();
  p.setTop(null);
  const result = await p.context.EvoPublicPage.execute('observe', {}, 'background_text');
  assert.equal(result.ok, true);
  assert.equal(result.data.crop, undefined);
  p.elements.set('.login-modal', [p.el()]);
  assert.equal((await p.context.EvoPublicPage.execute('observe', {}, 'background_text')).paused, true);
});

test('adapter update disposes its old observer before installing current version', () => {
  let disposed = false;
  const context = page().context;
  context.EvoPublicPage = { adapterVersion: 1, dispose() { disposed = true; } };
  vm.runInNewContext(source, context);
  assert.equal(disposed, true);
  assert.equal(context.EvoPublicPage.adapterVersion, 2);
});

test('background capture separates comments and loaded note images from body and avatars', async () => {
  const p = page(), root = p.el(), body = p.el('公开正文内容不是评论'), title = p.el('公开标题');
  p.context.location.href = sourceUrl;
  const comment = p.el(), commentBody = p.el('公开评论内容'), author = p.el('评论作者');
  comment.id = 'comment-123';
  comment.querySelector = selector => selector === '.content .note-text' ? commentBody : selector === '.author .name' ? author : null;
  comment.closest = () => null;
  const image = p.el(), slide = p.el(); image.currentSrc = 'https://sns-webpic-qc.xhscdn.com/public.webp'; image.naturalWidth = 1080;
  image.closest = () => slide; slide.getAttribute = () => '0';
  root.querySelector = selector => selector === '.comments-container' || selector === '.note-slider' ? p.el() : null;
  root.querySelectorAll = selector => selector === '.comments-container .comment-item' ? [comment,comment] : selector === '.note-slider .swiper-slide:not(.swiper-slide-duplicate) .note-slider-img img' ? [image] : [];
  body.closest = selector => selector === '#noteContainer' ? root : null;
  p.elements.set('#detail-title',[title]); p.elements.set('#detail-desc',[body]);
  const result = await p.context.EvoPublicPage.execute('observe',{},'background_text');
  assert.equal(result.ok,true);
  assert.equal(result.data.capture.body_text,'公开正文内容不是评论');
  assert.equal(result.data.capture.comments.length,1);
  assert.equal(result.data.capture.comments[0].text,'公开评论内容');
  assert.equal(result.data.capture.image_candidates.length,1);
  assert.equal(result.data.capture.image_candidates[0].index,0);
  assert.equal(result.data.capture.comments_status,'partial');
  assert.equal(result.data.screenshot,undefined);
});

test('missing comment IDs retain stable parent relationships when IDs arrive', async () => {
  const p=page(),root=p.el(),body=p.el('这是真实公开正文内容'),parent=p.el(),reply=p.el(),thread=p.el();
  p.context.location.href=sourceUrl;
  parent.querySelector=s=>s==='.author .name'?p.el('作者'):s==='.content .note-text'?p.el('根评论'):null;
  reply.querySelector=s=>s==='.author .name'?p.el('回复者'):s==='.content .note-text'?p.el('公开回复'):null;
  thread.querySelector=()=>parent;
  parent.closest=()=>null;
  reply.closest=s=>s==='.reply-container'?p.el():s==='.parent-comment'?thread:null;
  root.querySelector=s=>s==='.comments-container'?p.el():null;
  root.querySelectorAll=s=>s==='.comments-container .comment-item'?[parent,reply]:[];
  body.closest=s=>s==='#noteContainer'?root:null;
  p.elements.set('#detail-title',[p.el('公开标题')]);p.elements.set('#detail-desc',[body]);
  let result=await p.context.EvoPublicPage.execute('observe',{},'background_text');
  assert.equal(result.data.capture.comments[1].parent_key,result.data.capture.comments[0].key);
  parent.id='server-parent';reply.id='server-reply';
  result=await p.context.EvoPublicPage.execute('observe',{},'background_text');
  assert.equal(result.data.capture.comments.length,2);
  assert.equal(result.data.capture.comments[1].parent_key,result.data.capture.comments[0].key);
});
