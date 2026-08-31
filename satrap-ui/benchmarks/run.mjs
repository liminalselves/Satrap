import { spawn } from 'node:child_process';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import process from 'node:process';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';
import {
  createBenchmarkFixture,
  installHttpMocks,
  installWebSocketMocks,
} from './fixtures.mjs';
import { measureScenario, summarizeRuns } from './metrics.mjs';
import { createComparisonReport } from './report.mjs';

const benchmarkDir = path.dirname(fileURLToPath(import.meta.url));
const projectDir = path.resolve(benchmarkDir, '..');
const resultsDir = path.join(benchmarkDir, 'results');
const appOrigin = 'http://127.0.0.1:4173';
const baselineMode = process.argv.includes('--baseline');
const smokeMode = process.argv.includes('--smoke');
const headed = process.argv.includes('--headed');
const iterations = smokeMode ? 1 : Number(process.env.BENCHMARK_ITERATIONS ?? 5);
const warmups = smokeMode ? 0 : Number(process.env.BENCHMARK_WARMUPS ?? 1);
const idleDurationMs = smokeMode ? 500 : Number(process.env.BENCHMARK_IDLE_MS ?? 10_000);
const streamCounts = smokeMode ? [100] : [1_000, 5_000];
const historySizes = smokeMode ? [100] : [100, 500, 1_000];

function startPreviewServer() {
  const viteBin = path.join(projectDir, 'node_modules', 'vite', 'bin', 'vite.js');
  return spawn(process.execPath, [viteBin, 'preview', '--host', '127.0.0.1', '--port', '4173'], {
    cwd: projectDir,
    env: { ...process.env, FORCE_COLOR: '0' },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
}

async function waitForServer(server) {
  const timeoutAt = Date.now() + 15_000;
  while (Date.now() < timeoutAt) {
    if (server.exitCode !== null) throw new Error(`Vite preview 提前退出, code=${server.exitCode}`);
    try {
      const response = await fetch(appOrigin);
      if (response.ok) return;
    } catch {
      // 服务尚未就绪时继续等待
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error('等待 Vite preview 超时');
}

async function openPage(browser, { pathName, theme, messageCount }) {
  const context = await browser.newContext({
    viewport: { width: 1920, height: 1080 },
    colorScheme: theme,
    reducedMotion: 'no-preference',
  });
  const fixture = createBenchmarkFixture(messageCount);
  await installHttpMocks(context, appOrigin, fixture);
  await context.addInitScript((initialTheme) => {
    localStorage.setItem('satrap-theme', initialTheme);
    localStorage.setItem('satrap.chat.think', 'off');
  }, theme);
  const page = await context.newPage();
  const chatSockets = new Set();
  await installWebSocketMocks(page, chatSockets);
  await page.goto(`${appOrigin}${pathName}`, { waitUntil: 'domcontentloaded' });
  await page.locator(`[data-theme="${theme}"]`).waitFor({ state: 'attached' });
  return { context, page, fixture, chatSockets };
}

async function openChat(browser, messageCount) {
  const opened = await openPage(browser, { pathName: '/chat', theme: 'light', messageCount });
  await opened.page.getByText('继续最近对话', { exact: true }).waitFor();
  await opened.page.getByText('继续最近对话', { exact: true }).click();
  await opened.page.getByPlaceholder('输入消息, Enter 发送, Shift+Enter 换行').waitFor();
  const expectedMessages = Math.ceil(messageCount / 2) * 2;
  await opened.page.waitForFunction((expected) => (
    document.querySelectorAll('.chat-message-item').length >= expected
  ), expectedMessages, { timeout: 30_000 });
  return opened;
}

async function waitForChatSocket(chatSockets) {
  const timeoutAt = Date.now() + 5_000;
  while (Date.now() < timeoutAt) {
    const socket = chatSockets.values().next().value;
    if (socket) return socket;
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
  throw new Error('Chat WebSocket 夹具未连接');
}

async function runMeasured(browser, setup, action, settleMs = 250) {
  const opened = await setup();
  const session = await opened.context.newCDPSession(opened.page);
  try {
    return await measureScenario(opened.page, session, () => action(opened), settleMs);
  } finally {
    await session.detach();
    await opened.context.close();
  }
}

function buildScenarios(browser) {
  const scenarios = [
    {
      id: 'dashboard_idle_light',
      label: '仪表盘浅色静置',
      run: () => runMeasured(
        browser,
        () => openPage(browser, { pathName: '/', theme: 'light', messageCount: 100 }),
        async ({ page }) => page.waitForTimeout(idleDurationMs),
        0,
      ),
    },
    {
      id: 'dashboard_idle_dark',
      label: '仪表盘深色静置',
      run: () => runMeasured(
        browser,
        () => openPage(browser, { pathName: '/', theme: 'dark', messageCount: 100 }),
        async ({ page }) => page.waitForTimeout(idleDurationMs),
        0,
      ),
    },
    {
      id: 'glass_pointer_sweep',
      label: '玻璃卡片指针移动',
      run: () => runMeasured(
        browser,
        () => openPage(browser, { pathName: '/', theme: 'light', messageCount: 100 }),
        async ({ page }) => {
          const pointerEvents = await page.evaluate(async () => {
            let emitted = 0;
            await new Promise((resolve) => {
              let frame = 0;
              const move = () => {
                for (let index = 0; index < 4; index += 1) {
                  const progress = (frame * 4 + index) / 480;
                  const event = new PointerEvent('pointermove', {
                    bubbles: true,
                    clientX: 280 + progress * 1_400,
                    clientY: 180 + Math.sin(progress * Math.PI * 8) * 320 + 320,
                    pointerType: 'mouse',
                  });
                  document.dispatchEvent(event);
                  emitted += 1;
                }
                frame += 1;
                if (frame < 120) requestAnimationFrame(move);
                else resolve();
              };
              requestAnimationFrame(move);
            });
            return emitted;
          });
          return { pointer_events_requested: pointerEvents };
        },
      ),
    },
  ];

  for (const messageCount of historySizes) {
    scenarios.push({
      id: `chat_history_scroll_${messageCount}`,
      label: `Chat ${messageCount} 条消息滚动`,
      run: () => runMeasured(
        browser,
        () => openChat(browser, messageCount),
        async ({ page }) => {
          const scrollArea = page.locator('.chat-message-item').first().locator('xpath=ancestor::div[contains(@class,"overflow-y-auto")][1]');
          await scrollArea.evaluate(async (element) => {
            const maximum = element.scrollHeight - element.clientHeight;
            const started = performance.now();
            await new Promise((resolve) => {
              const step = (timestamp) => {
                const progress = Math.min(1, (timestamp - started) / 2_000);
                element.scrollTop = maximum * progress;
                if (progress < 1) requestAnimationFrame(step);
                else resolve();
              };
              requestAnimationFrame(step);
            });
          });
          return { history_messages: messageCount };
        },
      ),
    });
  }

  for (const deltaCount of streamCounts) {
    scenarios.push({
      id: `chat_streaming_${deltaCount}`,
      label: `Chat ${deltaCount} 个流式增量`,
      run: () => runMeasured(
        browser,
        () => openChat(browser, 20),
        async ({ page, chatSockets, fixture }) => {
          const socket = await waitForChatSocket(chatSockets);
          const input = page.getByPlaceholder('输入消息, Enter 发送, Shift+Enter 换行');
          await input.fill('执行流式性能测试');
          await input.press('Enter');
          socket.send(JSON.stringify({
            type: 'turn_start',
            user_input: '执行流式性能测试',
            turn_id: fixture.turns.length + 1,
            turn_index: fixture.turns.length,
            variant_index: 0,
          }));
          const firstContentAt = performance.now();
          socket.send(JSON.stringify({ type: 'content_delta', delta: '测' }));
          await page.locator('.chat-message-item').last().getByText('测', { exact: true }).waitFor();
          const firstContentMs = performance.now() - firstContentAt;
          for (let index = 1; index < deltaCount; index += 1) {
            socket.send(JSON.stringify({ type: 'content_delta', delta: '测' }));
          }
          socket.send(JSON.stringify({
            type: 'turn_done',
            answer: '测'.repeat(deltaCount),
            turn_id: fixture.turns.length + 1,
            turn_index: fixture.turns.length,
            variant_index: 0,
            variant_count: 1,
          }));
          await page.getByTitle('发送').waitFor();
          const renderedCount = await page.locator('.chat-message-item').last().evaluate((element) => (
            [...(element.textContent ?? '')].filter((character) => character === '测').length
          ));
          return {
            stream_deltas: deltaCount,
            first_content_ms: Math.round(firstContentMs * 1000) / 1000,
            rendered_stream_chars: renderedCount,
            stream_integrity: renderedCount === deltaCount ? 1 : 0,
          };
        },
        500,
      ),
    });
  }

  scenarios.push({
    id: 'chat_common_interactions',
    label: 'Chat 常用交互',
    run: () => runMeasured(
      browser,
      () => openChat(browser, 100),
      async ({ page }) => {
        await page.getByTitle('展开选项').click();
        await page.getByTitle('收起选项').click();
        await page.getByTitle('对话设置').click();
        await page.getByRole('dialog').waitFor();
        await page.keyboard.press('Escape');
        await page.getByRole('dialog').waitFor({ state: 'hidden' });
        await page.getByTitle('切换到暗色模式').click();
        await page.locator('[data-theme="dark"]').waitFor({ state: 'attached' });
        await page.getByTitle('切换到亮色模式').click();
        await page.locator('[data-theme="light"]').waitFor({ state: 'attached' });
        return { interaction_steps: 6 };
      },
    ),
  });
  return scenarios;
}

async function run() {
  await mkdir(resultsDir, { recursive: true });
  const server = startPreviewServer();
  let browser;
  try {
    await waitForServer(server);
    browser = await chromium.launch({
      headless: !headed,
      channel: headed ? undefined : 'chromium',
      args: [
        '--disable-background-timer-throttling',
        '--disable-renderer-backgrounding',
        '--disable-backgrounding-occluded-windows',
        '--enable-precise-memory-info',
      ],
    });
    const scenarios = buildScenarios(browser);
    const results = [];
    for (const scenario of scenarios) {
      process.stdout.write(`\n[benchmark] ${scenario.label}\n`);
      for (let index = 0; index < warmups; index += 1) {
        await scenario.run();
        process.stdout.write(`  预热 ${index + 1}/${warmups}\n`);
      }
      const runs = [];
      for (let index = 0; index < iterations; index += 1) {
        const measured = await scenario.run();
        runs.push(measured);
        process.stdout.write(`  正式 ${index + 1}/${iterations}: ${measured.average_fps} FPS, P95 ${measured.p95_frame_ms} ms\n`);
      }
      results.push({
        id: scenario.id,
        label: scenario.label,
        runs,
        summary: summarizeRuns(runs),
      });
    }

    const integrityFailures = results.filter((scenario) => (
      scenario.id.startsWith('chat_streaming')
      && scenario.runs.some((item) => item.stream_integrity !== 1)
    ));
    if (integrityFailures.length > 0) {
      throw new Error(`流式完整性校验失败: ${integrityFailures.map((item) => item.label).join(', ')}`);
    }

    const current = {
      metadata: {
        generated_at: new Date().toISOString(),
        platform: `${os.platform()} ${os.release()} ${os.arch()}`,
        cpu: os.cpus()[0]?.model ?? 'unknown',
        logical_cpus: os.cpus().length,
        memory_gb: Math.round((os.totalmem() / 1_073_741_824) * 100) / 100,
        node: process.version,
        browser: await browser.version(),
        viewport: '1920x1080',
        headless: !headed,
        iterations,
        warmups,
        idle_duration_ms: idleDurationMs,
      },
      scenarios: results,
    };

    const latestPath = path.join(resultsDir, smokeMode ? 'smoke.json' : 'latest.json');
    await writeFile(latestPath, `${JSON.stringify(current, null, 2)}\n`, 'utf8');
    if (smokeMode) {
      process.stdout.write(`\n[benchmark] Smoke 结果: ${latestPath}\n`);
      return;
    }
    const baselinePath = path.join(resultsDir, 'baseline.json');
    let baseline = null;
    try {
      baseline = JSON.parse(await readFile(baselinePath, 'utf8'));
    } catch {
      baseline = null;
    }
    if (baselineMode || !baseline) {
      await writeFile(baselinePath, `${JSON.stringify(current, null, 2)}\n`, 'utf8');
      baseline = baselineMode ? null : baseline;
    }
    const report = createComparisonReport(current, baselineMode ? null : baseline);
    await writeFile(path.join(resultsDir, 'comparison.md'), `${report}\n`, 'utf8');
    process.stdout.write(`\n[benchmark] 结果: ${latestPath}\n`);
  } finally {
    await browser?.close();
    server.kill();
  }
}

await run();
