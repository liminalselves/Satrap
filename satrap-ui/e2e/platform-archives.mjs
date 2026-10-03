import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { mkdir } from 'node:fs/promises';
import { chromium } from 'playwright';
import { createServer } from 'vite';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
process.chdir(root);
const server = await createServer({ root, server: { host: '127.0.0.1', port: 0 } });
let browser;
try {
  await server.listen();
  const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
  const cors = { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true', 'Access-Control-Allow-Headers': 'Content-Type', 'Access-Control-Allow-Methods': 'GET,POST,OPTIONS' };
  browser = await chromium.launch({ headless: true });
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
  await page.goto(deepUrl);
  await page.getByText('最新讨论', { exact: true }).waitFor();
  assert.equal(await page.getByRole('button', { name: '编辑第 1 项' }).count(), 0);
  assert.ok(await page.getByLabel('档案对话类型').getByRole('option', { name: '圈子讨论', exact: true }).count());
  await page.getByLabel('档案对话类型').selectOption('circle');
  await page.getByText('主要账号对话', { exact: true }).click();
  await page.getByText('最新讨论', { exact: true }).waitFor();
  await page.getByRole('button', { name: '查看引用消息 m1' }).click();
  await page.getByRole('dialog').getByText('引用原文', { exact: true }).waitFor();
  await page.keyboard.press('Escape');
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
  await browser?.close();
  await server.close();
}
