import { finishConfirmation } from "./confirmation.mjs";
import assert from "node:assert/strict";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { runSuite, mockConfigVersions } from './runtime.mjs';
import { pathToFileURL } from 'node:url';

async function friends(runtime) {







  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  const server = await runtime.vite({ root, server: { host: '127.0.0.1', port: 0 } });
  let browser;
  try {
    await runtime.listen(server);
    const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
    const cors = { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true', 'Access-Control-Allow-Headers': 'Content-Type', 'Access-Control-Allow-Methods': 'GET,POST,PATCH,OPTIONS' };
    browser = await runtime.browser;
    const page = await browser.newPage();
    const errors = [];
    const writes = [];
    let account = 'bot@example.test';
    let protectedIds = [];
    let supported = true;
    let unreadableSuspicious = true;
    let friends = [{ user_id: 'alice', nickname: '同名', remark: '甲' }, { user_id: 'bob', nickname: '同名', remark: '乙' }, { user_id: 'manager', nickname: '管理者', remark: '' }];
    const requestFields = { request_category: 'normal', revision: 1, archived: false, archived_at: null, platform_state: 'unknown', execution_state: 'not_started', last_checked_at: null, decision: null, can_handle: true, requires_confirmation: false, handling_reason: '可处理' };
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
        if (tail === '/requests') {
          const view = url.searchParams.get('view');
          const category = url.searchParams.get('request_category');
          assert.ok(['all', 'normal', 'suspicious'].includes(category));
          const source = category === 'normal' ? { state: 'not_queried', complete: false } : {
            state: 'queried', complete: false, reason: '查询覆盖范围未确认: 平台接口未提供总数或分页证据, 不能证明已覆盖全部申请',
            unavailable_count: unreadableSuspicious ? 1 : 0,
            unavailable_reasons: unreadableSuspicious ? [{ code: 'invalid_credential', message: '平台返回的申请缺少有效处理凭据, 无法登记或处理', count: 1 }] : [],
          };
          return route.fulfill({ headers: cors, json: { items: requests.filter((row) => (view === 'all' || row.archived === (view === 'archived')) && (category === 'all' || row.request_category === category)), next_cursor: null, has_more: false, coverage: { complete: false, suspicious: source } } });
        }
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
            if (row.request_category === 'suspicious') assert.equal(body.params.remark, '');
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
    await mockConfigVersions(page, origin, () => JSON.stringify({}));
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
    const unreadableMessage = page.getByText('有 1 条可疑申请未能读取: 平台返回的申请缺少有效处理凭据, 无法登记或处理', { exact: true });
    await unreadableMessage.waitFor();
    await page.getByText('可疑申请查询: 查询覆盖范围未确认: 平台接口未提供总数或分页证据, 不能证明已覆盖全部申请', { exact: true }).waitFor();
    unreadableSuspicious = false;
    await page.getByRole('button', { name: '刷新', exact: true }).click();
    await unreadableMessage.waitFor({ state: 'hidden' });
    await page.getByText('可疑申请查询: 查询覆盖范围未确认: 平台接口未提供总数或分页证据, 不能证明已覆盖全部申请', { exact: true }).waitFor();
    await page.getByText('申请人: 昵称未提供 · ID: charlie', { exact: true }).waitFor();
    await page.getByRole('button', { name: '同意', exact: true }).click();
    await page.getByLabel('好友备注', { exact: true }).fill('测试备注');
    await page.getByRole('button', { name: '确认同意', exact: true }).click();
    await page.getByRole('status').filter({ hasText: '平台返回成功' }).waitFor();
    await page.getByRole('button', { name: '关闭', exact: true }).click();
    await page.getByText('当前筛选下没有可展示的申请记录', { exact: true }).waitFor();
    requests.push({ ...requestFields, request_category: 'suspicious', request_id: 'rq_doubt', user_id: 'doubt-user', nickname: '可疑昵称', comment: '可疑验证信息', suspicious_reason: '平台风险提示', request_source: '账号查找', requested_at: Date.now() / 1000 - 40, received_at: Date.now() / 1000, expires_at: Date.now() / 1000 + 600 });
    await page.getByRole('button', { name: '可疑申请', exact: true }).click();
    await page.getByText('平台标记为可疑 · 原因: 平台风险提示', { exact: true }).waitFor();
    await page.getByText('申请来源: 账号查找', { exact: true }).waitFor();
    await page.getByRole('button', { name: '同意', exact: true }).click();
    assert.equal(await page.getByLabel('好友备注').count(), 0);
    await page.getByRole('button', { name: '确认同意', exact: true }).click();
    await page.getByRole('status').filter({ hasText: '平台返回成功' }).waitFor();
    await page.getByRole('button', { name: '关闭', exact: true }).click();
    await page.getByRole('button', { name: '普通申请', exact: true }).click();
    await page.getByRole('button', { name: '已归档', exact: true }).click();
    const archivedRow = page.getByText('申请人: 昵称未提供 · ID: archived-user', { exact: true }).locator('..');
    await archivedRow.getByRole('button', { name: '重新核验', exact: true }).click();
    await page.getByText('仅完成本地核验, 当前平台无法查询申请是否仍有效', { exact: true }).waitFor();
    await archivedRow.getByRole('button', { name: '同意', exact: true }).click();
    await page.getByRole('status').filter({ hasText: '此申请已在本地归档' }).waitFor();
    await page.getByRole('button', { name: '确认同意', exact: true }).click();
    await page.getByRole('status').filter({ hasText: '平台返回成功' }).waitFor();
    await page.getByRole('button', { name: '关闭', exact: true }).click();
    await page.waitForFunction(() => Array.from(document.querySelectorAll('button')).filter((button) => button.textContent === '同意').every((button) => button.disabled));
    assert.equal(writes.filter((record) => record.action_type === 'handle_request').length, 3);
    await page.getByText('申请保留设置', { exact: true }).click();
    await page.getByLabel('申请凭据保留天数').fill('10');
    await page.getByLabel('申请历史保留天数').fill('60');
    await page.getByRole('button', { name: '保存保留设置', exact: true }).click();
    await page.getByText('申请保留设置已保存', { exact: true }).waitFor();
    assert.deepEqual(requestPolicy, { credential_days: 10, history_days: 60 });
    await archivedRow.getByRole('button', { name: '删除历史', exact: true }).click();
    await finishConfirmation(page, true, /不删除防重复执行记录/);
    await page.waitForFunction(() => !document.body.textContent.includes('ID: archived-user'));
    await page.getByRole('button', { name: '操作记录与审批', exact: true }).click();
    await page.getByRole('button', { name: '批准删除', exact: true }).waitFor();
    await page.getByRole('button', { name: '批准删除', exact: true }).click();
    await finishConfirmation(page, true, /bob[\s\S]*乙|乙[\s\S]*bob/);
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
    if (browser) await runtime.closeContexts();
    await runtime.releaseServer(server);
  }

}

async function account_requests(runtime) {







  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  const server = await runtime.vite({ root, server: { host: '127.0.0.1', port: 0 } });
  let browser;
  try {
    await runtime.listen(server);
    const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
    browser = await runtime.browser;
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
    await dialog.getByRole('button', { name: '接受邀请', exact: true }).click();
    await finishConfirmation(page, false);
    assert.equal(writes.length, 0);
    await dialog.getByRole('button', { name: '下一批邀请' }).click();
    await dialog.getByText('目标群: 888 · 邀请人: 321', { exact: true }).waitFor();
    failNextWrite = true;
    await dialog.getByRole('button', { name: '拒绝邀请', exact: true }).evaluate((button) => { button.click(); button.click(); });
    await page.getByRole('dialog', { name: '确认操作', exact: true }).waitFor();
    assert.equal(writes.length, 0);
    await finishConfirmation(page);
    await dialog.getByRole('alert').filter({ hasText: '模拟提交失败' }).waitFor();
    await dialog.getByRole('button', { name: '拒绝邀请', exact: true }).click();
    await finishConfirmation(page);
    await dialog.getByRole('status').filter({ hasText: '平台已执行' }).waitFor();
    assert.equal(writes[0].action_id, writes[1].action_id);
    assert.equal(writes[1].approve, false);
    await dialog.getByLabel('邀请记录范围').selectOption('archived');
    await dialog.getByText('已拒绝邀请', { exact: true }).waitFor();
    assert.ok(await dialog.getByRole('button', { name: '接受邀请', exact: true }).isDisabled());
    await dialog.getByRole('button', { name: '下一批邀请' }).click();
    await dialog.getByText('目标群: 777 · 邀请人: 321', { exact: true }).waitFor();
    await dialog.getByRole('button', { name: '接受邀请', exact: true }).click();
    await finishConfirmation(page);
    await dialog.getByText('接受群 777 的邀请 · 平台已执行', { exact: true }).waitFor();
    assert.equal(writes[2].expected_revision, 2);
    await dialog.getByRole('button', { name: '批准执行', exact: true }).click();
    await finishConfirmation(page);
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
    await dialog.getByLabel('邀请记录范围').selectOption('active');
    await dialog.getByRole('button', { name: '接受邀请', exact: true }).click();
    const unchangedWrites = writes.length;
    await page.getByRole('dialog', { name: '确认操作', exact: true }).waitFor();
    await page.getByRole('dialog', { name: '机器人 100 的入群邀请', exact: true }).getByRole('button').first().dispatchEvent('click');
    await page.getByRole('dialog', { name: '确认操作', exact: true }).waitFor({ state: 'hidden' });
    assert.equal(writes.length, unchangedWrites);
    await page.getByRole('button', { name: '机器人入群邀请', exact: true }).click();
    await dialog.getByRole('button', { name: '接受邀请', exact: true }).click();
    await page.getByRole('dialog', { name: '确认操作', exact: true }).waitFor();
    await page.locator('a[href="/"]').first().dispatchEvent('click');
    await page.getByRole('dialog', { name: '确认操作', exact: true }).waitFor({ state: 'hidden' });
    assert.equal(writes.length, unchangedWrites);
    assert.deepEqual(errors, []);
    assert.ok(reads.length > 4);
    console.log('PASS: 未入群账号邀请查询/分页, 提交失败复用幂等 ID, 归档确认, 终态禁用, 独立审批及关闭取消查询, 异步确认取消/重复点击/来源关闭/站内导航取消');
  } finally {
    if (browser) await runtime.closeContexts();
    await runtime.releaseServer(server);
  }

}

export const scenarios = [
  ["friends", friends],
  ["account-requests", account_requests]
];
if (process.argv[1] && pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url) await runSuite(scenarios);
