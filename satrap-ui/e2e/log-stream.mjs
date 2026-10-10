import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';
import { createServer, preview } from 'vite';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const production = process.env.LOG_STREAM_PRODUCTION === '1';
const server = production ? await preview({ root, preview: { host: '127.0.0.1', port: 0 } })
  : await createServer({ root, server: { host: '127.0.0.1', port: 0 } });
let browser;
try {
  if (!production) await server.listen();
  const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
  const screenshots = path.join(root, 'test-results', 'log-stream');
  await fs.mkdir(screenshots, { recursive: true });
  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 }, acceptDownloads: true });
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.addInitScript(() => {
    window.logLongTasks = [];
    new PerformanceObserver((entries) => {
      for (const entry of entries.getEntries()) window.logLongTasks.push({ start: entry.startTime, duration: entry.duration });
    }).observe({ type: 'longtask', buffered: true });
  });
  const cors = { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true',
    'Access-Control-Allow-Headers': 'Content-Type', 'Access-Control-Allow-Methods': 'GET,POST,OPTIONS' };
  await page.route(origin + '/ui-config.json', (route) => route.fulfill({ json: {
    backend_api: 'http://127.0.0.1:19870', control_api: 'http://127.0.0.1:19871', chat_api: 'http://127.0.0.1:19872',
  } }));
  await page.route(/^http:\/\/127\.0\.0\.1:1987[012]\//, (route) => route.fulfill({
    status: route.request().method() === 'OPTIONS' ? 204 : 200,
    json: { ok: true, running: true, adapters: {} }, headers: cors,
  }));
  let socket, newSocket;
  await page.routeWebSocket(/ws:\/\/127\.0\.0\.1:1987[012]\//, (ws) => {
    if (new URL(ws.url()).pathname === '/ws/logs') { socket = ws; newSocket?.(); }
  });
  await page.goto(origin + '/logs');
  await page.getByText('已连接', { exact: true }).waitFor();
  const terminal = page.getByLabel('日志内容', { exact: true });
  const send = (first, count, content) => {
    for (let index = first; index < first + count; index++) socket.send(JSON.stringify({
      type: 'log', data: { content: content?.(index) || `日志-${index}`, level: index % 2 ? 'INFO' : 'WARNING' },
    }));
  };
  const started = await page.evaluate(() => performance.now());
  for (let batch = 0; batch < 50; batch++) send(batch * 100, 100);
  await page.getByText('共 5000 条日志', { exact: true }).waitFor();
  await terminal.getByText('日志-4999', { exact: true }).waitFor();
  assert.ok(await terminal.locator('[data-log-id]').count() < 100);
  const burstMetrics = await page.evaluate((start) => ({
    duration: performance.now() - start,
    longTasks: window.logLongTasks.filter((entry) => entry.start >= start),
  }), started);
  await page.screenshot({ path: path.join(screenshots, 'wide.png') });
  const allDownload = page.waitForEvent('download');
  await page.getByRole('button', { name: '导出', exact: true }).click();
  const downloadAll = await allDownload;
  const contentAll = await fs.readFile(await downloadAll.path(), 'utf8');
  assert.deepEqual(contentAll.split('\n'), Array.from({ length: 5000 }, (_, index) => `日志-${index}`));
  const filterStart = await page.evaluate(() => performance.now());
  await page.getByPlaceholder('搜索日志...').fill('日志-1234');
  await page.getByText('共 1 条日志', { exact: true }).waitFor();
  await terminal.getByText('日志-1234', { exact: true }).waitFor();
  const filterMs = await page.evaluate((start) => performance.now() - start, filterStart);
  await page.getByPlaceholder('搜索日志...').fill('');
  await page.getByText('共 5000 条日志', { exact: true }).waitFor();
  await page.getByRole('button', { name: 'WARNING', exact: true }).click();
  await page.getByText('共 2500 条日志', { exact: true }).waitFor();
  const filteredDownload = page.waitForEvent('download');
  await page.getByRole('button', { name: '导出', exact: true }).click();
  const filteredContent = await fs.readFile(await (await filteredDownload).path(), 'utf8');
  assert.equal(filteredContent.split('\n').length, 2500);
  assert.ok(filteredContent.split('\n').every((line) => Number(line.split('-')[1]) % 2 === 1));
  await page.getByRole('button', { name: 'WARNING', exact: true }).click();
  await page.getByText('共 5000 条日志', { exact: true }).waitFor();
  await terminal.evaluate((element) => { element.scrollTop = 14000; });
  await page.getByText('已暂停滚动', { exact: true }).waitFor();
  await page.waitForFunction(() => {
    const element = document.querySelector('[aria-label="日志内容"]');
    const top = element.getBoundingClientRect().top;
    return [...element.querySelectorAll('[data-log-id]')].some((row) => row.getBoundingClientRect().bottom > top + 20 && row.getBoundingClientRect().top < top + element.clientHeight);
  });
  const reading = await terminal.evaluate((element) => {
    const top = element.getBoundingClientRect().top;
    const row = [...element.querySelectorAll('[data-log-id]')].find((item) => item.getBoundingClientRect().bottom > top + 20);
    return { id: row.dataset.logId, top: row.getBoundingClientRect().top };
  });
  send(5000, 20);
  await page.getByText('共 5000 条日志', { exact: true }).waitFor();
  await page.waitForTimeout(200);
  const readingAfter = await terminal.locator(`[data-log-id="${reading.id}"]`).evaluate((element) => element.getBoundingClientRect().top);
  assert.ok(Math.abs(readingAfter - reading.top) < 2);
  await page.getByRole('button', { name: '暂停', exact: true }).click();
  const frozenIds = await terminal.locator('[data-log-id]').evaluateAll((elements) => elements.map((element) => element.dataset.logId));
  send(5020, 20);
  await page.getByText('共 5000 条日志, 暂停期间收到 20 条', { exact: true }).waitFor();
  assert.deepEqual(await terminal.locator('[data-log-id]').evaluateAll((elements) => elements.map((element) => element.dataset.logId)), frozenIds);
  const pausedDownload = page.waitForEvent('download');
  await page.getByRole('button', { name: '导出', exact: true }).click();
  const pausedContent = await fs.readFile(await (await pausedDownload).path(), 'utf8');
  assert.equal(pausedContent.split('\n').at(-1), '日志-5019');
  await page.getByRole('button', { name: '继续', exact: true }).click();
  const resumedDownload = page.waitForEvent('download');
  await page.getByRole('button', { name: '导出', exact: true }).click();
  const resumedContent = await fs.readFile(await (await resumedDownload).path(), 'utf8');
  assert.equal(resumedContent.split('\n')[0], '日志-40');
  assert.equal(resumedContent.split('\n').at(-1), '日志-5039');
  await page.getByRole('button', { name: '清空', exact: true }).click();
  await page.getByText('共 0 条日志', { exact: true }).waitFor();
  send(5040, 30, (index) => `多行-${index}\n${'中文换行测试 '.repeat(70)}`);
  await page.getByText('共 30 条日志', { exact: true }).waitFor();
  await terminal.evaluate((element) => { element.scrollTop = element.scrollHeight; });
  await terminal.getByText(/多行-5069/).waitFor();
  await page.setViewportSize({ width: 390, height: 844 });
  await terminal.getByText(/多行-5069/).waitFor();
  assert.ok(await terminal.locator('[data-log-id]').count() < 30);
  await page.screenshot({ path: path.join(screenshots, 'narrow.png') });
  const changedHistory = new Promise((resolve) => { newSocket = resolve; });
  await page.getByRole('button', { name: '100 行', exact: true }).click();
  await page.getByRole('button', { name: '200 行', exact: true }).click();
  await changedHistory;
  await page.getByText('共 0 条日志', { exact: true }).waitFor();
  send(6000, 1);
  await terminal.getByText('日志-6000', { exact: true }).waitFor();
  assert.deepEqual(errors, []);
  console.log('PASS: 5000 条突发保留顺序/完整导出, 可见 DOM <100, 搜索/级别筛选/清空/切换历史均有效');
  console.log('PASS: 自动滚动/阅读锚点, 暂停快照与计数/继续, 多行和窄屏动态行高');
  console.log(JSON.stringify({ production, burstMs: burstMetrics.duration, filterMs, longTaskCount: burstMetrics.longTasks.length, maxLongTaskMs: Math.max(0, ...burstMetrics.longTasks.map((entry) => entry.duration)) }));
} finally {
  if (browser) await browser.close();
  if (production) await new Promise((resolve, reject) => server.httpServer.close((error) => error ? reject(error) : resolve()));
  else await server.close();
}
