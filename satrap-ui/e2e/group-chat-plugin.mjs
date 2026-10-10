import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { readFile } from 'node:fs/promises';
import { load } from 'js-yaml';
import { chromium } from 'playwright';
import { createServer } from 'vite';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const meta = load(await readFile(path.join(root, '../satrap/expend/plugins/group_chat/meta.yaml'), 'utf8'));
const schema = Object.fromEntries(Object.entries(meta.config_schema).map(([key, value]) => [key, { options: [], required: false, nullable: false, session_overridable: true, ...value }]));
const defaults = Object.fromEntries(Object.entries(schema).map(([key, value]) => [key, value.default]));
const plugin = { ...meta, config_schema: schema, source: 'builtin', usage_count: 0, edictum_configs: [], chat_enabled: false,
  capabilities: Object.fromEntries(['tools', 'skills', 'handlers', 'commands', 'mcp'].map((kind) => [kind, meta[kind] || {}])) };
const server = await createServer({ root, server: { host: '127.0.0.1', port: 0 } });
let browser;
try {
  await server.listen();
  const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
  const cors = { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true', 'Access-Control-Allow-Headers': 'Content-Type', 'Access-Control-Allow-Methods': 'GET,PUT,OPTIONS' };
  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  const errors = [];
  const writes = [];
  let overrides = {};
  let revision = 'initial';
  page.on('pageerror', (error) => errors.push(error.message));
  await page.route(origin + '/ui-config.json', (route) => route.fulfill({ json: { backend_api: 'http://127.0.0.1:19870', control_api: 'http://127.0.0.1:19871', chat_api: 'http://127.0.0.1:19872' } }));
  await page.route(/^http:\/\/127\.0\.0\.1:1987[012]\//, (route) => {
    if (route.request().method() === 'OPTIONS') return route.fulfill({ status: 204, headers: cors });
    const pathname = new URL(route.request().url()).pathname;
    if (pathname === '/config/plugins') return route.fulfill({ json: { plugins: [plugin] }, headers: cors });
    if (pathname === '/config/plugins/group_chat/config') {
      if (route.request().method() === 'PUT') {
        const body = route.request().postDataJSON();
        assert.equal(body.expected_revision, revision);
        overrides = body.config;
        writes.push(overrides);
        revision += '-saved';
      }
      return route.fulfill({ json: { ok: true, schema, defaults, overrides, effective: { ...defaults, ...overrides }, revision, runtime: [] }, headers: cors });
    }
    if (pathname === '/config/plugin-model-options') return route.fulfill({ json: { options: {} }, headers: cors });
    if (pathname === '/config/rag/knowledge-bases') return route.fulfill({ json: { knowledge_bases: [] }, headers: cors });
    return route.fulfill({ json: { ok: true, running: false, adapters: {} }, headers: cors });
  });
  await page.routeWebSocket(/ws:\/\/127\.0\.0\.1:1987[012]\//, () => {});
  await page.goto(origin + '/plugins/group_chat');
  await page.getByRole('heading', { name: 'group_chat', exact: true }).waitFor();
  for (const name of Object.keys(meta.tools)) await page.getByText(name, { exact: true }).waitFor();
  await page.getByText('group_chat.environment', { exact: true }).waitFor();
  await page.getByRole('button', { name: '全局参数', exact: true }).click();
  for (const [name, field] of Object.entries(schema)) {
    const input = page.getByLabel(name, { exact: true });
    await input.waitFor();
    if (field.type === 'bool') {
      assert.equal(await input.isChecked(), field.default);
      continue;
    }
    assert.equal(await input.inputValue(), String(field.default));
    if (field.type === 'textarea') continue;
    assert.equal(await input.getAttribute('min'), String(field.minimum));
    assert.equal(await input.getAttribute('max'), String(field.maximum));
    assert.equal(await input.getAttribute('step'), '1');
  }
  await page.getByLabel('member_cache_ttl', { exact: true }).fill('0');
  await page.getByLabel('message_limit', { exact: true }).fill('25');
  await page.getByLabel('cross_group_query_callers', { exact: true }).fill('123\n789');
  await page.getByLabel('allowed_groups', { exact: true }).fill('456');
  await page.getByLabel('self_nickname_enabled', { exact: true }).check();
  await page.getByLabel('nickname_allowed_callers', { exact: true }).fill('123');
  await page.getByRole('button', { name: '保存全局参数', exact: true }).click();
  await page.getByText('全局参数已保存', { exact: true }).waitFor();
  assert.deepEqual(writes, [{ member_cache_ttl: 0, message_limit: 25, cross_group_query_callers: '123\n789',
    allowed_groups: '456', self_nickname_enabled: true, nickname_allowed_callers: '123' }]);
  await page.reload();
  await page.getByRole('button', { name: '全局参数', exact: true }).click();
  await page.getByLabel('message_limit', { exact: true }).waitFor();
  assert.equal(await page.getByLabel('message_limit', { exact: true }).inputValue(), '25');
  assert.equal(await page.getByLabel('cross_group_query_callers', { exact: true }).inputValue(), '123\n789');
  assert.equal(await page.getByLabel('allowed_groups', { exact: true }).inputValue(), '456');
  assert.equal(await page.getByLabel('nickname_allowed_callers', { exact: true }).inputValue(), '123');
  assert.equal(await page.getByLabel('self_nickname_enabled', { exact: true }).isChecked(), true);
  await page.getByRole('button', { name: '恢复默认值', exact: true }).click();
  await page.getByRole('button', { name: '保存全局参数', exact: true }).click();
  await page.getByText('全局参数已保存', { exact: true }).waitFor();
  assert.deepEqual(writes.at(-1), {});
  await page.setViewportSize({ width: 390, height: 844 });
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
  assert.deepEqual(errors, []);
  console.log('PASS: group_chat 实际元数据的能力展示, 查询与摘要配置, 零值保存, 刷新持久值, 恢复默认及窄屏布局');
} finally {
  if (browser) await browser.close();
  await server.close();
}
