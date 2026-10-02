/** 共用表单必填校验的页面回归: 空值不发写请求且弹窗保留, 填好后才提交 */
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';
import { createServer } from 'vite';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const server = await createServer({ root, server: { host: '127.0.0.1', port: 0 } });
let browser;
try {
  await server.listen();
  const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
  const writes = [];
  let user = { platform_id: 'test-platform', platform_label: '测试平台', platform_type: 'test', user_id: 'user-1', user_nickname: '甲', user_platform: 'test', user_session: [], has_profile: true, revision: 'v1', conversations: [], conversation_count: 0 };
  const cors = {
    'Access-Control-Allow-Origin': origin,
    'Access-Control-Allow-Credentials': 'true',
    'Access-Control-Allow-Headers': 'Content-Type',
  };
  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  await page.route(origin + '/ui-config.json', (route) => route.fulfill({ json: {
    backend_api: 'http://127.0.0.1:19870', control_api: 'http://127.0.0.1:19871', chat_api: 'http://127.0.0.1:19872',
  } }));
  await page.route(/^http:\/\/127\.0\.0\.1:1987[012]\//, (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (request.method() === 'POST') writes.push({ path: url.pathname, body: request.postDataJSON() });
    if (url.pathname === '/config/conversations/users') {
      if (request.method() === 'POST') {
        const data = request.postDataJSON();
        if (data.action === 'create') user = { ...user, user_id: data.user_id, revision: 'v2' };
        if (data.action === 'associate') user = { ...user, user_session: [data.session_id], revision: 'v3' };
        return route.fulfill({ json: { ok: true, user }, headers: cors });
      }
      return route.fulfill({ json: url.searchParams.has('user_id') ? { user } : { items: [user], total: 1, new_revision: 'missing' }, headers: cors });
    }
    const responses = {
      '/api/health': { running: true, adapters: {} },
      '/config/models': {},
      '/config/conversations/platforms': { platforms: ['test-platform'], items: [{ id: 'test-platform', type: 'test', type_label: '测试', label: '测试平台' }] },
      '/api/checkpoints': { conversation_id: 'conv-test', checkpoints: [{ checkpoint_id: 'ckpt-1', created_at: 1 }], branches: [] },
      '/api/checkpoint/audit': { mutations: [] },
      '/api/checkpoint/fork': { ok: true, conversation_id: 'conv-test:fork:retry_v2' },
    };
    return route.fulfill({ json: responses[url.pathname] ?? { ok: true }, headers: cors });
  });
  // 页面还会发一次会话引导请求与状态 WS, 都不属于被验证的写入, 写请求断言按路径前缀过滤
  await page.routeWebSocket(/ws:\/\/127\.0\.0\.1:1987[012]\//, () => {});
  const writesTo = (prefix) => writes.filter((item) => item.path.startsWith(prefix));
  const dialog = page.getByRole('dialog');

  // 1. 模型配置: 配置名称必填, 空名称不发创建请求
  await page.goto(origin + '/models');
  await page.getByRole('button', { name: '新增配置', exact: true }).click();
  const modelName = dialog.getByLabel(/配置名称/);
  assert.equal(await modelName.inputValue(), 'default');
  await modelName.fill('');
  assert.equal(await modelName.evaluate((element) => element.validity.valueMissing), true);
  await dialog.getByRole('button', { name: '创建', exact: true }).click();
  await page.waitForTimeout(200);
  assert.deepEqual(writesTo('/config/models'), []);
  assert.equal(await dialog.isVisible(), true);
  await modelName.fill('probe-model');
  await dialog.getByRole('button', { name: '创建', exact: true }).click();
  await dialog.waitFor({ state: 'hidden' });
  assert.deepEqual(writesTo('/config/models').map((item) => item.path), ['/config/models/llm/probe-model']);

  // 2. 用户资料: 空 ID 不发创建请求
  await page.goto(origin + '/users');
  await page.getByRole('button', { name: '添加用户资料', exact: true }).click();
  const userId = dialog.getByLabel('资料用户 ID');
  assert.equal(await userId.inputValue(), '');
  const saveUser = dialog.getByRole('button', { name: '保存资料', exact: true });
  assert.equal(await saveUser.isDisabled(), true);
  await saveUser.evaluate((element) => element.click());
  await page.waitForTimeout(200);
  assert.deepEqual(writesTo('/config/conversations/users'), []);
  assert.equal(await dialog.isVisible(), true);
  await userId.fill('probe');
  await saveUser.click();
  await dialog.waitFor({ state: 'hidden' });
  assert.deepEqual(writesTo('/config/conversations/users').map((item) => item.body.user_id), ['probe']);

  // 3. 列表关联: 空对话 ID 不发请求
  await page.getByRole('button', { name: '编辑用户资料', exact: true }).waitFor();
  await page.getByText('高级资料操作', { exact: true }).click();
  const sessionId = page.getByLabel('关联对话 ID');
  assert.equal(await sessionId.inputValue(), '');
  const associate = page.getByRole('button', { name: '添加关联', exact: true });
  assert.equal(await associate.isDisabled(), true);
  await associate.evaluate((element) => element.click());
  await page.waitForTimeout(200);
  assert.deepEqual(writesTo('/config/conversations/users').filter((item) => item.body.action === 'associate'), []);
  await sessionId.fill('sr7dws');
  await associate.click();
  await page.waitForFunction(() => document.body.textContent.includes('此用户暂无关联对话') && document.body.textContent.includes('编辑用户资料'));
  assert.deepEqual(writesTo('/config/conversations/users').filter((item) => item.body.action === 'associate').map((item) => item.body.session_id), ['sr7dws']);

  // 4. 检查点分支: 分支名必填, 空分支名不发 Fork 请求
  await page.goto(origin + '/checkpoints');
  await page.getByPlaceholder('输入对话 ID (如 conv-xxx)').fill('conv-test');
  await page.getByRole('button', { name: '查询', exact: true }).click();
  await page.getByRole('button', { name: 'Fork 分支', exact: true }).click();
  const branchName = dialog.getByLabel(/分支名/);
  assert.equal(await branchName.inputValue(), '');
  await dialog.getByRole('button', { name: 'Fork', exact: true }).click();
  await page.waitForTimeout(200);
  assert.deepEqual(writesTo('/api/checkpoint/fork'), []);
  assert.equal(await dialog.isVisible(), true);
  await branchName.fill('retry_v2');
  await dialog.getByRole('button', { name: 'Fork', exact: true }).click();
  await dialog.waitFor({ state: 'hidden' });
  assert.deepEqual(writesTo('/api/checkpoint/fork').map((item) => item.body.branch_name), ['retry_v2']);

  console.log('PASS: 模型配置名称/用户 ID/关联对话 ID/分支名为空时不发写请求, 填写后正常提交');
} finally {
  if (browser) await browser.close();
  await server.close();
}
