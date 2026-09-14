/** 多模态附件浏览器回归, API 请求使用受控数据 */
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';
import { createServer } from 'vite';
import { createBenchmarkFixture } from '../benchmarks/fixtures.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const server = await createServer({ root, server: { host: '127.0.0.1', port: 0 } });
let browser;
try {
  await server.listen();
  const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
  browser = await chromium.launch({ headless: true });
  const context = await browser.newContext();
  const page = await context.newPage();
  page.on('pageerror', (error) => console.error(error.stack));
  const image = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg==';
  const video = await page.evaluate(async () => {
    const canvas = document.createElement('canvas');
    canvas.width = canvas.height = 64;
    const stream = canvas.captureStream(10);
    const recorder = new MediaRecorder(stream, { mimeType: 'video/webm;codecs=vp8' });
    const chunks = [];
    recorder.ondataavailable = (event) => chunks.push(event.data);
    const stopped = new Promise((resolve) => { recorder.onstop = resolve; });
    recorder.start();
    canvas.getContext('2d').fillRect(0, 0, 64, 64);
    await new Promise((resolve) => setTimeout(resolve, 300));
    recorder.stop();
    await stopped;
    stream.getTracks().forEach((track) => track.stop());
    return await new Promise((resolve) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.readAsDataURL(new Blob(chunks, { type: 'video/webm' }));
    });
  });
  const fixture = createBenchmarkFixture(2);
  fixture.turns[0].attachments = [
    { name: '图片.png', url: 'private/image.png', type: 'image/png' },
    { name: '视频.webm', url: 'private/video.webm', type: 'video/webm' },
  ];
  const requests = [];
  let mediaRemoved = false;
  await context.route(`${origin}/ui-config.json`, (route) => route.fulfill({ json: {
    backend_api: 'http://127.0.0.1:19870', control_api: 'http://127.0.0.1:19871', chat_api: 'http://127.0.0.1:19872',
  } }));
  await context.route(/^http:\/\/127\.0\.0\.1:1987[012]\//, (route) => {
    const url = new URL(route.request().url());
    if (route.request().method() === 'OPTIONS') return route.fulfill({ status: 204, headers: {
      'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true',
      'Access-Control-Allow-Headers': 'Content-Type', 'Access-Control-Allow-Methods': 'GET, POST, PUT, DELETE',
    } });
    const responses = {
      '/api/chat/health': { ok: true, conversations: 1, preloaded: 0 },
      '/api/chat/conversations': { conversations: fixture.conversations },
      '/api/chat/turns': { turns: fixture.turns },
      '/api/chat/runs': { ok: true, runs: [], next_cursor: null },
      '/api/chat/models': { models: ['benchmark-model'] },
      '/api/chat/models/detail': { ok: true, models: { 'benchmark-model': { supports_visual_input: true } } },
      '/api/chat/plugins': { plugins: [] },
      '/api/projects': { projects: [] },
    };
    if (url.pathname === '/api/chat/media') {
      requests.push(url);
      assert.equal(url.searchParams.get('conversation'), 'benchmark-active');
      return route.fulfill({ status: mediaRemoved ? 400 : 200,
        json: mediaRemoved ? { error: '媒体文件已移除' } : { data_url: url.searchParams.get('source').endsWith('.png') ? image : video },
        headers: { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true' },
      });
    }
    return route.fulfill({ json: responses[url.pathname] ?? { ok: true },
      headers: { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true', 'Access-Control-Allow-Headers': 'Content-Type' },
    });
  });
  const sockets = [];
  await page.routeWebSocket('ws://127.0.0.1:19872/ws/chat**', (socket) => sockets.push(socket));
  await page.routeWebSocket('ws://127.0.0.1:19870/ws/status**', () => {});
  await page.goto(`${origin}/chat`);
  await page.getByText('Benchmark 2 条消息', { exact: true }).first().click();
  const deadline = Date.now() + 10000;
  while (!sockets.length) {
    assert.ok(Date.now() < deadline, '等待聊天订阅超时');
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  sockets[0].send(JSON.stringify({
    type: 'snapshot', conversation_id: 'benchmark-active', stream_id: 'media-test', seq: 1,
    state: 'idle', active_turn_id: null, turns: fixture.turns, pending_user_inputs: [],
  }));
  try {
    await page.getByText('图片.png', { exact: true }).waitFor({ timeout: 5000 });
  } catch (error) {
    console.error(await page.locator('body').innerText());
    throw error;
  }
  assert.equal(requests.length, 0);
  await page.getByRole('button', { name: '预览', exact: true }).first().click();
  const displayed = page.getByRole('img', { name: '图片.png', exact: true });
  await displayed.waitFor();
  await page.waitForFunction(() => document.querySelector('img[alt="图片.png"]')?.naturalWidth > 0);
  assert.equal(requests.length, 1);
  await page.getByRole('button', { name: '收起', exact: true }).click();
  await displayed.waitFor({ state: 'detached' });
  await page.getByRole('button', { name: '预览', exact: true }).last().click();
  await page.locator('video').waitFor();
  await page.waitForFunction(() => document.querySelector('video')?.readyState >= 1);
  assert.equal(await page.locator('video').evaluate((element) => element.controls && element.paused), true);
  await page.locator('video').evaluate((element) => element.play());
  await page.getByRole('button', { name: '收起', exact: true }).click();
  mediaRemoved = true;
  await page.getByRole('button', { name: '预览', exact: true }).last().click();
  await page.getByRole('alert').filter({ hasText: '媒体文件已移除' }).waitFor();
  console.log('媒体预览按需加载, 图片解码, 视频播放, 收起和错误反馈通过');
} finally {
  await browser?.close();
  await server.close();
}
