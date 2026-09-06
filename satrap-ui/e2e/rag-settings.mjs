/** 知识库管理及通用会话覆盖浏览器回归, 所有模型和管理请求使用受控响应 */
import assert from 'node:assert/strict';
import path from 'node:path';
import { mkdir } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';
import { createServer } from 'vite';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const server = await createServer({ root, server: { host: '127.0.0.1', port: 0 } });
let browser;
try {
  await server.listen();
  const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
  browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const options = { llm: [{ value: 'chat-model', label: '会话模型' }], embed: [{ value: 'embed-model', label: '向量模型' }], rerank: [{ value: 'rank-model', label: '重排模型' }] };
  const baseConfig = { embed: 'embed-model', chunk_size: 800, chunk_overlap: 120, batch_size: 32, duplicate_policy: 'skip' };
  const libraries = [{ id: 'global1', name: '共享产品资料', scope: 'global', session_id: '', config: baseConfig, revision: 1, is_default: 0, status: 'ready', last_error: '', document_count: 0, chunk_count: 0 }];
  const upload = { extensions: ['.txt', '.md', '.pdf', '.docx', '.xlsx', '.csv', '.json'], max_file_bytes: 33554432, max_text_chars: 1000000, missing_parsers: {}, text_encoding: 'UTF-8' };
  const documents = [];
  const actions = [], saves = [], historyQueries = [];
  let revision = 0, overrides = {};
  const fields = {
    rerank: { type: 'rerank', default: '', description: '后端重排模型' },
    top_k: { type: 'number', integer: true, minimum: 1, default: 5 },
    threshold: { type: 'number', nullable: true, default: null },
    enabled: { type: 'bool', default: true },
    global_db_ids: { type: 'knowledge_bases', scope: 'global', default: [] },
    fixed: { type: 'string', session_overridable: false, default: '固定参数' },
  };
  const inherited = { rerank: '', top_k: 7, threshold: null, enabled: true, global_db_ids: [], fixed: '固定参数' };
  await context.route(`${origin}/ui-config.json`, (route) => route.fulfill({ json: { backend_api: 'http://127.0.0.1:19870', control_api: 'http://127.0.0.1:19871', chat_api: 'http://127.0.0.1:19872' } }));
  await context.route(/^http:\/\/127\.0\.0\.1:1987[012]\//, async (route) => {
    const url = new URL(route.request().url());
    const method = route.request().method();
    let json = { ok: true }, status = 200;
    if (url.pathname === '/config/session-instances') json = { sessions: [] };
    if (url.pathname === '/chat/history') {
      historyQueries.push(Object.fromEntries(url.searchParams));
      json = { items: [{ conversation_id: 'chat-one', title: '测试会话' }], total: 1, page: 1, page_size: 50, storage_size_bytes: null, mode: 'cold' };
    }
    if (url.pathname.endsWith('/rag')) {
      if (method === 'POST') {
        const payload = route.request().postDataJSON();
        actions.push(payload);
        if (payload.action === 'create') {
          libraries.push({ ...libraries[0], id: 'local1', name: payload.name, scope: payload.scope, session_id: 'chat-one', config: payload.config, is_default: 1 });
          json = { ok: true, knowledge_base: libraries.at(-1) };
        } else if (payload.action === 'update') {
          const kb = libraries.find((item) => item.id === payload.kb_id);
          Object.assign(kb, { name: payload.name, config: payload.config, is_default: Number(payload.is_default), revision: kb.revision + 1 });
        } else if (payload.action === 'ingest') {
          documents.push({ source_id: 'doc1', source: payload.source, chunk_count: 1 });
          json = { ok: true, status: 'indexed' };
        } else if (payload.action === 'search') {
          json = { ok: true, status: 'found', results: [{ text: '可引用的苹果资料', sources: [{ source: '说明.md' }] }], warnings: [] };
        }
      } else json = { ok: true, knowledge_bases: libraries, documents: url.searchParams.get('kb_id') ? documents : [], model_options: options, upload };
    }
    if (url.pathname.endsWith('/rag/upload') && method === 'POST') {
      const source = url.searchParams.get('source') || url.searchParams.get('file_name');
      const kb_id = url.searchParams.get('kb_id');
      actions.push({ action: 'ingest', kb_id, source, text: route.request().postDataBuffer().toString('utf8') });
      documents.push({ source_id: 'doc1', source, chunk_count: 1 });
      json = { ok: true, status: 'indexed', chunks: 1, source };
    }
    if (url.pathname.endsWith('/session-plugin-config')) {
      if (method === 'PUT') {
        const payload = route.request().postDataJSON();
        saves.push(payload);
        if (payload.expected_revision !== revision) { status = 409; json = { error: '配置已更新, 请重新加载' }; }
        else { overrides = payload.overrides; revision++; }
      }
      if (status === 200) json = { ok: true, schema: fields, inherited, config: { ...inherited, ...overrides }, overrides, revision, sources: { top_k: Object.hasOwn(overrides, 'top_k') ? 'session' : 'named' }, inherited_sources: { top_k: 'named' }, model_options: options, runtime: { status: 'next_turn' } };
    }
    return route.fulfill({ status, json, headers: { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true', 'Access-Control-Allow-Headers': 'Content-Type', 'Access-Control-Allow-Methods': 'GET,POST,PUT,OPTIONS' } });
  });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  page.on('dialog', (dialog) => dialog.accept());
  await page.routeWebSocket('ws://127.0.0.1:19870/ws/status**', () => {});
  await page.goto(`${origin}/rag`);
  await page.getByRole('button', { name: '共享产品资料', exact: true }).waitFor();
  await page.getByLabel('管理范围').selectOption('platform:chat');
  await page.getByRole('option', { name: '测试会话', exact: true }).waitFor({ state: 'attached' });
  await page.getByLabel('Chat 会话', { exact: true }).selectOption('chat-one');
  await page.getByRole('button', { name: '新建知识库', exact: true }).click();
  await page.getByLabel('知识库名称').fill('当前会话资料');
  await page.getByLabel('embed', { exact: false }).selectOption('embed-model');
  await page.getByRole('dialog').getByRole('button', { name: '保存', exact: true }).click();
  await page.getByRole('button', { name: '粘贴文本', exact: true }).click();
  await page.getByLabel('文档来源').fill('说明.md');
  await page.getByLabel('导入文本').fill('苹果资料');
  await page.getByRole('button', { name: '导入到此库' }).click();
  await page.getByText('说明.md · 1 块', { exact: true }).waitFor();
  await page.getByLabel('检索问题').fill('苹果');
  await page.getByLabel('重排配置').selectOption('rank-model');
  await page.getByRole('button', { name: '测试检索' }).click();
  await page.locator('pre').filter({ hasText: '可引用的苹果资料' }).waitFor();
  assert.equal(actions.find((item) => item.action === 'create').scope, 'session');
  assert.equal(actions.find((item) => item.action === 'ingest').kb_id, 'local1');
  assert.equal(actions.find((item) => item.action === 'search').config.rerank, 'rank-model');
  assert.ok(historyQueries.every((item) => item.page_size === '50'));
  const output = path.resolve(root, '../.satrap/test-artifacts');
  await mkdir(output, { recursive: true });
  await page.screenshot({ path: path.join(output, 'rag-management.png'), fullPage: true });
  await page.evaluate(async () => {
    const React = await import('/node_modules/.vite/deps/react.js');
    const client = await import('/node_modules/.vite/deps/react-dom_client.js');
    const { SessionPluginSettingsModal } = await import('/src/components/common/SessionPluginSettingsModal.tsx');
    const host = document.createElement('div');
    document.body.appendChild(host);
    client.default.createRoot(host).render(React.default.createElement(SessionPluginSettingsModal, { context: { platformId: 'chat', sessionId: 'chat-one' }, plugins: ['example'], onClose() {} }));
  });
  const modal = page.getByRole('dialog');
  await modal.getByLabel('top_k', { exact: true }).fill('3');
  await modal.getByLabel('enabled', { exact: true }).uncheck();
  await modal.getByLabel('rerank', { exact: true }).selectOption('rank-model');
  assert.equal(await modal.getByLabel('fixed', { exact: true }).isDisabled(), true);
  await modal.getByRole('button', { name: '保存', exact: true }).click();
  await modal.getByRole('status').waitFor();
  assert.deepEqual(saves[0], { expected_revision: 0, overrides: { top_k: 3, enabled: false, rerank: 'rank-model' } });
  await modal.getByRole('button', { name: '全部恢复继承' }).click();
  assert.equal(await modal.getByLabel('top_k', { exact: true }).inputValue(), '7');
  assert.equal(await modal.getByText('继承命名配置', { exact: true }).isVisible(), true);
  await modal.getByRole('button', { name: '保存', exact: true }).click();
  await page.waitForFunction(() => document.querySelector('[role=status]')?.textContent?.includes('已保存'));
  assert.deepEqual(saves[1], { expected_revision: 1, overrides: {} });
  await page.screenshot({ path: path.join(output, 'session-overrides.png'), fullPage: true });
  assert.deepEqual(errors, []);
  console.log('PASS: 管理页面, 模型选择, 会话分页, 导入检索, 独立覆写, 禁止覆写字段, 恢复继承');
} finally {
  await browser?.close();
  await server.close();
}
