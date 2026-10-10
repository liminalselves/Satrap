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
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.clock.install();
  const errors = [], authenticated = new Set(), authCalls = new Map(), businessCalls = new Map();
  const releases = [];
  let runtimeRelease, holdOthers = true;
  page.on('pageerror', (error) => errors.push(error.message));
  await context.route('**/*', async (route) => {
    const request = route.request(), url = new URL(request.url());
    if (url.origin === origin && url.pathname !== '/ui-config.json') return route.continue();
    const headers = { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true',
      'Access-Control-Allow-Headers': 'Content-Type, Authorization', 'Access-Control-Allow-Methods': 'GET,POST,OPTIONS' };
    const reply = (json, status = 200) => route.fulfill({ json, status, headers });
    if (request.method() === 'OPTIONS') return route.fulfill({ status: 204, headers });
    if (url.pathname === '/ui-config.json') {
      await new Promise((done) => { runtimeRelease = done; });
      return reply({ backend_api: 'http://127.0.0.1:19870', control_api: 'http://127.0.0.1:19871', chat_api: 'http://127.0.0.1:19872' });
    }
    if (url.pathname === '/auth/session') {
      authCalls.set(url.port, (authCalls.get(url.port) || 0) + 1);
      assert.equal(request.headers().authorization, 'Bearer synthetic-startup-token');
      if (holdOthers && url.port !== '19871') await new Promise((done) => { releases.push(done); });
      if (request.failure()) return;
      authenticated.add(url.port);
      return reply({ ok: true });
    }
    assert.equal(authenticated.has(url.port), true, `认证之前出现业务请求: ${url.port}${url.pathname}`);
    businessCalls.set(url.port, (businessCalls.get(url.port) || 0) + 1);
    if (url.pathname === '/config/versions') return reply({ ok: true, resources: Object.fromEntries((url.searchParams.get('resources') || '').split(',').map((name) => [name, { change_token: 'v1', state: 'available' }])) });
    if (url.pathname === '/status' || url.pathname === '/api/health') return reply({ running: true, managed: true, adapters: {} });
    if (url.pathname === '/config/plugins') return reply({ plugins: [{ name: 'available_probe', source: 'builtin', version: '1', usage_count: 0, capabilities: {}, config_schema: {} }] });
    throw new Error(`未匹配首屏认证夹具: ${request.method()} ${url.pathname}`);
  });
  const socketCalls = new Map();
  await page.routeWebSocket(/ws:\/\/127\.0\.0\.1:1987[012]\//, (connection) => {
    const port = new URL(connection.url()).port;
    assert.equal(authenticated.has(port), true, '认证之前建立了 WebSocket');
    socketCalls.set(port, (socketCalls.get(port) || 0) + 1);
  });
  await page.goto(origin + '/plugins#token=synthetic-startup-token', { waitUntil: 'domcontentloaded' });
  await page.getByRole('status').filter({ hasText: '正在读取服务配置' }).waitFor();
  assert.equal(authCalls.size, 0);
  runtimeRelease();
  await page.getByRole('link', { name: /available_probe/ }).waitFor();
  assert.equal(new URL(page.url()).hash, '');
  assert.equal(authCalls.get('19870'), 1);
  assert.equal(authCalls.get('19871'), 1);
  assert.equal(authCalls.get('19872'), 1);
  assert.equal(businessCalls.get('19870') || 0, 0);
  assert.equal(businessCalls.get('19872') || 0, 0);
  assert.equal(socketCalls.get('19870') || 0, 0);
  await page.clock.fastForward(8500);
  await page.getByRole('alert').filter({ hasText: '后端认证失败' }).waitFor();
  await page.getByRole('alert').filter({ hasText: 'Chat认证失败' }).waitFor();
  assert.equal(await page.getByRole('link', { name: /available_probe/ }).isVisible(), true);
  holdOthers = false; releases.forEach((release) => release());
  await page.getByRole('button', { name: '重试后端认证', exact: true }).click();
  await page.getByRole('alert').filter({ hasText: '后端认证失败' }).waitFor({ state: 'hidden' });
  assert.equal(authCalls.get('19870'), 2);
  assert.equal(authCalls.get('19871'), 1);
  assert.equal(await page.getByRole('alert').filter({ hasText: 'Chat认证失败' }).isVisible(), true);
  assert.deepEqual(errors, []);
  console.log('PASS: 运行配置发现期间先显示启动状态, 控制服务成功后页面可用且无需等待另两服务');
  console.log('PASS: 未认证无业务/WS 请求, URL 令牌及时移除, 超时分别展示且单服务重试不影响其它服务');
} finally {
  if (browser) await browser.close();
  await server.close();
}
