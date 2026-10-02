/** Agent 路由浏览器回归, 覆盖冷目录, 额外类型和草稿保留 */
import assert from 'node:assert/strict';
import path from 'node:path';
import fs from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';
import { createServer } from 'vite';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const artifacts = path.join(root, 'test-results', 'agent-routing');
const server = await createServer({ root, server: { host: '127.0.0.1', port: 0 } });
let browser;
let page;
try {
  await fs.mkdir(artifacts, { recursive: true });
  await server.listen();
  const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
  browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  let revision = 1;
  let running = false;
  let applicationStatus = 'failed';
  let applications = [];
  let platforms = [{ id: 'future-bot', type: 'future', settings: { extension: 'kept' } }];
  const writes = [];
  const requests = [];
  const edictum = Object.fromEntries(['personal', 'group-agent', 'disabled'].map((name) => [name, {
    provider: 'edictum', edictum_type: 'simple', enabled: name !== 'disabled', model_name: 'chat', description: '', params: {},
    plugins: [{ name: 'example', enabled: true }],
  }]));
  const adapterTypes = [
    { type: 'future', status: 'available', display_name: '未来平台', conversation_kinds: { private: '私聊', group: '群聊', topic: '话题' } },
    { type: 'other-future', status: 'available', display_name: '另一平台', conversation_kinds: { board: '看板' } },
  ];
  await context.route(`${origin}/ui-config.json`, (route) => route.fulfill({ json: {
    backend_api: 'http://127.0.0.1:19870', control_api: 'http://127.0.0.1:19871', chat_api: 'http://127.0.0.1:19872',
  } }));
  await context.route(/^http:\/\/127\.0\.0\.1:1987[012]\//, async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const headers = { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true', 'Access-Control-Allow-Headers': '*', 'Access-Control-Allow-Methods': '*' };
    if (request.method() === 'OPTIONS') return route.fulfill({ status: 204, headers });
    requests.push(url.pathname);
    if (url.pathname.startsWith('/config/platforms')) {
      if (request.method() === 'PUT') {
        if (url.searchParams.get('expected_revision') !== String(revision)) return route.fulfill({ status: 409, headers, json: { ok: false, code: 'config_revision_conflict' } });
        const payload = request.postDataJSON();
        writes.push(payload);
        platforms = [payload];
        revision++;
      }
      return route.fulfill({ headers, json: { ok: true, platforms, revision: String(revision), adapter_types: adapterTypes, default_session_type: 'flow' } });
    }
    if (url.pathname === '/api/config/reload') {
      assert.equal(request.postDataJSON().expected_config_revision, String(revision));
      applications = [{ id: 'future-bot', status: applicationStatus, saved_revision: String(revision), active_revision: applicationStatus === 'applied' ? String(revision) : 'old', error: applicationStatus === 'failed' ? '模拟应用失败' : undefined }];
      return route.fulfill({ headers, json: { ok: true, platforms: applications } });
    }
    const responses = {
      '/auth/session': { ok: true }, '/status': { running },
      '/api/health': { running, adapters: {}, platform_config: applications },
      '/config/session-classes': { flow: { class_path: 'example.Flow', enabled: true, model_key: 'model_name', params: { model_name: 'flow-model' } } },
      '/config/edictum/sessions': edictum,
      '/api/sessions': { sessions: [] }, '/config/session-instances': { sessions: [] },
      '/api/diagnostics': { records: [], available: false }, '/config/models/asr': {},
    };
    return route.fulfill({ headers, json: responses[url.pathname] || { ok: true } });
  });
  page = await context.newPage();
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.routeWebSocket('ws://127.0.0.1:19870/ws/status**', () => {});
  await page.routeWebSocket('ws://127.0.0.1:19872/ws/chat**', () => {});
  await page.goto(`${origin}/platforms`);
  await page.getByTitle('编辑', { exact: true }).click();
  const dialog = page.getByRole('dialog');
  const value = (provider, name) => JSON.stringify([provider, name]);
  const privateMode = dialog.getByLabel('私聊 Agent 来源', { exact: true });
  const groupMode = dialog.getByLabel('群聊 Agent 来源', { exact: true });
  const topicMode = dialog.getByLabel('话题 Agent 来源', { exact: true });
  const refresh = async () => Promise.all([
    page.waitForResponse((response) => new URL(response.url()).pathname === '/config/platforms'),
    page.waitForResponse((response) => new URL(response.url()).pathname === '/api/health'),
    page.getByRole('button', { name: '刷新', exact: true }).click(),
  ]);
  assert.equal(await topicMode.inputValue(), 'inherit');
  await dialog.getByText('实际配置: SessionClass / flow', { exact: true }).first().waitFor();

  // 只保存其他字段不能自动启用类型路由
  await dialog.getByRole('button', { name: '保存修改' }).click();
  await dialog.waitFor({ state: 'hidden' });
  assert.equal('session_bindings' in writes[0], false);
  assert.equal(requests.includes('/api/config/reload'), false);
  await page.getByTitle('编辑', { exact: true }).click();
  await privateMode.selectOption('value');
  await dialog.getByRole('button', { name: '保存修改' }).click();
  await page.getByText('私聊 Agent 必须选择命名配置', { exact: true }).waitFor();
  assert.equal(writes.length, 1);
  await dialog.getByLabel('私聊 Agent 配置', { exact: true }).selectOption(value('edictum', 'personal'));
  await groupMode.selectOption('value');
  const groupSelect = dialog.getByLabel('群聊 Agent 配置', { exact: true });
  assert.equal(await groupSelect.locator('option').filter({ hasText: 'disabled' }).evaluate((option) => option.disabled), true);
  await groupSelect.selectOption(value('edictum', 'group-agent'));
  await topicMode.selectOption('value');
  await dialog.getByLabel('话题 Agent 配置', { exact: true }).selectOption(value('session_class', 'flow'));
  await dialog.getByText('模型: flow-model · 插件: 由流程决定', { exact: true }).waitFor();
  await dialog.getByText('模型: chat · 插件: 1', { exact: true }).first().waitFor();
  await page.screenshot({ animations: 'disabled', path: path.join(artifacts, 'desktop.png') });
  await dialog.getByRole('button', { name: '保存修改' }).click();
  await dialog.waitFor({ state: 'hidden' });
  assert.deepEqual(writes.at(-1).session_bindings, {
    private: { mode: 'value', provider: 'edictum', config_name: 'personal' },
    group: { mode: 'value', provider: 'edictum', config_name: 'group-agent' },
    topic: { mode: 'value', provider: 'session_class', config_name: 'flow' },
  });
  assert.equal(writes.at(-1).settings.extension, 'kept');

  // 切换平台类型保留原路由, 并明确要求处理不再支持的类型
  await page.getByTitle('编辑', { exact: true }).click();
  await dialog.getByLabel('类型', { exact: true }).selectOption('other-future');
  await dialog.getByLabel('看板 Agent 来源', { exact: true }).waitFor();
  assert.equal(await dialog.getByLabel('private Agent 配置', { exact: true }).inputValue(), value('edictum', 'personal'));
  await dialog.getByRole('button', { name: '保存修改' }).click();
  await page.getByText('private 已不受当前适配器支持, 请删除该绑定', { exact: true }).waitFor();
  await dialog.getByLabel('类型', { exact: true }).selectOption('future');

  // 并发冲突保留恢复继承后的草稿, 取消关闭也保留
  await groupMode.selectOption('inherit');
  revision++;
  await dialog.getByRole('button', { name: '保存修改' }).click();
  await page.getByText('保存失败: 配置已被其他操作修改, 当前草稿已保留; 请复制草稿并刷新后合并', { exact: true }).waitFor();
  assert.equal(await groupMode.inputValue(), 'inherit');
  page.once('dialog', (event) => event.dismiss());
  await dialog.getByRole('button', { name: '取消', exact: true }).click();
  assert.equal(await dialog.isVisible(), true);
  page.once('dialog', (event) => event.accept());
  await dialog.getByRole('button', { name: '取消', exact: true }).click();
  await dialog.waitFor({ state: 'hidden' });
  running = true;
  await refresh();
  await page.getByTitle('编辑', { exact: true }).click();
  await groupMode.selectOption('inherit');
  await dialog.getByRole('button', { name: '保存修改' }).click();
  await dialog.waitFor({ state: 'hidden' });
  assert.deepEqual(writes.at(-1).session_bindings.group, { mode: 'inherit' });
  await page.getByText('应用失败', { exact: true }).waitFor();
  applicationStatus = 'applied';
  await page.getByRole('button', { name: '重试应用 future-bot', exact: true }).click();
  await page.getByText('已生效', { exact: true }).waitFor();

  // 删除或停用的配置保留可见值, 禁止以空白或另一个配置替代
  platforms[0].session_bindings.private = { mode: 'value', provider: 'edictum', config_name: 'gone' };
  platforms[0].session_bindings.topic = { mode: 'value', provider: 'edictum', config_name: 'disabled' };
  revision++;
  await refresh();
  await page.getByTitle('编辑', { exact: true }).click();
  assert.equal(await dialog.getByLabel('私聊 Agent 配置', { exact: true }).inputValue(), value('edictum', 'gone'));
  await dialog.getByRole('button', { name: '保存修改' }).click();
  await page.getByText('私聊 Agent 配置已删除, 请重新选择', { exact: true }).waitFor();
  await privateMode.selectOption('inherit');
  await dialog.getByRole('button', { name: '保存修改' }).click();
  await page.getByText('话题 Agent 配置已停用, 请重新选择', { exact: true }).waitFor();
  await page.setViewportSize({ width: 390, height: 844 });
  await dialog.getByTestId('agent-routing').scrollIntoViewIfNeeded();
  const bounds = await dialog.boundingBox();
  assert.ok(bounds.x >= 0 && bounds.x + bounds.width <= 390);
  await page.screenshot({ animations: 'disabled', path: path.join(artifacts, 'mobile.png') });
  assert.deepEqual(errors, []);
  console.log('Agent routing e2e passed: cold metadata, future kinds, save/inherit, missing/disabled, drafts and retry');
} catch (error) {
  if (page) {
    await page.screenshot({ animations: 'disabled', path: path.join(artifacts, 'failure.png') });
    await fs.writeFile(path.join(artifacts, 'failure.txt'), await page.locator('body').innerText(), 'utf8');
  }
  throw error;
} finally {
  if (browser) await browser.close();
  await server.close();
}
