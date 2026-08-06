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
  name: string;
  class_path: string;
  description?: string;
  enabled: boolean;
  model_key?: string;
  params?: Record<string, unknown>;
}

export interface PlatformConfig {
  id: string;
  type: string;
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
  session_class_config_path?: string;
  session_db_path?: string;
  user_db_path?: string;
  session_scan_paths?: string[];
}
