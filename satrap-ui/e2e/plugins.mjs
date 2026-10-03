import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';
import { createServer } from 'vite';
import { mkdir } from 'node:fs/promises';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const server = await createServer({ root, server: { host: '127.0.0.1', port: 0 } });
let browser;
try {
  await server.listen();
  const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
  const plugins = [
    { name: 'builtin_probe', version: '1', author: '作者', description: '内置测试', source: 'builtin', usage_count: 1, edictum_configs: ['assistant'], chat_enabled: false, config_schema: {}, capabilities: { tools: { probe_tool: '测试工具' }, skills: { probe_skill: '测试技能' }, mcp: { probe_mcp: '测试连接' }, handlers: { probe_handler: '测试前处理' }, commands: { probe_command: '测试命令' } } },
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
  const globalSchema = { note: { type: 'string', default: 'default', description: '全局备注' }, count: { type: 'number', default: 1, minimum: 0, integer: true }, model: { type: 'llm', default: '', nullable: true, description: '命名模型' }, prompt: { type: 'textarea', default: null, description: '未设置时可直接填写' } };
  plugins[0].config_schema = globalSchema;
  const caps = Object.fromEntries(Object.entries(plugins[0].capabilities).map(([kind, items]) => [kind, Object.fromEntries(Object.keys(items).map((name) => [name, true]))]));
  const locations = [
    { kind: 'chat', id: 'chat', label: 'Chat', present: false, enabled: false, capabilities: caps, revision: 'chat-first', parent_enabled: true, availability: { allowed: true } },
    { kind: 'edictum', id: 'assistant', label: 'assistant', present: true, enabled: true, capabilities: caps, revision: 'edictum-first', parent_enabled: true, availability: { allowed: true, message: '激活实例时校验适用平台' } },
  ];
  let failUsageSave = false;
  let runtimeFailed = true;
  let runtimeReads = 0;
  page.on('pageerror', (error) => errors.push(error.message));
  await page.route(origin + '/ui-config.json', (route) => route.fulfill({ json: { backend_api: 'http://127.0.0.1:19870', control_api: 'http://127.0.0.1:19871', chat_api: 'http://127.0.0.1:19872' } }));
  await page.route(/^http:\/\/127\.0\.0\.1:1987[012]\//, (route) => {
    if (route.request().method() === 'OPTIONS') return route.fulfill({ status: 204, headers: cors });
    const pathname = new URL(route.request().url()).pathname;
    if (pathname === '/config/plugins/builtin_probe/usages') {
      if (route.request().method() === 'PUT') {
        const body = route.request().postDataJSON();
        const location = locations.find((item) => item.kind === body.kind && item.id === body.location_id);
        assert.equal(body.expected_revision, location.revision);
        if (failUsageSave) return route.fulfill({ status: 500, json: { error: '位置保存测试失败' }, headers: cors });
        Object.assign(location, body.state);
        location.revision += '-saved';
        plugins[0].chat_enabled = locations[0].present && locations[0].enabled;
        plugins[0].usage_count = locations.filter((item) => item.present).length;
      }
      return route.fulfill({ json: { ok: true, locations, saved: route.request().method() === 'PUT', runtime: [{ target: 'Chat', status: 'next_activation', sessions: [] }, { target: 'Edictum', status: 'error', error: '应用测试失败', sessions: [] }] }, headers: cors });
    }
    if (pathname === '/config/plugins/builtin_probe/runtime') {
      runtimeReads++;
      return route.fulfill({ json: { ok: true, services: [{ target: 'Chat', status: 'stopped', instances: [] }, { target: 'Edictum', status: 'available', instances: [{ platform_id: 'onebot', session_id: 'group-1', location_id: 'assistant', plugin: { name: 'builtin_probe', status: runtimeFailed ? 'error' : 'loaded', enabled: true, error: runtimeFailed ? '加载测试失败' : '', drift: runtimeFailed, capabilities: { applied: caps, desired: caps, loaded: runtimeFailed ? { skills: { probe_skill: true } } : caps } } }] }] }, headers: cors });
    }
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
    if (pathname === '/config/plugins/reconcile') { runtimeRetries++; runtimeFailed = false; return route.fulfill({ json: { ok: true, runtime: [{ target: 'Chat', status: 'applied', sessions: [] }, { target: 'Edictum', status: 'next_activation', sessions: [] }] }, headers: cors }); }
    if (pathname === '/config/plugin-model-options') return route.fulfill({ json: { options: { llm: [{ value: 'test_model', label: '测试模型' }] } }, headers: cors });
    if (pathname === '/config/rag') return route.fulfill({ json: { knowledge_bases: [] }, headers: cors });
    const adminResponses = {
      '/config/edictum/sessions': { assistant: { provider: 'edictum', edictum_type: 'async_simple', enabled: true, description: '', model_name: 'test_model', params: {}, plugins: [{ name: 'builtin_probe', enabled: true, config: { note: '命名备注' }, capabilities: caps }] } },
      '/config/edictum/types': { types: [{ name: 'async_simple', is_async: true, description: '', config_schema: {}, capabilities: { plugins: true, mcp: true, stream: true } }] },
      '/config/edictum/plugins': { plugins },
      '/config/models': { test_model: { model: 'model' } },
      '/config/platforms': { platforms: [] },
      '/config/session-classes': { configs: {} },
      '/api/sessions': { sessions: [] },
      '/api/chat/health': { ok: true, conversations: 0, preloaded: 0 },
      '/api/chat/models': { models: ['test_model'] },
      '/api/chat/models/detail': { ok: true, models: {} },
      '/api/chat/conversations': { conversations: [] },
      '/api/chat/conversations/preload': { ok: true, conversation_id: 'preload-probe' },
      '/api/chat/plugins': { plugins: [ { ...plugins[0], enabled: false, capabilities: Object.fromEntries(Object.entries(plugins[0].capabilities).map(([kind, items]) => [kind, Object.entries(items).map(([name, description]) => ({ name, description, enabled: true }))])) } ] },
      '/api/chat/runs': { ok: true, runs: [], next_cursor: null },
      '/api/projects': { projects: [] },
    };
    if (pathname in adminResponses) return route.fulfill({ json: adminResponses[pathname], headers: cors });
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
  await page.getByLabel('prompt', { exact: true }).fill('可填写默认空值');
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
  assert.deepEqual(globalOverrides, { note: '保留草稿', count: 0, model: 'test_model', prompt: '可填写默认空值' });
  await page.getByLabel('model 使用空值', { exact: true }).check();
  assert.equal(await page.getByLabel('model', { exact: true }).isDisabled(), true);
  await page.getByRole('button', { name: '保存全局参数', exact: true }).click();
  await page.getByText('全局参数已保存', { exact: true }).waitFor();
  assert.equal(globalOverrides.model, null);
  await page.getByText(/Edictum：应用失败/).waitFor();
  await page.getByRole('button', { name: '重试运行应用', exact: true }).click();
  await page.getByText('Edictum：下次激活时应用', { exact: true }).waitFor();
  assert.equal(runtimeRetries, 1);
  await page.getByRole('button', { name: '恢复默认值', exact: true }).click();
  assert.equal(await page.getByLabel('note', { exact: true }).inputValue(), 'default');
  await page.getByRole('button', { name: '保存全局参数', exact: true }).click();
  await page.getByText('全局参数已保存', { exact: true }).waitFor();
  assert.deepEqual(globalOverrides, {});
  runtimeFailed = true;
  await page.getByRole('button', { name: '使用位置', exact: true }).click();
  await page.getByRole('button', { name: '添加到此位置', exact: true }).click();
  await page.getByLabel('工具 probe_tool', { exact: true }).uncheck();
  for (const name of ['技能 probe_skill', 'MCP probe_mcp', '前处理 probe_handler', '命令 probe_command']) {
    await page.getByLabel(name, { exact: true }).uncheck();
    await page.getByLabel(name, { exact: true }).check();
  }
  const beforePoll = runtimeReads;
  await page.waitForResponse((response) => new URL(response.url()).pathname === '/config/plugins/builtin_probe/runtime' && runtimeReads > beforePoll);
  assert.equal(await page.getByLabel('工具 probe_tool', { exact: true }).isChecked(), false);
  await page.getByText('工具：未加载', { exact: true }).waitFor();
  failUsageSave = true;
  await page.getByRole('button', { name: '保存使用配置', exact: true }).click();
  await page.getByRole('alert').filter({ hasText: '位置保存测试失败' }).waitFor();
  assert.equal(await page.getByLabel('工具 probe_tool', { exact: true }).isChecked(), false);
  failUsageSave = false;
  await page.getByRole('button', { name: '保存使用配置', exact: true }).click();
  await page.getByText('使用配置已保存', { exact: true }).waitFor();
  assert.equal(locations[0].capabilities.tools.probe_tool, false);
  await page.getByLabel('启用插件', { exact: true }).uncheck();
  await page.getByRole('button', { name: '保存使用配置', exact: true }).click();
  await page.getByText('使用配置已保存', { exact: true }).waitFor();
  assert.equal(locations[0].enabled, false);
  assert.equal(locations[0].capabilities.tools.probe_tool, false);
  await page.getByRole('button', { name: '重试实例应用', exact: true }).click();
  await page.getByText('已加载并启用', { exact: true }).waitFor();
  assert.equal(runtimeRetries, 2);
  await page.getByRole('button', { name: '移除', exact: true }).click();
  await page.getByRole('button', { name: '保存使用配置', exact: true }).click();
  await page.getByText('使用配置已保存', { exact: true }).waitFor();
  assert.equal(locations[0].present, false);
  await page.getByLabel('使用位置', { exact: true }).selectOption('edictum:assistant');
  assert.equal(await page.getByRole('link', { name: '打开命名配置参数' }).getAttribute('href'), '/agents?edictum=assistant');
  const screenshots = path.resolve(root, '..', '.satrap', 'plugin-management-preview');
  await mkdir(screenshots, { recursive: true });
  await page.screenshot({ path: path.join(screenshots, 'desktop.png'), fullPage: true });
  await page.getByRole('heading', { name: '运行实例', exact: true }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(screenshots, 'runtime.png'), fullPage: true });
  await page.getByRole('heading', { name: 'builtin_probe', exact: true }).scrollIntoViewIfNeeded();
  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
  await page.screenshot({ path: path.join(screenshots, 'mobile.png'), fullPage: true });
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.getByRole('link', { name: '打开命名配置参数' }).click();
  await page.getByRole('dialog').getByLabel('note', { exact: true }).waitFor();
  assert.equal(await page.getByRole('dialog').getByLabel('note', { exact: true }).inputValue(), '命名备注');
  assert.equal(await page.getByRole('dialog').getByRole('link', { name: '打开插件详情', exact: true }).first().getAttribute('target'), '_blank');
  await page.goto(origin + '/chat');
  await page.getByTitle('对话设置', { exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: '插件', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: '配置', exact: true }).click();
  const globalDialog = page.getByRole('dialog').last();
  await globalDialog.getByLabel('note', { exact: true }).fill('Chat统一保存');
  await globalDialog.getByRole('button', { name: '保存', exact: true }).click();
  await globalDialog.getByText('全局参数已保存，部分运行实例应用失败', { exact: true }).waitFor();
  assert.equal(globalOverrides.note, 'Chat统一保存');
  await globalDialog.getByRole('button', { name: '重试运行应用', exact: true }).click();
  await globalDialog.getByText('Edictum：下次激活时应用', { exact: true }).waitFor();
  assert.deepEqual(errors, []);
  console.log('PASS: 插件目录搜索、来源与平台筛选、详情能力、直接打开和不存在提示');
  console.log('PASS: ZIP 上传预览、取消清理、显式安装、打开详情及刷新列表');
  console.log('PASS: 全局参数草稿跨刷新、离开确认、失败保留、零值保存、恢复默认及应用重试');
  console.log('PASS: 使用位置添加、五类能力编辑、轮询保留草稿、失败保留、停用保留独立开关、移除及实际加载状态');
  console.log('PASS: 命名配置深链、全局继承与恢复、Chat 快捷配置统一保存并重试 Edictum 应用');
} finally {
  if (browser) await browser.close();
  await server.close();
}
