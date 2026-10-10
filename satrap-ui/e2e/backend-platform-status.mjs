import assert from 'node:assert/strict';
import path from 'node:path';
import fs from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';
import { createServer } from 'vite';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const artifacts = path.join(root, 'test-results', 'backend-platform-status');
const server = await createServer({ root, server: { host: '127.0.0.1', port: 0 } });
let browser;
try {
  await fs.mkdir(artifacts, { recursive: true });
  await server.listen();
  const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
  browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', (error) => { errors.push(error.message); console.error('pageerror:', error.stack); });
  const state = { running: true, runtime: 'r1', account: '10', reloads: 0, writes: 0,
    applyFailure: false, saveFailure: false, probeFailure: false, sourcesDown: false, staleHealth: false };
  const sessions = { demo: { enabled: true, class_path: 'demo.Demo', is_async: true, params: {} } };
  const edictum = { named: { enabled: true, edictum_type: 'simple', model_name: 'm', plugins: [], params: {} } };
  const platforms = [
    { id: 'bot', type: 'onebot', enable: true, session_provider: 'session_class', session_type: 'demo', settings: {} },
    { id: 'misskey', type: 'misskey', enable: true, session_provider: 'edictum', session_type: 'named', settings: {} },
  ];
  let socket;
  let socketCount = 0;
  let stopRelease;
  let probeRelease;
  let holdStop = false;
  let holdProbe = false;
  let probeCalls = 0;
  const adapters = () => Object.fromEntries(platforms.map((item) => [item.id, {
    status: 'running', started: true, config_type: item.type, session_type: item.session_type,
    session_provider: item.session_provider, client_self_id: item.id === 'bot' ? state.account : '',
  }]));
  const health = () => ({ running: state.running || state.staleHealth, runtime_id: state.runtime,
    adapters: state.running || state.staleHealth ? adapters() : {}, platform_config: platforms.map((item) => ({
      id: item.id, status: state.applyFailure ? 'failed' : 'applied', saved_revision: 'v1', active_revision: 'v1',
      error: state.applyFailure ? '模拟应用失败' : undefined,
    })) });
  await page.routeWebSocket('ws://127.0.0.1:19870/ws/status**', (connection) => { socket = connection; ++socketCount; });
  await context.route('**/*', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.origin === origin && url.pathname !== '/ui-config.json') return route.continue();
    const headers = { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true',
      'Access-Control-Allow-Headers': 'Content-Type', 'Access-Control-Allow-Methods': 'GET,POST,PATCH,OPTIONS' };
    const reply = (json, status = 200) => route.fulfill({ status, json, headers });
    if (request.method() === 'OPTIONS') return route.fulfill({ status: 204, headers });
    const pathname = url.pathname;
    if (pathname === '/ui-config.json') return reply({ backend_api: 'http://127.0.0.1:19870', control_api: 'http://127.0.0.1:19871', chat_api: 'http://127.0.0.1:19872' });
    if (pathname === '/api/auth/session') return reply({ ok: true });
    if (state.sourcesDown && ['/api/health', '/status'].includes(pathname)) return route.abort('connectionfailed');
    if (pathname === '/api/health') return reply(health());
    if (pathname === '/status') return reply({ running: state.running, managed: state.running });
    if (pathname === '/stop') {
      if (holdStop) await new Promise((resolve) => { stopRelease = resolve; });
      state.running = false;
      return reply({ ok: true });
    }
    if (pathname === '/start' || pathname === '/restart') {
      state.running = true;
      state.staleHealth = false;
      state.runtime = `r${Number(state.runtime.slice(1)) + 1}`;
      return reply({ ok: true });
    }
    const toggle = pathname.match(/^\/config\/(session-classes|edictum\/sessions)\/([^/]+)\/(enable|disable)$/);
    if (toggle) {
      ++state.writes;
      if (state.saveFailure) return reply({ ok: false, error: '模拟保存失败' });
      const configs = toggle[1] === 'session-classes' ? sessions : edictum;
      configs[toggle[2]].enabled = toggle[3] === 'enable';
      return reply({ ok: true, config: configs[toggle[2]] });
    }
    if (pathname === '/config/session-classes') return reply(sessions);
    if (pathname === '/config/edictum/sessions') return reply(edictum);
    if (pathname === '/config/edictum/types') return reply({ types: [{ name: 'simple', is_async: true,
      description: '测试会话', config_schema: {}, capabilities: { plugins: false, mcp: false, stream: false } }] });
    if (pathname === '/config/edictum/plugins') return reply({ plugins: [] });
    if (pathname === '/config/platforms') return reply({ ok: true, platforms, revision: 'v1' });
    if (pathname === '/api/config/reload') { ++state.reloads; return reply({ ok: true, platforms: health().platform_config, edictum_sessions: [] }); }
    if (pathname === '/api/sessions' || pathname === '/config/session-instances') return reply({ sessions: [] });
    if (pathname === '/config') return reply({ ok: true, config: { api: { host: '127.0.0.1', port: 19870 } }, exists: true });
    if (/^\/api\/platforms\/(bot|misskey)\/connection-test$/.test(pathname)) {
      ++probeCalls;
      assert.equal(request.method(), 'POST');
      assert.deepEqual(request.postDataJSON(), {});
      if (holdProbe) await new Promise((resolve) => { probeRelease = resolve; });
      return reply({ ok: !state.probeFailure, detail: state.probeFailure ? '通信请求超时 (8 秒)' : '通信正常, 对端已响应', elapsed_ms: 12 });
    }
    if (/\/groups\/(accounts|[^/]+\/info)$/.test(pathname)) {
      throw new Error('通信检查不应请求群管理接口');
    }
    if (pathname.includes('/wake/diagnostics')) return reply({ records: [], available: true });
    return reply({});
  });
  await page.goto(`${origin}/sessions`);
  const switchDemo = page.getByRole('switch', { name: '会话配置 demo' });
  await switchDemo.waitFor();
  await page.locator('header').getByText('后端运行中', { exact: true }).waitFor();
  const connections = socketCount;
  assert.equal(await page.getByTitle('禁用', { exact: true }).count(), 0);
  assert.equal(await page.getByRole('button', { name: '创建会话', exact: true }).count(), 1);
  await switchDemo.click();
  await page.getByText('已保存并应用', { exact: true }).waitFor();
  assert.equal(sessions.demo.enabled, false);
  assert.equal(state.reloads, 1);
  await page.screenshot({ path: path.join(artifacts, 'session-toggle.png'), fullPage: true });

  await page.locator('aside').getByRole('link', { name: '平台状态', exact: true }).click();
  await page.getByText('会话配置停用', { exact: true }).waitFor();
  socket.send(JSON.stringify({ type: 'status', data: { running: true, adapters: adapters() } }));
  await page.getByText('保存版本 v1 · 生效版本 v1', { exact: true }).first().waitFor();
  assert.equal(socketCount, connections);
  await page.getByRole('button', { name: '测试连接 bot', exact: true }).click();
  const dialog = page.getByRole('dialog');
  assert.equal(await dialog.getByRole('textbox').count(), 0);
  await dialog.getByRole('button', { name: '开始检查', exact: true }).click();
  await dialog.getByText('通信正常, 对端已响应', { exact: true }).waitFor();
  assert.equal(probeCalls, 1);
  await page.screenshot({ path: path.join(artifacts, 'connection-check.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  await page.screenshot({ path: path.join(artifacts, 'connection-check-mobile.png'), fullPage: true });
  await page.setViewportSize({ width: 1440, height: 1000 });
  state.probeFailure = true;
  await dialog.getByRole('button', { name: '开始检查', exact: true }).click();
  await dialog.getByText('通信请求超时 (8 秒)', { exact: true }).waitFor();
  state.probeFailure = false;
  holdProbe = true;
  await dialog.getByRole('button', { name: '开始检查', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('[role="dialog"] [aria-busy="true"]'));
  while (!probeRelease) await new Promise((resolve) => setTimeout(resolve, 10));
  state.account = '11';
  socket.send(JSON.stringify({ type: 'status', data: { running: true, adapters: adapters() } }));
  await dialog.getByText('平台或配置已变化, 旧结果已清除, 请重新测试', { exact: true }).waitFor();
  probeRelease();
  holdProbe = false;
  await dialog.getByRole('button', { name: '开始检查', exact: true }).waitFor();
  assert.equal(await dialog.getByText('通信正常, 对端已响应', { exact: true }).count(), 0);
  await dialog.getByRole('button', { name: '关闭检查', exact: true }).click();
  await page.getByRole('button', { name: '测试连接 misskey', exact: true }).click();
  await dialog.getByRole('button', { name: '开始检查', exact: true }).click();
  await dialog.getByText('通信正常, 对端已响应', { exact: true }).waitFor();
  assert.equal(probeCalls, 4);
  await dialog.getByRole('button', { name: '关闭检查', exact: true }).click();

  await page.locator('aside').getByRole('link', { name: 'Agent 配置', exact: true }).click();
  await page.getByText('Edictum 流程', { exact: true }).click();
  const switchNamed = page.getByRole('switch', { name: '会话配置 named' });
  state.saveFailure = true;
  await switchNamed.click();
  await page.getByText('保存失败', { exact: true }).waitFor();
  assert.equal(await switchNamed.getAttribute('aria-checked'), 'true');
  state.saveFailure = false;
  state.applyFailure = true;
  await switchNamed.click();
  await page.getByText('已保存, 应用未成功', { exact: true }).waitFor();
  assert.equal(await switchNamed.getAttribute('aria-checked'), 'false');
  const writes = state.writes;
  state.applyFailure = false;
  await page.getByRole('button', { name: '重试应用', exact: true }).click();
  await page.getByText('已保存并应用', { exact: true }).waitFor();
  assert.equal(state.writes, writes);

  await page.locator('aside').getByRole('link', { name: '仪表盘', exact: true }).click();
  await page.getByText('会话配置停用', { exact: true }).first().waitFor();
  holdStop = true;
  await page.getByRole('button', { name: '停止后端', exact: true }).click();
  await page.getByRole('button', { name: '后端停止中', exact: true }).waitFor();
  assert.equal(await page.getByRole('button', { name: '后端停止中', exact: true }).isDisabled(), true);
  await page.locator('aside').getByRole('link', { name: '系统设置', exact: true }).click();
  await page.getByRole('button', { name: '后端停止中', exact: true }).waitFor();
  state.staleHealth = true;
  stopRelease();
  await page.getByRole('button', { name: '启动后端', exact: true }).waitFor();
  await page.locator('header').getByText('后端未运行', { exact: true }).waitFor();
  const reloadsBeforeStoppedToggle = state.reloads;
  await page.locator('aside').getByRole('link', { name: 'Agent 配置', exact: true }).click();
  await page.getByRole('switch', { name: '会话配置 demo' }).click();
  await page.getByText('已保存, 后端启动后生效', { exact: true }).waitFor();
  assert.equal(sessions.demo.enabled, true);
  assert.equal(state.reloads, reloadsBeforeStoppedToggle);
  await page.locator('aside').getByRole('link', { name: '系统设置', exact: true }).click();
  await page.getByRole('button', { name: '启动后端', exact: true }).click();
  await page.locator('header').getByText('后端运行中', { exact: true }).waitFor();
  await page.getByRole('button', { name: '停止后端', exact: true }).waitFor();
  state.sourcesDown = true;
  await page.getByRole('button', { name: '刷新状态', exact: true }).click();
  await page.locator('header').getByText('后端状态未知', { exact: true }).waitFor();
  assert.equal(await page.getByRole('button', { name: '启动后端', exact: true }).isDisabled(), true);
  assert.deepEqual(errors, []);
  console.log('backend/platform status E2E passed; screenshots:', artifacts);
} finally {
  await browser?.close();
  await server.close();
}
