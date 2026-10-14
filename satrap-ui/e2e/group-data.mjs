import assert from "node:assert/strict";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { mkdir, readFile } from "node:fs/promises";
import { load } from "js-yaml";
import { runSuite, mockConfigVersions } from './runtime.mjs';
import { pathToFileURL } from 'node:url';

async function group_chat_media(runtime) {








  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  const meta = load(await readFile(path.join(root, '../satrap/expend/plugins/group_chat/meta.yaml'), 'utf8'));
  const plugin = { ...meta, source: 'builtin', usage_count: 0, edictum_configs: [], config_schema: {}, capabilities: { tools: meta.tools, skills: meta.skills, handlers: meta.handlers } };
  const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jGMsAAAAASUVORK5CYII=', 'base64');
  const sticker = { sticker_id: 'st/image:opaque', name: '收到', tags: ['确认'], collection: '常用', kind: 'image', content_revision: 1, enabled: true, mime_type: 'image/png', adapter_type: null, width: 1, height: 1, size_bytes: png.length };
  const now = Date.now() / 1000;
  const records = ['bot/a', 'bot/b'].map((self_id) => ({ platform_id: 'future', adapter_id: 'future', platform_type: 'future', type_label: '扩展平台', self_id, conversation_kind: 'circle', conversation_kind_label: '圈子讨论', chat_id: '群/中文', label: self_id === 'bot/a' ? '主要账号' : '备用账号', revision: 0, message_count: 1, last_message_at: now }));
  const server = await runtime.vite({ root, server: { host: '127.0.0.1', port: 0 } });
  let browser;
  try {
    await runtime.listen(server);
    const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
    browser = await runtime.browser;
    const page = await browser.newPage();
    const errors = [];
    const uploads = [];
    const changes = [];
    const settingsWrites = [];
    const settings = new Map();
    let items = [];
    let lostUpload = true;
    let editConflict = true;
    page.on('pageerror', (error) => errors.push(error.message));
    const cors = { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true', 'Access-Control-Allow-Headers': 'Content-Type', 'Access-Control-Allow-Methods': 'GET,POST,PUT,PATCH,DELETE,OPTIONS' };
    await page.route(origin + '/ui-config.json', (route) => route.fulfill({ json: { backend_api: 'http://127.0.0.1:19870', control_api: 'http://127.0.0.1:19871', chat_api: 'http://127.0.0.1:19872' } }));
    await page.routeWebSocket(/ws:\/\/127\.0\.0\.1:1987[012]\//, () => {});
    await page.route(/^http:\/\/127\.0\.0\.1:1987[012]\//, async (route) => {
      const request = route.request();
      if (request.method() === 'OPTIONS') return route.fulfill({ status: 204, headers: cors });
      const url = new URL(request.url());
      const pathname = decodeURIComponent(url.pathname);
      const respond = (json, status = 200) => route.fulfill({ json, status, headers: cors });
      if (pathname === '/config/versions') return respond({ ok: true, resources: Object.fromEntries(
        (url.searchParams.get('resources') || '').split(',').filter(Boolean).map((name) => [name, { state: 'available', change_token: name + ':stable' }]),
      ) });
      if (pathname === '/config/plugins') return respond({ plugins: [plugin] });
      if (pathname === '/api/group-chat/stickers' && request.method() === 'GET') return respond({ ok: true, items, has_more: false, next_cursor: null });
      if (pathname === '/api/group-chat/stickers/upload') {
        const body = request.postDataBuffer().toString('utf8');
        assert.match(request.headers()['content-type'], /multipart\/form-data/);
        assert.match(body, /name="file"/);
        assert.match(body, /收到/);
        const key = /name="idempotency_key"\r\n\r\n([^\r]+)/.exec(body)[1];
        uploads.push(key);
        items = [sticker];
        if (lostUpload) { lostUpload = false; return respond({ error: '保存结果暂时无法读取，请重试' }, 503); }
        return respond({ ok: true, sticker });
      }
      if (pathname === '/api/group-chat/stickers/st/image:opaque/preview') return respond({ preview: 'data:image/png;base64,' + png.toString('base64') });
      if (pathname === '/api/group-chat/stickers/st/image:opaque' && request.method() === 'PATCH') {
        const body = request.postDataJSON();
        changes.push(body);
        assert.equal(body.expected_revision, 1);
        if (editConflict) { editConflict = false; return respond({ error: '表情已被修改，请刷新后重试' }, 409); }
        items = [{ ...sticker, name: body.name, tags: body.tags, enabled: body.enabled, content_revision: 2 }];
        return respond({ ok: true, sticker: items[0] });
      }
      if (pathname === '/api/group-chat/stickers/st/image:opaque' && request.method() === 'DELETE') {
        assert.equal(request.postDataJSON().expected_revision, 2);
        items = [];
        return respond({ ok: true, deleted: true });
      }
      if (pathname === '/config/conversations/platforms') return respond({ platforms: ['future'], items: [{ id: 'future', type: 'future', type_label: '扩展平台', label: '扩展平台实例', capabilities: {} }] });
      if (pathname === '/config/conversations/archive') return respond({ items: records, total: 2, warnings: [], conversation_kinds: [{ value: 'circle', label: '圈子讨论' }], self_ids: records.map((record) => record.self_id) });
      if (pathname === '/config/conversations/archive/data') return respond({ ok: true, items: [], scope: { label: '主要账号' }, revision: 0, retention_days: 30, backups: [], coverage: { archived_from: null, archived_to: null }, has_more: false });
      if (pathname === '/api/platforms/future/group-chat/sticker-settings') {
        assert.equal(url.searchParams.get('conversation_kind'), 'circle');
        assert.equal(url.searchParams.get('chat_id'), '群/中文');
        const account = url.searchParams.get('self_id');
        const current = settings.get(account) || { collections: [], revision: 0 };
        if (request.method() === 'PUT') {
          const body = request.postDataJSON();
          settingsWrites.push({ account, ...body });
          assert.equal(body.expected_revision, current.revision);
          settings.set(account, { collections: body.collections, revision: current.revision + 1 });
        }
        return respond({ ok: true, available_collections: ['常用'], ...(settings.get(account) || current) });
      }
      const responses = { '/status': { running: false }, '/api/health': { running: false, adapters: {} }, '/config': { backend: { platforms: [] }, models: {} }, '/config/models': {}, '/config/models/llm': {}, '/config/session-classes': {}, '/config/edictum/sessions': {}, '/config/edictum/types': { types: [] }, '/config/edictum/plugins': { plugins: [] }, '/config/session-instances': { sessions: [] } };
      return respond(responses[pathname] || {});
    });
    await page.goto(origin + '/plugins/group_chat?tab=stickers');
    await page.getByRole('button', { name: '添加图片表情' }).click();
    await page.getByLabel('表情图片', { exact: true }).setInputFiles({ name: '收到.png', mimeType: 'image/png', buffer: png });
    await page.getByLabel('表情名称', { exact: true }).fill('收到');
    await page.getByLabel('表情标签', { exact: true }).fill('确认');
    await page.getByRole('button', { name: '保存表情', exact: true }).click();
    await page.getByText('保存结果暂时无法读取，请重试').waitFor();
    assert.equal(await page.getByLabel('表情名称', { exact: true }).inputValue(), '收到');
    await page.getByRole('button', { name: '保存表情', exact: true }).click();
    await page.getByText('表情已保存，请在对应群的「表情设置」中启用集合').waitFor();
    assert.equal(uploads.length, 2);
    assert.equal(uploads[0], uploads[1]);
    await page.getByRole('button', { name: '预览', exact: true }).click();
    await page.getByRole('img', { name: '收到', exact: true }).waitFor();
    await page.keyboard.press('Escape');
    await page.getByRole('button', { name: '编辑', exact: true }).click();
    await page.getByLabel('表情名称', { exact: true }).fill('明白了');
    await page.getByRole('button', { name: '保存表情', exact: true }).click();
    await page.getByText('表情已被修改，请刷新后重试').waitFor();
    assert.equal(await page.getByLabel('表情名称', { exact: true }).inputValue(), '明白了');
    await page.getByRole('button', { name: '保存表情', exact: true }).click();
    await page.getByText('明白了 · 已启用', { exact: true }).waitFor();
    assert.equal(changes[0].idempotency_key, changes[1].idempotency_key);
    await page.setViewportSize({ width: 390, height: 844 });
    await mkdir(path.join(root, 'test-results/group-chat'), { recursive: true });
    await page.screenshot({ path: path.join(root, 'test-results/group-chat/stickers-mobile.png'), fullPage: true });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.goto(`${origin}/conversations?view=archive&archive_platform=future&archive_self=${encodeURIComponent('bot/a')}&archive_kind=circle&archive_chat=${encodeURIComponent('群/中文')}`);
    await page.getByRole('button', { name: '表情设置', exact: true }).click();
    const choice = page.getByLabel('启用表情集合 常用', { exact: true });
    await choice.waitFor();
    assert.equal(await choice.isChecked(), false);
    await choice.check();
    await page.getByRole('button', { name: '保存群表情设置' }).click();
    await page.getByText('此对话的表情集合已保存，后续查询立即使用新设置').waitFor();
    assert.deepEqual(settingsWrites[0].collections, ['常用']);
    assert.equal(settingsWrites[0].account, 'bot/a');
    await page.getByRole('button').filter({ hasText: '备用账号' }).click();
    await page.getByRole('button', { name: '表情设置', exact: true }).click();
    await page.getByLabel('启用表情集合 常用', { exact: true }).waitFor();
    assert.equal(await page.getByLabel('启用表情集合 常用', { exact: true }).isChecked(), false);
    await page.setViewportSize({ width: 390, height: 844 });
    await page.getByRole('button', { name: '保存群表情设置' }).scrollIntoViewIfNeeded();
    await page.screenshot({ path: path.join(root, 'test-results/group-chat/sticker-settings-mobile.png'), fullPage: true });
    await page.screenshot({ path: path.join(root, 'test-results/group-chat/sticker-settings-panel-mobile.png') });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
    await page.goto(origin + '/plugins/group_chat?tab=stickers');
    await page.getByRole('button', { name: '删除', exact: true }).click();
    await page.getByRole('button', { name: '确认删除表情' }).click();
    await page.getByText('表情已删除，未提交的回复草稿将不能使用它').waitFor();
    await page.getByText('暂无表情，可以先添加图片').waitFor();
    assert.deepEqual(errors, []);
    console.log('group-chat media e2e passed: multipart, retry intent, preview, edit conflict, scoped collections, deletion and mobile');
  } finally {
    if (browser) await runtime.closeContexts();
    await runtime.releaseServer(server);
  }

}

async function group_chat_summaries(runtime) {







  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  const server = await runtime.vite({ root, server: { host: '127.0.0.1', port: 0 } });
  let browser;
  try {
    await runtime.listen(server);
    const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
    const now = Date.now() / 1000;
    const records = ['bot/a', 'bot/b'].map((self_id) => ({ platform_id: 'future', adapter_id: 'future', platform_type: 'future', type_label: '扩展平台', self_id,
      conversation_kind: 'circle', conversation_kind_label: '圈子讨论', chat_id: '群/中文', label: self_id === 'bot/a' ? '主要账号' : '备用账号', revision: 0, message_count: 1, last_message_at: now }));
    const summary = { schema_version: 1, summary_id: 'summary/a', title: '项目讨论摘要', revision: 1, state: 'active', created_at: now, expires_at: now + 86400,
      points: [{ text: '成员建议先进行联调，发布日期仍未确认', source_message_ids: ['source/a'] }],
      resolved_range: { start_time: '2026-10-04T09:00:00+08:00', end_time: '2026-10-04T18:00:00+08:00' },
      selection: { selected_count: 200, all_local_matches_selected: false, truncated: true, reasons: ['context_budget'] },
      archive_coverage: { platform_history_complete: false, archived_from: now - 60, archived_to: now, retention_days: 30 } };
    let deleted = false;
    let conflict = true;
    const writes = [];
    browser = await runtime.browser;
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', (error) => errors.push(error.message));
    const cors = { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true', 'Access-Control-Allow-Headers': 'Content-Type', 'Access-Control-Allow-Methods': 'GET,DELETE,POST,OPTIONS' };
    await page.route(origin + '/ui-config.json', (route) => route.fulfill({ json: { backend_api: 'http://127.0.0.1:19870', control_api: 'http://127.0.0.1:19871', chat_api: 'http://127.0.0.1:19872' } }));
    await page.routeWebSocket(/ws:\/\/127\.0\.0\.1:1987[012]\//, () => {});
    await page.route(/^http:\/\/127\.0\.0\.1:1987[012]\//, async (route) => {
      const request = route.request();
      if (request.method() === 'OPTIONS') return route.fulfill({ status: 204, headers: cors });
      const url = new URL(request.url());
      const pathname = decodeURIComponent(url.pathname);
      if (pathname === '/config/conversations/platforms') return route.fulfill({ json: { platforms: ['future'], items: [{ id: 'future', type: 'future', type_label: '扩展平台', label: '扩展平台实例', cold: false, capabilities: {} }] }, headers: cors });
      if (pathname === '/config/conversations/archive') return route.fulfill({ json: { items: records, total: 2, warnings: [], conversation_kinds: [{ value: 'circle', label: '圈子讨论' }], self_ids: records.map((record) => record.self_id) }, headers: cors });
      if (pathname === '/config/conversations/archive/data') {
        const body = request.postDataJSON();
        if (body.action === 'message') return route.fulfill({ json: { ok: true, item: { message_id: 'source/a', sender_id: 'member:a', nickname: '小明', card: '', message_time: now, text: '我们先联调，发布时间再讨论', status: 'active', truncated: false } }, headers: cors });
        return route.fulfill({ json: { ok: true, items: [], scope: { label: '主要账号' }, revision: 0, retention_days: 30, backups: [], coverage: { archived_from: null, archived_to: null }, has_more: false }, headers: cors });
      }
      if (pathname.startsWith('/api/platforms/future/group-chat/summaries')) {
        assert.equal(url.searchParams.get('conversation_kind'), 'circle');
        assert.equal(url.searchParams.get('chat_id'), '群/中文');
        if (request.method() === 'DELETE') {
          const body = request.postDataJSON();
          writes.push(body);
          assert.equal(body.expected_revision, 1);
          assert.ok(body.idempotency_key);
          if (conflict) return route.fulfill({ status: 409, json: { error: '摘要状态已变化，请刷新', code: 'revision_conflict' }, headers: cors });
          deleted = true;
          return route.fulfill({ json: { ok: true, status: 'deleted' }, headers: cors });
        }
        if (pathname.endsWith('/summaries')) return route.fulfill({ json: { ok: true, items: deleted || url.searchParams.get('self_id') === 'bot/b' ? [] : [summary], has_more: false, next_cursor: null }, headers: cors });
        return route.fulfill({ json: { ok: true, summary }, headers: cors });
      }
      const responses = { '/status': { running: false }, '/api/health': { running: false, adapters: {} }, '/config': { backend: { platforms: [] }, models: {} }, '/config/models': {}, '/config/models/llm': {}, '/config/session-classes': {}, '/config/edictum/sessions': {}, '/config/edictum/types': { types: [] }, '/config/edictum/plugins': { plugins: [] }, '/config/session-instances': { sessions: [] } };
      return route.fulfill({ json: responses[pathname] || {}, headers: cors });
    });
    await mockConfigVersions(page, origin, () => JSON.stringify({}));
  await page.goto(`${origin}/conversations?view=archive&archive_platform=future&archive_self=${encodeURIComponent('bot/a')}&archive_kind=circle&archive_chat=${encodeURIComponent('群/中文')}`);
    await page.getByRole('button', { name: '群摘要', exact: true }).click();
    await page.getByText('项目讨论摘要', { exact: true }).waitFor();
    await page.getByText('部分记录摘要', { exact: true }).waitFor();
    await page.getByRole('button', { name: '查看摘要', exact: true }).click();
    await page.getByText('成员建议先进行联调，发布日期仍未确认', { exact: true }).waitFor();
    await page.getByRole('button', { name: '查看出处 source/a' }).click();
    await page.getByText('我们先联调，发布时间再讨论', { exact: true }).waitFor();
    await page.keyboard.press('Escape');
    await page.getByText('我们先联调，发布时间再讨论', { exact: true }).waitFor({ state: 'hidden' });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.getByText('成员建议先进行联调，发布日期仍未确认', { exact: true }).scrollIntoViewIfNeeded();
    await mkdir(path.join(root, 'test-results/group-chat'), { recursive: true });
    await page.screenshot({ path: path.join(root, 'test-results/group-chat/summary-detail-mobile.png'), fullPage: true });
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.keyboard.press('Escape');
    await page.getByText('成员建议先进行联调，发布日期仍未确认', { exact: true }).waitFor({ state: 'hidden' });
    await page.getByRole('button', { name: '删除摘要', exact: true }).click();
    await page.getByRole('button', { name: '确认删除摘要' }).click();
    await page.getByText(/摘要状态已变化，请刷新/).waitFor();
    conflict = false;
    await page.getByRole('button', { name: '确认删除摘要' }).click();
    await page.getByText('摘要已删除，原始消息和模型上下文保持原状', { exact: true }).waitFor();
    assert.equal(writes[0].idempotency_key, writes[1].idempotency_key);
    await page.getByRole('button').filter({ hasText: '备用账号' }).click();
    await page.getByRole('button', { name: '群摘要', exact: true }).click();
    await page.getByText('暂无匹配摘要，可在群里请机器人总结指定时段的讨论', { exact: true }).waitFor();
    await page.setViewportSize({ width: 390, height: 844 });
    await page.getByRole('heading', { name: '备用账号 · 群摘要' }).scrollIntoViewIfNeeded();
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
    await mkdir(path.join(root, 'test-results/group-chat'), { recursive: true });
    await page.screenshot({ path: path.join(root, 'test-results/group-chat/summaries-mobile.png'), fullPage: true });
    assert.deepEqual(errors, []);
    console.log('PASS: 摘要覆盖说明, 来源查看, 冲突草稿和幂等重试, 账号隔离与窄屏');
  } finally {
    if (browser) await runtime.closeContexts();
    await runtime.releaseServer(server);
  }

}

async function group_chat_memory_reminders(runtime) {







  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  const server = await runtime.vite({ root, server: { host: '127.0.0.1', port: 0 } });
  let browser;
  try {
    await runtime.listen(server);
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
    browser = await runtime.browser;
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
    await mockConfigVersions(page, origin, () => JSON.stringify({}));
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
    if (browser) await runtime.closeContexts();
    await runtime.releaseServer(server);
  }

}

export const scenarios = [
  ["group-chat-media", group_chat_media],
  ["group-chat-summaries", group_chat_summaries],
  ["group-chat-memory-reminders", group_chat_memory_reminders]
];
if (process.argv[1] && pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url) await runSuite(scenarios);
