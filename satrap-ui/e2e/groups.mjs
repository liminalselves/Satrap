import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';
import { createServer } from 'vite';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const artifacts = path.join(root, 'test-results', 'groups');
const server = await createServer({ root, server: { host: '127.0.0.1', port: 0 } });
let browser;
try {
  await fs.mkdir(artifacts, { recursive: true });
  await server.listen();
  const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
  browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const writes = [];
  const decisions = [];
  const actionPosts = [];
  const dryRuns = [];
  const rejectedActions = [];
  let eventReads = 0;
  let actionReads = 0;
  let holdPendingActions = false;
  let releasePendingActions = null;
  let lostAction = null;
  let lostSend = null;
  const modelConfigs = {};
  const edictumConfigs = {};
  const platforms = [];
  const firstRun = { reloads: 0, syncs: 0, sent: [], diagnostics: [] };
  let platformRevision = 'platforms-1';
  let directoryReady = false;
  const row = { group_id: '456', group_name: '测试交流群', member_count: 42, max_member_count: 500,
    membership: 'joined', confirmed_at: 1_790_000_000, response_enabled: false, response_source: 'account' };
  let unbound = true;
  let syncMode = 'never';
  let settingsMode = 'selected';
  let settingsRevision = 1;
  let settingsApprovalDefaults = {};
  const settingsWrites = [];
  let holdOldList = false;
  let releaseOldList = null;
  let config = {
    account: '100', current_account: '100', group: row, explicit: { policy: {}, session: {}, events: {}, approval: {} },
    revision: 0, saved_revision: 0, active_revision: 0, apply_status: 'applied', route_generation: 0,
    session_instances: { known_scoped_count: 1, current_route_count: 1, session_ids: ['session-a'],
      override_counts: { model: 1, prompt: 1, plugins: 0 } },
    base_revision: 'base-1',
    effective: { policy: { enabled: false, wake_mode: 'necessity' },
      session: { binding: { provider: 'edictum', config_name: 'simple' }, scope: 'legacy_user', model: 'base', prompt: '原提示词', plugins: [] },
      approval: { kick_group_member: 'approval_required' }, events: { group_ban: true } },
    sources: { policy: { enabled: { source: 'account', source_label: '账号接入模式', source_index: null } },
      session: { binding: 'platform', scope: 'platform', model: 'named_config', prompt: 'named_config' },
      approval: { kick_group_member: 'default' }, events: { group_ban: 'default' } },
    capabilities: { policy_fields: ['enabled'], session_fields: ['binding', 'scope', 'model', 'prompt', 'plugins'],
      binding_available: true, model_reference_available: true,
      approval_actions: ['kick_group_member'], event_kinds: ['group_ban'] },
  };
  let conflict = false;
  let actionState = 'pending';
  let actionReject = 0;
  const injectAtMessage = (message) => {
    if (!message.includes('@100') || unbound || !directoryReady || !config.effective.policy.enabled) return null;
    const platform = platforms.find((item) => item.id === 'ob');
    const namedSession = edictumConfigs[platform?.session_type];
    if (!platform || !namedSession || !modelConfigs[namedSession.model_name]) return null;
    const reply = { group_id: '456', message: 'fixture-reply', trigger: '@100', model: namedSession.model_name };
    firstRun.sent.push(reply);
    firstRun.diagnostics.push({ request_id: 'first-run-at', recorded_at: '2026-09-27T00:00:00Z',
      session_id: 'fixture-session', self_id: '100', stages: ['ingress', 'wake', 'model', 'send'],
      statuses: { send: 'sent' }, reason_codes: ['at_self'], send_status: 'sent' });
    return reply;
  };
  await context.route(`${origin}/ui-config.json`, (route) => route.fulfill({ json: {
    backend_api: 'http://127.0.0.1:19870', control_api: 'http://127.0.0.1:19871', chat_api: 'http://127.0.0.1:19872',
  } }));
  await context.route(/^http:\/\/127\.0\.0\.1:1987[012]\//, async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const pathname = url.pathname;
    const headers = { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true',
      'Access-Control-Allow-Headers': 'Content-Type', 'Access-Control-Allow-Methods': 'GET, POST, PATCH, PUT, DELETE, OPTIONS' };
    if (request.method() === 'OPTIONS') return route.fulfill({ status: 204, headers });
    const reply = (json, status = 200) => route.fulfill({ status, json, headers });
    if (pathname === '/config/models') return reply(url.searchParams.get('type') === 'llm' ? modelConfigs : {});
    if (pathname === '/config/models/llm/base' && request.method() === 'POST') {
      assert.deepEqual(Object.keys(modelConfigs), []);
      modelConfigs.base = request.postDataJSON();
      return reply({ ok: true });
    }
    if (pathname === '/config/edictum/types') return reply({ types: [{ name: 'simple', is_async: true,
      description: '测试会话', config_schema: {}, capabilities: { plugins: false, mcp: false, stream: false } }] });
    if (pathname === '/config/edictum/plugins') return reply({ plugins: [] });
    if (pathname === '/config/edictum/sessions' && request.method() === 'POST') {
      const payload = request.postDataJSON();
      assert.equal(payload.model_name, 'base');
      edictumConfigs[payload.name] = { ...payload, provider: 'edictum' };
      return reply({ ok: true, name: payload.name, config: edictumConfigs[payload.name] });
    }
    if (pathname === '/config/edictum/sessions') return reply(edictumConfigs);
    if (pathname === '/config/platforms' && request.method() === 'POST') {
      assert.equal(url.searchParams.get('expected_revision'), platformRevision);
      const payload = request.postDataJSON();
      assert.equal(payload.session_provider, 'edictum');
      assert.equal(payload.session_type, 'simple');
      assert.equal(payload.type, 'onebot');
      platforms.push(payload);
      platformRevision = 'platforms-2';
      return reply({ ok: true, platforms, revision: platformRevision });
    }
    if (pathname === '/config/platforms') return reply({ ok: true, platforms, revision: platformRevision });
    if (pathname === '/api/config/reload' && request.method() === 'POST') {
      firstRun.reloads += 1;
      if (platforms.length) unbound = false;
      return reply({ ok: true, platforms: platforms.map((item) => ({ id: item.id, status: 'applied' })),
        edictum_sessions: [] });
    }
    if (pathname === '/api/platforms/ob/groups/sync' && request.method() === 'POST') {
      assert.equal(request.postDataJSON().expected_self_id, '100');
      assert.equal(unbound, false);
      firstRun.syncs += 1;
      directoryReady = true;
      syncMode = 'complete';
      return reply({ sync_id: 'first-run-sync', status: 'complete', reused: false });
    }
    if (pathname === '/api/platforms/ob/groups/sync/first-run-sync') {
      return reply({ sync_id: 'first-run-sync', status: 'complete', complete: true, truncated: false });
    }
    if (pathname === '/api/platforms/ob/groups/456/config' && request.method() === 'PATCH') {
      const payload = request.postDataJSON();
      writes.push(payload);
      if (conflict) return reply({ error: '群配置已变化', code: 'revision_conflict' }, 409);
      assert.equal(payload.expected_self_id, '100');
      assert.equal(payload.expected_revision, config.revision);
      config = structuredClone(config);
      const previousSection = config.explicit[payload.section];
      config.explicit[payload.section] = payload.values;
      config.revision += 1;
      config.saved_revision = config.revision;
      config.active_revision = config.revision;
      if (payload.section === 'session') {
        if (JSON.stringify(previousSection?.binding) !== JSON.stringify(payload.values.binding)
          || JSON.stringify(previousSection?.scope) !== JSON.stringify(payload.values.scope)) {
          config.route_generation += 1;
          config.session_instances.current_route_count = 0;
        }
        config.effective.session.scope = payload.values.scope?.value || 'legacy_user';
        for (const key of ['model', 'prompt', 'plugins']) {
          if (payload.values[key]?.mode === 'value') {
            config.effective.session[key] = payload.values[key].value;
            config.sources.session[key] = 'group';
          }
        }
      }
      if (payload.section === 'approval') {
        for (const [action, value] of Object.entries(payload.values)) {
          config.effective.approval[action] = value.value;
          config.sources.approval[action] = 'group';
        }
      }
      if (payload.section === 'policy') {
        const enabled = payload.values.enabled?.mode === 'value' ? payload.values.enabled.value : false;
        config.effective.policy.enabled = enabled;
        config.sources.policy.enabled = enabled
          ? { source: 'group', source_label: '本群覆盖', source_index: null }
          : { source: 'account', source_label: '账号接入模式', source_index: null };
      }
      return reply(config);
    }
    if (pathname === '/api/platforms/ob/groups/456/dry-run' && request.method() === 'POST') {
      const payload = request.postDataJSON();
      dryRuns.push(payload);
      assert.equal(payload.expected_self_id, '100');
      assert.equal(payload.expected_revision, config.revision);
      return reply({ response_enabled: payload.values.enabled?.value === true,
        explicit: { triggered: true, reason: 'at_self' },
        automatic: { decision: { triggered: false, reason: 'disabled' } }, draft: true });
    }
    if (pathname.endsWith('/actions/action-123456/decision') && request.method() === 'POST') {
      decisions.push(request.postDataJSON());
      actionState = 'succeeded';
      return reply({ action_id: 'action-123456', state: actionState });
    }
    if (pathname === '/api/platforms/ob/groups/456/send' && request.method() === 'POST') {
      const payload = request.postDataJSON();
      lostSend = { action_id: payload.action_id, self_id: '100', group_id: '456',
        action_type: 'send_message', params: { message_length: payload.message.length }, actor_kind: 'panel',
        state: 'unknown', created_at: 1_790_000_000, expires_at: null, decision_at: null,
        executed_at: null, result: { reason: 'interrupted' } };
      return reply({ error: '连接中断', reason: 'group_service_unavailable' }, 503);
    }
    if (pathname === '/api/platforms/ob/groups/456/actions' && request.method() === 'POST') {
      const payload = request.postDataJSON();
      if (actionReject) {
        rejectedActions.push(payload);
        return reply({ error: actionReject === 403 ? '机器人没有群管理权限' : '群名不能为空',
          code: actionReject === 403 ? 'group_permission_denied' : 'invalid_params' }, actionReject);
      }
      actionPosts.push(payload);
      lostAction = { ...payload, self_id: '100', group_id: '456', actor_kind: 'panel', state: 'unknown',
        created_at: 1_790_000_000, expires_at: null, decision_at: null, executed_at: null,
        result: { reason: 'interrupted' } };
      return reply({ error: '连接中断', reason: 'group_service_unavailable' }, 503);
    }
    if (pathname.startsWith('/api/platforms/ob/groups/456/actions/') && request.method() === 'GET') {
      const id = pathname.split('/').at(-1);
      if (lostAction?.action_id === id) return reply(lostAction);
      if (lostSend?.action_id === id) return reply(lostSend);
    }
    if (pathname === '/api/platforms/ob/groups/456/actions') {
      actionReads += 1;
      const filter = url.searchParams.get('state') || 'pending';
      if (filter === 'pending' && holdPendingActions) await new Promise((resolve) => { releasePendingActions = resolve; });
      return reply({
      items: [{ action_id: filter === 'succeeded' ? 'action-succeeded' : 'action-123456',
        self_id: '100', group_id: '456', action_type: 'kick_group_member',
        params: { user_id: '42' }, actor_kind: 'model', state: actionState,
        created_at: 1_790_000_000, expires_at: 1_790_000_600, decision_at: null, executed_at: null, result: null }],
      total: 1, page: 1, page_size: 25,
    });
    }
    if (pathname === '/api/platforms/ob/groups/456/config') return reply({ ...config, account: url.searchParams.get('account') || '100' });
    if (pathname === '/api/platforms/ob/groups/456/events') {
      eventReads += 1;
      return reply({ items: [], volatile: true, capacity: 4096, truncated: false });
    }
    if (pathname === '/api/platforms/ob/groups/456/diagnostics') return reply({ records: firstRun.diagnostics, available: true });
    if (pathname === '/api/platforms/ob/groups/456/diagnostics/first-run-at') return reply({ records: [
      { stage: 'ingress', reason: 'at_self' }, { stage: 'send', status: 'sent' },
    ] });
    if (pathname === '/api/platforms/ob/groups/456/action-types') return reply({ items: [
      { action_type: 'set_group_name', schema: { name: 'string' }, risk: 'normal',
        approval_mode: 'auto_execute', approval_source: 'default', available: true,
        capability: 'unknown', membership: 'joined' },
      { action_type: 'kick_group_member', schema: { user_id: 'string' }, risk: 'high',
        approval_mode: 'approval_required', approval_source: 'default', available: false,
        capability: 'unsupported', membership: 'joined' },
    ] });
    if (pathname === '/api/platforms/ob/groups/bindings') return reply({ account: '100',
      items: [{ provider: 'edictum', config_name: 'simple', enabled: true, available: true,
        description: '简单会话', session_fields: ['binding', 'scope', 'model', 'prompt', 'plugins'] },
      { provider: 'session_class', config_name: 'basic', enabled: true, available: true,
        description: '基础会话', session_fields: ['binding', 'scope'] }],
      models: ['base', 'other'], plugins: [{ name: 'search', description: '搜索', config_schema: {
        limit: { type: 'number', description: '上限', session_overridable: true, integer: true },
      } }],
    });
    if (pathname === '/api/platforms/ob/groups/456/info') return reply({ group_name: row.group_name, member_count: 42, max_member_count: 500 });
    if (pathname === '/api/platforms/ob/groups/456/members') return reply({ items: [], total_loaded: 0, page: 1, page_size: 25, truncated: false, complete: true });
    if (pathname === '/api/platforms/ob/groups/settings' && request.method() === 'PATCH') {
      const payload = request.postDataJSON();
      assert.equal(payload.expected_self_id, '100');
      assert.equal(payload.expected_revision, settingsRevision);
      settingsWrites.push(payload);
      settingsMode = payload.mode;
      settingsApprovalDefaults = payload.approval_defaults;
      settingsRevision += 1;
    }
    if (pathname === '/api/platforms/ob/groups/settings') return reply({ self_id: url.searchParams.get('account') || '100', mode: settingsMode,
      approval_defaults: settingsApprovalDefaults,
      approval_actions: [{ action_type: 'kick_group_member', risk: 'high' }],
      approval_inheriting_counts: { kick_group_member: 1 },
      revision: settingsRevision, migrated_at: 1_790_000_000, last_bound_at: 1_790_000_000,
      legacy_adopted: true, current: true, apply_status: 'applied' });
    if (pathname === '/api/platforms/ob/groups/accounts' && unbound) return reply({
      items: [], current_account: '', waiting_for_account: true,
    });
    if (pathname === '/api/platforms/ob/groups/accounts') return reply({
      items: [{ self_id: '100', mode: 'selected', revision: 1, last_bound_at: 1_790_000_000 },
        { self_id: '101', mode: 'selected', revision: 1, last_bound_at: 1_790_000_000 }],
      current_account: '100', waiting_for_account: false,
    });
    if (pathname === '/api/platforms/ob/groups') {
      const requestedAccount = url.searchParams.get('account') || '100';
      if (requestedAccount === '100' && holdOldList) await new Promise((resolve) => { releaseOldList = resolve; });
      return reply({ items: directoryReady ? [{ ...row, group_name: requestedAccount === '101' ? '历史账号群' : row.group_name,
        response_enabled: settingsMode === 'all' || config.effective.policy.enabled }]
        : [], total: directoryReady ? 1 : 0, page: 1, page_size: 25,
      counts: { joined: directoryReady ? 1 : 0, response_enabled: settingsMode === 'all' ? 1 : 0, configured: 0 }, account: url.searchParams.get('account') || '100',
      current_account: '100', account_generation: 1,
      sync: { status: syncMode, sync_id: 'sync-1', complete: syncMode === 'complete', truncated: syncMode !== 'complete',
        reason: syncMode === 'complete' ? null : 'invalid_entry',
        started_at: 1_790_000_000, completed_at: 1_790_000_001, last_complete_at: 1_790_000_001, connection_generation: 1 } });
    }
    const defaults = {
      '/api/health': { running: true, adapters: { ob: { config_type: 'onebot', status: 'running', started: true } } },
      '/status': { running: true },
      '/api/sessions': { sessions: [
        { platform_id: 'ob', session_id: 'session-a', provider_name: 'edictum', session_type: 'simple',
          active: false, created_at: 1_790_000_000, last_used_at: 1_790_000_000, message_count: 2 },
        { platform_id: 'ob', session_id: 'session-b', provider_name: 'edictum', session_type: 'simple',
          active: false, created_at: 1_790_000_000, last_used_at: 1_790_000_000, message_count: 1 },
      ] }, '/config/session-instances': { sessions: [] },
      '/config/session-classes': {}, '/config/models': {},
    };
    return reply(defaults[pathname] || { ok: true });
  });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.routeWebSocket('ws://127.0.0.1:19870/ws/status**', () => {});
  await page.goto(`${origin}/models`);
  await page.getByRole('button', { name: '新增配置' }).click();
  await page.getByLabel('配置名称').fill('base');
  await page.getByLabel('模型', { exact: true }).fill('fixture-model');
  await page.getByLabel('Base URL').fill('http://fixture.invalid/v1');
  await page.getByLabel('API Key').fill('fixture-key');
  await page.getByRole('button', { name: '创建', exact: true }).click();
  await page.getByText('base', { exact: true }).last().waitFor();
  assert.equal(modelConfigs.base.model, 'fixture-model');
  await page.goto(`${origin}/sessions`);
  await page.getByRole('button', { name: 'Edictum 流程' }).click();
  await page.getByRole('button', { name: '新建 Edictum 配置' }).click();
  await page.getByLabel('配置名称').fill('simple');
  await page.getByLabel('绑定 LLM').selectOption('base');
  await page.getByRole('button', { name: '创建', exact: true }).click();
  await page.getByText('Edictum 配置已创建').waitFor();
  assert.equal(edictumConfigs.simple.model_name, 'base');
  await page.goto(`${origin}/platforms`);
  await page.getByRole('button', { name: '添加平台' }).click();
  await page.getByLabel('平台名称').fill('ob');
  await page.getByLabel('类型', { exact: true }).selectOption('onebot');
  await page.getByLabel('平台默认 Provider').selectOption('edictum');
  await page.getByLabel('Edictum 命名配置').selectOption('simple');
  await page.getByLabel('Self ID').fill('100');
  await page.getByLabel('Host').fill('127.0.0.1');
  await page.getByLabel('Port').fill('3000');
  await page.getByRole('button', { name: '创建', exact: true }).click();
  await page.getByText('配置已保存并生效').waitFor();
  assert.equal(firstRun.reloads >= 2, true);
  assert.equal(unbound, false);
  await page.goto(`${origin}/platforms/ob/groups?account=100`);
  await page.getByText('尚未同步群列表').last().waitFor();
  assert.equal(await page.getByText('测试交流群').count(), 0);
  await page.getByRole('button', { name: '同步群列表' }).click();
  await page.getByText('测试交流群').last().waitFor();
  assert.equal(firstRun.syncs, 1);
  await page.getByRole('link', { name: '进入' }).click();
  assert.equal(injectAtMessage('@100 启用前不应回复'), null);
  await page.getByRole('link', { name: '响应策略', exact: true }).click();
  await page.locator('label:has-text("本群设置") select').selectOption('true');
  await page.getByRole('button', { name: '保存并应用' }).click();
  await page.getByText('响应开启').first().waitFor();
  assert.equal(config.effective.policy.enabled, true);
  assert.equal(platforms[0].session_type, 'simple');
  assert.equal(injectAtMessage('普通群消息'), null);
  assert.deepEqual(injectAtMessage('@100 首次接入'),
    { group_id: '456', message: 'fixture-reply', trigger: '@100', model: 'base' });
  assert.deepEqual(firstRun.sent, [{ group_id: '456', message: 'fixture-reply', trigger: '@100', model: 'base' }]);
  await page.getByRole('link', { name: '事件与诊断' }).click();
  await page.getByText('first-run-at').waitFor();
  await page.getByRole('button', { name: '查看阶段明细' }).click();
  await page.getByText('"status": "sent"').waitFor();
  await page.goto(`${origin}/platforms/ob/groups/456/not-a-tab?account=100`);
  await page.getByText('群详情标签不存在').waitFor();
  await page.getByRole('alert').getByRole('link', { name: '返回群列表' }).click();
  await page.getByText('测试交流群').last().waitFor();
  firstRun.diagnostics.length = 0;
  unbound = true;
  config.explicit.policy = {};
  config.effective.policy.enabled = false;
  config.revision = 0;
  config.saved_revision = 0;
  config.active_revision = 0;
  await page.goto(`${origin}/platforms/ob/groups`);
  await page.getByText('等待机器人连接并确认账号').waitFor();
  assert.equal(await page.getByText('测试交流群').count(), 0);
  unbound = false;
  await page.goto(`${origin}/platforms/ob/groups?account=100`);
  await page.getByText('测试交流群').last().waitFor();
  assert.equal(await page.getByText('关闭', { exact: true }).count() > 0, true);
  for (const theme of ['dark', 'light']) {
    await page.evaluate((value) => document.documentElement.setAttribute('data-theme', value), theme);
    await page.screenshot({ animations: 'disabled', fullPage: true, path: path.join(artifacts, `${theme}-list.png`) });
  }
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ animations: 'disabled', fullPage: true, path: path.join(artifacts, 'narrow-list.png') });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1), true);
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.getByRole('button', { name: '管理默认设置' }).click();
  await page.getByText('当前继承群 1').waitFor();
  await page.getByText('移出成员', { exact: true }).locator('..').locator('select').selectOption('auto_execute');
  await Promise.all([
    page.waitForResponse((response) => response.url().includes('/groups/settings') && response.request().method() === 'PATCH'),
    page.getByRole('button', { name: '保存默认设置' }).click(),
  ]);
  assert.deepEqual(settingsWrites.at(-1).approval_defaults, { kick_group_member: 'auto_execute' });
  assert.equal(settingsMode, 'selected');
  await page.getByRole('button', { name: '管理默认设置' }).click();
  assert.equal(await page.getByText('移出成员', { exact: true }).locator('..').locator('select').inputValue(), 'auto_execute');
  await page.getByRole('button', { name: '取消' }).click();
  await page.getByRole('button', { name: '修改接入模式' }).click();
  await page.getByLabel('全部群, 含以后新加入群').check();
  await page.getByRole('button', { name: '保存模式' }).click();
  await page.getByText('响应开启 1').waitFor();
  assert.equal(settingsMode, 'all');
  await page.getByRole('button', { name: '修改接入模式' }).click();
  await page.getByLabel('仅所选群').check();
  await page.getByRole('button', { name: '保存模式' }).click();
  await page.getByText('响应开启 0').waitFor();
  assert.equal(settingsMode, 'selected');
  holdOldList = true;
  await page.getByLabel('搜索群名或群号').fill('延迟请求');
  await page.waitForRequest((request) => request.url().includes('/api/platforms/ob/groups?')
    && request.url().includes(encodeURIComponent('延迟请求')));
  await page.getByLabel('查看账号').selectOption('101');
  await page.getByText('历史账号群').last().waitFor();
  assert.equal(await page.getByText('历史账号只读').count() > 0, true);
  const oldListResponse = page.waitForResponse((result) => result.url().includes('/api/platforms/ob/groups?')
    && result.url().includes(encodeURIComponent('延迟请求'))
    && result.url().includes('account=100'));
  holdOldList = false;
  releaseOldList();
  await oldListResponse;
  await page.waitForTimeout(100);
  assert.equal(await page.getByText('测试交流群').count(), 0);
  await page.goto(`${origin}/platforms/ob/groups?account=100`);
  await page.getByText('测试交流群').last().waitFor();
  syncMode = 'partial';
  await page.reload();
  await page.getByText('同步不完整').waitFor();
  assert.equal(await page.getByText('测试交流群').last().isVisible(), true);
  syncMode = 'complete';
  await page.reload();
  await page.getByText('测试交流群').last().waitFor();
  await page.getByRole('link', { name: '进入' }).click();
  await page.getByPlaceholder('输入 1 到 1000 字符').fill('测试发送');
  await page.getByRole('button', { name: '发送', exact: true }).click();
  await page.getByText('连接中断').waitFor();
  assert.equal(await page.getByRole('button', { name: '新操作' }).isDisabled(), true);
  await page.getByRole('button', { name: '按操作 ID 查询' }).click();
  await page.getByText('请先核实平台状态').waitFor();
  await page.getByRole('button', { name: '我已核实平台状态, 可以创建新操作' }).click();
  assert.equal(await page.getByPlaceholder('输入 1 到 1000 字符').inputValue(), '');
  await page.getByRole('link', { name: '响应策略', exact: true }).click();
  await page.locator('label:has-text("本群设置") select').selectOption('true');
  await page.locator('label:has-text("示例消息") textarea').fill('@机器人 你好');
  await page.getByLabel('示例消息 @机器人').check();
  const policyWrites = writes.length;
  await Promise.all([
    page.waitForResponse((response) => response.url().endsWith('/groups/456/dry-run')),
    page.getByRole('button', { name: '试算草稿' }).click(),
  ]);
  await page.getByText('基于当前草稿: 允许响应').waitFor();
  assert.deepEqual(dryRuns.at(-1).values.enabled, { mode: 'value', value: true });
  assert.deepEqual(dryRuns.at(-1).scenario.probe, { text: '@机器人 你好', at_self: true });
  assert.equal(writes.length, policyWrites);
  await Promise.all([
    page.waitForResponse((response) => response.url().includes('/groups/456/config') && response.request().method() === 'PATCH'),
    page.getByRole('button', { name: '保存并应用' }).click(),
  ]);
  await page.reload();
  await page.getByText('有效状态: 开启').waitFor();
  assert.equal(await page.locator('label:has-text("本群设置") select').inputValue(), 'true');
  assert.equal(await page.getByText('本群覆盖').count() > 0, true);
  await page.locator('label:has-text("本群设置") select').selectOption('inherit');
  await Promise.all([
    page.waitForResponse((response) => response.url().includes('/groups/456/config') && response.request().method() === 'PATCH'),
    page.getByRole('button', { name: '保存并应用' }).click(),
  ]);
  await page.getByRole('link', { name: '事件与诊断' }).click();
  await page.getByText('近期没有可见事件').waitFor();
  await page.evaluate(() => Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' }));
  const hiddenReads = eventReads;
  await page.waitForTimeout(5_300);
  assert.equal(eventReads, hiddenReads);
  await page.evaluate(() => { delete document.visibilityState; });
  await page.waitForTimeout(5_300);
  assert.equal(eventReads > hiddenReads, true);
  await page.getByRole('link', { name: '会话配置' }).click();
  const afterLeaveReads = eventReads;
  await page.waitForTimeout(5_300);
  assert.equal(eventReads, afterLeaveReads);
  await page.getByText('模型与提示词').waitFor();
  await page.getByText('当前路由可归属实例: 1').waitFor();
  await page.getByText('本群可归属实例 ID: session-a').waitFor();
  await page.locator('label:has-text("模型来源") select').first().selectOption('value');
  await page.locator('label:has-text("模型来源") select').last().selectOption('other');
  await page.locator('label:has-text("系统提示词") select').selectOption('value');
  await page.locator('label:has-text("系统提示词") textarea').fill('');
  await page.locator('label:has-text("search") select').selectOption('disabled');
  let backPrompt = 0;
  page.once('dialog', async (dialog) => {
    assert.equal(dialog.message(), '当前表单有未保存的修改, 确定放弃吗?');
    backPrompt += 1;
    await dialog.dismiss();
  });
  await page.evaluate(() => window.history.back());
  await page.waitForTimeout(100);
  assert.equal(backPrompt, 1);
  assert.equal(new URL(page.url()).pathname.endsWith('/session'), true);
  assert.equal(await page.locator('label:has-text("模型来源") select').last().inputValue(), 'other');
  await page.getByRole('button', { name: '保存并应用' }).click();
  await Promise.all([
    page.waitForResponse((response) => response.url().includes('/groups/456/config') && response.request().method() === 'PATCH'),
    page.getByRole('button', { name: '确认保存覆盖' }).click(),
  ]);
  assert.equal(writes.at(-1).section, 'session');
  assert.deepEqual(writes.at(-1).values.prompt, { mode: 'value', value: '' });
  assert.deepEqual(writes.at(-1).values.model, { mode: 'value', value: 'other' });
  assert.deepEqual(writes.at(-1).values.plugins, { mode: 'value', value: [{ name: 'search', mode: 'disabled', config: {} }] });
  for (const theme of ['dark', 'light']) {
    await page.evaluate((value) => document.documentElement.setAttribute('data-theme', value), theme);
    await page.locator('main').evaluate((element) => { element.scrollTop = 0; });
    await page.screenshot({ animations: 'disabled', fullPage: true, path: path.join(artifacts, `${theme}-session.png`) });
  }
  await page.setViewportSize({ width: 390, height: 844 });
  await page.locator('main').evaluate((element) => { element.scrollTop = 0; });
  await page.screenshot({ animations: 'disabled', fullPage: true, path: path.join(artifacts, 'narrow-session.png') });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1), true);
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.getByRole('link', { name: '查看本群可归属实例' }).click();
  await page.getByText('筛选机器人 100 的群 456').waitFor();
  assert.equal(await page.getByRole('link', { name: '清除筛选' }).count(), 1);
  await page.getByText('session-a', { exact: true }).waitFor();
  assert.equal(await page.getByText('session-b', { exact: true }).count(), 0);
  await page.goBack();
  await page.getByText('模型与提示词').waitFor();
  await page.locator('label:has-text("范围来源") select').selectOption('value');
  await page.locator('label:has-text("本群范围") select').selectOption('group_shared');
  await page.getByRole('button', { name: '保存并应用' }).click();
  const impactDialog = page.getByRole('dialog', { name: '切换会话绑定或范围' });
  await impactDialog.getByText('此后使用新会话; 原历史保留, 不自动迁移').waitFor();
  await impactDialog.getByText('当前路由可归属实例 1 个').waitFor();
  assert.equal(await impactDialog.evaluate((element) => element.contains(document.activeElement)), true);
  await page.keyboard.press('Shift+Tab');
  assert.equal(await impactDialog.getByRole('button', { name: '确认保存并切换' })
    .evaluate((element) => element === document.activeElement), true);
  await page.getByRole('button', { name: '取消' }).click();
  assert.equal(await page.getByRole('button', { name: '保存并应用' })
    .evaluate((element) => element === document.activeElement), true);
  await page.locator('label:has-text("本群范围") select').selectOption('group_member');
  await page.locator('label:has-text("范围来源") select').selectOption('inherit');
  await page.getByRole('link', { name: '审批与记录' }).click();
  await page.getByText('action-123456').waitFor();
  await Promise.all([
    page.waitForResponse((response) => response.url().endsWith('/actions/action-123456/decision')),
    page.getByRole('button', { name: '批准' }).click(),
  ]);
  assert.equal(decisions.length, 1);
  assert.equal(decisions[0].expected_self_id, '100');
  await page.getByRole('button', { name: '刷新' }).click();
  for (const theme of ['dark', 'light']) {
    await page.evaluate((value) => document.documentElement.setAttribute('data-theme', value), theme);
    await page.screenshot({ animations: 'disabled', fullPage: true, path: path.join(artifacts, `${theme}-actions.png`) });
  }
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ animations: 'disabled', fullPage: true, path: path.join(artifacts, 'narrow-actions.png') });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1), true);
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.locator('select').first().selectOption('all');
  await page.getByText('action-123456').waitFor();
  holdPendingActions = true;
  await Promise.all([
    page.waitForRequest((request) => request.url().includes('/groups/456/actions?')
      && request.url().includes('state=pending')),
    page.locator('select').first().selectOption('pending'),
  ]);
  await page.locator('select').first().selectOption('succeeded');
  await page.getByText('action-succeeded').waitFor();
  const staleActionResponse = page.waitForResponse((response) => response.url().includes('/groups/456/actions?')
    && response.url().includes('state=pending'));
  holdPendingActions = false;
  releasePendingActions();
  await staleActionResponse;
  await page.waitForTimeout(100);
  assert.equal(await page.getByText('action-123456').count(), 0);
  await page.locator('select').first().selectOption('all');
  await page.getByText('action-123456').waitFor();
  await page.evaluate(() => Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' }));
  const hiddenActionReads = actionReads;
  await page.waitForTimeout(10_300);
  assert.equal(actionReads, hiddenActionReads);
  await page.evaluate(() => { delete document.visibilityState; });
  await page.waitForTimeout(10_300);
  assert.equal(actionReads > hiddenActionReads, true);
  await page.getByRole('link', { name: '群管理' }).click();
  const afterActionsLeave = actionReads;
  await page.waitForTimeout(10_300);
  assert.equal(actionReads, afterActionsLeave);
  await page.getByText('移出成员', { exact: true }).locator('..').locator('select').selectOption('auto_execute');
  await Promise.all([
    page.waitForResponse((response) => response.url().includes('/groups/456/config') && response.request().method() === 'PATCH'),
    page.getByRole('button', { name: '保存本群审批设置' }).click(),
  ]);
  assert.equal(writes.at(-1).section, 'approval');
  assert.deepEqual(writes.at(-1).values.kick_group_member, { mode: 'value', value: 'auto_execute' });
  assert.equal(writes.at(-1).expected_revision, 3);
  await page.locator('label:has-text("动作") select').selectOption('kick_group_member');
  await page.getByText('此动作当前不可用: 平台已确认不支持').waitFor();
  assert.equal(await page.getByRole('button', { name: '提交管理动作' }).isDisabled(), true);
  await page.locator('label:has-text("动作") select').selectOption('set_group_name');
  actionReject = 403;
  await page.locator('label:has-text("群名") input').fill('新群名');
  await page.getByRole('button', { name: '提交管理动作' }).click();
  await page.getByText('机器人没有群管理权限').waitFor();
  assert.equal(rejectedActions.length, 1);
  await page.getByRole('button', { name: '开始新操作' }).click();
  actionReject = 400;
  await page.getByRole('button', { name: '提交管理动作' }).click();
  await page.getByText('群名不能为空').waitFor();
  assert.equal(rejectedActions.length, 2);
  await page.getByRole('button', { name: '开始新操作' }).click();
  actionReject = 0;
  await page.locator('label:has-text("群名") input').fill('新群名');
  await page.getByRole('button', { name: '提交管理动作' }).click();
  await page.getByText('连接中断').waitFor();
  assert.equal(actionPosts.length, 1);
  await page.getByRole('button', { name: '按原 ID 重试提交' }).click();
  await page.getByText('连接中断').waitFor();
  assert.equal(actionPosts.length, 2);
  assert.equal(actionPosts[0].action_id, actionPosts[1].action_id);
  assert.deepEqual(actionPosts[0].params, actionPosts[1].params);
  await page.getByRole('button', { name: '按原 ID 查询状态' }).click();
  await page.getByText('状态: unknown').waitFor();
  assert.equal(actionPosts.length, 2);
  assert.equal(await page.getByRole('button', { name: '开始新操作' }).isDisabled(), true);
  await page.getByRole('button', { name: '我已核实平台状态, 可以创建新操作' }).click();
  await page.locator('label:has-text("群名") input').fill('另一个群名');
  await page.getByRole('button', { name: '提交管理动作' }).click();
  await page.getByText('连接中断').waitFor();
  assert.equal(actionPosts.length, 3);
  assert.notEqual(actionPosts[2].action_id, actionPosts[0].action_id);
  conflict = true;
  await page.goto(`${origin}/platforms/ob/groups/456/session?account=100`);
  await page.getByText('模型与提示词').waitFor();
  await page.locator('label:has-text("系统提示词") textarea').fill('未保存草稿');
  await page.getByRole('button', { name: '保存并应用' }).click();
  await Promise.all([
    page.waitForResponse((response) => response.url().includes('/groups/456/config') && response.request().method() === 'PATCH'),
    page.getByRole('button', { name: '确认保存覆盖' }).click(),
  ]);
  await page.getByText('群配置已变化').waitFor();
  assert.equal(await page.locator('label:has-text("系统提示词") textarea').inputValue(), '未保存草稿');
  await page.getByRole('button', { name: '取消' }).click();
  await page.locator('label:has-text("绑定来源") select').selectOption('value');
  await page.locator('label:has-text("Provider") select').selectOption('session_class');
  await page.locator('label:has-text("命名配置") select').selectOption('basic');
  await page.getByText('目标配置不支持已有的').waitFor();
  assert.equal(await page.getByRole('button', { name: '保存并应用' }).isDisabled(), true);
  await page.getByRole('button', { name: '模型恢复继承' }).click();
  await page.getByRole('button', { name: '提示词恢复继承' }).click();
  await page.getByRole('button', { name: '插件恢复继承' }).click();
  assert.equal(await page.getByRole('button', { name: '保存并应用' }).isEnabled(), true);
  await page.locator('label:has-text("范围来源") select').selectOption('value');
  await page.locator('label:has-text("本群范围") select').selectOption('group_shared');
  let discardPrompt = 0;
  page.once('dialog', async (dialog) => {
    assert.equal(dialog.message(), '当前表单有未保存的修改, 确定放弃吗?');
    discardPrompt += 1;
    await dialog.dismiss();
  });
  await page.getByRole('link', { name: '响应策略', exact: true }).click();
  assert.equal(discardPrompt, 1);
  assert.equal(new URL(page.url()).pathname.endsWith('/session'), true);
  assert.equal(await page.locator('label:has-text("本群范围") select').inputValue(), 'group_shared');
  page.once('dialog', async (dialog) => { await dialog.accept(); });
  await page.getByRole('link', { name: '返回群列表' }).click();
  await page.getByLabel('查看账号').selectOption('101');
  await page.getByRole('cell', { name: /历史账号群/ }).waitFor();
  await page.getByRole('link', { name: '进入' }).click();
  await page.getByRole('link', { name: '会话配置' }).click();
  await page.getByText('当前连接账号不同, 此账号的群配置只读').waitFor();
  assert.equal(await page.locator('label:has-text("范围来源") select').inputValue(), 'inherit');
  assert.equal(await page.getByRole('button', { name: '保存并应用' }).count(), 0);
  conflict = false;
  await page.goto(`${origin}/platforms/ob/groups/456/session?account=100`);
  await page.getByText('模型与提示词').waitFor();
  await page.locator('label:has-text("范围来源") select').selectOption('value');
  await page.locator('label:has-text("本群范围") select').selectOption('group_shared');
  await page.getByRole('button', { name: '保存并应用' }).click();
  await Promise.all([
    page.waitForResponse((response) => response.url().includes('/groups/456/config') && response.request().method() === 'PATCH'),
    page.getByRole('button', { name: '确认保存并切换' }).click(),
  ]);
  assert.deepEqual(writes.at(-1).values.scope, { mode: 'value', value: 'group_shared' });
  assert.equal(config.route_generation, 1);
  await page.getByText('当前路由可归属实例: 0').waitFor();
  await page.getByRole('link', { name: '查看本群可归属实例' }).click();
  await page.getByText('session-a', { exact: true }).waitFor();
  assert.deepEqual(errors, []);
  console.log(`groups E2E passed; screenshots: ${artifacts}`);
} finally {
  if (browser) await browser.close();
  await server.close();
}
