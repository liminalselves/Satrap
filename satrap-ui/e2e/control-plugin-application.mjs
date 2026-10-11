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
  const page = await browser.newPage(); await page.clock.install();
  const errors = [], writes = [], applications = [], queries = [], operations = new Map();
  page.on('pageerror', (error) => errors.push(error.message));
  page.on('dialog', (dialog) => dialog.accept());
  const schema = { note: { type: 'string', default: 'default', description: '备注' } };
  const caps = { tools: { probe_tool: true } };
  const plugins = ['probe', 'other'].map((name) => ({ name, version: '1', source: 'builtin', description: '', config_schema: schema,
    capabilities: { tools: { probe_tool: '测试工具' } }, usage_count: 2, edictum_configs: ['assistant'], chat_enabled: true }));
  let global = { note: 'old' }, globalRevision = 'one', failSave = false, legacy = false, holdSave = false, releaseSave;
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
  await page.clock.runFor(121000);
  await page.getByRole('alert').filter({ hasText: '全局参数已保存, 运行时应用未确认' }).waitFor();
  assert.equal(applications.length, 1); assert.equal(await page.getByLabel('note', { exact: true }).inputValue(), '已保存的草稿');
  firstJob.result = success;
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
  if (browser) await browser.close();
  await server.close();
}
