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
  const configs = {
    assistant: { edictum_type: 'async_simple', enabled: true, description: '', model_name: 'probe', params: { system_prompt: '旧提示词', custom: '保留参数' }, plugins: [] },
  };
  const writes = [];
  let configReads = 0;
  let failSave = false;
  const cors = { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true', 'Access-Control-Allow-Headers': 'Content-Type', 'Access-Control-Allow-Methods': 'GET,POST,PATCH,OPTIONS' };
  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  await page.route(origin + '/ui-config.json', (route) => route.fulfill({ json: {
    backend_api: 'http://127.0.0.1:19870', control_api: 'http://127.0.0.1:19871', chat_api: 'http://127.0.0.1:19872',
  } }));
  await page.route(/^http:\/\/127\.0\.0\.1:1987[012]\//, (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (request.method() === 'OPTIONS') return route.fulfill({ status: 204, headers: cors });
    if (url.pathname === '/config/edictum/sessions') {
      configReads++;
      return route.fulfill({ json: configs, headers: cors });
    }
    if (url.pathname === '/config/edictum/sessions/assistant' && request.method() === 'PATCH') {
      const body = request.postDataJSON();
      writes.push(body);
      if (failSave) return route.fulfill({ status: 500, json: { detail: '保存测试失败' }, headers: cors });
      Object.assign(configs.assistant, body);
      return route.fulfill({ json: { config: configs.assistant }, headers: cors });
    }
    const responses = {
      '/api/health': { running: false, adapters: {} },
      '/config/models': { probe: { model: 'probe', thinking_fields: ['reasoning_effort'], thinking_levels: ['low', 'high'] } },
      '/config/session-classes': { configs: {} },
      '/config/platforms': { platforms: [] },
      '/api/sessions': { sessions: [] },
      '/config/edictum/types': { types: [{ name: 'async_simple', is_async: true, description: '', config_schema: {}, capabilities: { plugins: true, mcp: true, stream: true } }] },
      '/config/edictum/plugins': { plugins: [{ name: 'probe_plugin', description: '', version: '1', config_schema: { note: { type: 'str', default: '' } }, capabilities: { tools: { probe_tool: '测试工具' } } }] },
      '/config/plugin-model-options': { options: {} },
      '/config/rag': { knowledge_bases: [] },
    };
    return route.fulfill({ json: responses[url.pathname] ?? { ok: true }, headers: cors });
  });
  await page.routeWebSocket(/ws:\/\/127\.0\.0\.1:1987[012]\//, () => {});
  await page.goto(origin + '/sessions');
  await page.getByRole('button', { name: 'Edictum 会话', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await page.getByTitle('管理插件', { exact: true }).click();
  await dialog.getByRole('button', { name: '添加', exact: true }).click();
  await dialog.getByLabel('note', { exact: true }).fill('保留草稿');
  await dialog.getByText('probe_tool', { exact: true }).locator('..').locator('..').getByRole('checkbox').uncheck();
  const readsBefore = configReads;
  await new Promise((resolve, reject) => {
    const deadline = Date.now() + 16000;
    const check = () => {
      if (configReads >= readsBefore + 2) return resolve();
      if (Date.now() > deadline) return reject(new Error('后台轮询未完成'));
      setTimeout(check, 100);
    };
    check();
  });
  assert.equal(await dialog.getByLabel('note', { exact: true }).inputValue(), '保留草稿');
  assert.equal(await dialog.getByRole('button', { name: '添加', exact: true }).count(), 0);
  failSave = true;
  await dialog.getByRole('button', { name: '保存插件配置', exact: true }).click();
  await page.getByText(/插件配置保存失败/).waitFor();
  assert.equal(await dialog.getByLabel('note', { exact: true }).inputValue(), '保留草稿');
  assert.equal(writes.at(-1).plugins[0].capabilities.tools.probe_tool, false);
  await dialog.getByRole('button', { name: '取消', exact: true }).click();
  await dialog.waitFor({ state: 'hidden' });
  await page.getByTitle('管理插件', { exact: true }).click();
  await dialog.getByRole('button', { name: '添加', exact: true }).waitFor();
  assert.equal(await dialog.getByRole('button', { name: '添加', exact: true }).count(), 1);
  failSave = false;
  await dialog.getByRole('button', { name: '添加', exact: true }).click();
  await dialog.getByRole('button', { name: '保存插件配置', exact: true }).click();
  await dialog.waitFor({ state: 'hidden' });
  await page.getByTitle('管理插件', { exact: true }).click();
  await dialog.getByLabel('note', { exact: true }).waitFor();
  assert.equal(await dialog.getByRole('button', { name: '添加', exact: true }).count(), 0);
  await dialog.getByRole('button', { name: '取消', exact: true }).click();
  await dialog.waitFor({ state: 'hidden' });
  await page.getByTitle('编辑配置', { exact: true }).click();
  assert.equal(await dialog.getByLabel('系统提示词', { exact: true }).inputValue(), '旧提示词');
  assert.deepEqual(JSON.parse(await dialog.getByLabel('其他会话参数 (JSON 对象)', { exact: true }).inputValue()), { custom: '保留参数' });
  await dialog.getByLabel('系统提示词', { exact: true }).fill('你是群聊助手\n使用中文回复');
  await dialog.getByRole('button', { name: '保存', exact: true }).click();
  await dialog.waitFor({ state: 'hidden' });
  assert.deepEqual(configs.assistant.params, { custom: '保留参数', system_prompt: '你是群聊助手\n使用中文回复' });
  await page.getByTitle('编辑配置', { exact: true }).click();
  await dialog.getByLabel('系统提示词', { exact: true }).fill('');
  await dialog.getByRole('button', { name: '保存', exact: true }).click();
  await dialog.waitFor({ state: 'hidden' });
  assert.equal(configs.assistant.params.system_prompt, '');
  await page.getByTitle('编辑配置', { exact: true }).click();
  await dialog.getByLabel('默认思考强度', { exact: true }).selectOption('high');
  assert.equal(await dialog.getByLabel('默认思考强度', { exact: true }).getByRole('option', { name: '中', exact: true }).count(), 0);
  await dialog.getByLabel('温度', { exact: true }).fill('0');
  await dialog.getByLabel('top_p', { exact: true }).fill('0.8');
  await dialog.getByLabel('最大输出 token 数', { exact: true }).fill('2048');
  await dialog.getByRole('button', { name: '保存', exact: true }).click();
  await dialog.waitFor({ state: 'hidden' });
  assert.equal(configs.assistant.params.thinking, 'high');
  assert.deepEqual(configs.assistant.params.model_params, { temperature: 0, top_p: 0.8, max_tokens: 2048 });
  await page.getByTitle('编辑配置', { exact: true }).click();
  assert.equal(await dialog.getByLabel('温度', { exact: true }).inputValue(), '0');
  await dialog.getByLabel('温度', { exact: true }).fill('');
  await dialog.getByRole('button', { name: '保存', exact: true }).click();
  await dialog.waitFor({ state: 'hidden' });
  assert.deepEqual(configs.assistant.params.model_params, { top_p: 0.8, max_tokens: 2048 });
  console.log('PASS: Edictum 插件草稿跨轮询保留, 保存失败保留, 取消重开和保存重开正确初始化');
  console.log('PASS: Edictum 提示词从旧 JSON 回填, 修改和清空正常保存, 其他参数保留');
  console.log('PASS: 思考强度按模型选项配置, 生成参数保存回填, 零值与恢复继承有效');
} finally {
  if (browser) await browser.close();
  await server.close();
}
