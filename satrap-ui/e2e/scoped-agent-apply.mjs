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
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  const configs = { assistant: { provider: 'edictum', edictum_type: 'async_simple', enabled: true,
    description: '', model_name: 'model', params: { system_prompt: '旧提示词', custom: '保留' }, plugins: [] },
    other: { provider: 'edictum', edictum_type: 'async_simple', enabled: true, description: '无关配置', model_name: 'other-model', params: {}, plugins: [] } };
  const untouched = structuredClone(configs.other);
  const reads = {}, writes = [], applications = [], previews = [];
  let failSave = false, failApply = false, releaseApply, applyEntered;
  let holdApply = true;
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
      if (failSave) return reply({ error: '冷配置写入失败' }, 500);
      configs.assistant = { ...configs.assistant, ...body };
      return reply({ ok: true, name: 'assistant', config: configs.assistant });
    }
    if (url.pathname === '/api/edictum/runtime/preview') {
      previews.push(request.postDataJSON());
      return reply({ ok: true, edictum_sessions: [{ action: 'restart', config_name: 'assistant' }] });
    }
    if (url.pathname === '/api/edictum/runtime/apply') {
      applications.push(request.postDataJSON());
      if (holdApply) { holdApply = false; await new Promise((resolve) => { releaseApply = resolve; applyEntered(); }); }
      if (failApply) return reply({ error: '应用读取失败' }, 503);
      return reply({ ok: applications.length > 1, edictum_sessions: applications.length === 1
        ? [{ ok: false, platform_id: 'ob', session_id: 'failed-one', action: 'restart', error: '候选创建失败', old_runtime_preserved: true },
          { ok: true, platform_id: 'ob', session_id: 'success-one', action: 'restart' }]
        : [{ ok: true, platform_id: 'ob', session_id: 'failed-one', action: 'restart' }] });
    }
    if (['/api/config/reload', '/api/edictum/plugins/reconcile'].includes(url.pathname)) throw new Error('完整配置保存/重试不应调用全量或仅插件协调');
    const responses = {
      '/config/models': { model: { model: 'probe-model' } }, '/config/session-classes': {}, '/config/edictum/sessions': configs,
      '/config/platforms': { platforms: [] },
      '/config/edictum/types': { types: [{ name: 'async_simple', is_async: true, description: '', config_schema: {}, capabilities: { plugins: true } }] },
      '/config/edictum/plugins': { plugins: [] }, '/api/sessions': { sessions: [] },
    };
    return reply(responses[url.pathname] || { ok: true, running: true, adapters: {} });
  });
  await page.routeWebSocket(/ws:\/\/127\.0\.0\.1:1987[012]\//, () => {});
  page.on('dialog', (dialog) => dialog.accept());
  await page.goto(origin + '/agents');
  await page.getByRole('button', { name: 'Edictum 流程', exact: true }).click();
  await page.getByTitle('编辑配置', { exact: true }).first().click();
  const dialog = page.getByRole('dialog');
  await dialog.getByLabel('系统提示词', { exact: true }).fill('新的提示词');
  const entered = new Promise((resolve) => { applyEntered = resolve; });
  const before = structuredClone(reads);
  await dialog.getByRole('button', { name: '保存', exact: true }).click();
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('保存后未进入完整配置应用')), 10000);
    entered.then(() => { clearTimeout(timer); resolve(); });
  });
  assert.deepEqual(applications, [{ config_name: 'assistant' }]);
  assert.equal(previews[0].config_name, 'assistant');
  assert.equal(previews[0].config.params.system_prompt, '新的提示词');
  releaseApply();
  await dialog.waitFor({ state: 'hidden' });
  await page.getByText('Edictum 配置已保存, 但后端热加载失败', { exact: true }).waitFor();
  for (const pathName of ['/config/edictum/sessions', '/config/edictum/types', '/config/edictum/plugins', '/config/platforms']) assert.equal(reads[pathName], before[pathName]);
  await page.getByRole('button', { name: '重试未同步会话', exact: true }).click();
  await page.getByText('配置同步重试完成', { exact: true }).waitFor();
  assert.deepEqual(applications[1], { session_refs: [{ platform_id: 'ob', session_id: 'failed-one' }] });
  assert.equal(await page.getByRole('button', { name: '重试未同步会话', exact: true }).isDisabled(), true);
  await page.getByTitle('编辑配置', { exact: true }).first().click();
  assert.equal(await dialog.getByLabel('系统提示词', { exact: true }).inputValue(), '新的提示词');
  await dialog.getByLabel('系统提示词', { exact: true }).fill('保留失败草稿');
  failSave = true;
  await dialog.getByRole('button', { name: '保存', exact: true }).click();
  await page.getByText('保存失败: Request failed with status code 500', { exact: true }).waitFor();
  assert.equal(await dialog.getByLabel('系统提示词', { exact: true }).inputValue(), '保留失败草稿');
  assert.equal(applications.length, 2);
  assert.equal(configs.assistant.params.system_prompt, '新的提示词');
  failSave = false; failApply = true;
  await dialog.getByRole('button', { name: '保存', exact: true }).click();
  await page.getByText('Edictum 配置已保存, 运行时应用未确认: 应用读取失败', { exact: true }).waitFor();
  assert.equal(await dialog.getByLabel('系统提示词', { exact: true }).inputValue(), '保留失败草稿');
  assert.equal(configs.assistant.params.system_prompt, '保留失败草稿');
  assert.equal(configs.assistant.params.custom, '保留');
  assert.deepEqual(configs.other, untouched);
  assert.equal(writes.length, 3); assert.equal(applications.length, 3);
  assert.deepEqual(errors, []);
  console.log('PASS: 完整 Agent 保存只应用目标配置, 局部回填且无全量读取, 完整重试只处理失败实例');
  console.log('PASS: 冷保存失败不应用, 已保存后应用失败明确提示并保留草稿, 无关 Agent 及额外参数保留');
} finally {
  if (browser) await browser.close();
  await server.close();
}
