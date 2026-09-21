/** ASR 模型配置与转录测试的浏览器回归, 接口使用受控响应, 不调用真实服务 */
import assert from 'node:assert/strict';
import path from 'node:path';
import fs from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';
import { createServer } from 'vite';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const server = await createServer({ root, server: { host: '127.0.0.1', port: 0 } });
const artifacts = path.join(root, 'test-results', 'asr-models');
let browser;
try {
  await fs.mkdir(artifacts, { recursive: true });
  await server.listen();
  const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
  browser = await chromium.launch({ headless: true });
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
  await browser?.close();
  await server.close();
}
