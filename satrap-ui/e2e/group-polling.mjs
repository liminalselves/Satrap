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
  page.on('pageerror', (error) => errors.push(error.message));
  await page.clock.install();
  const config = {
    account: '100', current_account: '100', group: { group_id: '456', group_name: '轮询测试群', membership: 'joined' },
    explicit: { policy: {}, session: {}, events: {}, approval: {} }, revision: 0, saved_revision: 0,
    active_revision: 0, apply_status: 'applied', route_generation: 0, base_revision: 'base',
    effective: { policy: { enabled: true }, session: {}, approval: {}, events: { group_ban: true } },
    sources: { policy: {}, session: {}, approval: {}, events: {} },
    capabilities: { policy_fields: [], session_fields: [], approval_actions: [], event_kinds: ['group_ban'] },
  };
  const counts = { config: 0, events: 0, diagnostics: 0, actions: 0, writes: 0 };
  let holdEvents = false, holdConfig = false;
  let releaseEvents, releaseConfig;
  let heldEvents, heldConfig;
  const cors = { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true',
    'Access-Control-Allow-Headers': 'Content-Type', 'Access-Control-Allow-Methods': 'GET,PATCH,POST,OPTIONS' };
  await page.route(origin + '/ui-config.json', (route) => route.fulfill({ json: {
    backend_api: 'http://127.0.0.1:19870', control_api: 'http://127.0.0.1:19871', chat_api: 'http://127.0.0.1:19872',
  } }));
  await page.route(/^http:\/\/127\.0\.0\.1:1987[012]\//, async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const reply = (json) => route.fulfill({ json, headers: cors });
    if (request.method() === 'OPTIONS') return route.fulfill({ status: 204, headers: cors });
    if (url.pathname === '/config/versions') {
      const names = (url.searchParams.get('resources') || '').split(',');
      return reply({ ok: true, resources: Object.fromEntries(names.map((name) => [name, { state: 'available', change_token: 'stable' }])) });
    }
    if (url.pathname.endsWith('/groups/456/config')) {
      if (request.method() === 'PATCH') {
        const body = request.postDataJSON();
        assert.equal(body.expected_revision, config.revision);
        assert.equal(body.expected_self_id, '100');
        counts.writes++;
        config.explicit.events = body.values;
        config.revision++; config.saved_revision++; config.active_revision++;
        return reply(config);
      }
      counts.config++;
      const snapshot = structuredClone(config);
      if (holdConfig) {
        holdConfig = false;
        await new Promise((resolve) => { releaseConfig = resolve; heldConfig(); });
      }
      if (request.failure()) return;
      return reply(snapshot);
    }
    if (url.pathname.endsWith('/groups/456/events')) {
      counts.events++;
      if (holdEvents) {
        holdEvents = false;
        await new Promise((resolve) => { releaseEvents = resolve; heldEvents(); });
      }
      if (request.failure()) return;
      return reply({ items: [], volatile: true, capacity: 4096, truncated: false });
    }
    if (url.pathname.endsWith('/groups/456/diagnostics')) {
      counts.diagnostics++;
      return reply({ records: [], available: true });
    }
    if (url.pathname.endsWith('/groups/456/actions')) {
      counts.actions++;
      return reply({ items: [], total: 0, page: 1, page_size: 25 });
    }
    if (url.pathname === '/config/models' || url.pathname === '/config/session-classes' || url.pathname === '/config/edictum/sessions') return reply({});
    if (url.pathname === '/config/platforms') return reply({ platforms: [] });
    return reply({ ok: true, running: true, adapters: {} });
  });
  await page.routeWebSocket(/ws:\/\/127\.0\.0\.1:1987[012]\//, () => {});
  const visibility = (hidden) => page.evaluate((value) => {
    if (value) Object.defineProperty(document, 'hidden', { configurable: true, get: () => true });
    else delete document.hidden;
    document.dispatchEvent(new Event('visibilitychange'));
  }, hidden);
  await page.goto(origin + '/platforms/ob/groups/456/events?account=100');
  await page.getByRole('heading', { name: '接收事件设置', exact: true }).waitFor();
  const eventSetting = page.locator('label').filter({ hasText: '群禁言' }).locator('select');
  await eventSetting.selectOption('false');
  const configPending = new Promise((resolve) => { heldConfig = resolve; });
  holdConfig = true;
  await page.clock.runFor(5500);
  await configPending;
  const lateConfig = page.waitForResponse((response) => new URL(response.url()).pathname.endsWith('/groups/456/config') && response.request().method() === 'GET');
  await page.getByRole('button', { name: '保存事件设置', exact: true }).click();
  await page.getByText(/配置修订 1/).waitFor();
  releaseConfig();
  await lateConfig;
  await page.getByText(/配置修订 1/).waitFor();
  assert.equal(counts.writes, 1);
  const eventPending = new Promise((resolve) => { heldEvents = resolve; });
  holdEvents = true;
  await page.getByRole('button', { name: '刷新', exact: true }).first().click();
  await eventPending;
  const slowReads = counts.events;
  await page.clock.runFor(7000);
  assert.equal(counts.events, slowReads);
  const slowResponse = page.waitForResponse((response) => new URL(response.url()).pathname.endsWith('/groups/456/events'));
  await visibility(true);
  releaseEvents();
  await slowResponse;
  await page.clock.runFor(60000);
  assert.equal(counts.events, slowReads);
  await eventSetting.selectOption('true');
  const resumed = page.waitForResponse((response) => new URL(response.url()).pathname.endsWith('/groups/456/events'));
  await visibility(false);
  await resumed;
  assert.equal(counts.events, slowReads + 1);
  assert.equal(await eventSetting.inputValue(), 'true');
  const canceledPending = new Promise((resolve) => { heldEvents = resolve; });
  holdEvents = true;
  await page.getByRole('button', { name: '刷新', exact: true }).first().click();
  await canceledPending;
  const cancellation = page.waitForEvent('requestfailed', { predicate: (request) => new URL(request.url()).pathname.endsWith('/groups/456/events') });
  page.once('dialog', (dialog) => dialog.accept());
  await page.getByRole('link', { name: '审批与记录', exact: true }).click();
  await cancellation;
  releaseEvents();
  await page.getByText('共 0 条', { exact: true }).waitFor();
  const afterLeave = counts.events;
  await page.clock.runFor(15000);
  assert.equal(counts.events, afterLeave);
  await page.locator('select').filter({ has: page.locator('option[value="executing"]') }).selectOption('executing');
  await page.clock.runFor(61000);
  await visibility(true);
  const deadlineReads = counts.actions;
  await page.clock.runFor(1000);
  await visibility(false);
  await page.clock.runFor(20000);
  assert.equal(counts.actions, deadlineReads);
  assert.deepEqual(errors, []);
  console.log('PASS: 群配置保存后拒收迟到快照, 事件慢读取不重叠, 隐藏停止/恢复单次补查并保留草稿');
  console.log('PASS: 切换群详情标签取消只读请求, 审批执行中轮询保留 60 秒截止且恢复可见不重启');
} finally {
  if (browser) await browser.close();
  await server.close();
}
