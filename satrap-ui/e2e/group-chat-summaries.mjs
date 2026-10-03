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
    conversation_kind: 'circle', conversation_kind_label: '圈子讨论', chat_id: '群/中文', label: self_id === 'bot/a' ? '主要账号' : '备用账号', revision: 0, message_count: 1, last_message_at: now }));
  const summary = { schema_version: 1, summary_id: 'summary/a', title: '项目讨论摘要', revision: 1, state: 'active', created_at: now, expires_at: now + 86400,
    points: [{ text: '成员建议先进行联调，发布日期仍未确认', source_message_ids: ['source/a'] }],
    resolved_range: { start_time: '2026-10-04T09:00:00+08:00', end_time: '2026-10-04T18:00:00+08:00' },
    selection: { selected_count: 200, all_local_matches_selected: false, truncated: true, reasons: ['context_budget'] },
    archive_coverage: { platform_history_complete: false, archived_from: now - 60, archived_to: now, retention_days: 30 } };
  let deleted = false;
  let conflict = true;
  const writes = [];
  browser = await chromium.launch({ headless: true });
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
  if (browser) await browser.close();
  await server.close();
}
