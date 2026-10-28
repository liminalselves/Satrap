import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { emptyConfigResponse, runSuite, mockConfigVersions } from './runtime.mjs';

async function user_docs(runtime) {
  /** 用户文档: 设置入口, 深链重定向, 锚点定位与不存在页面; 服务全部离线 */
  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  const server = await runtime.vite({ root, server: { host: '127.0.0.1', port: 0 } });
  await runtime.listen(server);
  const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
  const artifacts = process.env.SATRAP_E2E_ARTIFACTS;
  const context = await runtime.browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await context.route('**/*', (route) => {
    const url = new URL(route.request().url());
    if (url.origin === origin && url.pathname !== '/ui-config.json') return route.continue();
    if (url.pathname === '/ui-config.json') return route.fulfill({ json: { backend_api: 'http://127.0.0.1:19870', control_api: 'http://127.0.0.1:19871', chat_api: 'http://127.0.0.1:19872' } });
    const cors = { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true', 'Access-Control-Allow-Headers': '*', 'Access-Control-Allow-Methods': '*' };
    if (route.request().method() === 'OPTIONS') return route.fulfill({ status: 204, headers: cors });
    if (url.pathname === '/status' || url.pathname === '/api/health') return route.fulfill({ headers: cors, json: { running: false, managed: false, adapters: {} } });
    return route.fulfill({ headers: cors, json: emptyConfigResponse(route.request().url()) });
  });
  await page.routeWebSocket(/ws:\/\/127\.0\.0\.1:1987[012]\//, () => {});
  await mockConfigVersions(page, origin, () => '{}');
  try {
    await page.goto(`${origin}/settings`);
    await page.getByRole('button', { name: '使用文档', exact: true }).click();
    await page.waitForURL(`${origin}/docs`);
    await page.getByRole('heading', { level: 1, name: '使用指南' }).first().waitFor();
    assert.equal(await page.getByRole('navigation', { name: '面包屑' }).getByText('使用文档', { exact: true }).count(), 1);
    if (artifacts) await page.screenshot({ animations: 'disabled', path: path.join(artifacts, 'docs-index.png') });

    // 大纲: 点击二级标题跳转并高亮, 折叠后子项隐藏
    const outline = page.getByRole('navigation', { name: '文档大纲' });
    await outline.getByRole('link', { name: '审批', exact: true }).click();
    await page.waitForURL(/\/docs\/index#/);
    await page.waitForFunction(() => document.querySelector('nav[aria-label="文档大纲"] a[aria-current="location"]')?.textContent === '审批');
    if (artifacts) await page.screenshot({ animations: 'disabled', path: path.join(artifacts, 'docs-outline.png') });
    await outline.getByRole('button', { name: '使用指南' }).click();
    assert.equal(await outline.getByRole('link', { name: '审批', exact: true }).count(), 0);
    await outline.getByRole('button', { name: '使用指南' }).click();
    assert.equal(await outline.getByRole('link', { name: '审批', exact: true }).count(), 1);

    // 设置页深链转到文档页, 并滚动到锚点所在标题
    await page.goto(`${origin}/settings?doc=index#${encodeURIComponent('插件')}`);
    await page.waitForURL(/\/docs\/index#/);
    const heading = page.locator('[id="插件"]');
    await heading.waitFor();
    await page.waitForFunction(() => {
      const target = document.getElementById('插件');
      const main = document.querySelector('main');
      return target && main && main.scrollTop > 0 && Math.abs(target.getBoundingClientRect().top - main.getBoundingClientRect().top) < 140;
    });
    if (artifacts) await page.screenshot({ animations: 'disabled', path: path.join(artifacts, 'docs-anchor.png') });

    // 文档内的锚点链接只改 hash, 不离开文档页
    await page.locator('a[href="#插件"], a[href="#%E6%8F%92%E4%BB%B6"]').first().click();
    assert.match(page.url(), /\/docs\/index#/);

    await page.goto(`${origin}/docs/not-a-page`);
    await page.getByText('文档不存在', { exact: true }).waitFor();
    await page.getByRole('link', { name: '返回文档首页' }).click();
    await page.getByRole('heading', { level: 1, name: '使用指南' }).first().waitFor();

    // 返回设置: 面包屑上方的返回链接
    await page.getByRole('link', { name: '系统设置' }).first().click();
    await page.waitForURL(`${origin}/settings`);
    assert.deepEqual(errors, []);
    console.log('PASS: 设置入口打开文档, 深链重定向并定位锚点, 不存在页面可返回首页');
  } finally {
    await context.close();
  }
}

export const scenarios = [
  ['user-docs', user_docs],
];
if (process.argv[1] && pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url) await runSuite(scenarios);
