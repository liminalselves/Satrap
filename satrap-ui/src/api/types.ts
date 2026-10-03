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

export type ModelConfig = LLMConfig | EmbeddingConfig | ReRankConfig | ASRConfig;
export type ModelType = 'llm' | 'embedding' | 'rerank' | 'asr';

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
}

export interface EdictumPluginConfig {
  name: string;
  enabled?: boolean;
  config?: Record<string, unknown>;
  capabilities?: Record<string, Record<string, boolean>>;
}

export interface EdictumPluginConfigField {
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
}

export interface EdictumAvailablePlugin {
  name: string;
  version: string;
  author: string;
  description: string;
  config_schema: Record<string, EdictumPluginConfigField>;
  capabilities: Record<string, Record<string, string>>;
  compatibility?: { satrap?: string };
  applicability?: { session_types?: string[]; platforms?: string[] | '*' };
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
      plugin: { name: string; status: string; enabled: boolean; error?: string; drift?: boolean; restart_required?: boolean; capabilities: { applied: Record<string, Record<string, boolean>>; desired: Record<string, Record<string, boolean>>; loaded: Record<string, Record<string, boolean>>; loaded_known?: boolean } };
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
}

export interface ConversationCatalog {
  items: ConversationRecord[];
  total: number;
  facets?: Record<string, { value: string; label: string }[]>;
  warnings?: string[];
  facet_names?: Record<string, string>;
}

export interface ConversationDataItem {
  index: number;
  role?: string;
  content?: unknown;
  reasoning_content?: string | null;
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
  llm_timeout: number;
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
