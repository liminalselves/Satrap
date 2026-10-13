import assert from "node:assert/strict";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { mkdir } from "node:fs/promises";
import { finishConfirmation } from "./confirmation.mjs";
import { runSuite, mockConfigVersions } from './runtime.mjs';
import { pathToFileURL } from 'node:url';

async function conversations(runtime) {







  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  const server = await runtime.vite({ root, server: { host: '127.0.0.1', port: 0 } });
  let browser;
  try {
    await runtime.listen(server);
    const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
    const cors = { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true', 'Access-Control-Allow-Headers': 'Content-Type', 'Access-Control-Allow-Methods': 'GET,POST,OPTIONS' };
    browser = await runtime.browser;
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', (error) => { errors.push(error.message); console.error(error.stack); });
    let contexts = [{ role: 'system', content: '原始系统提示词' }, { role: 'user', content: '模型原始输入' }, { role: 'assistant', content: '模型原始回复', reasoning_content: '思考内容' }];
    let history = [{ id: 1, user_input: '展示原始输入', answer: '展示原始回复', created_at: 1, variants: [{ answer: '其他回复版本' }], tool_call_records: [{ name: 'probe' }] }];
    const backups = { context: [], history: [] };
    let failSave = false;
    const requests = [];
    const records = () => [{ conversation_id: 'conv-1', title: '测试对话', context_ids: ['conv-1_main'], message_count: contexts.length, history_count: history.length, platform_id: 'chat', supports_history: true }];
    const revision = (layer) => JSON.stringify(layer === 'context' ? contexts : history);
    await page.route('**/*', async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      const pathname = url.pathname;
      if (!pathname.startsWith('/config') && !pathname.startsWith('/api') && pathname !== '/status' && pathname !== '/auth/session') return route.continue();
      if (request.method() === 'OPTIONS') return route.fulfill({ status: 204, headers: cors });
      if (pathname === '/config/conversations/platforms') return route.fulfill({ json: { platforms: ['chat', 'local', 'future-instance'], items: [{ id: 'chat', type: 'chat', type_label: 'Chat', label: 'Chat', supports_history: true }, { id: 'local', type: 'local', type_label: '本地', label: '本地', supports_history: false }, { id: 'future-instance', type: 'future-type', type_label: '新增平台', label: '未来实例', supports_history: false }] }, headers: cors });
      if (pathname === '/config/conversations') {
        const future = { conversation_id: 'conv-1', title: '新增平台频道', context_ids: ['conv-1_main'], message_count: 3, history_count: 0, platform_id: 'future-instance', supports_history: false, facets: { channel: ['channel-1'] }, facet_labels: { 'channel:channel-1': '自定义频道' } };
        const items = url.searchParams.get('platform_type') === 'future-type' || url.searchParams.get('platform_id') === 'future-instance' ? [future] : url.searchParams.get('scope') === 'all' ? [...records(), future] : url.searchParams.get('platform_id') === 'chat' ? records() : [];
        return route.fulfill({ json: { items, total: items.length, facet_names: { channel: '频道' }, facets: { kind: [{ value: 'session', label: '会话' }, { value: 'legacy_child', label: '旧子代理记录' }], channel: [{ value: 'channel-1', label: '自定义频道' }] } }, headers: cors });
      }
      if (pathname === '/config/conversations/data') {
        const body = request.postDataJSON();
        requests.push(body);
        const { layer } = body;
        if (body.action) {
          assert.equal(body.expected_revision, revision(layer));
          if (failSave) return route.fulfill({ status: 500, json: { error: '保存测试失败' }, headers: cors });
          const value = structuredClone(layer === 'context' ? contexts : history);
          const backup = { id: `backup-${backups[layer].length}`, layer, reason: body.action, created_at: Date.now() / 1000, value };
          if (body.action === 'edit') {
            if (layer === 'context') contexts[body.index] = { ...contexts[body.index], content: body.content, reasoning_content: body.reasoning_content };
            else history[body.index] = { ...history[body.index], user_input: body.user_input, answer: body.answer, thinking: body.thinking };
          } else if (body.action === 'clear') {
            if (layer === 'context') contexts = body.keep_system ? contexts.filter((item) => item.role === 'system') : [];
            else history = [];
          } else if (body.action === 'delete') {
            if (layer === 'context') contexts.splice(body.index, 1);
            else history.splice(body.index, 1);
          } else if (body.action === 'restore') {
            const saved = backups[layer].find((item) => item.id === body.backup_id);
            if (layer === 'context') contexts = structuredClone(saved.value);
            else history = structuredClone(saved.value);
          }
          backups[layer].unshift(backup);
        }
        const value = layer === 'context' ? contexts : history;
        return route.fulfill({ json: { ok: true, conversation_id: body.conversation_id, layer, revision: revision(layer), source: 'memory', total: value.length, items: value.map((item, index) => ({ ...item, index })), backups: backups[layer].map(({ value, ...rest }) => rest), saved: !!body.action }, headers: cors });
      }
      const responses = {
        '/status': { running: false }, '/api/health': { running: false, adapters: {} },
        '/config': { backend: { platforms: [] }, models: {} }, '/config/models': {}, '/config/models/llm': {},
        '/config/session-classes': { configs: {} }, '/config/edictum/sessions': {}, '/config/edictum/types': { types: [] },
        '/config/edictum/plugins': { plugins: [] }, '/config/platforms': { platforms: [] },
        '/config/session-instances': { sessions: [] }, '/api/checkpoints': { checkpoints: [], branches: [] }, '/api/checkpoint/audit': { mutations: [] },
      };
      return route.fulfill({ json: responses[pathname] || {}, headers: cors });
    });
    await mockConfigVersions(page, origin, () => JSON.stringify({revision}));
  await page.goto(`${origin}/conversations?platform=chat&conversation=conv-1`);
    await page.getByText('原始系统提示词', { exact: true }).waitFor();
    assert.equal(await page.getByLabel('上下文范围').inputValue(), 'conv-1_main');
    await page.getByRole('button', { name: '编辑第 2 项' }).click();
    const dialog = page.getByRole('dialog');
    await dialog.getByLabel('消息内容', { exact: true }).fill('修改后的模型输入');
    failSave = true;
    await dialog.getByRole('button', { name: '保存修改' }).click();
    await dialog.getByText('保存测试失败', { exact: true }).waitFor();
    assert.equal(await dialog.getByLabel('消息内容', { exact: true }).inputValue(), '修改后的模型输入');
    failSave = false;
    await dialog.getByRole('button', { name: '保存修改' }).click();
    await dialog.waitFor({ state: 'hidden' });
    assert.equal(contexts[1].content, '修改后的模型输入');
    assert.equal(history[0].user_input, '展示原始输入');
    await page.getByRole('button', { name: '编辑第 2 项' }).click();
    await dialog.getByLabel('消息内容', { exact: true }).fill('未保存草稿');
    await page.waitForTimeout(100);
    let action = 'dismiss';
    let confirms = 0;
    const native = (item) => { confirms++; void item[action](); };
    page.on('dialog', native);
    await page.locator('aside a[href="/agents"]').evaluate((element) => element.click());
    await page.waitForTimeout(100);
    assert.ok(page.url().includes('/conversations?'));
    assert.equal(await dialog.getByLabel('消息内容', { exact: true }).inputValue(), '未保存草稿');
    await dialog.getByRole('button', { name: '取消', exact: true }).click();
    await page.waitForTimeout(100);
    assert.equal(await dialog.isVisible(), true);
    action = 'accept';
    await dialog.getByRole('button', { name: '取消', exact: true }).click();
    await dialog.waitFor({ state: 'hidden' });
    assert.ok(confirms >= 3);
    await page.getByRole('button', { name: 'Chat 展示历史', exact: true }).click();
    await page.getByText('用户：展示原始输入', { exact: true }).waitFor();
    await page.getByRole('button', { name: '编辑第 1 项' }).click();
    await dialog.getByLabel('用户输入', { exact: true }).fill('只改展示输入');
    await dialog.getByLabel('助手回复', { exact: true }).fill('只改展示回复');
    await dialog.getByRole('button', { name: '保存修改' }).click();
    await dialog.waitFor({ state: 'hidden' });
    assert.equal(history[0].answer, '只改展示回复');
    assert.equal(contexts[1].content, '修改后的模型输入');
    await page.getByRole('button', { name: '删除第 1 项' }).click();
    await page.getByRole('dialog', { name: '删除历史轮次' }).getByRole('button', { name: '确认', exact: true }).click();
    await page.getByText('暂无独立展示历史', { exact: true }).waitFor();
    await page.getByText('修改备份与恢复 (2)', { exact: true }).click();
    await page.getByRole('button', { name: '恢复备份', exact: true }).first().click();
    await page.getByRole('dialog', { name: '恢复修改备份' }).getByRole('button', { name: '确认', exact: true }).click();
    await page.getByText('用户：只改展示输入', { exact: true }).waitFor();
    await page.getByRole('button', { name: '上下文', exact: true }).click();
    await page.getByRole('button', { name: '清空上下文', exact: true }).click();
    await page.getByRole('dialog', { name: '清空上下文' }).getByRole('button', { name: '确认', exact: true }).click();
    await page.getByText('共 1 条消息', { exact: false }).waitFor();
    assert.deepEqual(contexts, [{ role: 'system', content: '原始系统提示词' }]);
    assert.equal(history[0].answer, '只改展示回复');
    page.off('dialog', native);
    const output = path.join(root, '..', '.satrap', 'conversation-data-preview');
    await mkdir(output, { recursive: true });
    await page.locator('main').evaluate((element) => { element.scrollTop = 0; });
    await page.screenshot({ path: path.join(output, 'desktop.png'), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.locator('main').evaluate((element) => { element.scrollTop = 0; });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.screenshot({ path: path.join(output, 'mobile.png'), fullPage: true });
    await page.goto(`${origin}/conversations`);
    await page.getByRole('button').filter({ hasText: '新增平台频道' }).waitFor();
    await page.getByLabel('平台类型', { exact: true }).selectOption('future-type');
    await page.getByRole('button').filter({ hasText: '新增平台频道' }).click();
    await page.waitForURL('**/conversations?**record_platform=future-instance**');
    assert.equal(await page.getByRole('button', { name: 'Chat 展示历史', exact: true }).isDisabled(), true);
    await page.getByLabel('筛选频道', { exact: true }).selectOption('channel-1');
    assert.ok(page.url().includes('f.channel=channel-1'));
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.goto(`${origin}/sessions?edictum=fixture`);
    await page.waitForURL('**/agents?edictum=fixture');
    await page.getByRole('heading', { name: 'Agent 配置', exact: true }).waitFor({ timeout: 10000 }).catch(async (error) => { console.error(await page.locator('body').innerText()); throw error; });
    assert.equal(await page.getByRole('heading', { name: '会话实例', exact: true }).count(), 0);
    await page.goto(`${origin}/conversations/instances`);
    await page.getByRole('heading', { name: '对话实例', exact: true }).waitFor();
    await page.goto(`${origin}/checkpoints?platform=chat&conversation=conv-1_main`);
    await page.waitForURL('**/conversations/versions?platform=chat&conversation=conv-1_main');
    await page.getByRole('heading', { name: '版本与恢复', exact: true }).waitFor();
    assert.equal(await page.getByPlaceholder('输入对话 ID (如 conv-xxx)').inputValue(), 'conv-1_main');
    assert.deepEqual(errors, []);
    assert.ok(requests.some((item) => item.conversation_id === 'conv-1_main' && item.layer === 'context' && item.action === 'edit'));
    assert.ok(requests.some((item) => item.conversation_id === 'conv-1' && item.layer === 'history' && item.action === 'edit'));
    console.log('PASS: 对话列表、深链、工作流上下文、分别编辑、失败保留、离开确认、删除与恢复、清空保留提示词、手机布局和旧路由兼容');
  } finally {
    await runtime.closeContexts();
    await runtime.releaseServer(server);
  }

}

async function conversation_users(runtime) {








  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  const server = await runtime.vite({ root, server: { host: '127.0.0.1', port: 0 } });
  let browser;
  try {
    await runtime.listen(server);
    const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
    browser = await runtime.browser;
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
    await mockConfigVersions(page, origin, () => JSON.stringify({revision}));
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
    await page.getByRole('button', { name: '移除关联', exact: true }).click();
    await finishConfirmation(page);
    await page.waitForFunction(() => !document.querySelector('button')?.disabled && ![...document.querySelectorAll('button')].some((item) => item.textContent === '移除关联'));
    assert.equal(users[0].conversations[0].routed, true);
    await page.getByRole('button', { name: '编辑用户资料', exact: true }).waitFor();
    await page.getByText('高级资料操作', { exact: true }).click();
    await page.getByRole('button', { name: '删除用户资料', exact: true }).click();
    await finishConfirmation(page, true, /对话、消息和平台路由会保留/);
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
    await runtime.closeContexts();
    await runtime.releaseServer(server);
  }

}

async function platform_archives(runtime) {







  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  process.chdir(root);
  const server = await runtime.vite({ root, server: { host: '127.0.0.1', port: 0 } });
  let browser;
  try {
    await runtime.listen(server);
    const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
    const cors = { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true', 'Access-Control-Allow-Headers': 'Content-Type', 'Access-Control-Allow-Methods': 'GET,POST,OPTIONS' };
    browser = await runtime.browser;
    const page = await browser.newPage();
    const errors = [];
    const requests = [];
    page.on('pageerror', (error) => errors.push(error.message));
    const now = Date.now() / 1000;
    const records = ['bot-1', 'bot-2'].map((self, index) => ({ adapter_id: 'future', platform_id: 'future', platform_type: 'future-type', type_label: '新增平台', self_id: self, conversation_kind: 'circle', conversation_kind_label: '圈子讨论', chat_id: '同一会话/中文', label: index ? '备用账号对话' : '主要账号对话', revision: 0, message_count: 2, last_message_at: now }));
    const message = (id, text, extra = {}) => ({ message_id: id, sender_id: 'member', nickname: '昵称', card: '群名片', message_time: now, received_at: now, time_source: 'platform', direction: 'inbound', text, components: [{ type: 'Plain' }], mentions: [], reply_to_message_id: null, media: [], status: 'active', source: 'platform_event', verified: true, truncated: false, ...extra });
    const states = {
      'bot-1': { revision: 0, backups: [], messages: [message('m2', '最新讨论', { reply_to_message_id: 'm1', mentions: ['target-a', 'target-b'] }), message('m1', '引用原文'), message('m0', '较早讨论')] },
      'bot-2': { revision: 0, backups: [], messages: [message('other', '备用账号独立记录')] },
    };
    let definitions = [{ id: 'future', type: 'future-type', session_provider: 'session_class', session_type: 'default', settings: { extension: 'kept', message_archive_retention_days: 30 } }];
    const definitionWrites = [];
    let conflict = false;
    let releaseHeld;
    let signalHeld;
    let holdNext = false;
    const held = new Promise((resolve) => { signalHeld = resolve; });
    await page.route('**/*', async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      const pathname = url.pathname;
      if (!pathname.startsWith('/config') && !pathname.startsWith('/api') && pathname !== '/status' && pathname !== '/auth/session') return route.continue();
      if (request.method() === 'OPTIONS') return route.fulfill({ status: 204, headers: cors });
      requests.push({ pathname, params: Object.fromEntries(url.searchParams), body: request.postData() ? request.postDataJSON() : undefined });
      if (pathname.startsWith('/config/platforms')) {
        if (request.method() === 'PUT') {
          const value = request.postDataJSON();
          definitionWrites.push(value);
          definitions = [value];
        }
        return route.fulfill({ json: { ok: true, platforms: definitions, revision: String(definitionWrites.length + 1), default_session_type: 'default', adapter_types: [{ type: 'future-type', status: 'available', display_name: '新增平台', conversation_kinds: { circle: '圈子讨论' } }] }, headers: cors });
      }
      if (pathname === '/config/conversations/platforms') return route.fulfill({ json: { platforms: ['future'], items: [{ id: 'future', type: 'future-type', type_label: '新增平台', label: '未来实例', supports_history: false }] }, headers: cors });
      if (pathname === '/config/conversations/archive') {
        const items = records.filter((record) => (!url.searchParams.get('self_id') || record.self_id === url.searchParams.get('self_id')) && (!url.searchParams.get('q') || record.label.includes(url.searchParams.get('q'))));
        return route.fulfill({ json: { items: items.map((item) => ({ ...item, message_count: states[item.self_id].messages.filter((message) => message.status === 'active').length, revision: states[item.self_id].revision })), total: items.length, self_ids: ['bot-1', 'bot-2'], conversation_kinds: [{ value: 'circle', label: '圈子讨论' }], warnings: [] }, headers: cors });
      }
      if (pathname === '/config/conversations/archive/data') {
        const body = request.postDataJSON();
        assert.equal(body.platform_id, 'future');
        assert.equal(body.conversation_kind, 'circle');
        assert.equal(body.chat_id, '同一会话/中文');
        const state = states[body.self_id];
        assert.ok(state);
        if (body.action === 'read') {
          const matches = state.messages.filter((item) => item.status === 'active' && (!body.keyword || item.text.includes(body.keyword)) && (!body.sender_id || item.sender_id === body.sender_id));
          const items = body.cursor ? matches.filter((item) => item.message_id === 'm0') : matches.filter((item) => item.message_id !== 'm0').reverse();
          const snapshot = { ok: true, items, revision: state.revision, scope: { adapter_id: 'future', self_id: body.self_id, conversation_kind: 'circle', chat_id: body.chat_id, label: records.find((item) => item.self_id === body.self_id).label }, retention_days: 7, backups: state.backups.map(({ backup_id, action, created_at, expires_at }) => ({ backup_id, action, created_at, expires_at })), coverage: { archived_from: now - 100, archived_to: now, complete: false, retention_days: 7 }, has_more: !body.cursor && !body.keyword && matches.some((item) => item.message_id === 'm0'), next_cursor: 'older-cursor', truncated: false };
          if (holdNext && body.self_id === 'bot-1') {
            holdNext = false;
            signalHeld();
            await new Promise((resolve) => { releaseHeld = resolve; });
          }
          return route.fulfill({ json: snapshot, headers: cors });
        }
        if (body.action === 'message') {
          const item = state.messages.find((item) => item.message_id === body.message_id);
          return route.fulfill({ status: item ? 200 : 404, json: item ? { ok: true, item } : { error: '消息未被采集' }, headers: cors });
        }
        if (conflict) return route.fulfill({ status: 409, json: { error: '档案已被其他管理操作修改，请刷新', code: 'revision_conflict' }, headers: cors });
        assert.equal(body.expected_revision, state.revision);
        if (body.action === 'restore') {
          const backup = state.backups.find((item) => item.backup_id === body.backup_id);
          assert.ok(backup);
          state.messages = backup.messages;
          state.backups = state.backups.filter((item) => item !== backup);
          return route.fulfill({ json: { ok: true, revision: ++state.revision, restored_count: backup.messages.length, skipped_count: 0 }, headers: cors });
        }
        assert.ok(body.action === 'delete' || body.action === 'clear');
        const backup = { backup_id: `backup-${state.revision}`, action: body.action, created_at: now, expires_at: now + 86400, messages: structuredClone(state.messages) };
        state.backups.unshift(backup);
        let count = 0;
        state.messages = state.messages.map((item) => {
          if (body.action === 'clear' || body.message_ids.includes(item.message_id)) {
            count += item.status === 'active' ? 1 : 0;
            return { ...item, text: '', sender_id: '', nickname: '', card: '', mentions: [], components: [], reply_to_message_id: null, status: 'deleted' };
          }
          return item;
        });
        return route.fulfill({ json: { ok: true, deleted_count: count, revision: ++state.revision, backup_id: backup.backup_id, expires_at: backup.expires_at }, headers: cors });
      }
      const responses = { '/status': { running: false }, '/api/health': { running: false, adapters: {} }, '/config': { backend: { platforms: [] }, models: {} }, '/config/models': {}, '/config/models/llm': {}, '/config/session-classes': { default: { class_path: 'example.Flow', enabled: true, params: {} } }, '/config/edictum/sessions': {}, '/config/edictum/types': { types: [] }, '/config/edictum/plugins': { plugins: [] }, '/config/session-instances': { sessions: [] } };
      return route.fulfill({ json: responses[pathname] || {}, headers: cors });
    });

    const deepUrl = `${origin}/conversations?view=archive&archive_platform=future&archive_self=bot-1&archive_kind=circle&archive_chat=${encodeURIComponent('同一会话/中文')}&conversation=old-context&record_platform=future`;
    await mockConfigVersions(page, origin, () => JSON.stringify({}));
  await page.goto(deepUrl);
    await page.getByText('最新讨论', { exact: true }).waitFor();
    assert.equal(await page.getByRole('button', { name: '编辑第 1 项' }).count(), 0);
    assert.ok(await page.getByLabel('档案对话类型').getByRole('option', { name: '圈子讨论', exact: true }).count());
    await page.getByLabel('档案对话类型').selectOption('circle');
    await page.getByText('主要账号对话', { exact: true }).click();
    await page.getByText('最新讨论', { exact: true }).waitFor();
    await page.getByRole('button', { name: '查看引用消息 m1' }).click();
    await page.getByRole('dialog').getByText('引用原文', { exact: true }).waitFor();
    await page.getByRole('dialog').locator('button').first().click();
    await page.getByRole('dialog').waitFor({ state: 'hidden' });
    await page.getByRole('button', { name: '较早消息' }).click();
    await page.getByText('较早讨论', { exact: true }).waitFor();
    await page.getByRole('button', { name: '较新消息' }).click();
    await page.getByLabel('档案消息关键词').fill('最新');
    await page.getByLabel('档案发送者 ID').fill('member');
    await page.getByLabel('档案开始时间').fill('2026-10-01T00:00');
    await page.getByLabel('档案结束时间').fill('2026-10-04T00:00');
    await page.getByRole('button', { name: '查询消息', exact: true }).click();
    await page.getByText('最新讨论', { exact: true }).waitFor();
    const filtered = requests.filter((request) => request.body?.keyword === '最新').at(-1).body;
    assert.equal(filtered.sender_id, 'member');
    assert.match(filtered.start_time, /Z$/);
    await page.getByLabel('选择档案消息 m2').check();
    await page.getByRole('button', { name: '删除所选档案（1）' }).click();
    await page.getByRole('dialog').getByRole('button', { name: '确认删除档案' }).click();
    await page.getByText('当前条件下没有可检索消息', { exact: true }).waitFor();
    assert.equal(states['bot-1'].messages.find((item) => item.message_id === 'm2').status, 'deleted');
    assert.equal(states['bot-2'].messages[0].text, '备用账号独立记录');
    await page.getByText('可恢复的档案删除（1）', { exact: true }).click();
    await page.getByRole('button', { name: '恢复此删除' }).click();
    await page.getByRole('dialog').getByRole('button', { name: '确认恢复' }).click();
    await page.getByText('最新讨论', { exact: true }).waitFor();
    conflict = true;
    await page.getByRole('button', { name: '清空此对话全部档案' }).click();
    await page.getByRole('dialog').getByText('这会清空此对话的全部档案，不受当前消息筛选影响。', { exact: true }).waitFor();
    await page.getByRole('dialog').getByRole('button', { name: '确认删除档案' }).click();
    await page.getByRole('dialog').getByRole('alert').waitFor();
    assert.equal(states['bot-1'].messages[0].status, 'active');
    await page.getByRole('dialog').getByRole('button', { name: '取消', exact: true }).click();
    conflict = false;
    await page.getByRole('button', { name: '清空此对话全部档案' }).click();
    await page.getByRole('dialog').getByRole('button', { name: '确认删除档案' }).click();
    await page.getByText('当前条件下没有可检索消息', { exact: true }).waitFor();
    assert.ok(states['bot-1'].messages.every((item) => item.status === 'deleted'));
    assert.ok(states['bot-2'].messages.every((item) => item.status === 'active'));

    states['bot-1'].messages = [message('late', '旧请求迟到内容')];
    holdNext = true;
    await page.goto(deepUrl);
    await held;
    await page.getByText('备用账号对话', { exact: true }).click();
    await page.getByText('备用账号独立记录', { exact: true }).waitFor();
    const returned = page.waitForResponse((response) => response.url().endsWith('/config/conversations/archive/data') && response.request().postDataJSON()?.self_id === 'bot-1');
    releaseHeld();
    await returned;
    assert.equal(await page.getByText('旧请求迟到内容', { exact: true }).count(), 0);
    assert.equal(requests.filter((request) => request.pathname === '/config/conversations' || request.pathname === '/config/conversations/data').length, 0);
    await page.setViewportSize({ width: 390, height: 844 });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1));
    const output = path.join(root, 'test-results', 'platform-archives');
    await mkdir(output, { recursive: true });
    await page.screenshot({ path: path.join(output, 'mobile.png'), fullPage: true });
    await page.getByRole('heading', { name: '备用账号对话', exact: true }).scrollIntoViewIfNeeded();
    await page.screenshot({ path: path.join(output, 'mobile-detail.png') });
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.getByRole('heading', { name: '对话记录', exact: true }).scrollIntoViewIfNeeded();
    await page.screenshot({ path: path.join(output, 'desktop.png') });
    await page.goto(`${origin}/platforms`);
    await page.getByTitle('编辑', { exact: true }).click();
    const settingsDialog = page.getByRole('dialog');
    assert.equal(await settingsDialog.getByLabel(/平台消息档案保留天数/).inputValue(), '30');
    assert.equal((await settingsDialog.getByLabel(/Settings JSON/).inputValue()).includes('message_archive_retention_days'), false);
    await settingsDialog.getByLabel(/平台消息档案保留天数/).fill('7');
    await settingsDialog.getByRole('button', { name: '保存修改' }).click();
    await settingsDialog.waitFor({ state: 'hidden' });
    assert.equal(definitionWrites[0].settings.message_archive_retention_days, 7);
    assert.equal(definitionWrites[0].settings.extension, 'kept');
    await page.getByTitle('编辑', { exact: true }).click();
    assert.equal(await settingsDialog.getByLabel(/平台消息档案保留天数/).inputValue(), '7');
    await settingsDialog.getByLabel(/平台消息档案保留天数/).fill('');
    await settingsDialog.getByRole('button', { name: '保存修改' }).click();
    await settingsDialog.waitFor({ state: 'hidden' });
    assert.equal('message_archive_retention_days' in definitionWrites[1].settings, false);
    assert.equal(definitionWrites[1].settings.extension, 'kept');
    assert.deepEqual(errors, []);
    console.log('平台档案浏览器检查通过: 动态类型, 查询分页, 引用, 删除/恢复/冲突, 账号隔离, 迟到请求, 窄屏布局与未来适配器保留期设置');
  } finally {
    await runtime.closeContexts();
    await runtime.releaseServer(server);
  }

}

export const scenarios = [
  ["conversations", conversations],
  ["conversation-users", conversation_users],
  ["platform-archives", platform_archives]
];
if (process.argv[1] && pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url) await runSuite(scenarios);
