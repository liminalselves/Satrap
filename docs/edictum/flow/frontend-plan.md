# 流程会话 (issue #4) 前端实现计划

这是交给实现 agent 的任务清单。任务按阶段排列，后一阶段依赖前一阶段。每项写明改哪里、做什么、怎样算完成。

后端已经在 `issue-4` 分支验收通过，见 [runtime-contract.md](runtime-contract.md) 和 [node-contract.md](node-contract.md)。原型位于 `satrap-ui-preview/src/flow/`，被 `.gitignore` 忽略，不入库。原型的视觉和交互已由用户认可，但它的数据格式、发送方式和检查规则已经过时。**现行行为以后端为准**。

## 0. 待用户确认的决定

计划先按下表的推荐写。用户改选后，只需调整对应条目。

| 决定 | 推荐 (本计划采用) | 备选 |
| --- | --- | --- |
| 原型里的模拟器 (`Simulator.tsx`) | 不带进生产。改为在会话实例页提供真实运行状态面板 (F3-2) | 照搬前端模拟器，代价是语义会和后端漂移；或者后端加 dry-run 接口 |
| 编辑器放在哪里 | 独立全屏页 `/agents/:name/flow`。Agent 编辑抽屉里的 flow 类型只保留基本字段，外加一个"打开流程编辑器"按钮 | 画布嵌在抽屉里 |
| 开发顺序 | 直接在生产 `satrap-ui` 开发，原型只作参考，不再维护 | 先在 preview 里同步一版，用户看过后再移植 |
| 保存的并发保护 | 后端为命名配置加 `expected_revision`，冲突时返回 409 (B-2) | 不加，后保存的覆盖先保存的 |

## 0.1 通用约束

- 不改 `satrap-ui` 里与本任务无关的页面。只暂存本任务改动的文件。用户自己的无关改动常在工作区里，不要动。
- 所有下拉框都用 kit `Select`，禁止使用原生 `<select>`。新的玻璃效果和动效要受 `data-glass` / `data-ambient` 开关控制。浮层按现有层级排列：Drawer 110 < Modal 120 < Select 170 < Tooltip 200 < Toast 210。
- 检查规则只以后端为准。前端不复制 `validate.ts` 的规则，只保留**连线时的即时拦截**，即端口类型不匹配时不允许连上。完整检查调用 `POST /config/edictum/flow/validate`。
- 字段一律用后端的 wire 名：节点类型 payload 里配置字段叫 `schema` (不是 `config_schema`)；边端点是 `from`/`to`，写成 `"节点.端口"`；问题的严重程度字段是 `level`，取值 `error`/`warning`，没有 `severity`；数值范围用 `minimum`/`maximum`。
- 新增的 TS 接口和类型，每个字段都要写有意义的注释，与仓库的字段注释规范一致。
- e2e 场景和每个测试文件都串行跑，不并行。
- 本机 Git Bash 里先执行 `export PATH="/f/nodejs:$PATH"`，否则找不到 node。

## B. 后端前置改动 (先做，小改)

### B-1 Control 的类型列表带上 flow 元数据 [阻塞]

- **现状**：`satrap/core/backend/control_server.py:3782` 的 `GET /config/edictum/types` 直接调 `create_default_edictum_type_registry().list_types()`，flow 项没有 `flow` 字段。只有 Backend 的 `/api/config/edictum/types` 走了 `EdictumConfigService.list_types()` (`edictum_service.py:65`)。前端调用的是 Control 的接口 (`satrap-ui/src/api/control.ts:537`)，所以拿不到节点和端口定义。
- **改法**：Control 改走 `EdictumConfigService.list_types()`，在工作线程里执行。
- **验收**：Control 返回的 flow 项包含 `flow.node_types` (内置节点和插件节点)、`flow.port_types` 和 `flow.models`。补一个后端测试。

### B-2 命名配置的保存加 `expected_revision` [待确认，推荐做]

- **现状**：Control 的 `PATCH /config/edictum/sessions/{name}` 和 Backend 的 PATCH/PUT 都不传 revision，请求体里带 `expected_revision` 会被当成未知字段拒绝。manager 层已经支持 `expected_revision` (`satrap/edictum/config.py:338`)。
- **改法**：
  - `GET /config/edictum/sessions/{name}` 和列表接口的每一项都返回 `revision`。
  - PATCH 接受可选的 `expected_revision`。不匹配时返回 `409 {"error", "code": "config_revision_conflict", "revision": 当前值}`。
  - 不传 `expected_revision` 时行为不变，现有的 Agent 编辑抽屉不受影响。
- **验收**：后端测试覆盖匹配、不匹配和不传三种情况。

### B-3 运行状态接口的两处小问题 [建议]

- Control 代理把 Backend 返回的 404 (平台实例未运行、会话未激活) 转成了 503 (`control_server.py:3810` 加上 `:1738` 的代理)。改为原样透传 404 和错误信息，前端据此显示"会话未运行"，而不是"后端不可用"。
- `runtime_status` 没有预算用量。如果流程配置了 `budget`，返回 `budget: {used, max_calls, window_minutes, count}`；没配置时返回 `null`。

## F0. 基础设施

### F0-1 依赖

- `satrap-ui/package.json` 加 `@xyflow/react`，版本与原型一致 (`^12.12.1`)。
- vite 分包时把它放进单独的 chunk，只在流程编辑页懒加载，不进首页包。

### F0-2 API 类型和客户端

- `satrap-ui/src/api/types.ts`：
  - `EdictumTypeDefinition` 加可选字段 `flow?: FlowTypeMetadata`。
  - 新增 `FlowTypeMetadata`、`FlowNodeTypePayload`、`FlowPortPayload`、`FlowPortType`、`FlowIssue`、`FlowDefinitionJSON`、`FlowRuntimeStatus` 等类型，字段按后端 wire 格式写 (见附录 A)。
  - `EdictumPluginConfigField` 加 `label?`、`group?`、`visible_when?`。`type` 的取值加上 `time`。
- `satrap-ui/src/api/edictum.ts` / `control.ts` 新增：
  - `validateFlow({flow, conversation_kinds?, platform_id?}) → {ok, issues}`
  - `getFlowRuntime({platform_id, session_id}) → FlowRuntimeStatus`
  - `getPluginFlowNodeDefaults(plugin) → {nodes, revision, schema}`
  - `putPluginFlowNodeDefaults(plugin, {config, expected_revision})`。这是整张表替换，冲突返回 409。
- `update` 支持 `expected_revision`，依赖 B-2。
- **验收**：vitest 覆盖响应解析，包括 `flow` 字段缺失时的兼容，以及 409 转成 `ApiError` 时带上 code。

### F0-3 配置字段渲染器补齐

`satrap-ui/src/components/common/PluginConfigFields.tsx` 同时被插件页面使用，改动要向后兼容：

- `time`：改用时间输入，前端校验 `HH:MM`，范围 `00:00`–`24:00`。
- `group`：按 group 分段渲染，段标题就是 group 名。没有 group 的字段放在最前面。
- `visible_when`：值可以是单个值，也可以是列表，表示命中其中任意一个就显示。条件不满足时隐藏该字段，但**保留它的值**，因为后端仍会校验隐藏字段。
- `option_labels`：已经支持，确认节点 schema 也能用上。
- **验收**：vitest 覆盖上述三种新行为。插件页面相关的 e2e (`plugin-configuration.mjs`) 不回退。

## F1. 流程编辑器 (核心)

### F1-1 路由和入口

- 新路由 `/agents/:name/flow`，页面目录 `satrap-ui/src/pages/FlowEditor/`，懒加载。
- `AgentEditor.tsx`：
  - 类型为 `flow` 时，隐藏 JSON params 文本框、顶层插件选择和提示词管理。flow 不支持顶层 `plugins`，后端会拒绝，见 `providers/edictum.py:251`。
  - 显示"打开流程编辑器"按钮。
  - 新建 flow 配置时：先在抽屉里填名称和描述，然后进入编辑器，选择模板或空白画布；第一次保存时才调用 create。
- 面包屑和命令面板加上这个页面。`nav.ts` 不加新的一级菜单，编辑器归在"Agent 配置"下面。

### F1-2 存档格式转换 `flowModel.ts`

- `toReactFlow(definition)`：
  - `nodes` 转成 RF 节点，位置取 `layout[id]`，没有时自动排布。
  - `edges[].from/to` 转成 `source/sourceHandle/target/targetHandle`。拆分 `"节点.端口"` 时按**最后一个 `.`** 切开，因为插件节点的 id 可能含点。
  - `label` 转成边的注释。
- `toDefinition(rf)`：反向转换。
  - 只写后端认识的字段：`version`、`nodes[{id, type, version, title, config}]`、`edges[{from, to, label?}]`、`defaults`、`layout`、`budget`。
  - 视口不写进存档，存在 `usePrefsStore` 里，按配置名区分。
  - 节点 `config` 只写**显式设置过**的字段。继承来的值不写，否则会把默认值固化进存档。
  - 节点的 `version` 取 payload 里的当前版本。
- **验收**：vitest 往返测试。用原型的三个模板加一个含插件节点 (id 带点) 的流程，转换来回后与原 JSON 深度相等。

### F1-3 节点面板和画布

- 节点面板完全由 `flow.node_types` 驱动，不硬编码节点：
  - 按 `category` 分组。
  - 插件节点按 `plugin` 二级分组。
  - `maxPerFlow` 达到上限的节点置灰。
  - `note` 归入"其他"。
- 节点渲染从原型的 `FlowNodeView.tsx` 移植：
  - 端口按 `side` 放在左侧或右侧，颜色按端口类型区分。
  - 逻辑门显示 A/B 两个引脚。
  - `derived` 端口的颜色跟随上游类型。
  - 写快照的 WF 显示"读上一版"标记，条件是状态槽的 `writer_reads_previous` 为真。
  - 节点上显示问题角标。
- 连线时的即时拦截：
  - 端口类型取交集，为空就不允许连。
  - 单输入端口 (`multiple: false`) 已有连线时，新连线替换旧连线。
- 边的说明：
  - 读取说明由前端按目标端口和节点配置生成，规则从原型的 `edgeReading()` 移植。插件节点有 `reading` 时优先用它。
  - 用户注释写进 `label`。
  - 双击边打开注释编辑。
- 撤销和重做：至少支持节点、边的增删改和移动，深度 50。
- **验收**：用原型的 `agent_rp` 模板在生产里能完整画出来，视觉和原型一致。用户已认可原型，有差异的地方截图对照。

### F1-4 检查器

- 节点检查器用 F0-3 的渲染器，按节点 `schema` 渲染：
  - 每个字段显示来源：类型默认、插件全局、流程默认、节点。
  - 支持"重置为继承"和"提升为流程默认"。
  - 合并顺序与后端 `resolve_node_config` 一致：类型默认 < 插件全局 < 流程默认 < 节点。插件全局默认通过 `getPluginFlowNodeDefaults` 读取。注意 `flow.defaults.plugin` 只是占位的空对象，不是实际值。
- 特殊字段：
  - **WF 的 `plugins`**：后端存成每行一个插件名的字符串。UI 做成插件多选，从 `/config/edictum/plugins` 取列表，显示插件的 `flow.workflow` 声明 (例如只能装在前台)。保存时转回每行一个名字。
  - **WF 的 `model_name`**：用模型选择器，选项来自 `flow.models`。
  - **输出节点的 `send_mode`**：三个选项，显示 `option_labels` 里的中文名。选 `group_chat_reply` 时提示"仅群聊前台运行可用，需要 WF 装 group_chat"。
  - **状态槽的 `on_new`、阈值磁贴的 `on_new`**：照 schema 渲染即可。
- 边检查器：显示读取说明 (只读) 和说明所属的节点 (可点击跳转)，注释可编辑，可删除这条边。
- 流程级设置面板：
  - 流程默认，即 `defaults[节点类型] = {version, config}`。
  - 预算，即 `budget: {max_calls, window_minutes, count}`，可以关闭 (`null`)。

### F1-5 检查

- 编辑后 debounce 600 ms，调用 `validateFlow`。请求在途时又有新的编辑，就 abort 旧请求。
- 问题面板：
  - 按 `category` 分组，`error` 排在 `warning` 前面。
  - 点击问题定位到 `node` 或 `edge`，有 `field` 时在检查器里高亮对应字段。
  - 显示 `hint`。
- `conversation_kinds`：编辑器顶部提供"群聊 / 私聊 / 都用"切换，默认群聊，影响 `flow_reply_mode_private` 等规则。
- `platform_id`：可选。选择后，检查会带上适配器的后台发送能力 (`flow_background_send_unsupported`)。
- 后端不可用时，问题面板显示"检查服务不可用"，保存按钮照常可用。保存时后端仍会做完整检查。

### F1-6 保存

- 保存前先检查一次。有 `error` 时禁止保存，并列出这些问题。只有 `warning` 时可以保存。
- 新建时调用 create；编辑时调用 update，并带上 `expected_revision` (依赖 B-2)。
- 收到 409 冲突时弹窗提示"配置已被别处修改"，提供两个选项：
  - 重新加载，丢弃本地修改。
  - 覆盖，用最新的 revision 重新提交。
- 保存失败时，后端返回的 `error` 字符串原样显示。保存时的检查错误是单条字符串，不是问题列表。
- 后端在运行时，沿用 `Sessions/index.tsx:291` 的 `commitEdictum` 流程：先预览受影响的会话，确认后保存，再提交运行时应用。不要另写一套。
- 离开页面时如果有未保存的修改，弹出确认。

### F1-7 模板

- 移植原型的三个模板：`agent_rp`、`logic`、`handoff`。改动如下：
  - 发送方式改为 `send_mode: flow_reply`，去掉 `fallback_send`。
  - Agent WF 的 `plugins` 填 `flow_snapshot`。
  - 节点类型和版本与 `flow.node_types` 一致。
  - 插件节点 (`quiet_hours.in_hours`、`llm_budget.within`) 在对应插件未安装时，模板里省略这些节点，并提示用户。
- **验收**：三个模板直接保存时，后端检查没有 `error`。用 e2e 的 fixture 返回的问题来断言。

## F2. 插件节点全局默认

- 插件详情页 (`satrap-ui/src/pages/Plugins/PluginDetail.tsx`) 增加"流程节点默认"分区，只对有 `flow_nodes` 的插件显示：
  - 每个节点一张卡片，按 `schema` 渲染。
  - 保存时调用 `putPluginFlowNodeDefaults`。这个接口是**整张表替换**，必须把所有节点一起提交。
  - 请求带上 `expected_revision`。冲突时给出与 F1-6 相同的处理。
  - `nodes` 里只有已保存的覆盖值。没覆盖的字段显示类型默认，标注"未设置"。
- 编辑器检查器里的"插件全局"层，在这里修改后刷新。

## F3. 运行时

### F3-1 会话实例里的流程会话

- `InstancesPanel.tsx` / `InstanceInspector.tsx`：流程类型的实例显示"流程运行状态"入口。群会话页 (`Groups/GroupSession.tsx`) 也加同样的入口。

### F3-2 运行状态面板 [替代原型的模拟器]

- 调用 `getFlowRuntime` 的 payload 见附录 A。页面可见时每 5 秒轮询一次，不可见时暂停。
- 显示内容：
  - **阈值磁贴**：每个 WF 的已提交位置和积压条数。
  - **状态槽**：版本、`covers_to`、提交时间、run_id。接口不返回内容，不要尝试显示内容。
  - **运行中的 WF**：`running_wfs`。
  - **最近事件**：`events`，按 `recorded_at` 倒序。`events_source` 为 `memory_fallback` 时标注"诊断未开启，仅显示进程内记录"。
  - **插件节点实例**：`node_status`，`ready` 为假时显示 `error`。
  - **预算**：依赖 B-3。没有该字段时不显示。
- 可选：在编辑器画布上叠加只读的运行状态，例如运行中的 WF 高亮、阈值磁贴上显示积压数。依赖用户在编辑器里选择一个会话实例。可以放到后续再做。
- 会话未运行时 (B-3 修好后是 404)，显示"会话未运行"，不报错。

## F4. 测试和文档

### F4-1 vitest

- 存档格式转换往返 (F1-2)。
- 字段渲染器的 `time`/`group`/`visible_when` (F0-3)。
- API 响应解析 (F0-2)。
- 检查请求的 debounce 和 abort (F1-5)。

### F4-2 e2e (新文件 `satrap-ui/e2e/flow-editor.mjs`)

按现有约定：`export const scenarios = [[name, fn], ...]`，用 `page.route` 提供有状态的 fixture。fixture 的数据从后端真实 payload 复制，**不要手写字段**：先跑一次后端，把 `/config/edictum/types` 的 flow 项和 validate 的响应存成 `e2e/fixtures/flow-*.json`。

| 场景 | 断言 |
| --- | --- |
| `flow-create-template` | 新建 flow 配置，选择 `agent_rp` 模板，保存；create 请求体里的 `params.flow` 与预期的 JSON 一致 |
| `flow-edit-roundtrip` | 打开已有流程，修改一个节点字段，移动一个节点，编辑一条边的注释，保存；PATCH 请求体只改了这三处 |
| `flow-validate-issues` | fixture 返回 error 和 warning；问题面板分组正确，点击问题能定位到节点和字段，有 error 时保存按钮被禁用 |
| `flow-connect-reject` | 拖一条类型不匹配的边，连不上；单输入端口连第二条线时替换旧线 |
| `flow-save-conflict` | PATCH 返回 409；弹出冲突提示，"重新加载"和"覆盖"两条路径都能走通 (依赖 B-2) |
| `flow-plugin-defaults` | 插件详情页保存节点默认，请求是整张表加 `expected_revision`；409 时提示冲突 |
| `flow-runtime-status` | 运行状态面板显示位置、积压、状态槽版本和事件；`memory_fallback` 时有标注；会话未运行时显示提示 |
| `flow-agent-editor-entry` | Agent 编辑抽屉里 flow 类型隐藏 JSON params 和顶层插件，显示编辑器入口 |

- `.github/scripts/select_tests.py`：`satrap-ui/src/pages/FlowEditor/` 和 `satrap/edictum/flow/` 映射到上表的场景。
- 跑插件页面相关的 e2e 场景，确认 F0-3 没有带来回退。

### F4-3 文档

- `docs/user/` 加一篇面向用户的流程编辑器说明：节点分类、连线规则、读取说明和注释、检查问题的含义、运行状态面板，以及验收图的画法示例。新建文件，不改现有的 `docs/user/index.md`，除非需要在里面加链接。
- `docs/edictum/flow/README.md` 的文档表加上本计划。

## 附录 A. 后端 wire 格式速查

以下内容来自 `issue-4` 分支的代码。实现时以代码为准，不确定的地方先读代码。

**类型列表中 flow 项的 `flow` 字段** (`edictum_service.py:79`)

```ts
{
  node_types: FlowNodeTypePayload[];  // 内置节点在前, 插件节点在后
  port_types: Record<'messages'|'context'|'text'|'state'|'condition'|'event'|'handoff', {label: string; signal: boolean}>;
  defaults: { flow: {}; plugin: Record<string, Record<string, {}>> };  // 只是占位, 实际值读 flow-nodes 接口
  models: Array<{ name: string; ctx: number; tools: boolean }>;
}
```

**节点类型** (`core/type/flow/registry.py:56`)

```ts
{
  type: string;            // 内置节点名, 或 "插件名.节点名"
  version: number;
  label: string;
  description: string;
  role: 'source'|'transform'|'workflow'|'state'|'sink'|'trigger'|'condition'|'logic'|'note';
  category: string;
  inputs: PortPayload[];
  outputs: PortPayload[];
  schema: Record<string, ConfigFieldPayload>;  // 注意叫 schema
  maxPerFlow?: number;
  callsModel?: true;
  needs?: 'message'|'batch'|'call';
  emits?: 'message'|'batch'|'call';
  plugin?: string;
  reading?: Record<string, string>;
}
// PortPayload: { name; label; types: PortType[]; required; multiple; side?: 'left'|'right'; derived?: true }
// ConfigFieldPayload: { type; default; description; options: string[]; required; nullable;
//   session_overridable; minimum: number|null; maximum: number|null; integer; scope;
//   label?; option_labels?; group?; visible_when? }
```

**存档格式** (`params.flow`)：

```ts
{
  version?: number;   // 存档版本, 当前是 3
  nodes: Array<{ id: string; type: string; version?: number; title?: string; config?: Record<string, unknown> }>;
  edges?: Array<{ from: string; to: string; label?: string }>;  // "节点.端口", 按最后一个点拆分
  defaults?: Record<string, { version?: number; config?: Record<string, unknown> }>;
  layout?: Record<string, { x: number; y: number }>;
  budget?: { max_calls: number; window_minutes?: number; count?: 'all'|'wf' } | null;
}
```

- 节点 `config` 里的未知字段会让检查报 `flow_config_unknown_field`，保存被拒。
- 其他位置的未知字段目前会原样保存，但这不是契约规定的行为，不要依赖。

**检查** `POST /config/edictum/flow/validate`

- 请求 `{flow, conversation_kinds?, platform_id?}`。
- 响应 `{ok, issues: [{code, level, category, message, hint?, node?, edge?, field?}]}`。
- `edge` 的格式是 `"源节点.端口->目标节点.端口"`。
- 完整的问题码表见 `satrap/edictum/flow/validate.py`。前端不需要逐个处理问题码，按 `level`/`category` 显示即可。

**运行状态** `GET /config/edictum/flow/runtime?platform_id=&session_id=` (`session.py:1576`)

```ts
{
  flow_name; session_id; scope_key; history_start: number;
  tiles: Record<tileId, { title: string; wfs: Record<wfId, { committed: number; backlog: number }> }>;
  states: Record<stateId, { version: number; covers_to: number; committed_at: number; run_id: string }>;
  running_wfs: string[];
  events: Array<{ event_id; request_id; sequence: number; phase; status; recorded_at: number; metadata: Record<string, unknown> }>;
  events_source: 'debug_store'|'memory_fallback';
  node_status: Record<nodeId, { ready: boolean; error: string }>;
}
```

**插件节点全局默认** `GET/PUT /config/plugins/{plugin}/flow-nodes`

- GET 返回 `{ok, nodes: {节点名: {version, config}}, revision, schema: {节点名: {字段: ConfigFieldPayload}}}`。节点名不带插件前缀。
- PUT 请求 `{config: {节点名: {version?, config}}, expected_revision}`，整张表替换。
- `revision` 是文件的 sha256，文件不存在时是字符串 `"missing"`。
- 409 表示冲突。
