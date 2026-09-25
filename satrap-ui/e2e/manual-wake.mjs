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
  // 两种真实拒绝信封: 'reason' 是 409 + 稳定原因码, 'plain' 是 400 + 参数解析错误文本
  let wakeErrorShape = 'reason';
  let statusMode = 'poll';
  let statusCalls = 0;
  let legacyRejectionsCalls = 0;
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
      const body = request.postDataJSON();
      requests.push(body);
      if (responseMode === 'error') {
        if (wakeErrorShape === 'plain') {
          // 参数解析失败的信封: 只有 error 字段, 没有 reason (后端 _parse_json_object 失败路径)
          return route.fulfill({ headers, status: 400,
            json: { status: 'rejected', error: '请求体必须是 JSON 对象' } });
        }
        // 真实拒绝信封: 后端只给 status/reason, 不含 error 字段 (E2)
        return route.fulfill({ headers, status: 409,
          json: { status: 'rejected', request_id: body.request_id, reason: 'queue_full' } });
      }
      return route.fulfill({ headers, status: 200, json: { status: responseMode } });
    }
    if (pathname === '/api/platforms/wake/rejections') {
      // 旧拒绝记录接口: 弹窗改用阶段诊断面板后不得再请求, 计数器只为断言 (返回空不影响页面)
      legacyRejectionsCalls += 1;
      return route.fulfill({ headers, json: { records: [] } });
    }
    if (pathname.startsWith('/api/platforms/wake/diagnostics')) {
      // 阶段诊断与手动状态是两个接口, 必须先分流 (批次8 在弹窗内展示阶段结果)
      const tail = pathname.replace('/api/platforms/wake/diagnostics', '').replace(/^\//, '');
      const query = new URL(request.url()).searchParams;
      diagnosticsCalls.push({ adapterId: query.get('adapter_id') || '', stage: query.get('stage') || '', requestId: query.get('request_id') || '' });
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
      const executionRecords = [{
        request_id: params.get('request_id') || 'req-1', adapter_id: 'onebot-test', session_id: 'group:20000',
        actor_id: '30000', self_id: '1', message_id: '', recorded_at: '2026-09-24T10:15:33',
        stages: ['projection', 'model', 'send'],
        statuses: { projection: 'failed', model: 'ok', send: 'unknown' },
        reason_codes: ['failed', 'completed', 'in_flight_unconfirmed'],
        attachments: 'record:unsupported:asr_not_configured', notes: '', send_status: 'submitted', turn_id: 'turn-1',
      }];
      // 拒绝预设: 只保留命中拒绝阶段的请求, 但仍返回该请求的全部阶段 (限流请求另带一条发送阶段 skip)
      const rejectionRecords = [
        { request_id: 'req-rejected', adapter_id: 'onebot-test', session_id: 'group:20000', actor_id: '30000', self_id: '1', message_id: '', recorded_at: '2026-09-24T10:14:00',
          stages: ['wake_decision'], statuses: { wake_decision: 'dropped' }, reason_codes: ['not_woken'], attachments: '', notes: '', send_status: '', turn_id: '' },
        { request_id: 'req-limited', adapter_id: 'onebot-test', session_id: 'group:20000', actor_id: '30000', self_id: '1', message_id: '', recorded_at: '2026-09-24T10:15:30',
          stages: ['rate_limit', 'send'], statuses: { rate_limit: 'dropped', send: 'skipped' }, reason_codes: ['rate_limited', 'no_business_send'], attachments: '', notes: 'rate_limited', send_status: '', turn_id: '' },
      ];
      const records = params.get('stage') === 'wake_decision,rate_limit' ? rejectionRecords : executionRecords;
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
  // 未跟踪请求时默认"仅看拒绝": 提交前即可看到近期被拒绝的请求, 行内仍展示该请求的全部阶段
  const diagnostics = dialog.getByTestId('request-diagnostics-panel');
  await diagnostics.waitFor();
  const rejectionsOnly = diagnostics.getByTestId('diagnostics-rejections-only');
  await rejectionsOnly.waitFor();
  assert.equal(await rejectionsOnly.isChecked(), true);
  await diagnostics.getByText('唤醒决策 · 未处理', { exact: true }).waitFor();
  await diagnostics.getByText('限流 · 未处理', { exact: true }).waitFor();
  assert.equal(diagnosticsCalls.some((call) => call.adapterId === 'onebot-test' && call.stage === 'wake_decision,rate_limit' && call.requestId === ''), true);
  // 取消预设回到近期请求浏览, 重新勾选后可继续筛选拒绝
  await rejectionsOnly.uncheck();
  await diagnostics.getByText('补全 · 失败', { exact: true }).waitFor();
  assert.equal(diagnosticsCalls.at(-1).stage, '');
  await rejectionsOnly.check();
  await diagnostics.getByText('唤醒决策 · 未处理', { exact: true }).waitFor();
  await dialog.getByLabel('目标群号').fill('20000');
  await dialog.getByLabel('会话成员 ID', { exact: false }).fill('30000');
  await dialog.getByLabel('唤醒正文', { exact: false }).fill('请处理这条消息');
  await dialog.getByRole('button', { name: '提交唤醒' }).click();
  await page.getByText('唤醒失败, 输入已保留: 平台事件队列已满, 请稍后重试', { exact: true }).waitFor();
  assert.equal(await dialog.getByLabel('唤醒正文', { exact: false }).inputValue(), '请处理这条消息');
  // 400 + error 信封同样要显示后端原文并保留输入, 不能只靠辅助函数单测覆盖
  wakeErrorShape = 'plain';
  await dialog.getByRole('button', { name: '提交唤醒' }).click();
  await page.getByText('唤醒失败, 输入已保留: 请求体必须是 JSON 对象', { exact: true }).waitFor();
  assert.equal(await dialog.getByLabel('唤醒正文', { exact: false }).inputValue(), '请处理这条消息');
  wakeErrorShape = 'reason';
  responseMode = 'accepted';
  await dialog.getByRole('button', { name: '提交唤醒' }).click();
  // 受理后弹窗保留, 跟踪面板轮询至终态, 聚焦请求自动取消拒绝过滤并禁用该筛选
  const statusPanel = dialog.getByTestId('wake-status-panel');
  await statusPanel.waitFor();
  await statusPanel.getByText('执行中', { exact: true }).waitFor();
  await statusPanel.getByText('已送达', { exact: true }).waitFor({ timeout: 10000 });
  assert.equal(await rejectionsOnly.isChecked(), false);
  assert.equal(await rejectionsOnly.isDisabled(), true);
  // 手动请求同样能看到阶段结果: 附件失败与未确认发送分段同时可见
  const focusedRow = diagnostics.getByTestId('diagnostics-row');
  await focusedRow.getByText('补全 · 失败', { exact: true }).waitFor();
  await focusedRow.getByText(/语音 不支持/).waitFor();
  await focusedRow.getByRole('button', { name: '展开详情' }).click();
  const detail = focusedRow.getByTestId('request-diagnostic-detail');
  await detail.getByText('业务段: 已确认 0 · 未确认 1 · 失败 0', { exact: true }).waitFor();
  await detail.getByText('不确定, 不自动重发', { exact: true }).waitFor();
  await detail.getByText(/有已提交未确认的动作, 结果不确定/).waitFor();
  assert.equal(diagnosticsCalls.some((call) => call.stage === '' && call.requestId === requests[1].request_id), true);
  assert.equal(await dialog.isVisible(), true);
  assert.equal(requests[0].request_id, requests[1].request_id);
  assert.equal(requests[1].group_id, '20000');
  assert.equal('operator' in requests[1], false);
  // 返回近期请求: 清除聚焦后默认预设恢复, 不再残留旧过滤
  const callsBeforeReturn = diagnosticsCalls.length;
  await diagnostics.getByTestId('diagnostics-clear-request').click();
  await diagnostics.getByText('唤醒决策 · 未处理', { exact: true }).waitFor();
  assert.equal(await rejectionsOnly.isChecked(), true);
  assert.equal(await rejectionsOnly.isDisabled(), false);
  assert.equal(diagnosticsCalls.slice(callsBeforeReturn).some((call) => call.stage === 'wake_decision,rate_limit' && call.requestId === ''), true);
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
  // 弹窗只走阶段诊断接口, 不再请求旧拒绝记录端点
  assert.equal(legacyRejectionsCalls, 0);
  console.log('PASS: 手动唤醒页面, 400/409 两种错误信封均保留输入, 幂等重试, 状态跟踪至终态, 拒绝预设与返回近期请求, 空窗口提示, 阶段诊断, unknown 不重发, 存储降级显示');
} finally {
  await browser?.close();
  await server.close();
}
