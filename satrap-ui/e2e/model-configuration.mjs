import { conversationPageResponse, turnPageResponse } from './config-fixtures.mjs';
import { emptyConfigResponse } from './runtime.mjs';
import assert from "node:assert/strict";
import path from "node:path";
import fs from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { runSuite, mockConfigVersions, openConversation } from './runtime.mjs';
import { pathToFileURL } from 'node:url';
import { createBenchmarkFixture } from '../benchmarks/fixtures.mjs';

async function asr_models(runtime) {
  /** ASR 模型配置与转录测试的浏览器回归, 接口使用受控响应, 不调用真实服务 */







  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  const server = await runtime.vite({ root, server: { host: '127.0.0.1', port: 0 } });
  const artifacts = path.join(root, 'test-results', 'asr-models');
  let browser;
  try {
    await fs.mkdir(artifacts, { recursive: true });
    await runtime.listen(server);
    const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
    browser = await runtime.browser;
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    const models = { llm: {}, embedding: {}, rerank: {}, asr: {} };
    const writes = [];
    const tests = [];
    let testMode = 'error';
    await context.route(`${origin}/ui-config.json`, (route) => route.fulfill({ json: {
      backend_api: 'http://127.0.0.1:19870', control_api: 'http://127.0.0.1:19871', chat_api: 'http://127.0.0.1:19872',
    } }));
    await context.route(/^http:\/\/127\.0\.0\.1:1987[012]\//, async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      const pathname = url.pathname;
      const headers = { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true', 'Access-Control-Allow-Headers': 'Content-Type', 'Access-Control-Allow-Methods': 'GET, POST, PUT, DELETE, OPTIONS' };
      if (request.method() === 'OPTIONS') return route.fulfill({ status: 204, headers });
      if (pathname === '/config/models/asr/speech/test' && request.method() === 'POST') {
        tests.push(request.postDataJSON());
        if (testMode === 'error') return route.fulfill({ status: 400, json: { ok: false, error: '音频格式不受支持' }, headers });
        return route.fulfill({ json: { ok: true, text: '你好, 世界', model: 'whisper-1', language: 'zh', duration: 1.5, elapsed_ms: 321 }, headers });
      }
      const match = pathname.match(/^\/config\/models\/(\w+)\/([^/]+)$/);
      if (match && ['POST', 'PUT'].includes(request.method())) {
        const [, type, name] = match;
        const payload = request.postDataJSON();
        writes.push({ type, name: decodeURIComponent(name), payload });
        models[type][decodeURIComponent(name)] = { ...payload, api_key: payload.api_key ? '****' : undefined };
        return route.fulfill({ json: { ok: true }, headers });
      }
      if (pathname === '/config/models') {
        return route.fulfill({ json: models[url.searchParams.get('type')] ?? {}, headers });
      }
      const responses = { '/api/health': { running: true, adapters: {} }, '/status': { running: true } };
      return route.fulfill({ json: responses[pathname] ?? emptyConfigResponse(route.request().url()), headers });
    });
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', (error) => errors.push(error.message));
    await page.routeWebSocket('ws://127.0.0.1:19870/ws/status**', () => {});
    await mockConfigVersions(page, origin, () => JSON.stringify({models}));
  await page.goto(`${origin}/models`);
    await page.getByRole('button', { name: 'ASR 配置', exact: true }).click();
    await page.getByRole('button', { name: '新增配置' }).click();
    const dialog = page.getByRole('dialog');
    await dialog.getByLabel('配置名称').fill('speech');
    await dialog.getByLabel('模型', { exact: true }).fill('whisper-1');
    await dialog.getByLabel('Base URL').fill('http://127.0.0.1:9000/v1');
    await dialog.getByLabel('API Key').fill('asr-secret');
    await dialog.getByLabel('识别语言').fill('zh');
    await dialog.getByRole('button', { name: '创建' }).click();
    await dialog.waitFor({ state: 'hidden' });
    assert.deepEqual(writes.map((w) => [w.type, w.name, w.payload.model, w.payload.api_key, w.payload.language]), [['asr', 'speech', 'whisper-1', 'asr-secret', 'zh']]);
    await page.getByRole('heading', { name: 'speech' }).waitFor();
    assert.equal(await page.getByText('asr-secret', { exact: true }).count(), 0);

    await page.getByRole('button', { name: '测试转录 speech' }).click();
    const testDialog = page.getByRole('dialog');
    await testDialog.getByLabel('音频文件').setInputFiles({ name: 'clip.wav', mimeType: 'audio/wav', buffer: Buffer.from('RIFF....WAVEfmt ') });
    await testDialog.getByRole('button', { name: '开始转录' }).click();
    await testDialog.getByRole('alert').filter({ hasText: '音频格式不受支持' }).waitFor();
    assert.equal(tests.length, 1);
    assert.equal(tests[0].filename, 'clip.wav');
    assert.equal(Buffer.from(tests[0].audio_base64, 'base64').toString(), 'RIFF....WAVEfmt ');
    assert.equal('api_key' in tests[0], false);

    testMode = 'ok';
    await testDialog.getByRole('button', { name: '开始转录' }).click();
    await testDialog.getByRole('status').filter({ hasText: '你好, 世界' }).waitFor();
    await testDialog.getByText('whisper-1', { exact: false }).first().waitFor();
    assert.equal(tests.length, 2);
    await page.screenshot({ path: path.join(artifacts, 'asr-test.png'), animations: 'disabled' });
    await testDialog.getByRole('button', { name: '关闭' }).click();
    await testDialog.waitFor({ state: 'hidden' });
    assert.deepEqual(errors, []);
    console.log('PASS: ASR 配置新增, 密钥脱敏, 转录测试失败与成功呈现');
  } finally {
    await runtime.closeContexts();
    await runtime.releaseServer(server);
  }

}

async function model_dimensions(runtime) {
  /** 验证清空维度后的 PATCH 语义和重新打开表单的显示 */






  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  const server = await runtime.vite({ root, server: { host: '127.0.0.1', port: 0 } });
  let browser;
  try {
    await runtime.listen(server);
    const origin = 'http://127.0.0.1:' + server.httpServer.address().port;
    const base = 'http://127.0.0.1:19871';
    const config = { name: 'default', model: 'embedding-model', api_key: '****test', dimensions: 1024 };
    const patches = [];
    browser = await runtime.browser;
    const page = await browser.newPage();
    await page.route(origin + '/ui-config.json', (route) => route.fulfill({ json: { backend_api: base, control_api: base, chat_api: base } }));
    await page.route(base + '/**', (route) => {
      const request = route.request(), url = new URL(request.url());
      let json = { ok: true };
      if (url.pathname === '/config/models') json = url.searchParams.get('type') === 'embedding' ? { default: config } : {};
      if (request.method() === 'PATCH') {
        const patch = request.postDataJSON();
        patches.push(patch);
        Object.assign(config, patch);
      }
      return route.fulfill({ json, headers: { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true' } });
    });
    await page.routeWebSocket(base.replace('http', 'ws') + '/**', () => {});
    await mockConfigVersions(page, origin, () => JSON.stringify({config}));
  await page.goto(origin + '/models');
    await page.getByRole('button', { name: 'Embedding 配置', exact: true }).click();
    await page.getByRole('button', { name: '编辑', exact: true }).click();
    const dimensions = page.getByPlaceholder('留空使用模型默认维度，不发送 dimensions 参数');
    assert.equal(await dimensions.inputValue(), '1024');
    await dimensions.fill('');
    await page.getByRole('button', { name: '保存修改', exact: true }).click();
    await page.getByRole('dialog').waitFor({ state: 'hidden' });
    assert.equal(patches[0].dimensions, null);
    await page.getByRole('button', { name: '编辑', exact: true }).click();
    assert.equal(await dimensions.inputValue(), '');
    await dimensions.fill('768');
    await page.getByRole('button', { name: '保存修改', exact: true }).click();
    await page.getByRole('dialog').waitFor({ state: 'hidden' });
    assert.equal(patches[1].dimensions, 768);
    console.log('PASS: 清空维度提交 null, 重新打开保持空白, 数值填写仍正常');
  } finally {
    if (browser) await runtime.closeContexts();
    await runtime.releaseServer(server);
  }

}

async function shared_llm_editor(runtime) {
  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  const server = await runtime.vite({ root, server: { host: '127.0.0.1', port: 0 } });
  try {
    await runtime.listen(server);
    const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
    const context = await runtime.browser.newContext({ viewport: { width: 1280, height: 900 } });
    const secretKey = 'sk-' + 'x'.repeat(193) + 'last';
    const models = { default: { name: 'default', model: 'offline', api_key: '*'.repeat(secretKey.length - 4) + 'last', context_window: 4096,
      history_ratio: 0.6, context_strategy: 'summarize', context_threshold: 0.9, truncation_floor: 0.3,
      summary_keep_recent_turns: 3, thinking_fields: ['reasoning_effort'], thinking_levels: ['low', 'high'],
      supports_visual_input: false, lock_api_key: true } };
    const writes = [];
    const fixture = createBenchmarkFixture(1);
    let reloads = 0;
    let revealCalls = 0;
    let failReveal = false;
    let failNext = false;
    await context.route(`${origin}/ui-config.json`, (route) => route.fulfill({ json: {
      backend_api: 'http://127.0.0.1:19870', control_api: 'http://127.0.0.1:19871', chat_api: 'http://127.0.0.1:19872',
    } }));
    await context.route(/^http:\/\/127\.0\.0\.1:1987[012]\//, (route) => {
      const request = route.request(), url = new URL(request.url()), pathname = url.pathname;
      const headers = { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true',
        'Access-Control-Allow-Headers': '*', 'Access-Control-Allow-Methods': '*' };
      if (request.method() === 'OPTIONS') return route.fulfill({ status: 204, headers });
      if (pathname === '/config/models/llm/default/api-key') {
        assert.equal(request.method(), 'POST');
        revealCalls++;
        return route.fulfill({ status: failReveal ? 503 : 200, headers,
          json: failReveal ? { error: '模拟密钥读取失败' } : { api_key: secretKey } });
      }
      if (['POST', 'PATCH', 'PUT'].includes(request.method()) && (pathname.startsWith('/config/models/llm/') || pathname.startsWith('/api/chat/models'))) {
        if (failNext) {
          failNext = false;
          return route.fulfill({ status: 400, headers, json: { ok: false, error: '模拟保存失败' } });
        }
        const config = request.postDataJSON();
        writes.push({ pathname, config });
        models[config.name] = { ...models[config.name], ...config };
        return route.fulfill({ headers, json: { ok: true, refreshed_conversations: 1, deferred_conversations: 2 } });
      }
      if (pathname === '/api/config/reload') reloads += 1;
      const responses = {
        '/config/models': url.searchParams.get('type') === 'llm' ? models : {},
        '/api/chat/models': { models: Object.keys(models) }, '/api/chat/models/detail': { ok: true, models },
        '/api/chat/settings': { default_model: 'default', revision: 'chat-default-v1' },
        '/api/chat/model': { ok: true, model: 'default', available: true },
        '/api/chat/health': { ok: true }, '/api/chat/plugins': { plugins: [] },
        '/api/chat/conversations/page': conversationPageResponse(route.request().url(), fixture.conversations), '/api/chat/turns': { turns: fixture.turns },
        '/api/chat/turns/page': turnPageResponse(route.request().url(), fixture.turns),
        '/api/chat/runs': { ok: true, runs: [], next_cursor: null }, '/api/projects': { projects: [] },
        '/api/health': { running: true, adapters: {} }, '/status': { running: true },
        '/api/config/reload': { ok: true, platforms: [], edictum_sessions: [] },
      };
      return route.fulfill({ headers, json: responses[pathname] ?? emptyConfigResponse(route.request().url()) });
    });
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', (error) => errors.push(error.message));
    await page.routeWebSocket(/^ws:\/\/127\.0\.0\.1:1987[02]\//, () => {});
    await mockConfigVersions(page, origin, () => JSON.stringify(models));
    await page.goto(`${origin}/models`);
    const keyField = page.getByRole('textbox', { name: 'API Key default', exact: true });
    await keyField.waitFor();
    assert.equal(await keyField.inputValue(), '•'.repeat(secretKey.length));
    assert.equal(revealCalls, 0);
    await page.getByRole('button', { name: '显示 API Key default', exact: true }).click();
    await page.waitForFunction((expected) => document.querySelector('input[aria-label="API Key default"]')?.value === expected, secretKey);
    assert.equal(revealCalls, 1);
    for (const width of [1280, 375]) {
      await page.setViewportSize({ width, height: 900 });
      const bounds = await keyField.evaluate((element) => {
        const card = element.closest('.glass-card').getBoundingClientRect();
        const eye = element.parentElement.querySelector('button').getBoundingClientRect();
        return { cardRight: card.right, eyeRight: eye.right, width: document.documentElement.clientWidth,
          pageWidth: document.documentElement.scrollWidth };
      });
      assert.ok(bounds.eyeRight <= bounds.cardRight && bounds.pageWidth <= bounds.width + 1);
    }
    await page.getByRole('button', { name: '隐藏 API Key default', exact: true }).click();
    assert.equal(await keyField.inputValue(), '•'.repeat(secretKey.length));
    failReveal = true;
    await page.getByRole('button', { name: '显示 API Key default', exact: true }).click();
    await page.getByText('读取 API Key 失败，请重试', { exact: true }).waitFor();
    assert.equal(await keyField.inputValue(), '•'.repeat(secretKey.length));
    failReveal = false;
    await page.getByRole('button', { name: '显示 API Key default', exact: true }).click();
    await page.waitForFunction((expected) => document.querySelector('input[aria-label="API Key default"]')?.value === expected, secretKey);
    await page.getByRole('button', { name: 'Embedding 配置', exact: true }).click();
    await page.getByRole('button', { name: 'LLM 配置', exact: true }).click();
    assert.equal(await keyField.inputValue(), '•'.repeat(secretKey.length));
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.getByRole('button', { name: '编辑', exact: true }).click();
    let dialog = page.getByRole('dialog', { name: '编辑模型: default', exact: true });
    assert.equal(await dialog.getByLabel('API Key').inputValue(), '');
    assert.equal(await dialog.getByLabel('保留最近轮数').inputValue(), '3');
    await dialog.getByLabel('上下文窗口').fill('8000');
    await dialog.getByLabel('历史比例').fill('0.75');
    await dialog.getByLabel('触发比例').fill('0.8');
    await dialog.getByLabel('压缩目标比例').fill('0.9');
    await dialog.getByRole('button', { name: '保存修改', exact: true }).click();
    await dialog.getByRole('alert').filter({ hasText: '上下文比例必须满足' }).waitFor();
    assert.equal(writes.length, 0);
    await dialog.getByLabel('压缩目标比例').fill('0.4');
    await dialog.getByLabel('保留最近轮数').fill('4');
    await dialog.getByText('历史预算: 6,000', { exact: true }).waitFor();
    await dialog.getByRole('button', { name: '保存修改', exact: true }).click();
    await dialog.waitFor({ state: 'hidden' });
    assert.equal('api_key' in writes[0].config, false);
    assert.equal(writes[0].config.context_strategy, 'summarize');
    assert.equal(writes[0].config.summary_keep_recent_turns, 4);
    assert.equal(writes[0].config.lock_api_key, true);
    await page.getByRole('button', { name: '编辑', exact: true }).click();
    assert.equal(await dialog.getByLabel('上下文窗口').inputValue(), '8000');
    await dialog.getByRole('button', { name: '取消', exact: true }).click();
    await page.getByRole('button', { name: '新增配置', exact: true }).click();
    dialog = page.getByRole('dialog', { name: '新增模型配置', exact: true });
    await dialog.getByLabel('配置名称').fill('platform-new');
    await dialog.getByLabel('模型 ID').fill('offline-new');
    await dialog.getByRole('button', { name: '创建', exact: true }).click();
    await dialog.waitFor({ state: 'hidden' });
    assert.equal(writes[1].config.context_strategy, 'sliding');
    assert.equal(writes[1].config.context_threshold, 0.8);

    await page.goto(`${origin}/chat`);
    await openConversation(page, fixture.conversations[0].title);
    await page.getByTitle('对话设置', { exact: true }).click();
    const settings = page.getByRole('dialog', { name: '对话设置', exact: true });
    await settings.getByRole('button', { name: '模型管理', exact: true }).click();
    await settings.locator('.glass-card').filter({ hasText: 'default' }).getByTitle('编辑', { exact: true }).click();
    dialog = page.getByRole('dialog', { name: '编辑模型: default', exact: true });
    assert.equal(await dialog.getByLabel('上下文窗口').inputValue(), '8000');
    assert.equal(await dialog.getByLabel('保留最近轮数').inputValue(), '4');
    assert.equal(await dialog.getByLabel('配置名称').isDisabled(), true);
    assert.equal(await dialog.getByLabel('API Key').inputValue(), '');
    await dialog.getByLabel('上下文处理策略').selectOption('mid_truncate');
    assert.equal(await dialog.getByLabel('保留最近轮数').count(), 0);
    failNext = true;
    await dialog.getByRole('button', { name: '保存修改', exact: true }).click();
    await dialog.getByRole('alert').waitFor();
    assert.equal(await dialog.getByLabel('上下文处理策略').inputValue(), 'mid_truncate');
    await dialog.getByRole('button', { name: '保存修改', exact: true }).click();
    await dialog.waitFor({ state: 'hidden' });
    assert.equal(writes[2].pathname, '/api/chat/models/default');
    assert.equal(writes[2].config.context_strategy, 'mid_truncate');
    assert.equal('api_key' in writes[2].config, false);
    assert.equal(reloads, 1);
    await settings.getByRole('button', { name: '新增模型', exact: true }).click();
    dialog = page.getByRole('dialog', { name: '新增模型配置', exact: true });
    await dialog.getByLabel('配置名称').fill('chat-new');
    await dialog.getByLabel('模型 ID').fill('offline-chat');
    await dialog.getByLabel('上下文处理策略').selectOption('summarize');
    assert.equal(await dialog.getByLabel('保留最近轮数').inputValue(), '6');
    await dialog.getByRole('button', { name: '创建', exact: true }).click();
    await dialog.waitFor({ state: 'hidden' });
    assert.equal(writes[3].config.context_strategy, 'summarize');
    models['step-5'] = { name: 'step-5', model: 'external-step' };
    models.default.summary_keep_recent_turns = 2;
    models.default.context_strategy = 'summarize';
    await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
    await settings.getByText('step-5', { exact: true }).waitFor();
    await settings.locator('.glass-card').filter({ hasText: 'default' }).getByTitle('编辑', { exact: true }).click();
    dialog = page.getByRole('dialog', { name: '编辑模型: default', exact: true });
    assert.equal(await dialog.getByLabel('保留最近轮数').inputValue(), '2');
    await dialog.getByLabel('历史比例').fill('0.5');
    models.default.history_ratio = 0.7;
    models['step-5'].model = 'external-updated';
    await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
    await settings.getByText('external-updated', { exact: true }).waitFor();
    assert.equal(await dialog.getByLabel('历史比例').inputValue(), '0.5');
    await dialog.getByRole('button', { name: '取消', exact: true }).click();
    delete models['step-5'];
    await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
    await settings.getByText('step-5', { exact: true }).waitFor({ state: 'hidden' });
    assert.deepEqual(errors, []);
    console.log('PASS: 共用 LLM 表单; 外部新增, 修改与删除自动同步, 未保存输入保留, Chat 热加载');
  } finally {
    await runtime.closeContexts();
    await runtime.releaseServer(server);
  }
}

async function chat_model_binding(runtime) {
  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  const server = await runtime.vite({ root, server: { host: '127.0.0.1', port: 0 } });
  try {
    await runtime.listen(server);
    const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
    const context = await runtime.browser.newContext({ viewport: { width: 1400, height: 1100 } });
    const page = await context.newPage();
    page.setDefaultTimeout(15000);
    const errors = [];
    page.on('pageerror', (error) => errors.push(error.message));
    const cors = { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true',
      'Access-Control-Allow-Headers': 'Content-Type', 'Access-Control-Allow-Methods': 'GET,POST,PUT,DELETE,OPTIONS' };
    const models = Object.fromEntries(['A', 'B'].map((name) => [name, { name, model: `model-${name}`, supports_visual_input: true }]));
    const bindings = { a: 'A', b: 'A' };
    const rows = ['a', 'b'].map((id, index) => ({ conversation_id: id, title: `会话 ${id.toUpperCase()}`, model: 'A', last_at: 10 - index, turn_count: id === 'a' ? 1 : 0, project_id: null }));
    const variant = (model, index, answer) => ({ variant_index: index, thinking: null, answer, tool_calls: [], segments: [{ type: 'content', content: answer }], created_at: 1,
      response_metadata: { status: 'completed', model, model_id: `model-${model}` } });
    const original = variant('A', 0, 'A 的旧回复');
    const turns = { a: [{ id: 1, turn_index: 0, user_input: '原问题', answer: original.answer, thinking: null, tool_calls: [], segments: original.segments,
      created_at: 1, active_variant: 0, variant_count: 1, variants: [original], response_metadata: original.response_metadata }], b: [] };
    const sockets = new Map();
    const sequences = new Map();
    const emit = (cid, event) => {
      const seq = (sequences.get(cid) ?? 0) + 1; sequences.set(cid, seq);
      for (const socket of sockets.get(cid) ?? []) socket.send(JSON.stringify({ ...event, seq, stream_id: 'binding-run', conversation_id: cid }));
    };
    let defaults = { default_model: 'A', revision: 'r1' };
    let holdSwitch = true, rejectSwitch = false, releaseSwitch, enteredSwitch;
    const switching = new Promise((resolve) => { enteredSwitch = resolve; });
    const preloads = [], requests = [];
    await context.route(origin + '/ui-config.json', (route) => route.fulfill({ json: {
      backend_api: 'http://127.0.0.1:19870', control_api: 'http://127.0.0.1:19871', chat_api: 'http://127.0.0.1:19872',
    } }));
    await context.route(/^http:\/\/127\.0\.0\.1:1987[012]\//, async (route) => {
      const request = route.request(), url = new URL(request.url()), method = request.method(), path = url.pathname;
      if (method === 'OPTIONS') return route.fulfill({ status: 204, headers: cors });
      const reply = (json, status = 200) => route.fulfill({ status, json, headers: cors });
      if (path === '/api/chat/settings') {
        if (method === 'PUT') {
          const payload = request.postDataJSON();
          assert.equal(payload.expected_revision, defaults.revision);
          defaults = { default_model: payload.default_model, revision: 'r2' };
        }
        return reply(defaults);
      }
      if (path === '/api/chat/model') {
        const cid = url.searchParams.get('conversation');
        if (method === 'PUT') {
          const payload = request.postDataJSON();
          enteredSwitch();
          if (holdSwitch) await new Promise((resolve) => { releaseSwitch = resolve; });
          if (rejectSwitch) { rejectSwitch = false; return reply({ error: '模拟绑定保存失败' }, 500); }
          assert.equal(payload.expected_model, bindings[cid]);
          bindings[cid] = payload.model;
          emit(cid, { type: 'model_changed', model: payload.model, available: true });
        }
        return reply({ ok: true, model: bindings[cid], available: Boolean(models[bindings[cid]]) });
      }
      if (path === '/api/chat/conversations/preload') {
        const payload = request.postDataJSON(), cid = `draft-${preloads.length}`;
        preloads.push(payload); bindings[cid] = payload.model; turns[cid] = [];
        return reply({ ok: true, conversation_id: cid });
      }
      if (path === '/api/chat/retry' || path === '/api/chat/send') {
        const payload = request.postDataJSON(), cid = payload.conversation, model = bindings[cid];
        assert.equal(payload.expected_model, model);
        requests.push({ kind: path.endsWith('retry') ? 'retry' : 'send', model, payload });
        let turn;
        if (path.endsWith('retry')) {
          turn = turns[cid].at(-1);
          const next = variant(model, turn.variants.length, `${model} 的重新生成`);
          Object.assign(turn, { answer: next.answer, response_metadata: next.response_metadata, segments: next.segments,
            active_variant: next.variant_index, variant_count: turn.variant_count + 1, variants: [...turn.variants, next] });
        } else {
          const next = variant(model, 0, payload.text === '错误验证' ? '部分输出' : `${model} 的新回复`);
          if (payload.text === '错误验证') next.response_metadata = { status: 'failed', model, model_id: `model-${model}`,
            error_text: '请求失败 [HTTP 429 / rate_limit]: 服务商限流', error: { code: 'rate_limit', message: '服务商限流', status: 429 } };
          turn = { id: 100 + turns[cid].length, turn_index: turns[cid].length, user_input: payload.text,
            answer: next.answer, thinking: null, segments: next.segments, tool_calls: [], created_at: 2,
            active_variant: 0, variant_count: 1, variants: [next], response_metadata: next.response_metadata };
          turns[cid].push(turn);
        }
        await reply({ ok: true, conversation_id: cid, turn_id: turn.id, turn_index: turn.turn_index, variant_index: turn.active_variant });
        if (payload.text === '错误验证') emit(cid, { type: 'content_delta', delta: '部分输出' });
        emit(cid, { type: payload.text === '错误验证' ? 'error' : 'turn_done', error: turn.response_metadata.error_text, answer: turn.answer, turn_id: turn.id, turn_index: turn.turn_index,
          variant_index: turn.active_variant, variant_count: turn.variant_count, response_metadata: turn.response_metadata });
        return;
      }
      if (path === '/api/chat/turns/variant') {
        const payload = request.postDataJSON(), turn = turns[payload.conversation].find((item) => item.turn_index === payload.turn_index);
        const selected = turn.variants[payload.variant_index];
        Object.assign(turn, { active_variant: selected.variant_index, answer: selected.answer, segments: selected.segments, response_metadata: selected.response_metadata });
        return reply({ ok: true, turn });
      }
      const cid = url.searchParams.get('conversation');
      const responses = {
        '/api/chat/health': { ok: true }, '/api/chat/models': { models: ['A', 'B'] }, '/api/chat/models/detail': { models },
        '/api/chat/conversations/page': conversationPageResponse(url, rows.map((row) => ({ ...row, model: bindings[row.conversation_id] }))),
        '/api/chat/turns/page': turnPageResponse(url, turns[cid] ?? []), '/api/chat/turns': { turns: turns[cid] ?? [] },
        '/api/chat/plugins': { plugins: [] }, '/api/projects': { projects: [] }, '/api/chat/runs': { ok: true, runs: [], next_cursor: null },
        '/api/chat/default-agent-prompt': { ok: true, default_agent_prompt_enabled: false },
        '/api/chat/command-history': { ok: true, command_history_enabled: false }, '/api/chat/max-iterations': { ok: true, max_iterations: 10 },
        '/api/chat/approval': { ok: true, available: false, mode: 'user', revision: 0, plan_mode: false },
      };
      return reply(responses[path] ?? emptyConfigResponse(url));
    });
    await context.routeWebSocket(/ws:\/\/127\.0\.0\.1:1987[012]\//, (ws) => {
      const url = new URL(ws.url());
      if (url.pathname !== '/ws/chat') return;
      const cid = url.searchParams.get('conversation');
      const group = sockets.get(cid) ?? new Set(); group.add(ws); sockets.set(cid, group);
      ws.onClose(() => group.delete(ws));
      emit(cid, { type: 'snapshot', state: 'idle', active_turn_id: null, turns: turns[cid] ?? [], pending_user_inputs: [], model: bindings[cid] });
    });
    await mockConfigVersions(page, origin, () => '{}');
    await page.goto(origin + '/chat');
    await page.getByText('会话 A', { exact: true }).first().click();
    await page.getByTitle('展开选项', { exact: true }).click();
    await page.getByRole('button', { name: 'A', exact: true }).waitFor();
    await page.getByPlaceholder(/输入消息/).fill('下一条 B');
    await page.getByRole('button', { name: 'A', exact: true }).click();
    await page.getByRole('button', { name: 'B', exact: true }).last().click();
    await switching;
    assert.equal(await page.getByTitle('发送', { exact: true }).isDisabled(), true);
    assert.equal(requests.length, 0);
    holdSwitch = false; releaseSwitch();
    await page.getByText('正在确认会话模型…', { exact: true }).waitFor({ state: 'hidden' });
    await page.getByRole('button', { name: 'B', exact: true }).waitFor();
    await page.getByTitle('重试', { exact: true }).click();
    await page.locator('[data-response-model="B"]').waitFor();
    assert.deepEqual(requests.map((item) => [item.kind, item.model]), [['retry', 'B']]);
    await page.getByTitle('上一个回复', { exact: true }).click();
    await page.locator('[data-response-model="A"]').waitFor();
    assert.equal(bindings.a, 'B');
    await page.getByTitle('下一个回复', { exact: true }).click();
    await page.getByTitle('发送', { exact: true }).click();
    await page.getByText('B 的新回复', { exact: true }).waitFor();
    await page.getByTitle('重试', { exact: true }).last().waitFor();
    assert.equal(requests.at(-1).model, 'B');
    await page.getByPlaceholder(/输入消息/).fill('错误验证');
    await page.getByTitle('发送', { exact: true }).click();
    await page.getByText(/请求失败 \[HTTP 429 \/ rate_limit\]: 服务商限流/).waitFor();
    rejectSwitch = true;
    await page.getByRole('button', { name: 'B', exact: true }).click();
    await page.getByRole('button', { name: 'A', exact: true }).last().click();
    await page.getByText(/模型切换失败/).waitFor();
    assert.equal(bindings.a, 'B');
    await page.getByText('会话 B', { exact: true }).first().click();
    await page.getByRole('button', { name: 'A', exact: true }).waitFor();
    await page.getByTitle('对话设置', { exact: true }).click();
    const settings = page.getByRole('dialog');
    const defaultField = settings.getByText('整体默认模型', { exact: true }).locator('..');
    await defaultField.getByRole('button').click();
    await page.getByRole('button', { name: 'B', exact: true }).last().click();
    await page.getByText('整体默认模型已保存, 仅影响之后创建的会话', { exact: true }).waitFor();
    assert.equal(bindings.b, 'A');
    const screenshots = path.join(root, 'test-results', 'chat-model-binding');
    await fs.mkdir(screenshots, { recursive: true });
    await page.screenshot({ path: path.join(screenshots, 'default-settings.png') });
    await page.keyboard.press('Escape');
    await settings.waitFor({ state: 'hidden' });
    const newPreload = page.waitForResponse((response) => new URL(response.url()).pathname === '/api/chat/conversations/preload');
    await page.getByRole('button', { name: '新建对话', exact: true }).click();
    await page.getByRole('button', { name: 'B', exact: true }).waitFor();
    await newPreload;
    assert.equal(preloads.at(-1).model, 'B');
    await page.reload();
    await page.getByText('会话 A', { exact: true }).first().click();
    await page.getByText(/请求失败 \[HTTP 429 \/ rate_limit\]: 服务商限流/).waitFor();
    await page.getByText('会话 B', { exact: true }).first().click();
    await page.getByTitle('展开选项', { exact: true }).click();
    await page.getByRole('button', { name: 'A', exact: true }).waitFor();
    const second = await context.newPage();
    second.setDefaultTimeout(15000);
    second.on('pageerror', (error) => errors.push(error.message));
    await second.goto(origin + '/chat');
    await second.getByText('会话 B', { exact: true }).first().click();
    await second.getByTitle('展开选项', { exact: true }).click();
    await second.getByRole('button', { name: 'A', exact: true }).click();
    await second.getByRole('button', { name: 'B', exact: true }).last().click();
    await page.getByRole('button', { name: 'B', exact: true }).waitFor();
    assert.equal(bindings.b, 'B');
    await page.screenshot({ path: path.join(screenshots, 'tabs-binding.png') });
    delete models.B;
    await page.reload();
    await page.getByText('会话 B', { exact: true }).first().click();
    await page.getByText('模型 B 不可用, 请切换模型或检查模型配置', { exact: true }).waitFor();
    await page.getByPlaceholder(/输入消息/).fill('不能偷偷换模型执行');
    assert.equal(await page.getByTitle('发送', { exact: true }).isDisabled(), true);
    assert.equal(bindings.b, 'B');
    delete models.A;
    await page.reload();
    await page.getByText('会话 B', { exact: true }).first().click();
    await page.getByText('模型 B 不可用, 请切换模型或检查模型配置', { exact: true }).waitFor();
    assert.deepEqual(errors, []);
    console.log('PASS: 会话绑定切换等待确认, 普通发送/重试使用当前模型, 旧变体标记, 保存失败恢复, 整体默认仅影响新会话, 刷新回填, 双标签同步和删除后明确不可用');
  } finally {
    await runtime.closeContexts(); await runtime.releaseServer(server);
  }
}

async function img_models(runtime) {
  // 验证图片配置新增, 重载, 编辑, 删除和测试预览, 不调用真实生成服务
  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  await fs.mkdir(path.join(root, 'test-results'), { recursive: true });
  const server = await runtime.vite({ root, server: { host: '127.0.0.1', port: 0 } });
  await runtime.listen(server);
  const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
  const context = await runtime.browser.newContext({ viewport: { width: 1280, height: 1000 } });
  const models = { llm: {}, embedding: {}, rerank: {}, asr: {}, img: {} };
  const writes = [], tests = [], errors = [];
  let revision = 1;
  let testMode = 'error';
  const tinyPng = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEElEQVR4nGP8zwACTGCSAQANHQEDgslx/wAAAABJRU5ErkJggg==';
  try {
    await context.route(`${origin}/ui-config.json`, (route) => route.fulfill({ json: {
      backend_api: 'http://127.0.0.1:19870', control_api: 'http://127.0.0.1:19871', chat_api: 'http://127.0.0.1:19872',
    } }));
    await context.route(/^http:\/\/127\.0\.0\.1:1987[012]\//, async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      const headers = { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true',
        'Access-Control-Allow-Headers': 'Content-Type', 'Access-Control-Allow-Methods': 'GET, POST, PATCH, DELETE, OPTIONS' };
      if (request.method() === 'OPTIONS') return route.fulfill({ status: 204, headers });
      if (url.pathname.endsWith('/test') && request.method() === 'POST') {
        tests.push(request.postDataJSON());
        return route.fulfill({ status: testMode === 'error' ? 502 : 200, headers, json: testMode === 'error'
          ? { ok: false, error: '图片服务暂时不可用' }
          : { ok: true, model: 'nai-diffusion-4-5-full', elapsed_ms: 100, images: [{ data_url: tinyPng, width: 1, height: 1 }] } });
      }
      const match = url.pathname.match(/^\/config\/models\/(\w+)\/([^/]+)$/);
      if (match && ['POST', 'PATCH', 'DELETE'].includes(request.method())) {
        const [, type, encodedName] = match;
        const name = decodeURIComponent(encodedName);
        const body = request.method() === 'DELETE' ? {} : request.postDataJSON();
        writes.push({ method: request.method(), type, name, body });
        if (request.method() === 'DELETE') delete models[type][name];
        else {
          const nextName = body.name || name;
          models[type][nextName] = { ...models[type][name], ...body, api_key: '********key' };
          if (nextName !== name) delete models[type][name];
        }
        revision++;
        return route.fulfill({ headers, json: { ok: true } });
      }
      if (url.pathname === '/config/models') return route.fulfill({ headers, json: models[url.searchParams.get('type')] || {} });
      return route.fulfill({ headers, json: url.pathname === '/status' ? { running: true } : emptyConfigResponse(request.url()) });
    });
    const page = await context.newPage();
    page.on('pageerror', (error) => errors.push(error.message));
    await page.routeWebSocket('ws://127.0.0.1:19870/ws/status**', () => {});
    await mockConfigVersions(page, origin, () => String(revision));
    await page.goto(`${origin}/models`);
    await page.getByRole('button', { name: '图片模型', exact: true }).click();
    await page.getByRole('button', { name: '新增配置', exact: true }).click();
    let dialog = page.getByRole('dialog');
    await dialog.getByLabel('配置名称').fill('paint');
    await dialog.getByLabel('模型 ID').fill('nai-diffusion-4-5-full');
    await dialog.getByLabel('请求协议').selectOption('novelai');
    await dialog.getByLabel('生成请求地址').fill('https://image.test/exact/?v=1');
    await dialog.getByLabel('编辑请求地址').fill('https://image.test/edit');
    await dialog.getByLabel('API Key').fill('img-secret');
    await dialog.locator('summary').filter({ hasText: '扩展参数 (JSON)' }).click();
    await dialog.getByLabel('扩展参数 (JSON)').fill('[]');
    await dialog.getByRole('button', { name: '创建', exact: true }).click();
    await dialog.getByRole('alert').waitFor();
    assert.equal(writes.length, 0);
    await dialog.getByLabel('扩展参数 (JSON)').fill('');
    await dialog.getByLabel('分辨率设置', { exact: true }).selectOption('pixels');
    await dialog.getByLabel('常用尺寸', { exact: true }).selectOption('512x768');
    await dialog.getByLabel('随机种子模式', { exact: true }).selectOption('fixed');
    await dialog.getByLabel('固定随机种子', { exact: true }).fill('0');
    await dialog.getByLabel('采样步数', { exact: true }).fill('20');
    await dialog.getByLabel('提示词引导强度', { exact: true }).fill('5');
    await dialog.getByLabel('生成数量', { exact: true }).fill('2');
    const positiveInjection = dialog.getByLabel('在提示词末尾注入默认正向提示词', { exact: true });
    const negativeInjection = dialog.getByLabel('在负向提示词开头注入默认负向提示词', { exact: true });
    assert.equal(await positiveInjection.isChecked(), false);
    assert.equal(await negativeInjection.isChecked(), false);
    await positiveInjection.check();
    await negativeInjection.check();
    await page.screenshot({ path: path.join(root, 'test-results', 'img-nai-default-prompts.png'), animations: 'disabled' });
    const sampler = dialog.getByLabel('采样器', { exact: true });
    assert.equal(await sampler.getAttribute('role'), 'combobox');
    assert.equal(await dialog.locator('datalist').count(), 0);
    await sampler.click();
    await page.getByRole('option', { name: 'k_euler_ancestral', exact: true }).click();
    assert.equal(await sampler.inputValue(), 'k_euler_ancestral');
    await sampler.fill('custom_sampler_v2');
    await sampler.press('Enter');
    assert.equal(await sampler.inputValue(), 'custom_sampler_v2');
    assert.equal(writes.length, 0);
    await sampler.click();
    await page.getByRole('option', { name: '使用默认值', exact: true }).click();
    assert.equal(await sampler.inputValue(), '');
    await sampler.fill('custom_sampler_v2');
    await sampler.press('Escape');
    assert.equal(await sampler.getAttribute('aria-expanded'), 'false');
    assert.equal(await dialog.isVisible(), true);
    await dialog.locator('summary').filter({ hasText: 'NAI 高级参数' }).click();
    const schedule = dialog.getByLabel('噪声调度', { exact: true });
    await schedule.click();
    await page.getByRole('option', { name: 'karras', exact: true }).click();
    assert.equal(await schedule.inputValue(), 'karras');
    await schedule.fill('custom_noise_v2');
    await schedule.press('ArrowDown');
    await schedule.press('Enter');
    assert.equal(await schedule.inputValue(), '');
    await schedule.fill('custom_noise_v2');
    await schedule.press('Enter');
    assert.equal(await schedule.inputValue(), 'custom_noise_v2');
    await dialog.locator('summary').filter({ hasText: 'NAI 高级参数' }).click();
    await dialog.locator('summary').filter({ hasText: '多角色提示词' }).click();
    await dialog.getByRole('button', { name: '添加角色', exact: true }).click();
    await dialog.getByLabel('角色 1 提示词', { exact: true }).fill('woman, blue hair');
    await dialog.getByRole('button', { name: '添加角色 1 位置', exact: true }).click();
    await dialog.getByLabel('角色 1 位置 1 X', { exact: true }).fill('0.25');
    await dialog.locator('summary').filter({ hasText: '多角色提示词' }).click();
    await dialog.locator('summary').filter({ hasText: '扩展参数 (JSON)' }).click();
    await dialog.getByText('绘图默认参数', { exact: true }).evaluate((element) => element.scrollIntoView({ block: 'start' }));
    await page.screenshot({ path: path.join(root, 'test-results', 'img-params-form.png'), animations: 'disabled' });
    await sampler.click();
    await page.getByRole('listbox').waitFor();
    await page.screenshot({ path: path.join(root, 'test-results', 'img-sampler-select.png'), animations: 'disabled' });
    await sampler.press('Escape');
    await dialog.getByLabel('配置名称').scrollIntoViewIfNeeded();
    await page.screenshot({ path: path.join(root, 'test-results', 'img-model-form.png'), animations: 'disabled' });
    await dialog.getByRole('button', { name: '创建', exact: true }).click();
    await dialog.waitFor({ state: 'hidden' });
    assert.equal(writes[0].body.base_url, 'https://image.test/exact/?v=1');
    assert.equal(writes[0].body.default_params.seed, 0);
    assert.deepEqual(writes[0].body.default_params.size, [512, 768]);
    assert.equal(writes[0].body.default_params.n, 2);
    assert.equal(writes[0].body.default_params.steps, 20);
    assert.equal(writes[0].body.default_params.inject_default_positive_prompt, true);
    assert.equal(writes[0].body.default_params.inject_default_negative_prompt, true);
    assert.equal(writes[0].body.default_params.sampler, 'custom_sampler_v2');
    assert.equal(writes[0].body.default_params.noise_schedule, 'custom_noise_v2');
    assert.equal(writes[0].body.default_params.use_coords, true);
    assert.deepEqual(writes[0].body.default_params.characters[0].centers, [[0.25, 0.5]]);
    await page.getByRole('heading', { name: 'paint', exact: true }).waitFor();
    await page.getByRole('button', { name: '测试生成 paint', exact: true }).click();
    dialog = page.getByRole('dialog');
    await dialog.getByRole('button', { name: '开始生成', exact: true }).click();
    await dialog.getByRole('alert').filter({ hasText: '图片服务暂时不可用' }).waitFor();
    assert.equal(tests.length, 1);
    assert.deepEqual(Object.keys(tests[0]), ['prompt']);
    testMode = 'ok';
    await dialog.getByRole('button', { name: '开始生成', exact: true }).click();
    await dialog.getByRole('img', { name: '测试生成图片 1' }).waitFor();
    await dialog.getByRole('img', { name: '测试生成图片 1' }).evaluate((image) => image.decode());
    await page.screenshot({ path: path.join(root, 'test-results', 'img-models.png'), animations: 'disabled' });
    await dialog.getByRole('button', { name: '关闭', exact: true }).click();
    await page.getByRole('button', { name: '编辑', exact: true }).click();
    dialog = page.getByRole('dialog');
    assert.equal(await dialog.getByLabel('API Key').inputValue(), '');
    await dialog.getByLabel('配置名称').fill('drawing');
    await dialog.getByLabel('编辑请求地址').fill('');
    assert.equal(await dialog.getByLabel('在提示词末尾注入默认正向提示词', { exact: true }).isChecked(), true);
    assert.equal(await dialog.getByLabel('在负向提示词开头注入默认负向提示词', { exact: true }).isChecked(), true);
    await dialog.getByRole('button', { name: '清空绘图默认值', exact: true }).click();
    assert.equal(await dialog.getByLabel('在提示词末尾注入默认正向提示词', { exact: true }).isChecked(), false);
    assert.equal(await dialog.getByLabel('在负向提示词开头注入默认负向提示词', { exact: true }).isChecked(), false);
    await dialog.getByRole('button', { name: '保存修改', exact: true }).click();
    await dialog.waitFor({ state: 'hidden' });
    assert.equal(writes[1].method, 'PATCH');
    assert.equal(writes[1].body.edit_base_url, null);
    assert.equal(writes[1].body.default_params, null);
    assert.equal('api_key' in writes[1].body, false);
    await page.reload();
    await page.getByRole('button', { name: '图片模型', exact: true }).click();
    await page.getByRole('heading', { name: 'drawing', exact: true }).waitFor();
    await page.getByRole('button', { name: '删除 drawing', exact: true }).click();
    const confirmation = page.getByRole('dialog', { name: '确认操作', exact: true });
    await confirmation.getByRole('button', { name: '确认', exact: true }).click();
    await page.getByRole('heading', { name: 'drawing', exact: true }).waitFor({ state: 'hidden' });
    assert.equal(writes.at(-1).method, 'DELETE');
    const legacyParams = { size: '832x1216', seed: 0, steps: 23, sm: false, use_coords: true,
      inject_default_positive_prompt: false, inject_default_negative_prompt: true,
      sampler: 'legacy_custom_sampler', noise_schedule: 'legacy_custom_noise',
      characters: [{ prompt: 'woman', negative_prompt: 'hat', centers: [[0.2, 0.5], [0.8, 0.5]] }],
      native: { parameters: { future: false } } };
    models.img.legacy = { model: 'nai-diffusion-4-5-full', api_type: 'novelai', name: 'legacy',
      base_url: 'https://image.test/exact', api_key: '****key', default_params: legacyParams };
    revision++;
    await page.reload();
    await page.getByRole('button', { name: '图片模型', exact: true }).click();
    await page.getByRole('button', { name: '编辑', exact: true }).click();
    dialog = page.getByRole('dialog');
    assert.equal(await dialog.getByLabel('图片宽度 (像素)', { exact: true }).inputValue(), '832');
    assert.equal(await dialog.getByLabel('图片高度 (像素)', { exact: true }).inputValue(), '1216');
    assert.equal(await dialog.getByLabel('固定随机种子', { exact: true }).inputValue(), '0');
    await dialog.getByLabel('请求协议').selectOption('openai_images');
    await dialog.getByText(/已有参数与当前协议不兼容/).waitFor();
    await dialog.getByLabel('请求协议').selectOption('novelai');
    assert.equal(await dialog.getByLabel('采样步数').inputValue(), '23');
    assert.equal(await dialog.getByLabel('在提示词末尾注入默认正向提示词', { exact: true }).isChecked(), false);
    assert.equal(await dialog.getByLabel('在负向提示词开头注入默认负向提示词', { exact: true }).isChecked(), true);
    assert.equal(await dialog.getByLabel('采样器', { exact: true }).inputValue(), 'legacy_custom_sampler');
    await dialog.locator('summary').filter({ hasText: 'NAI 高级参数' }).click();
    assert.equal(await dialog.getByLabel('SMEA', { exact: true }).getAttribute('data-value'), 'false');
    assert.equal(await dialog.getByLabel('噪声调度', { exact: true }).inputValue(), 'legacy_custom_noise');
    await dialog.locator('summary').filter({ hasText: '多角色提示词' }).click();
    assert.equal(await dialog.getByLabel('角色 1 位置 2 X').inputValue(), '0.8');
    await dialog.getByLabel('角色 1 提示词', { exact: true }).fill('woman, blue hair');
    await dialog.locator('summary').filter({ hasText: '扩展参数 (JSON)' }).click();
    const extras = await dialog.getByLabel('扩展参数 (JSON)').inputValue();
    await dialog.getByLabel('扩展参数 (JSON)').fill('{"steps":99}');
    await dialog.getByRole('button', { name: '保存修改', exact: true }).click();
    await dialog.getByRole('alert').filter({ hasText: '不要重复' }).waitFor();
    assert.equal(writes.length, 3);
    await dialog.getByLabel('扩展参数 (JSON)').fill(extras);
    await dialog.getByRole('button', { name: '保存修改', exact: true }).click();
    await dialog.waitFor({ state: 'hidden' });
    assert.deepEqual(writes.at(-1).body.default_params, { ...legacyParams,
      characters: [{ ...legacyParams.characters[0], prompt: 'woman, blue hair' }] });
    for (const apiType of ['seedream', 'openai_images']) {
      await page.getByRole('button', { name: '新增配置', exact: true }).click();
      dialog = page.getByRole('dialog');
      await dialog.getByLabel('配置名称').fill(apiType);
      await dialog.getByLabel('模型 ID').fill('image-model');
      await dialog.getByLabel('请求协议').selectOption(apiType);
      await dialog.getByLabel('生成请求地址').fill('https://image.test/exact');
      await dialog.getByLabel('API Key').fill('test-key');
      assert.equal(await dialog.getByLabel('采样步数', { exact: true }).count(), 0);
      assert.equal(await dialog.getByLabel('负向提示词', { exact: true }).count(), 0);
      assert.equal(await dialog.getByLabel('在提示词末尾注入默认正向提示词', { exact: true }).count(), 0);
      assert.equal(await dialog.getByLabel('在负向提示词开头注入默认负向提示词', { exact: true }).count(), 0);
      if (apiType === 'seedream') {
        assert.equal(await dialog.getByLabel('生成数量', { exact: true }).count(), 0);
        assert.equal(await dialog.getByLabel('输出格式', { exact: true }).count(), 0);
        await dialog.getByLabel('分辨率设置', { exact: true }).selectOption('value');
        await dialog.getByLabel('分辨率档位或尺寸值', { exact: true }).fill('2K');
        await dialog.getByLabel('随机种子模式', { exact: true }).selectOption('fixed');
        await dialog.getByLabel('固定随机种子', { exact: true }).fill('0');
      } else {
        assert.equal(await dialog.getByLabel('随机种子模式', { exact: true }).count(), 0);
        await dialog.getByLabel('分辨率设置', { exact: true }).selectOption('pixels');
        await dialog.getByLabel('常用尺寸', { exact: true }).selectOption('1024x1024');
        await dialog.getByLabel('生成数量', { exact: true }).fill('1');
        await dialog.getByLabel('输出格式', { exact: true }).selectOption('jpeg');
      }
      await dialog.getByRole('button', { name: '创建', exact: true }).click();
      await dialog.waitFor({ state: 'hidden' });
      assert.deepEqual(writes.at(-1).body.default_params, apiType === 'seedream'
        ? { size: '2K', seed: 0 } : { size: [1024, 1024], n: 1, output_format: 'jpeg' });
    }
    assert.deepEqual(errors, []);
    console.log('PASS: 图片模型新增, 参数校验, 密钥保留, 重命名, 清空默认值, 重载, 删除和生成预览');
  } finally {
    await runtime.closeContexts(); await runtime.releaseServer(server);
  }
}

export const scenarios = [
  ["img-models", img_models],
  ["chat-model-binding", chat_model_binding],
  ["asr-models", asr_models],
  ["model-dimensions", model_dimensions],
  ["shared-llm-editor", shared_llm_editor]
];
if (process.argv[1] && pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url) await runSuite(scenarios);
