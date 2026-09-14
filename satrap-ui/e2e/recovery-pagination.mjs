/** 恢复面板分页与会话切换回归, 仅使用隔离页面和受控接口 */
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
  if (browser) await browser.close();
  await server.close();
}
