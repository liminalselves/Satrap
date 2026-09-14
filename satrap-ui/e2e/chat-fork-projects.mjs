/** 验证真实聊天页面 fork 刷新后的项目分组, 所有接口均使用隔离数据 */
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
  const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
  browser = await chromium.launch({ headless: true });
  const context = await browser.newContext();
  const fixture = createBenchmarkFixture(2);
  const conversations = [
    { ...fixture.conversations[0], title: '项目甲原会话', project_id: 'a' },
    { ...fixture.conversations[1], title: '项目乙原会话', project_id: 'b' },
  ];
  let forked = false;
  await context.route(`${origin}/ui-config.json`, route => route.fulfill({ json: {
    backend_api: 'http://127.0.0.1:19870', control_api: 'http://127.0.0.1:19871', chat_api: 'http://127.0.0.1:19872',
  } }));
  await context.route(/^http:\/\/127\.0\.0\.1:1987[012]\//, route => {
    const pathname = new URL(route.request().url()).pathname;
    if (pathname === '/api/chat/fork' && route.request().method() === 'POST') {
      assert.deepEqual(route.request().postDataJSON(), { conversation: 'benchmark-active', turn_index: 0 });
      forked = true;
    }
    const responses = {
      '/api/chat/health': { ok: true, conversations: 2, preloaded: 0 },
      '/api/chat/conversations': { conversations: forked
        ? [...conversations, { ...conversations[0], conversation_id: 'fork', title: '项目甲分支' }]
        : conversations },
      '/api/chat/turns': { turns: fixture.turns },
      '/api/chat/models': { models: ['benchmark-model'] },
      '/api/chat/models/detail': { ok: true, models: {} },
      '/api/chat/plugins': { plugins: [] },
      '/api/chat/runs': { ok: true, runs: [], next_cursor: null },
      '/api/projects': { projects: [
        { project_id: 'a', name: '项目甲', root_path: 'a', created_at: 1 },
        { project_id: 'b', name: '项目乙', root_path: 'b', created_at: 1 },
      ] },
      '/api/chat/fork': { ok: true, conversation_id: 'fork', copied_turns: 1 },
    };
    return route.fulfill({ json: responses[pathname] ?? { ok: true }, headers: {
      'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true',
      'Access-Control-Allow-Headers': 'Content-Type',
    } });
  });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => { errors.push(error.message); console.error(error.stack); });
  await page.routeWebSocket('ws://127.0.0.1:19872/ws/chat**', socket => {
    socket.send(JSON.stringify({
      type: 'snapshot', conversation_id: forked ? 'fork' : 'benchmark-active',
      stream_id: 'fork-test', seq: 1, state: 'idle', active_turn_id: null,
      turns: fixture.turns, pending_user_inputs: [],
    }));
  });
  await page.routeWebSocket('ws://127.0.0.1:19870/ws/status**', () => {});
  await page.goto(`${origin}/chat`);
  await page.getByText('继续最近对话', { exact: true }).click();
  const group = name => page.getByText(name, { exact: true }).locator('..').locator('..');
  await group('项目甲').getByText('项目甲原会话', { exact: true }).waitFor();
  await group('项目乙').getByText('项目乙原会话', { exact: true }).waitFor();
  try {
    await page.getByTitle('从这里分支', { exact: true }).first().click({ timeout: 10000 });
  } catch (error) {
    console.error(await page.locator('body').innerText(), errors);
    throw error;
  }
  await group('项目甲').getByText('项目甲分支', { exact: true }).waitFor();
  assert.ok(forked);
  assert.equal(await group('项目甲').getByText('项目甲原会话', { exact: true }).count(), 1);
  assert.equal(await group('项目乙').getByText('项目乙原会话', { exact: true }).count(), 1);
  assert.deepEqual(errors, []);
  console.log('PASS: fork 后原有会话与新分支均保留项目分组');
} finally {
  await browser?.close();
  await server.close();
}
