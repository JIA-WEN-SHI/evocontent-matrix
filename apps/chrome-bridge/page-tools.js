(() => {
  if (globalThis.EvoPublicPage?.adapterVersion === 2) return;
  globalThis.EvoPublicPage?.dispose?.();
  const policy = globalThis.EvoPagePolicy;
  let state = { version: '', targets: {}, detailTargets: {}, opened: null, lastNote: null, captures: {}, commentAliases: {} };
  let mode = 'foreground_visual';
  let revision = 0;
  const observer = new MutationObserver(() => { revision++; });
  observer.observe(document.documentElement, { subtree: true, childList: true, attributes: true, characterData: true });
  const visible = el => {
    if (!el) return false;
    const style = getComputedStyle(el), r = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && Number(style.opacity) !== 0 &&
      r.width > 0 && r.height > 0 && r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth;
  };
  const first = selectors => [...document.querySelectorAll(selectors)].find(visible);
  const rendered = el => {
    if (!el || !el.isConnected || el.getBoundingClientRect().width <= 0 || el.getBoundingClientRect().height <= 0) return false;
    for (let node = el; node && node !== document.documentElement; node = node.parentElement) {
      const style = getComputedStyle(node);
      if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0) return false;
    }
    return true;
  };
  const searchField = () => first('input[placeholder*="搜索"]') || first('textarea[placeholder*="搜索"]') || first('textarea#search-input-in-feeds');
  function publicText(element, limit = 5000) {
    const isShown = mode === 'background_text' ? rendered : visible;
    if (!isShown(element)) return '';
    const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
    const parts = [];
    while (walker.nextNode()) {
      const node = walker.currentNode;
      if (!isShown(node.parentElement) || node.parentElement.closest('script,style,input,textarea,[contenteditable="true"],.translation-container')) continue;
      const range = document.createRange(); range.selectNodeContents(node);
      if (mode === 'background_text' || [...range.getClientRects()].some(r => r.top < innerHeight && r.bottom > 0 && r.left < innerWidth && r.right > 0)) {
        const text = node.textContent.trim();
        if (text) parts.push(text);
      }
      if (parts.join('\n').length >= limit) break;
    }
    return parts.join('\n').slice(0, limit);
  }
  function blocked() {
    if (!policy.isPublicPage(location.href)) return '已离开允许的公开页面，请重新连接';
    for (const selector of ['.login-container', '.login-modal', '.login-mask', '.captcha-container', '.verify-modal', 'iframe[src*="captcha"]', 'iframe[src*="verify"]', '[role="dialog"]']) {
      if (first(selector)) return '请自行处理登录、弹窗或验证码，再在项目中点击继续';
    }
    return null;
  }
  function rect(element) {
    const r = element.getBoundingClientRect();
    const x = Math.max(0, r.left), y = Math.max(0, r.top);
    return { x, y, width: Math.max(0, Math.min(innerWidth, r.right) - x), height: Math.max(0, Math.min(innerHeight, r.bottom) - y), viewport_width: innerWidth };
  }
  function verifyCapture(guard, crop) {
    if (blocked() || !guard || guard.revision !== revision || guard.href !== location.href || guard.version !== state.version) return false;
    const region = state.captureRegion;
    if (!region || !visible(region)) return false;
    const current = rect(region);
    if (Object.keys(current).some(key => Math.abs(current[key] - crop[key]) > 1)) return false;
    // Reject foreign overlays covering any sampled point in the public region.
    for (let y = 1; y <= 5; y++) for (let x = 1; x <= 5; x++) {
      const top = document.elementFromPoint(crop.x + crop.width * x / 6, crop.y + crop.height * y / 6);
      if (!top || (top !== region && !region.contains(top))) return false;
    }
    return true;
  }
  function withCapture(data, region) {
    if (mode === 'background_text') return { ok: true, data };
    state.captureRegion = region;
    if (!visible(region)) return { ok: false, paused: true, message: '没有可确认的公开截图区域，请打开公开搜索结果后继续' };
    const crop = rect(region);
    const capture_guard = { revision, href: location.href, version: state.version };
    if (crop.width < 40 || crop.height < 40 || !verifyCapture(capture_guard, crop)) return { ok: false, paused: true, message: '公开区域被遮挡，请关闭弹窗后继续' };
    return { ok: true, data: { ...data, crop, capture_guard } };
  }
  const fingerprintKey = value => {
    let hash = 2166136261;
    for (const char of value) hash = Math.imul(hash ^ char.charCodeAt(0),16777619);
    return (hash >>> 0).toString(16);
  };
  function readCapture(root, note, fullText) {
    const previous = state.captures[note.source_url];
    const capture = previous || {schema_version:1,source_url:note.source_url,title:note.title,body_text:'',tags:[],author_name:'',comments:[],image_candidates:[],truncation_reasons:[]};
    const reasons = new Set(capture.truncation_reasons);
    capture.body_text = fullText.slice(0,50000);
    capture.title = note.title; capture.captured_at = note.captured_at;
    capture.tags = [...root.querySelectorAll('#detail-desc a')].filter(rendered).map(e=>publicText(e,200)).filter(t=>t.startsWith('#')).slice(0,100);
    capture.author_name = publicText(root.querySelector('.author-container .username'),200);
    const addTarget = (element,operation) => {
      if (!rendered(element) || Object.keys(state.detailTargets).length >= 30) return;
      state.detailTargets[crypto.randomUUID()] = {element,operation,source:note.source_url};
    };
    const expand = [...root.querySelectorAll('.note-content button,.note-content .expand')].find(e=>rendered(e)&&/^(展开|展开全文)$/.test(e.innerText.trim()));
    if (expand) addTarget(expand,'expand_body');
    const body = root.querySelector('#detail-desc'), style = body ? getComputedStyle(body) : {};
    const clipped = !!expand || (style.webkitLineClamp && style.webkitLineClamp !== 'none' && style.webkitLineClamp !== '0') || (body && body.scrollHeight > body.clientHeight + 2 && ['hidden','clip'].includes(style.overflowY));
    capture.body_status = fullText.length > 50000 || clipped ? 'partial' : 'complete';
    if (fullText.length > 50000) reasons.add('正文达到 50000 字符上限');
    const comments = new Map(capture.comments.map(c=>[c.key,c]));
    const aliases = state.commentAliases[note.source_url] ||= new Map();
    const commentKey = (item,parentKey=null) => {
      const identity = (parentKey||'')+'|'+publicText(item.querySelector('.author .name'),200)+'|'+publicText(item.querySelector('.content .note-text'),2000);
      const fingerprint = fingerprintKey(identity);
      if (aliases.has(fingerprint)) return aliases.get(fingerprint);
      const key = (item.id || 'fallback-'+fingerprint).slice(0,120);
      if (aliases.size < 200) aliases.set(fingerprint,key);
      return key;
    };
    for (const item of root.querySelectorAll('.comments-container .comment-item')) {
      if (!rendered(item)) continue;
      const text = publicText(item.querySelector('.content .note-text'),2001);
      if (!text) continue;
      const parent = item.closest('.reply-container') ? item.closest('.parent-comment')?.querySelector('.comment-item') : null;
      const parent_key = parent ? commentKey(parent) : null;
      const author_name = publicText(item.querySelector('.author .name'),200);
      const key = commentKey(item,parent_key);
      if (comments.size >= 100 && !comments.has(key)) { reasons.add('评论及回复达到 100 条上限'); continue; }
      comments.set(key,{key,parent_key,text:text.slice(0,2000),author_name,observed_time:publicText(item.querySelector('.info .date span:first-child'),100),truncated:text.length>2000});
    }
    capture.comments = [...comments.values()].slice(0,100);
    const replies = [...root.querySelectorAll('.comments-container .parent-comment div')].filter(e=>rendered(e)&&/^展开\s*\d+\s*条回复$/.test(e.innerText.trim()));
    for (const button of replies) addTarget(button,'expand_reply');
    const end = root.querySelector('.comments-container .end-container');
    const empty = root.querySelector('.comments-container .empty-container');
    capture.comments_status = !root.querySelector('.comments-container') ? 'unsupported' : (end && rendered(end) && /THE END/.test(end.innerText) && !replies.length && !reasons.has('评论及回复达到 100 条上限')) ? 'complete' : empty && rendered(empty) && !capture.comments.length ? 'empty' : 'partial';
    const images = new Map(capture.image_candidates.map(i=>[i.key,i]));
    for (const img of root.querySelectorAll('.note-slider .swiper-slide:not(.swiper-slide-duplicate) .note-slider-img img')) {
      if (!img.naturalWidth || !img.currentSrc || !rendered(img)) continue;
      const index = Number(img.closest('.swiper-slide')?.getAttribute('data-swiper-slide-index'));
      if (!Number.isInteger(index) || index < 0 || index > 100) continue;
      let url;
      try { url = new URL(img.currentSrc); } catch { continue; }
      if (url.protocol !== 'https:' || url.hostname !== 'sns-webpic-qc.xhscdn.com' || url.username || url.password || (url.port && url.port !== '443')) continue;
      if (images.size >= 24 && !images.has('image-'+index)) {reasons.add('配图达到 24 张上限');continue;}
      images.set('image-'+index,{key:'image-'+index,index,url:url.href});
    }
    capture.image_candidates = [...images.values()].sort((a,b)=>a.index-b.index).slice(0,24);
    const slides = root.querySelectorAll('.note-slider .swiper-slide:not(.swiper-slide-duplicate)').length;
    capture.images_status = !root.querySelector('.note-slider') ? 'unsupported' : slides > 0 && images.size >= slides ? 'complete' : 'partial';
    if (capture.images_status !== 'complete' && images.size < 24) addTarget(root.querySelector('.arrow-controller.right:not(.forbidden)'),'next_image');
    capture.truncation_reasons = [...reasons].slice(0,20);
    state.captures[note.source_url] = capture;
    if (Object.keys(state.captures).length > 4) {
      const expired = Object.keys(state.captures)[0];
      delete state.captures[expired]; delete state.commentAliases[expired];
    }
    return JSON.parse(JSON.stringify(capture));
  }
  function observe() {
    const reason = blocked();
    if (reason) return { ok: false, paused: true, message: reason };
    state.version = crypto.randomUUID(); state.targets = {}; state.detailTargets = {};
    const find = selector => mode === 'background_text' ? [...document.querySelectorAll(selector)].find(rendered) : first(selector);
    const title = find('#detail-title'), body = find('#detail-desc');
    if (title || body) {
      const source = policy.noteUrl(location.href) || state.opened?.source_url;
      if (!source || !body) return { ok: false, paused: true, message: '未识别到公开笔记正文，请手动打开一篇笔记后继续' };
      const noteTitle = publicText(title, 300), fullText = publicText(body, mode === 'background_text' ? 50001 : 5000), text = fullText.slice(0,5000);
      const fingerprint = noteTitle + '\n' + text;
      const expectedTitle = state.opened?.title?.replace(/\s/g, '');
      if ((state.lastNote && state.lastNote.source !== source && state.lastNote.fingerprint === fingerprint) ||
          (state.opened && (source !== state.opened.source_url || !expectedTitle || noteTitle.replace(/\s/g, '') !== expectedTitle))) {
        return { ok: false, paused: true, transitioning: true, message: '笔记内容尚未与来源对应，请等正文加载完成后继续' };
      }
      state.lastNote = { source, fingerprint };
      const container = first('.note-content') || body;
      const data = { kind: 'note', version: state.version, note: {
        source_url: source, title: noteTitle || state.opened?.title || '公开笔记', text,
        captured_at: new Date().toISOString() } };
      if (mode === 'background_text') {
        const root = body.closest('#noteContainer');
        if (!root) return { ok:false,paused:true,message:'公开笔记详情结构无法确认' };
        data.capture = readCapture(root, data.note, fullText);
        data.detail_targets = Object.entries(state.detailTargets).map(([target_id,t]) => ({target_id,operation:t.operation}));
      }
      return withCapture(data, container);
    }
    const cards = [...document.querySelectorAll('section.note-item')].filter(visible).slice(0, 12);
    const results = [];
    for (const card of cards) {
      const link = [...card.querySelectorAll('a[href]')].find(a => visible(a) && policy.noteUrl(a.href));
      if (!link) continue;
      const target_id = crypto.randomUUID(), source_url = policy.noteUrl(link.href);
      const title = publicText([...card.querySelectorAll('.title')].find(visible), 200) || publicText(card, 200);
      state.targets[target_id] = { link, source_url, title };
      results.push({ target_id, source_url, title });
    }
    const field = searchField();
    if (!cards.length && !field) return { ok: false, paused: true, message: '未识别到小红书公开搜索页面，请打开搜索或发现页后继续' };
    const container = cards.length ? cards[0] : field;
    return withCapture({ kind: 'results', version: state.version, results }, container);
  }
  async function execute(operation, args, scope = 'foreground_visual') {
    if (!['foreground_visual','background_text'].includes(scope)) return {ok:false,message:'采集模式无效'};
    mode = scope;
    const reason = blocked();
    if (reason) return { ok: false, paused: true, message: reason };
    try { policy.validate(operation, args, state); }
    catch (e) { return { ok: false, message: e.message, code: 'stale_observation' }; }
    if (operation === 'observe' || operation === 'read_note') return observe();
    if (operation === 'search') {
      if (!searchField()) return { ok: false, paused: true, message: '请切回公开搜索页后继续' };
      return { ok: true, search_url: `https://www.xiaohongshu.com/search_result?keyword=${encodeURIComponent(args.query.trim())}` };
    }
    if (operation === 'open_note') {
      const target = state.targets[args.target_id];
      if (!target?.link?.isConnected || !visible(target.link) || policy.noteUrl(target.link.href) !== target.source_url) return { ok: false, message: '笔记位置或目标已变化，请重新观察' };
      state.opened = { source_url: target.source_url, title: target.title };
      target.link.click();
    } else if (operation === 'scroll_note') {
      const body = first('#detail-desc');
      if (!body) return { ok: false, message: '请先打开笔记正文' };
      const container = body.closest('.note-scroller,.content,.note-content') || body.parentElement;
      container.scrollBy({ top: (args.direction === 'down' ? 1 : -1) * 450, behavior: 'instant' });
    } else if (operation === 'close_note') {
      const close = first('.note-detail-mask .close-circle,.note-detail-mask .close-box');
      if (!close) return { ok: false, paused: true, message: '请手动关闭笔记回到搜索结果，再点击继续' };
      close.click(); state.opened = null;
    } else if (operation === 'scroll_comments') {
      const root = first('#noteContainer'), scroller = root?.querySelector('.note-scroller');
      if (!scroller) return {ok:false,paused:true,message:'未识别到公开评论滚动区'};
      scroller.scrollBy({top:450,behavior:'instant'});
      await new Promise(resolve=>setTimeout(resolve,400));
    } else if (['expand_body','expand_reply','next_image'].includes(operation)) {
      const target = state.detailTargets[args.target_id];
      if (!target?.element?.isConnected || !rendered(target.element) || !target.element.closest('#noteContainer') || (policy.noteUrl(location.href) || state.opened?.source_url) !== target.source) return {ok:false,paused:true,message:'公开内容目标已变化，请重新观察'};
      target.element.click();
      await new Promise(resolve=>setTimeout(resolve,400));
    }
    // Wait for a rendered-state change, without treating dispatch as success.
    for (let attempt = 0; attempt < 15; attempt++) {
      const result = observe();
      if (result.paused && !result.transitioning) return result;
      if (operation === 'open_note' ? result.data?.kind === 'note' : operation === 'close_note' ? result.data?.kind === 'results' : result.ok) {
        if (operation === 'open_note') {
          const fingerprint = state.lastNote?.fingerprint;
          await new Promise(resolve => setTimeout(resolve, 200));
          const settled = observe();
          if (!settled.ok || fingerprint !== state.lastNote?.fingerprint) continue;
          return settled;
        }
        return result;
      }
      await new Promise(resolve => setTimeout(resolve, 200));
    }
    return { ok: false, message: '页面操作没有产生可确认的结果' };
  }
  globalThis.EvoPublicPage = { execute, verifyCapture, adapterVersion:2, dispose:()=>observer.disconnect() };
})();
