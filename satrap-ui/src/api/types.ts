// API 类型定义 - 与后端 Python 类型对应

export interface BackendHealth {
  running: boolean;
  adapters?: Record<string, AdapterInfo>;
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
  lock_api_key?: boolean;
  thinking_field_name?: string | null;
  thinking_fields?: string[];
  thinking_levels?: string[];
  omit_none_thinking_fields?: boolean;
}

export interface EmbeddingConfig {
  name: string;
  model?: string;
  base_url?: string;
  api_key?: string;
  dimensions?: number;
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

export type ModelConfig = LLMConfig | EmbeddingConfig | ReRankConfig;

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
}

export interface EdictumAvailablePlugin {
  name: string;
  version: string;
  author: string;
  description: string;
  config_schema: Record<string, EdictumPluginConfigField>;
  capabilities: Record<string, Record<string, string>>;
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
  id: string;
  type: string;
  session_provider?: string;
  session_type?: string;
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

export interface UserInfo {
  user_id: string;
  user_platform?: string;
  user_nickname?: string;
  user_session: string[];
}

export interface ApiResponse<T = unknown> {
  ok?: boolean;
  error?: string;
  data?: T;
}

export interface LogEntry {
  timestamp: number;
  level: string;
  message: string;
  source?: string;
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
