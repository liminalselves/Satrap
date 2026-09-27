import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';
import { createServer } from 'vite';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const artifacts = path.join(root, 'test-results', 'groups');
const server = await createServer({ root, server: { host: '127.0.0.1', port: 0 } });
let browser;
try {
  await fs.mkdir(artifacts, { recursive: true });
  await server.listen();
  const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
  browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const writes = [];
  const decisions = [];
  const row = { group_id: '456', group_name: '测试交流群', member_count: 42, max_member_count: 500,
    membership: 'joined', confirmed_at: 1_790_000_000, response_enabled: false, response_source: 'account' };
  let config = {
    account: '100', current_account: '100', group: row, explicit: { policy: {}, session: {}, events: {}, approval: {} },
    revision: 0, saved_revision: 0, active_revision: 0, apply_status: 'applied', route_generation: 0,
    base_revision: 'base-1',
    effective: { policy: { enabled: false, wake_mode: 'necessity' },
      session: { binding: { provider: 'edictum', config_name: 'simple' }, scope: 'legacy_user', model: 'base', prompt: '原提示词', plugins: [] },
      approval: { kick_group_member: 'approval_required' }, events: { group_ban: true } },
    sources: { policy: { enabled: { source: 'account', source_label: '账号接入模式', source_index: null } },
      session: { binding: 'platform', scope: 'platform', model: 'named_config', prompt: 'named_config' },
      approval: { kick_group_member: 'default' }, events: { group_ban: 'default' } },
    capabilities: { policy_fields: ['enabled'], session_fields: ['binding', 'scope', 'model', 'prompt', 'plugins'],
      approval_actions: ['kick_group_member'], event_kinds: ['group_ban'] },
  };
  let conflict = false;
  let actionState = 'pending';
  await context.route(`${origin}/ui-config.json`, (route) => route.fulfill({ json: {
    backend_api: 'http://127.0.0.1:19870', control_api: 'http://127.0.0.1:19871', chat_api: 'http://127.0.0.1:19872',
  } }));
  await context.route(/^http:\/\/127\.0\.0\.1:1987[012]\//, async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const pathname = url.pathname;
    const headers = { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true',
      'Access-Control-Allow-Headers': 'Content-Type', 'Access-Control-Allow-Methods': 'GET, POST, PATCH, PUT, DELETE, OPTIONS' };
    if (request.method() === 'OPTIONS') return route.fulfill({ status: 204, headers });
    const reply = (json, status = 200) => route.fulfill({ status, json, headers });
    if (pathname === '/api/platforms/ob/groups/456/config' && request.method() === 'PATCH') {
      const payload = request.postDataJSON();
      writes.push(payload);
      if (conflict) return reply({ error: '群配置已变化', code: 'revision_conflict' }, 409);
      assert.equal(payload.expected_self_id, '100');
      assert.equal(payload.expected_revision, config.revision);
      config = structuredClone(config);
      config.explicit[payload.section] = payload.values;
      config.revision += 1;
      config.saved_revision = config.revision;
      config.active_revision = config.revision;
      if (payload.section === 'session') {
        for (const key of ['model', 'prompt', 'plugins']) {
          if (payload.values[key]?.mode === 'value') {
            config.effective.session[key] = payload.values[key].value;
            config.sources.session[key] = 'group';
          }
        }
      }
      return reply(config);
    }
    if (pathname.endsWith('/actions/action-123456/decision') && request.method() === 'POST') {
      decisions.push(request.postDataJSON());
      actionState = 'succeeded';
      return reply({ action_id: 'action-123456', state: actionState });
    }
    if (pathname === '/api/platforms/ob/groups/456/actions') return reply({
      items: [{ action_id: 'action-123456', self_id: '100', group_id: '456', action_type: 'kick_group_member',
        params: { user_id: '42' }, actor_kind: 'model', state: actionState,
        created_at: 1_790_000_000, expires_at: 1_790_000_600, decision_at: null, executed_at: null, result: null }],
      total: 1, page: 1, page_size: 25,
    });
    if (pathname === '/api/platforms/ob/groups/456/config') return reply({ ...config, account: url.searchParams.get('account') || '100' });
    if (pathname === '/api/platforms/ob/groups/456/events') return reply({ items: [], volatile: true, capacity: 4096, truncated: false });
    if (pathname === '/api/platforms/ob/groups/456/diagnostics') return reply({ records: [], available: true });
    if (pathname === '/api/platforms/ob/groups/456/action-types') return reply({ items: [] });
    if (pathname === '/api/platforms/ob/groups/bindings') return reply({ account: '100',
      items: [{ provider: 'edictum', config_name: 'simple', enabled: true, available: true,
        description: '简单会话', session_fields: ['binding', 'scope', 'model', 'prompt', 'plugins'] }],
      models: ['base', 'other'], plugins: [{ name: 'search', description: '搜索', config_schema: {
        limit: { type: 'number', description: '上限', session_overridable: true, integer: true },
      } }],
    });
    if (pathname === '/api/platforms/ob/groups/456/info') return reply({ group_name: row.group_name, member_count: 42, max_member_count: 500 });
    if (pathname === '/api/platforms/ob/groups/456/members') return reply({ items: [], total_loaded: 0, page: 1, page_size: 25, truncated: false, complete: true });
    if (pathname === '/api/platforms/ob/groups/settings') return reply({ self_id: '100', mode: 'selected', approval_defaults: {},
      revision: 1, migrated_at: 1_790_000_000, last_bound_at: 1_790_000_000, legacy_adopted: true, current: true });
    if (pathname === '/api/platforms/ob/groups/accounts') return reply({
      items: [{ self_id: '100', mode: 'selected', revision: 1, last_bound_at: 1_790_000_000 },
        { self_id: '101', mode: 'selected', revision: 1, last_bound_at: 1_790_000_000 }],
      current_account: '100', waiting_for_account: false,
    });
    if (pathname === '/api/platforms/ob/groups') return reply({ items: [row], total: 1, page: 1, page_size: 25,
      counts: { joined: 1, response_enabled: 0, configured: 0 }, account: url.searchParams.get('account') || '100',
      current_account: '100', account_generation: 1,
      sync: { status: 'complete', sync_id: 'sync-1', complete: true, truncated: false, reason: null,
        started_at: 1_790_000_000, completed_at: 1_790_000_001, last_complete_at: 1_790_000_001, connection_generation: 1 } });
    const defaults = {
      '/api/health': { running: true, adapters: { ob: { config_type: 'onebot', status: 'running', started: true } } },
      '/status': { running: true }, '/api/sessions': { sessions: [] }, '/config/session-instances': { sessions: [] },
    };
    return reply(defaults[pathname] || { ok: true });
  });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.routeWebSocket('ws://127.0.0.1:19870/ws/status**', () => {});
  await page.goto(`${origin}/platforms/ob/groups?account=100`);
  await page.getByText('测试交流群').last().waitFor();
  assert.equal(await page.getByText('关闭', { exact: true }).count() > 0, true);
  for (const theme of ['dark', 'light']) {
    await page.evaluate((value) => document.documentElement.setAttribute('data-theme', value), theme);
    await page.screenshot({ animations: 'disabled', path: path.join(artifacts, `${theme}-list.png`) });
  }
  await page.getByRole('link', { name: '进入' }).click();
  await page.getByRole('link', { name: '会话配置' }).click();
  await page.getByText('模型与提示词').waitFor();
  await page.locator('label:has-text("模型来源") select').first().selectOption('value');
  await page.locator('label:has-text("模型来源") select').last().selectOption('other');
  await page.locator('label:has-text("系统提示词") select').selectOption('value');
  await page.locator('label:has-text("系统提示词") textarea').fill('');
  await page.locator('label:has-text("search") select').selectOption('disabled');
  await page.getByRole('button', { name: '保存并应用' }).click();
  await Promise.all([
    page.waitForResponse((response) => response.url().includes('/groups/456/config') && response.request().method() === 'PATCH'),
    page.getByRole('button', { name: '确认保存覆盖' }).click(),
  ]);
  assert.equal(writes.at(-1).section, 'session');
  assert.deepEqual(writes.at(-1).values.prompt, { mode: 'value', value: '' });
  assert.deepEqual(writes.at(-1).values.model, { mode: 'value', value: 'other' });
  assert.deepEqual(writes.at(-1).values.plugins, { mode: 'value', value: [{ name: 'search', mode: 'disabled', config: {} }] });
  for (const theme of ['dark', 'light']) {
    await page.evaluate((value) => document.documentElement.setAttribute('data-theme', value), theme);
    await page.screenshot({ animations: 'disabled', path: path.join(artifacts, `${theme}-session.png`) });
  }
  await page.getByRole('link', { name: '审批与记录' }).click();
  await page.getByText('action-123456').waitFor();
  await page.getByRole('button', { name: '批准' }).click();
  assert.equal(decisions.length, 1);
  assert.equal(decisions[0].expected_self_id, '100');
  await page.getByRole('button', { name: '刷新' }).click();
  for (const theme of ['dark', 'light']) {
    await page.evaluate((value) => document.documentElement.setAttribute('data-theme', value), theme);
    await page.screenshot({ animations: 'disabled', path: path.join(artifacts, `${theme}-actions.png`) });
  }
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ animations: 'disabled', path: path.join(artifacts, 'narrow-actions.png') });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1), true);
  conflict = true;
  await page.goto(`${origin}/platforms/ob/groups/456/session?account=100`);
  await page.getByText('模型与提示词').waitFor();
  await page.locator('label:has-text("系统提示词") textarea').fill('未保存草稿');
  await page.getByRole('button', { name: '保存并应用' }).click();
  await Promise.all([
    page.waitForResponse((response) => response.url().includes('/groups/456/config') && response.request().method() === 'PATCH'),
    page.getByRole('button', { name: '确认保存覆盖' }).click(),
  ]);
  await page.getByText('群配置已变化').waitFor();
  assert.equal(await page.locator('label:has-text("系统提示词") textarea').inputValue(), '未保存草稿');
  assert.deepEqual(errors, []);
  console.log(`groups E2E passed; screenshots: ${artifacts}`);
} finally {
  if (browser) await browser.close();
  await server.close();
}
