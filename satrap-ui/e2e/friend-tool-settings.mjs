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
  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  const errors = [], writes = [];
  const handle = 'friend_manager_handle_request';
  const deletion = 'friend_manager_delete_friend';
  const send = 'friend_manager_send_request';
  let tools = { [deletion]: false }, toolRevision = 1;
  const inherited = { [handle]: false, [deletion]: true, [send]: true };
  page.on('pageerror', (error) => errors.push(error.message));
  await page.route(`${origin}/ui-config.json`, (route) => route.fulfill({ json: {
    backend_api: 'http://127.0.0.1:19870', control_api: 'http://127.0.0.1:19871', chat_api: 'http://127.0.0.1:19872',
  } }));
  await page.route(/^http:\/\/127\.0\.0\.1:1987[012]\//, (route) => {
    const request = route.request();
    let json = { ok: true }, status = 200;
    const url = new URL(request.url());
    if (url.pathname === '/config/models') json = {};
    if (url.pathname === '/config/session-classes') json = { configs: {} };
    if (url.pathname.endsWith('/session-plugin-config')) {
      if (request.method() === 'PUT') {
        const body = request.postDataJSON();
        writes.push(body);
        if (body.expected_tool_revision !== toolRevision) { status = 409; json = { error: '工具配置已更新' }; }
        else { tools = body.tool_overrides; toolRevision++; }
      }
      if (status === 200) json = { ok: true, schema: {}, config: {}, overrides: {}, inherited: {}, sources: {}, revision: 0,
        tool_overrides: tools, tool_revision: toolRevision, inherited_tools: inherited,
        tool_descriptions: { [handle]: '处理好友申请', [deletion]: '删除好友', [send]: '发送好友申请' } };
    }
    return route.fulfill({ status, json, headers: { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true',
      'Access-Control-Allow-Headers': 'Content-Type', 'Access-Control-Allow-Methods': 'GET,PUT,OPTIONS' } });
  });
  await page.routeWebSocket(/ws:\/\/127\.0\.0\.1:1987[012]\//, () => {});
  await page.goto(origin);
  await page.evaluate(async () => {
    const React = await import('/node_modules/.vite/deps/react.js');
    const client = await import('/node_modules/.vite/deps/react-dom_client.js');
    const { SessionPluginSettingsModal } = await import('/src/components/common/SessionPluginSettingsModal.tsx');
    const host = document.createElement('div');
    document.body.appendChild(host);
    client.default.createRoot(host).render(React.default.createElement(SessionPluginSettingsModal,
      { context: { platformId: 'chat', sessionId: 'one' }, plugins: ['friend_manager'], onClose() {} }));
  });
  const dialog = page.getByRole('dialog');
  await dialog.getByLabel(deletion, { exact: true }).waitFor();
  assert.equal(await dialog.getByRole('checkbox').count(), 3);
  assert.equal(await dialog.getByLabel(deletion).isChecked(), false);
  assert.equal(await dialog.getByLabel(handle).isChecked(), false);
  await dialog.getByLabel(deletion).check();
  await dialog.getByRole('button', { name: '保存', exact: true }).click();
  await dialog.getByRole('status').waitFor();
  assert.deepEqual(writes[0], { expected_revision: 0, overrides: {}, expected_tool_revision: 1, tool_overrides: { [deletion]: true } });
  await dialog.getByLabel(handle).check();
  toolRevision++;
  await dialog.getByRole('button', { name: '保存', exact: true }).click();
  await dialog.getByRole('alert').waitFor();
  assert.deepEqual(tools, { [deletion]: true });
  assert.equal(await dialog.getByLabel(handle).isChecked(), true);
  await dialog.getByRole('button', { name: '重新加载' }).click();
  await page.waitForFunction((name) => document.querySelector(`input[aria-label="${name}"]`)?.checked === false, handle);
  assert.equal(await dialog.getByLabel(handle).isChecked(), false);
  await dialog.getByRole('button', { name: '恢复工具继承' }).click();
  await dialog.getByRole('button', { name: '保存', exact: true }).click();
  await dialog.getByRole('status').waitFor();
  assert.deepEqual(writes.at(-1).tool_overrides, {});
  assert.equal(writes.at(-1).expected_tool_revision, 3);
  assert.equal(await dialog.getByLabel(deletion).isChecked(), true);
  assert.deepEqual(errors, []);
  console.log('PASS: 实例工具覆盖可见可编辑, 双修订保存, 冲突保留草稿, 恢复工具继承');
} finally {
  await browser?.close();
  await server.close();
}
