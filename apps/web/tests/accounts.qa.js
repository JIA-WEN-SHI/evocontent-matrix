// Run with playwright-cli -s=accountqa run-code --filename apps/web/tests/accounts.qa.js
async (page) => {
  await page.unrouteAll({ behavior: 'ignoreErrors' });
  page.setDefaultTimeout(10000);
  page.setDefaultNavigationTimeout(30000);
  const assert = (condition, message) => { if (!condition) throw new Error(message); };
  const writes = [];
  let accounts = [];
  let failNext = false;
  let holdWrite = false;
  let releaseWrite;
  let runtimeFailure = false;
  let holdStrategy = false;
  let releaseStrategy;
  const pageErrors = [];
  page.on('pageerror', error => pageErrors.push(error.message));
  let profile = { persona_name: 'Original', ip_positioning: 'Position', primary_goal: 'Goal', tone_style: 'Calm', cta_style: '', audience: ['Readers'], pain_points: [], content_pillars: [], forbidden_claims: [], focus_keywords: ['preserved'], prompt_overrides: { copy: 'preserved override' }, mcp_limit_default: 17 };
  const feedback = { checkpoints_hours: [12, 48], require_real_metrics_for_upgrade: true, synthetic_preview_enabled: false, auto_retro_after_last_checkpoint: true };
  let versions = [];
  await page.route('**/api/**', async route => {
    const request = route.request();
    const path = request.url().split('/').slice(3).join('/').split('?')[0].replace(/^/, '/');
    const method = request.method();
    const headers = { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*', 'access-control-allow-methods': '*' };
    const reply = (body, status = 200) => route.fulfill({ status, headers, contentType: 'application/json', body: JSON.stringify(body) });
    if (method === 'OPTIONS') return reply({});
    if (method !== 'GET') {
      const body = request.postDataJSON();
      writes.push({ path, method, body });
      if (holdWrite) await new Promise(resolve => { releaseWrite = resolve; });
      if (failNext) { failNext = false; return reply({ detail: '保存失败，请稍后重试。' }, 503); }
      if (path === '/api/accounts' && method === 'POST') {
        const row = { id: 'qa-account', channel: 'xiaohongshu', account_handle: null, login_mode: 'storage_state', storage_state_path: null, user_data_dir: null, cookies_json: null, login_username: null, has_login_password: false, publish_selector: '', is_active: true, tags: [], config_jsonb: {}, notes: null, last_login_check_at: null, last_login_check_status: null, last_login_check_message: null, created_at: '2026-09-18', updated_at: '2026-09-18', ...body };
        accounts.push(row); return reply(row);
      }
      if (path === '/api/accounts/qa-account' && method === 'PATCH') {
        accounts[0] = { ...accounts[0], ...body }; return reply(accounts[0]);
      }
      if (path.endsWith('/strategy')) { profile = body.strategy_profile; return reply({ status: 'ok', account: accounts[0], strategy_profile: profile, feedback_plan: feedback }); }
      if (path.endsWith('/prompt-versions')) {
        const row = { ...body, id: 'qa-prompt', account_id: 'qa-account', created_at: '2026-09-18', updated_at: '2026-09-18' };
        versions = [row, ...versions]; return reply(row);
      }
      if (path.endsWith('/activate')) { versions[0].status = 'active'; return reply(versions[0]); }
      throw new Error(`Unexpected write: ${method} ${path}`);
    }
    if (path === '/api/accounts') return reply({ items: accounts });
    if (path.endsWith('/strategy')) {
      if (holdStrategy && path.includes('/qa-account/')) await new Promise(resolve => { releaseStrategy = resolve; });
      return reply({ status: 'ok', strategy_profile: path.includes('/qa-second/') ? { ...profile, persona_name: 'Second persona' } : profile, feedback_plan: feedback });
    }
    if (path.endsWith('/runtime')) return runtimeFailure ? reply({ detail: 'unavailable' }, 503) : reply({ execution: {}, latest: {}, collection_plan: {} });
    if (path.endsWith('/loop/status')) return reply({ loop: { blockers: [], completion_score: 0 } });
    if (path.endsWith('/prompt-versions')) return reply({ items: versions });
    return reply({ status: 'ok', items: [], nodes: [], sections: [], steps: [], completion_score: 0, switch_rules: [] });
  });
  await page.goto('http://127.0.0.1:3000/accounts', { waitUntil: 'domcontentloaded' });
  const create = page.getByRole('button', { name: '新增账号', exact: true });
  await create.waitFor({ timeout: 10000 });
  await create.click();
  const dialog = page.getByRole('dialog');
  await dialog.getByLabel('账号名称', { exact: true }).fill('   ');
  await dialog.getByRole('button', { name: '保存', exact: true }).click();
  assert(writes.length === 0, 'Whitespace-only name must not reach API');
  await dialog.getByText('请输入账号名称。', { exact: true }).waitFor();
  await dialog.getByLabel('账号名称', { exact: true }).fill('QA account');
  holdWrite = true;
  await dialog.getByRole('button', { name: '保存', exact: true }).click();
  await dialog.getByRole('button', { name: '保存中...', exact: true }).waitFor();
  assert(await dialog.getByLabel('账号名称', { exact: true }).isDisabled(), 'Saving must lock form inputs');
  await page.keyboard.press('Escape');
  assert(await dialog.isVisible(), 'A pending save must not dismiss the dialog');
  holdWrite = false;
  releaseWrite();
  await dialog.waitFor({ state: 'hidden' });
  assert(writes[0].body.account_name === 'QA account', 'Create must send account name');
  await page.getByRole('button', { name: '编辑账号', exact: true }).click();
  await dialog.getByLabel('账号名称', { exact: true }).fill('Renamed account');
  failNext = true;
  await dialog.getByRole('button', { name: '保存', exact: true }).click();
  await dialog.getByRole('alert').waitFor();
  assert(await dialog.getByLabel('账号名称', { exact: true }).inputValue() === 'Renamed account', 'Failed save must preserve input');
  await dialog.getByRole('button', { name: '保存', exact: true }).click();
  await dialog.waitFor({ state: 'hidden' });
  assert(!('login_password' in writes.at(-1).body), 'Editing must not clear a saved password');
  assert(!('config_jsonb' in writes.at(-1).body), 'Editing basics must not replace configuration');
  const active = page.getByRole('switch', { name: '账号启用', exact: true });
  await active.click();
  await page.waitForFunction(() => document.querySelector('[role="switch"]')?.getAttribute('aria-checked') === 'false');
  assert(writes.at(-1).body.is_active === false, 'Toggle must persist inactive state');
  assert(await page.getByRole('button', { name: '运行今日闭环', exact: true }).isDisabled(), 'Inactive accounts must not start a daily loop');
  failNext = true;
  await active.click();
  await page.getByRole('main').getByRole('alert').waitFor();
  assert(await active.getAttribute('aria-checked') === 'false', 'Failed toggle must retain the last saved state');
  await page.getByRole('button', { name: '编辑定位与策略', exact: true }).click();
  await dialog.getByLabel('人设名称', { exact: true }).fill('Updated persona');
  await dialog.getByRole('button', { name: '保存', exact: true }).click();
  await dialog.waitFor({ state: 'hidden' });
  const strategyWrite = writes.find(write => write.path.endsWith('/strategy'));
  assert(strategyWrite.body.strategy_profile.persona_name === 'Updated persona', 'Strategy edits must persist');
  assert(strategyWrite.body.strategy_profile.prompt_overrides.copy === 'preserved override', 'Hidden prompt overrides must survive strategy save');
  assert(strategyWrite.body.strategy_profile.mcp_limit_default === 17, 'Collection configuration must survive strategy save');
  assert(JSON.stringify(strategyWrite.body.feedback_plan) === JSON.stringify(feedback), 'Feedback configuration must survive strategy save');
  await page.getByRole('button', { name: '新增提示词版本', exact: true }).click();
  await dialog.getByLabel('版本名称', { exact: true }).fill('qa-v1');
  await dialog.getByLabel('提示词全文', { exact: true }).fill('Use verified facts.');
  await dialog.getByRole('button', { name: '保存草稿', exact: true }).click();
  await dialog.waitFor({ state: 'hidden' });
  assert(writes.at(-1).body.status === 'draft', 'New prompt must remain draft until activation');
  await page.getByRole('button', { name: '编辑为新版本', exact: true }).click();
  assert(await dialog.getByLabel('提示词全文', { exact: true }).inputValue() === 'Use verified facts.', 'Prompt editing must start from the full source text');
  assert(await dialog.getByLabel('版本名称', { exact: true }).inputValue() === '', 'Editing a prompt must require a new version name');
  await dialog.getByRole('button', { name: '取消', exact: true }).click();
  await page.getByRole('button', { name: '设为当前版本', exact: true }).click();
  await page.getByText('当前生效', { exact: true }).waitFor();
  assert(writes.at(-1).path.endsWith('/qa-prompt/activate'), 'Activation must target the chosen version');
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole('button', { name: '编辑账号', exact: true }).click();
  const bounds = await dialog.boundingBox();
  assert(bounds.x >= 0 && bounds.x + bounds.width <= 390 && bounds.height <= 844, 'Dialog must fit mobile viewport');
  await page.screenshot({ path: 'output/playwright/accountqa-mobile.png' });
  await dialog.getByRole('button', { name: '取消', exact: true }).click();
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.getByRole('main').getByRole('heading', { name: '账号管理', exact: true }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: 'output/playwright/accountqa-desktop.png' });
  accounts[0] = { ...accounts[0], login_mode: 'credential', login_username: 'saved-user', has_login_password: true };
  runtimeFailure = true;
  await page.goto('http://127.0.0.1:3000/accounts?panel=profile', { waitUntil: 'domcontentloaded' });
  await page.getByRole('button', { name: '编辑定位与策略', exact: true }).waitFor();
  assert(await page.getByRole('button', { name: '编辑定位与策略', exact: true }).isEnabled(), 'An unrelated detail failure must not block strategy editing');
  await page.getByRole('button', { name: '编辑账号', exact: true }).click();
  assert(await dialog.getByLabel('新密码（留空保留原密码）', { exact: true }).inputValue() === '', 'Saved password must never be rendered');
  await dialog.getByRole('button', { name: '保存', exact: true }).click();
  await dialog.waitFor({ state: 'hidden' });
  assert(!('login_password' in writes.at(-1).body), 'An unchanged credential password must be omitted');
  runtimeFailure = false;
  for (const panel of ['profile', 'strategy', 'settings', 'security', 'knowledge']) {
    await page.evaluate(panel => window.history.pushState(null, '', `/accounts?panel=${panel}`), panel);
    await page.getByRole('button', { name: '新增提示词版本', exact: true }).waitFor();
    await page.locator(`#${panel}`).waitFor();
    assert(await page.locator(`#${panel}`).isVisible(), `Panel ${panel} must exist and be open`);
  }
  accounts.push({ ...accounts[0], id: 'qa-second', account_name: 'Second account' });
  holdStrategy = true;
  await page.getByRole('button', { name: '刷新', exact: true }).click();
  // Account selection stays available while the previous account's detail request is pending.
  await page.getByRole('button', { name: /Second account/ }).click();
  await page.getByText('人设：Second persona', { exact: true }).waitFor();
  holdStrategy = false;
  releaseStrategy();
  await page.getByRole('button', { name: '刷新', exact: true }).waitFor({ state: 'visible' });
  await page.getByRole('button', { name: '编辑定位与策略', exact: true }).click();
  assert(await dialog.getByLabel('人设名称', { exact: true }).inputValue() === 'Second persona', 'Late responses must not replace selected account configuration');
  await dialog.getByRole('button', { name: '取消', exact: true }).click();
  accounts = [];
  await page.goto('http://127.0.0.1:3000/accounts?panel=knowledge', { waitUntil: 'domcontentloaded' });
  await page.getByRole('region', { name: '知识工作台' }).waitFor();
  assert(await page.getByRole('combobox', { name: '知识范围' }).inputValue() === 'all', 'Knowledge workspace must support global records without an account');
  assert(pageErrors.length === 0, `Unexpected page errors: ${pageErrors.join('; ')}`);
  return { passed: true, writes: writes.length, checks: ['create', 'validation', 'saving-lock', 'failed-save-retry', 'password-preservation', 'active-toggle-success-and-failure', 'inactive-run-guard', 'strategy-preservation', 'prompt-draft', 'prompt-edit-copy', 'prompt-activation', 'mobile-dialog', 'partial-detail-failure', 'panel-links', 'account-selection-race', 'global-knowledge-without-account'] };
}
