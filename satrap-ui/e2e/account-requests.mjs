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
  page.setDefaultTimeout(15000);
  const errors = [], writes = [], reads = [];
  let failNextWrite = false, delayNextRead = false, releaseRead, heldRead;
  const invitation = (id, group, archived = false) => ({ request_id: id, group_id: group, user_id: '321', comment: '邀请机器人加入',
    revision: archived ? 2 : 1, archived, can_handle: true, execution_state: 'not_started', decision: null,
    handling_reason: '可处理', received_at: Date.now() / 1000 - (archived ? 1000 : 20) });
  const invitations = [invitation('rq-one', '999'), invitation('rq-two', '888'), invitation('rq-archive', '777', true)];
  const actions = [{ action_id: 'model-invite', group_id: '666', state: 'pending', params: { sub_type: 'invite', approve: true }, result: null }];
  const cors = { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true',
    'Access-Control-Allow-Headers': 'Content-Type', 'Access-Control-Allow-Methods': 'GET,POST,OPTIONS' };
  page.on('pageerror', (error) => errors.push(error.message));
  await page.route(origin + '/ui-config.json', (route) => route.fulfill({ json: {
    backend_api: 'http://127.0.0.1:19870', control_api: 'http://127.0.0.1:19871', chat_api: 'http://127.0.0.1:19872',
  } }));
  await page.route(/^http:\/\/127\.0\.0\.1:1987[012]\//, async (route) => {
    const request = route.request(), url = new URL(request.url());
    const reply = (json, status = 200) => route.fulfill({ json, status, headers: cors });
    if (request.method() === 'OPTIONS') return route.fulfill({ status: 204, headers: cors });
    if (url.pathname.includes('/account-requests/group-invites')) {
      assert.ok(url.pathname.startsWith('/api/platforms/'));
      if (request.method() === 'GET') {
        assert.equal(url.searchParams.get('account'), '100');
        reads.push(url.search);
        if (url.pathname.endsWith('/actions')) {
          assert.equal(url.searchParams.get('state'), 'all');
          return reply({ items: actions, total: actions.length, page: 1, page_size: 25 });
        }
        const view = url.searchParams.get('view');
        assert.ok(['active', 'archived', 'all'].includes(view));
        const items = invitations.filter((row) => view === 'all' || row.archived === (view === 'archived'));
        const start = url.searchParams.get('cursor') ? items.findIndex((row) => row.request_id === url.searchParams.get('cursor')) + 1 : 0;
        const snapshot = structuredClone({ items: items.slice(start, start + 1), next_cursor: start + 1 < items.length ? items[start].request_id : null,
          has_more: start + 1 < items.length, unavailable_count: 1 });
        if (delayNextRead) {
          delayNextRead = false;
          await new Promise((resolve) => { releaseRead = resolve; heldRead(); });
        }
        if (request.failure()) return;
        return reply(snapshot);
      }
      const body = request.postDataJSON();
      assert.equal(body.expected_self_id, '100');
      writes.push(body);
      if (url.pathname.endsWith('/decision')) {
        assert.deepEqual(Object.keys(body).sort(), ['approve', 'expected_self_id']);
        actions[0].state = body.approve ? 'succeeded' : 'rejected';
        return reply(actions[0]);
      }
      const row = invitations.find((item) => item.request_id === body.request_id);
      assert.equal(body.expected_revision, row.revision);
      assert.ok(body.action_id);
      assert.equal(Object.keys(body).length, 5);
      if (failNextWrite) { failNextWrite = false; return reply({ error: '模拟提交失败' }, 503); }
      Object.assign(row, { archived: true, can_handle: false, execution_state: 'succeeded', decision: body.approve ? 'accepted' : 'rejected' });
      const record = { action_id: body.action_id, group_id: row.group_id, state: 'succeeded', params: { ...body, sub_type: 'invite' }, result: { reason: 'platform_confirmed' } };
      actions.push(record);
      return reply(record);
    }
    if (url.pathname.endsWith('/groups/accounts')) return reply({ items: [{ self_id: '100', mode: 'all', revision: 1 }], current_account: '100', waiting_for_account: false });
    if (url.pathname.endsWith('/groups/settings')) return reply({ self_id: '100', mode: 'all', approval_actions: [], approval_defaults: {}, revision: 1 });
    if (url.pathname.endsWith('/groups')) return reply({ items: [], total: 0, page: 1, page_size: 25, account: '100', current_account: '100', counts: { joined: 0, response_enabled: 0, configured: 0 }, sync: { status: 'never' } });
    if (url.pathname === '/config/versions') return reply({ ok: true, resources: {} });
    if (url.pathname === '/config/platforms') return reply({ platforms: [] });
    return reply({ ok: true, running: true, managed: false, adapters: {}, configs: {}, plugins: [] });
  });
  await page.routeWebSocket(/ws:\/\/127\.0\.0\.1:1987[012]\//, () => {});
  await page.goto(origin + '/platforms/bot/groups?account=100');
  await page.getByRole('button', { name: '机器人入群邀请', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByText('目标群: 999 · 邀请人: 321', { exact: true }).waitFor();
  await dialog.getByText('有 1 条邀请缺少可读取的原记录, 无法处理', { exact: true }).waitFor();
  assert.equal(writes.length, 0);
  await dialog.getByRole('button', { name: '下一批邀请' }).click();
  await dialog.getByText('目标群: 888 · 邀请人: 321', { exact: true }).waitFor();
  failNextWrite = true;
  page.once('dialog', (message) => message.accept());
  await dialog.getByRole('button', { name: '拒绝邀请', exact: true }).click();
  await dialog.getByRole('alert').filter({ hasText: '模拟提交失败' }).waitFor();
  page.once('dialog', (message) => message.accept());
  await dialog.getByRole('button', { name: '拒绝邀请', exact: true }).click();
  await dialog.getByRole('status').filter({ hasText: '平台已执行' }).waitFor();
  assert.equal(writes[0].action_id, writes[1].action_id);
  assert.equal(writes[1].approve, false);
  await dialog.getByLabel('邀请记录范围').selectOption('archived');
  await dialog.getByText('已拒绝邀请', { exact: true }).waitFor();
  assert.ok(await dialog.getByRole('button', { name: '接受邀请', exact: true }).isDisabled());
  await dialog.getByRole('button', { name: '下一批邀请' }).click();
  await dialog.getByText('目标群: 777 · 邀请人: 321', { exact: true }).waitFor();
  page.once('dialog', (message) => message.accept());
  await dialog.getByRole('button', { name: '接受邀请', exact: true }).click();
  await dialog.getByText('接受群 777 的邀请 · 平台已执行', { exact: true }).waitFor();
  assert.equal(writes[2].expected_revision, 2);
  page.once('dialog', (message) => message.accept());
  await dialog.getByRole('button', { name: '批准执行', exact: true }).click();
  await dialog.getByText('接受群 666 的邀请 · 平台已执行', { exact: true }).waitFor();
  assert.equal(writes[3].approve, true);
  const pending = new Promise((resolve) => { heldRead = resolve; });
  delayNextRead = true;
  await dialog.getByRole('button', { name: '刷新', exact: true }).click();
  await pending;
  await page.keyboard.press('Escape');
  releaseRead();
  await dialog.waitFor({ state: 'hidden' });
  await page.getByRole('button', { name: '机器人入群邀请', exact: true }).click();
  await dialog.getByText('接受群 777 的邀请 · 平台已执行', { exact: true }).waitFor();
  assert.deepEqual(errors, []);
  assert.ok(reads.length > 4);
  console.log('PASS: 未入群账号邀请查询/分页, 提交失败复用幂等 ID, 归档确认, 终态禁用, 独立审批及关闭取消查询');
} finally {
  if (browser) await browser.close();
  await server.close();
}
