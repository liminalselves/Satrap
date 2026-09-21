/** 手动唤醒弹窗的真实页面回归, API 使用受控响应 */
import assert from 'node:assert/strict';
import path from 'node:path';
import fs from 'node:fs/promises';
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
  const page = await browser.newPage({ viewport: { width: 1100, height: 900 } });
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  const requests = [];
  let responseMode = 'error';
  await page.route(`${origin}/ui-config.json`, (route) => route.fulfill({ json: {
    backend_api: 'http://127.0.0.1:19870', control_api: 'http://127.0.0.1:19871', chat_api: 'http://127.0.0.1:19872',
  } }));
  await page.route(/^http:\/\/127\.0\.0\.1:1987[012]\//, async (route) => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname;
    const headers = { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true', 'Access-Control-Allow-Headers': 'Content-Type', 'Access-Control-Allow-Methods': 'GET, POST, OPTIONS' };
    if (request.method() === 'OPTIONS') return route.fulfill({ status: 204, headers });
    if (pathname === '/api/platforms/wake') {
      requests.push(request.postDataJSON());
      return route.fulfill({ headers, status: responseMode === 'error' ? 409 : 200,
        json: responseMode === 'error' ? { error: '目标群暂不可用' } : { status: responseMode } });
    }
    const responses = {
      '/api/health': { running: true, adapters: {} }, '/status': { running: true },
      '/api/sessions': { sessions: [] }, '/config/session-instances': { sessions: [] },
      '/config/session-classes': {}, '/config/edictum/sessions': {}, '/config/edictum/types': [],
      '/config/platforms': { ok: true, platforms: [{ id: 'onebot-test', type: 'onebot', settings: {} }] },
    };
    return route.fulfill({ headers, json: responses[pathname] ?? {} });
  });
  await page.routeWebSocket('ws://127.0.0.1:19870/ws/status**', (socket) => {
    socket.send(JSON.stringify({ type: 'status', data: { running: true, adapters: {} } }));
  });
  await page.goto(`${origin}/sessions`);
  await page.getByRole('button', { name: '手动唤醒群聊', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByLabel('平台实例', { exact: true }).selectOption('onebot-test');
  await dialog.getByLabel('目标群号').fill('20000');
  await dialog.getByLabel('会话成员 ID', { exact: false }).fill('30000');
  await dialog.getByLabel('唤醒正文', { exact: false }).fill('请处理这条消息');
  await dialog.getByRole('button', { name: '提交唤醒' }).click();
  await page.getByText('唤醒失败, 输入已保留: 目标群暂不可用', { exact: true }).waitFor();
  assert.equal(await dialog.getByLabel('唤醒正文', { exact: false }).inputValue(), '请处理这条消息');
  responseMode = 'accepted';
  await dialog.getByRole('button', { name: '提交唤醒' }).click();
  await dialog.waitFor({ state: 'hidden' });
  assert.equal(requests[0].request_id, requests[1].request_id);
  assert.equal(requests[1].group_id, '20000');
  assert.equal('operator' in requests[1], false);
  await page.getByRole('button', { name: '手动唤醒群聊', exact: true }).click();
  await dialog.getByLabel('平台实例', { exact: true }).selectOption('onebot-test');
  await dialog.getByLabel('目标群号').fill('20000');
  await dialog.getByLabel('会话成员 ID', { exact: false }).fill('30000');
  responseMode = 'no_pending';
  await dialog.getByRole('button', { name: '提交唤醒' }).click();
  await page.getByText('此群与成员范围内没有待处理正文', { exact: true }).waitFor();
  assert.equal(await dialog.isVisible(), true);
  await page.setViewportSize({ width: 390, height: 844 });
  const artifacts = path.join(root, 'test-results', 'manual-wake');
  await fs.mkdir(artifacts, { recursive: true });
  await page.screenshot({ path: path.join(artifacts, 'mobile.png'), animations: 'disabled' });
  assert.deepEqual(errors, []);
  console.log('PASS: 手动唤醒页面, 错误保留输入, 幂等重试, 空窗口提示');
} finally {
  await browser?.close();
  await server.close();
}
