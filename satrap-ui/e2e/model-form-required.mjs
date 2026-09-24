/** 验证共用表单的必填校验: 模型配置名称为空时不发创建请求, 填好后才提交 */
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
  const base = 'http://127.0.0.1:19871';
  const writes = [];
  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  await page.route(origin + '/ui-config.json', (route) => route.fulfill({ json: { backend_api: base, control_api: base, chat_api: base } }));
  await page.route(base + '/**', (route) => {
    const request = route.request(), url = new URL(request.url());
    // 只看模型配置写入: 页面本身还会向 /auth/session 发一次会话引导请求
    if (request.method() === 'POST' && url.pathname.startsWith('/config/models')) {
      writes.push({ method: 'POST', path: url.pathname, body: request.postDataJSON() });
    }
    let json = { ok: true };
    if (url.pathname === '/config/models') json = {};
    return route.fulfill({ json, headers: { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true' } });
  });
  await page.routeWebSocket(base.replace('http', 'ws') + '/**', () => {});
  await page.goto(origin + '/models');
  await page.getByRole('button', { name: '新增配置', exact: true }).click();
  const dialog = page.getByRole('dialog');
  const name = page.getByLabel(/配置名称/);
  assert.equal(await name.inputValue(), 'default');
  await name.fill('');
  // 原生必填约束必须仍然生效, 空名称提交被浏览器拦下
  assert.equal(await name.evaluate((element) => element.validity.valueMissing), true);
  await page.getByRole('button', { name: '创建', exact: true }).click();
  await page.waitForTimeout(200);
  assert.deepEqual(writes, []);
  assert.equal(await dialog.isVisible(), true);
  // 填好名称后才发出创建请求, 名称为空的路径不被放过
  await name.fill('probe-model');
  await page.getByRole('button', { name: '创建', exact: true }).click();
  await dialog.waitFor({ state: 'hidden' });
  assert.equal(writes.length, 1);
  assert.equal(writes[0].path, '/config/models/llm/probe-model');
  console.log('PASS: 空名称不发创建请求且弹窗保持打开, 填写名称后正常提交');
} finally {
  if (browser) await browser.close();
  await server.close();
}
