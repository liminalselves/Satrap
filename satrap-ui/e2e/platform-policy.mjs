/** 平台策略浏览器回归, 使用受控接口验证表单与保存行为 */
import assert from 'node:assert/strict';
import path from 'node:path';
import fs from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';
import { createServer } from 'vite';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const server = await createServer({ root, server: { host: '127.0.0.1', port: 0 } });
const artifacts = path.join(root, 'test-results', 'platform-policy');
let browser;
try {
  await fs.mkdir(artifacts, { recursive: true });
  await server.listen();
  const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
  browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  let platforms = [{ id: 'legacy-bot', type: 'aiocqhttp', settings: { extension: 'keep', access_token: 'test-secret-not-for-summary', group_whitelist: ['123'], wake_words: ['小助手'], wake_message_threshold: 5, message_text_limit: 1500 } }];
  let revision = 1;
  const writes = [];
  let applications = [];
  let reloadCount = 0;
  const dryRunCalls = [];
  const diagnosticsCalls = [];
  let diagnosticsFail = false;
  const diagnosticsDetail = {
    request_id: 'manual-1', adapter_id: 'new-bot', available: true, truncated: false,
    stages: ['projection', 'model', 'send'],
    records: [
      { recorded_at: '2026-09-24T10:00:01+08:00', adapter_id: 'new-bot', session_id: 'group:200', actor_id: '7', stage: 'projection', decision: 'partial', reason: '附件已解析 1 项, 未完成 1 项', message_id: '', request_id: 'manual-1', send_status: '', self_id: '1', status: 'partial', reason_code: 'partial', turn_id: 'turn-1', attachments: 'record:unsupported:asr_reason,file:failed:no_remote_url', notes: '' },
      { recorded_at: '2026-09-24T10:00:02+08:00', adapter_id: 'new-bot', session_id: 'group:200', actor_id: '7', stage: 'model', decision: 'ok', reason: '模型输出 12 字符', message_id: '', request_id: 'manual-1', send_status: '', self_id: '1', status: 'ok', reason_code: 'completed', turn_id: 'turn-1', attachments: '', notes: '' },
      { recorded_at: '2026-09-24T10:00:03+08:00', adapter_id: 'new-bot', session_id: 'group:200', actor_id: '7', stage: 'send', decision: 'unknown', reason: '业务段 已确认 1 未确认 1 失败 0', message_id: '', request_id: 'manual-1', send_status: 'submitted', self_id: '1', status: 'unknown', reason_code: 'in_flight_unconfirmed', turn_id: 'turn-1', attachments: '', notes: '' },
    ],
  };
  const diagnosticsManual = {
    request_id: 'manual-1', adapter_id: 'new-bot', session_id: 'group:200', actor_id: '7', self_id: '1', message_id: '',
    recorded_at: '2026-09-24T10:00:03+08:00', stages: ['projection', 'model', 'send'],
    statuses: { projection: 'partial', model: 'ok', send: 'unknown' },
    reason_codes: ['partial', 'completed', 'in_flight_unconfirmed'],
    attachments: 'record:unsupported:asr_reason', notes: '', send_status: 'submitted', turn_id: 'turn-1',
  };
  const diagnosticsAuto = {
    request_id: 'auto-1', adapter_id: 'legacy-bot', session_id: 'group:123', actor_id: '9', self_id: '1', message_id: 'm-9',
    recorded_at: '2026-09-24T09:59:00+08:00', stages: ['wake_decision', 'rate_limit'],
    statuses: { wake_decision: 'dropped', rate_limit: 'dropped' },
    reason_codes: ['wake_decision', 'rate_limit'], attachments: '', notes: '', send_status: '', turn_id: '',
  };
  await context.route(`${origin}/ui-config.json`, (route) => route.fulfill({ json: {
    backend_api: 'http://127.0.0.1:19870', control_api: 'http://127.0.0.1:19871', chat_api: 'http://127.0.0.1:19872',
  } }));
  await context.route(/^http:\/\/127\.0\.0\.1:1987[012]\//, async (route) => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname;
    const headers = { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true', 'Access-Control-Allow-Headers': 'Content-Type', 'Access-Control-Allow-Methods': 'GET, POST, PUT, DELETE, OPTIONS' };
    if (request.method() === 'OPTIONS') return route.fulfill({ status: 204, headers });
    if (pathname.startsWith('/config/platforms') && ['POST', 'PUT'].includes(request.method())) {
      if (new URL(request.url()).searchParams.get("expected_revision") !== String(revision)) {
        return route.fulfill({ status: 409, json: { ok: false, error: "配置已被其他操作修改" }, headers });
      }
      const payload = request.postDataJSON();
      if (payload.settings.group_whitelist?.some((id) => !/^[1-9]\d*$/.test(id))) {
        return route.fulfill({ json: { ok: false, error: 'group_whitelist 必须包含正整数群 ID' }, headers });
      }
      revision++;
      writes.push(payload);
      platforms = [...platforms.filter((item) => item.id !== payload.id), payload];
    }
    if (pathname === '/config/wake-dry-run') {
      const payload = request.postDataJSON();
      dryRunCalls.push(payload);
      const explicit = payload.probe
        ? { triggered: false, rule: 'no_match', reason: '未命中唤醒词或 @', score: null }
        : { triggered: null, rule: 'undetermined', reason: '未提供探测消息, 无法评估显式唤醒', score: null };
      return route.fulfill({ headers, json: {
        ok: true,
        resolved: { wake_mode: 'frequency', wake_message_threshold: 2, wake_cooldown: 60 },
        sources: {
          input_text_limit: { value: 500, source: 'platform', source_index: null, source_label: '平台配置' },
          wake_cooldown: { value: 60, source: 'time_rule', source_index: 0, source_label: '09:00-18:00 (第 1 条时段规则)' },
          wake_score_threshold: { value: 0.65, source: 'builtin_default', source_index: null, source_label: '未设置, 使用默认值' },
        },
        defaults: { input_text_limit: 20000, input_media_limit: 8 },
        explicit,
        automatic: {
          mode: 'frequency', observed: 2, steps: [],
          decision: { triggered: true, rule: 'frequency_threshold', reason: '累计有效正文达到阈值', score: null },
          deadline_decision: { triggered: false, rule: 'deadline_wait', reason: '未到最后期限', score: null },
          cooldown_remaining: 0,
          threshold: {
            value: 2, source: 'explicit', label: '2 (显式 wake_message_threshold)', talk_value: 0,
            talk_value_effective: false, closed: false, overridden: true,
            hint: 'wake_talk_value 已被显式 wake_message_threshold 覆盖, 不参与频率判断',
          },
        },
      } });
    }
    if (pathname.startsWith('/api/platforms/wake/diagnostics')) {
      // 阶段诊断: 列表与详情 (批次6 接口), 用受控记录验证中文阶段展示与筛选
      const url = new URL(request.url());
      const tail = pathname.replace('/api/platforms/wake/diagnostics', '').replace(/^\//, '');
      diagnosticsCalls.push({ adapterId: url.searchParams.get('adapter_id') || '', requestId: url.searchParams.get('request_id') || '', detail: tail, url: request.url() });
      if (diagnosticsFail) return route.fulfill({ status: 500, json: { error: 'diagnostics_unavailable' }, headers });
      if (tail) return route.fulfill({ headers, json: { ...diagnosticsDetail, request_id: tail } });
      const adapterId = url.searchParams.get('adapter_id') || '';
      const base = adapterId === 'quiet-bot' ? [] : adapterId === 'new-bot' ? [diagnosticsManual] : adapterId === 'legacy-bot' ? [diagnosticsAuto] : [diagnosticsManual, diagnosticsAuto];
      // 服务端按"命中任一请求阶段"过滤, 命中后仍返回该请求全部阶段
      const stageFilter = (url.searchParams.get('stage') || '').split(',').filter(Boolean);
      const records = stageFilter.length ? base.filter((item) => item.stages.some((stage) => stageFilter.includes(stage))) : base;
      return route.fulfill({ headers, json: { records, available: true, capacity: 256, records_per_request: 16, requests_total: records.length, records_total: records.length * 3 } });
    }
    if (pathname === '/api/config/reload') {
      assert.equal(request.postDataJSON().expected_config_revision, String(revision));
      const status = ['applied', 'pending_restart', 'failed'][Math.min(reloadCount++, 2)];
      applications = platforms.map((item) => ({ id: item.id, status, saved_revision: 'saved-version', active_revision: status === 'applied' ? 'saved-version' : 'old-version', error: status === 'failed' ? '模拟应用失败' : undefined }));
    }
    const responses = {
      '/api/health': { running: true, adapters: { 'quiet-bot': { config_type: 'onebot', status: 'running', started: true } }, platform_config: applications }, '/status': { running: true },
      '/api/config/reload': { ok: true, edictum_sessions: [], platforms: applications },
      '/config/session-classes': {}, '/config/edictum/sessions': {},
      '/api/sessions': { sessions: [] }, '/config/session-instances': { sessions: [] },
      // /chat 直达所需的聊天接口空数据 (聊天页自身行为由 chat-reconnect 脚本覆盖)
      '/api/chat/health': { ok: true, conversations: 0, preloaded: 0 }, '/api/chat/conversations': { conversations: [] },
      '/api/chat/turns': { turns: [] }, '/api/chat/models': { models: [] }, '/api/chat/models/detail': { ok: true, models: {} },
      '/api/chat/plugins': { plugins: [] }, '/api/chat/runs': { ok: true, runs: [], next_cursor: null },
      '/api/projects': { projects: [] },
    };
    return route.fulfill({ json: pathname.startsWith('/config/platforms') ? { ok: true, platforms, revision: String(revision) } : responses[pathname] ?? { ok: true }, headers });
  });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.routeWebSocket('ws://127.0.0.1:19870/ws/status**', () => {});
  await page.routeWebSocket('ws://127.0.0.1:19872/ws/chat**', () => {});
  await page.goto(`${origin}/platforms`);
  await Promise.all([page.waitForResponse((response) => response.url().endsWith('/api/health')), page.getByRole('button', { name: '刷新', exact: true }).click()]);
  assert.equal(await page.getByText('test-secret-not-for-summary', { exact: false }).count(), 0);
  await page.getByTitle('编辑', { exact: true }).click();
  const dialog = page.getByRole('dialog');
  const scope = dialog.getByLabel('上下文范围', { exact: false });
  assert.equal(await scope.inputValue(), 'legacy_user');
  assert.equal(await dialog.getByLabel('群白名单', { exact: false }).inputValue(), '123');
  // 已保存的数字策略在编辑时必须回填, 否则空白显示会让用户误以为未设置
  assert.equal(await dialog.getByLabel('自动参与消息阈值', { exact: false }).inputValue(), '5');
  assert.equal(await dialog.getByLabel('每条消息文本上限', { exact: false }).inputValue(), '1500');
  await scope.selectOption('group_member');
  await dialog.getByLabel('群白名单', { exact: false }).fill('123\n456');
  await dialog.getByLabel('唤醒词', { exact: false }).fill('小助手\n hello bot ');
  await dialog.getByLabel('机器人名字/别名', { exact: false }).fill('小萨\n Satrap ');
  await dialog.getByRole('button', { name: '保存修改' }).click();
  await dialog.waitFor({ state: 'hidden' });
  assert.deepEqual(writes[0].settings.group_whitelist, ['123', '456']);
  assert.deepEqual(writes[0].settings.wake_words, ['小助手', 'hello bot']);
  assert.deepEqual(writes[0].settings.wake_aliases, ['小萨', 'Satrap']);
  assert.equal(writes[0].settings.context_scope, 'group_member');
  assert.equal(writes[0].settings.extension, 'keep');
  await page.getByText('已生效', { exact: true }).waitFor();
  await page.getByTitle('编辑', { exact: true }).click();
  assert.equal(await scope.inputValue(), 'group_member');
  await dialog.getByLabel('群白名单', { exact: false }).fill('invalid');
  await dialog.getByRole('button', { name: '保存修改' }).click();
  await page.getByText('保存失败: group_whitelist 必须包含正整数群 ID', { exact: true }).waitFor();
  assert.equal(await dialog.getByLabel('群白名单', { exact: false }).inputValue(), 'invalid');
  await dialog.getByLabel('群白名单', { exact: false }).fill('');
  await dialog.getByRole('button', { name: '保存修改' }).click();
  await dialog.waitFor({ state: 'hidden' });
  assert.deepEqual(writes.at(-1).settings.group_whitelist, []);
  await page.getByText('待重启', { exact: true }).waitFor();
  await page.getByRole('button', { name: '添加平台', exact: true }).click();
  await dialog.getByLabel('类型', { exact: true }).selectOption('onebot');
  assert.equal(await scope.inputValue(), 'group_member');
  await dialog.getByLabel('平台名称').fill('new-bot');
  await dialog.getByLabel('自动参与模式', { exact: true }).selectOption('necessity');
  await dialog.getByLabel('必要性评分阈值', { exact: false }).fill('0.75');
  await dialog.getByLabel('频率模式最长等待秒数', { exact: false }).fill('45');
  const voice = dialog.getByLabel('语音转写来源', { exact: false });
  assert.equal(await voice.inputValue(), 'asr');
  await voice.selectOption('asr_then_platform');
  for (const theme of ['dark', 'light']) {
    await page.evaluate((value) => document.documentElement.setAttribute('data-theme', value), theme);
    await scope.scrollIntoViewIfNeeded();
    await page.screenshot({ animations: 'disabled', path: path.join(artifacts, `${theme}-desktop.png`) });
  }
  await page.setViewportSize({ width: 390, height: 844 });
  await scope.scrollIntoViewIfNeeded();
  await scope.focus();
  await page.keyboard.press('Tab');
  assert.equal(await dialog.getByLabel('群白名单', { exact: false }).evaluate((element) => element === document.activeElement), true);
  const bounds = await dialog.boundingBox();
  assert.ok(bounds.x >= 0 && bounds.x + bounds.width <= 390);
  await page.screenshot({ animations: 'disabled', path: path.join(artifacts, 'light-mobile.png') });
  await dialog.getByRole('button', { name: '创建', exact: true }).click();
  await dialog.waitFor({ state: 'hidden' });
  assert.equal(writes.at(-1).settings.context_scope, 'group_member');
  assert.equal(writes.at(-1).settings.wake_mode, 'necessity');
  assert.equal(writes.at(-1).settings.wake_score_threshold, 0.75);
  assert.equal(writes.at(-1).settings.wake_max_wait, 45);
  assert.equal(writes.at(-1).settings.voice_transcribe, 'asr_then_platform');
  assert.equal('voice_transcribe' in writes[0].settings, false);
  await page.getByText('应用失败', { exact: true }).first().waitFor();
  await page.getByTitle('编辑', { exact: true }).first().click();
  await dialog.getByLabel('唤醒词', { exact: false }).fill('保留我的草稿');
  revision++;
  const writesBeforeConflict = writes.length;
  await dialog.getByRole('button', { name: '保存修改' }).click();
  await page.getByText('保存失败: 配置已被其他操作修改, 当前草稿已保留; 请复制草稿并刷新后合并', { exact: true }).waitFor();
  assert.equal(await dialog.getByLabel('唤醒词', { exact: false }).inputValue(), '保留我的草稿');
  assert.equal(writes.length, writesBeforeConflict);

  // ── 批次4: 脏保护 (冲突后草稿未保存, 弹窗处于脏状态) ──
  let closeAction = 'dismiss';
  const closeHandler = (nativeDialog) => { void nativeDialog[closeAction](); };
  page.on('dialog', closeHandler);
  await page.keyboard.press('Escape');
  await page.waitForTimeout(300);
  assert.equal(await dialog.isVisible(), true);
  closeAction = 'accept';
  await page.keyboard.press('Escape');
  await dialog.waitFor({ state: 'hidden' });
  page.off('dialog', closeHandler);

  // ── 批次4: 群覆盖/时段规则行编辑器 ──
  // 冲突后页面本地版本过期, 先刷新同步再编辑保存
  await Promise.all([
    page.waitForResponse((response) => response.url().includes('/config/platforms')),
    page.getByRole('button', { name: '刷新', exact: true }).click(),
  ]);
  await page.getByTitle('编辑', { exact: true }).first().click();
  const groupEditor = dialog.getByTestId('wake-group-editor');
  await groupEditor.getByRole('button', { name: '添加群覆盖' }).click();
  await groupEditor.getByLabel('群号').fill('20');
  await groupEditor.getByLabel('自动参与模式状态').selectOption('value');
  await groupEditor.getByLabel('自动参与模式值').selectOption('necessity');
  // 数值字段的 label 带契约范围文本, 用正则定位状态与取值控件
  await groupEditor.getByLabel(/^冷却秒数.*状态$/).selectOption('value');
  await groupEditor.getByLabel(/^冷却秒数.*值$/).fill('30');
  await groupEditor.getByLabel('唤醒词状态').selectOption('off');
  await groupEditor.locator('span').filter({ hasText: '清空继承词表' }).waitFor();
  // 行内字段按契约严格校验: 排他上界 120 被拒并保留草稿, 改回继承后恢复可保存
  const groupMaxWaitState = groupEditor.getByLabel(/^最长等待秒数.*状态$/);
  const groupMaxWaitValue = groupEditor.getByLabel(/^最长等待秒数.*值$/);
  await groupMaxWaitState.selectOption('value');
  await groupMaxWaitValue.fill('120');
  const writesBeforeRowIssue = writes.length;
  await dialog.getByRole('button', { name: '保存修改' }).click();
  // 行内错误与草稿校验共用同一个结论, 试算同时被阻止
  await groupEditor.getByText(/最长等待秒数必须大于等于 0 且小于 120/).waitFor();
  const blockedPreview = dialog.getByTestId('wake-dry-run-blocked');
  await blockedPreview.waitFor();
  assert.match(await blockedPreview.innerText(), /群级\/时段规则有误/);
  assert.match(await blockedPreview.innerText(), /最长等待秒数必须大于等于 0 且小于 120/);
  assert.equal(writes.length, writesBeforeRowIssue);
  assert.equal(await groupMaxWaitValue.inputValue(), '120');
  await groupMaxWaitState.selectOption('inherit');
  const timeEditor = dialog.getByTestId('wake-time-editor');
  await timeEditor.getByRole('button', { name: '添加时段规则' }).click();
  await timeEditor.getByLabel('开始时间').fill('23:00');
  await timeEditor.getByLabel('结束时间').fill('07:00');
  await timeEditor.getByLabel(/^发言频率偏好.*状态$/).selectOption('off');
  await dialog.getByRole('button', { name: '保存修改' }).click();
  await dialog.waitFor({ state: 'hidden' });
  assert.deepEqual(writes.at(-1).settings.wake_group_overrides, { '20': { wake_mode: 'necessity', wake_cooldown: 30, wake_words: [] } });
  assert.deepEqual(writes.at(-1).settings.wake_time_rules, [{ start: '23:00', end: '07:00', settings: { wake_talk_value: 0 } }]);

  // ── 批次7: 平台级输入预算与 talk_value 往返 (旧平台: 别名 aiocqhttp 且含未知扩展字段) ──
  const legacyRow = page.getByText('legacy-bot', { exact: true }).locator('xpath=../..');
  await legacyRow.getByTitle('编辑', { exact: true }).click();
  const talkField = dialog.getByLabel('发言频率偏好 talk_value', { exact: false });
  const textBudget = dialog.getByLabel('单条消息输入文本预算', { exact: false });
  const mediaBudget = dialog.getByLabel('单条消息输入媒体上限', { exact: false });
  // 未设置时留空, 提示里给出默认值与仅平台级的约束
  assert.equal(await talkField.inputValue(), '');
  assert.equal(await textBudget.inputValue(), '');
  assert.equal(await mediaBudget.inputValue(), '');
  assert.match(await talkField.getAttribute('placeholder'), /留空表示未设置/);
  assert.match(await mediaBudget.getAttribute('placeholder'), /不进入群\/时段覆盖/);
  await textBudget.fill('500');
  await mediaBudget.fill('4');
  await talkField.fill('0');
  // talk_value 提示是静态文案: 前端不再自行推导"被显式阈值覆盖/关闭自动参与"结论
  const staticHint = await talkField.getAttribute('placeholder');
  assert.match(staticHint, /以试算结论为准/);
  assert.doesNotMatch(staticHint, /被显式阈值覆盖|关闭自动参与/);
  const thresholdField = dialog.getByLabel('自动参与消息阈值', { exact: false });
  await thresholdField.fill('');
  await talkField.fill('0.5');
  await thresholdField.fill('5');
  assert.equal(await talkField.getAttribute('placeholder'), staticHint);
  const overriddenTalkHint = await talkField.getAttribute('placeholder');
  assert.doesNotMatch(overriddenTalkHint, /被显式阈值覆盖|关闭自动参与/);
  await talkField.fill('0');
  // 命令操作员名单: 每行一项, 保存为字符串列表; 提示写明只有名单内的成员能执行受保护命令
  const operators = dialog.getByLabel('命令操作员名单', { exact: false });
  assert.match(await operators.getAttribute('placeholder'), /只有名单内的成员能执行/);
  await operators.fill(' 10001 \n\n10002 ');
  await dialog.getByRole('button', { name: '保存修改' }).click();
  await dialog.waitFor({ state: 'hidden' });
  assert.equal(writes.at(-1).id, 'legacy-bot');
  assert.deepEqual(writes.at(-1).settings.command_operators, ['10001', '10002']);
  assert.equal(writes.at(-1).settings.input_text_limit, 500);
  assert.equal(writes.at(-1).settings.input_media_limit, 4);
  // 0 按数字保存, 不能被当作空值丢弃
  assert.equal(writes.at(-1).settings.wake_talk_value, 0);
  assert.equal(writes.at(-1).settings.wake_message_threshold, 5);
  assert.equal(writes.at(-1).settings.extension, 'keep');
  await legacyRow.getByTitle('编辑', { exact: true }).click();
  assert.equal(await dialog.getByLabel('单条消息输入文本预算', { exact: false }).inputValue(), '500');
  assert.equal(await dialog.getByLabel('单条消息输入媒体上限', { exact: false }).inputValue(), '4');
  // 列表往返: 保存的数组在表单里还原成每行一项的文本
  assert.equal(await dialog.getByLabel('命令操作员名单', { exact: false }).inputValue(), '10001\n10002');
  const reopenedTalk = dialog.getByLabel('发言频率偏好 talk_value', { exact: false });
  assert.equal(await reopenedTalk.inputValue(), '0');
  // 清空表示未设置: 保存后该键被删除, 其余字段不受影响
  await reopenedTalk.fill('');
  await dialog.getByRole('button', { name: '保存修改' }).click();
  await dialog.waitFor({ state: 'hidden' });
  assert.equal('wake_talk_value' in writes.at(-1).settings, false);
  assert.equal(writes.at(-1).settings.input_text_limit, 500);
  // 范围校验在提交前拦截, 不产生写入也不丢草稿
  await legacyRow.getByTitle('编辑', { exact: true }).click();
  await dialog.getByLabel('发言频率偏好 talk_value', { exact: false }).fill('1.5');
  const writesBeforeRange = writes.length;
  await dialog.getByRole('button', { name: '保存修改' }).click();
  await page.getByText(/发言频率偏好必须在 0 到 1 之间/).waitFor();
  assert.equal(writes.length, writesBeforeRange);
  assert.equal(await dialog.isVisible(), true);
  assert.equal(await dialog.getByLabel('发言频率偏好 talk_value', { exact: false }).inputValue(), '1.5');
  // 越界的输入预算同样拦截
  await dialog.getByLabel('发言频率偏好 talk_value', { exact: false }).fill('');
  await dialog.getByLabel('单条消息输入媒体上限', { exact: false }).fill('33');
  await dialog.getByRole('button', { name: '保存修改' }).click();
  await page.getByText(/输入媒体上限必须在 1 到 32 之间/).waitFor();
  assert.equal(writes.length, writesBeforeRange);
  // 排他上界由校验器检查: 最长等待 120 被拦截, 草稿保留
  const maxWaitField = dialog.getByLabel('频率模式最长等待秒数', { exact: false });
  await dialog.getByLabel('单条消息输入媒体上限', { exact: false }).fill('4');
  await maxWaitField.fill('119.9999');
  await dialog.getByRole('button', { name: '保存修改' }).click();
  await dialog.waitFor({ state: 'hidden' });
  assert.equal(writes.at(-1).settings.wake_max_wait, 119.9999);
  await legacyRow.getByTitle('编辑', { exact: true }).click();
  await dialog.getByLabel('频率模式最长等待秒数', { exact: false }).fill('120');
  const writesBeforeExclusive = writes.length;
  await dialog.getByRole('button', { name: '保存修改' }).click();
  // 精确匹配: 行内错误的提示文案包含同一句字段消息
  await page.getByText('最长等待秒数必须大于等于 0 且小于 120（wake_max_wait）', { exact: true }).waitFor();
  assert.equal(writes.length, writesBeforeExclusive);
  assert.equal(await dialog.getByLabel('频率模式最长等待秒数', { exact: false }).inputValue(), '120');
  await dialog.getByLabel('频率模式最长等待秒数', { exact: false }).fill('');
  // 冷却保持有限非负: 超过一天的历史取值仍可保存
  await dialog.getByLabel('自动参与冷却秒数', { exact: false }).fill('86401');
  await dialog.getByRole('button', { name: '保存修改' }).click();
  await dialog.waitFor({ state: 'hidden' });
  assert.equal(writes.at(-1).settings.wake_cooldown, 86401);
  await legacyRow.getByTitle('编辑', { exact: true }).click();
  await dialog.getByLabel('单条消息输入媒体上限', { exact: false }).fill('4');
  await dialog.getByRole('button', { name: '保存修改' }).click();
  await dialog.waitFor({ state: 'hidden' });
  assert.equal(writes.at(-1).settings.message_text_limit, 1500);

  // ── 批次4: 唤醒规则试算预览 ──
  await page.getByTitle('编辑', { exact: true }).first().click();
  const dryRunPanel = dialog.getByTestId('wake-dry-run-panel');
  await dryRunPanel.getByLabel('试算群号').fill('20');
  await dryRunPanel.getByLabel('样例消息').fill('hello\nworld');
  await dryRunPanel.getByRole('button', { name: '运行试算' }).click();
  await dryRunPanel.getByText('无法判断', { exact: true }).waitFor();
  await dryRunPanel.getByText('触发', { exact: true }).waitFor();
  await dryRunPanel.getByText('wake_mode=frequency', { exact: true }).waitFor();
  // 批次7: 有效阈值与来源明细必须可见, 覆盖提示不显示成已关闭
  const thresholdRow = dryRunPanel.getByTestId('wake-threshold-preview');
  await thresholdRow.waitFor();
  assert.match(await thresholdRow.innerText(), /2 · 2 \(显式 wake_message_threshold\)/);
  await thresholdRow.getByText('被显式阈值覆盖', { exact: true }).waitFor();
  assert.equal(await thresholdRow.getByText('自动参与已关闭', { exact: true }).count(), 0);
  await dryRunPanel.getByText('wake_talk_value 已被显式 wake_message_threshold 覆盖, 不参与频率判断', { exact: true }).waitFor();
  const sourceTable = dryRunPanel.getByTestId('wake-source-table');
  await sourceTable.locator('summary').click();
  await sourceTable.getByText('时段规则 · 09:00-18:00 (第 1 条时段规则)', { exact: true }).waitFor();
  await sourceTable.getByText('默认值', { exact: true }).waitFor();
  await sourceTable.getByText('input_text_limit', { exact: true }).waitFor();
  await dryRunPanel.getByLabel('探测消息').fill('在吗');
  await dryRunPanel.getByRole('button', { name: '运行试算' }).click();
  await dryRunPanel.getByText('不触发', { exact: true }).waitFor();
  assert.equal(dryRunCalls.length, 2);
  assert.equal(dryRunCalls[0].group_id, '20');
  assert.deepEqual(dryRunCalls[0].steps, [{ text: 'hello' }, { text: 'world' }]);
  assert.deepEqual(dryRunCalls[1].probe, { text: '在吗', at_self: false });
  // 试算面板不改动表单, 直接关闭不应弹出放弃确认 (出现确认会被自动取消, 导致关窗超时)
  await page.keyboard.press('Escape');
  await dialog.waitFor({ state: 'hidden' });

  // ── 批次4: 脏保护拦截应用内导航 ──
  // 弹窗遮罩独占视口: 点击侧栏链接实际落在遮罩上, 走放弃确认; 遮罩吞掉点击不产生导航
  // (前置步骤验证过窄屏布局, 先恢复桌面视口确保侧栏可见)
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.getByTitle('编辑', { exact: true }).first().click();
  await dialog.getByLabel('唤醒词', { exact: false }).fill('拦截导航草稿');
  let navAction = 'dismiss';
  let navConfirmCount = 0;
  const navHandler = (nativeDialog) => { navConfirmCount += 1; void nativeDialog[navAction](); };
  page.on('dialog', navHandler);
  const sessionsLink = page.getByRole('link', { name: 'Agent 配置' });
  const linkBox = await sessionsLink.boundingBox();
  const linkCenter = { x: linkBox.x + linkBox.width / 2, y: linkBox.y + linkBox.height / 2 };
  await page.mouse.click(linkCenter.x, linkCenter.y);
  await page.waitForTimeout(300);
  assert.equal(navConfirmCount, 1);
  assert.ok(page.url().endsWith('/platforms'));
  assert.equal(await dialog.isVisible(), true);
  assert.equal(await dialog.getByLabel('唤醒词', { exact: false }).inputValue(), '拦截导航草稿');
  navAction = 'accept';
  await page.mouse.click(linkCenter.x, linkCenter.y);
  await dialog.waitFor({ state: 'hidden' });
  assert.equal(navConfirmCount, 2);
  assert.ok(page.url().endsWith('/platforms'));
  await sessionsLink.click();
  await page.waitForURL('**/agents');
  page.off('dialog', navHandler);

  // ── 批次8 (B8): 草稿行由表单持有, 校验只报错不删行 ──
  // legacy-bot 是唯一带群覆盖与时段规则的平台 (批次4 在该平台保存)
  await page.goto(`${origin}/platforms`);
  await legacyRow.getByTitle('编辑', { exact: true }).click();
  const rowsEditor = dialog.getByTestId('wake-group-editor');
  const groupRow = (index) => rowsEditor.locator('[data-row-id]').nth(index);
  const rowErrors = () => rowsEditor.getByTestId('wake-override-row-error');
  assert.equal(await rowsEditor.locator('[data-row-id]').count(), 1);
  // 已有群号清空: 行与同行其它显式值必须保留, 只是被标为错误
  await rowsEditor.getByLabel('群号').fill('');
  await rowErrors().getByText(/群号不能为空/).waitFor();
  assert.equal(await rowsEditor.locator('[data-row-id]').count(), 1);
  assert.equal(await rowsEditor.getByLabel('自动参与模式值').inputValue(), 'necessity');
  assert.equal(await rowsEditor.getByLabel(/^冷却秒数.*值$/).inputValue(), '30');
  await rowsEditor.getByLabel('群号').fill('20');
  await page.waitForFunction(() => document.querySelectorAll('[data-testid="wake-override-row-error"]').length === 0);
  // 重复群号: 保留第二行并在该行上报错, 保存被阻止
  await rowsEditor.getByRole('button', { name: '添加群覆盖' }).click();
  await rowsEditor.getByLabel('群号').nth(1).fill('20');
  await groupRow(1).getByText(/群号 20 重复/).waitFor();
  assert.equal(await rowsEditor.locator('[data-row-id]').count(), 2);
  let writesBeforeRowError = writes.length;
  await dialog.getByRole('button', { name: '保存修改' }).click();
  await page.getByText(/群号 20 重复/).last().waitFor();
  assert.equal(writes.length, writesBeforeRowError);
  assert.equal(await rowsEditor.locator('[data-row-id]').count(), 2);
  // 删除中间行: 按 rowId 删除, 其余行保留各自的值, 不发生错位
  await groupRow(1).getByLabel('群号').fill('31');
  await groupRow(1).getByLabel(/^冷却秒数.*状态$/).selectOption('value');
  await groupRow(1).getByLabel(/^冷却秒数.*值$/).fill('11');
  await rowsEditor.getByRole('button', { name: '添加群覆盖' }).click();
  await groupRow(2).getByLabel('群号').fill('32');
  await groupRow(2).getByLabel(/^冷却秒数.*状态$/).selectOption('value');
  await groupRow(2).getByLabel(/^冷却秒数.*值$/).fill('22');
  await groupRow(1).getByRole('button', { name: '删除群覆盖' }).click();
  assert.equal(await rowsEditor.locator('[data-row-id]').count(), 2);
  assert.equal(await rowsEditor.getByLabel('群号').nth(0).inputValue(), '20');
  assert.equal(await rowsEditor.getByLabel('群号').nth(1).inputValue(), '32');
  assert.equal(await rowsEditor.getByLabel(/^冷却秒数.*值$/).nth(1).inputValue(), '22');
  // 最后字段改回继承: 行不消失, 只是不再写入覆盖
  await groupRow(1).getByLabel(/^冷却秒数.*状态$/).selectOption('inherit');
  assert.equal(await rowsEditor.locator('[data-row-id]').count(), 2);
  assert.equal(await rowsEditor.getByLabel(/^冷却秒数.*值$/).count(), 1);
  // 已有时段暂时无效: 行与同行的显式关闭值保留, 保存被阻止
  const timeRowsEditor = dialog.getByTestId('wake-time-editor');
  await timeRowsEditor.getByLabel('开始时间').fill('');
  await timeRowsEditor.getByTestId('wake-override-row-error').getByText(/开始时间必须为 HH:MM/).waitFor();
  assert.equal(await timeRowsEditor.locator('[data-row-id]').count(), 1);
  assert.equal(await timeRowsEditor.getByLabel(/^发言频率偏好.*状态$/).inputValue(), 'off');
  writesBeforeRowError = writes.length;
  await dialog.getByRole('button', { name: '保存修改' }).click();
  await page.getByText(/开始时间必须为 HH:MM/).last().waitFor();
  assert.equal(writes.length, writesBeforeRowError);
  await timeRowsEditor.getByLabel('开始时间').fill('23:00');
  await page.waitForFunction(() => document.querySelectorAll('[data-testid="wake-override-row-error"]').length === 0);
  // 服务端修订冲突: 全部草稿(含未完成行)保留
  revision++;
  await dialog.getByRole('button', { name: '保存修改' }).click();
  await page.getByText('保存失败: 配置已被其他操作修改, 当前草稿已保留; 请复制草稿并刷新后合并', { exact: true }).waitFor();
  assert.equal(await rowsEditor.locator('[data-row-id]').count(), 2);
  assert.equal(await rowsEditor.getByLabel('群号').nth(1).inputValue(), '32');
  // 草稿仍脏: 关闭弹窗要走放弃确认
  const discardHandler = (nativeDialog) => { void nativeDialog.accept(); };
  page.on('dialog', discardHandler);
  await page.keyboard.press('Escape');
  await dialog.waitFor({ state: 'hidden' });
  page.off('dialog', discardHandler);
  await Promise.all([
    page.waitForResponse((response) => response.url().includes('/config/platforms')),
    page.getByRole('button', { name: '刷新', exact: true }).click(),
  ]);
  // 冲突期间放弃的只是本地草稿: 服务端已有值仍在, 重新打开应回到保存时的行
  await legacyRow.getByTitle('编辑', { exact: true }).click();
  assert.equal(await dialog.getByTestId('wake-group-editor').locator('[data-row-id]').count(), 1);
  assert.equal(await dialog.getByTestId('wake-group-editor').getByLabel('群号').inputValue(), '20');
  assert.equal(await dialog.getByTestId('wake-time-editor').getByLabel('开始时间').inputValue(), '23:00');
  await dialog.getByRole('button', { name: '保存修改' }).click();
  await dialog.waitFor({ state: 'hidden' });
  assert.deepEqual(writes.at(-1).settings.wake_group_overrides, { '20': { wake_mode: 'necessity', wake_cooldown: 30, wake_words: [] } });
  assert.deepEqual(writes.at(-1).settings.wake_time_rules, [{ start: '23:00', end: '07:00', settings: { wake_talk_value: 0 } }]);

  // ── 批次8 (B10): 平台页请求阶段诊断 (普通事件与手动请求共用) ──
  const diagnostics = page.getByTestId('request-diagnostics-panel');
  await diagnostics.waitFor();
  const manualRow = diagnostics.getByTestId('diagnostics-row').filter({ hasText: 'manual-1' });
  const autoRow = diagnostics.getByTestId('diagnostics-row').filter({ hasText: 'auto-1' });
  await manualRow.getByText('补全 · 部分送达', { exact: true }).waitFor();
  await manualRow.getByText('模型 · 正常', { exact: true }).waitFor();
  await manualRow.getByText('发送 · 状态未知', { exact: true }).waitFor();
  // unknown 必须显示"不确定, 不自动重发", 不能冒充已送达或确定失败
  await manualRow.getByText('不确定, 不自动重发', { exact: true }).waitFor();
  assert.equal(await manualRow.getByText('已送达', { exact: true }).count(), 0);
  await manualRow.getByText(/语音 不支持/).waitFor();
  // 只在决策点未处理的请求不显示成进行中
  await autoRow.getByText('唤醒决策 · 未处理', { exact: true }).waitFor();
  await autoRow.getByText('限流 · 未处理', { exact: true }).waitFor();
  assert.equal(await autoRow.getByText('进行中', { exact: true }).count(), 0);
  // 详情展开: 阶段原因中文可读, 已确认与未确认分段同时可见
  const callsBeforeDetail = diagnosticsCalls.length;
  await manualRow.getByRole('button', { name: '展开详情' }).click();
  const detail = manualRow.getByTestId('request-diagnostic-detail');
  await detail.getByText('业务段: 已确认 1 · 未确认 1 · 失败 0', { exact: true }).waitFor();
  await detail.getByText(/有已提交未确认的动作, 结果不确定/).waitFor();
  await detail.getByText('附件: 语音 不支持 (asr_reason); 文件 失败 (no_remote_url)', { exact: true }).waitFor();
  assert.ok(diagnosticsCalls.length > callsBeforeDetail);
  assert.equal(diagnosticsCalls.at(-1).detail, 'manual-1');
  await manualRow.getByRole('button', { name: '收起详情' }).click();
  // 平台筛选: 只保留该平台的记录; 空列表有明确说明
  await diagnostics.getByLabel('诊断平台筛选').selectOption('quiet-bot');
  await page.waitForFunction(() => document.querySelectorAll('[data-testid="diagnostics-row"]').length === 0);
  await diagnostics.getByTestId('diagnostics-empty').waitFor();
  await diagnostics.getByLabel('诊断平台筛选').selectOption('new-bot');
  await page.waitForFunction(() => document.querySelectorAll('[data-testid="diagnostics-row"]').length === 1);
  // 平台筛选必须真的作为查询参数到达服务端, 不能只在本地过滤
  assert.match(diagnosticsCalls.at(-1).url, /[?&]adapter_id=new-bot(&|$)/);
  // 仅看拒绝预设: 只留命中拒绝阶段的请求 (平台页默认关闭, 需显式勾选); 切换平台后不残留该过滤
  const rejectionsOnly = diagnostics.getByTestId('diagnostics-rejections-only');
  assert.equal(await rejectionsOnly.isChecked(), false);
  await rejectionsOnly.check();
  await page.waitForFunction(() => document.querySelectorAll('[data-testid="diagnostics-row"]').length === 0);
  assert.match(diagnosticsCalls.at(-1).url, /[?&]stage=wake_decision(%2C|,)rate_limit(&|$)/);
  await diagnostics.getByLabel('诊断平台筛选').selectOption('legacy-bot');
  await page.waitForFunction(() => document.querySelectorAll('[data-testid="diagnostics-row"]').length === 1);
  assert.equal(await rejectionsOnly.isChecked(), false);
  assert.doesNotMatch(diagnosticsCalls.at(-1).url, /stage=/);
  await diagnostics.getByLabel('诊断平台筛选').selectOption('new-bot');
  await page.waitForFunction(() => document.querySelectorAll('[data-testid="diagnostics-row"]').length === 1);
  // 查询失败: 给出实际可用的刷新入口
  diagnosticsFail = true;
  await diagnostics.getByTestId('diagnostics-refresh').click();
  await diagnostics.getByTestId('diagnostics-error').getByText(/诊断查询失败/).waitFor();
  diagnosticsFail = false;
  await diagnostics.getByRole('button', { name: '实际重试' }).click();
  await page.waitForFunction(() => document.querySelectorAll('[data-testid="diagnostics-row"]').length === 1);
  // 有界轮询: 全部为终态时立即停止, 有进行中请求时才继续, 达到上限由代码停止
  await diagnostics.getByLabel('诊断平台筛选').selectOption('legacy-bot');
  await page.waitForFunction(() => document.querySelectorAll('[data-testid="diagnostics-row"]').length === 1);
  await diagnostics.getByLabel('有界自动刷新').check();
  await diagnostics.getByTestId('diagnostics-poll-note').getByText('没有进行中的请求, 已停止自动刷新', { exact: true }).waitFor();
  assert.equal(await diagnostics.getByLabel('有界自动刷新').isChecked(), false);
  await diagnostics.getByLabel('诊断平台筛选').selectOption('');
  const callsBeforePolling = diagnosticsCalls.length;
  await diagnostics.getByLabel('有界自动刷新').check();
  await page.waitForTimeout(4500);
  assert.ok(diagnosticsCalls.length > callsBeforePolling);
  await diagnostics.getByLabel('有界自动刷新').uncheck();
  // 窄屏 + 键盘: 诊断控件仍可聚焦与操作
  await page.setViewportSize({ width: 390, height: 844 });
  await diagnostics.scrollIntoViewIfNeeded();
  await diagnostics.getByTestId('diagnostics-refresh').focus();
  assert.equal(await diagnostics.getByTestId('diagnostics-refresh').evaluate((element) => element === document.activeElement), true);
  const callsBeforeKeyboard = diagnosticsCalls.length;
  await page.keyboard.press('Enter');
  await page.waitForFunction((count) => window.__diagnosticsCalls === count, undefined, { timeout: 100 }).catch(() => null);
  assert.ok(diagnosticsCalls.length >= callsBeforeKeyboard);
  const panelBox = await diagnostics.boundingBox();
  assert.ok(panelBox.x >= 0 && panelBox.x + panelBox.width <= 390);
  await page.screenshot({ animations: 'disabled', path: path.join(artifacts, 'diagnostics-mobile.png') });
  await page.setViewportSize({ width: 1280, height: 900 });

  // ── 批次8 (B9): 数据路由下的统一离开拦截 ──
  await legacyRow.getByTitle('编辑', { exact: true }).click();
  const platformWakeWords = dialog.getByRole('textbox', { name: '唤醒词（留空不启用词语触发）' });
  await platformWakeWords.fill('后退草稿');
  // 新增未完成行同样属于脏草稿
  await dialog.getByTestId('wake-group-editor').getByRole('button', { name: '添加群覆盖' }).click();
  let backConfirmCount = 0;
  let backAction = 'dismiss';
  const backHandler = (nativeDialog) => { backConfirmCount += 1; void nativeDialog[backAction](); };
  page.on('dialog', backHandler);
  const historyBack = () => page.evaluate(() => window.history.back());
  const historyForward = () => page.evaluate(() => window.history.forward());
  await historyBack();
  await page.waitForTimeout(400);
  assert.equal(backConfirmCount, 1);
  assert.ok(page.url().endsWith('/platforms'));
  assert.equal(await dialog.isVisible(), true);
  assert.equal(await platformWakeWords.inputValue(), '后退草稿');
  assert.equal(await dialog.getByTestId('wake-group-editor').locator('[data-row-id]').count(), 2);
  // 取消离开后再退出, 仍然拦截; 确认放弃只导航一次
  await historyBack();
  await page.waitForTimeout(400);
  assert.equal(backConfirmCount, 2);
  assert.ok(page.url().endsWith('/platforms'));
  backAction = 'accept';
  await historyBack();
  await page.waitForURL('**/agents');
  assert.equal(backConfirmCount, 3);
  page.off('dialog', backHandler);
  // 非脏状态下的前进/后退不拦截
  backConfirmCount = 0;
  let unexpectedConfirm = 0;
  const quietHandler = (nativeDialog) => { unexpectedConfirm += 1; void nativeDialog.dismiss(); };
  page.on('dialog', quietHandler);
  await historyBack();
  await page.waitForURL('**/platforms');
  await historyForward();
  await page.waitForURL('**/agents');
  page.off('dialog', quietHandler);
  assert.equal(unexpectedConfirm, 0);
  // /chat 独立页直达与管理页直达仍按原路由装配渲染
  await page.goto(`${origin}/chat`);
  await page.getByTitle('返回管理面板').waitFor();
  assert.equal(await page.getByRole('link', { name: 'Agent 配置' }).count(), 0);
  await page.goto(`${origin}/platforms`);
  await page.getByRole('link', { name: 'Agent 配置' }).waitFor();
  await page.getByRole('heading', { name: '请求阶段诊断' }).waitFor();

  assert.deepEqual(errors, []);
  console.log('PASS: 旧配置/别名, 新配置默认隔离, 列表往返, 空白名单, 错误保留草稿, 键盘与窄屏, 行编辑器, 平台级输入预算与 talk_value 往返及静态提示, 契约边界 (排他上界/冷却非负) 与行内校验, 试算预览与来源明细, 脏保护, 草稿行保留与校验, 请求阶段诊断, 数据路由离开拦截');
} finally {
  await browser?.close();
  await server.close();
}

