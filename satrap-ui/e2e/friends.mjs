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
  const cors = { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true', 'Access-Control-Allow-Headers': 'Content-Type', 'Access-Control-Allow-Methods': 'GET,POST,PATCH,OPTIONS' };
  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  const errors = [];
  const writes = [];
  let account = 'bot@example.test';
  let protectedIds = [];
  let supported = true;
  let friends = [{ user_id: 'alice', nickname: '同名', remark: '甲' }, { user_id: 'bob', nickname: '同名', remark: '乙' }, { user_id: 'manager', nickname: '管理者', remark: '' }];
  const requestFields = { revision: 1, archived: false, archived_at: null, platform_state: 'unknown', execution_state: 'not_started', last_checked_at: null, decision: null, can_handle: true, requires_confirmation: false, handling_reason: '可处理' };
  let requests = [{ ...requestFields, request_id: 'rq_1', user_id: 'charlie', comment: '请添加我, 这是验证信息', received_at: Date.now() / 1000 - 20, expires_at: Date.now() / 1000 + 600 },
    { ...requestFields, request_id: 'rq_2', user_id: 'archived-user', comment: '本地到期但仍有凭据', revision: 2, archived: true, archived_at: Date.now() / 1000 - 100, requires_confirmation: true, received_at: Date.now() / 1000 - 1000, expires_at: Date.now() / 1000 - 400 }];
  let requestPolicy = { credential_days: 30, history_days: 90 };
  let actions = [{ action_id: 'model-pending', self_id: account, action_type: 'delete_friend', params: { user_id: 'bob' }, target: friends[1], actor_kind: 'model', state: 'pending', created_at: Date.now() / 1000, expires_at: Date.now() / 1000 + 600, result: null }];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.route(origin + '/ui-config.json', (route) => route.fulfill({ json: { backend_api: 'http://127.0.0.1:19870', control_api: 'http://127.0.0.1:19871', chat_api: 'http://127.0.0.1:19872' } }));
  await page.route(/^http:\/\/127\.0\.0\.1:1987[012]\//, async (route) => {
    if (route.request().method() === 'OPTIONS') return route.fulfill({ status: 204, headers: cors });
    const url = new URL(route.request().url());
    const pathname = url.pathname;
    if (pathname.includes('/friends')) {
      const tail = pathname.split('/friends')[1];
      const method = route.request().method();
      const body = method === 'POST' || method === 'PATCH' ? route.request().postDataJSON() : null;
      if (tail === '/info') return route.fulfill({ headers: cors, json: { current_account: account, protected_friend_ids: protectedIds, manager_ids: ['manager'], capabilities: Object.fromEntries(['list_friends', 'list_requests', 'handle_request', 'delete_friend', 'send_request'].map((name) => [name, { state: supported ? 'supported' : 'unsupported', reason: supported ? '适配器已实现' : name === 'send_request' ? '当前适配器不支持主动好友申请' : '当前适配器不支持好友操作' }])) } });
      if (body) {
        writes.push(body);
        assert.equal(body.expected_self_id, account);
      } else assert.equal(url.searchParams.get('account'), account);
      if (tail === '' && method === 'GET') {
        const q = url.searchParams.get('q') || '';
        const items = friends.filter((row) => [row.user_id, row.nickname, row.remark].some((value) => value.includes(q)));
        return route.fulfill({ headers: cors, json: { items, next_cursor: null, has_more: false, coverage: { complete: true } } });
      }
      if (tail === '/requests') return route.fulfill({ headers: cors, json: { items: requests.filter((row) => row.archived === (url.searchParams.get('view') === 'archived')), next_cursor: null, has_more: false } });
      if (tail === '/request-policy') {
        if (method === 'PATCH') requestPolicy = { credential_days: body.credential_days, history_days: body.history_days };
        return route.fulfill({ headers: cors, json: requestPolicy });
      }
      if (tail.startsWith('/requests/') && method === 'POST') {
        const row = requests.find((item) => item.request_id === tail.split('/')[2]);
        if (tail.endsWith('/delete')) {
          assert.equal(body.expected_revision, row.revision);
          requests = requests.filter((item) => item !== row);
          return route.fulfill({ headers: cors, json: { status: 'deleted' } });
        }
        return route.fulfill({ headers: cors, json: { ...row, verification: 'local_only', platform_query_supported: false } });
      }
      if (tail === '/policy') { protectedIds = body.protected_friend_ids; return route.fulfill({ headers: cors, json: { protected_friend_ids: protectedIds, manager_ids: ['manager'] } }); }
      if (tail === '/actions' && method === 'GET') return route.fulfill({ headers: cors, json: { items: actions, has_more: false } });
      if (tail === '/actions' && method === 'POST') {
        assert.ok(body.action_id);
        let target = null;
        if (body.action_type === 'delete_friend') { target = friends.find((row) => row.user_id === body.params.user_id); friends = friends.filter((row) => row.user_id !== body.params.user_id); }
        else if (body.action_type === 'send_request') {
          assert.equal(body.params.user_id, 'new-friend');
          assert.equal(body.params.message, '主动申请验证');
        } else {
          const row = requests.find((item) => item.request_id === body.params.request_id);
          assert.equal(body.params.expected_revision, row.revision);
          assert.equal(body.params.approve, true);
          if (row.request_id === 'rq_1') assert.equal(body.params.remark, '测试备注');
          Object.assign(row, { archived: true, archived_at: Date.now() / 1000, can_handle: false, execution_state: 'succeeded', platform_state: 'processed', revision: row.revision + 1 });
        }
        const record = { action_id: body.action_id, self_id: account, action_type: body.action_type, params: body.params, target, actor_kind: 'panel', state: 'succeeded', created_at: Date.now() / 1000, expires_at: Date.now() / 1000 + 600, result: { message: body.action_type === 'send_request' ? '好友申请已提交, 不代表已成为好友' : '平台返回成功', verification: body.action_type === 'delete_friend' ? 'confirmed' : 'not_verified' } };
        actions.unshift(record);
        return route.fulfill({ headers: cors, json: record });
      }
      if (tail === '/actions/model-pending/decision') {
        const record = actions.find((row) => row.action_id === 'model-pending');
        record.state = body.approve ? 'succeeded' : 'rejected';
        if (body.approve) friends = friends.filter((row) => row.user_id !== 'bob');
        record.result = { message: body.approve ? '平台返回成功' : '已拒绝', verification: 'confirmed' };
        return route.fulfill({ headers: cors, json: record });
      }
      return route.fulfill({ headers: cors, status: 404, json: { error: 'unexpected friend route' } });
    }
    return route.fulfill({ headers: cors, json: { ok: true, running: false, adapters: {}, configs: {}, plugins: [] } });
  });
  await page.routeWebSocket(/ws:\/\/127\.0\.0\.1:1987[012]\//, () => {});
  await page.goto(origin + '/platforms/generic/friends');
  await page.getByRole('heading', { name: '好友管理', exact: true }).waitFor();
  await page.getByRole('cell', { name: 'alice', exact: true }).waitFor();
  await page.getByLabel('搜索好友').fill('同名');
  await page.getByRole('button', { name: '搜索', exact: true }).click();
  await page.getByRole('cell', { name: 'bob', exact: true }).waitFor();
  await page.waitForFunction(() => document.querySelectorAll('tbody tr').length === 2);
  assert.equal(await page.getByRole('button', { name: '查看详情' }).count(), 2);
  await page.getByRole('row').filter({ has: page.getByRole('cell', { name: 'alice', exact: true }) }).getByRole('button', { name: '查看详情' }).click();
  await page.getByText('好友 ID: alice', { exact: true }).waitFor();
  await page.getByText('好友备注: 甲', { exact: true }).waitFor();
  assert.equal(writes.length, 0);
  await page.getByRole('button', { name: '确认删除', exact: true }).click();
  await page.getByRole('status').filter({ hasText: '好友关系已删除' }).waitFor();
  assert.equal(writes.length, 1);
  assert.equal(writes[0].params.user_id, 'alice');
  assert.ok(await page.getByRole('button', { name: '确认删除', exact: true }).isDisabled());
  await page.getByRole('button', { name: '关闭', exact: true }).click();
  await page.getByRole('button', { name: '好友申请', exact: true }).click();
  await page.getByText('申请人 ID: charlie', { exact: true }).waitFor();
  await page.getByRole('button', { name: '同意', exact: true }).click();
  await page.getByLabel('好友备注', { exact: true }).fill('测试备注');
  await page.getByRole('button', { name: '确认同意', exact: true }).click();
  await page.getByRole('status').filter({ hasText: '平台返回成功' }).waitFor();
  await page.getByRole('button', { name: '关闭', exact: true }).click();
  await page.getByText('没有待处理好友申请', { exact: true }).waitFor();
  await page.getByRole('button', { name: '已归档', exact: true }).click();
  const archivedRow = page.getByText('申请人 ID: archived-user', { exact: true }).locator('..');
  await archivedRow.getByRole('button', { name: '重新核验', exact: true }).click();
  await page.getByText('仅完成本地核验, 当前平台无法查询申请是否仍有效', { exact: true }).waitFor();
  await archivedRow.getByRole('button', { name: '同意', exact: true }).click();
  await page.getByRole('status').filter({ hasText: '此申请已在本地归档' }).waitFor();
  await page.getByRole('button', { name: '确认同意', exact: true }).click();
  await page.getByRole('status').filter({ hasText: '平台返回成功' }).waitFor();
  await page.getByRole('button', { name: '关闭', exact: true }).click();
  await page.waitForFunction(() => Array.from(document.querySelectorAll('button')).filter((button) => button.textContent === '同意').every((button) => button.disabled));
  assert.equal(writes.filter((record) => record.action_type === 'handle_request').length, 2);
  await page.getByText('申请保留设置', { exact: true }).click();
  await page.getByLabel('申请凭据保留天数').fill('10');
  await page.getByLabel('申请历史保留天数').fill('60');
  await page.getByRole('button', { name: '保存保留设置', exact: true }).click();
  await page.getByText('申请保留设置已保存', { exact: true }).waitFor();
  assert.deepEqual(requestPolicy, { credential_days: 10, history_days: 60 });
  page.once('dialog', (dialog) => { assert.ok(dialog.message().includes('不删除防重复执行记录')); dialog.accept(); });
  await archivedRow.getByRole('button', { name: '删除历史', exact: true }).click();
  await page.waitForFunction(() => !document.body.textContent.includes('申请人 ID: archived-user'));
  await page.getByRole('button', { name: '操作记录与审批', exact: true }).click();
  await page.getByRole('button', { name: '批准删除', exact: true }).waitFor();
  page.once('dialog', (dialog) => { assert.ok(dialog.message().includes('bob')); assert.ok(dialog.message().includes('乙')); dialog.accept(); });
  await page.getByRole('button', { name: '批准删除', exact: true }).click();
  await page.waitForFunction(() => !Array.from(document.querySelectorAll('button')).some((button) => button.textContent === '批准删除'));
  assert.equal(actions.find((row) => row.action_id === 'model-pending').state, 'succeeded');
  await page.getByRole('button', { name: '删除保护', exact: true }).click();
  await page.getByLabel('额外保护好友 ID').fill('protected-user');
  await page.getByRole('button', { name: '保存保护名单', exact: true }).click();
  await page.getByText('保护名单已保存', { exact: true }).waitFor();
  assert.deepEqual(protectedIds, ['protected-user']);
  await page.getByRole('button', { name: '好友列表', exact: true }).click();
  await page.getByLabel('搜索好友').fill('');
  await page.getByRole('button', { name: '搜索', exact: true }).click();
  await page.getByRole('cell', { name: 'manager', exact: true }).waitFor();
  await page.getByRole('button', { name: '查看详情', exact: true }).click();
  assert.ok(await page.getByRole('button', { name: '确认删除', exact: true }).isDisabled());
  await page.getByRole('button', { name: '取消', exact: true }).click();
  await page.getByRole('button', { name: '发送好友申请', exact: true }).click();
  await page.getByLabel('好友申请目标账号').fill('new-friend');
  await page.getByLabel('好友申请验证文字').fill('主动申请验证');
  const writesBeforeSend = writes.length;
  await page.getByRole('button', { name: '确认发送', exact: true }).click();
  await page.getByRole('status').filter({ hasText: '好友申请已提交, 不代表已成为好友' }).waitFor();
  assert.equal(writes.length, writesBeforeSend + 1);
  assert.equal(writes.at(-1).action_type, 'send_request');
  assert.ok(await page.getByRole('button', { name: '确认发送', exact: true }).isDisabled());
  await page.getByRole('button', { name: '关闭', exact: true }).click();
  await page.setViewportSize({ width: 390, height: 844 });
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
  supported = false;
  await page.getByRole('button', { name: '刷新', exact: true }).click();
  await page.getByText('当前适配器不支持好友操作', { exact: true }).waitFor();
  assert.ok(await page.getByRole('button', { name: '发送好友申请', exact: true }).isDisabled());
  account = 'new-account';
  await page.getByRole('button', { name: '刷新', exact: true }).click();
  await page.getByText('平台: generic · 机器人账号: new-account', { exact: true }).waitFor();
  assert.deepEqual(errors, []);
  console.log('PASS: 好友列表与同名搜索, 人工删除确认与防重复, 申请备注处理, 模型审批, 保护名单, 账号切换, 不支持能力与窄屏布局');
} finally {
  if (browser) await browser.close();
  await server.close();
}
