import { createServer } from 'vite';
import { chromium } from 'playwright';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { mkdir } from 'node:fs/promises';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const server = await createServer({ root, server: { host: '127.0.0.1', port: 0 } });
let browser;
try {
  await server.listen();
  const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
  const now = Date.now() / 1000;
  const records = ['bot/a', 'bot/b'].map((self_id) => ({ platform_id: 'future', adapter_id: 'future', platform_type: 'future', type_label: '扩展平台', self_id,
    conversation_kind: 'group', conversation_kind_label: '群聊', chat_id: '群/中文', label: self_id === 'bot/a' ? '主要账号' : '备用账号', revision: 0, message_count: 1, last_message_at: now }));
  const memory = { memory_id: 'memory/a', kind: 'group_rule', owner_user_id: '', key: 'quiet_hours', title: '安静时段', content: '晚上十点后保持安静',
    revision: 1, state: 'active', origin: 'model', source_message_ids: ['source/a'], source_status: 'available', updated_at: new Date().toISOString() };
  const proposal = { proposal_id: 'proposal/a', operation: 'create', memory_id: null, base_revision: 0, actor_id: 'member:a', state: 'pending',
    created_at: now, expires_at: now + 86400, proposed: { title: '讨论约定', content: '讨论时附带出处', key: 'sources', source_message_ids: ['source/a'] } };
  const makeReminder = (id, state, text) => ({ reminder_id: id, revision: 1, state, creator_user_id: 'member:a', creator_kind: 'model', text,
    mention_user_ids: ['member:a'], due_at: new Date((now + 1800) * 1000).toISOString(), due_at_utc: new Date((now + 1800) * 1000).toISOString(),
    source_message_id: 'source/a', source_status: 'available', reason: state === 'paused' ? 'reminders_disabled' : '',
    created_at: now, paused_at: state === 'paused' ? now : null, settled_at: null, delivery: null });
  const reminders = [makeReminder('reminder/paused', 'paused', '暂停的检查提醒'), makeReminder('reminder/unknown', 'unknown', '结果未确认的提醒')];
  let memoryConflict = true;
  let createFailure = true;
  const memoryWrites = [];
  const reminderWrites = [];
  let preference;
  let cleanup;
  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ timezoneId: 'Asia/Shanghai' });
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  const cors = { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true', 'Access-Control-Allow-Headers': 'Content-Type', 'Access-Control-Allow-Methods': 'GET,DELETE,PATCH,POST,OPTIONS' };
  await page.route(origin + '/ui-config.json', (route) => route.fulfill({ json: { backend_api: 'http://127.0.0.1:19870', control_api: 'http://127.0.0.1:19871', chat_api: 'http://127.0.0.1:19872' } }));
  await page.routeWebSocket(/ws:\/\/127\.0\.0\.1:1987[012]\//, () => {});
  await page.route(/^http:\/\/127\.0\.0\.1:1987[012]\//, async (route) => {
    const request = route.request();
    if (request.method() === 'OPTIONS') return route.fulfill({ status: 204, headers: cors });
    const url = new URL(request.url());
    const pathname = decodeURIComponent(url.pathname);
    const reply = (json, status = 200) => route.fulfill({ status, json, headers: cors });
    if (pathname === '/config/conversations/platforms') return reply({ platforms: ['future'], items: [{ id: 'future', type: 'future', type_label: '扩展平台', label: '扩展平台实例', cold: false, capabilities: {} }] });
    if (pathname === '/config/conversations/archive') return reply({ items: records, total: 2, warnings: [], conversation_kinds: [{ value: 'group', label: '群聊' }], self_ids: records.map((record) => record.self_id) });
    if (pathname === '/config/conversations/archive/data') {
      const body = request.postDataJSON();
      assert.ok(['bot/a', 'bot/b'].includes(body.self_id));
      assert.equal(body.chat_id, '群/中文');
      if (body.action === 'message') return reply({ ok: true, item: { message_id: 'source/a', sender_id: 'member:a', nickname: '小明', card: '', message_time: now, text: '请记住晚上十点后保持安静', status: 'active', truncated: false } });
      if (body.action === 'clear') {
        cleanup = body;
        return reply({ ok: true, deleted_count: 1, deleted_memory_count: 2, cleared_memory_proposal_count: 1, cancelled_reminder_count: 3, sending_reminder_count: 1, revision: 1, expires_at: now + 86400 });
      }
      return reply({ ok: true, items: [], scope: { label: '主要账号' }, revision: 0, retention_days: 30, backups: [], coverage: { archived_from: null, archived_to: null }, has_more: false });
    }
    if (pathname.startsWith('/api/platforms/future/memory/')) {
      assert.equal(url.port, '19871');
      assert.equal(url.searchParams.get('conversation_kind'), 'group');
      assert.equal(url.searchParams.get('chat_id'), '群/中文');
      if (pathname.endsWith('/proposals')) return reply({ items: [proposal] });
      if (pathname.endsWith('/decision')) {
        const body = request.postDataJSON();
        assert.deepEqual(body, { approve: true, expected_revision: 0 });
        proposal.state = 'approved';
        return reply({ ok: true, status: 'approved' });
      }
      if (request.method() === 'PATCH') {
        const body = request.postDataJSON();
        memoryWrites.push(body);
        assert.equal(body.expected_revision, 1);
        assert.ok(body.idempotency_key);
        if (memoryConflict) return reply({ error: '记忆已变化，请刷新后重试', code: 'revision_conflict' }, 409);
        Object.assign(memory, { title: body.title, content: body.content, revision: 2 });
        return reply({ ok: true, status: 'saved' });
      }
      if (request.method() === 'POST') {
        const body = request.postDataJSON();
        assert.equal(body.owner_user_id, 'member:a');
        assert.equal(body.kind, 'member_preference');
        preference = { ...memory, memory_id: 'preference/a', ...body, revision: 1, source_status: 'operator', source_message_ids: [] };
        return reply({ ok: true, status: 'saved', memory: preference });
      }
      if (request.method() === 'DELETE') {
        const body = request.postDataJSON();
        assert.equal(body.expected_revision, 1);
        assert.ok(body.idempotency_key);
        preference = undefined;
        return reply({ ok: true, status: 'deleted' });
      }
      return reply({ ok: true, items: url.searchParams.get('self_id') === 'bot/b' ? [] : url.searchParams.get('kind') === 'member_preference' ? preference ? [preference] : [] : [memory], has_more: false, next_cursor: null });
    }
    if (pathname.startsWith('/api/platforms/future/group-chat/reminders')) {
      assert.equal(url.searchParams.get('conversation_kind'), 'group');
      assert.equal(url.searchParams.get('chat_id'), '群/中文');
      if (request.method() === 'POST') {
        const body = request.postDataJSON();
        reminderWrites.push({ path: pathname, body });
        if (pathname.endsWith('/cancel') || pathname.endsWith('/resume')) {
          const record = reminders.find((item) => pathname.includes(item.reminder_id));
          assert.ok(record);
          assert.equal(body.expected_revision, record.revision);
          assert.equal(url.port, pathname.endsWith('/cancel') ? '19871' : '19870');
          record.state = pathname.endsWith('/cancel') ? 'cancelled' : 'scheduled';
          record.reason = '';
          record.revision++;
          return reply({ ok: true, status: record.state, reminder: record });
        }
        assert.equal(url.port, '19870');
        if (createFailure) return reply({ error: '平台暂不可用，请稍后重试', code: 'unavailable' }, 503);
        const record = { ...makeReminder('reminder/new-' + reminders.length, 'scheduled', body.text), creator_kind: 'operator', source_message_id: '', source_status: 'operator' };
        reminders.push(record);
        return reply({ ok: true, status: 'created', reminder: record });
      }
      assert.equal(url.port, '19871');
      const state = url.searchParams.get('state');
      return reply({ items: url.searchParams.get('self_id') === 'bot/b' ? [] : reminders.filter((item) => !state || item.state === state), has_more: false, next_cursor: null });
    }
    const responses = { '/status': { running: true }, '/api/health': { running: true, adapters: {} }, '/config': { backend: { platforms: [] }, models: {} }, '/config/models': {}, '/config/models/llm': {}, '/config/session-classes': {}, '/config/edictum/sessions': {}, '/config/edictum/types': { types: [] }, '/config/edictum/plugins': { plugins: [] }, '/config/session-instances': { sessions: [] } };
    return reply(responses[pathname] || {});
  });
  await page.goto(`${origin}/conversations?view=archive&archive_platform=future&archive_self=${encodeURIComponent('bot/a')}&archive_kind=group&archive_chat=${encodeURIComponent('群/中文')}`);
  await page.getByRole('button', { name: '长期记忆', exact: true }).click();
  await page.getByText('晚上十点后保持安静', { exact: true }).waitFor();
  await page.getByRole('button', { name: '查看出处 source/a', exact: true }).click();
  await page.getByText('请记住晚上十点后保持安静', { exact: true }).waitFor();
  await page.keyboard.press('Escape');
  await page.getByRole('button', { name: '编辑记忆', exact: true }).click();
  await page.getByLabel(/^内容/).fill('晚上十一点后保持安静');
  await page.getByRole('button', { name: '保存记忆', exact: true }).click();
  await page.getByRole('alert').getByText('记忆已变化，请刷新后重试', { exact: true }).waitFor();
  assert.equal(await page.getByLabel(/^内容/).inputValue(), '晚上十一点后保持安静');
  await page.setViewportSize({ width: 390, height: 844 });
  await mkdir(path.join(root, 'test-results/group-chat'), { recursive: true });
  await page.screenshot({ path: path.join(root, 'test-results/group-chat/memory-draft-mobile.png'), fullPage: true });
  memoryConflict = false;
  await page.getByRole('button', { name: '保存记忆', exact: true }).click();
  await page.getByText('晚上十一点后保持安静', { exact: true }).waitFor();
  assert.equal(memoryWrites[0].idempotency_key, memoryWrites[1].idempotency_key);
  await page.getByRole('button', { name: '待审批与处理记录', exact: true }).click();
  await page.getByRole('button', { name: '审阅并批准', exact: true }).click();
  await page.getByRole('button', { name: '确认批准', exact: true }).click();
  await page.getByText('讨论约定 · 新增 · 已批准', { exact: true }).waitFor();
  await page.getByRole('button', { name: '成员偏好', exact: true }).click();
  await page.getByRole('button', { name: '新增记忆', exact: true }).click();
  await page.getByLabel(/^成员 ID/).last().fill('member:a');
  await page.getByLabel(/^用途键/).fill('preferred_name');
  await page.getByLabel(/^标题/).last().fill('成员称呼');
  await page.getByLabel(/^内容/).fill('称呼我为小明');
  await page.getByRole('button', { name: '保存记忆', exact: true }).click();
  await page.getByText('称呼我为小明', { exact: true }).waitFor();
  await page.getByText('人工维护，没有平台消息出处', { exact: true }).waitFor();
  await page.getByRole('button', { name: '删除记忆', exact: true }).click();
  await page.getByRole('button', { name: '确认删除记忆', exact: true }).click();
  await page.getByText('暂无匹配的长期记忆', { exact: true }).waitFor();
  await page.getByRole('button', { name: '提醒', exact: true }).click();
  await page.getByText('可能已经送达，重新创建可能导致重复发送。', { exact: true }).waitFor();
  assert.equal(await page.getByRole('button', { name: /重发/ }).count(), 0);
  await page.getByRole('button', { name: '创建提醒', exact: true }).click();
  await page.getByLabel('提醒正文', { exact: true }).fill('半小时后检查结果');
  await page.getByLabel('提醒等待数量', { exact: true }).fill('30');
  await page.getByLabel('提醒提及成员', { exact: true }).fill('member:a');
  await page.getByRole('button', { name: '安排提醒', exact: true }).click();
  await page.getByText('平台暂不可用，请稍后重试', { exact: true }).waitFor();
  assert.equal(await page.getByLabel('提醒正文', { exact: true }).inputValue(), '半小时后检查结果');
  createFailure = false;
  await page.getByRole('button', { name: '安排提醒', exact: true }).click();
  await page.getByText('半小时后检查结果', { exact: true }).waitFor();
  assert.equal(reminderWrites[0].body.idempotency_key, reminderWrites[1].body.idempotency_key);
  assert.equal(reminderWrites[1].body.after_seconds, 1800);
  assert.deepEqual(reminderWrites[1].body.mention_user_ids, ['member:a']);
  await page.getByText('半小时后检查结果', { exact: true }).locator('..').getByRole('button', { name: '取消提醒', exact: true }).click();
  await page.getByRole('button', { name: '确认取消', exact: true }).click();
  await page.getByText('任务状态：已取消', { exact: true }).waitFor();
  await page.keyboard.press('Escape');
  await page.getByRole('button', { name: '恢复提醒', exact: true }).click();
  await page.getByRole('button', { name: '确认恢复', exact: true }).click();
  await page.getByText('任务状态：待执行', { exact: true }).waitFor();
  await page.keyboard.press('Escape');
  await page.getByRole('button', { name: '创建提醒', exact: true }).click();
  await page.getByLabel('提醒正文', { exact: true }).fill('按本地时间检查');
  await page.getByLabel('提醒时间方式', { exact: true }).selectOption('absolute');
  await page.getByLabel('提醒日期时间', { exact: true }).fill('2026-10-06T09:30');
  await page.getByRole('button', { name: '安排提醒', exact: true }).click();
  await page.getByText('按本地时间检查', { exact: true }).waitFor();
  assert.equal(reminderWrites.at(-1).body.due_at, '2026-10-06T01:30:00.000Z');
  assert.equal(reminderWrites.at(-1).body.after_seconds, undefined);
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
  await page.screenshot({ path: path.join(root, 'test-results/group-chat/reminders-mobile.png'), fullPage: true });
  await page.getByRole('button', { name: '原始消息', exact: true }).click();
  await page.getByRole('button', { name: '清空此对话全部档案', exact: true }).click();
  const deleteMemory = page.getByLabel('同时永久删除本群全部长期记忆与记忆提案', { exact: true });
  const cancelReminder = page.getByLabel('同时取消本群尚未开始发送的提醒', { exact: true });
  assert.equal(await deleteMemory.isChecked(), false);
  assert.equal(await cancelReminder.isChecked(), false);
  await deleteMemory.check();
  await cancelReminder.check();
  await page.screenshot({ path: path.join(root, 'test-results/group-chat/cleanup-mobile.png'), fullPage: true });
  await page.getByRole('button', { name: '确认删除档案', exact: true }).click();
  await page.getByText(/另已永久删除 2 条长期记忆/).waitFor();
  assert.equal(cleanup.delete_memories, true);
  assert.equal(cleanup.cancel_reminders, true);
  await page.getByText(/1 个已经开始发送，无法保证撤回/).waitFor();
  await page.getByRole('button').filter({ hasText: '备用账号' }).click();
  await page.getByRole('button', { name: '提醒', exact: true }).click();
  await page.getByText('当前范围没有提醒', { exact: true }).waitFor();
  assert.deepEqual(errors, []);
  console.log('PASS: 记忆来源与审批, 版本冲突保留草稿, 提醒幂等重试, 时间转换, 取消与恢复, 未知结果, 冷/运行时接口和账号隔离, 窄屏');
} finally {
  if (browser) await browser.close();
  await server.close();
}
