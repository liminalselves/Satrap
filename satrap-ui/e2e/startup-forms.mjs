import assert from "node:assert/strict";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { runSuite, mockConfigVersions } from './runtime.mjs';
import { pathToFileURL } from 'node:url';

async function auth_startup(runtime) {






  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  const server = await runtime.vite({ root, server: { host: '127.0.0.1', port: 0 } });
  let browser;
  try {
    await runtime.listen(server);
    const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
    browser = await runtime.browser;
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
    if (browser) await runtime.closeContexts();
    await runtime.releaseServer(server);
  }

}

async function form_required(runtime) {
  /** 共用表单必填校验的页面回归: 空值不发写请求且弹窗保留, 填好后才提交 */






  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  const server = await runtime.vite({ root, server: { host: '127.0.0.1', port: 0 } });
  let browser;
  try {
    await runtime.listen(server);
    const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
    const writes = [];
    let user = { platform_id: 'test-platform', platform_label: '测试平台', platform_type: 'test', user_id: 'user-1', user_nickname: '甲', user_platform: 'test', user_session: [], has_profile: true, revision: 'v1', conversations: [], conversation_count: 0 };
    const cors = {
      'Access-Control-Allow-Origin': origin,
      'Access-Control-Allow-Credentials': 'true',
      'Access-Control-Allow-Headers': 'Content-Type',
    };
    browser = await runtime.browser;
    const page = await browser.newPage();
    await page.route(origin + '/ui-config.json', (route) => route.fulfill({ json: {
      backend_api: 'http://127.0.0.1:19870', control_api: 'http://127.0.0.1:19871', chat_api: 'http://127.0.0.1:19872',
    } }));
    await page.route(/^http:\/\/127\.0\.0\.1:1987[012]\//, (route) => {
      const request = route.request();
      const url = new URL(request.url());
      if (request.method() === 'POST') writes.push({ path: url.pathname, body: request.postDataJSON() });
      if (url.pathname === '/config/conversations/users') {
        if (request.method() === 'POST') {
          const data = request.postDataJSON();
          if (data.action === 'create') user = { ...user, user_id: data.user_id, revision: 'v2' };
          if (data.action === 'associate') user = { ...user, user_session: [data.session_id], revision: 'v3' };
          return route.fulfill({ json: { ok: true, user }, headers: cors });
        }
        return route.fulfill({ json: url.searchParams.has('user_id') ? { user } : { items: [user], total: 1, new_revision: 'missing' }, headers: cors });
      }
      const responses = {
        '/api/health': { running: true, adapters: {} },
        '/config/models': {},
        '/config/conversations/platforms': { platforms: ['test-platform'], items: [{ id: 'test-platform', type: 'test', type_label: '测试', label: '测试平台' }] },
        '/api/checkpoints': { conversation_id: 'conv-test', checkpoints: [{ checkpoint_id: 'ckpt-1', created_at: 1 }], branches: [] },
        '/api/checkpoint/audit': { mutations: [] },
        '/api/checkpoint/fork': { ok: true, conversation_id: 'conv-test:fork:retry_v2' },
      };
      return route.fulfill({ json: responses[url.pathname] ?? { ok: true }, headers: cors });
    });
    // 页面还会发一次会话引导请求与状态 WS, 都不属于被验证的写入, 写请求断言按路径前缀过滤
    await page.routeWebSocket(/ws:\/\/127\.0\.0\.1:1987[012]\//, () => {});
    const writesTo = (prefix) => writes.filter((item) => item.path.startsWith(prefix));
    const dialog = page.getByRole('dialog');

    // 1. 模型配置: 配置名称必填, 空名称不发创建请求
    await mockConfigVersions(page, origin, () => JSON.stringify({}));
  await page.goto(origin + '/models');
    await page.getByRole('button', { name: '新增配置', exact: true }).click();
    const modelName = dialog.getByLabel(/配置名称/);
    assert.equal(await modelName.inputValue(), 'default');
    await modelName.fill('');
    assert.equal(await modelName.evaluate((element) => element.validity.valueMissing), true);
    await dialog.getByRole('button', { name: '创建', exact: true }).click();
    await page.waitForTimeout(200);
    assert.deepEqual(writesTo('/config/models'), []);
    assert.equal(await dialog.isVisible(), true);
    await modelName.fill('probe-model');
    await dialog.getByRole('button', { name: '创建', exact: true }).click();
    await dialog.waitFor({ state: 'hidden' });
    assert.deepEqual(writesTo('/config/models').map((item) => item.path), ['/config/models/llm/probe-model']);

    // 2. 用户资料: 空 ID 不发创建请求
    await page.goto(origin + '/users');
    await page.getByRole('button', { name: '添加用户资料', exact: true }).click();
    const userId = dialog.getByLabel('资料用户 ID');
    assert.equal(await userId.inputValue(), '');
    const saveUser = dialog.getByRole('button', { name: '保存资料', exact: true });
    assert.equal(await saveUser.isDisabled(), true);
    await saveUser.evaluate((element) => element.click());
    await page.waitForTimeout(200);
    assert.deepEqual(writesTo('/config/conversations/users'), []);
    assert.equal(await dialog.isVisible(), true);
    await userId.fill('probe');
    await saveUser.click();
    await dialog.waitFor({ state: 'hidden' });
    assert.deepEqual(writesTo('/config/conversations/users').map((item) => item.body.user_id), ['probe']);

    // 3. 列表关联: 空对话 ID 不发请求
    await page.getByRole('button', { name: '编辑用户资料', exact: true }).waitFor();
    await page.getByText('高级资料操作', { exact: true }).click();
    const sessionId = page.getByLabel('关联对话 ID');
    assert.equal(await sessionId.inputValue(), '');
    const associate = page.getByRole('button', { name: '添加关联', exact: true });
    assert.equal(await associate.isDisabled(), true);
    await associate.evaluate((element) => element.click());
    await page.waitForTimeout(200);
    assert.deepEqual(writesTo('/config/conversations/users').filter((item) => item.body.action === 'associate'), []);
    await sessionId.fill('sr7dws');
    await associate.click();
    await page.waitForFunction(() => document.body.textContent.includes('此用户暂无关联对话') && document.body.textContent.includes('编辑用户资料'));
    assert.deepEqual(writesTo('/config/conversations/users').filter((item) => item.body.action === 'associate').map((item) => item.body.session_id), ['sr7dws']);

    // 4. 检查点分支: 分支名必填, 空分支名不发 Fork 请求
    await page.goto(origin + '/checkpoints');
    await page.getByPlaceholder('输入对话 ID (如 conv-xxx)').fill('conv-test');
    await page.getByRole('button', { name: '查询', exact: true }).click();
    await page.getByRole('button', { name: 'Fork 分支', exact: true }).click();
    const branchName = dialog.getByLabel(/分支名/);
    assert.equal(await branchName.inputValue(), '');
    await dialog.getByRole('button', { name: 'Fork', exact: true }).click();
    await page.waitForTimeout(200);
    assert.deepEqual(writesTo('/api/checkpoint/fork'), []);
    assert.equal(await dialog.isVisible(), true);
    await branchName.fill('retry_v2');
    await dialog.getByRole('button', { name: 'Fork', exact: true }).click();
    await dialog.waitFor({ state: 'hidden' });
    assert.deepEqual(writesTo('/api/checkpoint/fork').map((item) => item.body.branch_name), ['retry_v2']);

    console.log('PASS: 模型配置名称/用户 ID/关联对话 ID/分支名为空时不发写请求, 填写后正常提交');
  } finally {
    if (browser) await runtime.closeContexts();
    await runtime.releaseServer(server);
  }

}

export const scenarios = [
  ["auth-startup", auth_startup],
  ["form-required", form_required]
];
if (process.argv[1] && pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url) await runSuite(scenarios);
