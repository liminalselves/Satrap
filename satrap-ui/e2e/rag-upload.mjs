/** 浏览器文件上传到真实 HTTP, 文档解析和 SQLite / FAISS 检索的完整回归 */
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdtemp, mkdir, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';
import { createServer } from 'vite';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const temporary = await mkdtemp(path.join(tmpdir(), 'satrap-rag-upload-'));
const server = await createServer({ root, server: { host: '127.0.0.1', port: 0 } });
let backend, browser;
try {
  await server.listen();
  const origin = 'http://127.0.0.1:' + server.httpServer.address().port;
  const token = 'browser-upload-test-token-longer-than-32-characters';
  backend = spawn(process.env.SATRAP_TEST_PYTHON || 'python', [path.join(root, 'e2e/rag-upload-backend.py'), temporary, origin, token], {
    cwd: path.dirname(root), env: { ...process.env, PYTHONPATH: path.dirname(root), PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' },
    stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true,
  });
  let logs = '';
  backend.stderr.on('data', (chunk) => { logs += chunk; });
  const port = await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error('后端启动超时: ' + logs)), 30000);
    let output = '';
    backend.stdout.on('data', (chunk) => {
      output += chunk;
      const match = output.match(/READY (\{[^\n]+\})/);
      if (match) { clearTimeout(timeout); resolve(JSON.parse(match[1]).port); }
    });
    backend.once('error', (error) => { clearTimeout(timeout); reject(error); });
    backend.once('exit', (code) => { clearTimeout(timeout); reject(new Error('后端退出 ' + code + ': ' + logs)); });
  });
  const base = 'http://127.0.0.1:' + port;
  browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  await context.route(origin + '/ui-config.json', (route) => route.fulfill({ json: { backend_api: base, control_api: base, chat_api: base } }));
  await context.route(base + '/**', async (route) => {
    const pathname = new URL(route.request().url()).pathname;
    if (pathname.startsWith('/config/rag') || pathname.startsWith('/auth/')) return route.continue();
    return route.fulfill({ json: { ok: true, sessions: [], models: [], running: false }, headers: { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true' } });
  });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.routeWebSocket(base.replace('http', 'ws') + '/**', () => {});
  await page.goto(origin + '/rag#token=' + token);
  await page.getByRole('button', { name: '上传文档', exact: true }).click();
  await page.getByLabel('导入文件').setInputFiles([
    { name: 'bad.exe', mimeType: 'application/octet-stream', buffer: Buffer.from('bad') },
    { name: 'bad.txt', mimeType: 'text/plain', buffer: Buffer.from([255]) },
  ]);
  await page.getByRole('alert').filter({ hasText: '不支持' }).waitFor();
  await page.getByRole('button', { name: '导入到此库' }).click();
  await page.getByRole('alert').filter({ hasText: 'UTF-8' }).waitFor();
  await page.getByRole('button', { name: '移除 bad.txt', exact: true }).click();
  const content = Buffer.from('中文文档'.repeat(100000), 'utf8');
  assert.ok(content.length > 1024 * 1024);
  await page.getByLabel('导入文件').setInputFiles({ name: '真实中文.md', mimeType: 'text/markdown', buffer: content });
  const artifacts = path.resolve(root, '../.satrap/test-artifacts');
  await mkdir(artifacts, { recursive: true });
  await page.screenshot({ path: path.join(artifacts, 'rag-upload-modal.png'), fullPage: true });
  const response = page.waitForResponse((res) => res.url().includes('/config/rag/upload') && res.request().method() === 'POST');
  await page.getByRole('button', { name: '导入到此库' }).click();
  const uploaded = await response;
  assert.equal(uploaded.status(), 200);
  assert.equal(uploaded.request().postDataBuffer().length, content.length);
  await page.getByText('真实中文.md · 4 块', { exact: true }).waitFor();
  await page.getByRole('button', { name: '关闭', exact: true }).click();
  await page.getByLabel('检索问题').fill('中文文档');
  await page.getByRole('button', { name: '测试检索' }).click();
  await page.locator('pre').filter({ hasText: '真实中文.md' }).waitFor();
  await page.getByRole('button', { name: '上传文档', exact: true }).click();
  await page.getByRole('group', { name: '文件拖放区域' }).evaluate((element) => {
    const transfer = new DataTransfer();
    transfer.items.add(new File(['中文文档'.repeat(100000)], '真实中文.md', { type: 'text/markdown' }));
    transfer.items.add(new File(['FAIL_ONCE'], '稍后重试.txt', { type: 'text/plain' }));
    transfer.items.add(new File(['后续正常文档'], '第二份.txt', { type: 'text/plain' }));
    element.dispatchEvent(new DragEvent('drop', { bubbles: true, dataTransfer: transfer }));
  });
  await page.getByRole('button', { name: '导入到此库' }).click();
  await page.getByRole('status').filter({ hasText: '共 3 个 · 成功 1 · 跳过 1 · 失败 1' }).waitFor();
  await page.getByRole('alert').filter({ hasText: '向量化或构建索引失败: RuntimeError: Embedding 服务返回 429: quota exceeded' }).waitFor();
  await page.getByText('第二份.txt · 1 块', { exact: true }).waitFor();
  const retryRequests = [];
  const trackRetry = (request) => { if (request.url().includes('/config/rag/upload') && request.method() === 'POST') retryRequests.push(new URL(request.url()).searchParams.get('file_name')); };
  page.on('request', trackRetry);
  await page.getByRole('button', { name: '重试失败文件', exact: true }).click();
  await page.getByRole('status').filter({ hasText: '共 3 个 · 成功 2 · 跳过 1 · 失败 0' }).waitFor();
  page.off('request', trackRetry);
  assert.deepEqual(retryRequests, ['稍后重试.txt']);
  await page.screenshot({ path: path.join(artifacts, 'rag-batch-results.png'), fullPage: true });
  await page.getByRole('button', { name: '关闭', exact: true }).click();
  await page.getByRole('button', { name: '上传文档', exact: true }).click();
  await page.getByRole('button', { name: '粘贴文本', exact: true }).click();
  await page.getByLabel('文档来源').fill('粘贴资料');
  await page.getByLabel('导入文本').fill('中文文档'.repeat(100000));
  await page.getByRole('button', { name: '导入到此库' }).click();
  await page.getByText('粘贴资料 · 4 块', { exact: true }).waitFor();
  assert.deepEqual(errors, []);
  console.log('PASS: 多文件拖放, 独立来源, 部分失败继续, 真实构建错误, 仅重试失败项, 重复跳过, 大文件及文本导入检索');
} finally {
  if (browser) await browser.close();
  if (backend && backend.exitCode === null) {
    backend.stdin.end('\n');
    await new Promise((resolve) => {
      const timer = setTimeout(() => { backend.kill(); resolve(); }, 10000);
      backend.once('exit', () => { clearTimeout(timer); resolve(); });
    });
  }
  await server.close();
  const resolved = path.resolve(temporary);
  if (path.dirname(resolved) !== path.resolve(tmpdir()) || !path.basename(resolved).startsWith('satrap-rag-upload-')) throw new Error('临时目录范围检查失败');
  await rm(resolved, { recursive: true, force: true });
}
