import assert from "node:assert/strict";
import path from "node:path";
import fs from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { runSuite, mockConfigVersions } from './runtime.mjs';
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
      return route.fulfill({ json: responses[pathname] ?? { ok: true }, headers });
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
    const models = { default: { name: 'default', model: 'offline', api_key: '****masked', context_window: 4096,
      history_ratio: 0.6, context_strategy: 'summarize', context_threshold: 0.9, truncation_floor: 0.3,
      summary_keep_recent_turns: 3, thinking_fields: ['reasoning_effort'], thinking_levels: ['low', 'high'],
      supports_visual_input: false, lock_api_key: true } };
    const writes = [];
    const fixture = createBenchmarkFixture(1);
    let reloads = 0;
    let failNext = false;
    await context.route(`${origin}/ui-config.json`, (route) => route.fulfill({ json: {
      backend_api: 'http://127.0.0.1:19870', control_api: 'http://127.0.0.1:19871', chat_api: 'http://127.0.0.1:19872',
    } }));
    await context.route(/^http:\/\/127\.0\.0\.1:1987[012]\//, (route) => {
      const request = route.request(), url = new URL(request.url()), pathname = url.pathname;
      const headers = { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true',
        'Access-Control-Allow-Headers': '*', 'Access-Control-Allow-Methods': '*' };
      if (request.method() === 'OPTIONS') return route.fulfill({ status: 204, headers });
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
        '/api/chat/health': { ok: true }, '/api/chat/plugins': { plugins: [] },
        '/api/chat/conversations': { conversations: fixture.conversations }, '/api/chat/turns': { turns: fixture.turns },
        '/api/chat/runs': { ok: true, runs: [], next_cursor: null }, '/api/projects': { projects: [] },
        '/api/health': { running: true, adapters: {} }, '/status': { running: true },
        '/api/config/reload': { ok: true, platforms: [], edictum_sessions: [] },
      };
      return route.fulfill({ headers, json: responses[pathname] ?? { ok: true } });
    });
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', (error) => errors.push(error.message));
    await page.routeWebSocket(/^ws:\/\/127\.0\.0\.1:1987[02]\//, () => {});
    await mockConfigVersions(page, origin, () => JSON.stringify(models));
    await page.goto(`${origin}/models`);
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
    await page.getByText('继续最近对话', { exact: true }).click();
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

export const scenarios = [
  ["asr-models", asr_models],
  ["model-dimensions", model_dimensions],
  ["shared-llm-editor", shared_llm_editor]
];
if (process.argv[1] && pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url) await runSuite(scenarios);
