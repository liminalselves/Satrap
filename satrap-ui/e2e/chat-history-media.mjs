import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';
import { preview } from 'vite';
import { createBenchmarkFixture, installHttpMocks, jsonResponse } from '../benchmarks/fixtures.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const server = await preview({ root, preview: { host: '127.0.0.1', port: 0 } });
const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
let browser;
try {
  browser = await chromium.launch({ headless: true });
  for (const count of process.env.CHAT_STRESS_COUNT ? [Number(process.env.CHAT_STRESS_COUNT)] : [100, 500, 1000]) {
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, reducedMotion: 'reduce' });
    const fixture = createBenchmarkFixture(count);
    fixture.turns.forEach((turn, index) => {
      if (index % 5 === 0) turn.attachments = [{ name: `image-${index}.png`, type: 'image/png', url: `/archive/image-${index}.png` }];
    });
    if (count === 1000) fixture.turns.at(-1).attachments = [{ name: 'tail.webm', type: 'video/webm', url: '/archive/tail.webm' }];
    fixture.conversations = [fixture.conversations[0], { ...fixture.conversations[1], title: '切换目标对话', turn_count: 1 }];
    await installHttpMocks(context, origin, fixture);
    const page = await context.newPage(), sockets = [], errors = [];
    let mediaReads = 0, failMedia = false, holdMedia = false, releaseMedia, videoData = '';
    const modelContext = [{ role: 'user', content: '独立模型上下文' }];
    let historyWrites = 0;
    await context.addInitScript(() => localStorage.setItem('satrap.chat.think', 'off'));
    page.on('pageerror', (error) => errors.push(error.message));
    await context.route('http://127.0.0.1:19872/api/chat/turns**', (route) => {
      const conversation = new URL(route.request().url()).searchParams.get('conversation');
      return route.fulfill(jsonResponse({ turns: conversation === 'benchmark-active' ? fixture.turns : [{ ...fixture.turns[0], answer: '目标对话原文', segments: [{ type: 'content', content: '目标对话原文' }] }] }, origin));
    });
    await context.route('http://127.0.0.1:19872/api/chat/media**', async (route) => {
      mediaReads++;
      if (failMedia) return route.fulfill({ ...jsonResponse({ error: '媒体读取测试失败' }, origin), status: 500 });
      if (holdMedia) { holdMedia = false; await new Promise((resolve) => { releaseMedia = resolve; }); }
      if (new URL(route.request().url()).searchParams.get('source') === '/archive/tail.webm') return route.fulfill(jsonResponse({ data_url: videoData }, origin));
      return route.fulfill(jsonResponse({ data_url: 'data:image/svg+xml;base64,' + Buffer.from('<svg xmlns="http://www.w3.org/2000/svg" width="800" height="600"><rect width="800" height="600" fill="green"/></svg>').toString('base64') }, origin));
    });
    await context.route('http://127.0.0.1:19871/config/conversations**', (route) => {
      const pathname = new URL(route.request().url()).pathname;
      if (pathname === '/config/conversations/platforms') return route.fulfill(jsonResponse({ platforms: ['chat'], items: [{ id: 'chat', type: 'chat', type_label: 'Chat', label: 'Chat', supports_history: true }] }, origin));
      if (pathname === '/config/conversations') return route.fulfill(jsonResponse({ items: [{ conversation_id: 'benchmark-active', platform_id: 'chat', title: '压力测试历史',
        context_ids: ['benchmark-active_main'], message_count: 1, history_count: fixture.turns.length, supports_history: true }], total: 1, facet_names: {}, facets: {} }, origin));
      assert.equal(pathname, '/config/conversations/data');
      const body = route.request().postDataJSON();
      const values = body.layer === 'history' ? fixture.turns : modelContext;
      if (body.action) {
        assert.equal(body.layer, 'history'); assert.equal(body.action, 'edit'); assert.equal(body.index, 250);
        assert.equal(body.expected_revision, JSON.stringify(values));
        values[body.index] = { ...values[body.index], user_input: body.user_input, answer: body.answer, thinking: body.thinking,
          segments: [{ type: 'content', content: body.answer }] };
        historyWrites++;
      }
      const offset = body.offset || 0;
      return route.fulfill(jsonResponse({ ok: true, conversation_id: body.conversation_id, layer: body.layer, source: 'database', revision: JSON.stringify(values), total: values.length,
        items: values.slice(offset, offset + 50).map((item, index) => ({ ...item, index: index + offset })), saved: Boolean(body.action),
        backups: historyWrites ? [{ id: 'stress-backup', layer: 'history', reason: 'edit', created_at: 1 }] : [] }, origin));
    });
    await page.routeWebSocket('ws://127.0.0.1:19872/ws/chat**', (socket) => sockets.push(socket));
    await page.routeWebSocket('ws://127.0.0.1:19870/ws/status**', () => {});
    const started = performance.now();
    await page.goto(origin + '/chat');
    await page.getByText('继续最近对话', { exact: true }).click();
    await page.waitForFunction((expected) => document.querySelectorAll('.chat-message-item').length === expected, count);
    const openMs = performance.now() - started;
    const items = page.locator('.chat-message-item');
    const scroll = items.first().locator('xpath=ancestor::div[contains(@class,"overflow-y-auto")][1]');
    const settle = () => page.evaluate(() => new Promise((resolve) => setTimeout(resolve, 450)));
    await settle();
    assert.equal(mediaReads, 0, '长历史不能自动请求全部媒体');
    assert.equal(await items.count(), count);
    if (count === 1000) {
      videoData = await page.evaluate(async () => {
        const canvas = document.createElement('canvas'); canvas.width = 160; canvas.height = 90;
        canvas.getContext('2d').fillRect(0, 0, 160, 90);
        const stream = canvas.captureStream(10), recorder = new MediaRecorder(stream, { mimeType: 'video/webm' }), chunks = [];
        const finished = new Promise((resolve) => { recorder.onstop = () => resolve(new Blob(chunks, { type: 'video/webm' })); });
        recorder.ondataavailable = (event) => chunks.push(event.data);
        recorder.start(); await new Promise((resolve) => setTimeout(resolve, 200)); recorder.stop();
        const blob = await finished; stream.getTracks().forEach((track) => track.stop());
        return new Promise((resolve) => { const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.readAsDataURL(blob); });
      });
      const videoRow = items.nth(count - 2);
      await videoRow.getByRole('button', { name: '预览', exact: true }).click();
      await videoRow.locator('video').waitFor();
      await page.waitForFunction(() => document.querySelector('.chat-message-item video')?.readyState >= 1);
      await settle();
      assert(await scroll.evaluate((element) => element.scrollHeight - element.clientHeight - element.scrollTop < 5), '底部媒体展开未跟随');
      await videoRow.getByRole('button', { name: '收起', exact: true }).click();
      await settle();
    }
    await items.nth(Math.floor(count / 2)).scrollIntoViewIfNeeded(); await settle();
    const anchor = items.nth(Math.floor(count / 2));
    const before = await anchor.evaluate((element) => element.getBoundingClientRect().top);
    const socket = sockets.at(-1);
    socket.send(JSON.stringify({ type: 'turn_start', user_input: '追加用户消息', turn_id: fixture.turns.length + 1, turn_index: fixture.turns.length, variant_index: 0 }));
    socket.send(JSON.stringify({ type: 'content_delta', delta: '流式完整内容' }));
    await page.waitForFunction((expected) => document.querySelectorAll('.chat-message-item').length === expected, count + 2);
    await settle();
    const after = await anchor.evaluate((element) => element.getBoundingClientRect().top);
    assert(Math.abs(after - before) < 4, `阅读旧消息被流式追加打断: ${before} -> ${after}`);
    assert.equal(await items.last().textContent().then((value) => value.includes('流式完整内容')), true);
    await scroll.evaluate((element) => { element.scrollTop = element.scrollHeight; }); await settle();
    for (let index = 0; index < 100; index++) socket.send(JSON.stringify({ type: 'content_delta', delta: '\n后续增量' }));
    socket.send(JSON.stringify({ type: 'turn_done', answer: '流式完整内容' + '\n后续增量'.repeat(100), turn_id: fixture.turns.length + 1, turn_index: fixture.turns.length, variant_index: 0, variant_count: 1 }));
    await settle();
    assert.equal(await items.last().textContent().then((value) => (value.match(/后续增量/g) || []).length), 100);
    const bottomGap = await scroll.evaluate((element) => element.scrollHeight - element.clientHeight - element.scrollTop);
    assert(bottomGap < 5, `底部追加未跟随: ${bottomGap}`);
    fixture.turns.push({ ...fixture.turns[0], id: fixture.turns.length + 1, turn_index: fixture.turns.length, attachments: null,
      user_input: '追加用户消息', answer: '流式完整内容' + '\n后续增量'.repeat(100), segments: [{ type: 'content', content: '流式完整内容' + '\n后续增量'.repeat(100) }] });
    const mediaRow = items.nth(0);
    await mediaRow.scrollIntoViewIfNeeded();
    const beforeImage = mediaReads;
    await mediaRow.getByRole('button', { name: '预览', exact: true }).click();
    await mediaRow.getByRole('img', { name: 'image-0.png', exact: true }).waitFor();
    assert.equal(mediaReads, beforeImage + 1);
    assert(await mediaRow.getByRole('img', { name: 'image-0.png', exact: true }).evaluate((element) => element.complete && element.naturalWidth > 0));
    await mediaRow.getByRole('button', { name: '收起', exact: true }).click();
    assert.equal(await mediaRow.getByRole('img', { name: 'image-0.png', exact: true }).count(), 0);
    failMedia = true;
    await mediaRow.getByRole('button', { name: '预览', exact: true }).click();
    await mediaRow.getByRole('alert').filter({ hasText: '媒体读取测试失败' }).waitFor();
    failMedia = false;
    await mediaRow.getByRole('button', { name: '预览', exact: true }).click();
    await mediaRow.getByRole('img', { name: 'image-0.png', exact: true }).waitFor();
    await mediaRow.getByRole('button', { name: '收起', exact: true }).click();
    holdMedia = true;
    const previewStarted = page.waitForRequest((request) => request.url().includes('/api/chat/media?'));
    const previewAborted = page.waitForEvent('requestfailed', { predicate: (request) => request.url().includes('/api/chat/media?') });
    await mediaRow.getByRole('button', { name: '预览', exact: true }).click();
    await previewStarted;
    await page.getByText('切换目标对话', { exact: true }).click();
    await page.getByText('目标对话原文', { exact: true }).waitFor();
    await previewAborted;
    assert.equal(typeof releaseMedia, 'function'); releaseMedia();
    assert.equal(await items.count(), 2);
    assert.equal(await page.locator('.chat-message-item img').count(), 0);
    await page.getByText(`Benchmark ${count} 条消息`, { exact: true }).click();
    await page.waitForFunction((expected) => document.querySelectorAll('.chat-message-item').length === expected, count + 2);
    assert.equal(await items.last().textContent().then((value) => (value.match(/后续增量/g) || []).length), 100);
    if (count === 1000) {
      const untouched = structuredClone(fixture.turns[249]);
      await page.goto(origin + '/conversations?platform=chat&conversation=benchmark-active');
      await page.getByRole('button', { name: 'Chat 展示历史', exact: true }).click();
      for (let index = 1; index <= 5; index++) {
        await page.getByRole('button', { name: '下一页消息', exact: true }).click();
        await page.getByRole('button', { name: `编辑第 ${index * 50 + 1} 项`, exact: true }).waitFor();
      }
      await page.getByRole('button', { name: '编辑第 251 项', exact: true }).click();
      const dialog = page.getByRole('dialog');
      assert.equal(await dialog.getByLabel('助手回复', { exact: true }).inputValue(), fixture.turns[250].answer);
      await dialog.getByLabel('助手回复', { exact: true }).fill('长历史第251轮编辑结果');
      await dialog.getByRole('button', { name: '保存修改', exact: true }).click();
      await dialog.waitFor({ state: 'hidden' });
      assert.equal(historyWrites, 1); assert.deepEqual(fixture.turns[249], untouched);
      assert.deepEqual(modelContext, [{ role: 'user', content: '独立模型上下文' }]);
      await page.goto(origin + '/chat');
      await page.getByText('继续最近对话', { exact: true }).click();
      await page.waitForFunction((expected) => document.querySelectorAll('.chat-message-item').length === expected, count + 2);
      await items.nth(501).scrollIntoViewIfNeeded();
      await items.nth(501).getByText('长历史第251轮编辑结果', { exact: true }).waitFor();
      await page.setViewportSize({ width: 420, height: 780 });
      await items.nth(501).scrollIntoViewIfNeeded(); await settle();
      const narrowBefore = await items.nth(501).evaluate((element) => element.getBoundingClientRect().top);
      sockets.at(-1).send(JSON.stringify({ type: 'snapshot', conversation_id: 'benchmark-active', stream_id: 'narrow-history', seq: 1,
        state: 'idle', active_turn_id: null, turns: fixture.turns, pending_user_inputs: [] }));
      await settle();
      const narrowAfter = await items.nth(501).evaluate((element) => element.getBoundingClientRect().top);
      assert(Math.abs(narrowAfter - narrowBefore) < 4, `窄屏快照刷新移动阅读位置: ${narrowBefore} -> ${narrowAfter}`);
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), '窄屏出现整页水平溢出');
    }
    assert.deepEqual(errors, []); assert.deepEqual(fixture.errors, []);
    console.log(JSON.stringify({ history_messages: count, image_attachments: fixture.turns.flatMap((turn) => turn.attachments || []).filter((item) => item.type.startsWith('image/')).length,
      video_attachments: fixture.turns.flatMap((turn) => turn.attachments || []).filter((item) => item.type.startsWith('video/')).length,
      first_open_ms: Math.round(openMs), anchor_delta: after - before, bottom_gap: bottomGap, media_reads: mediaReads }));
    await context.close();
  }
  console.log('PASS: 生产 Chat 长历史与媒体按需预览, 旧消息锚点, 完整流式追加, 底部跟随, 切换历史和分页编辑定位');
} finally {
  if (browser) await browser.close();
  await new Promise((resolve) => server.httpServer.close(resolve));
}
