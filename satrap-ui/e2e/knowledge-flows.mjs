import { autoAcceptConfirmations } from "./confirmation.mjs";
import assert from "node:assert/strict";
import path from "node:path";
import { mkdir, mkdtemp, rm } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { spawn } from "node:child_process";
import { tmpdir } from "node:os";
import { runSuite, mockConfigVersions } from './runtime.mjs';
import { pathToFileURL } from 'node:url';

async function rag_settings(runtime) {

  /** 知识库管理及通用会话覆盖浏览器回归, 所有模型和管理请求使用受控响应 */







  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  const server = await runtime.vite({ root, server: { host: '127.0.0.1', port: 0 } });
  let browser, stopConfirmations;
  try {
    await runtime.listen(server);
    const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
    browser = await runtime.browser;
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
    stopConfirmations = autoAcceptConfirmations(page);
    const errors = [];
    page.on('pageerror', (error) => errors.push(error.message));
    page.on('dialog', (dialog) => dialog.accept());
    await page.routeWebSocket('ws://127.0.0.1:19870/ws/status**', () => {});
    await mockConfigVersions(page, origin, () => JSON.stringify({revision}));
  await page.goto(`${origin}/rag`);
    await page.getByRole('button', { name: '共享产品资料', exact: true }).waitFor();
    await page.getByLabel('管理范围').selectOption('platform:chat');
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
    if (stopConfirmations) await stopConfirmations();
    await runtime.closeContexts();
    await runtime.releaseServer(server);
  }

}

async function rag_upload(runtime) {
  /** 浏览器文件上传到真实 HTTP, 文档解析和 SQLite / FAISS 检索的完整回归 */









  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  const temporary = await mkdtemp(path.join(tmpdir(), 'satrap-rag-upload-'));
  const server = await runtime.vite({ root, server: { host: '127.0.0.1', port: 0 } });
  let backend, browser;
  try {
    await runtime.listen(server);
    const origin = 'http://127.0.0.1:' + server.httpServer.address().port;
    const token = 'browser-upload-test-token-longer-than-32-characters';
    backend = spawn(process.env.SATRAP_TEST_PYTHON || 'python', [path.join(root, 'e2e/rag-upload-backend.py'), temporary, origin, token], {
      cwd: path.dirname(root), env: { ...process.env, PYTHONPATH: path.dirname(root), PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' },
      stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true,
    });
    let logs = '';
    backend.stderr.on('data', (chunk) => { logs += chunk; });
    const port = await new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(new Error('后端启动超时: ' + logs)), 30000);
      let output = '';
      backend.stdout.on('data', (chunk) => {
        output += chunk;
        const match = output.match(/READY (\{[^\n]+\})/);
        if (match) { clearTimeout(timeout); resolve(JSON.parse(match[1]).port); }
      });
      backend.once('error', (error) => { clearTimeout(timeout); reject(error); });
      backend.once('exit', (code) => { clearTimeout(timeout); reject(new Error('后端退出 ' + code + ': ' + logs)); });
    });
    const base = 'http://127.0.0.1:' + port;
    browser = await runtime.browser;
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    await context.route(origin + '/ui-config.json', (route) => route.fulfill({ json: { backend_api: base, control_api: base, chat_api: base } }));
    await context.route(base + '/**', async (route) => {
      const pathname = new URL(route.request().url()).pathname;
      if (pathname.startsWith('/config/rag') || pathname.startsWith('/auth/')) return route.continue();
      return route.fulfill({ json: { ok: true, sessions: [], models: [], running: false }, headers: { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true' } });
    });
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', (error) => errors.push(error.message));
    await page.routeWebSocket(base.replace('http', 'ws') + '/**', () => {});
    await mockConfigVersions(page, origin, () => JSON.stringify({}));
  await page.goto(origin + '/rag#token=' + token);
    await page.getByRole('button', { name: '上传文档', exact: true }).click();
    await page.getByLabel('导入文件').setInputFiles([
      { name: 'bad.exe', mimeType: 'application/octet-stream', buffer: Buffer.from('bad') },
      { name: 'bad.txt', mimeType: 'text/plain', buffer: Buffer.from([255]) },
    ]);
    await page.getByRole('alert').filter({ hasText: '不支持' }).waitFor();
    await page.getByRole('button', { name: '导入到此库' }).click();
    await page.getByRole('alert').filter({ hasText: 'UTF-8' }).waitFor();
    await page.getByRole('button', { name: '移除 bad.txt', exact: true }).click();
    const content = Buffer.from('中文文档'.repeat(100000), 'utf8');
    assert.ok(content.length > 1024 * 1024);
    await page.getByLabel('导入文件').setInputFiles({ name: '真实中文.md', mimeType: 'text/markdown', buffer: content });
    const artifacts = path.resolve(root, '../.satrap/test-artifacts');
    await mkdir(artifacts, { recursive: true });
    await page.screenshot({ path: path.join(artifacts, 'rag-upload-modal.png'), fullPage: true });
    const response = page.waitForResponse((res) => res.url().includes('/config/rag/upload') && res.request().method() === 'POST');
    await page.getByRole('button', { name: '导入到此库' }).click();
    const uploaded = await response;
    assert.equal(uploaded.status(), 200);
    assert.equal(uploaded.request().postDataBuffer().length, content.length);
    await page.getByText('真实中文.md · 4 块', { exact: true }).waitFor();
    await page.getByRole('button', { name: '关闭', exact: true }).click();
    await page.getByLabel('检索问题').fill('中文文档');
    await page.getByRole('button', { name: '测试检索' }).click();
    await page.locator('pre').filter({ hasText: '真实中文.md' }).waitFor();
    await page.getByRole('button', { name: '上传文档', exact: true }).click();
    await page.getByRole('group', { name: '文件拖放区域' }).evaluate((element) => {
      const transfer = new DataTransfer();
      transfer.items.add(new File(['中文文档'.repeat(100000)], '真实中文.md', { type: 'text/markdown' }));
      transfer.items.add(new File(['FAIL_ONCE'], '稍后重试.txt', { type: 'text/plain' }));
      transfer.items.add(new File(['后续正常文档'], '第二份.txt', { type: 'text/plain' }));
      element.dispatchEvent(new DragEvent('drop', { bubbles: true, dataTransfer: transfer }));
    });
    await page.getByRole('button', { name: '导入到此库' }).click();
    await page.getByRole('status').filter({ hasText: '共 3 个 · 成功 1 · 跳过 1 · 失败 1' }).waitFor();
    await page.getByRole('alert').filter({ hasText: '向量化或构建索引失败: RuntimeError: Embedding 服务返回 429: quota exceeded' }).waitFor();
    await page.getByText('第二份.txt · 1 块', { exact: true }).waitFor();
    const retryRequests = [];
    const trackRetry = (request) => { if (request.url().includes('/config/rag/upload') && request.method() === 'POST') retryRequests.push(new URL(request.url()).searchParams.get('file_name')); };
    page.on('request', trackRetry);
    await page.getByRole('button', { name: '重试失败文件', exact: true }).click();
    await page.getByRole('status').filter({ hasText: '共 3 个 · 成功 2 · 跳过 1 · 失败 0' }).waitFor();
    page.off('request', trackRetry);
    assert.deepEqual(retryRequests, ['稍后重试.txt']);
    await page.screenshot({ path: path.join(artifacts, 'rag-batch-results.png'), fullPage: true });
    await page.getByRole('button', { name: '关闭', exact: true }).click();
    await page.getByRole('button', { name: '上传文档', exact: true }).click();
    await page.getByRole('button', { name: '粘贴文本', exact: true }).click();
    await page.getByLabel('文档来源').fill('粘贴资料');
    await page.getByLabel('导入文本').fill('中文文档'.repeat(100000));
    await page.getByRole('button', { name: '导入到此库' }).click();
    await page.getByText('粘贴资料 · 4 块', { exact: true }).waitFor();
    assert.deepEqual(errors, []);
    console.log('PASS: 多文件拖放, 独立来源, 部分失败继续, 真实构建错误, 仅重试失败项, 重复跳过, 大文件及文本导入检索');
  } finally {
    if (browser) await runtime.closeContexts();
    if (backend && backend.exitCode === null) {
      backend.stdin.end('\n');
      await new Promise((resolve) => {
        const timer = setTimeout(() => { backend.kill(); resolve(); }, 10000);
        backend.once('exit', () => { clearTimeout(timer); resolve(); });
      });
    }
    await runtime.releaseServer(server);
    const resolved = path.resolve(temporary);
    if (path.dirname(resolved) !== path.resolve(tmpdir()) || !path.basename(resolved).startsWith('satrap-rag-upload-')) throw new Error('临时目录范围检查失败');
    await rm(resolved, { recursive: true, force: true });
  }

}

export const scenarios = [
  ["rag-settings", rag_settings],
  ["rag-upload", rag_upload]
];
if (process.argv[1] && pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url) await runSuite(scenarios);
