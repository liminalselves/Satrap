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
  let platforms = [{ id: 'legacy-bot', type: 'aiocqhttp', settings: { extension: 'keep', access_token: 'test-secret-not-for-summary', group_whitelist: ['123'], wake_words: ['小助手'] } }];
  let revision = 1;
  const writes = [];
  let applications = [];
  let reloadCount = 0;
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
    if (pathname === '/api/config/reload') {
      assert.equal(request.postDataJSON().expected_config_revision, String(revision));
      const status = ['applied', 'pending_restart', 'failed'][Math.min(reloadCount++, 2)];
      applications = platforms.map((item) => ({ id: item.id, status, saved_revision: 'saved-version', active_revision: status === 'applied' ? 'saved-version' : 'old-version', error: status === 'failed' ? '模拟应用失败' : undefined }));
    }
    const responses = {
      '/api/health': { running: true, adapters: {}, platform_config: applications }, '/status': { running: true },
      '/api/config/reload': { ok: true, edictum_sessions: [], platforms: applications },
      '/config/session-classes': {}, '/config/edictum/sessions': {},
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
  await page.getByText('应用失败', { exact: true }).first().waitFor();
  await page.getByTitle('编辑', { exact: true }).first().click();
  await dialog.getByLabel('唤醒词', { exact: false }).fill('保留我的草稿');
  revision++;
  const writesBeforeConflict = writes.length;
  await dialog.getByRole('button', { name: '保存修改' }).click();
  await page.getByText('保存失败: 配置已被其他操作修改, 当前草稿已保留; 请复制草稿并刷新后合并', { exact: true }).waitFor();
  assert.equal(await dialog.getByLabel('唤醒词', { exact: false }).inputValue(), '保留我的草稿');
  assert.equal(writes.length, writesBeforeConflict);
  assert.deepEqual(errors, []);
  console.log('PASS: 旧配置/别名, 新配置默认隔离, 列表往返, 空白名单, 错误保留草稿, 键盘与窄屏');
} finally {
  await browser?.close();
  await server.close();
}

