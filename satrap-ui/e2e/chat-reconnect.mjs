/** 聊天重连浏览器回归; 使用真实页面和受控 HTTP / WebSocket, 不访问用户服务 */
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';
import { createServer } from 'vite';
import { createBenchmarkFixture } from '../benchmarks/fixtures.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const server = await createServer({ root, server: { host: '127.0.0.1', port: 0 } });
let browser;
try {
  await server.listen();
  const address = server.httpServer.address();
  const origin = `http://127.0.0.1:${address.port}`;
  browser = await chromium.launch({ headless: true });
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
  await browser?.close();
  await server.close();
}
