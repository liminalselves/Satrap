import assert from "node:assert/strict";
import path from "node:path";
import { fileURLToPath } from "node:url";
import fs, { mkdir } from "node:fs/promises";
import { runSuite, mockConfigVersions } from './runtime.mjs';
import { pathToFileURL } from 'node:url';

async function administrators(runtime) {







  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  const server = await runtime.vite({ root, server: { host: '127.0.0.1', port: 0 } });
  let browser;
  try {
    await runtime.listen(server);
    const origin = server.resolvedUrls.local[0].replace(/\/$/, '');
    browser = await runtime.browser;
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    page.setDefaultTimeout(10000);
    const errors = [];
    const writes = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.routeWebSocket('ws://127.0.0.1:19870/ws/status**', () => {});
    let conflict = false;
    let runtimeStatus = 'unconfirmed';
    let releaseRuntime;
    let pendingRuntime = new Promise(resolve => { releaseRuntime = resolve; });
    let holdRuntime = true;
    let revision = 1;
    const group = { id: 'existing', name: '主要管理员', enabled: true, protect: false,
      members: [{ platform_id: 'future', platform_instance_id: 'old-instance', user_id: 'user@example.test' }],
      plugin_scope: { mode: 'selected', included: ['group_admin', 'temporarily_missing', 'disabled_plugin', 'other_platform_plugin'], excluded: [] } };
    const override = { id: 'intern', enabled: true, protect: false, platform_id: 'future',
      platform_instance_id: 'old-instance', user_id: 'intern@example.test', allow: [], deny: ['group_admin'] };
    const snapshot = { ok: true, groups: [group], overrides: [override], revision: 'v1', section_revision: 's1',
      migrated_from_legacy: true,
      platforms: [{ id: 'future', type: 'future-adapter', name: '扩展平台', instance_id: 'new-instance', enabled: true }],
      plugins: [{ name: 'group_admin', description: '管理', supports_administrators: true, enabled: true, loaded_platforms: ['future'], management_permissions: { write: { description: '请求群管理修改操作', system_admin: true, requirements: ['需开启管理写操作', '需要审批的动作仍须批准'] } } },
        { name: 'ordinary_plugin', description: '普通插件', supports_administrators: false, enabled: true, loaded_platforms: ['future'], management_permissions: {} },
        { name: 'disabled_plugin', description: '未启用插件', supports_administrators: true, enabled: false, loaded_platforms: ['future'], management_permissions: {} },
        { name: 'other_platform_plugin', description: '其他平台插件', supports_administrators: true, enabled: true, loaded_platforms: ['elsewhere'], management_permissions: {} }],
      plugin_errors: [{ name: 'invalid_plugin', error: '权限声明引用不存在的工具' }],
      invalid_members: [{ group_id: 'existing', platform_id: 'future', user_id: 'user@example.test' }],
      invalid_overrides: [{ override_id: 'intern', platform_id: 'future', user_id: 'intern@example.test' }],
      migration_pending: ['legacy-pending'],
      runtime: { status: 'unconfirmed', error: '尚未同步' } };
    const cors = { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true', 'Access-Control-Allow-Headers': 'Content-Type', 'Access-Control-Allow-Methods': 'GET,POST,PUT,OPTIONS' };
    await page.route('**/*', async route => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.origin === origin && url.pathname !== '/ui-config.json') return route.continue();
      if (url.pathname === '/ui-config.json') return route.fulfill({ json: { backend_api: 'http://127.0.0.1:19870', control_api: 'http://127.0.0.1:19871', chat_api: 'http://127.0.0.1:19872' } });
      if (request.method() === 'OPTIONS') return route.fulfill({ status: 204, headers: cors });
      if (request.method() !== 'GET' && url.pathname !== '/auth/session') writes.push({ path: url.pathname, query: url.search, payload: request.postDataJSON() });
      if (url.pathname === '/config/administrator-groups/runtime') {
        const confirmed = { section_revision: snapshot.section_revision, runtime: { status: 'applied' } };
        if (holdRuntime) { holdRuntime = false; await pendingRuntime; }
        return route.fulfill({ json: confirmed, headers: cors });
      }
      if (url.pathname === '/config/administrator-groups' || url.pathname === '/config/administrator-groups/snapshot') {
        if (request.method() === 'GET') return route.fulfill({ json: url.pathname.endsWith('/snapshot')
          ? { ...snapshot, runtime: { status: 'unconfirmed', error: '尚未确认运行状态' } } : snapshot, headers: cors });
        const payload = request.postDataJSON();
        if (conflict || payload.expected_revision !== snapshot.revision) return route.fulfill({ status: 409, json: { error: '配置已被其他操作修改' }, headers: cors });
        snapshot.groups = payload.groups.map(item => ({ ...item, members: item.members.map(member => ({ ...member, platform_instance_id: 'new-instance' })) }));
        snapshot.overrides = payload.overrides.map(item => ({ ...item, platform_instance_id: 'new-instance' }));
        snapshot.revision = `v${++revision}`;
        snapshot.section_revision = `s${revision}`;
        snapshot.invalid_members = [];
        snapshot.invalid_overrides = [];
        snapshot.migrated_from_legacy = false;
        snapshot.migration_pending = [];
        snapshot.runtime = { status: runtimeStatus };
        return route.fulfill({ json: snapshot, headers: cors });
      }
      if (url.pathname === '/config/administrator-groups/preview') {
        const { groups, overrides } = request.postDataJSON();
        const permissions = [{ id: 'write', description: '请求群管理修改操作' }];
        const rows = new Map();
        const rowFor = (platform_id, user_id) => {
          const key = `${platform_id}/${user_id}`;
          if (!rows.has(key)) rows.set(key, { platform_id, user_id, groupIds: [], override: null });
          return rows.get(key);
        };
        for (const item of groups) for (const member of item.members) rowFor(member.platform_id, member.user_id).groupIds.push(item.id);
        for (const entry of overrides) rowFor(entry.platform_id, entry.user_id).override = entry;
        // 与后端同口径: 任一来源非空就收录, allowed 由两侧来源决定
        const members = [...rows.values()].map(row => {
          const allow = row.groupIds.map(id => `group:${id}`);
          const deny = [];
          if (row.override?.enabled) {
            if (row.override.allow.includes('group_admin')) allow.push(`override:${row.override.id}`);
            if (row.override.deny.includes('group_admin')) deny.push(`override:${row.override.id}`);
          }
          const plugins = allow.length || deny.length
            ? [{ name: 'group_admin', allowed: allow.length > 0 && deny.length === 0, group_ids: row.groupIds, permissions, sources: { allow, deny } }]
            : [];
          return { platform_id: row.platform_id, user_id: row.user_id, override_id: row.override?.id || '', plugins };
        });
        return route.fulfill({ json: { members, groups, overrides }, headers: cors });
      }
      if (url.pathname === '/config/administrator-groups/apply') {
        assert.equal(request.postDataJSON().section_revision, snapshot.section_revision);
        snapshot.runtime = { status: 'applied' };
        return route.fulfill({ json: { runtime: snapshot.runtime }, headers: cors });
      }
      if (url.pathname === '/config') return request.method() === 'GET'
        ? route.fulfill({ json: { ok: true, exists: true, revision: 'stale-general', config: { api: { host: '127.0.0.1', port: 19870 }, administrator_groups: [group], administrator_overrides: [override] } }, headers: cors })
        : route.fulfill({ status: 409, json: { error: '配置已被其他操作修改' }, headers: cors });
      return route.fulfill({ json: url.pathname === '/status' || url.pathname === '/api/health' ? { running: true, runtime_id: 'test', adapters: {} } : {}, headers: cors });
    });
    await mockConfigVersions(page, origin, () => JSON.stringify({revision}));
  await page.goto(`${origin}/settings`);
    await page.getByRole('button', { name: '管理员', exact: true }).click();
    const panel = page.locator('[aria-label="管理员设置"]');
    const save = panel.getByRole('button', { name: '保存并应用', exact: true });
    // 卡片网格只显示摘要 (含失效身份标记), 详情在弹窗中编辑
    const groupCard = panel.locator('[aria-label^="管理组卡片"]').first();
    const overrideCard = panel.locator('[aria-label^="成员例外卡片"]').first();
    await groupCard.getByText('成员 1 人 · 指定插件 4 个 · 有失效身份', { exact: false }).waitFor();
    await overrideCard.getByText('允许 0 个 · 否决 1 个 · 身份已失效', { exact: false }).waitFor();
    assert.equal(await page.getByLabel('管理组名称').count(), 0);
    await groupCard.getByRole('button', { name: '配置 ▸', exact: false }).click();
    const dialog = page.getByRole('dialog');
    const name = dialog.getByLabel('管理组名称');
    await name.waitFor();
    assert.equal(await name.inputValue(), '主要管理员');
    await panel.getByText('正在确认管理员运行状态…', { exact: true }).waitFor();
    await name.fill('运行状态等待中的草稿');
    releaseRuntime();
    await panel.getByText('正在确认管理员运行状态…', { exact: true }).waitFor({ state: 'hidden' });
    assert.equal(await name.inputValue(), '运行状态等待中的草稿');
    await name.fill('主要管理员');
    await dialog.getByText('原平台已移除或重建, 此身份授权已失效', { exact: false }).waitFor();
    // 未接入与未在此平台加载的插件都不出现在选择器中 (option 对 Playwright 恒为 hidden)
    const allowPicker = dialog.getByLabel('添加允许的插件', { exact: true });
    assert.equal(await allowPicker.locator('option', { hasText: 'ordinary_plugin' }).count(), 0);
    assert.equal(await allowPicker.locator('option', { hasText: 'other_platform_plugin' }).count(), 0);
    await dialog.getByText('需开启管理写操作；需要审批的动作仍须批准', { exact: true }).waitFor();
    await dialog.getByText('temporarily_missing · 暂不可用', { exact: true }).waitFor();
    // 未启用插件不出现在选择器中; 已在名单中的保留显示并带"未启用"徽章, 仍可移除
    assert.equal(await dialog.getByLabel('添加允许的插件', { exact: true }).locator('option', { hasText: 'disabled_plugin' }).count(), 0);
    await dialog.getByText('disabled_plugin · 未启用', { exact: true }).waitFor();
    // 已在名单中的平台不适用条目同样保留显示并带徽章
    await dialog.getByText('other_platform_plugin · 平台不适用', { exact: true }).waitFor();
    await panel.getByText('invalid_plugin: 权限声明引用不存在的工具', { exact: true }).waitFor();
    // 迁移提示与例外层初始态 (横幅在面板层, 弹窗不影响)
    await panel.getByText('已将旧版“指定插件”模式下的组排除迁移为成员例外, 保存后生效。', { exact: true }).waitFor();
    await panel.getByText('迁移按当前成员展开, 之后新加入该组的成员不会自动继承这条否决。', { exact: true }).waitFor();
    // 迁移冲突提示与"旧排除未迁移仍生效"说明
    await panel.getByText('1 个组的旧排除与现有例外的启用状态冲突, 未自动迁移; 请调整对应例外条目的启用状态后保存。', { exact: true }).waitFor();
    await panel.getByText('涉及管理组: legacy-pending。未迁移期间这些旧排除仍按原规则生效, 可原样保留。', { exact: true }).waitFor();
    // 组在指定插件模式下只有"已允许"名单, 排除只存在于成员例外
    assert.equal(await dialog.locator('[aria-label="已排除的插件"]').count(), 0);
    assert.equal(await dialog.locator('[aria-label="已允许的插件"]').getByText('group_admin', { exact: true }).count(), 1);
    assert.equal(await dialog.getByText('需要否决某人时使用成员例外。', { exact: false }).count(), 1);
    await dialog.getByRole('button', { name: '绑定到当前平台', exact: true }).click();
    await name.fill('修改后的管理员');
    await page.keyboard.press('Escape');
    await dialog.waitFor({ state: 'hidden' });
    // 例外条目同样需要显式重绑, 否则保存时不会恢复其绑定
    await overrideCard.getByRole('button', { name: '配置 ▸', exact: false }).click();
    await dialog.waitFor();
    await dialog.getByText('原平台已移除或重建, 此例外已失效', { exact: false }).waitFor();
    await dialog.getByRole('button', { name: '绑定到当前平台', exact: true }).click();
    // 成员例外的双名单: 从否决侧移除, 加入允许侧, 再加入否决侧时自动移出允许侧 (以移除按钮判定名单成员, 选择器 option 含同名文本)
    const allowList = dialog.locator('[aria-label="额外允许的插件"]');
    const denyList = dialog.locator('[aria-label="否决的插件"]');
    await denyList.getByRole('button', { name: '移除 group_admin', exact: true }).waitFor();
    assert.equal(await allowList.getByRole('button', { name: '移除 group_admin', exact: true }).count(), 0);
    await denyList.getByRole('button', { name: '移除 group_admin', exact: true }).click();
    assert.equal(await denyList.getByRole('button', { name: '移除 group_admin', exact: true }).count(), 0);
    await allowList.getByLabel('添加允许的插件', { exact: true }).selectOption('group_admin');
    await allowList.getByRole('button', { name: '移除 group_admin', exact: true }).waitFor();
    await denyList.getByLabel('添加否决的插件', { exact: true }).selectOption('group_admin');
    await denyList.getByRole('button', { name: '移除 group_admin', exact: true }).waitFor();
    assert.equal(await allowList.getByRole('button', { name: '移除 group_admin', exact: true }).count(), 0);
    await page.keyboard.press('Escape');
    await dialog.waitFor({ state: 'hidden' });
    await panel.getByRole('button', { name: '预览有效权限', exact: true }).click();
    await panel.locator('[aria-label="有效权限预览"]').getByText('future / user@example.test', { exact: true }).waitFor();
    assert.equal(await panel.locator('[aria-label="有效权限预览"]').getByText('允许来源: group:existing', { exact: false }).count(), 1);
    // 组允许 + 例外否决 → 预览展示 allowed=false 的无效项与双侧来源
    const previewPane = panel.locator('[aria-label="有效权限预览"]');
    assert.equal(await previewPane.getByText('无效', { exact: true }).count(), 1);
    assert.equal(await previewPane.getByText('否决来源: override:intern', { exact: false }).count(), 1);
    conflict = true;
    await save.click();
    await panel.getByRole('alert').filter({ hasText: '配置已被其他操作修改' }).waitFor();
    // 草稿保留在卡片与弹窗状态中, 切换页签与重新打开弹窗都不丢失
    await groupCard.getByRole('button', { name: '配置 ▸', exact: false }).click();
    await dialog.waitFor();
    assert.equal(await name.inputValue(), '修改后的管理员');
    await page.keyboard.press('Escape');
    await dialog.waitFor({ state: 'hidden' });
    await page.getByRole('button', { name: '关于', exact: true }).click();
    await page.getByRole('button', { name: '管理员', exact: true }).click();
    await groupCard.getByRole('button', { name: '配置 ▸', exact: false }).click();
    await dialog.waitFor();
    assert.equal(await name.inputValue(), '修改后的管理员');
    await page.keyboard.press('Escape');
    await dialog.waitFor({ state: 'hidden' });
    await panel.getByRole('button', { name: '保留草稿并读取最新版本', exact: true }).click();
    await panel.getByText('已读取最新版本, 以下显示已保存的配置。编辑区仍是你的草稿, 请比较后再保存。', { exact: true }).waitFor();
    await panel.locator('details').getByText('主要管理员 · 启用 · 不保护账号', { exact: true }).waitFor();
    await save.waitFor();
    conflict = false;
    await save.click();
    await panel.getByText('已保存, 无法确认运行时生效', { exact: true }).waitFor();
    const last = writes.filter(item => item.path === '/config/administrator-groups').at(-1);
    assert.equal(last.payload.groups[0].name, '修改后的管理员');
    assert.deepEqual(last.payload.rebind_members, [{ group_id: 'existing', platform_id: 'future', user_id: 'user@example.test' }]);
    assert.deepEqual(last.payload.rebind_overrides, [{ override_id: 'intern', platform_id: 'future', user_id: 'intern@example.test' }]);
    assert.equal(last.payload.overrides[0].user_id, 'intern@example.test');
    await panel.getByRole('button', { name: '重试应用已保存配置', exact: true }).click();
    await panel.getByText('已保存并生效', { exact: true }).waitFor();
    // 保存后迁移提示消失, 账号保护开关与例外层落盘
    assert.equal(await panel.getByText('已将旧版“指定插件”模式下的组排除迁移为成员例外, 保存后生效。', { exact: true }).count(), 0);
    // 新建例外直接打开弹窗
    await panel.getByRole('button', { name: '新增成员例外', exact: true }).click();
    await dialog.waitFor();
    await dialog.getByLabel('例外平台').selectOption('future');
    await dialog.getByLabel('平台用户识别号').fill('another@example.test');
    await dialog.locator('[aria-label="额外允许的插件"]').getByLabel('添加允许的插件', { exact: true }).selectOption('group_admin');
    await page.keyboard.press('Escape');
    await dialog.waitFor({ state: 'hidden' });
    // 账号保护开关在组的弹窗里
    await groupCard.getByRole('button', { name: '配置 ▸', exact: false }).click();
    await dialog.waitFor();
    await dialog.getByRole('switch').click();
    await page.keyboard.press('Escape');
    await dialog.waitFor({ state: 'hidden' });
    await panel.getByRole('button', { name: '新增管理组', exact: true }).click();
    await dialog.waitFor();
    await dialog.getByLabel('管理组名称').fill('第二管理组');
    await dialog.getByRole('button', { name: '添加管理员成员', exact: true }).click();
    await dialog.getByLabel('管理员平台').selectOption('future');
    await dialog.getByLabel('平台用户识别号').fill('another@example.test');
    await dialog.getByLabel('适用插件', { exact: true }).selectOption('all');
    await dialog.locator('[aria-label="已排除的插件"]').getByLabel('添加排除的插件', { exact: true }).selectOption('group_admin');
    await page.keyboard.press('Escape');
    await dialog.waitFor({ state: 'hidden' });
    runtimeStatus = 'next_start';
    await save.click();
    await panel.getByText('已保存, 后端启动后生效', { exact: true }).waitFor();
    assert.equal(snapshot.groups[0].protect, true);   // 保护开关已打开
    assert.equal(snapshot.groups[1].plugin_scope.mode, 'all');
    assert.deepEqual(snapshot.groups[1].plugin_scope.excluded, ['group_admin']);
    assert.deepEqual(snapshot.groups[0].plugin_scope.excluded, []);
    assert.deepEqual([...snapshot.groups[0].plugin_scope.included].sort(), ['disabled_plugin', 'group_admin', 'other_platform_plugin', 'temporarily_missing']);
    assert.equal(snapshot.groups[1].members[0].user_id, 'another@example.test');
    assert.equal(snapshot.overrides.length, 2);
    assert.deepEqual(snapshot.overrides[0].deny, ['group_admin']);
    assert.deepEqual(snapshot.overrides[1].allow, ['group_admin']);
    assert.equal(snapshot.overrides[1].user_id, 'another@example.test');
    pendingRuntime = new Promise(resolve => { releaseRuntime = resolve; });
    holdRuntime = true;
    await panel.getByRole('button', { name: '刷新目录与状态', exact: true }).click();
    await panel.getByText('正在确认管理员运行状态…', { exact: true }).waitFor();
    await groupCard.getByRole('button', { name: '配置 ▸', exact: false }).click();
    await dialog.getByLabel('管理组名称').fill('保存后不能被旧状态覆盖');
    await page.keyboard.press('Escape');
    await save.click();
    await panel.getByText('已保存, 后端启动后生效', { exact: true }).waitFor();
    releaseRuntime();
    await page.waitForTimeout(100);
    assert.equal(await panel.getByText('已保存, 后端启动后生效', { exact: true }).count(), 1);
    await groupCard.getByRole('button', { name: '配置 ▸', exact: false }).click();
    assert.equal(await dialog.getByLabel('管理组名称').inputValue(), '保存后不能被旧状态覆盖');
    await page.keyboard.press('Escape');
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
    console.log('Administrator settings: dynamic catalogs, invalid declarations, stale rebind, migration notice, exception layer, protect switches, preview sources, draft preservation, conflict, hot save and runtime confirmation passed');
  } finally {
    if (browser) await runtime.closeContexts();
    await runtime.releaseServer(server);
  }

}

async function agent_routing(runtime) {
  /** Agent 路由浏览器回归, 覆盖冷目录, 额外类型和草稿保留 */







  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  const artifacts = path.join(root, 'test-results', 'agent-routing');
  const server = await runtime.vite({ root, server: { host: '127.0.0.1', port: 0 } });
  let browser;
  let page;
  try {
    await fs.mkdir(artifacts, { recursive: true });
    await runtime.listen(server);
    const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
    browser = await runtime.browser;
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    let revision = 1;
    let running = false;
    let applicationStatus = 'failed';
    let applications = [];
    let platforms = [{ id: 'future-bot', type: 'future', settings: { extension: 'kept' } }];
    const writes = [];
    const requests = [];
    const edictum = Object.fromEntries(['personal', 'group-agent', 'disabled'].map((name) => [name, {
      provider: 'edictum', edictum_type: 'simple', enabled: name !== 'disabled', model_name: 'chat', description: '', params: {},
      plugins: [{ name: 'example', enabled: true }],
    }]));
    const adapterTypes = [
      { type: 'future', status: 'available', display_name: '未来平台', conversation_kinds: { private: '私聊', group: '群聊', topic: '话题' } },
      { type: 'other-future', status: 'available', display_name: '另一平台', conversation_kinds: { board: '看板' } },
    ];
    await context.route(`${origin}/ui-config.json`, (route) => route.fulfill({ json: {
      backend_api: 'http://127.0.0.1:19870', control_api: 'http://127.0.0.1:19871', chat_api: 'http://127.0.0.1:19872',
    } }));
    await context.route(/^http:\/\/127\.0\.0\.1:1987[012]\//, async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      const headers = { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true', 'Access-Control-Allow-Headers': '*', 'Access-Control-Allow-Methods': '*' };
      if (request.method() === 'OPTIONS') return route.fulfill({ status: 204, headers });
      requests.push(url.pathname);
      if (url.pathname.startsWith('/config/platforms')) {
        if (request.method() === 'PUT') {
          if (url.searchParams.get('expected_revision') !== String(revision)) return route.fulfill({ status: 409, headers, json: { ok: false, code: 'config_revision_conflict' } });
          const payload = request.postDataJSON();
          writes.push(payload);
          platforms = [payload];
          revision++;
        }
        return route.fulfill({ headers, json: { ok: true, platforms, revision: String(revision), adapter_types: adapterTypes, default_session_type: 'flow' } });
      }
      if (url.pathname === '/api/config/reload') {
        assert.equal(request.postDataJSON().expected_config_revision, String(revision));
        applications = [{ id: 'future-bot', status: applicationStatus, saved_revision: String(revision), active_revision: applicationStatus === 'applied' ? String(revision) : 'old', error: applicationStatus === 'failed' ? '模拟应用失败' : undefined }];
        return route.fulfill({ headers, json: { ok: true, platforms: applications } });
      }
      const responses = {
        '/auth/session': { ok: true }, '/status': { running },
        '/api/health': { running, adapters: {}, platform_config: applications },
        '/config/session-classes': { flow: { class_path: 'example.Flow', enabled: true, model_key: 'model_name', params: { model_name: 'flow-model' } } },
        '/config/edictum/sessions': edictum,
        '/api/sessions': { sessions: [] }, '/config/session-instances': { sessions: [] },
        '/api/diagnostics': { records: [], available: false }, '/config/models/asr': {},
      };
      return route.fulfill({ headers, json: responses[url.pathname] || { ok: true } });
    });
    page = await context.newPage();
    const errors = [];
    page.on('pageerror', (error) => errors.push(error.message));
    await page.routeWebSocket('ws://127.0.0.1:19870/ws/status**', () => {});
    await page.routeWebSocket('ws://127.0.0.1:19872/ws/chat**', () => {});
    await mockConfigVersions(page, origin, () => JSON.stringify({revision, platforms, edictum}));
  await page.goto(`${origin}/platforms`);
    await page.getByTitle('编辑', { exact: true }).click();
    const dialog = page.getByRole('dialog');
    const value = (provider, name) => JSON.stringify([provider, name]);
    const privateMode = dialog.getByLabel('私聊 Agent 来源', { exact: true });
    const groupMode = dialog.getByLabel('群聊 Agent 来源', { exact: true });
    const topicMode = dialog.getByLabel('话题 Agent 来源', { exact: true });
    const refresh = async () => Promise.all([
      page.waitForResponse((response) => new URL(response.url()).pathname === '/config/platforms'),
      page.waitForResponse((response) => new URL(response.url()).pathname === '/api/health'),
      page.getByRole('button', { name: '刷新', exact: true }).click(),
    ]);
    assert.equal(await topicMode.inputValue(), 'inherit');
    await dialog.getByText('实际配置: SessionClass / flow', { exact: true }).first().waitFor();

    // 只保存其他字段不能自动启用类型路由
    await dialog.getByRole('button', { name: '保存修改' }).click();
    await dialog.waitFor({ state: 'hidden' });
    assert.equal('session_bindings' in writes[0], false);
    assert.equal(requests.includes('/api/config/reload'), false);
    await page.getByTitle('编辑', { exact: true }).click();
    await privateMode.selectOption('value');
    await dialog.getByRole('button', { name: '保存修改' }).click();
    await page.getByText('私聊 Agent 必须选择命名配置', { exact: true }).waitFor();
    assert.equal(writes.length, 1);
    await dialog.getByLabel('私聊 Agent 配置', { exact: true }).selectOption(value('edictum', 'personal'));
    await groupMode.selectOption('value');
    const groupSelect = dialog.getByLabel('群聊 Agent 配置', { exact: true });
    assert.equal(await groupSelect.locator('option').filter({ hasText: 'disabled' }).evaluate((option) => option.disabled), true);
    await groupSelect.selectOption(value('edictum', 'group-agent'));
    await topicMode.selectOption('value');
    await dialog.getByLabel('话题 Agent 配置', { exact: true }).selectOption(value('session_class', 'flow'));
    await dialog.getByText('模型: flow-model · 插件: 由流程决定', { exact: true }).waitFor();
    await dialog.getByText('模型: chat · 插件: 1', { exact: true }).first().waitFor();
    await page.screenshot({ animations: 'disabled', path: path.join(artifacts, 'desktop.png') });
    await dialog.getByRole('button', { name: '保存修改' }).click();
    await dialog.waitFor({ state: 'hidden' });
    assert.deepEqual(writes.at(-1).session_bindings, {
      private: { mode: 'value', provider: 'edictum', config_name: 'personal' },
      group: { mode: 'value', provider: 'edictum', config_name: 'group-agent' },
      topic: { mode: 'value', provider: 'session_class', config_name: 'flow' },
    });
    assert.equal(writes.at(-1).settings.extension, 'kept');

    // 切换平台类型保留原路由, 并明确要求处理不再支持的类型
    await page.getByTitle('编辑', { exact: true }).click();
    await dialog.getByLabel('类型', { exact: true }).selectOption('other-future');
    await dialog.getByLabel('看板 Agent 来源', { exact: true }).waitFor();
    assert.equal(await dialog.getByLabel('private Agent 配置', { exact: true }).inputValue(), value('edictum', 'personal'));
    await dialog.getByRole('button', { name: '保存修改' }).click();
    await page.getByText('private 已不受当前适配器支持, 请删除该绑定', { exact: true }).waitFor();
    await dialog.getByLabel('类型', { exact: true }).selectOption('future');

    // 并发冲突保留恢复继承后的草稿, 取消关闭也保留
    await groupMode.selectOption('inherit');
    revision++;
    await dialog.getByRole('button', { name: '保存修改' }).click();
    await page.getByText('保存失败: 配置已被其他操作修改, 当前草稿已保留; 请复制草稿并刷新后合并', { exact: true }).waitFor();
    assert.equal(await groupMode.inputValue(), 'inherit');
    page.once('dialog', (event) => event.dismiss());
    await dialog.getByRole('button', { name: '取消', exact: true }).click();
    assert.equal(await dialog.isVisible(), true);
    page.once('dialog', (event) => event.accept());
    await dialog.getByRole('button', { name: '取消', exact: true }).click();
    await dialog.waitFor({ state: 'hidden' });
    running = true;
    await refresh();
    await page.getByTitle('编辑', { exact: true }).click();
    await groupMode.selectOption('inherit');
    await dialog.getByRole('button', { name: '保存修改' }).click();
    await dialog.waitFor({ state: 'hidden' });
    assert.deepEqual(writes.at(-1).session_bindings.group, { mode: 'inherit' });
    await page.getByText('应用失败', { exact: true }).waitFor();
    applicationStatus = 'applied';
    await page.getByRole('button', { name: '重试应用 future-bot', exact: true }).click();
    await page.getByText('已生效', { exact: true }).waitFor();

    // 删除或停用的配置保留可见值, 禁止以空白或另一个配置替代
    platforms[0].session_bindings.private = { mode: 'value', provider: 'edictum', config_name: 'gone' };
    platforms[0].session_bindings.topic = { mode: 'value', provider: 'edictum', config_name: 'disabled' };
    revision++;
    await refresh();
    await page.getByTitle('编辑', { exact: true }).click();
    assert.equal(await dialog.getByLabel('私聊 Agent 配置', { exact: true }).inputValue(), value('edictum', 'gone'));
    await dialog.getByRole('button', { name: '保存修改' }).click();
    await page.getByText('私聊 Agent 配置已删除, 请重新选择', { exact: true }).waitFor();
    await privateMode.selectOption('inherit');
    await dialog.getByRole('button', { name: '保存修改' }).click();
    await page.getByText('话题 Agent 配置已停用, 请重新选择', { exact: true }).waitFor();
    await page.setViewportSize({ width: 390, height: 844 });
    await dialog.getByTestId('agent-routing').scrollIntoViewIfNeeded();
    const bounds = await dialog.boundingBox();
    assert.ok(bounds.x >= 0 && bounds.x + bounds.width <= 390);
    await page.screenshot({ animations: 'disabled', path: path.join(artifacts, 'mobile.png') });
    assert.deepEqual(errors, []);
    console.log('Agent routing e2e passed: cold metadata, future kinds, save/inherit, missing/disabled, drafts and retry');
  } catch (error) {
    if (page) {
      await page.screenshot({ animations: 'disabled', path: path.join(artifacts, 'failure.png') });
      await fs.writeFile(path.join(artifacts, 'failure.txt'), await page.locator('body').innerText(), 'utf8');
    }
    throw error;
  } finally {
    if (browser) await runtime.closeContexts();
    await runtime.releaseServer(server);
  }

}

async function backend_platform_status(runtime) {







  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  const artifacts = path.join(root, 'test-results', 'backend-platform-status');
  const server = await runtime.vite({ root, server: { host: '127.0.0.1', port: 0 } });
  let browser;
  try {
    await fs.mkdir(artifacts, { recursive: true });
    await runtime.listen(server);
    const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
    browser = await runtime.browser;
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', (error) => { errors.push(error.message); console.error('pageerror:', error.stack); });
    const state = { running: true, runtime: 'r1', account: '10', reloads: 0, writes: 0,
      applyFailure: false, saveFailure: false, probeFailure: false, sourcesDown: false, staleHealth: false };
    const sessions = { demo: { enabled: true, class_path: 'demo.Demo', is_async: true, params: {} } };
    const edictum = { named: { enabled: true, edictum_type: 'simple', model_name: 'm', plugins: [], params: {} } };
    const platforms = [
      { id: 'bot', type: 'onebot', enable: true, session_provider: 'session_class', session_type: 'demo', settings: {} },
      { id: 'misskey', type: 'misskey', enable: true, session_provider: 'edictum', session_type: 'named', settings: {} },
    ];
    let socket;
    let socketCount = 0;
    let stopRelease;
    let probeRelease;
    let holdStop = false;
    let holdProbe = false;
    let probeCalls = 0;
    const adapters = () => Object.fromEntries(platforms.map((item) => [item.id, {
      status: 'running', started: true, config_type: item.type, session_type: item.session_type,
      session_provider: item.session_provider, client_self_id: item.id === 'bot' ? state.account : '',
    }]));
    const health = () => ({ running: state.running || state.staleHealth, runtime_id: state.runtime,
      adapters: state.running || state.staleHealth ? adapters() : {}, platform_config: platforms.map((item) => ({
        id: item.id, status: state.applyFailure ? 'failed' : 'applied', saved_revision: 'v1', active_revision: 'v1',
        error: state.applyFailure ? '模拟应用失败' : undefined,
      })) });
    await page.routeWebSocket('ws://127.0.0.1:19870/ws/status**', (connection) => { socket = connection; ++socketCount; });
    await context.route('**/*', async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.origin === origin && url.pathname !== '/ui-config.json') return route.continue();
      const headers = { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true',
        'Access-Control-Allow-Headers': 'Content-Type', 'Access-Control-Allow-Methods': 'GET,POST,PATCH,OPTIONS' };
      const reply = (json, status = 200) => route.fulfill({ status, json, headers });
      if (request.method() === 'OPTIONS') return route.fulfill({ status: 204, headers });
      const pathname = url.pathname;
      if (pathname === '/ui-config.json') return reply({ backend_api: 'http://127.0.0.1:19870', control_api: 'http://127.0.0.1:19871', chat_api: 'http://127.0.0.1:19872' });
      if (pathname === '/api/auth/session') return reply({ ok: true });
      if (pathname === '/config/versions') return reply({ ok: true, resources: Object.fromEntries(
        (url.searchParams.get('resources') || '').split(',').map((name) => [name, { state: 'available',
          change_token: name === 'session_classes' ? JSON.stringify(sessions) : name === 'edictum_configs' ? JSON.stringify(edictum) : 'fixed' }]),
      ) });
      if (state.sourcesDown && ['/api/health', '/status'].includes(pathname)) return route.abort('connectionfailed');
      if (pathname === '/api/health') return reply(health());
      if (pathname === '/status') return reply({ running: state.running, managed: state.running });
      if (pathname === '/stop') {
        if (holdStop) await new Promise((resolve) => { stopRelease = resolve; });
        state.running = false;
        return reply({ ok: true });
      }
      if (pathname === '/start' || pathname === '/restart') {
        state.running = true;
        state.staleHealth = false;
        state.runtime = `r${Number(state.runtime.slice(1)) + 1}`;
        return reply({ ok: true });
      }
      const toggle = pathname.match(/^\/config\/(session-classes|edictum\/sessions)\/([^/]+)\/(enable|disable)$/);
      if (toggle) {
        ++state.writes;
        if (state.saveFailure) return reply({ ok: false, error: '模拟保存失败' });
        const configs = toggle[1] === 'session-classes' ? sessions : edictum;
        configs[toggle[2]].enabled = toggle[3] === 'enable';
        return reply({ ok: true, config: configs[toggle[2]] });
      }
      if (pathname === '/config/session-classes') return reply(sessions);
      if (pathname === '/config/edictum/sessions') return reply(edictum);
      if (pathname === '/config/edictum/types') return reply({ types: [{ name: 'simple', is_async: true,
        description: '测试会话', config_schema: {}, capabilities: { plugins: false, mcp: false, stream: false } }] });
      if (pathname === '/config/edictum/plugins') return reply({ plugins: [] });
      if (pathname === '/config/platforms') return reply({ ok: true, platforms, revision: 'v1' });
      if (pathname === '/api/config/reload') { ++state.reloads; return reply({ ok: true, platforms: health().platform_config, edictum_sessions: [] }); }
      if (pathname === '/api/sessions' || pathname === '/config/session-instances') return reply({ sessions: [] });
      if (pathname === '/config') return reply({ ok: true, config: { api: { host: '127.0.0.1', port: 19870 } }, exists: true });
      if (/^\/api\/platforms\/(bot|misskey)\/connection-test$/.test(pathname)) {
        ++probeCalls;
        assert.equal(request.method(), 'POST');
        assert.deepEqual(request.postDataJSON(), {});
        if (holdProbe) await new Promise((resolve) => { probeRelease = resolve; });
        return reply({ ok: !state.probeFailure, detail: state.probeFailure ? '通信请求超时 (8 秒)' : '通信正常, 对端已响应', elapsed_ms: 12 });
      }
      if (/\/groups\/(accounts|[^/]+\/info)$/.test(pathname)) {
        throw new Error('通信检查不应请求群管理接口');
      }
      if (pathname.includes('/wake/diagnostics')) return reply({ records: [], available: true });
      return reply({});
    });
    await page.goto(`${origin}/sessions`);
    const switchDemo = page.getByRole('switch', { name: '会话配置 demo' });
    await switchDemo.waitFor();
    await page.locator('header').getByText('后端运行中', { exact: true }).waitFor();
    const connections = socketCount;
    assert.equal(await page.getByTitle('禁用', { exact: true }).count(), 0);
    assert.equal(await page.getByRole('button', { name: '创建会话', exact: true }).count(), 1);
    await switchDemo.click();
    await page.getByText('已保存并应用', { exact: true }).waitFor();
    assert.equal(sessions.demo.enabled, false);
    assert.equal(state.reloads, 1);
    await page.screenshot({ path: path.join(artifacts, 'session-toggle.png'), fullPage: true });

    await page.locator('aside').getByRole('link', { name: '平台状态', exact: true }).click();
    await page.getByText('会话配置停用', { exact: true }).waitFor();
    socket.send(JSON.stringify({ type: 'status', data: { running: true, adapters: adapters() } }));
    await page.getByText('保存版本 v1 · 生效版本 v1', { exact: true }).first().waitFor();
    assert.equal(socketCount, connections);
    await page.getByRole('button', { name: '测试连接 bot', exact: true }).click();
    const dialog = page.getByRole('dialog');
    assert.equal(await dialog.getByRole('textbox').count(), 0);
    await dialog.getByRole('button', { name: '开始检查', exact: true }).click();
    await dialog.getByText('通信正常, 对端已响应', { exact: true }).waitFor();
    assert.equal(probeCalls, 1);
    await page.screenshot({ path: path.join(artifacts, 'connection-check.png'), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.screenshot({ path: path.join(artifacts, 'connection-check-mobile.png'), fullPage: true });
    await page.setViewportSize({ width: 1440, height: 1000 });
    state.probeFailure = true;
    await dialog.getByRole('button', { name: '开始检查', exact: true }).click();
    await dialog.getByText('通信请求超时 (8 秒)', { exact: true }).waitFor();
    state.probeFailure = false;
    holdProbe = true;
    await dialog.getByRole('button', { name: '开始检查', exact: true }).click();
    await page.waitForFunction(() => document.querySelector('[role="dialog"] [aria-busy="true"]'));
    while (!probeRelease) await new Promise((resolve) => setTimeout(resolve, 10));
    state.account = '11';
    socket.send(JSON.stringify({ type: 'status', data: { running: true, adapters: adapters() } }));
    await dialog.getByText('平台或配置已变化, 旧结果已清除, 请重新测试', { exact: true }).waitFor();
    probeRelease();
    holdProbe = false;
    await dialog.getByRole('button', { name: '开始检查', exact: true }).waitFor();
    assert.equal(await dialog.getByText('通信正常, 对端已响应', { exact: true }).count(), 0);
    await dialog.getByRole('button', { name: '关闭检查', exact: true }).click();
    await page.getByRole('button', { name: '测试连接 misskey', exact: true }).click();
    await dialog.getByRole('button', { name: '开始检查', exact: true }).click();
    await dialog.getByText('通信正常, 对端已响应', { exact: true }).waitFor();
    assert.equal(probeCalls, 4);
    await dialog.getByRole('button', { name: '关闭检查', exact: true }).click();

    await page.locator('aside').getByRole('link', { name: 'Agent 配置', exact: true }).click();
    await page.getByText('Edictum 流程', { exact: true }).click();
    const switchNamed = page.getByRole('switch', { name: '会话配置 named' });
    state.saveFailure = true;
    await switchNamed.click();
    await page.getByText('保存失败', { exact: true }).waitFor();
    assert.equal(await switchNamed.getAttribute('aria-checked'), 'true');
    state.saveFailure = false;
    state.applyFailure = true;
    await switchNamed.click();
    await page.getByText('已保存, 应用未成功', { exact: true }).waitFor();
    assert.equal(await switchNamed.getAttribute('aria-checked'), 'false');
    const writes = state.writes;
    state.applyFailure = false;
    await page.getByRole('button', { name: '重试应用', exact: true }).click();
    await page.getByText('已保存并应用', { exact: true }).waitFor();
    assert.equal(state.writes, writes);

    await page.locator('aside').getByRole('link', { name: '仪表盘', exact: true }).click();
    await page.getByText('会话配置停用', { exact: true }).first().waitFor();
    holdStop = true;
    await page.getByRole('button', { name: '停止后端', exact: true }).click();
    await page.getByRole('button', { name: '后端停止中', exact: true }).waitFor();
    assert.equal(await page.getByRole('button', { name: '后端停止中', exact: true }).isDisabled(), true);
    await page.locator('aside').getByRole('link', { name: '系统设置', exact: true }).click();
    await page.getByRole('button', { name: '后端停止中', exact: true }).waitFor();
    state.staleHealth = true;
    stopRelease();
    await page.getByRole('button', { name: '启动后端', exact: true }).waitFor();
    await page.locator('header').getByText('后端未运行', { exact: true }).waitFor();
    const reloadsBeforeStoppedToggle = state.reloads;
    await page.locator('aside').getByRole('link', { name: 'Agent 配置', exact: true }).click();
    await page.getByRole('switch', { name: '会话配置 demo' }).click();
    await page.getByText('已保存, 后端启动后生效', { exact: true }).waitFor();
    assert.equal(sessions.demo.enabled, true);
    assert.equal(state.reloads, reloadsBeforeStoppedToggle);
    await page.locator('aside').getByRole('link', { name: '系统设置', exact: true }).click();
    await page.getByRole('button', { name: '启动后端', exact: true }).click();
    await page.locator('header').getByText('后端运行中', { exact: true }).waitFor();
    await page.getByRole('button', { name: '停止后端', exact: true }).waitFor();
    state.sourcesDown = true;
    await page.getByRole('button', { name: '刷新状态', exact: true }).click();
    await page.locator('header').getByText('后端状态未知', { exact: true }).waitFor();
    assert.equal(await page.getByRole('button', { name: '启动后端', exact: true }).isDisabled(), true);
    assert.deepEqual(errors, []);
    console.log('backend/platform status E2E passed; screenshots:', artifacts);
  } finally {
    await runtime.closeContexts();
    await runtime.releaseServer(server);
  }

}

export const scenarios = [
  ["administrators", administrators],
  ["agent-routing", agent_routing],
  ["backend-platform-status", backend_platform_status]
];
if (process.argv[1] && pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url) await runSuite(scenarios);
