import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { emptyConfigResponse, runSuite, mockConfigVersions } from './runtime.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const fixture = (name) => JSON.parse(fs.readFileSync(path.join(root, 'e2e', 'fixtures', name), 'utf8'));
const TYPES = fixture('flow-types.json');
const FLOWS = fixture('flow-agent-rp.json');
const PLUGINS = [
  { name: 'flow_snapshot', version: '1.0.0', author: 'satrap', description: '让 WF 写入状态槽快照', config_schema: {}, capabilities: {},
    flow_workflow: { foreground_only: false, provides: ['state_write'] }, flow_nodes: [] },
  { name: 'group_chat', version: '1.0.0', author: 'satrap', description: '群聊富消息回复', config_schema: {}, capabilities: {},
    flow_workflow: { foreground_only: true, provides: [] }, flow_nodes: [] },
];

const flowConfig = (flow, revision = 'rev-1') => ({
  provider: 'edictum', edictum_type: 'flow', enabled: true, description: '群聊 Agent + RP', model_name: 'deepseek-v3',
  params: { flow }, plugins: [], revision,
});

/**
 * 启动开发服务和页面; handlers 可覆盖默认夹具
 *
 * 返回: 页面, 源地址, 记录的请求和页面错误
 */
async function open(runtime, handlers = {}) {
  const server = await runtime.vite({ root, server: { host: '127.0.0.1', port: 0 } });
  await runtime.listen(server);
  const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
  const context = await runtime.browser.newContext({ viewport: { width: 1600, height: 960 } });
  const page = await context.newPage();
  const errors = [];
  const requests = [];
  page.on('pageerror', (error) => errors.push(error.message));
  const cors = { 'Access-Control-Allow-Origin': origin, 'Access-Control-Allow-Credentials': 'true', 'Access-Control-Allow-Headers': '*', 'Access-Control-Allow-Methods': '*' };
  const state = { sessions: handlers.sessions ?? {}, validate: handlers.validate ?? (() => FLOWS.agent_rp_validate) };
  await context.route('**/*', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.origin === origin && url.pathname !== '/ui-config.json') return route.continue();
    if (url.pathname === '/ui-config.json') return route.fulfill({ json: { backend_api: 'http://127.0.0.1:19870', control_api: 'http://127.0.0.1:19871', chat_api: 'http://127.0.0.1:19872' } });
    if (request.method() === 'OPTIONS') return route.fulfill({ status: 204, headers: cors });
    const body = request.postData() ? JSON.parse(request.postData()) : undefined;
    requests.push({ method: request.method(), path: url.pathname, search: url.search, body });
    let handled = false;
    const reply = (json, status = 200) => { handled = true; return route.fulfill({ status, headers: cors, json }); };
    if (handlers.route) await handlers.route({ method: request.method(), url, body, state, reply });
    if (handled) return;
    if (url.pathname === '/status' || url.pathname === '/api/health') return reply({ running: false, managed: false, adapters: {} });
    if (url.pathname === '/config/edictum/types') return reply(TYPES);
    if (url.pathname === '/config/edictum/plugins') return reply({ plugins: PLUGINS });
    if (url.pathname === '/config/platforms') return reply({ ok: true, platforms: [{ id: 'qq-main', type: 'onebot', settings: {} }] });
    if (url.pathname === '/config/edictum/flow/validate') return reply(state.validate(body));
    if (/^\/config\/plugins\/[^/]+\/flow-nodes$/.test(url.pathname)) return reply({ ok: true, nodes: {}, revision: 'missing', schema: {} });
    if (url.pathname === '/config/edictum/sessions' && request.method() === 'GET') return reply(state.sessions);
    if (url.pathname === '/config/edictum/sessions' && request.method() === 'POST') {
      const { name, ...rest } = body;
      state.sessions[name] = { provider: 'edictum', ...rest, revision: 'rev-created' };
      return reply({ ok: true, name, config: state.sessions[name], revision: 'rev-created' });
    }
    const match = url.pathname.match(/^\/config\/edictum\/sessions\/([^/]+)$/);
    if (match) {
      const name = decodeURIComponent(match[1]);
      const current = state.sessions[name];
      if (request.method() === 'GET') return current ? reply(current) : reply({ error: `未知 Edictum 配置: ${name}` }, 404);
      if (request.method() === 'PATCH') {
        const { expected_revision: expected, ...patch } = body;
        if (expected !== undefined && expected !== current.revision) return reply({ error: '配置已被其他操作修改', code: 'config_revision_conflict', revision: current.revision }, 409);
        const revision = `rev-${Number(String(current.revision).replace(/\D/g, '') || 1) + 1}`;
        state.sessions[name] = { ...current, ...patch, revision };
        return reply({ ok: true, name, config: state.sessions[name], revision });
      }
    }
    return reply(emptyConfigResponse(request.url()));
  });
  await page.routeWebSocket(/ws:\/\/127\.0\.0\.1:1987[012]\//, () => {});
  await mockConfigVersions(page, origin, () => JSON.stringify(state.sessions).length.toString());
  return { page, origin, requests, errors, state };
}

const shot = async (page, name) => {
  const artifacts = process.env.SATRAP_E2E_ARTIFACTS;
  if (artifacts) await page.screenshot({ animations: 'disabled', path: path.join(artifacts, `${name}.png`) });
};

async function flow_edit_roundtrip(runtime) {
  /** 打开已有流程: 画布按存档还原, 不修改直接保存时 params.flow 与存档一致, 并带 expected_revision */
  const { page, origin, requests, errors } = await open(runtime, { sessions: { rp: flowConfig(FLOWS.agent_rp) } });
  await page.goto(`${origin}/agents/rp/flow`);
  await page.getByRole('heading', { name: '流程编辑' }).waitFor();
  await page.locator('[data-flow-node="agent"]').waitFor();
  assert.equal(await page.locator('.react-flow__node').count(), FLOWS.agent_rp.nodes.length);
  assert.equal(await page.locator('.react-flow__edge').count(), FLOWS.agent_rp.edges.length);
  await page.getByText('1 条提示').waitFor();
  await shot(page, 'flow-edit-roundtrip');

  // 对话类型: 切到私聊后消息入口和唤醒说明跟着变, 检查请求带上 private
  const wake = page.locator('[data-flow-node="wake"]');
  await wake.getByText('沿用群策略').waitFor();
  await page.getByTitle('会话会用在哪些对话类型上, 影响检查规则').click();
  await page.getByRole('option', { name: '私聊', exact: true }).click();
  await wake.getByText('私聊每条唤醒').waitFor();
  await page.locator('[data-flow-node="source"]').getByText(/^私聊消息档案/).waitFor();
  await page.getByText('每条入站消息 · 私聊每条唤醒').waitFor();
  await page.waitForResponse((response) => response.url().endsWith('/config/edictum/flow/validate')
    && JSON.parse(response.request().postData() || '{}').conversation_kinds?.[0] === 'private');
  await page.getByTitle('会话会用在哪些对话类型上, 影响检查规则').click();
  await page.getByRole('option', { name: '群聊', exact: true }).click();
  await wake.getByText('沿用群策略').waitFor();
  await page.getByRole('button', { name: '保存', exact: true }).click();
  await page.getByText('流程已保存').waitFor();
  await page.waitForURL(`${origin}/agents?edictum=rp`);
  assert.equal(await page.getByRole('dialog', { name: '管理插件' }).count(), 0, '流程配置返回时不弹出顶层插件管理');
  const patch = requests.find((item) => item.method === 'PATCH');
  assert.equal(patch.body.expected_revision, 'rev-1');
  assert.deepEqual(patch.body.plugins, []);
  assert.deepEqual(patch.body.params.flow, FLOWS.agent_rp);

  // 命令面板: 从其他页面直接打开流程编辑器
  await page.goto(`${origin}/plugins`);
  await page.getByRole('button', { name: '打开命令面板' }).click();
  await page.getByRole('combobox', { name: '搜索页面或操作' }).fill('编辑流程');
  await page.getByRole('dialog', { name: '命令面板' }).getByText('编辑流程 · rp').click();
  await page.waitForURL(`${origin}/agents/rp/flow`);
  await page.locator('[data-flow-node="agent"]').waitFor();
  assert.deepEqual(errors, []);
  console.log('PASS: 已有流程按存档还原, 原样保存后存档一致并带修订号并返回 Agent 配置, 命令面板可直达');
}

async function flow_agent_editor_entry(runtime) {
  /** 抽屉新建 flow: 隐藏参数和插件, 下一步进入编辑器且未创建配置; 选模板后第一次保存才 create */
  const { page, origin, requests, errors, state } = await open(runtime);
  await page.goto(`${origin}/agents`);
  await page.getByRole('button', { name: 'Edictum 流程', exact: true }).click();
  await page.locator('button').filter({ hasText: /^flow/ }).first().click();
  const drawer = page.getByRole('dialog');
  assert.equal(await drawer.getByRole('button', { name: '打开流程编辑器' }).count(), 0, '新建时只有底部一个流程入口');
  assert.equal(await drawer.getByLabel('其他会话参数 (JSON 对象)').count(), 0, 'flow 不显示 JSON 参数');
  assert.equal(await drawer.getByRole('button', { name: '管理插件' }).count(), 0, 'flow 不显示顶层插件');
  await drawer.getByPlaceholder('如: platform-assistant').fill('group-rp');
  await drawer.getByPlaceholder(/QQ 群客服助手/).fill('群聊 RP');
  await drawer.getByRole('button', { name: '下一步: 编辑流程' }).click();
  await page.waitForURL(/\/agents\/group-rp\/flow\?create=1/);
  assert.equal(requests.filter((item) => item.method === 'POST' && item.path === '/config/edictum/sessions').length, 0, '进入编辑器前不创建配置');

  const picker = page.getByRole('dialog', { name: '选择模板' });
  await picker.locator('[data-template="agent_rp"]').click();
  await page.locator('[data-flow-node="rp"]').waitFor();
  await page.getByRole('button', { name: '创建', exact: true }).click();
  await page.getByText('流程已创建').waitFor();
  await page.waitForURL(`${origin}/agents?edictum=group-rp`);
  const created = requests.find((item) => item.method === 'POST' && item.path === '/config/edictum/sessions');
  assert.equal(created.body.edictum_type, 'flow');
  assert.equal(created.body.description, '群聊 RP');
  assert.deepEqual(created.body.plugins, []);
  assert.equal(created.body.params.flow.version, 3);
  assert.ok(created.body.params.flow.nodes.some((node) => node.id === 'agent' && node.config.plugins === 'flow_snapshot'));
  assert.ok(state.sessions['group-rp']);
  assert.deepEqual(errors, []);
  console.log('PASS: 抽屉新建 flow 隐藏参数与插件, 选择模板后首次保存才创建配置并返回 Agent 配置');
}

async function flow_drawer_edit_entry(runtime) {
  /** 编辑已有 flow: 抽屉底部"编辑流程"直达; 有未保存修改时先保存再进入, 不用二次点击; Ctrl+S 保存后留在编辑器 */
  const { page, origin, requests, errors, state } = await open(runtime, { sessions: { rp: flowConfig(FLOWS.agent_rp) } });
  await page.goto(`${origin}/agents`);
  await page.getByRole('button', { name: 'Edictum 流程', exact: true }).click();
  await page.getByRole('button', { name: '打开 rp 配置' }).click();
  const drawer = page.getByRole('dialog');
  await drawer.getByRole('button', { name: '编辑流程', exact: true }).click();
  await page.waitForURL(`${origin}/agents/rp/flow`);
  await page.locator('[data-flow-node="agent"]').waitFor();
  assert.equal(requests.filter((item) => item.method === 'PATCH').length, 0, '没有修改时直接进入, 不保存');

  // Ctrl+S: 保存后留在编辑器
  await page.locator('[data-flow-node="agent"]').click();
  await page.getByLabel('节点名称').fill('Agent 快捷键');
  await page.keyboard.press('Control+s');
  await page.getByText('流程已保存').waitFor();
  assert.equal(new URL(page.url()).pathname, '/agents/rp/flow', 'Ctrl+S 后留在编辑器');
  assert.equal(state.sessions.rp.params.flow.nodes.find((node) => node.id === 'agent').title, 'Agent 快捷键');

  // 抽屉改了描述: 主按钮变为"保存并编辑流程", 一次点击先保存再进入
  await page.goto(`${origin}/agents`);
  await page.getByRole('button', { name: 'Edictum 流程', exact: true }).click();
  await page.getByRole('button', { name: '打开 rp 配置' }).click();
  await drawer.getByPlaceholder(/QQ 群客服助手/).fill('群聊 RP 改');
  await drawer.getByRole('button', { name: '保存并编辑流程' }).click();
  await page.waitForURL(`${origin}/agents/rp/flow`);
  await page.locator('[data-flow-node="agent"]').waitFor();
  assert.equal(state.sessions.rp.description, '群聊 RP 改');
  assert.equal(state.sessions.rp.params.flow.nodes.find((node) => node.id === 'agent').title, 'Agent 快捷键', '抽屉保存保留流程');
  assert.deepEqual(errors, []);
  console.log('PASS: 抽屉一个入口直达流程编辑器, 未保存修改先保存再进入; Ctrl+S 保存留在编辑器');
}

async function flow_validate_issues(runtime) {
  /** 检查问题: 节点角标, 分类分组, 点击定位到字段; 有错误时阻止保存; 编辑合并为一次检查 */
  const validations = [];
  const { page, origin, requests, errors } = await open(runtime, {
    sessions: { rp: flowConfig(FLOWS.agent_rp) },
    validate: (body) => { validations.push(body); return FLOWS.broken_validate; },
  });
  await page.goto(`${origin}/agents/rp/flow`);
  await page.getByText(/个错误/).first().waitFor();
  await page.locator('[data-flow-node="agent"]').getByLabel(/个错误/).waitFor();
  const panel = page.getByLabel('流程检查');
  await panel.getByText('触发', { exact: true }).waitFor();
  await panel.getByText('配置', { exact: true }).waitFor();
  assert.deepEqual(validations[0].conversation_kinds, ['group']);
  await shot(page, 'flow-validate-issues');

  await panel.locator('[data-issue="flow_config_invalid"]').first().click();
  await page.locator('[data-field="max_steps"].flow-field-focus').waitFor();
  const title = page.getByLabel('节点名称');
  assert.equal(await title.inputValue(), 'Agent');

  // 连续输入只触发一次检查
  const before = validations.length;
  await title.press('End');
  await title.pressSequentially('XYZ', { delay: 40 });
  await page.waitForTimeout(1200);
  assert.equal(validations.length, before + 1, '连续编辑合并为一次检查');
  assert.equal(validations.at(-1).flow.nodes.find((node) => node.id === 'agent').title, 'AgentXYZ');

  await page.getByRole('button', { name: '保存', exact: true }).click();
  await page.getByText(/还有 \d+ 个错误/).waitFor();
  assert.equal(requests.filter((item) => item.method === 'PATCH').length, 0, '有错误时不发送保存');
  assert.deepEqual(errors, []);
  console.log('PASS: 检查问题按分类显示并可定位字段, 连续编辑合并检查, 有错误时阻止保存');
}

async function flow_validate_unavailable(runtime) {
  /** 检查服务不可用: 显示提示, 保存照常可用 */
  const { page, origin, requests, errors } = await open(runtime, {
    sessions: { rp: flowConfig(FLOWS.agent_rp) },
    route: ({ url, reply }) => url.pathname === '/config/edictum/flow/validate' ? reply({ error: 'backend unavailable' }, 503) : null,
  });
  await page.goto(`${origin}/agents/rp/flow`);
  await page.getByText('检查服务不可用', { exact: false }).first().waitFor();
  await page.getByRole('button', { name: '保存', exact: true }).click();
  await page.getByText('流程已保存').waitFor();
  assert.equal(requests.filter((item) => item.method === 'PATCH').length, 1);
  assert.deepEqual(errors, []);
  console.log('PASS: 检查服务不可用时提示并允许保存');
}

async function flow_connect_reject(runtime) {
  /** 连线: 类型不符的端口拒绝, 单输入端口新连线替换旧连线, 撤销恢复 */
  const { page, origin, errors } = await open(runtime, { sessions: { rp: flowConfig(FLOWS.agent_rp) } });
  await page.goto(`${origin}/agents/rp/flow`);
  await page.locator('[data-flow-node="agent"]').waitFor();
  const edges = () => page.locator('.react-flow__edge').count();
  const drag = async (from, to) => {
    const a = await page.locator(`[data-port="${from}"]`).boundingBox();
    const b = await page.locator(`[data-port="${to}"]`).boundingBox();
    await page.mouse.move(a.x + a.width / 2, a.y + a.height / 2);
    await page.mouse.down();
    await page.mouse.move(b.x + b.width / 2, b.y + b.height / 2, { steps: 12 });
    await page.mouse.up();
  };
  const initial = await edges();
  // text 输出接到 messages 输入: 类型不符
  await drag('agent.text', 'rp_input.messages');
  assert.equal(await edges(), initial, '类型不符的连线被拒绝');
  // wake.messages 是单输入端口, 已连 bucket.out: 用 source.out 改连后替换旧连线
  await drag('source.out', 'wake.messages');
  await page.locator('[data-id="source.out->wake.messages"]').waitFor();
  assert.equal(await page.locator('[data-id="bucket.out->wake.messages"]').count(), 0, '单输入端口的旧连线被替换');
  assert.equal(await edges(), initial);
  await page.getByRole('button', { name: '撤销' }).click();
  await page.locator('[data-id="bucket.out->wake.messages"]').waitFor();
  assert.equal(await page.locator('[data-id="source.out->wake.messages"]').count(), 0);
  assert.deepEqual(errors, []);
  console.log('PASS: 类型不符的连线被拒绝, 单输入端口替换旧连线, 撤销可恢复');
}

async function flow_import_json(runtime) {
  /** 导入 JSON: 拖入命名配置文件替换画布并可撤销, 文件选择导入 params.flow, 粘贴错误内容不动画布, 导入后保存写入导入的流程 */
  const { page, origin, requests, errors } = await open(runtime, { sessions: { rp: flowConfig({ version: 3, nodes: [] }) } });
  await page.goto(`${origin}/agents/rp/flow`);
  await page.getByRole('heading', { name: '流程编辑' }).waitFor();
  const nodes = () => page.locator('.react-flow__node').count();
  assert.equal(await nodes(), 0);

  // 拖入完整配置文件 (配置名 -> 命名配置): 取出唯一的流程
  const file = JSON.stringify({ chat: { edictum_type: 'simple', params: {} }, rp: flowConfig(FLOWS.agent_rp) });
  const transfer = await page.evaluateHandle((text) => {
    const data = new DataTransfer();
    data.items.add(new File([text], 'edictum_session_config.json', { type: 'application/json' }));
    return data;
  }, file);
  const canvas = page.getByRole('main', { name: '流程画布' });
  await canvas.dispatchEvent('dragover', { dataTransfer: transfer });
  await page.getByText('松开以导入流程 JSON').waitFor();
  await shot(page, 'flow-import-drop');
  await canvas.dispatchEvent('drop', { dataTransfer: transfer });
  await page.getByText('已导入流程').waitFor();
  await page.locator('[data-flow-node="agent"]').waitFor();
  assert.equal(await nodes(), FLOWS.agent_rp.nodes.length);
  assert.equal(await page.getByText('松开以导入流程 JSON').count(), 0);
  await page.getByRole('button', { name: '撤销' }).click();
  await page.waitForFunction(() => document.querySelectorAll('.react-flow__node').length === 0);

  // 文件选择: 直接是 params.flow
  await page.getByLabel('选择流程 JSON 文件').setInputFiles({ name: 'flow.json', mimeType: 'application/json', buffer: Buffer.from(JSON.stringify(FLOWS.agent_rp)) });
  await page.locator('[data-flow-node="agent"]').waitFor();

  // 粘贴: 结构错误时提示原因, 画布不变
  await page.getByRole('button', { name: '查看配置' }).click();
  const dialog = page.getByRole('dialog', { name: '配置 JSON' });
  await dialog.getByRole('button', { name: '编辑并替换' }).click();
  const editor = dialog.getByLabel('流程 JSON');
  await editor.fill(JSON.stringify({ nodes: [{ id: 'a', type: 'note' }], edges: [{ from: 'a.out', to: 'b.in' }] }));
  await dialog.getByRole('button', { name: '替换画布' }).click();
  await page.getByText('不存在的节点 b').waitFor();
  await shot(page, 'flow-import-edit');
  assert.equal(await nodes(), FLOWS.agent_rp.nodes.length, '错误内容不替换画布');
  // 粘贴合法内容: 改一个标题后替换, 保存写入导入后的流程
  const edited = { ...FLOWS.agent_rp, nodes: FLOWS.agent_rp.nodes.map((node) => node.id === 'agent' ? { ...node, title: '导入的 Agent' } : node) };
  await editor.fill(JSON.stringify(edited));
  await dialog.getByRole('button', { name: '替换画布' }).click();
  await dialog.waitFor({ state: 'detached' });
  await page.locator('[data-flow-node="agent"]').getByText('导入的 Agent').waitFor();
  await shot(page, 'flow-import-json');
  await page.getByRole('button', { name: '保存', exact: true }).click();
  await page.getByText('流程已保存').waitFor();
  const patch = requests.find((item) => item.method === 'PATCH');
  assert.deepEqual(patch.body.params.flow, edited);
  assert.deepEqual(errors, []);
  console.log('PASS: 拖入配置文件与文件选择可导入并撤销, 错误 JSON 不动画布, 粘贴替换后保存写入导入的流程');
}

async function flow_save_conflict(runtime) {
  /** 409 冲突: 弹窗提示, 覆盖时用最新修订重新提交 */
  const { page, origin, requests, errors, state } = await open(runtime, { sessions: { rp: flowConfig(FLOWS.agent_rp) } });
  await page.goto(`${origin}/agents/rp/flow`);
  await page.locator('[data-flow-node="agent"]').click();
  await page.getByLabel('节点名称').fill('Agent 改');
  state.sessions.rp = { ...state.sessions.rp, revision: 'rev-7' };
  await page.getByRole('button', { name: '保存', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '配置已被别处修改' });
  await dialog.waitFor();
  await dialog.getByRole('button', { name: '覆盖' }).click();
  await page.getByText('流程已保存').waitFor();
  const patches = requests.filter((item) => item.method === 'PATCH');
  assert.deepEqual(patches.map((item) => item.body.expected_revision), ['rev-1', 'rev-7']);
  assert.equal(state.sessions.rp.params.flow.nodes.find((node) => node.id === 'agent').title, 'Agent 改');
  assert.deepEqual(errors, []);
  console.log('PASS: 修订冲突弹窗, 覆盖时使用最新修订重新提交');
}

async function flow_plugin_defaults(runtime) {
  /** 插件详情: 有 flow_nodes 时显示流程节点默认, 整表保存并带 revision */
  const budget = TYPES.types.find((type) => type.name === 'flow').flow.node_types.find((def) => def.type === 'llm_budget.within');
  const catalog = [{ name: 'llm_budget', version: '1.0.0', author: 'satrap', description: '模型调用预算', config_schema: {}, capabilities: {},
    source: 'builtin', usage_count: 0, edictum_configs: [], chat_enabled: false, flow_nodes: [budget], flow_workflow: { foreground_only: false, provides: [] } }];
  let saved;
  const { page, origin, errors } = await open(runtime, {
    route: ({ method, url, body, reply }) => {
      if (url.pathname === '/config/plugins') return reply({ plugins: catalog });
      if (url.pathname === '/config/plugins/llm_budget/flow-nodes' && method === 'GET') return reply({ ok: true, nodes: { within: { version: budget.version, config: { max_calls: 30 } } }, revision: 'fn-1', schema: { within: budget.schema } });
      if (url.pathname === '/config/plugins/llm_budget/flow-nodes' && method === 'PUT') {
        saved = body;
        return reply({ ok: true, nodes: body.config, revision: 'fn-2', schema: { within: budget.schema }, saved: true });
      }
      if (url.pathname.startsWith('/config/plugins/llm_budget')) return reply({ ok: true, name: 'llm_budget', schema: {}, values: {}, overrides: {}, revision: 'g-1' });
      return null;
    },
  });
  await page.goto(`${origin}/plugins/llm_budget?tab=config`);
  const section = page.getByRole('region', { name: budget.label });
  await section.waitFor();
  const calls = section.getByLabel(budget.schema.max_calls.label || 'max_calls');
  assert.equal(await calls.inputValue(), '30');
  await calls.fill('45');
  await page.getByRole('button', { name: '保存节点默认' }).click();
  await page.getByText('节点默认已保存').waitFor();
  assert.equal(saved.expected_revision, 'fn-1');
  assert.deepEqual(saved.config, { within: { version: budget.version, config: { max_calls: 45 } } });
  assert.deepEqual(errors, []);
  console.log('PASS: 插件详情显示流程节点默认, 整表保存并带 revision');
}

async function flow_runtime_status(runtime) {
  /** 流程会话实例的运行状态: 阈值积压, 状态槽版本, 预算和事件来源; 404 显示会话未运行 */
  const status = {
    flow_name: 'rp', session_id: 's1', scope_key: 'g:1', history_start: 1731600000,
    tiles: { th: { title: 'Agent 阈值', wfs: { agent: { committed: 120, backlog: 7 } } } },
    states: { snapshot: { version: 3, covers_to: 118, committed_at: 1731600500, run_id: 'run-abc' } },
    running_wfs: ['agent'], events: [{ event_id: '', request_id: '', sequence: -1, phase: 'fire', status: 'ok', recorded_at: 1731600600, metadata: { node: 'th' } }],
    events_source: 'memory_fallback', node_status: {}, budget: { used: 12, max_calls: 60, window_minutes: 60, count: 'all' },
  };
  let running = true;
  const session = { platform_id: 'qq-main', session_id: 's1', active: true, provider_name: 'edictum', session_type: 'rp', created_at: 1731600000, last_used_at: 1731600600, message_count: 4, session_config: {} };
  const { page, origin, errors } = await open(runtime, {
    sessions: { rp: flowConfig(FLOWS.agent_rp) },
    route: ({ url, reply }) => {
      if (url.pathname === '/status' || url.pathname === '/api/health') return reply({ running: true, managed: true, adapters: {} });
      if (url.pathname === '/api/sessions' || url.pathname === '/config/sessions') return reply({ sessions: [session] });
      if (url.pathname === '/config/edictum/flow/runtime') return running ? reply(status) : reply({ error: '会话未激活: s1' }, 404);
      return null;
    },
  });
  await page.goto(`${origin}/conversations/instances`);
  await page.getByText('s1').first().click();
  await page.getByRole('tab', { name: '流程运行状态' }).click();
  const panel = page.getByLabel('流程运行状态');
  await panel.getByText('积压 7 条').waitFor();
  await panel.getByText('v3').waitFor();
  await panel.getByText('12 / 60').waitFor();
  await panel.getByText('诊断未开启, 仅显示进程内记录').waitFor();
  await shot(page, 'flow-runtime-status');
  running = false;
  await page.getByText('会话未运行', { exact: false }).first().waitFor({ timeout: 15000 });
  assert.deepEqual(errors, []);
  console.log('PASS: 实例检查器显示流程运行状态, 会话停止后显示未运行');
}

export const scenarios = [
  ['flow-edit-roundtrip', flow_edit_roundtrip],
  ['flow-agent-editor-entry', flow_agent_editor_entry],
  ['flow-drawer-edit-entry', flow_drawer_edit_entry],
  ['flow-validate-issues', flow_validate_issues],
  ['flow-validate-unavailable', flow_validate_unavailable],
  ['flow-connect-reject', flow_connect_reject],
  ['flow-import-json', flow_import_json],
  ['flow-save-conflict', flow_save_conflict],
  ['flow-plugin-defaults', flow_plugin_defaults],
  ['flow-runtime-status', flow_runtime_status],
];
if (process.argv[1] && pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url) await runSuite(scenarios);
