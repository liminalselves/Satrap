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
  const plain = { note: { type: 'string', default: '', description: '备注' } };
  const plugins = ['plain_probe', 'model_probe', 'knowledge_probe', 'addable_probe', ...Array.from({ length: 8 }, (_, index) => `unused_${index}`)].map((name) => ({
    name, version: '1', description: name, source: 'builtin', usage_count: 1, capabilities: {},
    config_schema: name === 'model_probe' ? { ...plain, model: { type: 'llm', default: 'saved_model', description: '模型' } }
      : name === 'knowledge_probe' ? { ...plain, kb: { type: 'knowledge_base', default: '', description: '知识库' } } : plain,
  }));
  const configs = { agent: { enabled: true, edictum_type: 'simple', model_name: 'saved_model', params: {},
    plugins: plugins.filter((item) => item.name !== 'addable_probe').map((plugin) => ({ name: plugin.name, enabled: true, config: { note: `named-${plugin.name}` }, capabilities: {} })) } };
  let modelRelease;
  let holdModels = true;
  let modelReads = 0, knowledgeReads = 0, catalogReads = 0;
  const globalReads = [];
  const errors = [];
  browser = await chromium.launch({ headless: true });
  const context = await browser.newContext();
  const page = await context.newPage();
  page.on('pageerror', (error) => errors.push(error.message));
  await page.clock.install();
  await context.route('**/*', async (route) => {
    const request = route.request(), url = new URL(request.url());
    if (url.origin === origin && url.pathname !== '/ui-config.json') return route.continue();
    const headers = { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true',
      'Access-Control-Allow-Headers': 'Content-Type', 'Access-Control-Allow-Methods': 'GET,POST,PATCH,OPTIONS' };
    const reply = (json, status = 200) => route.fulfill({ json, status, headers });
    if (request.method() === 'OPTIONS') return route.fulfill({ status: 204, headers });
    if (url.pathname === '/ui-config.json') return reply({ backend_api: 'http://127.0.0.1:19870', control_api: 'http://127.0.0.1:19871', chat_api: 'http://127.0.0.1:19872' });
    if (url.pathname === '/api/auth/session' || url.pathname === '/auth/session') return reply({ ok: true });
    if (url.pathname === '/config/versions') return reply({ ok: true, resources: Object.fromEntries((url.searchParams.get('resources') || '').split(',').map((name) => [name, {
      state: 'available', change_token: name === 'plugin_catalog' ? JSON.stringify(plugins) : name === 'edictum_configs' ? JSON.stringify(configs) : 'v1',
    }])) });
    if (url.pathname === '/config/plugins') { catalogReads++; return reply({ plugins }); }
    const global = url.pathname.match(/^\/config\/plugins\/([^/]+)\/config$/);
    if (global) {
      globalReads.push(global[1]);
      const plugin = plugins.find((item) => item.name === global[1]);
      return reply({ ok: true, schema: plugin.config_schema, config: { note: 'inherited' }, overrides: {}, revision: 'v1' });
    }
    if (url.pathname === '/config/plugin-model-options') {
      modelReads++;
      if (holdModels) await new Promise((done) => { modelRelease = done; });
      return reply({ error: '模型选项测试失败' }, 500);
    }
    if (url.pathname === '/config/rag') { knowledgeReads++; return reply({ knowledge_bases: [{ id: 'kb1', name: '全局资料', scope: 'global' }] }); }
    if (url.pathname === '/config/edictum/sessions') return reply(configs);
    if (url.pathname === '/config/edictum/plugins') return reply({ plugins });
    if (url.pathname === '/config/edictum/types') return reply({ types: [{ name: 'simple', is_async: true, description: '', config_schema: {}, capabilities: { plugins: true } }] });
    if (url.pathname === '/config/models') return reply({ saved_model: { model: 'test' } });
    if (url.pathname === '/config/session-classes') return reply({});
    if (url.pathname === '/config/platforms') return reply({ platforms: [] });
    if (url.pathname === '/api/health' || url.pathname === '/status') return reply({ running: false, managed: false, adapters: {} });
    throw new Error(`未匹配插件加载夹具: ${request.method()} ${url.pathname}`);
  });
  await page.routeWebSocket(/ws:\/\/127\.0\.0\.1:1987[012]\//, () => {});

  await page.goto(origin + '/plugins/model_probe?tab=config');
  const note = page.getByLabel('note', { exact: true });
  await note.fill('慢选项期间的草稿');
  await page.waitForFunction(() => document.querySelector('select')?.disabled);
  assert.equal(modelReads, 1);
  assert.equal(knowledgeReads, 0);
  assert.equal(await page.getByText(/引用失效/).count(), 0);
  modelRelease(); holdModels = false;
  await page.getByRole('alert').filter({ hasText: '模型选项测试失败' }).waitFor();
  assert.equal(await note.inputValue(), '慢选项期间的草稿');
  assert.equal(await note.isEnabled(), true);
  assert.equal(knowledgeReads, 0);
  page.once('dialog', (confirmation) => confirmation.accept());
  await page.getByRole('link', { name: '← 返回插件列表' }).click();
  await page.getByRole('link', { name: /plain_probe/ }).click();
  await page.getByRole('heading', { name: 'plain_probe', exact: true }).waitFor();
  assert.equal(catalogReads, 1);

  await page.goto(origin + '/agents?edictum=agent');
  const dialog = page.getByRole('dialog');
  await dialog.getByLabel('note', { exact: true }).waitFor();
  await page.waitForFunction(() => !document.querySelector('[role="dialog"] input')?.disabled);
  assert.deepEqual(globalReads, ['model_probe', 'plain_probe']);
  const modelsBefore = modelReads;
  assert.equal(knowledgeReads, 0);
  await dialog.getByRole('button', { name: 'addable_probe', exact: true }).click();
  assert.deepEqual(globalReads, ['model_probe', 'plain_probe']);
  await dialog.getByRole('button', { name: '添加', exact: true }).click();
  await dialog.getByLabel('note', { exact: true }).fill('新增插件草稿');
  configs.agent.description = '外部变更';
  await page.clock.fastForward(31000);
  assert.equal(await dialog.getByLabel('note', { exact: true }).inputValue(), '新增插件草稿');
  assert.equal(await dialog.getByRole('button', { name: '添加', exact: true }).count(), 0);
  assert.equal(modelReads, modelsBefore);
  assert.equal(knowledgeReads, 0);
  await dialog.getByRole('button', { name: 'knowledge_probe', exact: true }).click();
  await dialog.getByLabel('kb', { exact: true }).selectOption('kb1');
  assert.equal(knowledgeReads, 1);
  assert.equal(modelReads, modelsBefore);
  assert.equal(globalReads.some((name) => name.startsWith('unused_')), false);
  assert.equal(catalogReads, 1);
  assert.deepEqual(errors, []);
  console.log('PASS: 慢/失败模型选项不阻塞普通参数, 保留草稿且不误报失效; 无需求不查询知识库');
  console.log('PASS: 12 插件仅选中/添加项读取继承值, 外部配置刷新不回退新增草稿, 知识库独立按需读取');
} finally {
  if (browser) await browser.close();
  await server.close();
}
