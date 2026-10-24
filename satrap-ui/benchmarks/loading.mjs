import { build } from 'vite';
import { chromium } from 'playwright';
import { createServer } from 'node:http';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { execFileSync } from 'node:child_process';
import path from 'node:path';
import os from 'node:os';
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { createBenchmarkFixture, responseFor, jsonResponse, installWebSocketMocks } from './fixtures.mjs';
import { measureScenario, summarizeRuns } from './metrics.mjs';

const directory = path.dirname(fileURLToPath(import.meta.url));
const project = path.resolve(directory, '..');
const results = process.env.SATRAP_BENCHMARK_OUTPUT ? path.resolve(process.env.SATRAP_BENCHMARK_OUTPUT) : path.join(directory, 'results/frontend-loading-2026-10-23');
const mode = process.argv.includes('--baseline') ? 'baseline' : 'after';
const baselineRevision = process.env.SATRAP_BENCHMARK_BASE || execFileSync('git', ['rev-parse', 'HEAD'], { cwd: project, encoding: 'utf8' }).trim();
const interaction = process.argv.includes('--interaction');
const catalog = process.argv.includes('--catalog');
const prefix = interaction ? 'interaction-chromium-' : catalog ? 'catalog-' : '';
const iterations = Number(process.env.BENCHMARK_ITERATIONS || 10);
const warmups = Number(process.env.BENCHMARK_WARMUPS || 2);
const scenarios = catalog ? ['/models', '/plugins'].flatMap((route) => [false, true].map((hot) => ({ route, delay: 150, turns: 50, hot }))) : interaction ? [{ route: '/', delay: 0, turns: 50 }, ...[100, 1000].map((turns) => ({ route: '/chat', delay: 0, turns, history: true })), { route: '/chat', delay: 0, turns: 10, history: true, streaming: true }]
  : [...[0, 150, 300].flatMap((delay) => ['/', '/chat'].map((route) => ({ route, delay, turns: 50 }))), ...[100, 1000, 5000].map((turns) => ({ route: '/chat', delay: 0, turns, history: true }))];
const target = path.join(results, `${prefix}${mode}.json`);
if (mode === 'baseline') {
  try { await readFile(target); throw new Error('baseline 已存在, 不允许自动覆盖'); }
  catch (error) { if (error.code !== 'ENOENT') throw error; }
}
const baselineSources = new Map();
const built = await build({ root: project, logLevel: 'silent', plugins: [{ name: 'fixed-baseline-source', enforce: 'pre', load(id) {
  const relative = path.relative(path.resolve(project, '..'), id.split('?')[0]).replaceAll('\\', '/');
  if (mode !== 'baseline' || !relative.startsWith('satrap-ui/src/')) return null;
  if (!baselineSources.has(relative)) baselineSources.set(relative, execFileSync('git', ['show', `${baselineRevision}:${relative}`], { cwd: project, encoding: 'utf8', maxBuffer: 5 * 1024 * 1024 }));
  return baselineSources.get(relative);
} }], build: { write: false, reportCompressedSize: false } });
const files = new Map(built.output.map((entry) => [entry.fileName, Buffer.from(entry.type === 'chunk' ? entry.code : entry.source)]));
const server = createServer((request, response) => {
  const pathname = new URL(request.url, 'http://localhost').pathname;
  const filename = pathname === '/' || !pathname.split('/').at(-1).includes('.') ? 'index.html' : decodeURIComponent(pathname.slice(1));
  const body = files.get(filename);
  if (!body) { response.writeHead(404); response.end(); return; }
  response.setHeader('Content-Type', filename.endsWith('.js') ? 'application/javascript' : filename.endsWith('.css') ? 'text/css' : filename.endsWith('.html') ? 'text/html' : 'application/octet-stream');
  response.end(body);
});
await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
const origin = `http://127.0.0.1:${server.address().port}`;
let browser;
const samples = [];
try {
  browser = await chromium.launch({ headless: true, ...(interaction ? { channel: 'chromium', args: ['--disable-background-timer-throttling', '--disable-renderer-backgrounding', '--disable-backgrounding-occluded-windows', '--enable-precise-memory-info'] } : {}) });
  for (const scenario of scenarios) {
    for (let run = -warmups; run < iterations; run++) {
      const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
      try {
        const page = await context.newPage();
        const fixture = createBenchmarkFixture(scenario.turns * 2);
        const plugins = Array.from({ length: 30 }, (_, index) => ({ name: `bench-plugin-${index}`, version: '1', source: 'builtin', description: '性能测试插件', author: 'benchmark', config_schema: {}, capabilities: {}, applicability: {}, usage_count: 0, edictum_configs: [], chat_enabled: false }));
        const requests = [];
        const errors = [];
        page.on('pageerror', (error) => errors.push(error.message));
        await context.route(`${origin}/ui-config.json`, (route) => route.fulfill(jsonResponse({ backend_api: 'http://127.0.0.1:19870', control_api: 'http://127.0.0.1:19871', chat_api: 'http://127.0.0.1:19872' }, origin)));
        const handler = async (route) => {
          const request = route.request();
          const url = new URL(request.url());
          const record = { path: url.pathname + url.search, started: performance.now(), bytes: 0 };
          requests.push(record);
          if (scenario.delay) await new Promise((resolve) => setTimeout(resolve, scenario.delay));
          try {
            let data;
            if (url.pathname === '/api/chat/approval') data = { ok: true, available: false, mode: 'user', revision: 0, plan_mode: false };
            else if (url.pathname === '/api/chat/command-history') data = { ok: true, command_history_enabled: false };
            else if (url.pathname === '/api/chat/default-agent-prompt') data = { ok: true, default_agent_prompt_enabled: false };
            else if (url.pathname === '/api/chat/max-iterations') data = { ok: true, max_iterations: 50 };
            else if (url.pathname === '/config/overview') data = { ok: true, model_counts: { llm: 1, embedding: 0, rerank: 0, asr: 0 }, session_count: 0 };
            else if (url.pathname === '/api/chat/conversations/page') data = { conversations: fixture.conversations.slice(0, 50), has_more: fixture.conversations.length > 50, next_cursor: 'fixture-cursor' };
            else if (url.pathname === '/config/snapshot') {
              const resource = url.searchParams.get('resource');
              const resources = resource === 'plugin_catalog' ? ['plugin_catalog', 'edictum_configs', 'chat_plugins'] : [resource];
              data = { data: resource === 'models' ? (url.searchParams.get('type') === 'llm' ? { 'benchmark-model': { model: 'benchmark-model' } } : {}) : resource === 'plugin_catalog' ? plugins : {}, versions: Object.fromEntries(resources.map((name) => [name, { change_token: 'fixture-v1', state: 'available' }])) };
            }
            else if (url.pathname === '/config/plugins') data = { plugins };
            else if (url.pathname === '/api/chat/turns/page') {
              const limit = Number(url.searchParams.get('limit') || 50);
              const before = Number(url.searchParams.get('before') || fixture.turns.length);
              data = { turns: fixture.turns.slice(Math.max(0, before - limit), before), has_more: before > limit, next_before: Math.max(0, before - limit) };
            } else data = responseFor(request, fixture);
            if (url.pathname === '/config/models' && url.searchParams.get('type') === 'llm') data = { 'benchmark-model': { model: 'benchmark-model' } };
            if (url.searchParams.get('include_version') === 'true') {
              const resources = url.pathname.includes('session-classes') ? ['session_classes'] : ['models'];
              data = { data, versions: Object.fromEntries(resources.map((name) => [name, { change_token: 'fixture-v1', state: 'available' }])) };
            }
            const response = jsonResponse(data, origin);
            record.bytes = Buffer.byteLength(response.body);
            await route.fulfill(response);
          } catch (error) {
            errors.push(String(error));
            await route.fulfill({ status: 500, body: '{}' });
          }
          record.finished = performance.now();
        };
        for (const port of [19870, 19871, 19872]) await context.route(`http://127.0.0.1:${port}/**`, handler);
        const sockets = new Set();
        await installWebSocketMocks(page, sockets);
        let started = performance.now();
        let hotVisible;
        await page.goto(origin + scenario.route);
        if (scenario.route === '/') await page.waitForFunction(() => /模型配置\s*1/.test(document.body.innerText));
        else if (scenario.route === '/models') await page.getByText('benchmark-model', { exact: true }).first().waitFor();
        else if (scenario.route === '/plugins') await page.getByText('bench-plugin-0', { exact: true }).first().waitFor();
        else await page.getByText('继续最近对话', { exact: true }).waitFor();
        if (scenario.hot) {
          await page.waitForTimeout(scenario.delay * 4 + 100);
          await page.locator('a[href="/"]').first().click();
          await page.waitForFunction(() => /模型配置\s*1/.test(document.body.innerText));
          await page.waitForTimeout(scenario.delay * 4 + 100);
          requests.length = 0;
          started = performance.now();
          await page.locator(`a[href="${scenario.route}"]`).first().click();
          await page.getByText(scenario.route === '/models' ? 'benchmark-model' : 'bench-plugin-0', { exact: true }).first().waitFor();
          hotVisible = performance.now() - started;
          await page.waitForTimeout(scenario.delay * 2 + 50);
        }
        const listReady = hotVisible ?? performance.now() - started;
        if (scenario.history) {
          await page.getByText('继续最近对话', { exact: true }).click();
          await page.waitForFunction(() => document.querySelectorAll('.chat-message-item').length >= 2);
          await page.getByText(`第 ${scenario.turns} 轮回答`, { exact: false }).last().waitFor();
        }
        const ready = hotVisible ?? performance.now() - started;
        let measured;
        if (interaction) {
          const session = await context.newCDPSession(page);
          measured = await measureScenario(page, session, async () => {
            if (scenario.streaming) {
              const socket = [...sockets][0];
              if (!socket) throw new Error('未建立流式测试连接');
              const sent = page.waitForResponse((response) => new URL(response.url()).pathname === '/api/chat/send');
              await page.getByPlaceholder('输入消息, Enter 发送, Shift+Enter 换行').fill('流式基准');
              await page.getByPlaceholder('输入消息, Enter 发送, Shift+Enter 换行').press('Enter');
              await sent;
              socket.send(JSON.stringify({ type: 'turn_start', user_input: '流式基准', turn_id: fixture.turns.length + 1, turn_index: fixture.turns.length, variant_index: 0 }));
              const first = performance.now();
              socket.send(JSON.stringify({ type: 'content_delta', delta: '测' }));
              await page.locator('.chat-message-item').last().getByText('测', { exact: true }).waitFor();
              const first_content_ms = performance.now() - first;
              for (let index = 1; index < 1000; index++) socket.send(JSON.stringify({ type: 'content_delta', delta: '测' }));
              socket.send(JSON.stringify({ type: 'turn_done', answer: '测'.repeat(1000), turn_id: fixture.turns.length + 1, turn_index: fixture.turns.length, variant_index: 0, variant_count: 1 }));
              await page.getByTitle('发送', { exact: true }).waitFor();
              const count = await page.locator('.chat-message-item').last().evaluate((element) => [...element.textContent].filter((character) => character === '测').length);
              if (count !== 1000) throw new Error('流式内容不完整');
              return { first_content_ms, stream_integrity: 1 };
            }
            if (scenario.history) await page.evaluate(async () => {
              const element = document.querySelector('.chat-message-item').closest('.overflow-y-auto');
              for (let index = 0; index < 120; index++) {
                element.scrollTop = (element.scrollHeight - element.clientHeight) * (0.5 + Math.sin(index / 12) * 0.4);
                await new Promise(requestAnimationFrame);
              }
            });
            else await page.waitForTimeout(1000);
            return {};
          });
        }
        await page.waitForTimeout(100);
        if (errors.length) throw new Error(errors.join('\n'));
        if (run >= 0) samples.push({ ...scenario, run, ready_ms: ready, list_ready_ms: listReady, api_requests: requests.length, config_requests: requests.filter((item) => item.path.startsWith('/config/')).length, bytes: requests.reduce((sum, item) => sum + item.bytes, 0), dom_nodes: await page.locator('*').count(), measured, requests: requests.map(({ path, started: start, finished, bytes }) => ({ path, start: start - started, end: finished === undefined ? null : finished - started, bytes })) });
      } catch (error) {
        if (scenario.history && error.name === 'TimeoutError') {
          samples.push({ ...scenario, run, timed_out: true, timeout_ms: 30000, error: error.message.split('\n')[0] });
          console.log(JSON.stringify({ timeout: scenario, run, timeout_ms: 30000 }));
          break;
        }
        throw error;
      } finally { await context.close(); }
    }
    console.log(JSON.stringify({ completed: scenario, samples: iterations }));
  }
  await mkdir(results, { recursive: true });
  const patch = execFileSync('git', ['diff', '--', 'satrap', 'satrap-ui/src'], { cwd: path.resolve(project, '..'), maxBuffer: 10 * 1024 * 1024 });
  const extraSources = {};
  for (const name of ['satrap/core/browser_sessions.py', 'satrap-ui/src/components/common/LogoutButton.tsx']) {
    try { extraSources[name] = createHash('sha256').update(await readFile(path.resolve(project, '..', name))).digest('hex'); } catch {}
  }
  const bundleHash = createHash('sha256');
  for (const [name, content] of [...files].sort(([left], [right]) => left.localeCompare(right))) bundleHash.update(name).update(content);
  const report = { commit: mode === 'baseline' ? baselineRevision : execFileSync('git', ['rev-parse', 'HEAD'], { cwd: project, encoding: 'utf8' }).trim(), source: mode === 'baseline' ? 'git revision via source loader' : 'working tree', launch_profile: interaction ? 'chromium-precise' : 'headless-shell-default', bundle_sha256: bundleHash.digest('hex'), patch_sha256: mode === 'baseline' ? null : createHash('sha256').update(patch).digest('hex'), extra_sources: mode === 'baseline' ? {} : extraSources, created_at: new Date().toISOString(), node: process.version, browser: browser.version(), os: `${os.platform()} ${os.release()}`, iterations, warmups, samples };
  await writeFile(target, JSON.stringify(report, null, 2) + '\n');
  if (mode === 'after') {
    const baseline = JSON.parse(await readFile(path.join(results, `${prefix}baseline.json`), 'utf8'));
    if (['browser', 'node', 'os', 'iterations', 'warmups'].some((key) => baseline[key] !== report[key])) throw new Error('基准环境或采样数不同, 不生成改善比例');
    const median = (values) => [...values].sort((a, b) => a - b)[Math.floor(values.length / 2)];
    const lines = ['# 信息加载 benchmark 前后对比', '', `修改前提交: ${baseline.commit}`, `修改后基于提交: ${report.commit}`, '', '| 页面 / 额外等待 / 轮次 | 修改前 P50 ms | 修改后 P50 ms | 变化 | 修改前请求 | 修改后请求 |', '| --- | ---: | ---: | ---: | ---: | ---: |'];
    for (const scenario of scenarios) {
      const select = (data) => data.samples.filter((sample) => sample.route === scenario.route && sample.delay === scenario.delay && sample.turns === scenario.turns && !!sample.history === !!scenario.history && !!sample.hot === !!scenario.hot);
      const before = select(baseline), after = select(report);
      if (before.some((sample) => sample.timed_out) || after.some((sample) => sample.timed_out)) {
        lines.push(`| ${scenario.route} / ${scenario.delay}ms / ${scenario.turns} 历史 | ${before.some((sample) => sample.timed_out) ? '超时 >=30,000' : median(before.map((sample) => sample.ready_ms)).toFixed(1)} | ${after.some((sample) => sample.timed_out) ? '超时 >=30,000' : median(after.map((sample) => sample.ready_ms)).toFixed(1)} | 超时场景不计算比例 | - | - |`);
        continue;
      }
      const left = median(before.map((sample) => sample.ready_ms)), right = median(after.map((sample) => sample.ready_ms));
      lines.push(`| ${scenario.route} / ${scenario.delay}ms / ${scenario.turns}${scenario.history ? ' 历史' : ''}${scenario.hot ? ' 热缓存' : ''} | ${left.toFixed(1)} | ${right.toFixed(1)} | ${((right / left - 1) * 100).toFixed(1)}% | ${median(before.map((sample) => sample.api_requests))} | ${median(after.map((sample) => sample.api_requests))} |`);
    }
    const interactionDetails = [];
    lines.push('', '| 页面 / 等待 / 轮次 | 修改前 P95 ms | 修改后 P95 ms | 修改前字节 | 修改后字节 | 修改前 DOM | 修改后 DOM |', '| --- | ---: | ---: | ---: | ---: | ---: | ---: |');
    for (const scenario of scenarios) {
      const select = (data) => data.samples.filter((sample) => sample.route === scenario.route && sample.delay === scenario.delay && sample.turns === scenario.turns && !!sample.history === !!scenario.history && !!sample.hot === !!scenario.hot);
      const before = select(baseline), after = select(report);
      if ([...before, ...after].some((sample) => sample.timed_out)) continue;
      const p95 = (values) => [...values].sort((a, b) => a - b)[Math.ceil(values.length * 0.95) - 1];
      lines.push(`| ${scenario.route} / ${scenario.delay}ms / ${scenario.turns}${scenario.history ? ' 历史' : ''}${scenario.hot ? ' 热缓存' : ''} | ${p95(before.map((sample) => sample.ready_ms)).toFixed(1)} | ${p95(after.map((sample) => sample.ready_ms)).toFixed(1)} | ${median(before.map((sample) => sample.bytes))} | ${median(after.map((sample) => sample.bytes))} | ${median(before.map((sample) => sample.dom_nodes))} | ${median(after.map((sample) => sample.dom_nodes))} |`);
      if (interaction) {
        const left = summarizeRuns(before.map((sample) => sample.measured)), right = summarizeRuns(after.map((sample) => sample.measured));
        interactionDetails.push('', '```json', JSON.stringify({ scenario, before: left, after: right }, null, 2), '```', '');
      }
    }
    await writeFile(path.join(results, `${prefix}comparison.md`), [...lines, ...interactionDetails].join('\n').trimEnd() + '\n');
  }
  console.log(`saved ${target}`);
} finally {
  await browser?.close();
  await new Promise((resolve) => server.close(resolve));
}
