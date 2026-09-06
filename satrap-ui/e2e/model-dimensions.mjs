/** 验证清空维度后的 PATCH 语义和重新打开表单的显示 */
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
  const origin = 'http://127.0.0.1:' + server.httpServer.address().port;
  const base = 'http://127.0.0.1:19871';
  const config = { name: 'default', model: 'embedding-model', api_key: '****test', dimensions: 1024 };
  const patches = [];
  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  await page.route(origin + '/ui-config.json', (route) => route.fulfill({ json: { backend_api: base, control_api: base, chat_api: base } }));
  await page.route(base + '/**', (route) => {
    const request = route.request(), url = new URL(request.url());
    let json = { ok: true };
    if (url.pathname === '/config/models') json = url.searchParams.get('type') === 'embedding' ? { default: config } : {};
    if (request.method() === 'PATCH') {
      const patch = request.postDataJSON();
      patches.push(patch);
      Object.assign(config, patch);
    }
    return route.fulfill({ json, headers: { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true' } });
  });
  await page.routeWebSocket(base.replace('http', 'ws') + '/**', () => {});
  await page.goto(origin + '/models');
  await page.getByRole('button', { name: 'Embedding 配置', exact: true }).click();
  await page.getByRole('button', { name: '编辑', exact: true }).click();
  const dimensions = page.getByPlaceholder('留空使用模型默认维度，不发送 dimensions 参数');
  assert.equal(await dimensions.inputValue(), '1024');
  await dimensions.fill('');
  await page.getByRole('button', { name: '保存修改', exact: true }).click();
  await page.getByRole('dialog').waitFor({ state: 'hidden' });
  assert.equal(patches[0].dimensions, null);
  await page.getByRole('button', { name: '编辑', exact: true }).click();
  assert.equal(await dimensions.inputValue(), '');
  await dimensions.fill('768');
  await page.getByRole('button', { name: '保存修改', exact: true }).click();
  await page.getByRole('dialog').waitFor({ state: 'hidden' });
  assert.equal(patches[1].dimensions, 768);
  console.log('PASS: 清空维度提交 null, 重新打开保持空白, 数值填写仍正常');
} finally {
  if (browser) await browser.close();
  await server.close();
}
