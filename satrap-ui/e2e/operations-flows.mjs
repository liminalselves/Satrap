import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { emptyConfigResponse, runSuite } from './runtime.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const artifacts = path.join(root, 'test-results', 'operations');

/** 为运营只读页面提供最小健康与认证夹具, 业务接口由各场景显式声明 */
async function openOperationsPage(runtime, pathname, handleApi) {
  const server = await runtime.vite({ root, server: { host: '127.0.0.1', port: 0 } });
  await runtime.listen(server);
  const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
  const context = await runtime.browser.newContext({ viewport: { width: 1440, height: 1000 } });
  await context.route(`${origin}/ui-config.json`, (route) => route.fulfill({ json: {
    backend_api: 'http://127.0.0.1:19870', control_api: 'http://127.0.0.1:19871', chat_api: 'http://127.0.0.1:19872',
  } }));
  await context.route(/^http:\/\/127\.0\.0\.1:1987[012]\//, async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const headers = {
      'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true',
      'Access-Control-Allow-Headers': '*', 'Access-Control-Allow-Methods': '*',
    };
    const reply = (json, status = 200) => route.fulfill({ status, json, headers });
    if (request.method() === 'OPTIONS') return route.fulfill({ status: 204, headers });
    if (await handleApi({ request, url, reply })) return;
    if (url.pathname === '/auth/session' || url.pathname === '/api/auth/session') return reply({ ok: true });
    if (url.pathname === '/status') return reply({ running: true, managed: true });
    if (url.pathname === '/api/health') return reply({ running: true, adapters: {}, platform_config: [] });
    if (url.pathname === '/api/chat/health') return reply({ ok: true, conversations: 0 });
    if (url.pathname === '/api/platforms') return reply({ platforms: [] });
    if (url.pathname === '/config/session-instances' || url.pathname === '/api/sessions') return reply({ sessions: [] });
    return reply(emptyConfigResponse(request.url()));
  });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.routeWebSocket(/ws:\/\/127\.0\.0\.1:1987[012]\//, () => {});
  await page.goto(origin + pathname);
  return { server, page, errors };
}

function approvalCatalog() {
  return {
    generated_at: Math.floor(Date.now() / 1000), items: [], total: 1, complete: true, warnings: [],
    platforms: [], pending_count: 1, by_state: { pending: 1 }, by_source: { memory: 1 },
    today_decided: 1, automatic_count: 0, expired_count: 0, decision_latency_ms: 20,
    window: { from: null, to: null, tz: 'Asia/Shanghai', bucket: null },
    today_window: { from: null, to: null, tz: 'Asia/Shanghai', bucket: 'day' },
    has_more: false, next_cursor: null,
  };
}

async function approvals_summary(runtime) {
  /** 汇总请求独立于来源筛选, 失败时列表计数不得冒充全局分面 */
  await fs.mkdir(artifacts, { recursive: true });
  let summaryDown = true;
  const summaryQueries = [];
  const { server, page, errors } = await openOperationsPage(runtime, '/platforms/approvals?source=memory', async ({ url, reply }) => {
    if (url.pathname === '/api/platform-actions/summary') {
      summaryQueries.push(Object.fromEntries(url.searchParams));
      if (summaryDown) { await reply({ error: '模拟汇总不可用' }, 503); return true; }
      await reply({
        ...approvalCatalog(), total: 7, pending_count: 7,
        by_state: { pending: 7 }, by_source: { group: 5, invitation: 0, friend: 0, memory: 2 }, today_decided: 3,
      });
      return true;
    }
    if (url.pathname === '/api/platform-actions') {
      await reply(approvalCatalog());
      return true;
    }
    return false;
  });
  try {
    await page.getByRole('heading', { name: '审批中心', exact: true }).waitFor();
    await page.getByText('汇总暂不可用, 数量可能未确认', { exact: true }).waitFor();
    const memory = page.getByRole('button', { name: /记忆提案/ });
    assert.match(await memory.innerText(), /—/);
    assert.ok(summaryQueries.length > 0);
    assert.ok(summaryQueries.every((query) => !Object.hasOwn(query, 'source')));
    await page.screenshot({ path: path.join(artifacts, 'approval-summary-degraded.png'), fullPage: true });

    summaryDown = false;
    await page.getByRole('button', { name: '刷新', exact: true }).first().click();
    await page.getByText('7 待处理', { exact: true }).waitFor();
    await page.getByText('今日已决定 3', { exact: true }).first().waitFor();
    assert.match(await memory.innerText(), /2/);
    assert.equal(await page.getByText('汇总暂不可用, 数量可能未确认', { exact: true }).count(), 0);
    assert.deepEqual(errors, []);
    console.log('PASS: 审批汇总不带来源筛选, 独立降级且不借用列表计数, 恢复后显示全历史分面');
  } finally {
    await runtime.closeContexts();
    await runtime.releaseServer(server);
  }
}

function usageTotals() {
  return {
    calls: 2, succeeded: 2, failed: 0, cancelled: 0, usage_reported_calls: 2, usage_missing_calls: 0,
    input_tokens: 80, output_tokens: 20, cached_tokens: 0, total_tokens: 100,
    token_reported_calls: { input_tokens: 2, output_tokens: 2, cached_tokens: 2, total_tokens: 2 },
    duration_p50_ms: 10, duration_p95_ms: 20, duration_method: 'exact',
  };
}

function usageMeta() {
  const now = Math.floor(Date.now() / 1000);
  return {
    generated_at: now, window: { from: now - 3600, to: now, tz: 'Asia/Shanghai', bucket: 'hour' },
    complete: true, sources: [], warnings: [],
  };
}

async function usage_partial_failure(runtime) {
  /** 成功来源正常更新, 部分或全部失败保留缓存且不伪装为空数据 */
  await fs.mkdir(artifacts, { recursive: true });
  let modelsDown = false;
  let allDown = false;
  const usage = usageTotals();
  const { server, page, errors } = await openOperationsPage(runtime, '/usage', async ({ url, reply }) => {
    if (!['/api/stats/overview', '/api/stats/models', '/api/stats/sessions'].includes(url.pathname)) return false;
    if (allDown || (modelsDown && url.pathname === '/api/stats/models')) {
      await reply({ error: '模拟统计来源不可用' }, 503);
      return true;
    }
    if (url.pathname === '/api/stats/models') {
      await reply({ ...usageMeta(), models: [{ model_config: 'chat-model', model: 'test-model', provider: 'test', usage, trend: [] }], has_more: false, next_cursor: null });
      return true;
    }
    if (url.pathname === '/api/stats/sessions') {
      await reply({ ...usageMeta(), sessions: [], has_more: false, next_cursor: null });
      return true;
    }
    await reply({
      ...usageMeta(), messages: { inbound: 1, outbound: 1, model_requests: 2, commands: 0 }, usage,
      series: [], by_adapter: [],
      comparison: {
        mode: 'yesterday_same_elapsed', baseline_window: { from: null, to: null, tz: 'Asia/Shanghai', bucket: null },
        messages: { current: 1, previous: 1, absolute_delta: 0, percent_delta: 0 },
        tokens: { current: 100, previous: 100, absolute_delta: 0, percent_delta: 0 },
      },
    });
    return true;
  });
  try {
    await page.getByRole('heading', { name: '用量统计', exact: true }).waitFor();
    await page.getByText('chat-model', { exact: true }).waitFor();

    modelsDown = true;
    await page.getByRole('button', { name: '刷新', exact: true }).click();
    await page.getByRole('alert').filter({ hasText: '部分数据读取失败' }).waitFor();
    await page.getByText('chat-model', { exact: true }).waitFor();
    await page.screenshot({ path: path.join(artifacts, 'usage-partial-failure.png'), fullPage: true });

    allDown = true;
    await page.getByRole('button', { name: '刷新', exact: true }).click();
    await page.getByRole('alert').filter({ hasText: '用量数据读取失败, 正在显示缓存数据' }).waitFor();
    await page.getByText('chat-model', { exact: true }).waitFor();

    allDown = false;
    modelsDown = false;
    await page.getByRole('button', { name: '刷新', exact: true }).click();
    await page.getByRole('alert').filter({ hasText: '数据读取失败' }).waitFor({ state: 'hidden' });
    await page.getByText('chat-model', { exact: true }).waitFor();
    assert.deepEqual(errors, []);
    console.log('PASS: Usage 部分与全部来源失败均保留缓存并提示, 恢复刷新后清除错误');
  } finally {
    await runtime.closeContexts();
    await runtime.releaseServer(server);
  }
}

async function rag_manager_refresh(runtime) {
  /** 内嵌 RagManager 在每次变更后刷新 revision, 列表失败不误报刷新成功 */
  await fs.mkdir(artifacts, { recursive: true });
  const kb = {
    id: 'manager-kb', name: '内嵌管理库', scope: 'session', session_id: 'chat-one',
    config: { embed: 'embed-model', chunk_size: 800, chunk_overlap: 120, batch_size: 32, duplicate_policy: 'skip' },
    revision: 1, is_default: 1, status: 'ready', last_error: '', document_count: 0, chunk_count: 0,
  };
  const documents = [];
  const jobs = [];
  const expectedRevisions = [];
  let listDown = false;
  const { server, page, errors } = await openOperationsPage(runtime, '/conversations/instances', async ({ request, url, reply }) => {
    if (url.pathname === '/status') { await reply({ running: false, managed: true }); return true; }
    if (url.pathname === '/api/sessions' || url.pathname === '/config/session-instances') {
      await reply({ sessions: [{ platform_id: 'ob', session_id: 'chat-one', provider_name: 'edictum', session_type: 'simple',
        active: false, created_at: 1_790_000_000, last_used_at: 1_790_000_000, message_count: 2 }] });
      return true;
    }
    if (url.pathname === '/config/rag' && request.method() === 'GET') {
      if (listDown) { await reply({ error: '模拟列表读取失败' }, 503); return true; }
      await reply({
        ok: true, knowledge_bases: [kb], documents,
        model_options: { llm: [], embed: [], rerank: [] },
        upload: { extensions: ['.txt'], max_file_bytes: 1024, max_text_chars: 10000, missing_parsers: {}, text_encoding: 'UTF-8' },
      });
      return true;
    }
    if (url.pathname === '/config/rag/jobs/upload' && request.method() === 'POST') {
      const expected = Number(url.searchParams.get('expected_revision'));
      expectedRevisions.push(expected);
      if (expected !== kb.revision) { await reply({ error: '知识库修订冲突' }, 409); return true; }
      const source = url.searchParams.get('source') || url.searchParams.get('file_name');
      kb.revision += 1;
      kb.document_count += 1;
      kb.chunk_count += 2;
      const now = Math.floor(Date.now() / 1000);
      const job = {
        job_id: `job-${jobs.length + 1}`, operation_id: url.searchParams.get('operation_id'), owner_service: 'chat', owner_runtime_id: 'test',
        platform_id: 'ob', session_id: 'chat-one', kb_id: kb.id, action: 'ingest', status: 'succeeded', phase: 'finished',
        progress: { unit: 'documents', done: 1, total: 1, percent: 100 }, created_at: now, started_at: now, finished_at: now,
        error: null, result: { status: 'indexed', source_id: source, chunks: 2, revision: kb.revision },
      };
      jobs.unshift(job);
      documents.push({
        document_id: source, source_id: source, name: source, size_bytes: 4, created_at: now, updated_at: now,
        last_indexed_at: now, status: 'ready', error: null, job_id: job.job_id, chunk_count: 2,
      });
      await reply({ job });
      return true;
    }
    if (url.pathname === '/config/rag/jobs' && request.method() === 'GET') {
      await reply({ items: jobs, has_more: false, next_cursor: null });
      return true;
    }
    if (url.pathname === '/config/rag/documents' && request.method() === 'GET') {
      await reply({ items: documents, has_more: false, next_cursor: null });
      return true;
    }
    return false;
  });
  try {
    await page.getByText('chat-one', { exact: true }).first().waitFor();
    await page.getByRole('button', { name: /^知识库(?:\s|$)/ }).first().click();
    const manager = page.getByRole('dialog', { name: '会话知识库', exact: true });
    await manager.getByText('rev 1', { exact: true }).waitFor();

    for (const [index, name] of ['第一份.txt', '第二份.txt'].entries()) {
      await manager.getByRole('button', { name: '导入文档', exact: true }).click();
      const dialog = page.getByRole('dialog', { name: '导入文档', exact: true });
      await dialog.getByLabel('导入文件').setInputFiles({ name, mimeType: 'text/plain', buffer: Buffer.from(name) });
      await dialog.getByRole('button', { name: '导入到此库', exact: true }).click();
      await manager.getByRole('status').filter({ hasText: '本次导入：成功 1' }).waitFor();
      await dialog.getByRole('button', { name: '关闭', exact: true }).click();
      await manager.getByText(`rev ${index + 2}`, { exact: true }).waitFor();
    }
    assert.deepEqual(expectedRevisions, [1, 2]);
    assert.equal(kb.document_count, 2);

    listDown = true;
    await manager.getByRole('button', { name: '刷新', exact: true }).click();
    await manager.getByRole('alert').filter({ hasText: '模拟列表读取失败' }).waitFor();
    assert.equal(await manager.getByText('知识库列表已刷新', { exact: true }).count(), 0);
    listDown = false;
    await manager.getByRole('button', { name: '刷新', exact: true }).click();
    await manager.getByText('知识库列表已刷新', { exact: true }).waitFor();
    await page.screenshot({ path: path.join(artifacts, 'rag-manager-refresh.png'), fullPage: true });
    assert.deepEqual(errors, []);
    console.log('PASS: 内嵌 RagManager 连续导入推进 revision, 列表与文档刷新, 失败不误报成功');
  } finally {
    await runtime.closeContexts();
    await runtime.releaseServer(server);
  }
}

async function settings_raw_rebase(runtime) {
  /** 保存全部遇到原始配置冲突后, 显式重读应保留草稿并允许基于新修订合并保存 */
  let revision = 'r1';
  const group = {
    id: 'g1', name: '旧管理员', enabled: true, protect: true, members: [], chats: [],
    plugin_scope: { mode: 'all', included: [], excluded: [] },
  };
  let config = {
    api: { host: '127.0.0.1', port: 19870 }, session_timeout: 30,
    administrator_groups: [group], administrator_overrides: [],
  };
  const snapshot = {
    ok: true, groups: [group], overrides: [], revision: 'r1', section_revision: 's1',
    platforms: [], plugins: [], runtime: { status: 'applied' },
  };
  const writes = [];
  const nextRevision = () => `r${Number(revision.slice(1)) + 1}`;
  const { server, page, errors } = await openOperationsPage(runtime, '/settings', async ({ request, url, reply }) => {
    if (url.pathname === '/status') { await reply({ running: false, managed: true }); return true; }
    if (url.pathname === '/config/validate-yaml') { await reply({ valid: true, errors: [] }); return true; }
    if (url.pathname === '/config/administrator-groups/runtime') {
      await reply({ section_revision: snapshot.section_revision, runtime: { status: 'applied' } });
      return true;
    }
    if (url.pathname === '/config/administrator-groups' || url.pathname === '/config/administrator-groups/snapshot') {
      if (request.method() === 'GET') { await reply(snapshot); return true; }
      const payload = request.postDataJSON();
      writes.push({ path: url.pathname, payload, expected: payload.expected_revision });
      if (payload.expected_revision !== revision) { await reply({ error: '管理员配置修订冲突' }, 409); return true; }
      snapshot.groups = payload.groups;
      snapshot.overrides = payload.overrides;
      revision = nextRevision();
      snapshot.revision = revision;
      snapshot.section_revision = `s${revision.slice(1)}`;
      config = { ...config, administrator_groups: payload.groups, administrator_overrides: payload.overrides };
      await reply(snapshot);
      return true;
    }
    if (url.pathname === '/config') {
      if (request.method() === 'GET') {
        await reply({ ok: true, exists: true, revision, config, path: 'config.yaml' });
        return true;
      }
      const expected = url.searchParams.get('expected_revision');
      const payload = request.postDataJSON();
      writes.push({ path: url.pathname, payload, expected });
      if (expected !== revision) { await reply({ ok: false, error: '原始配置修订冲突' }, 409); return true; }
      config = payload;
      revision = nextRevision();
      await reply({ ok: true, exists: true, revision, config, path: 'config.yaml' });
      return true;
    }
    return false;
  });
  try {
    const raw = page.locator('#sec-raw');
    const editor = raw.getByLabel('原始配置文件 (YAML)');
    await editor.waitFor();
    const draft = 'api:\n  host: 127.0.0.1\n  port: 19870\nsession_timeout: 45\nadministrator_groups:\n  - id: g1\n    name: 旧管理员\n    enabled: true\n    protect: true\n    members: []\n    chats: []\n    plugin_scope:\n      mode: all\n      included: []\n      excluded: []\nadministrator_overrides: []\n';
    await editor.fill(draft);

    await page.getByRole('button', { name: '管理员', exact: true }).click();
    const administrators = page.locator('[aria-label="管理员设置"]');
    await administrators.locator('[aria-label^="管理组卡片"]').first().getByRole('button').click();
    const drawer = page.getByRole('dialog');
    await drawer.getByLabel('管理组名称').fill('新管理员');
    await page.keyboard.press('Escape');
    await drawer.waitFor({ state: 'hidden' });
    const saveAll = page.getByRole('button', { name: '保存全部 (2)', exact: true });
    await saveAll.waitFor();
    await saveAll.click();
    await page.getByRole('button', { name: '保存全部 (1)', exact: true }).waitFor();
    await page.getByRole('button', { name: '原始配置', exact: true }).click();
    await raw.getByText('修订冲突: 配置文件已在其它位置更新', { exact: true }).waitFor();
    assert.equal(revision, 'r2');

    await raw.getByRole('button', { name: '重新读取', exact: true }).click();
    await raw.getByText('已读取最新版本, 编辑区仍是你的草稿', { exact: false }).waitFor();
    assert.equal(await raw.getByText('修订冲突: 配置文件已在其它位置更新', { exact: true }).count(), 0);
    assert.equal(await editor.inputValue(), draft);

    const merged = draft.replace('name: 旧管理员', 'name: 新管理员');
    await editor.fill(merged);
    await raw.getByRole('button', { name: '保存原始配置', exact: true }).click();
    const confirmation = page.getByRole('dialog');
    const saved = page.waitForResponse((response) => new URL(response.url()).pathname === '/config' && response.request().method() === 'PUT');
    await confirmation.getByRole('button', { name: '确认保存', exact: true }).click();
    assert.equal((await saved).status(), 200);
    assert.equal(revision, 'r3');
    assert.equal(config.session_timeout, 45);
    assert.equal(config.administrator_groups[0].name, '新管理员');
    const rawWrite = writes.find((item) => item.path === '/config');
    assert.equal(rawWrite.expected, 'r2');
    await page.screenshot({ path: path.join(artifacts, 'settings-raw-rebase.png'), fullPage: true });
    assert.deepEqual(errors, []);
    console.log('PASS: 保存全部冲突停止, 原始配置重读保留草稿并完成 rebase, 合并保存不丢管理员字段');
  } finally {
    await runtime.closeContexts();
    await runtime.releaseServer(server);
  }
}

export const scenarios = [
  ['approvals-summary', approvals_summary],
  ['usage-partial-failure', usage_partial_failure],
  ['rag-manager-refresh', rag_manager_refresh],
  ['settings-raw-rebase', settings_raw_rebase],
];

if (process.argv[1] && pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url) await runSuite(scenarios);
