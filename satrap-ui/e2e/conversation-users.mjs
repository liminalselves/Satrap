import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { mkdir } from 'node:fs/promises';
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
  page.setDefaultTimeout(10000);
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  page.on('console', (message) => { if (message.text().includes('only supports one blocker')) errors.push(message.text()); });
  const descriptors = [{ id: 'future-instance', type: 'future', label: '未来实例', type_label: '新增平台' }, { id: 'stored-instance', type: 'former', label: '已停用实例', type_label: '已保存类型' }];
  const makeUser = (platform, id, nickname, related = []) => ({ platform_id: platform, platform_label: descriptors.find((item) => item.id === platform).label, platform_type: platform === 'future-instance' ? 'future' : 'former', user_id: id, user_nickname: nickname, user_platform: '来源', has_profile: true, revision: 'v1', user_session: related.filter((item) => item.manual).map((item) => item.conversation_id), conversation_count: related.length, conversations: related });
  const related = { conversation_id: 'same-conversation', title: '路由对话', exists: true, manual: true, routed: true };
  let users = [makeUser('future-instance', 'same-user', '未来用户', [related]), makeUser('stored-instance', 'same-user', '保存用户', [{ ...related, title: '另一个平台对话' }]), makeUser('future-instance', 'empty', '无对话用户')];
  const requests = [];
  let failSave = false;
  let revision = 1;
  const cors = { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true', 'Access-Control-Allow-Headers': 'Content-Type', 'Access-Control-Allow-Methods': 'GET,POST,OPTIONS' };
  await page.route('**/*', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const pathname = url.pathname;
    if (!pathname.startsWith('/config') && !pathname.startsWith('/api') && pathname !== '/status' && pathname !== '/auth/session') return route.continue();
    if (request.method() === 'OPTIONS') return route.fulfill({ status: 204, headers: cors });
    if (pathname === '/config/conversations/platforms') return route.fulfill({ json: { platforms: descriptors.map((item) => item.id), items: descriptors }, headers: cors });
    if (pathname === '/config/conversations/users') {
      if (request.method() === 'GET') {
        const platform = url.searchParams.get('platform_id');
        const type = url.searchParams.get('platform_type');
        const id = url.searchParams.get('user_id');
        if (id) return route.fulfill({ json: { user: users.find((item) => item.platform_id === platform && item.user_id === id) || null }, headers: cors });
        const query = url.searchParams.get('q') || '';
        const items = users.filter((item) => (!platform || item.platform_id === platform) && (!type || item.platform_type === type) && `${item.user_id} ${item.user_nickname}`.includes(query));
        return route.fulfill({ json: { items, total: items.length, new_revision: 'missing' }, headers: cors });
      }
      const payload = request.postDataJSON(); requests.push(payload);
      if (failSave) return route.fulfill({ status: 409, json: { error: '资料已变化，保存测试失败' }, headers: cors });
      let user = users.find((item) => item.platform_id === payload.platform_id && item.user_id === payload.user_id);
      if (payload.action === 'create') { assert.equal(payload.expected_revision, 'missing'); user = makeUser(payload.platform_id, payload.user_id, payload.nickname); users.push(user); }
      else {
        assert.equal(payload.expected_revision, user.revision);
        if (payload.action === 'update') user.user_nickname = payload.nickname;
        if (payload.action === 'dissociate') { user.user_session = []; user.conversations = user.conversations.map((item) => ({ ...item, manual: false })); }
        if (payload.action === 'delete') { user.has_profile = false; user.user_nickname = ''; user.user_session = []; user.conversations = user.conversations.filter((item) => item.routed).map((item) => ({ ...item, manual: false })); }
      }
      user.revision = `v${++revision}`;
      return route.fulfill({ json: { ok: true, user }, headers: cors });
    }
    if (pathname === '/config/conversations') return route.fulfill({ json: { items: [{ platform_id: url.searchParams.get('platform_id'), conversation_id: 'same-conversation', context_ids: ['same-conversation'], title: '对话详情', message_count: 1, history_count: 0, supports_history: false }], total: 1 }, headers: cors });
    if (pathname === '/config/conversations/data') {
      const payload = request.postDataJSON(); requests.push(payload);
      return route.fulfill({ json: { total: 1, items: [{ index: 0, role: 'user', content: `来自 ${payload.platform_id} 的上下文` }], revision: 'context-v1', backups: [] }, headers: cors });
    }
    return route.fulfill({ json: pathname === '/status' || pathname === '/api/health' ? { running: false, adapters: {} } : {}, headers: cors });
  });
  await page.goto(`${origin}/users?platform_id=future-instance&user_id=same-user`);
  await page.getByRole('button', { name: '编辑用户资料', exact: true }).waitFor();
  assert.match(page.url(), /conversations\?/);
  assert.equal(await page.getByRole('link', { name: '用户管理', exact: true }).count(), 0);
  assert.equal(await page.getByLabel('记录平台').inputValue(), 'future-instance');
  await page.getByRole('button', { name: '查看对话', exact: true }).click();
  await page.getByText('来自 future-instance 的上下文', { exact: true }).waitFor();
  assert.equal(requests.at(-1).platform_id, 'future-instance');
  assert.deepEqual(errors, []);
  await page.getByRole('button', { name: '编辑用户资料', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByLabel('本地昵称').fill('修改昵称');
  page.once('dialog', (message) => message.dismiss());
  await dialog.getByRole('button', { name: '取消', exact: true }).click();
  assert.equal(await dialog.getByLabel('本地昵称').inputValue(), '修改昵称');
  failSave = true;
  await dialog.getByRole('button', { name: '保存资料', exact: true }).click();
  await dialog.getByText('资料已变化，保存测试失败', { exact: true }).waitFor();
  assert.equal(await dialog.getByLabel('本地昵称').inputValue(), '修改昵称');
  failSave = false;
  let unexpectedConfirm = 0;
  const unexpected = (message) => { unexpectedConfirm++; void message.dismiss(); };
  page.on('dialog', unexpected);
  await dialog.getByRole('button', { name: '保存资料', exact: true }).click();
  await dialog.waitFor({ state: 'hidden' });
  await page.getByRole('heading', { name: '修改昵称', exact: true }).waitFor();
  assert.equal(unexpectedConfirm, 0);
  page.off('dialog', unexpected);
  await page.getByLabel('记录平台').selectOption('stored-instance');
  await page.getByRole('button').filter({ has: page.getByText('保存用户', { exact: true }) }).click();
  await page.getByRole('heading', { name: '保存用户', exact: true }).waitFor();
  await page.getByRole('button', { name: '查看对话', exact: true }).click();
  await page.getByText('来自 stored-instance 的上下文', { exact: true }).waitFor();
  await page.getByLabel('平台类型').selectOption('future');
  await page.getByRole('button').filter({ has: page.getByText('无对话用户', { exact: true }) }).click();
  await page.getByText('此用户暂无关联对话', { exact: true }).waitFor();
  await page.getByRole('button', { name: '添加用户资料', exact: true }).click();
  await dialog.getByLabel('资料用户 ID').fill('created');
  await dialog.getByLabel('本地昵称').fill('新资料');
  await dialog.getByRole('button', { name: '保存资料', exact: true }).click();
  await dialog.waitFor({ state: 'hidden' });
  await page.getByRole('heading', { name: '新资料', exact: true }).waitFor();
  await page.getByRole('button', { name: '重置筛选', exact: true }).click();
  assert.match(page.url(), /view=users/);
  await page.getByRole('button').filter({ has: page.getByText('修改昵称', { exact: true }) }).click();
  await page.getByText('高级资料操作', { exact: true }).click();
  page.once('dialog', (message) => message.accept());
  await page.getByRole('button', { name: '移除关联', exact: true }).click();
  await page.waitForFunction(() => !document.querySelector('button')?.disabled && ![...document.querySelectorAll('button')].some((item) => item.textContent === '移除关联'));
  assert.equal(users[0].conversations[0].routed, true);
  await page.getByRole('button', { name: '编辑用户资料', exact: true }).waitFor();
  await page.getByText('高级资料操作', { exact: true }).click();
  page.once('dialog', (message) => { assert.match(message.message(), /对话、消息和平台路由会保留/); return message.accept(); });
  await page.getByRole('button', { name: '删除用户资料', exact: true }).click();
  await page.getByText('仅存在路由记录，尚未保存用户资料', { exact: true }).waitFor();
  assert.equal(users[0].conversations[0].routed, true);
  assert.equal(requests.some((item) => item.action && item.layer), false);
  const output = path.join(root, '..', '.satrap', 'conversation-users-preview');
  await mkdir(output, { recursive: true });
  await page.screenshot({ path: path.join(output, 'desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth), false);
  await page.screenshot({ path: path.join(output, 'mobile.png'), fullPage: true });
  assert.deepEqual(errors, []);
  console.log('用户视图浏览器验证通过');
} finally {
  await browser?.close();
  await server.close();
}
