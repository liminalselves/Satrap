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
  const plugins = [
    { name: 'builtin_probe', version: '1', author: '作者', description: '内置测试', source: 'builtin', usage_count: 2, edictum_configs: ['assistant'], chat_enabled: true, config_schema: {}, capabilities: { tools: { probe_tool: '测试工具' } } },
    { name: 'group_probe', version: '2', author: '群聊作者', description: '群聊测试', source: 'user', usage_count: 0, edictum_configs: [], chat_enabled: false, config_schema: {}, applicability: { session_types: ['platform'], platforms: ['onebot'] }, capabilities: { skills: { group_skill: '群聊技能' }, handlers: { pre_process: '前处理描述' } } },
  ];
  const cors = { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true', 'Access-Control-Allow-Headers': 'Content-Type', 'Access-Control-Allow-Methods': 'GET,POST,PATCH,PUT,OPTIONS' };
  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  const errors = [];
  let installs = 0;
  let discards = 0;
  page.on('pageerror', (error) => errors.push(error.message));
  await page.route(origin + '/ui-config.json', (route) => route.fulfill({ json: { backend_api: 'http://127.0.0.1:19870', control_api: 'http://127.0.0.1:19871', chat_api: 'http://127.0.0.1:19872' } }));
  await page.route(/^http:\/\/127\.0\.0\.1:1987[012]\//, (route) => {
    if (route.request().method() === 'OPTIONS') return route.fulfill({ status: 204, headers: cors });
    const pathname = new URL(route.request().url()).pathname;
    if (pathname === '/config/plugins/preview') {
      assert.equal(route.request().headers()['content-type'], 'application/zip');
      assert.equal(route.request().postData(), 'zip-preview-test');
      return route.fulfill({ json: { token: 'test-token', plugin: { ...plugins[1], name: 'installed_probe' }, expires_in: 600, file_count: 2, expanded_bytes: 100 }, headers: cors });
    }
    if (pathname === '/config/plugins/discard') { discards++; return route.fulfill({ json: { ok: true }, headers: cors }); }
    if (pathname === '/config/plugins/install') {
      assert.equal(route.request().postDataJSON().token, 'test-token');
      installs++;
      const plugin = { ...plugins[1], name: 'installed_probe' };
      plugins.push(plugin);
      return route.fulfill({ json: { ok: true, plugin }, headers: cors });
    }
    return route.fulfill({ json: pathname === '/config/plugins' ? { plugins } : { ok: true, running: false, adapters: {} }, headers: cors });
  });
  await page.routeWebSocket(/ws:\/\/127\.0\.0\.1:1987[012]\//, () => {});
  await page.goto(origin + '/plugins');
  await page.getByRole('link', { name: /builtin_probe/ }).waitFor();
  assert.equal(await page.getByRole('link', { name: /group_probe/ }).count(), 1);
  await page.getByLabel('插件来源').selectOption('user');
  assert.equal(await page.getByRole('link', { name: /builtin_probe/ }).count(), 0);
  await page.getByLabel('搜索插件').fill('群聊作者');
  await page.getByLabel('适用平台').selectOption('onebot');
  await page.getByRole('link', { name: /group_probe/ }).click();
  await page.getByRole('heading', { name: 'group_probe', exact: true }).waitFor();
  await page.getByText('group_skill', { exact: true }).waitFor();
  await page.getByText('前处理描述', { exact: true }).waitFor();
  await page.reload();
  await page.getByText('group_skill', { exact: true }).waitFor();
  await page.goto(origin + '/plugins/missing');
  await page.getByRole('alert').filter({ hasText: '插件不存在' }).waitFor();
  await page.goto(origin + '/plugins');
  await page.getByRole('button', { name: '安装插件', exact: true }).click();
  const dialog = page.getByRole('dialog');
  const file = { name: 'plugin.zip', mimeType: 'application/zip', buffer: Buffer.from('zip-preview-test') };
  await dialog.getByLabel('插件压缩包').setInputFiles(file);
  await dialog.getByRole('heading', { name: /installed_probe/ }).waitFor();
  assert.equal(installs, 0);
  const discarded = page.waitForResponse((response) => new URL(response.url()).pathname === '/config/plugins/discard');
  await dialog.getByRole('button', { name: '取消', exact: true }).click();
  await dialog.waitFor({ state: 'hidden' });
  await discarded;
  assert.equal(discards, 1);
  await page.getByRole('button', { name: '安装插件', exact: true }).click();
  await dialog.getByLabel('插件压缩包').setInputFiles(file);
  await dialog.getByRole('heading', { name: /installed_probe/ }).waitFor();
  await dialog.getByRole('button', { name: '安装', exact: true }).click();
  await page.getByRole('heading', { name: 'installed_probe', exact: true }).waitFor();
  assert.equal(installs, 1);
  assert.equal(plugins.at(-1).chat_enabled, false);
  await page.getByRole('link', { name: '← 返回插件列表' }).click();
  await page.getByRole('link', { name: /installed_probe/ }).waitFor();
  assert.deepEqual(errors, []);
  console.log('PASS: 插件目录搜索、来源与平台筛选、详情能力、直接打开和不存在提示');
  console.log('PASS: ZIP 上传预览、取消清理、显式安装、打开详情及刷新列表');
} finally {
  if (browser) await browser.close();
  await server.close();
}
