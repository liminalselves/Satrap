import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';
import { createServer, preview } from 'vite';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

export { emptyConfigResponse } from './config-fixtures.mjs';

export async function assertNoConfigErrors(page) {
  const errors = await page.locator('.glass-toast-error').allTextContents();
  assert.deepEqual(errors.filter((message) => /配置快照|配置来源|配置版本/.test(message)), [], '配置夹具必须符合生产快照契约');
}

/** 在侧栏打开会话; 进入 Chat 默认停在新对话欢迎页, 不再有"继续最近对话"入口 */
export async function openConversation(page, title, options = {}) {
  const row = page.locator('div.text-sm.truncate').filter({ hasText: new RegExp(`^${title.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}$`) }).first();
  await row.waitFor(options);
  await row.click(options);
}

export async function waitForRequestEntry(promise) {
  let timer;
  try {
    return await Promise.race([promise, new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error('预期的应用请求未在 30 秒内进入')), 30000);
    })]);
  } finally { clearTimeout(timer); }
}

// 旧夹具补齐真实管理读取要求的来源版本, 写入后的版本由场景数据计算
export async function mockConfigVersions(page, origin, version) {
  await page.route(/\/config\/snapshot(?:\?|$)/, async (route) => {
    const url = new URL(route.request().url());
    const resource = url.searchParams.get('resource');
    const paths = { models: '/config/models?type=' + (url.searchParams.get('type') || 'llm'), session_classes: '/config/session-classes',
      edictum_configs: '/config/edictum/sessions', plugin_catalog: '/config/plugins' };
    assert.ok(paths[resource], `未声明的快照资源: ${resource}`);
    const token = version();
    const response = await page.evaluate(async (target) => {
      const response = await fetch(target, { credentials: 'include' });
      return { status: response.status, body: await response.json() };
    }, url.origin + paths[resource]);
    assert.equal(response.status, 200, `快照正文夹具请求失败: ${resource}`);
    const data = resource === 'plugin_catalog' ? response.body.plugins : response.body;
    assert.ok(data && typeof data === 'object' && (resource === 'plugin_catalog' ? Array.isArray(data)
      : !Array.isArray(data) && Object.values(data).every((entry) => entry && typeof entry === 'object' && !Array.isArray(entry))), `无效快照正文夹具: ${resource}`);
    const resources = resource === 'plugin_catalog' ? ['plugin_catalog', 'edictum_configs', 'chat_plugins']
      : resource === 'edictum_configs' ? ['edictum_configs', 'plugin_catalog'] : [resource];
    return route.fulfill({ status: response.status, headers: { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true' },
      json: { data,
        versions: Object.fromEntries(resources.map((name) => [name, { state: 'available', change_token: token }])) } });
  });

  await page.route(/\/config\/versions(?:\?|$)/, (route) => {
    const url = new URL(route.request().url());
    const headers = { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true',
      'Access-Control-Allow-Headers': '*', 'Access-Control-Allow-Methods': '*' };
    if (route.request().method() === 'OPTIONS') return route.fulfill({ status: 204, headers });
    const token = version();
    return route.fulfill({ headers, json: { ok: true, resources: Object.fromEntries(
      (url.searchParams.get('resources') || '').split(',').filter(Boolean).map((name) => [name, { state: 'available', change_token: token }]),
    ) } });
  });
}

/** 自绘下拉 (role=combobox) 的选项; 打开列表读取后关闭. 控件不存在时返回空数组 */
export async function comboOptions(combo) {
  // 控件不存在 (例如没有可选项时不渲染) 视为没有选项
  if (!(await combo.count())) return [];
  await combo.click();
  const page = combo.page();
  const id = await combo.getAttribute('aria-controls');
  const list = page.locator(`[id="${id}"]`);
  await list.waitFor();
  const options = await list.locator('[role="option"]').evaluateAll((items) => items.map((item) => ({
    value: item.getAttribute('data-value'), label: item.textContent.trim(), disabled: item.getAttribute('aria-disabled') === 'true',
  })));
  await page.keyboard.press('Escape');
  await list.waitFor({ state: 'detached' });
  return options;
}

let comboPatched = false;
/** 让 selectOption / inputValue 同样适用于自绘下拉, 场景无需区分原生与自绘控件 */
async function patchComboLocator(browser) {
  if (comboPatched) return;
  const context = await browser.newContext();
  const page = await context.newPage();
  const proto = Object.getPrototypeOf(page.locator('body'));
  await context.close();
  const { selectOption, inputValue } = proto;
  const isCombo = (locator) => locator.evaluate((el) => el.getAttribute('role') === 'combobox' && el.tagName !== 'SELECT');
  proto.selectOption = async function patchedSelectOption(values, options) {
    if (!(await isCombo(this))) return selectOption.call(this, values, options);
    const wanted = Array.isArray(values) ? values[0] : values;
    const target = typeof wanted === 'object' && wanted !== null ? wanted : { value: wanted };
    await this.click(options);
    const id = await this.getAttribute('aria-controls');
    const items = this.page().locator(`[id="${id}"] [role="option"]`);
    const item = target.value !== undefined
      ? items.and(this.page().locator(`[data-value="${String(target.value).replace(/"/g, '\\"')}"]`))
      : items.filter({ hasText: target.label });
    await item.click(options);
    return [target.value ?? ''];
  };
  proto.inputValue = async function patchedInputValue(options) {
    if (!(await isCombo(this))) return inputValue.call(this, options);
    return (await this.getAttribute('data-value', options)) ?? '';
  };
  comboPatched = true;
}

// 业务场景共用服务与浏览器, 每个场景仍有独立的页面, 上下文和请求夹具
export async function runSuite(scenarios) {
  const selected = process.argv.includes('--case') ? process.argv[process.argv.indexOf('--case') + 1] : null;
  const cases = selected ? scenarios.filter(([name]) => name === selected) : scenarios;
  assert.ok(cases.length, `没有匹配的浏览器场景: ${selected}`);
  const browser = await chromium.launch({ headless: true });
  await patchComboLocator(browser);
  const servers = new Map();
  const isolated = new Set();
  const metrics = [];
  const close = async (server) => {
    if (typeof server.close === 'function') await server.close();
    else if (server.httpServer.listening) await new Promise((resolve, reject) => server.httpServer.close((error) => error ? reject(error) : resolve()));
  };
  const runtime = {
    browser,
    diagnostics: [],
    async vite(options, identity) {
      const key = identity || 'development';
      if (!servers.has(key)) servers.set(key, await createServer(options));
      const server = servers.get(key);
      if (identity) isolated.add(server);
      return server;
    },
    async preview(options) {
      if (!servers.has('production')) servers.set('production', await preview(options));
      return servers.get('production');
    },
    async listen(server) {
      if (!server.httpServer.listening) await server.listen();
    },
    async releaseServer(server) {
      if (!isolated.has(server)) return;
      await close(server);
      isolated.delete(server);
      for (const [key, value] of servers) if (value === server) servers.delete(key);
    },
    async closeContexts() {
      try {
        const pages = browser.contexts().flatMap((context) => context.pages());
        const results = await Promise.allSettled(pages.map(async (page) => ({
          url: page.url(),
          alerts: await page.getByRole('alert').allTextContents(),
          dialogs: await page.getByRole('dialog').allTextContents(),
        })));
        if (pages.length) runtime.diagnostics = results.map((result, index) => result.status === 'fulfilled' ? result.value
          : { url: pages[index].url(), diagnostic_error: String(result.reason) });
        // 页面导航可能使诊断读取失效, 保留诊断错误但不改变业务场景结果
      } finally {
        await Promise.all(browser.contexts().map((context) => context.close()));
      }
    },
  };
  const started = performance.now();
  try {
    for (const [name, scenario] of cases) {
      const before = performance.now();
      runtime.diagnostics = [];
      let error;
      try { await scenario(runtime); }
      catch (caught) {
        error = caught;
        console.error(`FAIL ${name}: ${caught.stack || caught}`);
      } finally {
        await runtime.closeContexts();
      }
      metrics.push({ name, seconds: (performance.now() - before) / 1000, ok: !error,
        ...(error ? { error: error.message, diagnostics: runtime.diagnostics } : {}) });
      if (process.env.SATRAP_BROWSER_REPORT) {
        await fs.writeFile(process.env.SATRAP_BROWSER_REPORT, JSON.stringify({ seconds: (performance.now() - started) / 1000, scenarios: metrics }, null, 2), 'utf8');
      }
      console.log(`${error ? 'FAIL' : 'PASS'} 场景 ${name}`);
    }
  } finally {
    await runtime.closeContexts();
    await browser.close();
    await Promise.all([...servers.values()].map(close));
  }
  const report = { seconds: (performance.now() - started) / 1000, scenarios: metrics };
  if (process.env.SATRAP_BROWSER_REPORT) {
    await fs.writeFile(process.env.SATRAP_BROWSER_REPORT, JSON.stringify(report, null, 2), 'utf8');
  }
  assert.ok(metrics.every((item) => item.ok), `${metrics.filter((item) => !item.ok).length} 个业务场景失败`);
  return report;
}
