import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';
import { createServer } from 'vite';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const scopeOnly = ({ operation_id, ...scope }) => { assert.match(operation_id, /^[\w-]{16,128}$/); return scope; };
const server = await createServer({ root, server: { host: '127.0.0.1', port: 0 } });
let browser;
try {
  await server.listen();
  const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  await page.clock.install();
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  const plugin = { name: 'probe', enabled: true, config: {}, capabilities: { tools: { probe_tool: true } } };
  const configs = { assistant: { provider: 'edictum', edictum_type: 'async_simple', enabled: true,
    description: '', model_name: 'model', params: {}, plugins: [plugin] },
    other: { provider: 'edictum', edictum_type: 'async_simple', enabled: true, description: '无关配置', model_name: 'other-model', params: {}, plugins: [] } };
  const untouched = structuredClone(configs.other);
  const reads = {}, writes = [], applications = [];
  let releaseApply, applyEntered;
  let holdApply = true;
  let timeoutApply = false;
  let previewCount = 0;
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
    if (url.pathname === '/config/versions') {
      const resources = (url.searchParams.get('resources') || '').split(',');
      return reply({ ok: true, resources: Object.fromEntries(resources.map((name) => [name, { state: 'available', change_token: JSON.stringify(configs) }])) });
    }
    if (url.pathname === '/config/edictum/sessions/assistant' && request.method() === 'PATCH') {
      const body = request.postDataJSON(); writes.push(body);
      configs.assistant = { ...configs.assistant, ...body };
      return reply({ ok: true, name: 'assistant', config: configs.assistant });
    }
    if (url.pathname === '/api/edictum/plugins/preview') {
      previewCount++;
      assert.equal(request.postDataJSON().config_name, 'assistant');
      return reply({ ok: true, edictum_sessions: [] });
    }
    if (url.pathname === '/api/edictum/plugins/reconcile') {
      applications.push(request.postDataJSON());
      if (holdApply || timeoutApply) {
        holdApply = false;
        await new Promise((resolve) => { releaseApply = resolve; applyEntered(); });
      }
      if (request.failure()) return;
      return reply({ ok: applications.length > 1, edictum_sessions: applications.length === 1
        ? [{ ok: false, platform_id: 'ob', session_id: 'failed-one', config_name: 'assistant', error: '测试安装失败' },
          { ok: true, platform_id: 'ob', session_id: 'success-one', config_name: 'assistant' }]
        : [{ ok: true, platform_id: 'ob', session_id: 'failed-one', config_name: 'assistant' }] });
    }
    if (url.pathname === '/api/config/reload') throw new Error('插件保存不应调用全量重载');
    const responses = {
      '/config/models': { model: { model: 'probe-model' } }, '/config/session-classes': {}, '/config/edictum/sessions': configs,
      '/config/platforms': { platforms: [] },
      '/config/edictum/types': { types: [{ name: 'async_simple', is_async: true, description: '', config_schema: {}, capabilities: { plugins: true } }] },
      '/config/edictum/plugins': { plugins: [{ name: 'probe', version: '1', description: '', config_schema: {}, capabilities: { tools: { probe_tool: '测试工具' } } }] },
      '/api/sessions': { sessions: [] },
    };
    return reply(responses[url.pathname] || { ok: true, running: true, adapters: {} });
  });
  await page.routeWebSocket(/ws:\/\/127\.0\.0\.1:1987[012]\//, () => {});
  await page.goto(origin + '/agents?edictum=assistant');
  const dialog = page.getByRole('dialog');
  await dialog.getByRole('button', { name: '保存插件配置', exact: true }).waitFor();
  const tool = dialog.getByText('probe_tool', { exact: true }).locator('..').locator('..').getByRole('checkbox');
  await tool.uncheck();
  const entered = new Promise((resolve) => { applyEntered = resolve; });
  const before = structuredClone(reads);
  await dialog.getByRole('button', { name: '保存插件配置', exact: true }).click();
  await entered;
  await dialog.getByRole('status').filter({ hasText: '配置已保存, 正在应用' }).waitFor();
  assert.deepEqual(applications.map(scopeOnly), [{ config_name: 'assistant' }]);
  await page.clock.runFor(11000);
  assert.equal(await dialog.getByRole('button', { name: '保存中…', exact: true }).isDisabled(), true);
  for (const pathName of ['/config/edictum/sessions', '/config/edictum/types', '/config/edictum/plugins', '/config/platforms']) assert.equal(reads[pathName], before[pathName]);
  releaseApply();
  await dialog.waitFor({ state: 'hidden' });
  await page.getByText(/1\/2 个活跃会话同步失败/).waitFor();
  assert.equal(configs.assistant.plugins[0].capabilities.tools.probe_tool, false);
  assert.deepEqual(configs.other, untouched);
  await page.getByRole('button', { name: '重试未同步会话', exact: true }).click();
  await page.getByText('插件漂移重试完成', { exact: true }).waitFor();
  assert.deepEqual(scopeOnly(applications[1]), { session_refs: [{ platform_id: 'ob', session_id: 'failed-one' }] });
  assert.equal(await page.getByRole('button', { name: '重试未同步会话', exact: true }).isDisabled(), true);
  timeoutApply = true;
  await page.getByTitle('管理插件', { exact: true }).first().click();
  await tool.check();
  const timedEntered = new Promise((resolve) => { applyEntered = resolve; });
  await dialog.getByRole('button', { name: '保存插件配置', exact: true }).click();
  await timedEntered;
  const failedRequest = page.waitForEvent('requestfailed', { timeout: 140000, predicate: (request) => new URL(request.url()).pathname === '/api/edictum/plugins/reconcile' });
  await failedRequest;
  await page.getByText(/插件配置已保存, 运行时应用未确认/).waitFor();
  releaseApply();
  assert.equal(await tool.isChecked(), true);
  assert.equal(configs.assistant.plugins[0].capabilities.tools.probe_tool, true);
  assert.equal(writes.length, 2); assert.equal(previewCount, 2);
  assert.deepEqual(errors, []);
  console.log('PASS: 插件保存只协调目标 Agent, 保存结果局部合入且不全量读取, 超过 10 秒继续等待');
  console.log('PASS: 部分失败显示实际结果/仅重试失败实例, 应用截止保留已保存事实和草稿, 无关配置未变化');
} finally {
  if (browser) await browser.close();
  await server.close();
}
