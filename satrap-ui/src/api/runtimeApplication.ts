import { apiClient, ApiError } from './client';
import { getApiBaseUrl } from '@/utils/constants';

export interface ApplicationStage {
  platform_id: string;
  session_id: string;
  status: string;
  phase: string;
  phase_elapsed_ms: number;
  timings_ms: Record<string, number>;
}

export interface ApplicationSnapshot<T> {
  operation_id: string;
  status: 'running' | 'completed' | 'failed' | 'cancelled';
  created_at: number;
  elapsed_ms: number;
  stage: ApplicationStage;
  instances: ApplicationStage[];
  instances_limited: boolean;
  result: T | null;
  error: string;
}

export class ApplicationUnconfirmedError extends Error {
  constructor(message: string, public readonly code?: 'query_stopped') {
    super(message);
    this.name = 'ApplicationUnconfirmedError';
  }
}

const PHASE_LABELS: Record<string, string> = {
  queued: '等待开始', config_read: '读取配置', waiting_lock: '等待会话空闲', install: '安装插件',
  uninstall: '卸载插件', model_build: '创建模型客户端', candidate_prepare: '准备新会话',
  source_check: '核对配置是否变化', context_sync: '同步上下文', applying: '应用配置',
  rollback: '恢复原插件', finished: '已结束', dispatch: '提交应用请求', awaiting_runtime: '等待运行服务状态',
};

export function applicationPhaseLabel(phase: string): string {
  return PHASE_LABELS[phase] || '应用中';
}

function delay(milliseconds: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) { reject(new ApplicationUnconfirmedError('状态查询已停止, 生效状态未确认', 'query_stopped')); return; }
    const finish = () => { signal?.removeEventListener('abort', abort); resolve(); };
    const timer = setTimeout(finish, milliseconds);
    const abort = () => { clearTimeout(timer); signal?.removeEventListener('abort', abort); reject(new ApplicationUnconfirmedError('状态查询已停止, 生效状态未确认', 'query_stopped')); };
    signal?.addEventListener('abort', abort, { once: true });
  });
}

function waitForVisibility(deadline: number, signal?: AbortSignal): Promise<void> {
  if (typeof document === 'undefined' || !document.hidden) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const cleanup = () => { clearTimeout(timer); document.removeEventListener('visibilitychange', changed); signal?.removeEventListener('abort', abort); };
    const changed = () => { if (!document.hidden) { cleanup(); resolve(); } };
    const abort = () => { cleanup(); reject(new ApplicationUnconfirmedError('状态查询已停止, 生效状态未确认', 'query_stopped')); };
    const timer = setTimeout(() => { cleanup(); reject(new ApplicationUnconfirmedError('等待应用超过 120 秒, 请查询任务状态')); }, Math.max(0, deadline - Date.now()));
    document.addEventListener('visibilitychange', changed);
    signal?.addEventListener('abort', abort, { once: true });
    if (signal?.aborted) abort();
  });
}

/**
 * 总观察截止覆盖认证及在途请求, 仅取消本次 HTTP 观察
 * 参数:
 * - timeout: 本次观察的剩余毫秒数
 * - signal: 页面生命周期, 取消时保留独立的停止原因
 * - action: 使用本次取消信号提交或观察, 不撤销后端已接受任务
 * 返回:
 * - 实际结果, 或明确的等待超时/页面停止错误
 */
async function applicationDeadline<T>(timeout: number, signal: AbortSignal | undefined, action: (signal: AbortSignal) => Promise<T>): Promise<T> {
  if (signal?.aborted) throw new ApplicationUnconfirmedError('状态查询已停止, 生效状态未确认', 'query_stopped');
  if (timeout <= 0) throw new ApplicationUnconfirmedError('等待应用超过 120 秒, 请查询任务状态');
  const controller = new AbortController();
  let stop = () => controller.abort();
  let timer: ReturnType<typeof setTimeout> | undefined;
  const interrupted = new Promise<never>((_resolve, reject) => {
    stop = () => { reject(new ApplicationUnconfirmedError('状态查询已停止, 生效状态未确认', 'query_stopped')); controller.abort(); };
    signal?.addEventListener('abort', stop, { once: true });
    timer = setTimeout(() => { reject(new ApplicationUnconfirmedError('等待应用超过 120 秒, 请查询任务状态')); controller.abort(); }, timeout);
  });
  try { return await Promise.race([action(controller.signal), interrupted]); }
  finally { if (timer !== undefined) clearTimeout(timer); signal?.removeEventListener('abort', stop); controller.abort(); }
}

export class RuntimeApplication<T> {
  readonly id = crypto.randomUUID();
  readonly baseUrl: string;
  private readonly currentService: () => string;
  private readonly queryPrefix: string;
  private readonly transport: ReturnType<typeof apiClient.forService>;
  private started = false;

  constructor(private readonly route: string, private readonly body: Record<string, unknown>,
    private readonly isResult: (value: unknown) => value is T, readonly label = '',
    options: { currentService?: () => string; queryPrefix?: string } = {}) {
    this.currentService = options.currentService || getApiBaseUrl;
    this.baseUrl = this.currentService().replace(/\/$/, '');
    this.queryPrefix = options.queryPrefix || '/api/runtime-operations/';
    this.transport = apiClient.forService(this.baseUrl, this.currentService);
  }

  private checkService(signal?: AbortSignal) {
    if (signal?.aborted) throw new ApplicationUnconfirmedError('状态查询已停止, 生效状态未确认', 'query_stopped');
    if (this.currentService().replace(/\/$/, '') !== this.baseUrl) throw new ApplicationUnconfirmedError('后端地址已变化, 请回到原服务查询应用状态');
  }

  private snapshot(value: unknown): ApplicationSnapshot<T> {
    if (!value || typeof value !== 'object') throw new ApplicationUnconfirmedError('应用状态响应无效');
    const item = value as ApplicationSnapshot<T>;
    if (item.operation_id !== this.id || !['running', 'completed', 'failed', 'cancelled'].includes(item.status)
      || !item.stage || typeof item.stage.phase !== 'string' || !Array.isArray(item.instances)
      || item.instances.some((stage) => !stage || typeof stage.phase !== 'string')
      || item.result !== null && !this.isResult(item.result)) {
      throw new ApplicationUnconfirmedError('应用状态与本次任务不匹配或响应无效');
    }
    return item;
  }

  async query(signal?: AbortSignal): Promise<ApplicationSnapshot<T>> {
    this.checkService(signal);
    try {
      const value = await this.transport.get<unknown>(`${this.queryPrefix}${encodeURIComponent(this.id)}`, signal);
      this.checkService(signal);
      return this.snapshot(value);
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) {
        throw new ApplicationUnconfirmedError('任务尚未接受, 服务已重启或记录已过期, 生效状态未确认');
      }
      throw error;
    }
  }

  async start(onProgress: (snapshot: ApplicationSnapshot<T>) => void, signal?: AbortSignal): Promise<T> {
    if (this.started) throw new ApplicationUnconfirmedError('本任务已提交过, 请查询状态, 不要重复提交');
    this.checkService(signal);
    this.started = true;
    return applicationDeadline(120000, signal, async (observing) => {
      let value: unknown;
      const startedAt = Date.now();
      try {
        value = await this.transport.post<unknown>(this.route, { ...this.body, operation_id: this.id }, observing);
      } catch (error) {
        this.checkService(observing);
        if (error instanceof ApiError && error.status !== undefined) throw error;
        value = await this.query(observing);
      }
      this.checkService(observing);
      if (this.isResult(value)) return value;
      return this.observe(this.snapshot(value), onProgress, observing, Math.max(0, 120000 - (Date.now() - startedAt)));
    });
  }

  async observe(initial: ApplicationSnapshot<T>, onProgress: (snapshot: ApplicationSnapshot<T>) => void,
    signal?: AbortSignal, timeout = 120000): Promise<T> {
    if (initial.status !== 'running') {
      this.checkService(signal); onProgress(initial);
      if (initial.result !== null) return initial.result;
      throw new ApplicationUnconfirmedError(initial.error || '任务已结束但没有可确认的应用结果');
    }
    return applicationDeadline(timeout, signal, async (observing) => {
      let current = initial;
      const deadline = Date.now() + timeout;
      for (;;) {
        this.checkService(observing);
        onProgress(current);
        if (current.status !== 'running') {
          if (current.result !== null) return current.result;
          throw new ApplicationUnconfirmedError(current.error || '任务已结束但没有可确认的应用结果');
        }
        await delay(Math.min(1000, Math.max(1, deadline - Date.now())), observing);
        await waitForVisibility(deadline, observing);
        this.checkService(observing);
        current = await this.query(observing);
      }
    });
  }
}
