/* 系统设置页使用的本地类型与纯函数。生产字段与应用配置保持一致。 */

export interface CommonConfig {
  host: string;
  port: number;
  defaultSessionType: string;
  maxSessions: number;
  idleTimeout: number;
  sessionTimeout: number | null;
  rateLimit: number;
  rateBurst: number;
}

export const initialCommonConfig: CommonConfig = {
  host: '127.0.0.1',
  port: 19870,
  defaultSessionType: 'agent',
  maxSessions: 100,
  idleTimeout: 3600,
  sessionTimeout: 120,
  rateLimit: 10,
  rateBurst: 20,
};

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

const asNumber = (value: unknown, fallback: number) =>
  typeof value === 'number' && Number.isFinite(value) ? value : fallback;

/** 从完整配置读取常用配置; 缺失字段回退到当前草稿的默认值 */
export function extractCommonConfig(data: unknown, fallback: CommonConfig = initialCommonConfig): CommonConfig {
  if (!isRecord(data)) return fallback;
  const api = isRecord(data.api) ? data.api : {};
  return {
    host: typeof api.host === 'string' && api.host.trim() ? api.host.trim() : fallback.host,
    port: asNumber(api.port, fallback.port),
    defaultSessionType: typeof data.default_session_type === 'string' ? data.default_session_type : fallback.defaultSessionType,
    maxSessions: asNumber(data.max_sessions, fallback.maxSessions),
    idleTimeout: asNumber(data.idle_timeout, fallback.idleTimeout),
    sessionTimeout: 'session_timeout' in data
      ? (data.session_timeout === null ? null : asNumber(data.session_timeout, fallback.sessionTimeout ?? 0))
      : fallback.sessionTimeout,
    rateLimit: asNumber(data.rate_limit, fallback.rateLimit),
    rateBurst: asNumber(data.rate_burst, fallback.rateBurst),
  };
}

/** 把常用配置合并回完整配置对象, 保留管理员、平台等其它区段 */
export function mergeCommonConfig(config: Record<string, unknown>, value: CommonConfig): Record<string, unknown> {
  const api = isRecord(config.api) ? config.api : {};
  return {
    ...config,
    api: { ...api, host: value.host, port: value.port },
    default_session_type: value.defaultSessionType,
    max_sessions: value.maxSessions,
    idle_timeout: value.idleTimeout,
    session_timeout: value.sessionTimeout,
    rate_limit: value.rateLimit,
    rate_burst: value.rateBurst,
  };
}

export function formatUptime(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds)) return '—';
  const s = Math.max(0, Math.floor(seconds));
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${pad(Math.floor(s / 3600))}:${pad(Math.floor((s % 3600) / 60))}:${pad(s % 60)}`;
}

export function formatUnixTime(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds)) return '—';
  return new Date(seconds * 1000).toLocaleString('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
  });
}

/* ---------- YAML 差异 (本地展示用) ---------- */

export interface DiffRow {
  kind: 'same' | 'add' | 'del';
  text: string;
  oldLine?: number;
  newLine?: number;
}

export function diffLines(before: string, after: string): DiffRow[] {
  const a = before.replace(/\r\n/g, '\n').split('\n');
  const b = after.replace(/\r\n/g, '\n').split('\n');
  const n = a.length;
  const m = b.length;
  const dp: number[][] = Array.from({ length: n + 1 }, () => new Array<number>(m + 1).fill(0));
  for (let i = 1; i <= n; i++) {
    for (let j = 1; j <= m; j++) {
      dp[i][j] = a[i - 1] === b[j - 1]
        ? dp[i - 1][j - 1] + 1
        : Math.max(dp[i - 1][j], dp[i][j - 1]);
    }
  }
  const reversed: DiffRow[] = [];
  let i = n;
  let j = m;
  while (i > 0 || j > 0) {
    if (i > 0 && j > 0 && a[i - 1] === b[j - 1]) {
      reversed.push({ kind: 'same', text: a[i - 1], oldLine: i, newLine: j });
      i--; j--;
    } else if (i > 0 && (j === 0 || dp[i - 1][j] >= dp[i][j - 1])) {
      reversed.push({ kind: 'del', text: a[i - 1], oldLine: i });
      i--;
    } else {
      reversed.push({ kind: 'add', text: b[j - 1], newLine: j });
      j--;
    }
  }
  return reversed.reverse();
}

export function diffSummary(rows: DiffRow[]): { added: number; removed: number } {
  return {
    added: rows.filter((row) => row.kind === 'add').length,
    removed: rows.filter((row) => row.kind === 'del').length,
  };
}

/** 仅保留差异附近的上下文 */
export function compactDiff(rows: DiffRow[], context = 3): DiffRow[] {
  const keep = new Set<number>();
  rows.forEach((row, index) => {
    if (row.kind === 'same') return;
    for (let i = Math.max(0, index - context); i <= Math.min(rows.length - 1, index + context); i++) keep.add(i);
  });
  return rows.filter((_, index) => keep.has(index));
}
