// API 类型定义 - 与后端 Python 类型对应

export interface PlatformConfigApplication {
  id: string;
  saved_revision: string | null;
  active_revision: string | null;
  status: 'applied' | 'pending_restart' | 'failed';
  reason?: string;
  error?: string;
}

export interface BackendHealth {
  running: boolean;
  runtime_id?: string;
  adapters?: Record<string, AdapterInfo>;
  platform_config?: PlatformConfigApplication[];
  error?: string;
}

export interface AdapterInfo {
  status: string;
  started: boolean;
  config_type?: string;
  session_provider?: string;
  session_type?: string;
  type?: string;
  last_error?: string;
  client_self_id?: string;
  conversation_kinds?: Record<string, string>;
  session_bindings?: Record<string, AgentBinding>;
}

export type AgentBinding = { mode: 'inherit' } | {
  mode: 'value'; provider: 'session_class' | 'edictum'; config_name: string;
};

export interface AdapterDeclaration {
  type: string;
  display_name: string;
  conversation_kinds: Record<string, string>;
  status: 'available' | 'unavailable';
  error?: string;
}

export interface LLMConfig {
  name: string;
  model?: string;
  base_url?: string;
  api_key?: string;
  temperature?: number;
  top_p?: number;
  max_tokens?: number;
  network_timeout?: number;
  context_window?: number;
  history_ratio?: number;
  context_strategy?: 'sliding' | 'mid_truncate' | 'summarize';
  context_threshold?: number;
  truncation_floor?: number;
  summary_keep_recent_turns?: number;
  lock_api_key?: boolean;
  thinking_field_name?: string | null;
  thinking_fields?: string[];
  thinking_levels?: string[];
  omit_none_thinking_fields?: boolean;
  supports_visual_input?: boolean;
  supports_native_video_input?: boolean;
  video_frame_interval?: number;
  api_type?: 'chat_completions' | 'responses';
  native_video_transport?: 'data_url' | 'url';
}

export interface EmbeddingConfig {
  name: string;
  model?: string;
  base_url?: string;
  api_key?: string;
  dimensions?: number | null;
  max_batch_size?: number;
}

export interface ReRankConfig {
  name: string;
  model?: string;
  base_url?: string;
  api_key?: string;
  top_k?: number;
  min_score?: number;
}

export interface ASRConfig {
  name: string;
  model?: string;
  base_url?: string;
  api_key?: string;
  language?: string;
  prompt?: string;
  timeout?: number;
}

export interface ImgConfig {
  name: string;
  model?: string;
  base_url?: string;
  edit_base_url?: string | null;
  api_key?: string;
  api_type?: 'openai_images' | 'seedream' | 'novelai';
  timeout?: number;
  default_params?: Record<string, unknown> | null;
  lock_api_key?: boolean;
  allow_insecure_base_url?: boolean;
}

export type ModelConfig = LLMConfig | EmbeddingConfig | ReRankConfig | ASRConfig | ImgConfig;
export type ModelType = 'llm' | 'embedding' | 'rerank' | 'asr' | 'img';

export interface SessionClassConfig {
    name?: string;
    class_path: string;
    description?: string;
    is_async?: boolean;
    enabled: boolean;
    context_key?: string;
    model_key?: string;
    params?: Record<string, unknown>;
  }

export interface EdictumTypeCapabilities {
  plugins: boolean;
  mcp: boolean;
  stream: boolean;
}

export interface EdictumTypeDefinition {
  name: string;
  is_async: boolean;
  description: string;
  config_schema: Record<string, unknown>;
  capabilities: EdictumTypeCapabilities;
  /** 仅 flow 类型携带: 流程编辑器需要的节点/端口契约; 其他类型没有该字段 */
  flow?: FlowTypeMetadata;
}

/** 流程端口数据类型; condition/event/handoff 是信号端口, 其余是数据端口 */
export type FlowPortType = 'messages' | 'context' | 'text' | 'state' | 'condition' | 'event' | 'handoff';

/** 节点角色, 决定画布渲染形态和连线规则 */
export type FlowNodeRole =
  | 'source' | 'transform' | 'workflow' | 'state' | 'sink'
  | 'trigger' | 'condition' | 'logic' | 'note';

/** 节点上的一个端口 */
export interface FlowPortPayload {
  /** 端口名, 边端点写作 "节点id.端口名" */
  name: string;
  /** 画布上显示的端口名 */
  label: string;
  /** 可接受的数据类型; 连线时与对端类型取交集, 为空则不允许连接 */
  types: FlowPortType[];
  /** 输入端口为真时必须有连线, 否则后端检查报 flow_port_required */
  required: boolean;
  /** 输入端口为真时允许多条连线; 为假时新连线替换旧连线 */
  multiple: boolean;
  /** 端口放在节点哪一侧; 缺省时输入在左, 输出在右 */
  side?: 'left' | 'right';
  /** 逻辑门输出: 实际类型由上游推导 (有触发输入则为 event, 否则 condition) */
  derived?: true;
}

/** 一种节点类型的完整契约 (内置节点或插件节点) */
export interface FlowNodeTypePayload {
  /** 节点类型名; 插件节点为 "插件名.节点名" */
  type: string;
  /** 节点契约版本; 存档时写入节点的 version 字段 */
  version: number;
  /** 节点面板和卡片上的名称 */
  label: string;
  /** 节点用途说明 */
  description: string;
  /** 节点角色 */
  role: FlowNodeRole;
  /** 节点面板分组名 */
  category: string;
  /** 输入端口, 按声明顺序 */
  inputs: FlowPortPayload[];
  /** 输出端口, 按声明顺序 */
  outputs: FlowPortPayload[];
  /** 节点配置字段 (后端 wire 名就叫 schema) */
  schema: Record<string, EdictumPluginConfigField>;
  /** 一张流程图里最多放几个; 缺省不限 */
  maxPerFlow?: number;
  /** 节点自己会调用模型 (插件节点配置了模型字段) */
  callsModel?: true;
  /** 条件节点需要的触发上下文: 单条消息/批次/交接调用 */
  needs?: 'message' | 'batch' | 'call';
  /** 触发磁贴发出的上下文类型 */
  emits?: 'message' | 'batch' | 'call';
  /** 插件节点所属插件名; 内置节点没有该字段 */
  plugin?: string;
  /** 插件声明的连线读取说明, 键为输入端口名 */
  reading?: Record<string, string>;
}

/** flow 类型的编辑器元数据 */
export interface FlowTypeMetadata {
  /** 全部可用节点类型, 内置在前, 插件节点在后 */
  node_types: FlowNodeTypePayload[];
  /** 端口类型的显示名和是否为信号端口 */
  port_types: Record<FlowPortType, { label: string; signal: boolean }>;
  /** 占位结构; 插件全局默认的实际值需读 /config/plugins/{plugin}/flow-nodes */
  defaults: { flow: Record<string, unknown>; plugin: Record<string, Record<string, Record<string, unknown>>> };
  /** 可选的 LLM 配置; ctx 为上下文窗口 (0 表示未知) */
  models: Array<{ name: string; ctx: number; tools: boolean }>;
}

/** 存档里的一个节点 */
export interface FlowDefinitionNode {
  /** 节点实例 id, 流程内唯一 */
  id: string;
  /** 节点类型名 */
  type: string;
  /** 写入时的节点契约版本; 缺省视为 1 */
  version?: number;
  /** 显示名; 缺省时后端用 id */
  title?: string;
  /** 只含显式设置的字段; 继承来的值不写入 */
  config?: Record<string, unknown>;
}

/** 存档里的一条边 */
export interface FlowDefinitionEdge {
  /** 起点 "节点id.输出端口" */
  from: string;
  /** 终点 "节点id.输入端口" */
  to: string;
  /** 用户写的连线注释; 自动生成的读取说明不存档 */
  label?: string;
}

/** 流程级硬预算 */
export interface FlowBudget {
  /** 窗口内允许的模型调用总数 */
  max_calls: number;
  /** 统计窗口分钟数, 默认 60 */
  window_minutes?: number;
  /** all 计 WF 和插件节点调用; wf 只算 WF */
  count?: 'all' | 'wf';
}

/** params.flow 存档格式 */
export interface FlowDefinitionJSON {
  /** 存档格式版本, 当前为 3 */
  version?: number;
  /** 节点列表 */
  nodes: FlowDefinitionNode[];
  /** 边列表 */
  edges?: FlowDefinitionEdge[];
  /** 流程默认: 节点类型名 -> {version, config} */
  defaults?: Record<string, { version?: number; config?: Record<string, unknown> }>;
  /** 画布位置: 节点 id -> 坐标 */
  layout?: Record<string, { x: number; y: number }>;
  /** 硬预算; null 或缺省表示不限 */
  budget?: FlowBudget | null;
}

/** 后端静态检查给出的一个问题 */
export interface FlowIssue {
  /** 稳定问题码, 如 flow_port_required */
  code: string;
  /** error 阻止保存; warning 只提示 */
  level: 'error' | 'warning';
  /** 问题分类, 用于面板分组 */
  category: string;
  /** 面向用户的说明 */
  message: string;
  /** 修复建议 */
  hint?: string;
  /** 相关节点 id */
  node?: string;
  /** 相关边, 格式 "源节点.端口->目标节点.端口" */
  edge?: string;
  /** 相关配置字段名 */
  field?: string;
}

/** 静态检查结果 */
export interface FlowValidateResult {
  /** 没有 error 级问题时为真 */
  ok: boolean;
  /** 全部问题 */
  issues: FlowIssue[];
}

/** 流程会话运行状态里的一条事件 */
export interface FlowRuntimeEvent {
  /** 诊断事件 id; 内存降级记录为空串 */
  event_id: string;
  /** 所属请求 id; 内存降级记录为空串 */
  request_id: string;
  /** 请求内序号; 内存降级记录为 -1 */
  sequence: number;
  /** 事件阶段, 如 fire/blocked/run_end */
  phase: string;
  /** 事件状态 */
  status: string;
  /** 记录时间, Unix 秒 */
  recorded_at: number;
  /** 其余事件字段 */
  metadata: Record<string, unknown>;
}

/** GET /config/edictum/flow/runtime 的返回 */
export interface FlowRuntimeStatus {
  /** 流程名 (命名配置名) */
  flow_name: string;
  /** 会话 id */
  session_id: string;
  /** 对话键 */
  scope_key: string;
  /** 档案起点 (/new 后更新), Unix 秒 */
  history_start: number;
  /** 阈值磁贴 -> 每个 WF 的已提交位置和积压条数 */
  tiles: Record<string, { title: string; wfs: Record<string, { committed: number; backlog: number }> }>;
  /** 状态槽的已提交版本; 不含内容 */
  states: Record<string, { version: number; covers_to: number; committed_at: number; run_id: string }>;
  /** 正在运行的 WF 节点 id */
  running_wfs: string[];
  /** 最近事件, 新的在前 */
  events: FlowRuntimeEvent[];
  /** debug_store 为持久化诊断; memory_fallback 表示诊断未开启或无记录, 仅进程内摘要 */
  events_source: 'debug_store' | 'memory_fallback';
  /** 插件节点实例状态 */
  node_status: Record<string, { ready: boolean; error: string }>;
  /** 配置了 budget 时的硬预算用量; 未配置时为 null */
  budget?: { used: number; max_calls: number; window_minutes: number; count: 'all' | 'wf' } | null;
}

/** 插件节点全局默认 GET/PUT /config/plugins/{plugin}/flow-nodes 的返回 */
export interface PluginFlowNodeDefaults {
  /** 请求成功 */
  ok: boolean;
  /** 已保存的覆盖值, 键为不带插件前缀的节点名; 未覆盖的节点不出现 */
  nodes: Record<string, { version: number; config: Record<string, unknown> }>;
  /** 默认文件的 sha256; 文件不存在时为 "missing" */
  revision: string;
  /** 各节点的配置字段契约 */
  schema: Record<string, Record<string, EdictumPluginConfigField>>;
  /** PUT 成功时为真 */
  saved?: boolean;
}

export interface EdictumPluginConfig {
  name: string;
  config_version?: number;
  migration_state?: Record<string, unknown>;
  enabled?: boolean;
  config?: Record<string, unknown>;
  capabilities?: Record<string, Record<string, boolean>>;
}

export interface EdictumPluginConfigField {
  label?: string;
  option_labels?: Record<string, string>;
  type: string;
  default: unknown;
  description: string;
  options?: string[];
  required?: boolean;
  nullable?: boolean;
  session_overridable?: boolean;
  minimum?: number | null;
  maximum?: number | null;
  integer?: boolean;
  scope?: string;
  /** 分组名; 渲染器按组分段显示, 无分组的字段在最前 */
  group?: string;
  /** 显示条件: 其他字段等于该值 (或命中列表中任意值) 时才显示; 只影响显示, 隐藏字段的值仍保留并由后端校验 */
  visible_when?: Record<string, unknown>;
}

export type PluginCapabilityKind = 'tools' | 'services' | 'handlers' | 'mcp' | 'skills' | 'commands';

export interface PluginCapabilityState {
  name: string;
  enabled: boolean;
  requested_enabled?: boolean;
  description: string;
  exported?: boolean;
  reason?: string;
  replaced_by?: string;
}
export type PluginCapabilitySelection = Partial<Record<PluginCapabilityKind, string[]>>;

export interface PluginInteropDeclarations {
  exports?: PluginCapabilitySelection;
  requires?: Record<string, true | (PluginCapabilitySelection & { optional?: boolean })>;
  replaces?: Record<string, Partial<Record<PluginCapabilityKind, Record<string, string>>>>;
  conflicts?: Record<string, true | PluginCapabilitySelection>;
}

export interface EdictumAvailablePlugin {
  name: string;
  config_version?: number;
  version: string;
  author: string;
  description: string;
  config_schema: Record<string, EdictumPluginConfigField>;
  capabilities: Record<string, Record<string, string>>;
  compatibility?: { satrap?: string };
  applicability?: { session_types?: string[]; platforms?: string[] | '*' };
  interop?: PluginInteropDeclarations;
  /** 插件装在流程 WF 上时的静态声明; 只用于编辑提示, 运行时以实际授权为准 */
  flow_workflow?: { foreground_only: boolean; provides: string[] };
  /** 插件提供的流程节点类型; 没有时为空数组 */
  flow_nodes?: FlowNodeTypePayload[];
}

export interface ManagedPlugin extends EdictumAvailablePlugin {
  source: 'builtin' | 'user';
  usage_count: number;
  edictum_configs: string[];
  chat_enabled: boolean;
}

export interface PluginInstallPreview {
  token: string;
  plugin: EdictumAvailablePlugin;
  expires_in: number;
  expanded_bytes: number;
  file_count: number;
}

export interface PluginRuntimeResult {
  target: string;
  status: 'applied' | 'next_activation' | 'error';
  error?: string;
  sessions: Array<Record<string, unknown>>;
  operation_id?: string;
  confirmation?: 'unconfirmed' | 'not_accepted';
}

export interface GlobalPluginConfig {
  ok: boolean;
  schema: Record<string, EdictumPluginConfigField>;
  config: Record<string, unknown>;
  overrides: Record<string, unknown>;
  revision: string;
  saved?: boolean;
  runtime?: PluginRuntimeResult[];
}

export interface PluginLocationState {
  present: boolean;
  enabled: boolean;
  capabilities: Record<string, Record<string, boolean>>;
}

export interface PluginLocation extends PluginLocationState {
  kind: 'chat' | 'edictum';
  id: string;
  label: string;
  revision: string;
  parent_enabled: boolean;
  availability: { allowed: boolean; message?: string; warnings?: string[] };
}

export interface PluginUsagesResult {
  ok: boolean;
  locations: PluginLocation[];
  saved?: boolean;
  runtime?: PluginRuntimeResult[];
}

export interface PluginRuntimeSnapshot {
  ok: boolean;
  services: Array<{
    target: string;
    status: 'available' | 'stopped' | 'error';
    error?: string;
    instances: Array<{
      platform_id: string;
      session_id: string;
      location_id: string;
      plugin: { name: string; status: string; enabled: boolean; error?: string; drift?: boolean; restart_required?: boolean; capabilities: { applied: Record<string, Record<string, boolean>>; desired: Record<string, Record<string, boolean>>; loaded: Record<string, Record<string, boolean>>; loaded_known?: boolean; effective?: Record<string, PluginCapabilityState[]> } };
    }>;
  }>;
}

export interface EdictumSessionConfig {
  provider: 'edictum';
  edictum_type: string;
  enabled: boolean;
  description: string;
  model_name: string;
  params: Record<string, unknown>;
  plugins: Array<string | EdictumPluginConfig>;
  /** 公开配置正文的 sha256; 更新时作为 expected_revision 防止覆盖他人修改 */
  revision?: string;
}

  export interface DiscoveredSessionClass {
    file_path: string;
    module_name: string;
    class_name: string;
    class_path: string;
    is_async: boolean;
    init_params: Record<string, unknown>;
    error: string;
  }

  export interface RuntimeSession {
    platform_id: string;
    session_id: string;
    route?: {
      scope: string;
      self_id: string;
      group_id: string;
      user_id: string;
      conversation_kind: string;
      conversation_id: string;
      target_label: string | null;
      kind_label: string | null;
      current: boolean;
    } | null;
    active?: boolean;
    provider_name?: string;
    session_type?: string;
    session_type_name?: string;
    created_at: number;
    last_used_at: number;
    message_count: number;
    session_config?: Record<string, unknown>;
    runtime?: {
      plugins?: Array<{
        name: string;
        enabled: boolean;
        status: 'pending' | 'loaded' | 'disabled' | 'error' | string;
        error?: string | null;
        last_error?: string | null;
        last_operation_status?: string;
        drift?: boolean;
        restart_required?: boolean;
        revision?: number;
        capabilities?: {
          applied: Record<string, Record<string, boolean>>;
          desired: Record<string, Record<string, boolean>>;
          effective?: Record<string, PluginCapabilityState[]>;
        };
      }>;
      plugin_summary?: {
        total: number;
        loaded: number;
        errors: number;
        pending: number;
        drift?: number;
        restart_required?: number;
      };
      plugin_revision?: number;
      plugin_fingerprint?: { applied: string; desired: string };
      config?: {
        status: 'applied' | 'restart_pending' | 'restarting' | 'error' | string;
        error?: string | null;
        drift: boolean;
        revision: number;
        applied: Record<string, unknown>;
        desired: Record<string, unknown>;
        applied_fingerprint: string;
        desired_fingerprint: string;
        last_restarted_at?: number | null;
      };
    };
  }

export interface PlatformConfig {
  enable?: boolean;
  id: string;
  type: string;
  session_provider?: string;
  session_type?: string;
  session_bindings?: Record<string, AgentBinding>;
  settings: Record<string, unknown>;
}

export interface Checkpoint {
  checkpoint_id: string;
  namespace: string;
  scope_id: string;
  branch_id?: string;
  name?: string;
  description?: string;
  snapshot_id?: string;
  batch_id?: string;
  state_revision: number;
  position: number;
  checkpoint_kind: string;
  parent_checkpoint_id?: string;
  source: string;
  reason?: string;
  created_at: number;
}

export interface ConversationRecord {
  conversation_id: string;
  title: string;
  message_count: number;
  history_count: number;
  context_ids: string[];
  platform_id?: string;
  platform_type?: string;
  supports_history?: boolean;
  facets?: Record<string, string[]>;
  facet_labels?: Record<string, string>;
  facet_names?: Record<string, string>;
  tags?: string[];
  last_activity_at?: number | null;
  contexts?: { id: string; kind: string; name?: string | null }[];
}

export interface ConversationPlatform {
  id: string;
  type: string;
  label: string;
  type_label: string;
  supports_history: boolean;
}

export interface PlatformArchiveRecord {
  adapter_id: string;
  platform_id: string;
  platform_type: string;
  type_label: string;
  self_id: string;
  conversation_kind: string;
  conversation_kind_label: string;
  chat_id: string;
  label: string;
  revision: number;
  message_count: number;
  last_message_at: number | null;
}

export interface PlatformArchiveCatalog {
  items: PlatformArchiveRecord[];
  total: number;
  conversation_kinds: Array<{ value: string; label: string }>;
  self_ids: string[];
  warnings: Array<{ platform_id: string; error: string }>;
}

export type PlatformArchiveIdentity = Pick<PlatformArchiveRecord, 'platform_id' | 'self_id' | 'conversation_kind' | 'chat_id'>;

export interface PlatformArchiveMessage {
  message_id: string;
  sender_id: string;
  nickname: string;
  card: string;
  message_time: number;
  received_at: number;
  time_source: 'platform' | 'local';
  direction: 'inbound' | 'outbound';
  text: string;
  components: Array<Record<string, unknown>>;
  reply_to_message_id: string | null;
  mentions: string[];
  media: Array<Record<string, unknown>>;
  status: 'active' | 'deleted' | 'recalled' | 'expired';
  source: string;
  verified: boolean;
  truncated: boolean;
}

export interface PlatformArchiveSnapshot {
  ok: boolean;
  items: PlatformArchiveMessage[];
  revision: number;
  scope: Pick<PlatformArchiveRecord, 'adapter_id' | 'self_id' | 'conversation_kind' | 'chat_id' | 'label'>;
  retention_days: number;
  backups: Array<{ backup_id: string; action: 'delete' | 'clear'; created_at: number; expires_at: number }>;
  coverage: { archived_from: number | null; archived_to: number | null; complete: boolean; retention_days: number };
  has_more: boolean;
  next_cursor: string | null;
  truncated: boolean;
}

export interface PlatformArchiveMutation {
  ok: boolean;
  revision: number;
  backup_id?: string;
  expires_at?: number;
  deleted_count?: number;
  restored_count?: number;
  skipped_count?: number;
  deleted_memory_count?: number;
  cleared_memory_proposal_count?: number;
  cancelled_reminder_count?: number;
  sending_reminder_count?: number;
}

export interface ConversationCatalog {
  items: ConversationRecord[];
  total: number;
  facets?: Record<string, { value: string; label: string }[]>;
  warnings?: string[];
  facet_names?: Record<string, string>;
  facets_scope?: 'platform' | 'result';
}

export interface ConversationDataItem {
  index: number;
  role?: string;
  content?: unknown;
  reasoning_content?: string | null;
  protocol_payload?: Record<string, unknown> | null;   // Responses 原始输出项, 编辑内容后由后端移除
  tool_calls?: unknown;
  tool_call_id?: string;
  user_input?: string;
  answer?: string;
  thinking?: string | null;
  created_at?: number;
  [key: string]: unknown;
}

export interface ConversationDataSnapshot {
  ok: boolean;
  conversation_id: string;
  layer: 'context' | 'history';
  revision: string;
  total: number;
  items: ConversationDataItem[];
  source: 'memory' | 'storage';
  read_notice?: string;
  backups: Array<{ id: string; layer: string; reason: string; created_at: number }>;
  saved?: boolean;
  backup_id?: string;
}

export interface ConversationUser extends UserInfo {
  platform_id: string;
  platform_label: string;
  platform_type: string;
  has_profile: boolean;
  revision: string;
  conversation_count: number;
  warning?: string;
  conversations: { conversation_id: string; title: string; exists: boolean; manual: boolean; routed: boolean; last_activity_at: number | null }[];
}

export interface ConversationUserCatalog {
  items: ConversationUser[];
  total: number;
  warnings?: string[];
  new_revision: string;
}

export interface UserInfo {
  user_id: string;
  user_platform?: string;
  user_nickname?: string;
  user_session: string[];
}



export interface BackendConfig {
  api_host: string;
  api_port: number;
  default_session_type: string;
  max_sessions: number;
  idle_timeout: number;
  session_timeout: number | null;
  rate_limit: number;
  rate_burst: number;
    platforms: PlatformConfig[];
    model_config_path?: string;
    data_root?: string;
    session_class_config_path?: string;
    edictum_config_path?: string;
  session_scan_paths?: string[];
}
export interface GroupChatSummary {
  schema_version: 1;
  summary_id: string;
  title: string;
  revision: number;
  state: 'active' | 'source_unavailable';
  created_at: number;
  expires_at: number;
  points: { text: string; source_message_ids: string[] }[];
  resolved_range: { start_time: string; end_time: string };
  selection: { selected_count: number; all_local_matches_selected: boolean; truncated: boolean; reasons: string[] };
  archive_coverage: { platform_history_complete: boolean; archived_from: number | null; archived_to: number | null; retention_days: number };
}

export interface GroupChatSummaryPage {
  ok: boolean;
  items: GroupChatSummary[];
  has_more: boolean;
  next_cursor: string | null;
}
export interface GroupChatSticker {
  sticker_id: string;
  name: string;
  tags: string[];
  collection: string;
  kind: 'image' | 'native';
  content_revision: number;
  enabled: boolean;
  adapter_type: string | null;
  mime_type: string | null;
  size_bytes: number | null;
  width: number | null;
  height: number | null;
}

export interface GroupChatStickerPage {
  ok: boolean;
  items: GroupChatSticker[];
  has_more: boolean;
  next_cursor: string | null;
}

export interface GroupChatStickerSettings {
  ok: boolean;
  collections: string[];
  available_collections: string[];
  revision: number;
}
