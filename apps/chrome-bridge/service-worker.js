importScripts('page-policy.js');
const BASE = 'http://127.0.0.1:8000/api/browser-bridge';
let polling = false;
let lastError = '';
let surfaceEpoch = 0;
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
async function deadline(promise, milliseconds, message) {
  let timer;
  try {
    return await Promise.race([promise, new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(message)), milliseconds);
    })]);
  } finally { clearTimeout(timer); }
}
async function revokeTab(tabId) {
  const { bridgeSession: session } = await chrome.storage.session.get('bridgeSession');
  if (session?.tab_id !== tabId) return;
  await chrome.storage.session.remove('bridgeSession');
  lastError = '标签页已关闭或离开公开页面，请重新配对';
  await request('/extension/disconnect', {}, session).catch(() => {});
}
chrome.tabs.onActivated.addListener(() => { surfaceEpoch++; });
chrome.windows.onFocusChanged.addListener(() => { surfaceEpoch++; });
chrome.tabs.onRemoved.addListener(tabId => { surfaceEpoch++; return revokeTab(tabId); });
chrome.tabs.onUpdated.addListener((tabId, change) => {
  if (change.url || change.status === 'loading') surfaceEpoch++;
  if (change.url && !EvoPagePolicy.isPublicPage(change.url)) return revokeTab(tabId);
  if (change.frozen || change.discarded) return pauseTab(tabId);
});
async function pauseTab(tabId) {
  const { bridgeSession: session } = await chrome.storage.session.get('bridgeSession');
  if (session?.tab_id !== tabId) return;
  lastError = '采集标签页已冻结或休眠，请自行恢复该页面后继续';
  await request('/extension/pause',{message:lastError},session).catch(()=>{});
}
async function request(path, body, session) {
  const response = await fetch(BASE + path, { method: body === undefined ? 'GET' : 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Extension-Origin': chrome.runtime.getURL('').replace(/\/$/, ''), ...(body?.code ? { 'X-Pairing-Code': body.code } : {}), ...(session?.token ? { Authorization: `Bearer ${session.token}` } : {}) },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }), signal: AbortSignal.timeout(15000) });
  const data = await response.json();
  if (!response.ok) { const e = new Error(typeof data.detail === 'string' ? data.detail : '项目连接失败'); e.status = response.status; throw e; }
  return data;
}
async function pageCall(tabId, operation, args, mode = 'foreground_visual') {
  await deadline(chrome.scripting.executeScript({ target: { tabId }, files: ['page-policy.js', 'page-tools.js'] }), 5000, '页面适配加载超时，请刷新小红书页面后继续');
  const result = await deadline(chrome.scripting.executeScript({ target: { tabId }, func: (op, input, scope) => globalThis.EvoPublicPage.execute(op, input, scope), args: [operation, args, mode] }), 12000, '页面操作超时，请确认笔记已加载后继续');
  return result[0]?.result || { ok: false, message: '公开页面未返回结果' };
}
async function currentTab(session) {
  const { bridgeSession } = await chrome.storage.session.get('bridgeSession');
  if (!bridgeSession || bridgeSession.token !== session.token) throw new Error('浏览器连接已断开');
  let tab;
  try { tab = await chrome.tabs.get(session.tab_id); }
  catch { await revokeTab(session.tab_id); throw new Error('标签页已关闭，请重新配对'); }
  if (tab.frozen || tab.discarded) throw new Error('采集标签页已冻结或休眠，请自行恢复该页面后继续');
  if ((session.mode || 'foreground_visual') === 'foreground_visual') {
    const [active] = await chrome.tabs.query({ active: true, windowId: tab.windowId });
    if (active?.id !== session.tab_id) throw new Error('请切回已连接的小红书标签页，再点击继续');
  }
  if (!EvoPagePolicy.isPublicPage(tab.url)) { await revokeTab(session.tab_id); throw new Error('已离开公开小红书页面，请重新连接'); }
  return tab;
}
async function screenshot(tab, crop, guard, session) {
  if (!crop || !guard || crop.width < 40 || crop.height < 40) throw new Error('没有公开截图边界');
  const epoch = surfaceEpoch;
  const check = async () => {
    await currentTab(session);
    const [result] = await chrome.scripting.executeScript({ target: { tabId: tab.id },
      func: (ticket, region) => globalThis.EvoPublicPage?.verifyCapture(ticket, region) === true, args: [guard, crop] });
    if (surfaceEpoch !== epoch) throw new Error('截图期间标签页发生切换');
    if (!result?.result) throw new Error('公开截图区域已变化');
  };
  await check();
  const uri = await chrome.tabs.captureVisibleTab(tab.windowId, { format: 'jpeg', quality: 55 }).catch(() => { throw new Error('Chrome 截图授权失效'); });
  await check();
  const bitmap = await createImageBitmap(await (await fetch(uri)).blob());
  const factor = bitmap.width / crop.viewport_width;
  const width = Math.min(960, Math.round(crop.width * factor));
  const height = Math.round(width * crop.height / crop.width);
  const canvas = new OffscreenCanvas(width, height);
  canvas.getContext('2d').drawImage(bitmap, crop.x * factor, crop.y * factor, crop.width * factor, crop.height * factor, 0, 0, width, height);
  bitmap.close();
  const blob = await canvas.convertToBlob({ type: 'image/jpeg', quality: 0.55 });
  const bytes = new Uint8Array(await blob.arrayBuffer());
  if (bytes.length > 500000) throw new Error('截图过大');
  let binary = ''; for (const byte of bytes) binary += String.fromCharCode(byte);
  return `data:image/jpeg;base64,${btoa(binary)}`;
}
async function execute(session, command) {
  const mode = session.mode || 'foreground_visual';
  if (command.mode && command.mode !== mode) return { ok: false, paused: true, message: '采集模式不匹配，请重新配对' };
  let tab;
  try { tab = await currentTab(session); }
  catch (e) { return { ok: false, paused: true, message: e.message }; }
  let result = await pageCall(tab.id, command.operation, command.arguments, mode);
  delete result.transitioning;
  if (result.search_url) {
    await currentTab(session);
    if (!EvoPagePolicy.isPublicPage(result.search_url)) return { ok: false, message: '搜索页面不允许' };
    await chrome.tabs.update(tab.id, { url: result.search_url });
    for (let i = 0; i < 40; i++) {
      await delay(200);
      tab = await currentTab(session);
      if (tab.status === 'complete') break;
    }
    result = await pageCall(tab.id, 'observe', {}, mode);
  }
  if (mode === 'background_text' && result.data) {
    try { await currentTab(session); }
    catch (e) { return {ok:false,paused:true,message:e.message}; }
    delete result.data.crop; delete result.data.capture_guard; delete result.data.screenshot;
    return result;
  }
  if (result.ok && result.data) {
    const originalSource = result.data.note?.source_url;
    for (let attempt = 0; attempt < 2; attempt++) {
      const crop = result.data.crop, guard = result.data.capture_guard;
      delete result.data.crop;
      delete result.data.capture_guard;
      try { result.data.screenshot = await screenshot(tab, crop, guard, session); break; }
      catch (e) {
        if (attempt === 0 && e.message === '公开截图区域已变化') {
          // Refresh pixels/evidence once; never dispatch the user's action again.
          await delay(250);
          const refreshed = await pageCall(tab.id, 'observe', {}, mode);
          delete refreshed.transitioning;
          if (!refreshed.ok || !refreshed.data) return refreshed;
          if (originalSource && refreshed.data.note?.source_url !== originalSource) return { ok: false, paused: true, message: '笔记来源已变化，请重新观察后继续' };
          result = refreshed;
          continue;
        }
        const messages = {
          '公开截图区域已变化': '公开截图区域已变化，请等页面稳定后继续',
          '截图期间标签页发生切换': '截图期间标签页发生切换，请保持已连接小红书页为当前标签页后继续',
          'Chrome 截图授权失效': 'Chrome 截图授权失效，请在小红书标签页重新打开插件并配对',
          '没有公开截图边界': '未识别到公开截图区域，请打开公开搜索结果后继续',
          '截图过大': '截图过大，请调整到单篇公开笔记区域后继续',
        };
        return { ok: false, paused: true, message: messages[e.message] || '截图生成失败，请重新加载插件后继续' };
      }
    }
  }
  return result;
}
async function poll() {
  if (polling) return;
  polling = true;
  try {
    while (true) {
      const { bridgeSession: session } = await chrome.storage.session.get('bridgeSession');
      if (!session) break;
      try {
        try {
          const bound = await chrome.tabs.get(session.tab_id);
          if (!EvoPagePolicy.isPublicPage(bound.url)) { await revokeTab(session.tab_id); break; }
          if (bound.frozen || bound.discarded) {
            await pauseTab(session.tab_id);
            await delay(1500);
            continue;
          }
        }
        catch { await revokeTab(session.tab_id); break; }
        const data = await request('/extension/poll', {}, session);
        if (data.command) {
          let result;
          try { result = await execute(session, data.command); }
          catch (e) { result = { ok: false, paused: true, message: /超时/.test(e.message || '') ? e.message : '页面权限或结构已变化，请打开公开笔记后继续' }; }
          await request('/extension/results', { command_id: data.command.id, result }, session);
        }
        lastError = '';
      } catch (e) {
        lastError = e.message;
        if ([401, 403].includes(e.status)) { await chrome.storage.session.remove('bridgeSession'); break; }
      }
      await delay(1500);
    }
  } finally { polling = false; }
}
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (sender.id !== chrome.runtime.id) return;
  (async () => {
    if (message.type === 'status') {
      const { bridgeSession } = await chrome.storage.session.get('bridgeSession');
      void poll();
      return { connected: !!bridgeSession, account_id: bridgeSession?.account_id, message: lastError, protocol_version:2, extension_version:'1.1.1' };
    }
    if (message.type === 'pair') {
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      if (!EvoPagePolicy.isPublicPage(tab?.url)) throw new Error('请先切到已登录的小红书公开搜索或笔记标签页');
      const info = await request('/extension/pair-info', { code: message.code });
      if (info.protocol_version !== 2) throw new Error('项目协议不匹配，请更新插件和项目');
      const observation = await pageCall(tab.id, 'observe', {}, info.mode);
      if (!observation.ok) throw new Error(observation.message || '请打开公开搜索或笔记页面');
      const session = await request('/extension/pair', { code: message.code, tab_id: tab.id, consent: true, protocol_version: 2 });
      await chrome.storage.session.set({ bridgeSession: session });
      void poll();
      return { connected: true, account_id: session.account_id };
    }
    if (message.type === 'pair-info') return request('/extension/pair-info', { code: message.code });
    if (message.type === 'disconnect') {
      const { bridgeSession } = await chrome.storage.session.get('bridgeSession');
      if (bridgeSession) await request('/extension/disconnect', {}, bridgeSession).catch(() => {});
      await chrome.storage.session.remove('bridgeSession');
      return { connected: false };
    }
    throw new Error('不支持的操作');
  })().then(sendResponse, e => sendResponse({ error: e.message }));
  return true;
});
