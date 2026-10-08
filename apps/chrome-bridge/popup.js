const status = document.getElementById('status');
let preview = null;
let workerReady = false;
let workerFailure = null;
const packageVersion = chrome.runtime.getManifest().version;
function showDiagnostics(data) {
  document.getElementById('diagnostics').textContent = `安装版本 ${packageVersion} · 运行版本 ${data?.extension_version || '未返回'} · 协议 ${data?.protocol_version ?? '未返回'} · 扩展 ID ${chrome.runtime.id}`;
}
showDiagnostics(null);
document.getElementById('code').addEventListener('input', () => {
  preview = null; document.getElementById('approval').hidden = true;
  document.getElementById('consent').checked = false;
  document.querySelector('#pair button').textContent = '检查配对码';
});
function render(data) {
  status.textContent = data.error || data.message || (data.connected ? '已连接当前小红书标签页' : '未连接项目');
  document.getElementById('pair').hidden = !!data.connected;
  document.getElementById('disconnect').hidden = !data.connected;
}
const workerCheck = chrome.runtime.sendMessage({ type: 'status' }).then(data => {
  showDiagnostics(data);
  let error = '';
  if (!data || typeof data !== 'object') error = '插件运行组件未返回状态响应，请提供插件详情截图';
  else if (data.error) error = data.error;
  else if (data.protocol_version == null || !data.extension_version) error = '插件运行组件未返回版本或协议信息，请核对实际加载的插件目录';
  else if (data.protocol_version !== 2 || data.extension_version !== '1.1.1' || packageVersion !== '1.1.1') error = '插件安装版本、运行版本或协议不匹配，请核对下方版本信息';
  if (error) {
    workerFailure = { connected: !!data?.connected, error };
    return render(workerFailure);
  }
  workerReady = true;
  render(data);
}).catch(e => {
  workerFailure = { error: e.message || '插件运行组件通信失败，请提供插件详情截图' };
  render(workerFailure);
});
document.getElementById('pair').addEventListener('submit', async event => {
  event.preventDefault();
  const button = event.target.querySelector('button'); button.disabled = true;
  try {
    await workerCheck;
    if (!workerReady) return render(workerFailure);
    const code = document.getElementById('code').value.trim();
    if (!preview) {
      const data = await chrome.runtime.sendMessage({ type: 'pair-info', code });
      if (data.error) return render(data);
      if (data.protocol_version !== 2) throw new Error('请更新项目后连接');
      preview = data;
      document.getElementById('provider').textContent = `${data.provider} · ${data.model_name}`;
      document.getElementById('scope').textContent = data.mode === 'background_text' ? '后台文本采集 · 仅控制当前配对的标签页，可切换其他标签页' : '前台视觉采集 · 请保持该标签页当前打开';
      document.getElementById('approval').hidden = false;
      button.textContent = '确认连接当前标签页';
      status.textContent = '请核对模型服务和操作范围';
    } else if (!document.getElementById('consent').checked) {
      status.textContent = '请先确认页面操作和模型服务授权';
    } else render(await chrome.runtime.sendMessage({ type: 'pair', code }));
  }
  catch (e) { render({ error: e.message || '无法连接，请检查本机项目是否运行' }); }
  finally { button.disabled = false; }
});
document.getElementById('disconnect').addEventListener('click', async () => render(await chrome.runtime.sendMessage({ type: 'disconnect' })));
