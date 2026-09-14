/** 验证检查点查询直接使用列表响应中的分支, 不发起额外分支请求 */
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
  await browser?.close();
  await server.close();
}
