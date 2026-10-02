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
  page.on('pageerror', (error) => errors.push(error.message));
  await page.route(origin + '/ui-config.json', (route) => route.fulfill({ json: { backend_api: 'http://127.0.0.1:19870', control_api: 'http://127.0.0.1:19871', chat_api: 'http://127.0.0.1:19872' } }));
  await page.route(/^http:\/\/127\.0\.0\.1:1987[012]\//, (route) => {
    if (route.request().method() === 'OPTIONS') return route.fulfill({ status: 204, headers: cors });
    const pathname = new URL(route.request().url()).pathname;
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
  assert.deepEqual(errors, []);
  console.log('PASS: 插件目录搜索、来源与平台筛选、详情能力、直接打开和不存在提示');
} finally {
  if (browser) await browser.close();
  await server.close();
}
