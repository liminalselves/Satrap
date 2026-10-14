import assert from "node:assert/strict";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createBenchmarkFixture } from "../benchmarks/fixtures.mjs";
import { runSuite, mockConfigVersions } from './runtime.mjs';
import { pathToFileURL } from 'node:url';

async function chat_reconnect(runtime) {
  /** 聊天重连浏览器回归; 使用真实页面和受控 HTTP / WebSocket, 不访问用户服务 */







  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  const server = await runtime.vite({ root, server: { host: '127.0.0.1', port: 0 } });
  let browser;
  try {
    await runtime.listen(server);
    const address = server.httpServer.address();
    const origin = `http://127.0.0.1:${address.port}`;
    browser = await runtime.browser;
    const context = await browser.newContext();
    const fixture = createBenchmarkFixture(2);
    await context.route(`${origin}/ui-config.json`, (route) => route.fulfill({ json: {
      backend_api: 'http://127.0.0.1:19870', control_api: 'http://127.0.0.1:19871', chat_api: 'http://127.0.0.1:19872',
    } }));
    await context.route(/^http:\/\/127\.0\.0\.1:1987[012]\//, (route) => {
      const pathname = new URL(route.request().url()).pathname;
      const responses = {
        '/api/chat/health': { ok: true, conversations: 1, preloaded: 0 },
        '/api/chat/conversations': { conversations: fixture.conversations },
        '/api/chat/turns': { turns: fixture.turns },
        '/api/chat/models': { models: ['benchmark-model'] },
        '/api/chat/models/detail': { ok: true, models: {} },
        '/api/chat/plugins': { plugins: [] },
        // 执行记录与恢复面板在聊天页常驻, 缺该接口会让整个页面渲染失败
        '/api/chat/runs': { ok: true, runs: [], next_cursor: null },
        '/api/projects': { projects: [] },
      };
      return route.fulfill({
        json: responses[pathname] ?? { ok: true },
        headers: { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true', 'Access-Control-Allow-Headers': 'Content-Type' },
      });
    });
    await context.addInitScript(() => localStorage.setItem('satrap.chat.think', 'off'));
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', (error) => errors.push(error.message));
    const sockets = [];
    await page.routeWebSocket('ws://127.0.0.1:19872/ws/chat**', (socket) => sockets.push(socket));
    await page.routeWebSocket('ws://127.0.0.1:19870/ws/status**', () => {});
    const waitFor = async (predicate) => {
      const deadline = Date.now() + 10_000;
      while (!(await predicate())) {
        assert.ok(Date.now() < deadline, '等待浏览器状态超时');
        await new Promise((resolve) => setTimeout(resolve, 25));
      }
    };
    const visible = (text) => page.getByText(text, { exact: true }).isVisible();
    const turn = (answer, generating = true) => ({
      ...fixture.turns[0], answer, generating,
      segments: [{ type: 'content', content: answer }],
    });
    const snapshot = (seq, answer, pending = [], state = 'running') => ({
      type: 'snapshot', conversation_id: 'benchmark-active', stream_id: 'test-epoch', seq,
      state, active_turn_id: state === 'idle' ? null : 1,
      turns: [turn(answer, state !== 'idle')], pending_user_inputs: pending,
    });
    const send = (socket, event) => socket.send(JSON.stringify(event));
    await mockConfigVersions(page, origin, () => JSON.stringify({}));
  await page.goto(`${origin}/chat`);
    try {
      await page.getByText('继续最近对话', { exact: true }).click({ timeout: 10_000 });
    } catch (error) {
      console.error('页面诊断', await page.locator('body').innerText(), errors);
      throw error;
    }
    await waitFor(() => sockets.length === 1);
    send(sockets[0], snapshot(10, '恢复中的回答'));
    await waitFor(() => visible('恢复中的回答'));
    send(sockets[0], { type: 'content_delta', delta: '片段', seq: 11, stream_id: 'test-epoch' });
    await waitFor(() => visible('恢复中的回答片段'));
    send(sockets[0], { type: 'content_delta', delta: '片段', seq: 11, stream_id: 'test-epoch' });
    send(sockets[0], { type: 'resync_required' });
    await waitFor(() => sockets.length === 2);
    send(sockets[1], snapshot(30, '包含掉线期间内容的完整回答', [
      { request_id: 'ask-1', question: '重连后继续确认', options: ['继续', '停止'] },
    ]));
    await waitFor(() => visible('包含掉线期间内容的完整回答'));
    await waitFor(() => visible('重连后继续确认'));
    assert.equal(await page.locator('.chat-message-item').count(), 2);
    assert.equal(await visible('恢复中的回答片段片段'), false);
    send(sockets[1], { type: 'ask_user_end', conversation_id: 'benchmark-active', request_id: 'ask-1', status: 'answered', seq: 31, stream_id: 'test-epoch' });
    await waitFor(async () => !(await visible('重连后继续确认')));
    send(sockets[1], { type: 'resync_required' });
    await waitFor(() => sockets.length === 3);
    send(sockets[2], snapshot(40, '任务已完成', [], 'idle'));
    await waitFor(() => visible('任务已完成'));
    assert.equal(await page.locator('.chat-message-item').count(), 2);
    assert.equal(await page.getByTitle('发送', { exact: true }).count(), 1);
    assert.deepEqual(errors, []);
    console.log('PASS: 快照恢复, 增量去重, 溢出重连, 询问恢复/结束, 完成状态恢复');
  } finally {
    await runtime.closeContexts();
    await runtime.releaseServer(server);
  }

}

async function checkpoint_branches(runtime) {
  /** 验证检查点查询直接使用列表响应中的分支, 不发起额外分支请求 */






  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  const server = await runtime.vite({ root, server: { host: '127.0.0.1', port: 0 } });
  let browser;
  try {
    await runtime.listen(server);
    const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
    browser = await runtime.browser;
    const page = await browser.newPage();
    const requests = [];
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route(`${origin}/ui-config.json`, route => route.fulfill({ json: {
      backend_api: 'http://127.0.0.1:19870', control_api: 'http://127.0.0.1:19871', chat_api: 'http://127.0.0.1:19872',
    } }));
    await page.route(/^http:\/\/127\.0\.0\.1:1987[012]\//, route => {
      const url = new URL(route.request().url());
      requests.push(url.pathname);
      const responses = {
        '/api/health': { running: true, adapters: {} },
        '/api/checkpoints': { conversation_id: 'conv-test', checkpoints: [], branches: [
          { scope_id: 'conv-test:fork:branch-a', name: '分支甲', created_at: 1 },
        ] },
        '/api/checkpoint/audit': { mutations: [] },
      };
      if (url.pathname === '/api/checkpoints') {
        assert.equal(url.searchParams.get('platform_id'), 'local');
        assert.equal(url.searchParams.get('conversation'), 'conv-test');
      }
      return route.fulfill({ json: responses[url.pathname] ?? { ok: true }, headers: {
        'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true',
        'Access-Control-Allow-Headers': 'Content-Type',
      } });
    });
    await page.routeWebSocket('ws://127.0.0.1:19870/ws/status**', () => {});
    await mockConfigVersions(page, origin, () => JSON.stringify({}));
  await page.goto(`${origin}/checkpoints`);
    await page.getByPlaceholder('输入对话 ID (如 conv-xxx)').fill('conv-test');
    await page.getByRole('button', { name: '查询', exact: true }).click();
    await page.getByText('分支甲', { exact: true }).waitFor();
    assert.equal(await page.getByText('conv-test:fork:branch-a', { exact: true }).count(), 1);
    assert.equal(requests.filter(url => url === '/api/checkpoints').length, 1);
    assert.equal(requests.filter(url => url === '/api/checkpoint/branches').length, 0);
    assert.deepEqual(errors, []);
    console.log('PASS: 检查点分支显示正常且无额外分支请求');
  } finally {
    await runtime.closeContexts();
    await runtime.releaseServer(server);
  }

}

async function recovery_pagination(runtime) {
  /** 恢复面板分页与会话切换回归, 仅使用隔离页面和受控接口 */






  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  const server = await runtime.vite({ root, server: { host: '127.0.0.1', port: 0 } });
  let browser;
  try {
    await runtime.listen(server);
    const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
    browser = await runtime.browser;
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', error => { errors.push(error.message); console.error(error.message); });
    let releaseLate;
    const row = id => ({ id, status: 'failed', error: id, created_at: 1, steps: [] });
    await page.route('**/api/chat/runs?**', async route => {
      const query = new URL(route.request().url()).searchParams;
      const conversation = query.get('conversation');
      let result;
      if (conversation === 'b') result = { runs: [row('新会话任务')], next_cursor: null };
      else if (query.get('unfinished') === 'true') result = { runs: [row('待确认任务')], next_cursor: null };
      else if (query.get('cursor') === 'late') {
        await new Promise(resolve => { releaseLate = resolve; });
        result = { runs: [row('旧会话迟到数据')], next_cursor: null };
      } else if (query.get('cursor')) result = { runs: [row('第二页任务')], next_cursor: 'late' };
      else result = { runs: [row('第一页任务')], next_cursor: 'page2' };
      await route.fulfill({ json: { ok: true, ...result }, headers: { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true' } });
    });
    await mockConfigVersions(page, origin, () => JSON.stringify({}));
  await page.goto(`${origin}/e2e/recovery-fixture.html`);
    await page.getByText('执行记录与恢复', { exact: true }).click();
    await page.getByText('第一页任务', { exact: true }).waitFor();
    await page.getByRole('button', { name: '加载更多' }).click();
    await page.getByText('第二页任务', { exact: true }).waitFor();
    assert.equal(await page.getByText('第一页任务', { exact: true }).count(), 1);
    await page.getByLabel('仅显示未完成任务').check();
    await page.getByText('待确认任务', { exact: true }).waitFor();
    assert.equal(await page.getByText('第一页任务', { exact: true }).count(), 0);
    await page.getByLabel('仅显示未完成任务').uncheck();
    await page.getByText('第一页任务', { exact: true }).waitFor();
    await page.getByRole('button', { name: '加载更多' }).click();
    await page.getByText('第二页任务', { exact: true }).waitFor();
    await page.getByRole('button', { name: '加载更多' }).click();
    const deadline = Date.now() + 10000;
    while (!releaseLate) {
      assert.ok(Date.now() < deadline);
      await new Promise(resolve => setTimeout(resolve, 10));
    }
    await page.evaluate(() => window.renderRecovery('b'));
    await page.getByText('新会话任务', { exact: true }).waitFor();
    const response = page.waitForResponse(r => r.url().includes('cursor=late'));
    releaseLate();
    await response;
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    assert.equal(await page.getByText('旧会话迟到数据', { exact: true }).count(), 0);
    assert.deepEqual(errors, []);
    console.log('恢复分页、筛选及会话切换回归通过');
  } finally {
    if (browser) await runtime.closeContexts();
    await runtime.releaseServer(server);
  }

}

export const scenarios = [
  ["chat-reconnect", chat_reconnect],
  ["checkpoint-branches", checkpoint_branches],
  ["recovery-pagination", recovery_pagination]
];
if (process.argv[1] && pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url) await runSuite(scenarios);
