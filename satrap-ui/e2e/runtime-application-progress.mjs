import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';
import { createServer } from 'vite';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const server = await createServer({ root, server: { host: '127.0.0.1', port: 0 } });
let browser;
try {
  await server.listen();
  const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  await page.clock.install();
  const errors = [], applications = [], writes = [], reads = {};
  page.on('pageerror', (error) => errors.push(error.message));
  page.on('dialog', (dialog) => dialog.accept());
  const configs = {
    assistant: { provider: 'edictum', edictum_type: 'async_simple', enabled: true, description: '', model_name: 'model', params: {},
      plugins: [{ name: 'probe', enabled: true, config: {}, capabilities: { tools: { probe_tool: true } } }] },
    other: { provider: 'edictum', edictum_type: 'async_simple', enabled: true, description: '无关配置', model_name: 'other', params: {}, plugins: [] },
  };
  const untouched = structuredClone(configs.other);
  const operations = new Map();
  let phase = 'waiting_lock';
  let firstResult = null;
  let fullResult = null;
  const stage = (phase, status = 'running', session = '') => ({ platform_id: session ? 'ob' : '', session_id: session, status, phase, phase_elapsed_ms: 1, timings_ms: {} });
  const snapshot = (id) => {
    const index = operations.get(id);
    const result = index === 0 ? firstResult : index === 2 ? fullResult : { ok: true, edictum_sessions: [{ ok: true, platform_id: 'ob', session_id: 'failed-one', config_name: 'assistant' }] };
    return { operation_id: id, status: result ? result.ok ? 'completed' : 'failed' : 'running', created_at: 1, elapsed_ms: 1,
      stage: stage(result ? 'finished' : 'applying'), instances: [stage(result ? 'finished' : phase, result ? result.ok ? 'completed' : 'failed' : 'running', 'failed-one')],
      instances_limited: false, result, error: '' };
  };
  const cors = { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true',
    'Access-Control-Allow-Headers': 'Content-Type', 'Access-Control-Allow-Methods': 'GET,PATCH,POST,OPTIONS' };
  await page.route(origin + '/ui-config.json', (route) => route.fulfill({ json: {
    backend_api: 'http://127.0.0.1:19870', control_api: 'http://127.0.0.1:19871', chat_api: 'http://127.0.0.1:19872',
  } }));
  await page.route(/^http:\/\/127\.0\.0\.1:1987[012]\//, async (route) => {
    const request = route.request(), url = new URL(request.url());
    const reply = (json, status = 200) => route.fulfill({ json, status, headers: cors });
    if (request.method() === 'OPTIONS') return route.fulfill({ status: 204, headers: cors });
    if (request.method() === 'GET') reads[url.pathname] = (reads[url.pathname] || 0) + 1;
    if (url.pathname === '/config/versions') return reply({ ok: true, resources: Object.fromEntries((url.searchParams.get('resources') || '').split(',').map((name) => [name, { state: 'available', change_token: JSON.stringify(configs) }])) });
    if (url.pathname === '/config/edictum/sessions/assistant' && request.method() === 'PATCH') {
      const body = request.postDataJSON(); writes.push(body); configs.assistant = { ...configs.assistant, ...body };
      return reply({ ok: true, name: 'assistant', config: configs.assistant });
    }
    if (url.pathname.endsWith('/preview')) return reply({ ok: true, edictum_sessions: [] });
    if (['/api/edictum/plugins/reconcile', '/api/edictum/runtime/apply'].includes(url.pathname)) {
      const body = request.postDataJSON();
      assert.match(body.operation_id, /^[\w-]{16,128}$/);
      assert.equal(operations.has(body.operation_id), false);
      operations.set(body.operation_id, applications.length); applications.push({ route: url.pathname, body });
      return reply(snapshot(body.operation_id), 202);
    }
    if (url.pathname.startsWith('/api/runtime-operations/')) {
      const id = url.pathname.split('/').at(-1);
      return operations.has(id) ? reply(snapshot(id)) : reply({ error: '任务不存在' }, 404);
    }
    if (url.pathname === '/api/config/reload') throw new Error('不应调用全量重载');
    const responses = {
      '/config/models': { model: { model: 'probe-model' } }, '/config/session-classes': {}, '/config/edictum/sessions': configs,
      '/config/platforms': { platforms: [] }, '/api/sessions': { sessions: [] },
      '/config/edictum/types': { types: [{ name: 'async_simple', is_async: true, description: '', config_schema: {}, capabilities: { plugins: true } }] },
      '/config/edictum/plugins': { plugins: [{ name: 'probe', version: '1', description: '', config_schema: {}, capabilities: { tools: { probe_tool: '测试工具' } } }] },
    };
    return reply(responses[url.pathname] || { ok: true, running: true, adapters: {} });
  });
  await page.routeWebSocket(/ws:\/\/127\.0\.0\.1:1987[012]\//, () => {});
  await page.goto(origin + '/agents?edictum=assistant');
  const dialog = page.getByRole('dialog');
  const tool = dialog.getByText('probe_tool', { exact: true }).locator('..').locator('..').getByRole('checkbox');
  await tool.uncheck();
  await dialog.getByRole('button', { name: '保存插件配置', exact: true }).click();
  await dialog.getByRole('status').filter({ hasText: '等待会话空闲' }).first().waitFor();
  assert.equal(writes.length, 1); assert.equal(applications.length, 1);
  phase = 'install'; await page.clock.runFor(1100);
  await dialog.getByRole('status').filter({ hasText: '安装插件' }).first().waitFor();
  await page.clock.runFor(121000);
  await page.getByText(/插件配置已保存, 运行时应用未确认: 等待应用超过 120 秒/).waitFor();
  assert.equal(await tool.isChecked(), false);
  assert.equal(applications.length, 1); assert.equal(writes.length, 1);
  firstResult = { ok: false, edictum_sessions: [
    { ok: false, platform_id: 'ob', session_id: 'failed-one', config_name: 'assistant', error: '测试安装失败' },
    { ok: true, platform_id: 'ob', session_id: 'success-one', config_name: 'assistant' },
  ] };
  await dialog.getByRole('button', { name: '查询应用状态', exact: true }).click();
  await page.getByText('测试安装失败', { exact: true }).waitFor();
  assert.equal(await tool.isChecked(), false);
  assert.equal(applications.length, 1); assert.equal(writes.length, 1);
  await page.keyboard.press('Escape');
  await page.getByRole('button', { name: '重试未同步会话', exact: true }).click();
  await page.getByText('插件漂移重试完成', { exact: true }).waitFor();
  assert.deepEqual(applications[1].body.session_refs, [{ platform_id: 'ob', session_id: 'failed-one' }]);
  assert.notEqual(applications[0].body.operation_id, applications[1].body.operation_id);
  await page.getByTitle('编辑配置', { exact: true }).first().click();
  await dialog.getByLabel('配置系统提示词', { exact: true }).check();
  await dialog.getByLabel('系统提示词', { exact: true }).fill('完整配置进度测试');
  phase = 'model_build';
  await dialog.getByRole('button', { name: '保存', exact: true }).click();
  await dialog.getByRole('status').filter({ hasText: '创建模型客户端' }).waitFor();
  await page.clock.runFor(121000);
  await page.getByText(/Edictum 配置已保存, 运行时应用未确认: 等待应用超过 120 秒/).waitFor();
  assert.equal(await dialog.getByLabel('系统提示词', { exact: true }).inputValue(), '完整配置进度测试');
  fullResult = { ok: true, edictum_sessions: [{ ok: true, platform_id: 'ob', session_id: 'failed-one' }] };
  await dialog.getByRole('button', { name: '查询应用状态', exact: true }).click();
  await dialog.getByRole('status').filter({ hasText: '应用已完成' }).waitFor();
  assert.equal(writes.length, 2); assert.equal(applications.length, 3);
  await page.keyboard.press('Escape');
  assert.equal(applications[2].route, '/api/edictum/runtime/apply');
  assert.equal(applications[2].body.config_name, 'assistant');
  assert.equal(configs.assistant.params.system_prompt, '完整配置进度测试');
  assert.deepEqual(configs.other, untouched); assert.deepEqual(errors, []);
  console.log('PASS: 202 实际阶段展示, 120 秒截止保留已保存事实和草稿, 查询不重发, 部分失败仅重试失败实例, 完整保存沿正确接口');
} finally {
  if (browser) await browser.close();
  await server.close();
}
