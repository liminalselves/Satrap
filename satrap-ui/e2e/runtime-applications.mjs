import { autoAcceptConfirmations } from "./confirmation.mjs";
import assert from "node:assert/strict";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { runSuite, waitForRequestEntry, emptyConfigResponse, assertNoConfigErrors } from './runtime.mjs';
import { pathToFileURL } from 'node:url';

async function control_plugin_application(runtime) {







  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  const server = await runtime.vite({ root, server: { host: '127.0.0.1', port: 0 } });
  let browser, stopConfirmations;
  try {
    await runtime.listen(server);
    const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
    browser = await runtime.browser;
    const page = await browser.newPage();
    stopConfirmations = autoAcceptConfirmations(page); await page.clock.install();
    const errors = [], writes = [], applications = [], queries = [], operations = new Map();
    page.on('pageerror', (error) => errors.push(error.message));
    page.on('dialog', (dialog) => dialog.accept());
    const schema = { note: { type: 'string', default: 'default', description: '备注' } };
    const caps = { tools: { probe_tool: true } };
    const plugins = ['probe', 'other'].map((name) => ({ name, version: '1', source: 'builtin', description: '', config_schema: schema,
      capabilities: { tools: { probe_tool: '测试工具' } }, usage_count: 2, edictum_configs: ['assistant'], chat_enabled: true }));
    let global = { note: 'old' }, globalRevision = 'one', failSave = false, legacy = false, holdSave = false, releaseSave;
    let holdQuery = false, releaseQuery;
    const locations = [
      { kind: 'chat', id: 'chat', label: 'Chat', present: true, enabled: true, capabilities: structuredClone(caps), revision: 'chat-one', parent_enabled: true, availability: { allowed: true } },
      { kind: 'edictum', id: 'assistant', label: 'assistant', present: true, enabled: true, capabilities: structuredClone(caps), revision: 'edictum-one', parent_enabled: true, availability: { allowed: true } },
    ];
    const unchangedChat = structuredClone(locations[0]);
    const success = { ok: true, runtime: [{ target: 'Chat', status: 'applied', sessions: [] }, { target: 'Edictum', status: 'next_activation', sessions: [] }] };
    const stage = (target, phase, status = 'running') => ({ platform_id: target, session_id: '', status, phase, phase_elapsed_ms: 1, timings_ms: {} });
    const snapshot = (id) => {
      const job = operations.get(id), status = job.result ? job.result.ok ? 'completed' : 'failed' : 'running';
      return { operation_id: id, status, created_at: 1, elapsed_ms: 1, stage: stage('', 'applying', status),
        instances: [stage('Chat', job.phase, status), stage('Edictum/onebot', job.phase, status)],
        instances_limited: false, result: job.result, error: '' };
    };
    const cors = { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true',
      'Access-Control-Allow-Headers': 'Content-Type', 'Access-Control-Allow-Methods': 'GET,PUT,POST,OPTIONS' };
    await page.route(origin + '/ui-config.json', (route) => route.fulfill({ json: {
      backend_api: 'http://127.0.0.1:19870', control_api: 'http://127.0.0.1:19871', chat_api: 'http://127.0.0.1:19872',
    } }));
    await page.route(/^http:\/\/127\.0\.0\.1:1987[012]\//, async (route) => {
      const request = route.request(), url = new URL(request.url()), pathname = url.pathname;
      const reply = (json, status = 200) => route.fulfill({ json, status, headers: cors });
      if (request.method() === 'OPTIONS') return route.fulfill({ status: 204, headers: cors });
      if (pathname === '/config/versions') return reply({ ok: true, resources: Object.fromEntries((url.searchParams.get('resources') || '').split(',').map((name) => [name, { state: 'available', change_token: globalRevision }])) });
      if (/\/config\/plugins\/(probe|other)\/config$/.test(pathname)) {
        if (request.method() === 'PUT') {
          const body = request.postDataJSON(); writes.push({ pathname, body });
          assert.equal(body.apply_runtime, false); assert.equal(body.expected_revision, globalRevision);
          if (failSave) return reply({ error: '保存冲突测试' }, 409);
          global = body.config; globalRevision += '-saved';
          if (holdSave) { holdSave = false; await new Promise((resolve) => { releaseSave = resolve; }); }
        }
        return reply({ ok: true, schema, config: { note: 'default', ...global }, overrides: global, revision: globalRevision,
          saved: request.method() === 'PUT', ...(legacy && request.method() === 'PUT' ? { runtime: success.runtime } : {}) });
      }
      if (pathname === '/config/plugins/probe/usages') {
        if (request.method() === 'PUT') {
          const body = request.postDataJSON(); writes.push({ pathname, body });
          assert.equal(body.apply_runtime, false);
          const location = locations.find((row) => row.kind === body.kind && row.id === body.location_id);
          assert.equal(body.expected_revision, location.revision);
          Object.assign(location, body.state); location.revision += '-saved';
        }
        return reply({ ok: true, locations, saved: request.method() === 'PUT' });
      }
      if (pathname === '/config/plugins/probe/runtime') return reply({ ok: true, services: [{ target: 'Chat', status: 'stopped', instances: [] }, { target: 'Edictum', status: 'available', instances: [] }] });
      if (pathname === '/config/plugins/reconcile') {
        assert.equal(request.method(), 'POST'); assert.equal(url.port, '19871');
        const body = request.postDataJSON(); assert.match(body.operation_id, /^[\w-]{16,128}$/);
        assert.equal(operations.has(body.operation_id), false); applications.push(body);
        operations.set(body.operation_id, { phase: 'waiting_lock', result: applications.length === 3 ? success : null });
        return reply(snapshot(body.operation_id), 202);
      }
      if (pathname.startsWith('/config/plugin-operations/')) {
        assert.equal(request.method(), 'GET'); assert.equal(url.port, '19871');
        const id = pathname.split('/').at(-1); queries.push(id);
        if (holdQuery) { holdQuery = false; await new Promise((resolve) => { releaseQuery = resolve; }); }
        return operations.has(id) ? reply(snapshot(id)) : reply({ error: '任务不存在' }, 404);
      }
      const responses = { '/config/plugins': { plugins }, '/config/models': {}, '/config/session-classes': {},
        '/config/edictum/sessions': {}, '/config/edictum/types': { types: [] }, '/config/edictum/plugins': { plugins },
        '/config/platforms': { platforms: [] }, '/api/sessions': { sessions: [] } };
      if (!(pathname in responses) && !['/api/auth/session', '/api/health', '/api/chat/health', '/health'].includes(pathname)) {
        if (!pathname.includes('auth') && !pathname.includes('status')) errors.push('未预期接口: ' + pathname);
      }
      return reply(responses[pathname] || { ok: true, running: true, adapters: {} });
    });
    await page.routeWebSocket(/ws:\/\/127\.0\.0\.1:1987[012]\//, () => {});
    await page.goto(origin + '/plugins/probe?tab=config');
    await page.getByLabel('note', { exact: true }).fill('已保存的草稿');
    failSave = true;
    await page.getByRole('button', { name: '保存全局参数', exact: true }).click();
    await page.getByRole('alert').filter({ hasText: '保存冲突测试' }).waitFor();
    assert.equal(applications.length, 0); assert.equal(await page.getByLabel('note', { exact: true }).inputValue(), '已保存的草稿');
    failSave = false;
    await page.getByRole('button', { name: '保存全局参数', exact: true }).click();
    await page.getByText('全局参数已保存', { exact: true }).waitFor();
    await page.getByRole('status').filter({ hasText: '等待会话空闲' }).first().waitFor();
    const first = applications[0], firstJob = operations.get(first.operation_id);
    assert.equal(first.plugin_name, 'probe'); assert.equal(first.kind, undefined); assert.equal(writes.length, 2);
    firstJob.phase = 'install'; await page.clock.runFor(1100);
    await page.getByRole('status').filter({ hasText: '安装插件' }).first().waitFor();
    await page.clock.runFor(116000);
    holdQuery = true;
    const queryStarted = page.waitForRequest((request) => request.url().includes('/config/plugin-operations/'));
    const queryAborted = page.waitForEvent('requestfailed', { predicate: (request) => request.url().includes('/config/plugin-operations/') });
    await page.clock.runFor(1100); await queryStarted;
    await page.clock.runFor(6000);
    await page.getByRole('alert').filter({ hasText: '全局参数已保存, 运行时应用未确认' }).waitFor();
    await queryAborted;
    assert.equal(applications.length, 1); assert.equal(await page.getByLabel('note', { exact: true }).inputValue(), '已保存的草稿');
    firstJob.result = success;
    assert.equal(typeof releaseQuery, 'function'); releaseQuery();
    await page.clock.runFor(100);
    await page.getByRole('alert').filter({ hasText: '全局参数已保存, 运行时应用未确认' }).waitFor();
    await page.getByRole('button', { name: '查询应用状态', exact: true }).click();
    await page.getByRole('status').filter({ hasText: '运行应用已结束' }).waitFor();
    assert.equal(applications.length, 1); assert.equal(writes.length, 2); assert(queries.includes(first.operation_id));

    await page.getByRole('button', { name: '使用位置', exact: true }).click();
    await page.getByLabel('使用位置', { exact: true }).selectOption('edictum:assistant');
    await page.getByLabel('工具 probe_tool', { exact: true }).uncheck();
    await page.getByRole('button', { name: '保存使用配置', exact: true }).click();
    await page.getByText('使用配置已保存', { exact: true }).waitFor();
    await page.getByRole('status').filter({ hasText: '等待会话空闲' }).first().waitFor();
    const second = applications[1], secondJob = operations.get(second.operation_id);
    assert.equal(second.kind, 'edictum'); assert.equal(second.location_id, 'assistant');
    await page.clock.runFor(121000);
    await page.getByRole('alert').filter({ hasText: '使用配置已保存, 运行时应用未确认' }).waitFor();
    secondJob.result = { ok: false, runtime: [{ target: 'Edictum', status: 'error', error: '测试安装失败', sessions: [] }] };
    await page.getByRole('button', { name: '查询应用状态', exact: true }).click();
    await page.getByText('Edictum：应用失败 · 测试安装失败', { exact: true }).waitFor();
    assert.equal(await page.getByLabel('工具 probe_tool', { exact: true }).isChecked(), false);
    assert.equal(applications.length, 2); assert.equal(writes.length, 3); assert.deepEqual(locations[0], unchangedChat);
    await page.getByRole('button', { name: '重试运行应用', exact: true }).click();
    await page.getByRole('status').filter({ hasText: '运行应用已结束' }).waitFor();
    const { operation_id, ...scope } = applications[2];
    assert.deepEqual(scope, { plugin_name: 'probe', kind: 'edictum', location_id: 'assistant' });
    assert.notEqual(operation_id, second.operation_id); assert.equal(writes.length, 3);

    await page.getByRole('button', { name: '全局参数', exact: true }).click();
    legacy = true;
    await page.getByLabel('note', { exact: true }).fill('旧服务兼容');
    await page.getByRole('button', { name: '保存全局参数', exact: true }).click();
    await page.getByText('Chat：运行实例已应用', { exact: true }).waitFor();
    assert.equal(applications.length, 3); assert.equal(writes.length, 4);

    legacy = false; holdSave = true;
    await page.getByLabel('note', { exact: true }).fill('离开页面后的保存');
    const pendingSave = page.waitForRequest((request) => request.method() === 'PUT');
    await page.getByRole('button', { name: '保存全局参数', exact: true }).click();
    await pendingSave;
    await page.goto(origin + '/plugins/other?tab=config');
    assert.equal(typeof releaseSave, 'function');
    await releaseSave();
    await page.getByLabel('note', { exact: true }).waitFor();
    await page.clock.runFor(1100);
    assert.equal(applications.length, 3); assert.equal(await page.getByRole('button', { name: '查询应用状态', exact: true }).count(), 0);
    assert.deepEqual(errors, []);
    console.log('PASS: 控制服务保存/应用分离, 真实阶段与超时, 已保存草稿保留, 查询不写入, 位置范围重试, 旧服务不重复应用, 离开取消迟到保存后应用');
  } finally {
    if (stopConfirmations) await stopConfirmations();
    if (browser) await runtime.closeContexts();
    await runtime.releaseServer(server);
  }

}

async function runtime_application_progress(runtime) {







  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  const server = await runtime.vite({ root, server: { host: '127.0.0.1', port: 0 } });
  let browser, stopConfirmations;
  try {
    await runtime.listen(server);
    const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
    browser = await runtime.browser;
    const page = await browser.newPage();
    await page.addInitScript(() => { Object.defineProperty(Crypto.prototype, 'randomUUID', { value: undefined, configurable: true }); });
    stopConfirmations = autoAcceptConfirmations(page);
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
      if (url.pathname === '/config/snapshot') {
        const resource = url.searchParams.get('resource');
        const snapshot = emptyConfigResponse(url.href);
        if (resource === 'edictum_configs') snapshot.data = configs;
        if (resource === 'models') snapshot.data = { model: { model: 'probe-model' } };
        for (const version of Object.values(snapshot.versions)) version.change_token = JSON.stringify(configs);
        return reply(snapshot);
      }
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
    await assertNoConfigErrors(page);
    console.log('PASS: 202 实际阶段展示, 120 秒截止保留已保存事实和草稿, 查询不重发, 部分失败仅重试失败实例, 完整保存沿正确接口');
  } finally {
    if (stopConfirmations) await stopConfirmations();
    if (browser) await runtime.closeContexts();
    await runtime.releaseServer(server);
  }

}

async function scoped_agent_apply(runtime) {







  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  const scopeOnly = ({ operation_id, ...scope }) => { assert.match(operation_id, /^[\w-]{16,128}$/); return scope; };
  const server = await runtime.vite({ root, server: { host: '127.0.0.1', port: 0 } });
  let browser, stopConfirmations;
  try {
    await runtime.listen(server);
    const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
    browser = await runtime.browser;
    const page = await browser.newPage();
    await page.addInitScript(() => { Object.defineProperty(Crypto.prototype, 'randomUUID', { value: undefined, configurable: true }); });
    stopConfirmations = autoAcceptConfirmations(page);
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
      if (url.pathname === '/config/snapshot') {
        const resource = url.searchParams.get('resource');
        const snapshot = emptyConfigResponse(url.href);
        if (resource === 'edictum_configs') snapshot.data = configs;
        if (resource === 'models') snapshot.data = { model: { model: 'probe-model' } };
        for (const version of Object.values(snapshot.versions)) version.change_token = JSON.stringify(configs);
        return reply(snapshot);
      }
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
    assert.deepEqual(applications.map(scopeOnly), [{ config_name: 'assistant' }]);
    assert.equal(previews[0].config_name, 'assistant');
    assert.equal(previews[0].config.params.system_prompt, '新的提示词');
    releaseApply();
    await dialog.waitFor({ state: 'hidden' });
    await page.getByText('Edictum 配置已保存, 但后端热加载失败', { exact: true }).waitFor();
    for (const pathName of ['/config/edictum/sessions', '/config/edictum/types', '/config/edictum/plugins', '/config/platforms']) assert.equal(reads[pathName], before[pathName]);
    await page.getByRole('button', { name: '重试未同步会话', exact: true }).click();
    await page.getByText('配置同步重试完成', { exact: true }).waitFor();
    assert.deepEqual(scopeOnly(applications[1]), { session_refs: [{ platform_id: 'ob', session_id: 'failed-one' }] });
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
    if (stopConfirmations) await stopConfirmations();
    if (browser) await runtime.closeContexts();
    await runtime.releaseServer(server);
  }

}

async function scoped_plugin_apply(runtime) {






  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  const scopeOnly = ({ operation_id, ...scope }) => { assert.match(operation_id, /^[\w-]{16,128}$/); return scope; };
  const server = await runtime.vite({ root, server: { host: '127.0.0.1', port: 0 } });
  let browser;
  try {
    await runtime.listen(server);
    const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
    browser = await runtime.browser;
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
    await waitForRequestEntry(entered);
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
    await waitForRequestEntry(timedEntered);
    const failedRequest = page.waitForEvent('requestfailed', { timeout: 140000, predicate: (request) => new URL(request.url()).pathname === '/api/edictum/plugins/reconcile' });
    await page.clock.fastForward(119000);
    assert.equal(await dialog.getByRole('button', { name: '保存中…', exact: true }).isDisabled(), true);
    await page.clock.fastForward(1100);
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
    if (browser) await runtime.closeContexts();
    await runtime.releaseServer(server);
  }

}

export const scenarios = [
  ["control-plugin-application", control_plugin_application],
  ["runtime-application-progress", runtime_application_progress],
  ["scoped-agent-apply", scoped_agent_apply],
  ["scoped-plugin-apply", scoped_plugin_apply]
];
if (process.argv[1] && pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url) await runSuite(scenarios);
