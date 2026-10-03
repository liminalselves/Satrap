import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { mkdir, readFile } from 'node:fs/promises';
import { load } from 'js-yaml';
import { chromium } from 'playwright';
import { createServer } from 'vite';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const meta = load(await readFile(path.join(root, '../satrap/expend/plugins/group_chat/meta.yaml'), 'utf8'));
const plugin = { ...meta, source: 'builtin', usage_count: 0, edictum_configs: [], config_schema: {}, capabilities: { tools: meta.tools, skills: meta.skills, handlers: meta.handlers } };
const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jGMsAAAAASUVORK5CYII=', 'base64');
const sticker = { sticker_id: 'st/image:opaque', name: '收到', tags: ['确认'], collection: '常用', kind: 'image', content_revision: 1, enabled: true, mime_type: 'image/png', adapter_type: null, width: 1, height: 1, size_bytes: png.length };
const now = Date.now() / 1000;
const records = ['bot/a', 'bot/b'].map((self_id) => ({ platform_id: 'future', adapter_id: 'future', platform_type: 'future', type_label: '扩展平台', self_id, conversation_kind: 'circle', conversation_kind_label: '圈子讨论', chat_id: '群/中文', label: self_id === 'bot/a' ? '主要账号' : '备用账号', revision: 0, message_count: 1, last_message_at: now }));
const server = await createServer({ root, server: { host: '127.0.0.1', port: 0 } });
let browser;
try {
  await server.listen();
  const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
  browser = await chromium.launch({ headless: true });
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
  if (browser) await browser.close();
  await server.close();
}
