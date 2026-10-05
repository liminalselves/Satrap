import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { mkdir } from 'node:fs/promises';
import { chromium } from 'playwright';
import { createServer } from 'vite';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const server = await createServer({ root, server: { host: '127.0.0.1', port: 0 } });
let browser;
try {
  await server.listen();
  const origin = server.resolvedUrls.local[0].replace(/\/$/, '');
  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  page.setDefaultTimeout(10000);
  const errors = [];
  const writes = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.routeWebSocket('ws://127.0.0.1:19870/ws/status**', () => {});
  let conflict = false;
  let runtimeStatus = 'unconfirmed';
  let revision = 1;
  const group = { id: 'existing', name: '主要管理员', enabled: true,
    members: [{ platform_id: 'future', platform_instance_id: 'old-instance', user_id: 'user@example.test' }],
    plugin_scope: { mode: 'selected', included: ['group_admin', 'temporarily_missing'], excluded: [] } };
  const snapshot = { ok: true, groups: [group], revision: 'v1', section_revision: 's1',
    platforms: [{ id: 'future', type: 'future-adapter', name: '扩展平台', instance_id: 'new-instance', enabled: true }],
    plugins: [{ name: 'group_admin', description: '管理', supports_administrators: true, management_permissions: { write: { description: '请求群管理修改操作', system_admin: true, requirements: ['需开启管理写操作', '需要审批的动作仍须批准'] } } },
      { name: 'ordinary_plugin', description: '普通插件', supports_administrators: false, management_permissions: {} }],
    plugin_errors: [{ name: 'invalid_plugin', error: '权限声明引用不存在的工具' }],
    invalid_members: [{ group_id: 'existing', platform_id: 'future', user_id: 'user@example.test' }],
    runtime: { status: 'unconfirmed', error: '尚未同步' } };
  const cors = { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true', 'Access-Control-Allow-Headers': 'Content-Type', 'Access-Control-Allow-Methods': 'GET,POST,PUT,OPTIONS' };
  await page.route('**/*', async route => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.origin === origin && url.pathname !== '/ui-config.json') return route.continue();
    if (url.pathname === '/ui-config.json') return route.fulfill({ json: { backend_api: 'http://127.0.0.1:19870', control_api: 'http://127.0.0.1:19871', chat_api: 'http://127.0.0.1:19872' } });
    if (request.method() === 'OPTIONS') return route.fulfill({ status: 204, headers: cors });
    if (request.method() !== 'GET' && url.pathname !== '/auth/session') writes.push({ path: url.pathname, query: url.search, payload: request.postDataJSON() });
    if (url.pathname === '/config/administrator-groups') {
      if (request.method() === 'GET') return route.fulfill({ json: snapshot, headers: cors });
      const payload = request.postDataJSON();
      if (conflict || payload.expected_revision !== snapshot.revision) return route.fulfill({ status: 409, json: { error: '配置已被其他操作修改' }, headers: cors });
      snapshot.groups = payload.groups.map(item => ({ ...item, members: item.members.map(member => ({ ...member, platform_instance_id: 'new-instance' })) }));
      snapshot.revision = `v${++revision}`;
      snapshot.section_revision = `s${revision}`;
      snapshot.invalid_members = [];
      snapshot.runtime = { status: runtimeStatus };
      return route.fulfill({ json: snapshot, headers: cors });
    }
    if (url.pathname === '/config/administrator-groups/preview') {
      const groups = request.postDataJSON().groups;
      return route.fulfill({ json: { members: groups.flatMap(item => item.members.map(member => ({ ...member, plugins: [{ name: 'group_admin', permissions: [{ id: 'write', description: '请求群管理修改操作' }] }] }))) }, headers: cors });
    }
    if (url.pathname === '/config/administrator-groups/apply') {
      assert.equal(request.postDataJSON().section_revision, snapshot.section_revision);
      snapshot.runtime = { status: 'applied' };
      return route.fulfill({ json: { runtime: snapshot.runtime }, headers: cors });
    }
    if (url.pathname === '/config') return request.method() === 'GET'
      ? route.fulfill({ json: { ok: true, exists: true, revision: 'stale-general', config: { api: { host: '127.0.0.1', port: 19870 }, administrator_groups: [group] } }, headers: cors })
      : route.fulfill({ status: 409, json: { error: '配置已被其他操作修改' }, headers: cors });
    return route.fulfill({ json: url.pathname === '/status' || url.pathname === '/api/health' ? { running: true, runtime_id: 'test', adapters: {} } : {}, headers: cors });
  });
  await page.goto(`${origin}/settings`);
  await page.getByRole('button', { name: '管理员', exact: true }).click();
  const panel = page.locator('[aria-label="管理员设置"]');
  const name = panel.getByLabel('管理组名称');
  const save = panel.getByRole('button', { name: '保存并应用', exact: true });
  await name.waitFor();
  assert.equal(await name.inputValue(), '主要管理员');
  await panel.getByText('原平台已移除或重建, 此身份授权已失效', { exact: false }).waitFor();
  await panel.getByText('ordinary_plugin · 未接入', { exact: true }).waitFor();
  await panel.getByText('需开启管理写操作；需要审批的动作仍须批准', { exact: true }).waitFor();
  await panel.getByText('temporarily_missing · 暂不可用', { exact: true }).waitFor();
  await panel.getByText('invalid_plugin: 权限声明引用不存在的工具', { exact: true }).waitFor();
  await panel.getByRole('button', { name: '绑定到当前平台', exact: true }).click();
  await name.fill('修改后的管理员');
  await panel.getByRole('button', { name: '预览有效权限', exact: true }).click();
  await panel.locator('[aria-label="有效权限预览"]').getByText('future / user@example.test', { exact: true }).waitFor();
  conflict = true;
  await save.click();
  await panel.getByRole('alert').filter({ hasText: '配置已被其他操作修改' }).waitFor();
  assert.equal(await name.inputValue(), '修改后的管理员');
  await page.getByRole('button', { name: '关于', exact: true }).click();
  await page.getByRole('button', { name: '管理员', exact: true }).click();
  assert.equal(await name.inputValue(), '修改后的管理员');
  await panel.getByRole('button', { name: '保留草稿并读取最新版本', exact: true }).click();
  await panel.getByText('已读取最新版本, 以下显示已保存的配置。编辑区仍是你的草稿, 请比较后再保存。', { exact: true }).waitFor();
  await panel.locator('details').getByText('主要管理员 · 启用', { exact: true }).waitFor();
  await save.waitFor();
  conflict = false;
  await save.click();
  await panel.getByText('已保存, 无法确认运行时生效', { exact: true }).waitFor();
  const last = writes.filter(item => item.path === '/config/administrator-groups').at(-1);
  assert.equal(last.payload.groups[0].name, '修改后的管理员');
  assert.deepEqual(last.payload.rebind_members, [{ group_id: 'existing', platform_id: 'future', user_id: 'user@example.test' }]);
  await panel.getByRole('button', { name: '重试应用已保存配置', exact: true }).click();
  await panel.getByText('已保存并生效', { exact: true }).waitFor();
  await panel.getByRole('button', { name: '新增管理组', exact: true }).click();
  const newGroup = panel.locator('fieldset').last();
  await newGroup.getByLabel('管理组名称').fill('第二管理组');
  await newGroup.getByRole('button', { name: '添加管理员成员', exact: true }).click();
  await newGroup.getByLabel('管理员平台').selectOption('future');
  await newGroup.getByLabel('平台用户识别号').fill('another@example.test');
  await newGroup.getByLabel('适用插件', { exact: true }).selectOption('all');
  await newGroup.getByText('group_admin', { exact: true }).locator('..').getByLabel('排除').check();
  runtimeStatus = 'next_start';
  await save.click();
  await panel.getByText('已保存, 后端启动后生效', { exact: true }).waitFor();
  assert.equal(snapshot.groups[1].plugin_scope.mode, 'all');
  assert.deepEqual(snapshot.groups[1].plugin_scope.excluded, ['group_admin']);
  assert.equal(snapshot.groups[1].members[0].user_id, 'another@example.test');
  assert.equal(writes.some(item => item.path === '/restart'), false);
  assert.deepEqual(errors, []);
  await page.mouse.move(2, 2);
  await mkdir(path.join(root, 'test-results', 'administrators'), { recursive: true });
  await page.screenshot({ path: path.join(root, 'test-results', 'administrators', 'settings.png'), fullPage: true });
  await panel.screenshot({ path: path.join(root, 'test-results', 'administrators', 'panel.png') });
  await page.getByRole('button', { name: '常用配置', exact: true }).click();
  const generalSave = page.waitForResponse(response => new URL(response.url()).pathname === '/config' && response.request().method() === 'PUT');
  await page.getByRole('button', { name: '保存配置', exact: true }).click();
  assert.equal((await generalSave).status(), 409);
  assert.equal(writes.filter(item => item.path === '/config').at(-1).query, '?expected_revision=stale-general');
  assert.equal(writes.some(item => item.path === '/restart'), false);
  console.log('Administrator settings: dynamic catalogs, invalid declarations, stale identity rebind, preview, draft preservation, conflict, hot save and runtime confirmation passed');
} finally {
  if (browser) await browser.close();
  await server.close();
}
