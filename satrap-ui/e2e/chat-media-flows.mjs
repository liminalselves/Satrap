import { finishConfirmation } from './confirmation.mjs';
import { conversationPageResponse, turnPageResponse } from './config-fixtures.mjs';
import { emptyConfigResponse, comboOptions } from './runtime.mjs';
import assert from "node:assert/strict";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createBenchmarkFixture, installHttpMocks, jsonResponse } from "../benchmarks/fixtures.mjs";
import { runSuite, mockConfigVersions, assertNoConfigErrors, waitForRequestEntry, openConversation } from './runtime.mjs';
import { pathToFileURL } from 'node:url';

async function chat_history_media(runtime) {







  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  const server = await runtime.preview({ root, preview: { host: '127.0.0.1', port: 0 } });
  const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
  let browser;
  try {
    browser = await runtime.browser;
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
      await mockConfigVersions(page, origin, () => JSON.stringify({}));
  await page.goto(origin + '/chat');
      await openConversation(page, fixture.conversations[0].title);
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
      assert.equal(await page.locator('.chat-message-item img:not([alt="Satrap"])').count(), 0);
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
        await openConversation(page, fixture.conversations[0].title);
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
    if (browser) await runtime.closeContexts();
    await runtime.releaseServer(server);
  }

}

async function chat_media(runtime) {
  /** 多模态附件浏览器回归, API 请求使用受控数据 */







  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  const server = await runtime.vite({ root, server: { host: '127.0.0.1', port: 0 } });
  let browser;
  try {
    await runtime.listen(server);
    const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
    browser = await runtime.browser;
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
        '/api/chat/conversations/page': conversationPageResponse(route.request().url(), fixture.conversations),
        '/api/chat/turns': { turns: fixture.turns },
        '/api/chat/turns/page': turnPageResponse(route.request().url(), fixture.turns),
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
      return route.fulfill({ json: responses[url.pathname] ?? emptyConfigResponse(route.request().url()),
        headers: { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true', 'Access-Control-Allow-Headers': 'Content-Type' },
      });
    });
    const sockets = [];
    await page.routeWebSocket('ws://127.0.0.1:19872/ws/chat**', (socket) => sockets.push(socket));
    await page.routeWebSocket('ws://127.0.0.1:19870/ws/status**', () => {});
    await mockConfigVersions(page, origin, () => JSON.stringify({}));
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
    await runtime.closeContexts();
    await runtime.releaseServer(server);
  }

}

async function chat_fork_projects(runtime) {
  /** 验证真实聊天页面 fork 刷新后的项目分组, 所有接口均使用隔离数据 */







  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  const server = await runtime.vite({ root, server: { host: '127.0.0.1', port: 0 } });
  let browser;
  try {
    await runtime.listen(server);
    const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
    browser = await runtime.browser;
    const context = await browser.newContext();
    const fixture = createBenchmarkFixture(2);
    const conversations = [
      { ...fixture.conversations[0], title: '项目甲原会话', project_id: 'a' },
      { ...fixture.conversations[1], title: '项目乙原会话', project_id: 'b' },
    ];
    const directory = [...conversations, ...Array.from({ length: 4998 }, (_, index) => ({ ...conversations[0], conversation_id: 'older-' + index,
      title: index === 4997 ? '检索唯一旧会话' : '普通记录-' + index, project_id: index === 4997 ? 'b' : null, last_at: 1 - index }))];
    const pageReads = [];
    let forked = false, releaseFork, enterFork, failRefresh = false;
    const forkEntered = new Promise((resolve) => { enterFork = resolve; });
    const cold = { ...conversations[0], conversation_id: 'cold-history', title: '冷历史打开验证', project_id: null };
    const sent = { ...conversations[0], conversation_id: 'new-sent', title: '首条发送验证', project_id: null };
    const dialogs = [];
    const sentTurns = [];
    let newSnapshotSent;
    const newSnapshotReady = new Promise((resolve) => { newSnapshotSent = resolve; });
    let preloadCount = 0;
    await context.route(`${origin}/ui-config.json`, route => route.fulfill({ json: {
      backend_api: 'http://127.0.0.1:19870', control_api: 'http://127.0.0.1:19871', chat_api: 'http://127.0.0.1:19872',
    } }));
    await context.route(/^http:\/\/127\.0\.0\.1:1987[012]\//, async route => {
      const url = new URL(route.request().url());
      const pathname = url.pathname;
      assert.notEqual(pathname, '/api/chat/conversations', '聊天操作不能回退为全量目录');
      if (pathname === '/api/chat/conversations/page') {
        pageReads.push(Object.fromEntries(url.searchParams));
        if (failRefresh && !url.searchParams.get('conversation_id')) {
          failRefresh = false;
          return route.fulfill({ status: 503, json: { error: '模拟目录刷新失败' }, headers: { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true' } });
        }
        let rows = forked ? [{ ...conversations[0], conversation_id: 'fork', title: '项目甲分支' }, ...directory] : directory;
        if (url.searchParams.get('conversation_id')) rows = [...rows, cold, sent].filter((row) => row.conversation_id === url.searchParams.get('conversation_id'));
        if (url.searchParams.get('search')) rows = rows.filter((row) => row.title.includes(url.searchParams.get('search')));
        if (url.searchParams.get('project_id')) rows = rows.filter((row) => row.project_id === url.searchParams.get('project_id'));
        const offset = Number(url.searchParams.get('cursor') || 0);
        const selected = rows.slice(offset, offset + 50);
        const more = rows.length > offset + 50;
        return route.fulfill({ json: { conversations: selected, has_more: more, next_cursor: more ? String(offset + 50) : null }, headers: {
          'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true', 'Access-Control-Allow-Headers': 'Content-Type' } });
      }
      if (pathname === '/api/chat/conversations/preload') {
        preloadCount++;
        return route.fulfill({ json: { ok: true, conversation_id: sent.conversation_id }, headers: { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true' } });
      }
      if (pathname === '/api/chat/send') {
        sentTurns.push({ ...fixture.turns[0], user_input: route.request().postDataJSON().text, answer: '发送已确认' });
        return route.fulfill({ json: { ok: true, turn_index: 0, turn_id: 1, variant_index: 0 }, headers: { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true' } });
      }
      if (pathname === '/api/chat/fork' && route.request().method() === 'POST') {
        assert.deepEqual(route.request().postDataJSON(), { conversation: 'benchmark-active', turn_index: 0 });
        enterFork();
        await new Promise((resolve) => { releaseFork = resolve; });
        forked = true;
      }
      const responses = {
        '/api/chat/health': { ok: true, conversations: 2, preloaded: 0 },
        '/api/chat/turns': { turns: url.searchParams.get('conversation') === sent.conversation_id ? sentTurns : fixture.turns },
        '/api/chat/turns/page': turnPageResponse(route.request().url(), url.searchParams.get('conversation') === sent.conversation_id ? sentTurns : fixture.turns),
        '/api/chat/models': { models: ['benchmark-model'] },
        '/api/chat/models/detail': { ok: true, models: { 'benchmark-model': { name: 'benchmark-model', model: 'benchmark-model' } } },
        '/api/chat/plugins': { plugins: [] },
        '/api/chat/history': { items: [cold], total: 1, page: 1, page_size: 50, mode: 'hot', storage_size_bytes: 0 },
        '/api/chat/runs': { ok: true, runs: [], next_cursor: null },
        '/api/projects': { projects: [
          { project_id: 'a', name: '项目甲', root_path: 'a', created_at: 1 },
          { project_id: 'b', name: '项目乙', root_path: 'b', created_at: 1 },
        ] },
        '/api/chat/fork': { ok: true, conversation_id: 'fork', copied_turns: 1 },
      };
      return route.fulfill({ json: responses[pathname] ?? emptyConfigResponse(route.request().url()), headers: {
        'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true',
        'Access-Control-Allow-Headers': 'Content-Type',
      } });
    });
    const page = await context.newPage();
    const errors = [];
    page.on('dialog', async (dialog) => { dialogs.push(dialog.message()); await dialog.dismiss(); });
    page.on('pageerror', error => { errors.push(error.message); console.error(error.stack); });
    await page.routeWebSocket('ws://127.0.0.1:19872/ws/chat**', socket => {
      const id = new URL(socket.url()).searchParams.get('conversation');
      socket.send(JSON.stringify({
        type: 'snapshot', conversation_id: new URL(socket.url()).searchParams.get('conversation'),
        stream_id: 'fork-test', seq: 1, state: 'idle', active_turn_id: null,
        turns: id === sent.conversation_id ? sentTurns : fixture.turns, pending_user_inputs: [],
      }));
      if (id === sent.conversation_id) newSnapshotSent();
    });
    await page.routeWebSocket('ws://127.0.0.1:19870/ws/status**', () => {});
    await mockConfigVersions(page, origin, () => JSON.stringify({}));
  await page.goto(`${origin}/chat`);
    await openConversation(page, '项目甲原会话');
    const group = name => page.getByText(name, { exact: true }).locator('..').locator('..');
    await group('项目甲').getByText('项目甲原会话', { exact: true }).waitFor();
    await group('项目乙').getByText('项目乙原会话', { exact: true }).waitFor();
    const search = page.getByPlaceholder('搜索对话', { exact: true });
    await search.fill('检索唯一旧会话');
    await page.getByText('检索唯一旧会话', { exact: true }).waitFor();
    await search.fill('');
    const projectRead = page.waitForResponse((response) => {
      const url = new URL(response.url());
      return url.pathname === '/api/chat/conversations/page' && url.searchParams.get('project_id') === 'b' && !url.searchParams.get('search');
    });
    await page.getByLabel('筛选对话项目').selectOption('b');
    await projectRead;
    await page.getByText('检索唯一旧会话', { exact: true }).waitFor();
    await page.getByLabel('筛选对话项目').selectOption('');
    await group('项目甲').getByText('项目甲原会话', { exact: true }).waitFor();

    try {
      await page.getByTitle('从这里分支', { exact: true }).first().click({ timeout: 10000 });
    } catch (error) {
      console.error(await page.locator('body').innerText(), errors);
      throw error;
    }
    await forkEntered;
    const changedFilters = page.waitForResponse((response) => {
      const query = new URL(response.url()).searchParams;
      return query.get('search') === '项目乙' && query.get('project_id') === 'b';
    });
    await search.fill('项目乙');
    await page.getByLabel('筛选对话项目').selectOption('b');
    await changedFilters;
    const refreshed = page.waitForResponse((response) => response.status() === 503 && new URL(response.url()).searchParams.get('search') === '项目乙');
    failRefresh = true;
    releaseFork();
    await refreshed;
    assert.equal(pageReads.filter((query) => !query.conversation_id).at(-1).search, '项目乙');
    assert.equal(pageReads.filter((query) => !query.conversation_id).at(-1).project_id, 'b');
    await page.getByText(/分支已创建.*侧栏刷新失败/).waitFor();
    await page.getByLabel('筛选对话项目').selectOption('');
    await group('项目甲').getByText('项目甲分支', { exact: true }).waitFor();
    await search.fill('');
    await group('项目甲').getByText('项目甲原会话', { exact: true }).waitFor();
    assert.ok(forked);
    assert.equal(await group('项目甲').getByText('项目甲原会话', { exact: true }).count(), 1);
    assert.equal(await group('项目乙').getByText('项目乙原会话', { exact: true }).count(), 1);
    assert.deepEqual(errors, []);
    assert.ok(pageReads.some((query) => query.search === '检索唯一旧会话'));
    assert.ok(pageReads.some((query) => query.project_id === 'b'));
    await page.getByTitle('对话设置', { exact: true }).click();
    await page.getByRole('dialog', { name: '对话设置', exact: true }).getByRole('button', { name: '数据管理', exact: true }).click();
    await page.getByText('管理历史与回收站', { exact: true }).click();
    await page.getByRole('dialog', { name: '会话历史管理', exact: true }).getByRole('button', { name: '打开', exact: true }).click();
    await page.locator('div.text-sm.truncate').filter({ hasText: new RegExp(`^${cold.title}$`) }).waitFor();
    await group('项目甲').getByText('项目甲原会话', { exact: true }).click();
    await page.getByText(cold.title, { exact: true }).waitFor();
    await page.getByRole('button', { name: '新建对话', exact: true }).click();
    await newSnapshotReady;
    await page.evaluate(() => new Promise(requestAnimationFrame));
    assert.equal(await page.locator('.chat-message-item').count(), 0);
    const input = page.getByPlaceholder('输入消息, Enter 发送, Shift+Enter 换行', { exact: true });
    await input.fill(sent.title);
    const sendResponse = page.waitForResponse((response) => new URL(response.url()).pathname === '/api/chat/send');
    await input.press('Enter');
    await sendResponse;
    await group('项目甲').getByText('项目甲原会话', { exact: true }).click();
    await page.getByText(sent.title, { exact: true }).waitFor();
    assert.ok(preloadCount > 0);
    assert.deepEqual(dialogs, []);
    await assertNoConfigErrors(page);
    console.log('PASS: 搜索期间 Fork 使用最新筛选, 冷历史打开和首条发送后切走不丢失侧栏入口');
  } finally {
    await runtime.closeContexts();
    await runtime.releaseServer(server);
  }

}

async function chat_sidebar_state(runtime) {
  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  const server = await runtime.preview({ root, preview: { host: '127.0.0.1', port: 0 } });
  const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
  const context = await runtime.browser.newContext();
  const page = await context.newPage();
  const fixture = createBenchmarkFixture(2);
  fixture.conversations[0].title = '甲会话'; fixture.conversations[0].project_id = 'a';
  fixture.conversations[1].title = '乙会话'; fixture.conversations[1].project_id = 'b';
  for (let index = fixture.conversations.length; index < 150; index++) fixture.conversations.push({
    ...fixture.conversations[0], conversation_id: `benchmark-${index}`, title: `性能测试会话 ${index}`, project_id: null,
    last_at: fixture.conversations[0].last_at - index * 120,
  });
  fixture.conversations[55].title = '第二页会话'; fixture.conversations[105].title = '第三页会话';
  fixture.conversations[119].title = '待移除会话';
  fixture.conversations[149].title = '原分页末项';
  let projects = [{ project_id: 'a', name: '项目甲', root_path: 'a-workspace' }, { project_id: 'b', name: '项目乙', root_path: 'b-workspace' }];
  let delayPage = false, delayRefresh = false, failRefresh = false, releasePage, releaseRefresh, enterPage, enterRefresh;
  let enterMoveRefresh, releaseMoveRefresh;
  let failSecondPage = false;
  const pageEntered = new Promise((resolve) => { enterPage = resolve; });
  const refreshEntered = new Promise((resolve) => { enterRefresh = resolve; });
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  const headers = { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true', 'Access-Control-Allow-Headers': 'Content-Type', 'Access-Control-Allow-Methods': 'GET,POST,DELETE,OPTIONS' };
  await context.route(origin + '/ui-config.json', route => route.fulfill({ json: { backend_api: 'http://127.0.0.1:19870', control_api: 'http://127.0.0.1:19871', chat_api: 'http://127.0.0.1:19872' } }));
  await context.route(/^http:\/\/127\.0\.0\.1:1987[012]\//, async route => {
    const url = new URL(route.request().url());
    const reply = (json, status = 200) => route.fulfill({ json, status, headers });
    if (route.request().method() === 'OPTIONS') return route.fulfill({ status: 204, headers });
    if (url.pathname === '/api/chat/conversations/page') {
      if (url.searchParams.get('cursor') === '50' && failSecondPage) {
        failSecondPage = false;
        return reply({ error: '后续页刷新失败' }, 503);
      }
      if (!url.searchParams.has('conversation_id') && enterMoveRefresh) {
        const entered = enterMoveRefresh; enterMoveRefresh = null; entered();
        await new Promise((resolve) => { releaseMoveRefresh = resolve; });
        return reply({ error: '非活跃移动刷新失败' }, 503);
      }
      if (url.searchParams.has('cursor') && delayPage) {
        delayPage = false; enterPage();
        await new Promise((resolve) => { releasePage = resolve; });
        return reply({ error: '旧分页迟到失败' }, 503);
      }
      if (!url.searchParams.has('conversation_id') && delayRefresh) {
        delayRefresh = false; enterRefresh();
        await new Promise((resolve) => { releaseRefresh = resolve; });
        return reply({ error: '旧刷新迟到失败' }, 503);
      }
      if (!url.searchParams.has('conversation_id') && failRefresh) {
        failRefresh = false;
        return reply({ error: '当前刷新失败' }, 503);
      }
      return reply(conversationPageResponse(url.href, fixture.conversations));
    }
    if (/^\/api\/chat\/conversations\/[^/]+\/project$/.test(url.pathname)) {
      const id = decodeURIComponent(url.pathname.split('/')[4]);
      fixture.conversations.find((item) => item.conversation_id === id).project_id = route.request().postDataJSON().project_id;
      return reply({ ok: true });
    }
    if (url.pathname.startsWith('/api/projects/') && route.request().method() === 'DELETE') {
      const id = decodeURIComponent(url.pathname.split('/').at(-1));
      projects = projects.filter((item) => item.project_id !== id);
      fixture.conversations.forEach((item) => { if (item.project_id === id) item.project_id = null; });
      return reply({ ok: true });
    }
    const responses = {
      '/api/chat/health': { ok: true, conversations: fixture.conversations.length },
      '/api/chat/models': { models: ['benchmark-model'] }, '/api/chat/models/detail': { ok: true, models: { 'benchmark-model': { name: 'benchmark-model', model: 'benchmark-model' } } },
      '/api/chat/plugins': { plugins: [] }, '/api/chat/runs': { runs: [] },
      '/api/chat/turns': { turns: fixture.turns },
        '/api/chat/turns/page': turnPageResponse(route.request().url(), fixture.turns), '/api/projects': { projects },
    };
    return reply(responses[url.pathname] ?? emptyConfigResponse(url.href));
  });
  await page.routeWebSocket('ws://127.0.0.1:19872/ws/chat**', socket => socket.send(JSON.stringify({ type: 'snapshot', conversation_id: new URL(socket.url()).searchParams.get('conversation'), stream_id: 'sidebar-state', seq: 1, state: 'idle', turns: fixture.turns, pending_user_inputs: [] })));
  await page.routeWebSocket('ws://127.0.0.1:19870/ws/status**', () => {});
  await mockConfigVersions(page, origin, () => '{}');
  const selector = page.getByLabel('筛选对话项目');
  const afterRender = () => page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  const row = (title) => page.locator('div.text-sm.truncate').filter({ hasText: new RegExp(`^${title}$`) }).locator('..').locator('..');
  const moved = () => row('甲会话');
  const moveToB = async () => {
    await moved().getByTitle('移入/移出项目', { exact: true }).click();
    await page.getByRole('dialog', { name: '移动会话到项目', exact: true }).getByRole('button', { name: /项目乙/ }).click();
  };
  try {
    await page.goto(origin + '/chat');
    await openConversation(page, '甲会话');
    const moveRefreshEntered = new Promise((resolve) => { enterMoveRefresh = resolve; });
    await row('乙会话').getByTitle('移入/移出项目', { exact: true }).click();
    await page.getByRole('dialog', { name: '移动会话到项目', exact: true }).getByRole('button', { name: /项目甲/ }).click();
    await waitForRequestEntry(moveRefreshEntered); await afterRender();
    assert.equal(await row('乙会话').count(), 1, '非活跃会话在移动后的刷新在途时仍保留入口');
    releaseMoveRefresh();
    await page.getByRole('alert').filter({ hasText: '非活跃移动刷新失败' }).waitFor();
    assert.equal(await row('乙会话').count(), 1, '移动成功但刷新失败时保留非活跃会话入口');
    await row('乙会话').getByTitle('移入/移出项目', { exact: true }).click();
    await page.getByRole('dialog', { name: '移动会话到项目', exact: true }).getByRole('button', { name: /项目乙/ }).click();
    await page.getByRole('alert').filter({ hasText: '非活跃移动刷新失败' }).waitFor({ state: 'hidden' });
    delayPage = true;
    await page.getByRole('button', { name: '加载更多对话', exact: true }).click();
    await waitForRequestEntry(pageEntered);
    await selector.selectOption('b');
    await page.locator('div.text-sm.truncate').filter({ hasText: /^乙会话$/ }).waitFor();
    const oldPageFailed = page.waitForResponse((response) => response.status() === 503 && new URL(response.url()).searchParams.has('cursor'));
    releasePage(); await oldPageFailed; await afterRender();
    assert.equal(await page.getByRole('alert').filter({ hasText: '旧分页迟到失败' }).count(), 0);
    await selector.selectOption('a');
    await page.locator('div.text-sm.truncate').filter({ hasText: /^甲会话$/ }).waitFor();
    delayRefresh = true;
    await moveToB(); await waitForRequestEntry(refreshEntered);
    // 刷新请求仍被挂起, 移出当前筛选项目的会话应立即从本地列表消失
    await page.locator('div.text-sm.truncate').filter({ hasText: /^甲会话$/ }).waitFor({ state: 'detached' });
    // 切换筛选会中止旧刷新; 无论请求被中止还是迟到失败, 都不能把错误显示到新筛选下
    const isOldRefresh = (request) => request.url().includes('/conversations/page') && new URL(request.url()).searchParams.get('project_id') === 'a';
    const oldRefreshSettled = Promise.race([
      page.waitForEvent('requestfailed', isOldRefresh),
      page.waitForResponse((response) => response.status() === 503 && isOldRefresh(response.request())),
    ]);
    await selector.selectOption('b');
    await page.locator('div.text-sm.truncate').filter({ hasText: /^甲会话$/ }).waitFor();
    releaseRefresh(); await oldRefreshSettled; await afterRender();
    assert.equal(await page.getByRole('alert').filter({ hasText: '旧刷新迟到失败' }).count(), 0);
    failRefresh = true;
    await moveToB();
    await page.getByRole('alert').filter({ hasText: '当前刷新失败' }).waitFor();
    await moveToB();
    await page.getByRole('alert').filter({ hasText: '当前刷新失败' }).waitFor({ state: 'hidden' });
    await page.getByTitle('b-workspace', { exact: true }).locator('..').getByTitle('删除项目 (仅解绑会话, 磁盘文件不动)', { exact: true }).click();
    await finishConfirmation(page);
    await page.waitForFunction(() => document.querySelector('[aria-label="筛选对话项目"]')?.getAttribute('data-value') === '');
    await page.locator('div.text-sm.truncate').filter({ hasText: /^乙会话$/ }).waitFor();
    assert.equal((await comboOptions(selector)).some((option) => option.value === 'b'), false);
    await page.getByRole('button', { name: '加载更多对话', exact: true }).click();
    await row('第二页会话').waitFor();
    const movePaged = async (title) => {
      await row(title).getByTitle('移入/移出项目', { exact: true }).click();
      await page.getByRole('dialog', { name: '移动会话到项目', exact: true }).getByRole('button', { name: /项目甲/ }).click();
    };
    failSecondPage = true;
    await movePaged('第二页会话');
    await page.getByRole('alert').filter({ hasText: '后续页刷新失败' }).waitFor();
    assert.equal(await row('第二页会话').count(), 1, '第二页刷新失败不能提交只有第一页的目录');
    await movePaged('第二页会话');
    await page.getByRole('alert').filter({ hasText: '后续页刷新失败' }).waitFor({ state: 'hidden' });
    assert.equal(await row('第二页会话').count(), 1, '成功刷新仍保留第二页的非活跃会话');
    await page.getByRole('button', { name: '加载更多对话', exact: true }).click();
    await row('第三页会话').waitFor();
    await row('待移除会话').waitFor();
    await row('原分页末项').waitFor();
    const latest = fixture.conversations[0];
    fixture.conversations.unshift(...[2, 1].map((index) => ({ ...latest,
      conversation_id: `new-head-${index}`, title: `新增会话 ${index}`, project_id: null, last_at: latest.last_at + index,
    })));
    fixture.conversations = fixture.conversations.filter((item) => item.title !== '待移除会话');
    const lastPageRefreshed = page.waitForResponse((response) => new URL(response.url()).pathname === '/api/chat/conversations/page' && new URL(response.url()).searchParams.get('cursor') === '150');
    await movePaged('第三页会话');
    await lastPageRefreshed; await afterRender();
    assert.equal(await row('第二页会话').count(), 1);
    assert.equal(await row('第三页会话').count(), 1, '成功刷新仍保留第三页的非活跃会话');
    assert.equal(await row('原分页末项').count(), 1, '新会话挤动分页边界后仍保留原已加载末项');
    assert.equal(await row('待移除会话').count(), 0, '刷新后移除服务端已删除的会话');
    assert.equal(await page.getByRole('button', { name: '加载更多对话', exact: true }).count(), 0);
    const projectPagesRefreshed = page.waitForResponse((response) => new URL(response.url()).pathname === '/api/chat/conversations/page' && new URL(response.url()).searchParams.get('cursor') === '150');
    await page.getByTitle('a-workspace', { exact: true }).locator('..').getByTitle('删除项目 (仅解绑会话, 磁盘文件不动)', { exact: true }).click();
    await finishConfirmation(page);
    await projectPagesRefreshed; await afterRender();
    assert.equal(await row('第二页会话').count(), 1);
    assert.equal(await row('第三页会话').count(), 1);
    assert.equal(await row('原分页末项').count(), 1);
    assert.equal((await comboOptions(selector)).some((option) => option.value === 'a'), false);
    assert.deepEqual(errors, []);
    await assertNoConfigErrors(page);
    console.log('PASS: 移动和删除项目保留已展开分页及原末项, 后续页刷新失败不提交部分目录, 删除和筛选正常生效');
  } finally {
    releasePage?.(); releaseRefresh?.(); releaseMoveRefresh?.();
    await runtime.closeContexts(); await runtime.releaseServer(server);
  }
}

async function chat_history_races(runtime) {
  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  const server = await runtime.preview({ root, preview: { host: '127.0.0.1', port: 0 } });
  const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
  const context = await runtime.browser.newContext(), page = await context.newPage();
  const fixture = createBenchmarkFixture(120), errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await installHttpMocks(context, origin, fixture);
  const gates = [0, 1, 2, 3].map(() => {
    let enter, release;
    const entered = new Promise((resolve) => { enter = resolve; });
    const released = new Promise((resolve) => { release = resolve; });
    return { entered, released, enter, release };
  });
  let olderPhase = 'stale', chatSocket, socketReady, initialRequests = 0;
  const connected = new Promise((resolve) => { socketReady = resolve; });
  await context.route('http://127.0.0.1:19872/api/chat/model?**', async (route) => {
    if (route.request().method() !== 'GET') return route.fallback();
    gates[3].enter(); await gates[3].released;
    return route.fallback();
  });
  await context.route('http://127.0.0.1:19872/api/chat/turns/page**', async (route) => {
    if (route.request().method() !== 'GET') return route.fallback();
    const older = new URL(route.request().url()).searchParams.has('before');
    if (!older) {
      initialRequests++;
      gates[0].enter(); await gates[0].released;
      return route.fulfill({ ...jsonResponse({ error: '旧历史迟到失败0' }, origin), status: 503 });
    }
    const phase = olderPhase;
    if (phase === 'stale') {
      gates[1].enter(); await gates[1].released;
      return route.fulfill({ ...jsonResponse({ error: '旧历史迟到失败1' }, origin), status: 503 });
    }
    if (phase === 'current-error') return route.fulfill({ ...jsonResponse({ error: '当前历史加载失败' }, origin), status: 503 });
    gates[2].enter(); await gates[2].released;
    return route.fallback();
  });
  await page.routeWebSocket('ws://127.0.0.1:19872/ws/chat**', (socket) => {
    chatSocket = socket; socketReady();
    socket.send(JSON.stringify({ type: 'subscribed', conversation_id: 'benchmark-active' }));
  });
  await page.routeWebSocket('ws://127.0.0.1:19870/ws/status**', () => {});
  const afterRender = () => page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  const sendSnapshot = async (seq) => {
    const turns = fixture.turns.slice(-50).map((turn, index, all) => index === all.length - 1
      ? { ...turn, answer: `快照版本${seq}`, segments: [{ type: 'content', content: `快照版本${seq}` }] } : turn);
    chatSocket.send(JSON.stringify({ type: 'snapshot', conversation_id: 'benchmark-active', stream_id: 'history-race', seq,
      state: 'idle', active_turn_id: null, turns, has_more: true, next_before: turns[0].turn_index, pending_user_inputs: [] }));
    await page.getByText(`快照版本${seq}`, { exact: true }).waitFor();
  };
  try {
    await page.goto(origin + '/chat');
    await openConversation(page, fixture.conversations[0].title);
    await waitForRequestEntry(gates[0].entered); await waitForRequestEntry(connected);
    await waitForRequestEntry(gates[3].entered);
    await page.locator('textarea').fill('只检查历史读取, 不发送');
    gates[3].release();
    await page.waitForFunction(() => document.querySelector('button[title="发送"]')?.disabled === false);
    await afterRender();
    assert.equal(initialRequests, 1, '首屏历史请求未完成时, 模型绑定回填不能重复发起历史读取');
    await sendSnapshot(1);
    const firstFailed = page.waitForResponse((response) => response.url().includes('/api/chat/turns/page') && !new URL(response.url()).searchParams.has('before') && response.status() === 503);
    gates[0].release(); await firstFailed; await afterRender();
    assert.equal(initialRequests, 1, '新快照到达后不能补发首屏历史请求');
    assert.equal(await page.getByRole('alert').filter({ hasText: '旧历史迟到失败0' }).count(), 0);
    await page.getByRole('button', { name: '加载更早消息', exact: true }).click();
    await waitForRequestEntry(gates[1].entered);
    await sendSnapshot(2);
    const olderFailed = page.waitForResponse((response) => response.url().includes('/api/chat/turns/page') && new URL(response.url()).searchParams.has('before') && response.status() === 503);
    olderPhase = 'current-error';
    gates[1].release(); await olderFailed; await afterRender();
    assert.equal(await page.getByRole('alert').filter({ hasText: '旧历史迟到失败1' }).count(), 0);
    await page.getByRole('button', { name: '加载更早消息', exact: true }).click();
    await page.getByRole('alert').filter({ hasText: '当前历史加载失败' }).waitFor();
    await sendSnapshot(3);
    await page.getByRole('alert').filter({ hasText: '当前历史加载失败' }).waitFor({ state: 'hidden' });
    await page.getByRole('button', { name: '加载更早消息', exact: true }).click();
    await page.getByRole('alert').filter({ hasText: '当前历史加载失败' }).waitFor();
    const automaticFailed = page.waitForResponse((response) => response.url().includes('/api/chat/turns/page') && new URL(response.url()).searchParams.has('before') && response.status() === 503);
    await page.evaluate(() => {
      const scroller = [...document.querySelectorAll('div')].find((element) => element.style.overflowAnchor === 'none');
      if (!scroller) throw new Error('未找到消息滚动容器');
      scroller.scrollTop = 0;
      scroller.dispatchEvent(new Event('scroll', { bubbles: true }));
    });
    await automaticFailed; await afterRender();
    assert.equal(await page.getByRole('button', { name: '加载更早消息', exact: true }).count(), 1);
    olderPhase = 'retry';
    await page.getByRole('button', { name: '加载更早消息', exact: true }).focus();
    await page.keyboard.press('Enter');
    await waitForRequestEntry(gates[2].entered);
    assert.equal(await page.getByRole('button', { name: '加载更早消息', exact: true }).count(), 1);
    gates[2].release();
    await page.getByText('第 1 轮用户消息, 请解释当前测试内容', { exact: true }).waitFor();
    await page.getByRole('alert').filter({ hasText: '当前历史加载失败' }).waitFor({ state: 'hidden' });
    assert.equal(await page.getByText('快照版本3', { exact: true }).count(), 1);
    assert.deepEqual(errors, []); assert.deepEqual(fixture.errors, []);
    console.log('PASS: 首屏历史去重, 快照使旧错误失效, 迟到失败不污染新快照且当前失败可重试');
  } finally {
    gates.forEach((gate) => gate.release());
    await runtime.closeContexts(); await runtime.releaseServer(server);
  }
}

export const scenarios = [
  ["chat-history-races", chat_history_races],
  ["chat-sidebar-state", chat_sidebar_state],
  ['chat-output-files', async (runtime) => {
    const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
    const server = await runtime.preview({ root, preview: { host: '127.0.0.1', port: 0 } });
    const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
    const context = await runtime.browser.newContext({ acceptDownloads: true });
    const fixture = createBenchmarkFixture(2);
    const file = { file_id: 'sf_' + 'a'.repeat(32), name: '报告.zip', size: 3, sha256: 'hash', mime: 'application/zip' };
    fixture.turns[0].output_files = [file];
    await installHttpMocks(context, origin, fixture);
    let reads = 0, fail = false;
    await context.route('http://127.0.0.1:19872/api/chat/files/**', (route) => {
      reads++;
      assert.equal(new URL(route.request().url()).pathname, `/api/chat/files/benchmark-active/${file.file_id}`);
      return route.fulfill({ status: fail ? 404 : 200, contentType: 'application/octet-stream', body: Buffer.from([0, 255, 65]),
        headers: { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true',
          'Content-Disposition': `attachment; filename*=UTF-8''${encodeURIComponent(file.name)}` } });
    });
    const page = await context.newPage(), errors = [];
    page.on('pageerror', (error) => errors.push(error.message));
    await page.routeWebSocket('ws://127.0.0.1:19872/ws/chat**', () => {});
    await page.routeWebSocket('ws://127.0.0.1:19870/ws/status**', () => {});
    await mockConfigVersions(page, origin, () => '{}');
    try {
      await page.goto(`${origin}/chat`);
      await openConversation(page, fixture.conversations[0].title);
      const button = page.getByRole('button', { name: `下载 ${file.name}`, exact: true });
      await button.waitFor();
      const download = page.waitForEvent('download');
      await button.click();
      const received = await download;
      assert.equal(received.suggestedFilename(), file.name);
      const chunks = [];
      for await (const chunk of await received.createReadStream()) chunks.push(chunk);
      assert.deepEqual(Buffer.concat(chunks), Buffer.from([0, 255, 65]));
      await page.reload();
      await page.waitForFunction(() => document.querySelector('[aria-label="下载 报告.zip"]') || document.querySelector('div.text-sm.truncate'));
      if (!(await button.isVisible())) await openConversation(page, fixture.conversations[0].title);
      await button.waitFor();
      fail = true;
      await button.click();
      await page.getByRole('alert').filter({ hasText: '文件不存在' }).waitFor();
      assert.equal(reads, 2);
      assert.deepEqual(errors, []);
    } finally { await context.close(); }
  }],
  ["chat-history-media", chat_history_media],
  ["chat-media", chat_media],
  ["chat-fork-projects", chat_fork_projects]
];
if (process.argv[1] && pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url) await runSuite(scenarios);
