import { chromium } from 'playwright';
import { createServer } from 'node:http';
import { readFile, mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';
import { createBenchmarkFixture, responseFor, jsonResponse, installWebSocketMocks } from './fixtures.mjs';

const directory = path.dirname(fileURLToPath(import.meta.url));
const dist = path.resolve(directory, '../dist');
const server = createServer(async (request, response) => {
  try {
    const pathname = new URL(request.url, 'http://localhost').pathname;
    const filename = pathname.split('/').at(-1).includes('.') ? pathname.slice(1) : 'index.html';
    const target = path.resolve(dist, filename);
    if (!target.startsWith(dist + path.sep)) throw new Error('无效静态路径');
    response.setHeader('Content-Type', filename.endsWith('.js') ? 'application/javascript' : filename.endsWith('.css') ? 'text/css' : filename.endsWith('.html') ? 'text/html; charset=utf-8' : 'application/octet-stream');
    response.end(await readFile(target));
  } catch { response.writeHead(404); response.end(); }
});
await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
const origin = `http://127.0.0.1:${server.address().port}`;
const browser = await chromium.launch({ headless: true, channel: 'chromium' });
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
const page = await context.newPage();
page.setDefaultTimeout(10000);
const fixture = createBenchmarkFixture();
const errors = [];
const reads = [];
const decisions = [];
const checks = [];
let unavailable = false;
let failDecision = false;
let partial = false;
let backendDown = false;
const common = { platform_id: 'qq', self_id: '100', group_id: '888', action_id: 'collision', source: 'group', action_type: 'ban_group_member',
  params: { user_id: '42', duration: 43200 }, actor_kind: 'model', actor_id: 'sender', state: 'pending', created_at: Date.now() / 1000,
  expires_at: Date.now() / 1000 + 600, result: null, target: {}, can_decide: true, unavailable_reason: '' };
const actions = [{ ...common }, { ...common, source: 'invitation', action_id: 'invite', action_type: 'handle_group_request', params: { approve: false } },
  { ...common, source: 'friend', group_id: '', action_type: 'delete_friend' },
  { ...common, self_id: 'old', source: 'friend', action_id: 'historical', group_id: '', action_type: 'delete_friend', can_decide: false, unavailable_reason: '历史账号记录仅供查看' }];
const platforms = [{ platform_id: 'qq', current_account: '100', online: true, accounts: ['100', 'old'], pending_count: 3 }];
const records = [{ platform_id: 'qq', conversation_id: 'group-root', title: 'QQ 测试群', context_ids: ['group-context', 'group-extra'], message_count: 1, history_count: 0, supports_history: false, facets: {} },
  { platform_id: 'chat', conversation_id: 'chat-root', title: 'Chat 测试对话', context_ids: ['chat-context'], message_count: 1, history_count: 0, supports_history: true, facets: {} }];
page.on('pageerror', (error) => errors.push(error.message));
await context.route(`${origin}/ui-config.json`, (route) => route.fulfill(jsonResponse({ backend_api: 'http://127.0.0.1:19870', control_api: 'http://127.0.0.1:19871', chat_api: 'http://127.0.0.1:19872' }, origin)));
const handle = async (route) => {
  const request = route.request();
  const url = new URL(request.url());
  const pathname = url.pathname;
  reads.push(pathname);
  try {
    let data;
    if (pathname === '/api/platform-actions' || pathname.endsWith('/platform-actions/summary')) {
      if (unavailable) return await route.fulfill({ ...jsonResponse({ error: '后端不可用' }, origin), status: 503 });
      const state = url.searchParams.get('state') || 'pending';
      const account = url.searchParams.get('account') || 'current';
      const selected = actions.filter((item) => (state === 'all' || item.state === state) && item.self_id === (account === 'current' ? '100' : account)
        && (!url.searchParams.get('source') || url.searchParams.get('source') === 'all' || item.source === url.searchParams.get('source'))
        && (!url.searchParams.get('action_id') || item.action_id === url.searchParams.get('action_id')));
      data = { items: pathname.endsWith('/summary') ? [] : selected, total: selected.length, pending_count: actions.filter((item) => item.state === 'pending' && item.self_id === '100').length,
        platforms, complete: !partial, warnings: partial ? ['broken: 动作记录读取失败'] : [], has_more: false, next_cursor: null };
    } else if (backendDown && pathname === '/api/health') data = { running: false, adapters: {} };
    else if (backendDown && pathname === '/status') data = { running: false, managed: true };
    else if (pathname.endsWith('/decision')) {
      decisions.push({ path: pathname, body: request.postDataJSON() });
      const source = pathname.includes('/friends/') ? 'friend' : pathname.includes('/account-requests/') ? 'invitation' : 'group';
      const item = actions.find((entry) => entry.source === source && pathname.includes('/' + entry.action_id + '/decision'));
      assert.ok(item);
      item.state = source === 'invitation' ? 'unknown' : 'succeeded'; item.can_decide = false;
      if (failDecision) return await route.abort('failed');
      data = item;
    } else if (pathname === '/config/conversations/platforms') data = { platforms: ['qq', 'chat'], items: [{ id: 'qq', type: 'onebot', label: 'QQ', type_label: 'QQ' }, { id: 'chat', type: 'chat', label: 'Chat', type_label: 'Chat' }] };
    else if (pathname === '/config/conversations') {
      const selected = records.filter((item) => (!url.searchParams.get('platform_id') || item.platform_id === url.searchParams.get('platform_id'))
        && (!url.searchParams.get('conversation_id') || item.conversation_id === url.searchParams.get('conversation_id')));
      data = { items: selected, total: selected.length, facets: {}, warnings: [] };
    } else if (pathname === '/config/conversations/data') {
      const payload = request.postDataJSON();
      assert.equal(payload.action, undefined, '浏览器验收不修改原记录');
      data = { ok: true, ...payload, source: 'storage', revision: 'v1', total: payload.layer === 'context' ? 1 : 0, items: payload.layer === 'context' ? [{ index: 0, role: 'user', content: '测试上下文原文' }] : [], backups: [] };
    } else if (pathname === '/config/conversations/archive') data = { items: [], total: 0, warnings: [], conversation_kinds: [], self_ids: [] };
    else if (pathname === '/config/platforms') data = { ok: true, platforms: [{ id: 'qq', type: 'onebot', enable: true, settings: {} }], adapter_types: [], revision: 'v1' };
    else if (pathname === '/api/platforms/wake/diagnostics') data = { items: [], total: 0 };
    else if (pathname === '/config/snapshot') {
      const resource = url.searchParams.get('resource');
      data = { data: {}, versions: { [resource]: { change_token: 'v1', state: 'available' } } };
    } else data = responseFor(request, fixture);
    await route.fulfill(jsonResponse(data, origin));
  } catch (error) {
    errors.push(String(error));
    await route.fulfill({ ...jsonResponse({ error: String(error) }, origin), status: 500 });
  }
};
for (const port of [19870, 19871, 19872]) await context.route(`http://127.0.0.1:${port}/**`, handle);
await installWebSocketMocks(page, new Set());
try {
  await page.goto(origin + '/conversations?platform=qq&conversation=group-root');
  await page.getByText('测试上下文原文', { exact: true }).waitFor();
  await page.getByRole('combobox', { name: '上下文范围' }).selectOption('group-extra');
  assert.equal(await page.getByRole('tab', { name: 'Chat 展示历史' }).count(), 0);
  await page.getByRole('link', { name: '查看平台消息档案' }).click();
  assert.equal(new URL(page.url()).searchParams.get('platform'), 'qq');
  assert.equal(new URL(page.url()).searchParams.get('view'), 'archive');
  checks.push('QQ 仅显示模型上下文, 档案链接保留平台');
  await page.goto(origin + '/conversations?platform=chat&conversation=chat-root');
  await page.getByText('测试上下文原文', { exact: true }).waitFor();
  const history = page.getByRole('tab', { name: 'Chat 展示历史' });
  await history.click();
  await page.getByText('暂无独立展示历史', { exact: true }).waitFor();
  await history.press('ArrowLeft');
  assert.equal(await page.getByRole('tab', { name: '模型上下文' }).getAttribute('aria-selected'), 'true');
  await page.getByRole('button', { name: '编辑第 1 项', exact: true }).click();
  await page.getByRole('textbox', { name: '消息内容', exact: true }).fill('未保存的编辑');
  page.once('dialog', (dialog) => dialog.dismiss());
  await page.getByRole('dialog').getByRole('button', { name: '取消', exact: true }).click();
  assert.equal(await page.getByRole('textbox', { name: '消息内容', exact: true }).inputValue(), '未保存的编辑');
  page.once('dialog', (dialog) => dialog.accept());
  await page.getByRole('dialog').getByRole('button', { name: '取消', exact: true }).click();
  checks.push('Chat 标签切换, 空历史, 键盘导航和脏编辑保护');
  await page.getByRole('combobox', { name: '记录平台' }).selectOption('');
  await page.getByRole('button', { name: /QQ 测试群/ }).click();
  await page.getByText('测试上下文原文', { exact: true }).waitFor();
  assert.equal(await page.getByRole('combobox', { name: '上下文范围' }).inputValue(), 'group-context');
  checks.push('切换记录后重置上下文范围和标签');
  await page.goto(origin + '/platforms');
  await page.getByRole('link', { name: '审批中心 · 待审批 3', exact: true }).waitFor();
  assert.ok(reads.includes('/api/platform-actions/summary'));
  assert.equal(reads.filter((url) => /\/groups\/.*\/actions$/.test(url)).length, 0);
  await page.getByRole('link', { name: '审批中心 · 待审批 3', exact: true }).click();
  await page.getByRole('heading', { name: '拒绝群 888 的入群邀请', exact: true }).waitFor();
  assert.equal(await page.getByRole('button', { name: '批准执行', exact: true }).count(), 3);
  checks.push('平台页只读摘要, 三类审批同屏且邀请不重复');
  const invitation = page.locator('.glass-card').filter({ has: page.getByRole('heading', { name: '拒绝群 888 的入群邀请', exact: true }) });
  await invitation.getByRole('button', { name: '批准执行', exact: true }).click();
  await page.getByRole('dialog').getByText(/批准执行: 拒绝群/).waitFor();
  await page.getByRole('dialog').getByRole('button', { name: '确认', exact: true }).click();
  await page.getByText(/重新读取: 结果未确认/).waitFor();
  assert.equal(decisions.length, 1);
  assert.ok(decisions[0].path.includes('/account-requests/group-invites/'));
  assert.equal(decisions[0].body.approve, true);
  checks.push('批准原拒绝邀请动作, 原接口执行且读回 unknown');
  await page.getByRole('combobox', { name: '审批状态' }).selectOption('unknown');
  await page.getByRole('heading', { name: '拒绝群 888 的入群邀请', exact: true }).waitFor();
  assert.equal(await page.getByRole('button', { name: '批准执行', exact: true }).count(), 0);
  await page.getByRole('combobox', { name: '审批状态' }).selectOption('pending');
  await page.getByRole('heading', { name: '删除好友 42', exact: true }).waitFor();
  failDecision = true;
  await page.locator('.glass-card').filter({ has: page.getByRole('heading', { name: '删除好友 42', exact: true }) }).getByRole('button', { name: '批准执行', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: '确认', exact: true }).click();
  await page.getByText(/重新读取: 已成功/).waitFor();
  assert.equal(decisions.length, 2);
  failDecision = false;
  checks.push('响应丢失后按原动作查询, 不重放写请求');
  await page.getByRole('combobox', { name: '审批账号' }).selectOption('old');
  await page.getByText('历史账号记录仅供查看', { exact: true }).waitFor();
  assert.equal(await page.getByRole('button', { name: '批准执行', exact: true }).isDisabled(), true);
  await page.reload();
  await page.getByText('历史账号记录仅供查看', { exact: true }).waitFor();
  assert.equal(await page.getByRole('combobox', { name: '审批账号' }).inputValue(), 'old');
  checks.push('历史账号只读, URL 刷新保留筛选');
  unavailable = true;
  await page.getByRole('button', { name: '刷新审批', exact: true }).click();
  await page.getByRole('alert').filter({ hasText: '后端不可用' }).waitFor();
  checks.push('服务读取失败有明确提示');
  unavailable = false; partial = true;
  await page.getByRole('button', { name: '刷新审批', exact: true }).click();
  await page.getByRole('alert').filter({ hasText: 'broken: 动作记录读取失败' }).waitFor();
  await page.getByText('历史账号记录仅供查看', { exact: true }).waitFor();
  checks.push('部分平台失败仍保留可用记录和覆盖警告');
  backendDown = true;
  await page.routeWebSocket('ws://127.0.0.1:19870/ws/status**', (socket) => socket.send(JSON.stringify({ type: 'status', data: { running: false, adapters: {} } })));
  await page.reload();
  await page.getByText('后端未运行, 暂不可读取或审批', { exact: true }).waitFor();
  assert.equal(await page.getByRole('button', { name: '批准执行', exact: true }).count(), 0);
  checks.push('后端离线时不显示可执行审批');
  assert.deepEqual(errors, []);
  const results = path.join(directory, 'results/platform-approvals-2026-10-23');
  await mkdir(results, { recursive: true });
  await writeFile(path.join(results, 'browser.json'), JSON.stringify({ browser: browser.version(), checks, decisions: decisions.map((item) => ({ ...item })), page_errors: errors }, null, 2) + '\n');
  console.log(JSON.stringify({ checks, browser: browser.version() }));
} finally {
  await browser.close();
  await new Promise((resolve) => server.close(resolve));
}
