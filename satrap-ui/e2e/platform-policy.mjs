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
        explicit,
        automatic: {
          mode: 'frequency', observed: 2, steps: [],
          decision: { triggered: true, rule: 'frequency_threshold', reason: '累计有效正文达到阈值', score: null },
          deadline_decision: { triggered: false, rule: 'deadline_wait', reason: '未到最后期限', score: null },
          cooldown_remaining: 0,
        },
      } });
    }
    if (pathname === '/api/config/reload') {
      assert.equal(request.postDataJSON().expected_config_revision, String(revision));
      const status = ['applied', 'pending_restart', 'failed'][Math.min(reloadCount++, 2)];
      applications = platforms.map((item) => ({ id: item.id, status, saved_revision: 'saved-version', active_revision: status === 'applied' ? 'saved-version' : 'old-version', error: status === 'failed' ? '模拟应用失败' : undefined }));
    }
    const responses = {
      '/api/health': { running: true, adapters: {}, platform_config: applications }, '/status': { running: true },
      '/api/config/reload': { ok: true, edictum_sessions: [], platforms: applications },
      '/config/session-classes': {}, '/config/edictum/sessions': {},
      '/api/sessions': { sessions: [] }, '/config/session-instances': { sessions: [] },
    };
    return route.fulfill({ json: pathname.startsWith('/config/platforms') ? { ok: true, platforms, revision: String(revision) } : responses[pathname] ?? { ok: true }, headers });
  });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.routeWebSocket('ws://127.0.0.1:19870/ws/status**', () => {});
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
  await groupEditor.getByLabel('冷却秒数状态').selectOption('value');
  await groupEditor.getByLabel('冷却秒数值').fill('30');
  await groupEditor.getByLabel('唤醒词状态').selectOption('off');
  await groupEditor.locator('span').filter({ hasText: '清空继承词表' }).waitFor();
  const timeEditor = dialog.getByTestId('wake-time-editor');
  await timeEditor.getByRole('button', { name: '添加时段规则' }).click();
  await timeEditor.getByLabel('开始时间').fill('23:00');
  await timeEditor.getByLabel('结束时间').fill('07:00');
  await timeEditor.getByLabel(/^发言频率偏好.*状态$/).selectOption('off');
  await dialog.getByRole('button', { name: '保存修改' }).click();
  await dialog.waitFor({ state: 'hidden' });
  assert.deepEqual(writes.at(-1).settings.wake_group_overrides, { '20': { wake_mode: 'necessity', wake_cooldown: 30, wake_words: [] } });
  assert.deepEqual(writes.at(-1).settings.wake_time_rules, [{ start: '23:00', end: '07:00', settings: { wake_talk_value: 0 } }]);

  // ── 批次4: 唤醒规则试算预览 ──
  await page.getByTitle('编辑', { exact: true }).first().click();
  const dryRunPanel = dialog.getByTestId('wake-dry-run-panel');
  await dryRunPanel.getByLabel('试算群号').fill('20');
  await dryRunPanel.getByLabel('样例消息').fill('hello\nworld');
  await dryRunPanel.getByRole('button', { name: '运行试算' }).click();
  await dryRunPanel.getByText('无法判断', { exact: true }).waitFor();
  await dryRunPanel.getByText('触发', { exact: true }).waitFor();
  await dryRunPanel.getByText('wake_mode=frequency', { exact: true }).waitFor();
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
  const sessionsLink = page.getByRole('link', { name: '会话管理' });
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
  await page.waitForURL('**/sessions');
  page.off('dialog', navHandler);

  assert.deepEqual(errors, []);
  console.log('PASS: 旧配置/别名, 新配置默认隔离, 列表往返, 空白名单, 错误保留草稿, 键盘与窄屏, 行编辑器, 试算预览, 脏保护');
} finally {
  await browser?.close();
  await server.close();
}

