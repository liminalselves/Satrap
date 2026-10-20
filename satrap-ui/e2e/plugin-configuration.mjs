import assert from "node:assert/strict";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { mkdir, readFile } from "node:fs/promises";
import { load } from "js-yaml";
import { runSuite, mockConfigVersions } from './runtime.mjs';
import { pathToFileURL } from 'node:url';

async function plugins(runtime) {







  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  const server = await runtime.vite({ root, server: { host: '127.0.0.1', port: 0 } });
  let browser;
  try {
    await runtime.listen(server);
    const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
    const plugins = [
      { name: 'builtin_probe', version: '1', author: '作者', description: '内置测试', source: 'builtin', usage_count: 1, edictum_configs: ['assistant'], chat_enabled: false, config_schema: {}, interop: { exports: { services: ['probe_service'] }, requires: { formatter: { optional: true, services: ['format'] } }, replaces: { legacy: { tools: { old_tool: 'probe_tool' } } } }, capabilities: { services: { probe_service: '测试服务' }, tools: { probe_tool: '测试工具' }, skills: { probe_skill: '测试技能' }, mcp: { probe_mcp: '测试连接' }, handlers: { probe_handler: '测试前处理' }, commands: { probe_command: '测试命令' } } },
      { name: 'group_probe', version: '2', author: '群聊作者', description: '群聊测试', source: 'user', usage_count: 0, edictum_configs: [], chat_enabled: false, config_schema: {}, applicability: { session_types: ['platform'], platforms: ['onebot'] }, capabilities: { skills: { group_skill: '群聊技能' }, handlers: { pre_process: '前处理描述' } } },
    ];
    const cors = { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true', 'Access-Control-Allow-Headers': 'Content-Type', 'Access-Control-Allow-Methods': 'GET,POST,PATCH,PUT,OPTIONS' };
    browser = await runtime.browser;
    const page = await browser.newPage();
    const errors = [];
    let installs = 0;
    let discards = 0;
    let globalOverrides = { note: 'old' };
    let globalRevision = 'first';
    let failGlobalSave = false;
    let runtimeRetries = 0;
    const retryScopes = [];
    const applicationScope = ({ operation_id, ...scope }) => { assert.match(operation_id, /^[\w-]{16,128}$/); return scope; };
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
    let holdRuntime = false;
    let releaseRuntime;
    let runtimeHeld;
    page.on('pageerror', (error) => errors.push(error.message));
    await page.route(origin + '/ui-config.json', (route) => route.fulfill({ json: { backend_api: 'http://127.0.0.1:19870', control_api: 'http://127.0.0.1:19871', chat_api: 'http://127.0.0.1:19872' } }));
    await page.route(/^http:\/\/127\.0\.0\.1:1987[012]\//, async (route) => {
      if (route.request().method() === 'OPTIONS') return route.fulfill({ status: 204, headers: cors });
      const pathname = new URL(route.request().url()).pathname;
      if (pathname === '/config/versions') {
        const names = (new URL(route.request().url()).searchParams.get('resources') || '').split(',');
        return route.fulfill({ json: { ok: true, resources: Object.fromEntries(names.map((name) => [name, { state: 'available', change_token: JSON.stringify(plugins) }])) }, headers: cors });
      }
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
        const failed = runtimeFailed;
        if (holdRuntime) {
          holdRuntime = false;
          await new Promise((resolve) => { releaseRuntime = resolve; runtimeHeld(); });
        }
        if (route.request().failure()) return;
        return route.fulfill({ json: { ok: true, services: [{ target: 'Chat', status: 'stopped', instances: [] }, { target: 'Edictum', status: 'available', instances: [{ platform_id: 'onebot', session_id: 'group-1', location_id: 'assistant', plugin: { name: 'builtin_probe', status: failed ? 'error' : 'loaded', enabled: true, error: failed ? '加载测试失败' : '', drift: failed, capabilities: { applied: caps, desired: caps, loaded: failed ? { skills: { probe_skill: true } } : caps, effective: { services: [{ name: 'probe_service', description: '测试服务', requested_enabled: true, enabled: false, reason: 'replaced_by', replaced_by: 'replacement.services.new_service' }] } } } }] }] }, headers: cors });
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
      if (pathname === '/config/plugins/reconcile') { retryScopes.push(route.request().postDataJSON()); runtimeRetries++; runtimeFailed = false; return route.fulfill({ json: { ok: true, runtime: [{ target: 'Chat', status: 'applied', sessions: [] }, { target: 'Edictum', status: 'next_activation', sessions: [] }] }, headers: cors }); }
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
    await page.getByRole('region', { name: '插件交互关系' }).getByText('可选依赖 · formatter', { exact: true }).waitFor();
    await page.getByText('old_tool → probe_tool', { exact: true }).waitFor();
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
    assert.deepEqual(applicationScope(retryScopes[0]), { plugin_name: 'builtin_probe' });
    await page.getByRole('button', { name: '恢复默认值', exact: true }).click();
    assert.equal(await page.getByLabel('note', { exact: true }).inputValue(), 'default');
    await page.getByRole('button', { name: '保存全局参数', exact: true }).click();
    await page.getByText('全局参数已保存', { exact: true }).waitFor();
    assert.deepEqual(globalOverrides, {});
    runtimeFailed = true;
    await page.getByRole('button', { name: '使用位置', exact: true }).click();
    await page.getByRole('button', { name: '添加到此位置', exact: true }).click();
    await page.getByLabel('工具 probe_tool', { exact: true }).uncheck();
    for (const name of ['插件服务 probe_service', '技能 probe_skill', 'MCP probe_mcp', '前处理 probe_handler', '命令 probe_command']) {
      await page.getByLabel(name, { exact: true }).uncheck();
      await page.getByLabel(name, { exact: true }).check();
    }
    const beforePoll = runtimeReads;
    await page.waitForResponse((response) => new URL(response.url()).pathname === '/config/plugins/builtin_probe/runtime' && runtimeReads > beforePoll);
    assert.equal(await page.getByLabel('工具 probe_tool', { exact: true }).isChecked(), false);
    await page.getByText('工具：未加载', { exact: true }).waitFor();
    await page.getByText('插件服务：probe_service (期望启用; 已被替代: replacement.services.new_service)', { exact: true }).waitFor();
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
    await page.getByRole('button', { name: '重试运行应用', exact: true }).click();
    await page.getByText('已加载并启用', { exact: true }).waitFor();
    assert.deepEqual(applicationScope(retryScopes[1]), { plugin_name: 'builtin_probe', kind: 'chat', location_id: 'chat' });
    runtimeFailed = true;
    await page.getByText('配置与运行状态不同步 · 加载测试失败', { exact: true }).waitFor();
    const heldRuntimeStarted = new Promise((resolve) => { runtimeHeld = resolve; });
    holdRuntime = true;
    const beforeHeldPoll = runtimeReads;
    await heldRuntimeStarted;
    assert.equal(runtimeReads, beforeHeldPoll + 1);
    assert.equal(typeof releaseRuntime, 'function');
    await page.getByRole('button', { name: '重试实例应用', exact: true }).click();
    await page.getByText('已加载并启用', { exact: true }).waitFor();
    assert.equal(runtimeRetries, 3);
    assert.deepEqual(applicationScope(retryScopes[2]), { plugin_name: 'builtin_probe' });
    assert.equal(runtimeReads, beforeHeldPoll + 2);
    const obsoleteRuntime = page.waitForResponse((response) => new URL(response.url()).pathname === '/config/plugins/builtin_probe/runtime');
    releaseRuntime();
    await obsoleteRuntime;
    await page.getByText('已加载并启用', { exact: true }).waitFor();
    assert.equal(await page.getByRole('button', { name: '重试实例应用', exact: true }).count(), 0);
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
    assert.deepEqual(retryScopes[3], { plugin_name: 'builtin_probe' });
    assert.deepEqual(errors, []);
    console.log('PASS: 插件目录搜索、来源与平台筛选、详情能力、直接打开和不存在提示');
    console.log('PASS: ZIP 上传预览、取消清理、显式安装、打开详情及刷新列表');
    console.log('PASS: 全局参数草稿跨刷新、离开确认、失败保留、零值保存、恢复默认及应用重试');
    console.log('PASS: 使用位置添加、六类能力编辑和实际替代原因展示、轮询保留草稿、失败保留、停用保留独立开关、移除及实际加载状态');
    console.log('PASS: 在途旧运行状态读取不复用于应用后刷新, 迟到响应不能覆盖应用后的新状态');
    console.log('PASS: 命名配置深链、全局继承与恢复、Chat 快捷配置统一保存并重试 Edictum 应用');
  } finally {
    if (browser) await runtime.closeContexts();
    await runtime.releaseServer(server);
  }

}

async function plugin_loading(runtime) {






  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  const server = await runtime.vite({ root, server: { host: '127.0.0.1', port: 0 } });
  let browser;
  try {
    await runtime.listen(server);
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
    browser = await runtime.browser;
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
    if (browser) await runtime.closeContexts();
    await runtime.releaseServer(server);
  }

}

async function edictum_settings(runtime) {






  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  const server = await runtime.vite({ root, server: { host: '127.0.0.1', port: 0 } });
  let browser;
  try {
    await runtime.listen(server);
    const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
    const configs = {
      assistant: { edictum_type: 'async_simple', enabled: true, description: '', model_name: 'probe', params: { system_prompt: '旧提示词', custom: '保留参数' }, plugins: [] },
    };
    const writes = [];
    let configReads = 0;
    let failSave = false;
    const cors = { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true', 'Access-Control-Allow-Headers': 'Content-Type', 'Access-Control-Allow-Methods': 'GET,POST,PATCH,OPTIONS' };
    browser = await runtime.browser;
    const page = await browser.newPage();
    await page.clock.install();
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
        '/config/edictum/plugins': { plugins: [{ name: 'probe_plugin', config_version: 1, description: '', version: '1', config_schema: { note: { type: 'str', default: '' } }, capabilities: { tools: { probe_tool: '测试工具' } } }] },
        '/config/plugins/probe_plugin/config': { ok: true, schema: { note: { type: 'string', default: '' } }, config: { note: '全局继承' }, overrides: { note: '全局继承' }, revision: 'first' },
        '/config/plugin-model-options': { options: {} },
        '/config/rag': { knowledge_bases: [] },
      };
      return route.fulfill({ json: responses[url.pathname] ?? { ok: true }, headers: cors });
    });
    await page.routeWebSocket(/ws:\/\/127\.0\.0\.1:1987[012]\//, () => {});
    await mockConfigVersions(page, origin, () => JSON.stringify({configs}));
  await page.goto(origin + '/sessions');
    await page.getByRole('button', { name: 'Edictum 流程', exact: true }).click();
    const dialog = page.getByRole('dialog');
    await page.getByTitle('管理插件', { exact: true }).click();
    await dialog.getByRole('button', { name: '添加', exact: true }).click();
    await dialog.getByText('继承全局配置', { exact: true }).waitFor();
    assert.equal(await dialog.getByLabel('note', { exact: true }).inputValue(), '全局继承');
    await dialog.getByText('继承全局配置', { exact: true }).waitFor();
    await dialog.getByLabel('note', { exact: true }).fill('保留草稿');
    await dialog.getByText('probe_tool', { exact: true }).locator('..').locator('..').getByRole('checkbox').uncheck();
    const readsBefore = configReads;
    configs.assistant.description = '后台更新的说明';
    await page.clock.fastForward(31000);
    await new Promise((resolve, reject) => {
      const deadline = Date.now() + 16000;
      const check = () => {
        if (configReads >= readsBefore + 1) return resolve();
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
    assert.equal(writes.at(-1).plugins[0].config_version, 1);
    await dialog.getByRole('button', { name: '恢复继承', exact: true }).click();
    assert.equal(await dialog.getByLabel('note', { exact: true }).inputValue(), '全局继承');
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
    if (browser) await runtime.closeContexts();
    await runtime.releaseServer(server);
  }

}

async function friend_tool_settings(runtime) {






  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  const server = await runtime.vite({ root, server: { host: '127.0.0.1', port: 0 } });
  let browser;
  try {
    await runtime.listen(server);
    const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
    browser = await runtime.browser;
    const page = await browser.newPage();
    const errors = [], writes = [];
    const handle = 'friend_manager_handle_request';
    const deletion = 'friend_manager_delete_friend';
    const send = 'friend_manager_send_request';
    let tools = { [deletion]: false }, toolRevision = 1;
    const inherited = { [handle]: false, [deletion]: true, [send]: true };
    page.on('pageerror', (error) => errors.push(error.message));
    await page.route(`${origin}/ui-config.json`, (route) => route.fulfill({ json: {
      backend_api: 'http://127.0.0.1:19870', control_api: 'http://127.0.0.1:19871', chat_api: 'http://127.0.0.1:19872',
    } }));
    await page.route(/^http:\/\/127\.0\.0\.1:1987[012]\//, (route) => {
      const request = route.request();
      let json = { ok: true }, status = 200;
      const url = new URL(request.url());
      if (url.pathname === '/config/models') json = {};
      if (url.pathname === '/config/session-classes') json = { configs: {} };
      if (url.pathname.endsWith('/session-plugin-config')) {
        if (request.method() === 'PUT') {
          const body = request.postDataJSON();
          writes.push(body);
          if (body.expected_tool_revision !== toolRevision) { status = 409; json = { error: '工具配置已更新' }; }
          else { tools = body.tool_overrides; toolRevision++; }
        }
        if (status === 200) json = { ok: true, schema: {}, config: {}, overrides: {}, inherited: {}, sources: {}, revision: 0,
          tool_overrides: tools, tool_revision: toolRevision, inherited_tools: inherited,
          tool_descriptions: { [handle]: '处理好友申请', [deletion]: '删除好友', [send]: '发送好友申请' } };
      }
      return route.fulfill({ status, json, headers: { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true',
        'Access-Control-Allow-Headers': 'Content-Type', 'Access-Control-Allow-Methods': 'GET,PUT,OPTIONS' } });
    });
    await page.routeWebSocket(/ws:\/\/127\.0\.0\.1:1987[012]\//, () => {});
    await mockConfigVersions(page, origin, () => JSON.stringify({}));
  await page.goto(origin);
    await page.evaluate(async () => {
      const React = await import('/node_modules/.vite/deps/react.js');
      const client = await import('/node_modules/.vite/deps/react-dom_client.js');
      const { SessionPluginSettingsModal } = await import('/src/components/common/SessionPluginSettingsModal.tsx');
      const host = document.createElement('div');
      document.body.appendChild(host);
      client.default.createRoot(host).render(React.default.createElement(SessionPluginSettingsModal,
        { context: { platformId: 'chat', sessionId: 'one' }, plugins: ['friend_manager'], onClose() {} }));
    });
    const dialog = page.getByRole('dialog');
    await dialog.getByLabel(deletion, { exact: true }).waitFor();
    assert.equal(await dialog.getByRole('checkbox').count(), 3);
    assert.equal(await dialog.getByLabel(deletion).isChecked(), false);
    assert.equal(await dialog.getByLabel(handle).isChecked(), false);
    await dialog.getByLabel(deletion).check();
    await dialog.getByRole('button', { name: '保存', exact: true }).click();
    await dialog.getByRole('status').waitFor();
    assert.deepEqual(writes[0], { expected_revision: 0, overrides: {}, expected_tool_revision: 1, tool_overrides: { [deletion]: true } });
    await dialog.getByLabel(handle).check();
    toolRevision++;
    await dialog.getByRole('button', { name: '保存', exact: true }).click();
    await dialog.getByRole('alert').waitFor();
    assert.deepEqual(tools, { [deletion]: true });
    assert.equal(await dialog.getByLabel(handle).isChecked(), true);
    await dialog.getByRole('button', { name: '重新加载' }).click();
    await page.waitForFunction((name) => document.querySelector(`input[aria-label="${name}"]`)?.checked === false, handle);
    assert.equal(await dialog.getByLabel(handle).isChecked(), false);
    await dialog.getByRole('button', { name: '恢复工具继承' }).click();
    await dialog.getByRole('button', { name: '保存', exact: true }).click();
    await dialog.getByRole('status').waitFor();
    assert.deepEqual(writes.at(-1).tool_overrides, {});
    assert.equal(writes.at(-1).expected_tool_revision, 3);
    assert.equal(await dialog.getByLabel(deletion).isChecked(), true);
    assert.deepEqual(errors, []);
    console.log('PASS: 实例工具覆盖可见可编辑, 双修订保存, 冲突保留草稿, 恢复工具继承');
  } finally {
    await runtime.closeContexts();
    await runtime.releaseServer(server);
  }

}

async function main_merge(runtime) {








  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  const meta = load(await readFile(path.join(root, '../satrap/expend/plugins/group_admin/meta.yaml'), 'utf8'));
  const schema = Object.fromEntries(Object.entries(meta.config_schema).map(([key, value]) => [key, {
    options: [], required: false, nullable: false, session_overridable: true, ...value,
  }]));
  const defaults = Object.fromEntries(Object.entries(schema).map(([key, value]) => [key, value.default]));
  const plugin = { ...meta, config_schema: schema, source: 'builtin', usage_count: 0, edictum_configs: [], chat_enabled: false,
    capabilities: Object.fromEntries(['tools', 'skills', 'handlers', 'commands', 'mcp'].map(kind => [kind, meta[kind] || {}])) };
  const server = await runtime.vite({ root, server: { host: '127.0.0.1', port: 0 } });
  let browser;
  try {
    await runtime.listen(server);
    const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
    const cors = { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true',
      'Access-Control-Allow-Headers': 'Content-Type', 'Access-Control-Allow-Methods': 'GET,PUT,POST,OPTIONS' };
    browser = await runtime.browser;
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
        return reply({ ok: true, schema, defaults, overrides, config: { ...defaults, ...overrides }, revision, runtime: [] });
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
    await mockConfigVersions(page, origin, () => JSON.stringify({revision, config}));
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
    if (browser) await runtime.closeContexts();
    await runtime.releaseServer(server);
  }

}

async function skill_library(runtime) {
  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  const server = await runtime.vite({ root, server: { host: '127.0.0.1', port: 0 } });
  try {
    await runtime.listen(server);
    const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
    const page = await runtime.browser.newPage({ viewport: { width: 1280, height: 1000 } });
    page.setDefaultTimeout(10000);
    const errors = [], writes = [];
    page.on('pageerror', (error) => errors.push(error.message));
    await page.routeWebSocket(/ws:\/\/127\.0\.0\.1:1987[012]\//, () => {});
    const cors = { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true',
      'Access-Control-Allow-Headers': 'Content-Type', 'Access-Control-Allow-Methods': 'GET,POST,PUT,DELETE,OPTIONS' };
    let items = [], conflict = false;
    await page.route(origin + '/ui-config.json', (route) => route.fulfill({ json: {
      backend_api: 'http://127.0.0.1:19870', control_api: 'http://127.0.0.1:19871', chat_api: 'http://127.0.0.1:19872',
    } }));
    await page.route(/^http:\/\/127\.0\.0\.1:1987[012]\//, async (route) => {
      const request = route.request(), url = new URL(request.url());
      if (request.method() === 'OPTIONS') return route.fulfill({ status: 204, headers: cors });
      if (url.pathname === '/config/plugins') return route.fulfill({ json: { plugins: [{
        name: 'skills', description: '按任务加载技能', source: 'builtin', usage_count: 0, config_schema: {}, capabilities: {},
      }] }, headers: cors });
      if (url.pathname.startsWith('/api/skills')) {
        if (request.method() === 'GET') return route.fulfill({ json: { items }, headers: cors });
        const payload = request.postDataJSON();
        writes.push({ method: request.method(), payload });
        if (conflict) return route.fulfill({ status: 409, json: { error: '版本已变化，请刷新后再保存' }, headers: cors });
        if (request.method() === 'DELETE') { items = []; return route.fulfill({ json: { ok: true }, headers: cors }); }
        if (request.method() === 'PUT') assert.equal(payload.expected_revision, items[0].revision);
        const item = { name: payload.name || 'weekly', description: payload.description || '整理工作记录', markdown: payload.markdown || '# 远程技能',
          source: payload.filename || payload.url || 'upload', revision: request.method() === 'POST' ? 1 : items[0].revision + 1,
          enabled: payload.enabled, scopes: payload.scopes };
        items = [item];
        return route.fulfill({ json: { ok: true, item }, headers: cors });
      }
      return route.fulfill({ json: url.pathname === '/status' || url.pathname === '/api/health' ? { running: false, adapters: {} } : {}, headers: cors });
    });
    await mockConfigVersions(page, origin, () => JSON.stringify({ revision: 1 }));
    await page.goto(`${origin}/plugins/skills?tab=skills`);
    await page.getByRole('region', { name: '技能库' }).getByText('尚未导入外部技能').waitFor();
    await page.getByRole('button', { name: '导入技能', exact: true }).click();
    await page.getByLabel('Markdown 文件', { exact: true }).setInputFiles({ name: 'weekly.md', mimeType: 'text/markdown', buffer: Buffer.from('---\nname: weekly\ndescription: 整理工作记录\n---\n# 周报步骤') });
    await page.getByLabel('Markdown 正文', { exact: true }).waitFor();
    await page.waitForFunction(() => document.querySelector('textarea')?.value.includes('# 周报步骤'));
    await page.getByRole('button', { name: '保存技能', exact: true }).click();
    await page.getByText('weekly', { exact: true }).waitFor();
    assert.equal(writes[0].payload.filename, 'weekly.md');
    await page.getByRole('button', { name: '查看和编辑', exact: true }).click();
    await page.getByLabel('用途简介', { exact: true }).fill('新版用途');
    await page.getByLabel('允许使用此技能').uncheck();
    conflict = true;
    await page.getByRole('button', { name: '保存技能', exact: true }).click();
    await page.getByText('版本已变化，请刷新后再保存', { exact: false }).waitFor();
    assert.equal(await page.getByLabel('用途简介', { exact: true }).inputValue(), '新版用途');
    conflict = false;
    await page.getByRole('button', { name: '保存技能', exact: true }).click();
    await page.getByText('已停用', { exact: true }).waitFor();
    await page.getByRole('button', { name: '删除', exact: true }).click();
    await page.getByRole('button', { name: '确认', exact: true }).click();
    await page.getByText('尚未导入外部技能', { exact: true }).waitFor();
    await page.getByRole('button', { name: '导入技能', exact: true }).click();
    await page.getByLabel('导入方式', { exact: true }).selectOption('url');
    await page.getByLabel('Markdown 地址', { exact: true }).fill('https://example.com/SKILL.md');
    await page.getByRole('button', { name: '保存技能', exact: true }).click();
    await page.getByText('weekly', { exact: true }).waitFor();
    assert.equal(writes.at(-1).payload.url, 'https://example.com/SKILL.md');
    await page.setViewportSize({ width: 390, height: 844 });
    await mkdir(path.join(root, 'test-results', 'skill-library'), { recursive: true });
    await page.screenshot({ path: path.join(root, 'test-results', 'skill-library', 'narrow.png'), fullPage: true });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    assert.deepEqual(errors, []);
    console.log('PASS: 技能文件与 URL 导入, 版本冲突保留草稿, 编辑停用, 删除和窄屏布局');
  } finally {
    await runtime.closeContexts();
    await runtime.releaseServer(server);
  }
}

export const scenarios = [
  ["skill-library", skill_library],
  ["plugins", plugins],
  ["plugin-loading", plugin_loading],
  ["edictum-settings", edictum_settings],
  ["friend-tool-settings", friend_tool_settings],
  ["main-merge", main_merge]
];
if (process.argv[1] && pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url) await runSuite(scenarios);
