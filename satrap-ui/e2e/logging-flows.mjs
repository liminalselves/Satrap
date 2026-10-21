import assert from "node:assert/strict";
import path from "node:path";
import { fileURLToPath } from "node:url";
import fs, { mkdir } from "node:fs/promises";
import { runSuite, mockConfigVersions } from './runtime.mjs';
import { pathToFileURL } from 'node:url';

async function log_retention(runtime) {







  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  const server = await runtime.vite({ root, server: { host: '127.0.0.1', port: 0 } });
  let browser;
  try {
    await runtime.listen(server);
    const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
    browser = await runtime.browser;
    const page = await browser.newPage({ viewport: { width: 1280, height: 1100 } });
    await page.clock.install();
    page.setDefaultTimeout(10000);
    const errors = [];
    const writes = [];
    page.on('pageerror', (error) => errors.push(error.message));
    await page.routeWebSocket('ws://127.0.0.1:19870/ws/status**', () => {});
    let failSave = false;
    let failCleanup = false;
    let running = true;
    let revision = 1;
    let loggingReads = 0;
    let holdRead = false;
    let readRelease;
    const snapshot = { ok: true, policy: { enabled: true, retention_days: 30 }, revision: 'v1', directory: 'F:/隔离测试/logs', config_path: 'F:/隔离测试/logging.json', last_cleanup: null, maintenance_error: null, status_error: null };
    const cors = { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true', 'Access-Control-Allow-Headers': 'Content-Type', 'Access-Control-Allow-Methods': 'GET,POST,PUT,OPTIONS' };
    await page.route('**/*', async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.origin === origin && url.pathname !== '/ui-config.json') return route.continue();
      if (url.pathname === '/ui-config.json') return route.fulfill({ json: { backend_api: 'http://127.0.0.1:19870', control_api: 'http://127.0.0.1:19871', chat_api: 'http://127.0.0.1:19872' } });
      if (request.method() === 'OPTIONS') return route.fulfill({ status: 204, headers: cors });
      if (request.method() !== 'GET' && url.pathname !== '/auth/session') writes.push({ path: url.pathname, payload: request.postDataJSON() });
      if (url.pathname === '/config/logging') {
        if (request.method() === 'GET') {
          loggingReads++;
          const captured = structuredClone(snapshot);
          if (holdRead) await new Promise((done) => { readRelease = done; });
          if (request.failure()) return;
          return route.fulfill({ json: captured, headers: cors });
        }
        const payload = request.postDataJSON();
        if (failSave || payload.expected_revision !== snapshot.revision) return route.fulfill({ status: 409, json: { error: '日志策略版本冲突, 保存失败' }, headers: cors });
        snapshot.policy = payload.policy;
        snapshot.revision = `v${++revision}`;
        return route.fulfill({ json: snapshot, headers: cors });
      }
      if (url.pathname === '/config/logging/cleanup') {
        if (failCleanup) return route.fulfill({ status: 500, json: { error: '模拟清理故障' }, headers: cors });
        assert.equal(request.postDataJSON().expected_revision, snapshot.revision);
        snapshot.last_cleanup = { created_at: 1790958000, retention_days: snapshot.policy.retention_days, cutoff: '2026-09-04', policy_revision: snapshot.revision, deleted: ['已过期.log'], skipped: [{ file: '活动文件.log', reason: '进程仍在使用' }, { file: '旧版日志.log', reason: '无法确认旧版进程占用, 保留文件' }], errors: [{ file: '拒绝删除.log', reason: '权限不足' }] };
        return route.fulfill({ json: { ok: false, result: snapshot.last_cleanup }, headers: cors });
      }
      if (url.pathname === '/config') return route.fulfill({ json: { ok: true, exists: true, config: { api: { host: '127.0.0.1', port: 19870 } } }, headers: cors });
      return route.fulfill({ json: url.pathname === '/status' || url.pathname === '/api/health' ? { running, runtime_id: 'test', adapters: {} } : {}, headers: cors });
    });
    await mockConfigVersions(page, origin, () => JSON.stringify({revision}));
  await page.goto(`${origin}/settings`);
    await page.getByRole('button', { name: '日志保留', exact: true }).click();
    const panel = page.getByRole('region', { name: '日志保留设置' });
    const days = panel.getByLabel('保留天数', { exact: true });
    const checkbox = panel.getByLabel('自动清理过期日志');
    const save = panel.getByRole('button', { name: '保存日志策略', exact: true });
    const cleanup = panel.getByRole('button', { name: '立即清理过期日志', exact: true });
    await cleanup.waitFor();
    await panel.getByText('F:/隔离测试/logs', { exact: false }).waitFor();
    assert.equal(await days.inputValue(), '30');
    assert.equal(await checkbox.isChecked(), true);
    await days.fill('60');
    holdRead = true;
    const beforeSlowRead = loggingReads;
    await page.clock.fastForward(11000);
    await page.waitForFunction(() => document.querySelector('section[aria-label="日志保留设置"] button')?.disabled);
    assert.equal(loggingReads, beforeSlowRead + 1);
    await page.clock.fastForward(15000);
    assert.equal(loggingReads, beforeSlowRead + 1);
    await page.evaluate(() => {
      Object.defineProperty(document, 'hidden', { configurable: true, value: true });
      document.dispatchEvent(new Event('visibilitychange'));
    });
    const slowCompleted = page.waitForResponse((response) => new URL(response.url()).pathname === '/config/logging');
    holdRead = false; readRelease(); await slowCompleted;
    await page.clock.fastForward(60000);
    assert.equal(loggingReads, beforeSlowRead + 1);
    const resumed = page.waitForResponse((response) => new URL(response.url()).pathname === '/config/logging');
    await page.evaluate(() => {
      Object.defineProperty(document, 'hidden', { configurable: true, value: false });
      document.dispatchEvent(new Event('visibilitychange'));
    });
    await resumed;
    assert.equal(loggingReads, beforeSlowRead + 2);
    assert.equal(await days.inputValue(), '60');
    holdRead = true;
    await page.clock.fastForward(11000);
    const cancelled = page.waitForEvent('requestfailed', { predicate: (request) => new URL(request.url()).pathname === '/config/logging' });
    await page.getByRole('button', { name: '关于', exact: true }).click();
    await cancelled;
    holdRead = false; readRelease();
    const whileInactive = loggingReads;
    await page.clock.fastForward(40000);
    assert.equal(loggingReads, whileInactive);
    await page.getByRole('button', { name: '日志保留', exact: true }).click();
    await panel.getByRole('button', { name: '刷新日志状态', exact: true }).waitFor();
    assert.equal(await days.inputValue(), '60');
    await days.fill('0');
    assert.equal(await save.isDisabled(), true);
    await panel.getByText('请输入 1 至 3650 的整数', { exact: true }).waitFor();
    await days.fill('1.5');
    assert.equal(await save.isDisabled(), true);
    assert.equal(writes.length, 0);
    await days.fill('60');
    await checkbox.uncheck();
    failSave = true;
    await save.click();
    await panel.getByText('日志策略版本冲突, 保存失败', { exact: true }).waitFor();
    assert.equal(await days.inputValue(), '60');
    assert.equal(await checkbox.isChecked(), false);
    assert.equal(await cleanup.isDisabled(), true);
    await page.getByRole('button', { name: '关于', exact: true }).click();
    await page.getByRole('button', { name: '日志保留', exact: true }).click();
    assert.equal(await days.inputValue(), '60');
    snapshot.policy = { enabled: true, retention_days: 45 };
    snapshot.revision = 'external';
    await panel.getByRole('button', { name: '刷新日志状态', exact: true }).click();
    await panel.getByText('已保存策略在其它位置发生变化, 请重新加载并核对草稿。', { exact: true }).waitFor();
    assert.equal(await days.inputValue(), '60');
    await panel.getByRole('button', { name: '放弃草稿并加载已保存策略', exact: true }).click();
    assert.equal(await days.inputValue(), '45');
    assert.equal(await checkbox.isChecked(), true);
    failSave = false;
    await days.fill('50');
    await save.click();
    await page.getByText('日志策略已保存, 无需重启后端', { exact: true }).waitFor();
    assert.equal(snapshot.policy.retention_days, 50);
    assert.equal(writes.at(-1).payload.expected_revision, 'external');
    await page.getByRole('button', { name: '停止后端', exact: true }).waitFor();
    assert.equal(writes.some((item) => ['/restart', '/stop', '/config'].includes(item.path)), false);
    await cleanup.click();
    await panel.getByText('拒绝删除.log: 权限不足', { exact: true }).waitFor();
    await panel.getByText('跳过的文件及原因', { exact: true }).click();
    await panel.getByText('活动文件.log: 进程仍在使用', { exact: true }).waitFor();
    await panel.getByText('旧版日志.log: 无法确认旧版进程占用, 保留文件', { exact: true }).waitFor();
    failCleanup = true;
    await cleanup.click();
    await panel.getByText('模拟清理故障', { exact: true }).waitFor();
    assert.equal(await days.inputValue(), '50');
    failCleanup = false;
    await checkbox.uncheck();
    await save.click();
    await cleanup.waitFor({ state: 'visible' });
    assert.equal(snapshot.policy.enabled, false);
    running = false;
    await page.getByRole('button', { name: '刷新状态', exact: true }).click();
    await page.getByRole('button', { name: '启动后端', exact: true }).waitFor();
    await cleanup.click();
    assert.equal(writes.at(-1).path, '/config/logging/cleanup');
    assert.equal(writes.some((item) => item.path === '/restart'), false);
    assert.deepEqual(errors, []);
    await mkdir(path.join(root, 'test-results', 'log-retention'), { recursive: true });
    await page.screenshot({ path: path.join(root, 'test-results', 'log-retention', 'settings.png'), fullPage: true });
    console.log('Log retention settings: validation, draft preservation, conflict, hot save, manual cleanup, partial failure and no restart passed');
    console.log('PASS: 慢轮询不重叠, 隐藏停止/恢复单次补查, 离开标签取消只读请求并保留草稿');
  } finally {
    if (browser) await runtime.closeContexts();
    await runtime.releaseServer(server);
  }

}

async function log_stream(runtime) {







  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  const production = process.env.LOG_STREAM_PRODUCTION === '1';
  const server = production ? await runtime.preview({ root, preview: { host: '127.0.0.1', port: 0 } })
    : await runtime.vite({ root, server: { host: '127.0.0.1', port: 0 } });
  let browser;
  try {
    if (!production) await runtime.listen(server);
    const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
    const screenshots = path.join(root, 'test-results', 'log-stream');
    await fs.mkdir(screenshots, { recursive: true });
    browser = await runtime.browser;
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
    await mockConfigVersions(page, origin, () => JSON.stringify({}));
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
    if (browser) await runtime.closeContexts();
    await runtime.releaseServer(server);
  }

}

async function storage_audit(runtime) {
  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  const server = await runtime.vite({ root, server: { host: '127.0.0.1', port: 0 } });
  try {
    await runtime.listen(server);
    const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
    const page = await runtime.browser.newPage({ viewport: { width: 1280, height: 1100 } });
    page.setDefaultTimeout(10000);
    const errors = [];
    const writes = [];
    page.on('pageerror', (error) => errors.push(error.message));
    await page.routeWebSocket('ws://127.0.0.1:19870/ws/status**', () => {});
    const cors = { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true',
      'Access-Control-Allow-Headers': 'Content-Type', 'Access-Control-Allow-Methods': 'GET,POST,OPTIONS' };
    const items = ['old-main', 'old-workflow', 'old-files'].map((id, index) => ({
      item_id: id, category: index < 2 ? 'orphan_database_rows' : 'orphan_session_directory',
      platform_id: 'chat', session_id: id, path: index < 2 ? 'F:/data/platform.db' : 'F:/data/sessions/old-files',
      size_bytes: index < 2 ? null : 4096, modified_at: 0, reason: '会话已不存在',
      recommended_action: 'cleanup', auto_safe: true, details: {},
    }));
    await page.route('**/*', async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.origin === origin && url.pathname !== '/ui-config.json') return route.continue();
      if (url.pathname === '/ui-config.json') return route.fulfill({ json: {
        backend_api: 'http://127.0.0.1:19870', control_api: 'http://127.0.0.1:19871', chat_api: 'http://127.0.0.1:19872',
      } });
      if (request.method() === 'OPTIONS') return route.fulfill({ status: 204, headers: cors });
      if (request.method() !== 'GET' && url.pathname !== '/auth/session') writes.push(url.pathname);
      if (url.pathname === '/config') return route.fulfill({ json: {
        ok: true, exists: true, config: { api: { host: '127.0.0.1', port: 19870 } },
      }, headers: cors });
      if (url.pathname === '/storage/audit') return route.fulfill({ json: {
        items, summary: { count: 3, size_bytes: 4096, unmeasured_count: 2 },
      }, headers: cors });
      return route.fulfill({ json: url.pathname === '/status' || url.pathname === '/api/health'
        ? { running: false, adapters: {} } : {}, headers: cors });
    });
    await mockConfigVersions(page, origin, () => JSON.stringify({ revision: 1 }));
    await page.goto(`${origin}/settings`);
    await page.getByRole('button', { name: '数据维护', exact: true }).click();
    await page.getByRole('button', { name: '扫描', exact: true }).click();
    await page.getByText('已统计文件 4.0 KiB', { exact: true }).waitFor();
    await page.getByText('记录大小未单独统计 2 项', { exact: true }).waitFor();
    assert.equal(await page.getByRole('cell', { name: '未单独统计', exact: true }).count(), 2);
    assert.equal(await page.getByText(/GiB/).count(), 0);
    assert.deepEqual(writes, []);
    assert.deepEqual(errors, []);
    await mkdir(path.join(root, 'test-results', 'storage-audit'), { recursive: true });
    await page.screenshot({ path: path.join(root, 'test-results', 'storage-audit', 'settings.png'), fullPage: true });
    console.log('PASS: 数据库记录不重复计入整库大小, 文件汇总及未知大小正常显示, 扫描不触发清理');
  } finally {
    await runtime.closeContexts();
    await runtime.releaseServer(server);
  }
}

export const scenarios = [
  ["storage-audit", storage_audit],
  ["log-retention", log_retention],
  ["log-stream", log_stream]
];
if (process.argv[1] && pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url) await runSuite(scenarios);
