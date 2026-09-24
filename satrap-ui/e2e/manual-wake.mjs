/** 手动唤醒弹窗的真实页面回归, API 使用受控响应 */
import assert from 'node:assert/strict';
import path from 'node:path';
import fs from 'node:fs/promises';
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
  const page = await browser.newPage({ viewport: { width: 1100, height: 900 } });
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  const requests = [];
  let responseMode = 'error';
  let statusMode = 'poll';
  let statusCalls = 0;
  const diagnosticsCalls = [];
  await page.route(`${origin}/ui-config.json`, (route) => route.fulfill({ json: {
    backend_api: 'http://127.0.0.1:19870', control_api: 'http://127.0.0.1:19871', chat_api: 'http://127.0.0.1:19872',
  } }));
  await page.route(/^http:\/\/127\.0\.0\.1:1987[012]\//, async (route) => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname;
    const headers = { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true', 'Access-Control-Allow-Headers': 'Content-Type', 'Access-Control-Allow-Methods': 'GET, POST, OPTIONS' };
    if (request.method() === 'OPTIONS') return route.fulfill({ status: 204, headers });
    if (pathname === '/api/platforms/wake') {
      requests.push(request.postDataJSON());
      return route.fulfill({ headers, status: responseMode === 'error' ? 409 : 200,
        json: responseMode === 'error' ? { error: '目标群暂不可用' } : { status: responseMode } });
    }
    if (pathname === '/api/platforms/wake/rejections') {
      return route.fulfill({ headers, json: { records: [
        { adapter_id: 'onebot-test', session_id: 'group:20000', actor_id: '30000', stage: 'rate_limit', decision: '', reason: '请求频率限制, 需等待 3.0s', recorded_at: '2026-09-24T10:15:30', message_id: '', request_id: 'req-x', send_status: 'success' },
        { adapter_id: 'onebot-test', session_id: 'group:20000', actor_id: '30000', stage: 'wake_decision', decision: 'skip', reason: 'frequency_cooldown: 冷却中', recorded_at: '2026-09-24T10:14:00', message_id: '', request_id: '', send_status: '' },
      ] } });
    }
    if (pathname.startsWith('/api/platforms/wake/diagnostics')) {
      // 阶段诊断与手动状态是两个接口, 必须先分流 (批次8 在弹窗内展示阶段结果)
      const tail = pathname.replace('/api/platforms/wake/diagnostics', '').replace(/^\//, '');
      diagnosticsCalls.push(new URL(request.url()).searchParams.get('adapter_id') || '');
      if (tail) {
        return route.fulfill({ headers, json: {
          request_id: tail, adapter_id: 'onebot-test', available: true, truncated: false,
          stages: ['projection', 'model', 'send'],
          records: [
            { recorded_at: '2026-09-24T10:15:31', adapter_id: 'onebot-test', session_id: 'group:20000', actor_id: '30000', stage: 'projection', decision: 'failed', reason: '附件已解析 0 项, 未完成 1 项', message_id: '', request_id: tail, send_status: '', self_id: '1', status: 'failed', reason_code: 'failed', turn_id: 'turn-1', attachments: 'record:unsupported:asr_not_configured', notes: '', },
            { recorded_at: '2026-09-24T10:15:32', adapter_id: 'onebot-test', session_id: 'group:20000', actor_id: '30000', stage: 'model', decision: 'ok', reason: '模型输出 6 字符', message_id: '', request_id: tail, send_status: '', self_id: '1', status: 'ok', reason_code: 'completed', turn_id: 'turn-1', attachments: '', notes: '', },
            { recorded_at: '2026-09-24T10:15:33', adapter_id: 'onebot-test', session_id: 'group:20000', actor_id: '30000', stage: 'send', decision: 'unknown', reason: '业务段 已确认 0 未确认 1 失败 0', message_id: '', request_id: tail, send_status: 'submitted', self_id: '1', status: 'unknown', reason_code: 'in_flight_unconfirmed', turn_id: 'turn-1', attachments: '', notes: '', },
          ],
        } });
      }
      const params = new URL(request.url()).searchParams;
      // 语音转写失败但模型仍执行, 发送未确认: 阶段之间互不覆盖
      const records = [{
        request_id: params.get('request_id') || 'req-1', adapter_id: 'onebot-test', session_id: 'group:20000',
        actor_id: '30000', self_id: '1', message_id: '', recorded_at: '2026-09-24T10:15:33',
        stages: ['projection', 'model', 'send'],
        statuses: { projection: 'failed', model: 'ok', send: 'unknown' },
        reason_codes: ['failed', 'completed', 'in_flight_unconfirmed'],
        attachments: 'record:unsupported:asr_not_configured', notes: '', send_status: 'submitted', turn_id: 'turn-1',
      }];
      const records_ = params.get('adapter_id') === 'absent-bot' ? [] : records;
      return route.fulfill({ headers, json: { records: records_, available: true, capacity: 256, records_total: records_.length } });
    }
    const statusMatch = pathname.match(/^\/api\/platforms\/wake\/(.+)$/);
    if (statusMatch && request.method() === 'GET') {
      statusCalls += 1;
      if (statusMode === 'degraded') {
        // 存储降级: 不能报成未找到或已送达
        return route.fulfill({ status: 503, headers, json: { status: 'unknown', request_id: decodeURIComponent(statusMatch[1]), reason: 'store_degraded', detail: '归档目录不可读' } });
      }
      if (statusMode === 'unknown') {
        return route.fulfill({ headers, json: { status: 'unknown', request_id: decodeURIComponent(statusMatch[1]), detail: 'in_flight_unconfirmed', target: 'group:20000' } });
      }
      // StrictMode 下效应会双跑, 前两次都按执行中返回, 第三次起才进入终态
      return route.fulfill({ headers, json: statusCalls <= 2
        ? { status: 'executing', detail: '管线执行中', target: 'group:20000' }
        : { status: 'sent', detail: '已发送 1 段', target: 'group:20000' } });
    }
    const responses = {
      '/api/health': { running: true, adapters: {} }, '/status': { running: true },
      '/api/sessions': { sessions: [] }, '/config/session-instances': { sessions: [] },
      '/config/session-classes': {}, '/config/edictum/sessions': {}, '/config/edictum/types': [],
      '/config/platforms': { ok: true, platforms: [{ id: 'onebot-test', type: 'onebot', settings: {} }] },
    };
    return route.fulfill({ headers, json: responses[pathname] ?? {} });
  });
  await page.routeWebSocket('ws://127.0.0.1:19870/ws/status**', (socket) => {
    socket.send(JSON.stringify({ type: 'status', data: { running: true, adapters: {} } }));
  });
  await page.goto(`${origin}/sessions`);
  await page.getByRole('button', { name: '手动唤醒群聊', exact: true }).click();
  const dialog = page.getByRole('dialog');
  await dialog.getByLabel('平台实例', { exact: true }).selectOption('onebot-test');
  await dialog.getByLabel('目标群号').fill('20000');
  await dialog.getByLabel('会话成员 ID', { exact: false }).fill('30000');
  await dialog.getByLabel('唤醒正文', { exact: false }).fill('请处理这条消息');
  await dialog.getByRole('button', { name: '提交唤醒' }).click();
  await page.getByText('唤醒失败, 输入已保留: 目标群暂不可用', { exact: true }).waitFor();
  assert.equal(await dialog.getByLabel('唤醒正文', { exact: false }).inputValue(), '请处理这条消息');
  responseMode = 'accepted';
  await dialog.getByRole('button', { name: '提交唤醒' }).click();
  // 受理后弹窗保留, 跟踪面板轮询至终态, 拒绝记录面板渲染受控记录
  const statusPanel = dialog.getByTestId('wake-status-panel');
  await statusPanel.waitFor();
  await statusPanel.getByText('执行中', { exact: true }).waitFor();
  await statusPanel.getByText('已送达', { exact: true }).waitFor({ timeout: 10000 });
  const rejectionsPanel = dialog.getByTestId('wake-rejections-panel');
  await rejectionsPanel.waitFor();
  await rejectionsPanel.getByText(/请求频率限制/).waitFor();
  await rejectionsPanel.getByText(/frequency_cooldown/).waitFor();
  // 手动请求同样能看到阶段结果: 附件失败与未确认发送分段同时可见
  const diagnostics = dialog.getByTestId('request-diagnostics-panel');
  await diagnostics.waitFor();
  assert.equal(diagnosticsCalls.at(-1), 'onebot-test');
  const focusedRow = diagnostics.getByTestId('diagnostics-row');
  await focusedRow.getByText('补全 · 失败', { exact: true }).waitFor();
  await focusedRow.getByText(/语音 不支持/).waitFor();
  await focusedRow.getByRole('button', { name: '展开详情' }).click();
  const detail = focusedRow.getByTestId('request-diagnostic-detail');
  await detail.getByText('业务段: 已确认 0 · 未确认 1 · 失败 0', { exact: true }).waitFor();
  await detail.getByText('不确定, 不自动重发', { exact: true }).waitFor();
  await detail.getByText(/有已提交未确认的动作, 结果不确定/).waitFor();
  assert.equal(await dialog.isVisible(), true);
  assert.equal(requests[0].request_id, requests[1].request_id);
  assert.equal(requests[1].group_id, '20000');
  assert.equal('operator' in requests[1], false);
  await page.keyboard.press('Escape');
  await dialog.waitFor({ state: 'hidden' });
  await page.getByRole('button', { name: '手动唤醒群聊', exact: true }).click();
  await dialog.getByLabel('平台实例', { exact: true }).selectOption('onebot-test');
  await dialog.getByLabel('目标群号').fill('20000');
  await dialog.getByLabel('会话成员 ID', { exact: false }).fill('30000');
  responseMode = 'no_pending';
  await dialog.getByRole('button', { name: '提交唤醒' }).click();
  await page.getByText('此群与成员范围内没有待处理正文', { exact: true }).waitFor();
  assert.equal(await dialog.isVisible(), true);
  // ── 批次8: unknown 与存储降级不得显示成已送达/确定失败 ──
  await page.keyboard.press('Escape');
  await dialog.waitFor({ state: 'hidden' });
  await page.getByRole('button', { name: '手动唤醒群聊', exact: true }).click();
  await dialog.getByLabel('平台实例', { exact: true }).selectOption('onebot-test');
  await dialog.getByLabel('目标群号').fill('20000');
  await dialog.getByLabel('会话成员 ID', { exact: false }).fill('30000');
  await dialog.getByLabel('唤醒正文', { exact: false }).fill('未知结果场景');
  responseMode = 'accepted';
  statusMode = 'unknown';
  const requestsBeforeUnknown = requests.length;
  await dialog.getByRole('button', { name: '提交唤醒' }).click();
  const unknownPanel = dialog.getByTestId('wake-status-panel');
  await unknownPanel.getByText('状态未知', { exact: true }).waitFor();
  await unknownPanel.getByText('不确定, 不自动重发', { exact: true }).waitFor();
  // 不确定结果不触发自动重发: 请求数只增加本次提交一次
  assert.equal(requests.length, requestsBeforeUnknown + 1);
  await page.waitForTimeout(2200);
  assert.equal(requests.length, requestsBeforeUnknown + 1);
  // 存储降级: 明确显示降级而不是未找到
  statusMode = 'degraded';
  await unknownPanel.getByRole('button', { name: '刷新状态' }).click();
  await unknownPanel.getByText(/状态存储降级中/).waitFor();
  // 手动刷新按钮始终可用 (查询窗口结束后仍能重新取数)
  statusMode = 'unknown';
  await unknownPanel.getByRole('button', { name: '刷新状态' }).click();
  await unknownPanel.getByText('不确定, 不自动重发', { exact: true }).waitFor();

  await page.setViewportSize({ width: 390, height: 844 });
  const artifacts = path.join(root, 'test-results', 'manual-wake');
  await fs.mkdir(artifacts, { recursive: true });
  await page.screenshot({ path: path.join(artifacts, 'mobile.png'), animations: 'disabled' });
  assert.deepEqual(errors, []);
  console.log('PASS: 手动唤醒页面, 错误保留输入, 幂等重试, 状态跟踪至终态, 拒绝记录列表, 空窗口提示, 阶段诊断, unknown 不重发, 存储降级显示');
} finally {
  await browser?.close();
  await server.close();
}
