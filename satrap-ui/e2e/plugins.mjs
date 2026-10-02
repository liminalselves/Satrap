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
  const plugins = [
    { name: 'builtin_probe', version: '1', author: '作者', description: '内置测试', source: 'builtin', usage_count: 2, edictum_configs: ['assistant'], chat_enabled: true, config_schema: {}, capabilities: { tools: { probe_tool: '测试工具' } } },
    { name: 'group_probe', version: '2', author: '群聊作者', description: '群聊测试', source: 'user', usage_count: 0, edictum_configs: [], chat_enabled: false, config_schema: {}, applicability: { session_types: ['platform'], platforms: ['onebot'] }, capabilities: { skills: { group_skill: '群聊技能' }, handlers: { pre_process: '前处理描述' } } },
  ];
  const cors = { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true', 'Access-Control-Allow-Headers': 'Content-Type', 'Access-Control-Allow-Methods': 'GET,POST,PATCH,PUT,OPTIONS' };
  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  const errors = [];
  let installs = 0;
  let discards = 0;
  let globalOverrides = { note: 'old' };
  let globalRevision = 'first';
  let failGlobalSave = false;
  let runtimeRetries = 0;
  const globalSchema = { note: { type: 'string', default: 'default', description: '全局备注' }, count: { type: 'number', default: 1, minimum: 0, integer: true }, model: { type: 'llm', default: '', description: '命名模型' } };
  page.on('pageerror', (error) => errors.push(error.message));
  await page.route(origin + '/ui-config.json', (route) => route.fulfill({ json: { backend_api: 'http://127.0.0.1:19870', control_api: 'http://127.0.0.1:19871', chat_api: 'http://127.0.0.1:19872' } }));
  await page.route(/^http:\/\/127\.0\.0\.1:1987[012]\//, (route) => {
    if (route.request().method() === 'OPTIONS') return route.fulfill({ status: 204, headers: cors });
    const pathname = new URL(route.request().url()).pathname;
    if (pathname === '/config/plugins/builtin_probe/config') {
      if (route.request().method() === 'PUT') {
        const body = route.request().postDataJSON();
        assert.equal(body.expected_revision, globalRevision);
        if (failGlobalSave) return route.fulfill({ status: 500, json: { error: '保存测试失败' }, headers: cors });
        globalOverrides = body.config;
        globalRevision += '-saved';
      }
      return route.fulfill({ json: { ok: true, schema: globalSchema, config: { note: 'default', count: 1, ...globalOverrides }, overrides: globalOverrides, revision: globalRevision, saved: route.request().method() === 'PUT', runtime: [{ target: 'Chat', status: 'applied', sessions: [{ conversation_id: 'probe-chat', ok: true }] }, { target: 'Edictum', status: 'error', error: '应用测试失败', sessions: [] }] }, headers: cors });
    }
    if (pathname === '/config/plugins/reconcile') { runtimeRetries++; return route.fulfill({ json: { ok: true, runtime: [{ target: 'Chat', status: 'applied', sessions: [] }, { target: 'Edictum', status: 'next_activation', sessions: [] }] }, headers: cors }); }
    if (pathname === '/config/plugin-model-options') return route.fulfill({ json: { options: { llm: [{ value: 'test_model', label: '测试模型' }] } }, headers: cors });
    if (pathname === '/config/rag') return route.fulfill({ json: { knowledge_bases: [] }, headers: cors });
    if (pathname === '/config/plugins/preview') {
      assert.equal(route.request().headers()['content-type'], 'application/zip');
      assert.equal(route.request().postData(), 'zip-preview-test');
      return route.fulfill({ json: { token: 'test-token', plugin: { ...plugins[1], name: 'installed_probe' }, expires_in: 600, file_count: 2, expanded_bytes: 100 }, headers: cors });
    }
    if (pathname === '/config/plugins/discard') { discards++; return route.fulfill({ json: { ok: true }, headers: cors }); }
    if (pathname === '/config/plugins/install') {
      assert.equal(route.request().postDataJSON().token, 'test-token');
      installs++;
      const plugin = { ...plugins[1], name: 'installed_probe' };
      plugins.push(plugin);
      return route.fulfill({ json: { ok: true, plugin }, headers: cors });
    }
    return route.fulfill({ json: pathname === '/config/plugins' ? { plugins } : { ok: true, running: false, adapters: {} }, headers: cors });
  });
  await page.routeWebSocket(/ws:\/\/127\.0\.0\.1:1987[012]\//, () => {});
  await page.goto(origin + '/plugins');
  await page.getByRole('link', { name: /builtin_probe/ }).waitFor();
  assert.equal(await page.getByRole('link', { name: /group_probe/ }).count(), 1);
  await page.getByLabel('插件来源').selectOption('user');
  assert.equal(await page.getByRole('link', { name: /builtin_probe/ }).count(), 0);
  await page.getByLabel('搜索插件').fill('群聊作者');
  await page.getByLabel('适用平台').selectOption('onebot');
  await page.getByRole('link', { name: /group_probe/ }).click();
  await page.getByRole('heading', { name: 'group_probe', exact: true }).waitFor();
  await page.getByText('group_skill', { exact: true }).waitFor();
  await page.getByText('前处理描述', { exact: true }).waitFor();
  await page.reload();
  await page.getByText('group_skill', { exact: true }).waitFor();
  await page.goto(origin + '/plugins/missing');
  await page.getByRole('alert').filter({ hasText: '插件不存在' }).waitFor();
  await page.goto(origin + '/plugins');
  await page.getByRole('button', { name: '安装插件', exact: true }).click();
  const dialog = page.getByRole('dialog');
  const file = { name: 'plugin.zip', mimeType: 'application/zip', buffer: Buffer.from('zip-preview-test') };
  await dialog.getByLabel('插件压缩包').setInputFiles(file);
  await dialog.getByRole('heading', { name: /installed_probe/ }).waitFor();
  assert.equal(installs, 0);
  const discarded = page.waitForResponse((response) => new URL(response.url()).pathname === '/config/plugins/discard');
  await dialog.getByRole('button', { name: '取消', exact: true }).click();
  await dialog.waitFor({ state: 'hidden' });
  await discarded;
  assert.equal(discards, 1);
  await page.getByRole('button', { name: '安装插件', exact: true }).click();
  await dialog.getByLabel('插件压缩包').setInputFiles(file);
  await dialog.getByRole('heading', { name: /installed_probe/ }).waitFor();
  await dialog.getByRole('button', { name: '安装', exact: true }).click();
  await page.getByRole('heading', { name: 'installed_probe', exact: true }).waitFor();
  assert.equal(installs, 1);
  assert.equal(plugins.at(-1).chat_enabled, false);
  await page.getByRole('link', { name: '← 返回插件列表' }).click();
  await page.getByRole('link', { name: /installed_probe/ }).waitFor();
  await page.goto(origin + '/plugins/builtin_probe');
  await page.getByRole('button', { name: '全局参数', exact: true }).click();
  await page.getByLabel('note', { exact: true }).fill('保留草稿');
  await page.getByLabel('count', { exact: true }).fill('0');
  await page.getByLabel('model', { exact: true }).selectOption('test_model');
  await page.getByRole('button', { name: '刷新', exact: true }).click();
  await page.getByRole('button', { name: '刷新', exact: true }).waitFor({ state: 'visible' });
  assert.equal(await page.getByLabel('note', { exact: true }).inputValue(), '保留草稿');
  page.once('dialog', (dialog) => dialog.dismiss());
  await page.getByRole('link', { name: '插件管理', exact: true }).click();
  assert.equal(await page.getByLabel('note', { exact: true }).inputValue(), '保留草稿');
  failGlobalSave = true;
  await page.getByRole('button', { name: '保存全局参数', exact: true }).click();
  await page.getByRole('alert').filter({ hasText: '保存测试失败' }).waitFor();
  assert.equal(await page.getByLabel('note', { exact: true }).inputValue(), '保留草稿');
  failGlobalSave = false;
  await page.getByRole('button', { name: '保存全局参数', exact: true }).click();
  await page.getByText('全局参数已保存', { exact: true }).waitFor();
  assert.deepEqual(globalOverrides, { note: '保留草稿', count: 0, model: 'test_model' });
  await page.getByText(/Edictum：应用失败/).waitFor();
  await page.getByRole('button', { name: '重试运行应用', exact: true }).click();
  await page.getByText('Edictum：下次激活时应用', { exact: true }).waitFor();
  assert.equal(runtimeRetries, 1);
  await page.getByRole('button', { name: '恢复默认值', exact: true }).click();
  assert.equal(await page.getByLabel('note', { exact: true }).inputValue(), 'default');
  await page.getByRole('button', { name: '保存全局参数', exact: true }).click();
  await page.getByText('全局参数已保存', { exact: true }).waitFor();
  assert.deepEqual(globalOverrides, {});
  assert.deepEqual(errors, []);
  console.log('PASS: 插件目录搜索、来源与平台筛选、详情能力、直接打开和不存在提示');
  console.log('PASS: ZIP 上传预览、取消清理、显式安装、打开详情及刷新列表');
  console.log('PASS: 全局参数草稿跨刷新、离开确认、失败保留、零值保存、恢复默认及应用重试');
} finally {
  if (browser) await browser.close();
  await server.close();
}
