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
  const cors = { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true', 'Access-Control-Allow-Headers': 'Content-Type', 'Access-Control-Allow-Methods': 'GET,POST,OPTIONS' };
  browser = await chromium.launch({ headless: true });
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
  await browser?.close();
  await server.close();
}
