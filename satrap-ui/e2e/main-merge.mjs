import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { readFile } from 'node:fs/promises';
import { load } from 'js-yaml';
import { chromium } from 'playwright';
import { createServer } from 'vite';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const meta = load(await readFile(path.join(root, '../satrap/expend/plugins/group_admin/meta.yaml'), 'utf8'));
const schema = Object.fromEntries(Object.entries(meta.config_schema).map(([key, value]) => [key, {
  options: [], required: false, nullable: false, session_overridable: true, ...value,
}]));
const defaults = Object.fromEntries(Object.entries(schema).map(([key, value]) => [key, value.default]));
const plugin = { ...meta, config_schema: schema, source: 'builtin', usage_count: 0, edictum_configs: [], chat_enabled: false,
  capabilities: Object.fromEntries(['tools', 'skills', 'handlers', 'commands', 'mcp'].map(kind => [kind, meta[kind] || {}])) };
const server = await createServer({ root, server: { host: '127.0.0.1', port: 0 } });
let browser;
try {
  await server.listen();
  const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
  const cors = { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true',
    'Access-Control-Allow-Headers': 'Content-Type', 'Access-Control-Allow-Methods': 'GET,PUT,POST,OPTIONS' };
  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  const errors = [];
  const writes = [];
  const decisions = [];
  let overrides = {};
  let revision = 'initial';
  const action = { action_id: 'model-approval-1', self_id: '100', group_id: '456', action_type: 'ban_group_member',
    params: { user_id: '123', duration: 60 }, actor_kind: 'model', state: 'pending', policy_revision: 1,
    created_at: Date.now() / 1000, expires_at: Date.now() / 1000 + 600, decision_at: null, executed_at: null, result: null };
  const config = { account: '100', current_account: '100', group: { group_id: '456', group_name: '合并验收群', membership: 'joined' },
    explicit: { policy: {}, session: {}, events: {}, approval: {} }, revision: 0, saved_revision: 0, active_revision: 0,
    apply_status: 'applied', route_generation: 0, base_revision: 'base', effective: { policy: { enabled: true }, session: {}, approval: {}, events: {} },
    sources: { policy: {}, session: {}, approval: {}, events: {} },
    capabilities: { policy_fields: [], session_fields: [], approval_actions: [], event_kinds: [] } };
  page.on('pageerror', error => errors.push(error.message));
  await page.route(origin + '/ui-config.json', route => route.fulfill({ json: {
    backend_api: 'http://127.0.0.1:19870', control_api: 'http://127.0.0.1:19871', chat_api: 'http://127.0.0.1:19872',
  } }));
  await page.route(/^http:\/\/127\.0\.0\.1:1987[012]\//, route => {
    const url = new URL(route.request().url());
    const pathname = url.pathname;
    const reply = json => route.fulfill({ json, headers: cors });
    if (route.request().method() === 'OPTIONS') return route.fulfill({ status: 204, headers: cors });
    if (pathname === '/config/plugins') return reply({ plugins: [plugin] });
    if (pathname === '/config/plugins/group_admin/config') {
      if (route.request().method() === 'PUT') {
        const body = route.request().postDataJSON();
        assert.equal(body.expected_revision, revision);
        overrides = body.config;
        writes.push(overrides);
        revision += '-saved';
      }
      return reply({ ok: true, schema, defaults, overrides, effective: { ...defaults, ...overrides }, revision, runtime: [] });
    }
    if (pathname === '/api/platforms/ob/groups/456/config') return reply(config);
    if (pathname.endsWith('/actions/model-approval-1/decision')) {
      const body = route.request().postDataJSON();
      assert.equal(body.expected_self_id, '100');
      assert.equal(body.approve, true);
      decisions.push(body);
      action.state = 'succeeded';
      return reply(action);
    }
    if (pathname === '/api/platforms/ob/groups/456/actions') {
      const items = url.searchParams.get('state') === 'all' || url.searchParams.get('state') === action.state ? [action] : [];
      return reply({ items, total: items.length, page: 1, page_size: 25 });
    }
    if (pathname === '/config/plugin-model-options') return reply({ options: {} });
    if (pathname === '/config/rag/knowledge-bases') return reply({ knowledge_bases: [] });
    return reply({ ok: true, running: true, adapters: { ob: { config_type: 'onebot', status: 'running', started: true } } });
  });
  await page.routeWebSocket(/ws:\/\/127\.0\.0\.1:1987[012]\//, () => {});
  await page.goto(origin + '/plugins/group_admin');
  await page.getByRole('heading', { name: 'group_admin', exact: true }).waitFor();
  assert.equal(await page.getByText('group_admin_set_card', { exact: true }).count(), 0);
  assert.equal(await page.getByText('group_admin_get_group_info', { exact: true }).count(), 0);
  await page.getByRole('button', { name: '全局参数', exact: true }).click();
  await page.getByLabel('write_tools_enabled', { exact: true }).check();
  await page.getByLabel('allowed_callers', { exact: true }).fill('123');
  await page.getByLabel('allowed_read_callers', { exact: true }).fill('123\n789');
  await page.getByLabel('high_risk_approval', { exact: true }).check();
  await page.getByRole('button', { name: '保存全局参数', exact: true }).click();
  await page.getByText('全局参数已保存', { exact: true }).waitFor();
  assert.deepEqual(writes, [{ write_tools_enabled: true, allowed_callers: '123', allowed_read_callers: '123\n789', high_risk_approval: true }]);
  await page.reload();
  await page.getByRole('button', { name: '全局参数', exact: true }).click();
  assert.equal(await page.getByLabel('high_risk_approval', { exact: true }).isChecked(), true);
  assert.equal(await page.getByLabel('allowed_read_callers', { exact: true }).inputValue(), '123\n789');
  await page.goto(origin + '/platforms/ob/groups/456/actions?account=100');
  await page.getByText('ID: model-approval-1', { exact: true }).waitFor();
  assert.equal(await page.getByRole('button', { name: '批准', exact: true }).count(), 1);
  await page.getByRole('button', { name: '批准', exact: true }).click();
  await page.getByText('共 0 条', { exact: true }).waitFor();
  await page.locator('select').filter({ has: page.locator('option[value="pending"]') }).selectOption('all');
  await page.getByText('ID: model-approval-1', { exact: true }).waitFor();
  assert.ok((await page.locator('main').innerText()).includes('已成功'));
  assert.equal(decisions.length, 1);
  assert.equal(await page.getByRole('button', { name: '批准', exact: true }).count(), 0);
  await page.setViewportSize({ width: 390, height: 844 });
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
  assert.deepEqual(errors, []);
  console.log('PASS: 实际 group_admin 新配置保存回填, 无旧工具, 单一模型持久审批及窄屏');
} finally {
  if (browser) await browser.close();
  await server.close();
}
