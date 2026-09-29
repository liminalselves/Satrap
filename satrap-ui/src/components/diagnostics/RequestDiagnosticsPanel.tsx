import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { Button } from '@/components/ui/Button';
import { Badge } from '@/components/ui/Badge';
import { backendApi } from '@/api/backend';
import type { RequestDiagnosticDetail, RequestDiagnosticRecord, RequestDiagnosticSummary } from '@/api/backend';

// 阶段与原因码的中文展示: 只做翻译, 不改变服务端给出的结论
export const STAGE_LABELS: Record<string, string> = {
  wake_decision: '唤醒决策',
  rate_limit: '限流',
  projection: '补全',
  model: '模型',
  send: '发送',
};

const STATUS_LABELS: Record<string, string> = {
  ok: '正常', sent: '已送达', partial: '部分送达', failed: '失败', unknown: '状态未知',
  dropped: '未处理', skipped: '跳过', resolved: '已解析', disabled: '未启用', unsupported: '不支持',
  too_large: '超出上限', pending: '进行中', submitted: '已提交', blocked: '被拒绝', unavailable: '不可用',
};

const REASON_LABELS: Record<string, string> = {
  no_business_send: '没有业务发送尝试与回执',
  in_flight_unconfirmed: '有已提交未确认的动作, 结果不确定',
  confirmed_prefix: '只有前缀已确认, 后续未确认',
  all_segments_confirmed: '全部分段已确认',
  no_confirmed_send: '没有已确认的发送',
  cancelled: '执行被取消',
  cancelled_before_send: '发送前被取消',
  no_response: '没有收到回执',
  llm_timeout: '模型调用超时',
  completed: '模型调用完成',
  image_download_failed: '图片下载失败',
  image_url_refreshed: '图片地址已刷新',
  image_unavailable: '图片无法获取',
  image_media_budget_dropped: '图片超出本事件媒体预算',
  image_format_unknown: '无法识别图片格式',
  video_download_failed: '视频下载失败',
  video_media_budget_dropped: '视频超出本事件媒体预算',
  video_unsupported_by_implementation: '当前实现不支持视频回源',
  media_unavailable: '媒体无法获取',
  command_candidate: '平台命令入口已冻结正文',
  operator_required: '受保护命令缺少操作员授权',
  not_found: '未找到该请求记录',
  scheduler_unavailable: '调度器未运行, 近期诊断不可用',
  nil: '无',
};

const ATTACHMENT_KIND_LABELS: Record<string, string> = {
  record: '语音', file: '文件', image: '图片', video: '视频', quote: '引用', forward: '转发', attachment: '附件',
};

const STATUS_VARIANTS: Record<string, 'success' | 'warning' | 'error' | 'info' | 'default'> = {
  ok: 'success', sent: 'success', resolved: 'success',
  partial: 'warning', unknown: 'warning', pending: 'info', submitted: 'info', skipped: 'default',
  dropped: 'default', disabled: 'default', failed: 'error', unsupported: 'warning', too_large: 'warning',
  blocked: 'error', unavailable: 'warning',
};

const TERMINAL_SEND_STATUSES = new Set(['sent', 'partial', 'failed', 'skipped']);
const POLL_INTERVAL_MS = 4000;
// 有界轮询: 最多自动刷新这么多轮后停下, 之后只能手动刷新
const POLL_MAX_ROUNDS = 45;
// 仅看拒绝: 命中任一拒绝阶段即保留该请求, 与旧版拒绝记录查询的请求集合一致 (含该请求的执行阶段)
export const REJECTION_STAGE_FILTER = 'wake_decision,rate_limit';
const REJECTION_EMPTY_TEXT = '近期没有被拒绝的请求';

export function statusLabel(status: string): string {
  return STATUS_LABELS[status] || status;
}

export function stageLabel(stage: string): string {
  return STAGE_LABELS[stage] || stage;
}

export function reasonLabel(code: string): string {
  if (!code) return '';
  return REASON_LABELS[code] || code;
}

function statusVariant(status: string): 'success' | 'warning' | 'error' | 'info' | 'default' {
  return STATUS_VARIANTS[status] || 'default';
}

/** 附件/引用/转发的阶段码 (kind:status:reason 与 kind:status) 转中文, 未知码原样显示 */
export function attachmentLabel(code: string): string {
  const parts = code.split(':');
  if (parts.length < 2) return code;
  const kind = ATTACHMENT_KIND_LABELS[parts[0]] || parts[0];
  const status = statusLabel(parts[1]);
  const reason = parts[2] && parts[2] !== 'none' ? reasonLabel(parts[2]) : '';
  return reason ? `${kind} ${status} (${reason})` : `${kind} ${status}`;
}

/** 发送结果里的已确认/未确认/失败分段, 不把 unknown 显示成已送达或确定失败 */
function sendBreakdown(reason: string): string {
  const confirmed = /已确认 (\d+)/.exec(reason);
  const pending = /未确认 (\d+)/.exec(reason);
  const failed = /失败 (\d+)/.exec(reason);
  if (!confirmed || !pending || !failed) return reason;
  return `业务段: 已确认 ${confirmed[1]} · 未确认 ${pending[1]} · 失败 ${failed[1]}`;
}

/** 是否仍在进行中: 尚未到发送阶段, 或发送结果标记为已提交未确认 */
function inFlight(summary: RequestDiagnosticSummary): boolean {
  // 只在决策/限流点结束的请求没有后续执行, 不视为进行中
  const executed = summary.stages.some((stage) => stage !== 'wake_decision' && stage !== 'rate_limit');
  if (!executed) return false;
  if (!summary.stages.includes('send')) return true;
  const status = summary.statuses?.send || '';
  if (TERMINAL_SEND_STATUSES.has(status)) return false;
  return status === 'unknown' && summary.reason_codes.includes('in_flight_unconfirmed');
}

function StageChips({ summary }: { summary: RequestDiagnosticSummary }) {
  if (!summary.stages || summary.stages.length === 0) return <span className="text-text-secondary">无阶段记录</span>;
  return (
    <span className="flex flex-wrap gap-1">
      {summary.stages.map((stage, index) => {
        const status = summary.statuses?.[stage] || '';
        return (
          <Badge key={`${stage}-${index}`} variant={statusVariant(status)}>
            {stageLabel(stage)}{status ? ` · ${statusLabel(status)}` : ''}
          </Badge>
        );
      })}
    </span>
  );
}

function DetailRecords({ detail }: { detail: RequestDiagnosticDetail }) {
  const records = detail.records || [];
  if (records.length === 0) {
    return (
      <p className="text-xs text-text-secondary" data-testid="request-diagnostic-detail-empty">
        {detail.reason === 'scheduler_unavailable'
          ? '调度器未运行, 近期诊断不可用'
          : '该请求的近期诊断已被容量淘汰, 不再可查 (持久账本中的状态仍可查询)'}
      </p>
    );
  }
  return (
    <div className="space-y-1" data-testid="request-diagnostic-detail">
      {detail.truncated && <p className="text-xs text-text-secondary">该请求阶段记录已达上限, 仅保留最近若干条</p>}
      {records.map((record: RequestDiagnosticRecord, index) => (
        <div key={`${record.stage}-${index}`} className="rounded-sm bg-glass p-2 text-xs">
          <div className="flex flex-wrap items-center gap-2">
            <span className="shrink-0 font-mono text-text-secondary">{record.recorded_at.slice(11, 19)}</span>
            <Badge variant="info">{stageLabel(record.stage)}</Badge>
            <Badge variant={statusVariant(record.status)}>{statusLabel(record.status)}</Badge>
            {record.stage === 'send' && record.send_status && (
              <Badge variant={statusVariant(record.send_status)}>回执 {statusLabel(record.send_status)}</Badge>
            )}
            {record.stage === 'send' && record.status === 'unknown' && (
              <Badge variant="warning">不确定, 不自动重发</Badge>
            )}
          </div>
          <p className="mt-1 break-all text-text-primary">{sendBreakdown(record.reason)}</p>
          {record.reason_code && <p className="text-text-secondary">原因码: {record.reason_code} ({reasonLabel(record.reason_code)})</p>}
          {record.attachments && (
            <p className="break-all text-text-secondary">
              附件: {record.attachments.split(',').filter(Boolean).map(attachmentLabel).join('; ')}
            </p>
          )}
          {record.notes && <p className="break-all text-text-secondary">说明: {record.notes}</p>}
          {record.turn_id && <p className="break-all text-text-secondary">发送尝试: {record.turn_id}</p>}
        </div>
      ))}
    </div>
  );
}

interface RequestDiagnosticsPanelProps {
  /** 适配器过滤; 空串表示全部 */
  adapterId?: string;
  /** 可选适配器列表: 提供时渲染平台筛选控件 */
  adapterOptions?: string[];
  onAdapterChange?: (adapterId: string) => void;
  /** 聚焦单个请求 (手动唤醒跟踪) */
  requestId?: string;
  /** 固定阶段过滤 */
  stage?: string;
  /** 未聚焦请求时"仅看拒绝"预设的初始状态 */
  rejectionsOnlyDefault?: boolean;
  /** 提供时, 聚焦请求下渲染"返回近期请求"入口 */
  onClearRequest?: () => void;
  /** 外部触发刷新 */
  refreshKey?: number;
  limit?: number;
  /** 是否允许自动刷新 (有界轮询) */
  allowPolling?: boolean;
}

export function RequestDiagnosticsPanel({
  adapterId = '', adapterOptions, onAdapterChange, requestId = '', stage = '', refreshKey = 0,
  limit = 20, allowPolling = true, rejectionsOnlyDefault = false, onClearRequest,
}: RequestDiagnosticsPanelProps) {
  const [records, setRecords] = useState<RequestDiagnosticSummary[]>([]);
  const [stats, setStats] = useState<{ capacity?: number; records_total?: number }>({});
  const [available, setAvailable] = useState(true);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [polling, setPolling] = useState(false);
  const [pollNote, setPollNote] = useState('');
  const [rejectionsOnly, setRejectionsOnly] = useState(rejectionsOnlyDefault);
  const [expanded, setExpanded] = useState<{ requestId: string; detail: RequestDiagnosticDetail | null } | null>(null);
  const rounds = useRef(0);
  const mounted = useRef(true);

  // 聚焦单个请求时不能叠加拒绝筛选: 正常请求会因过滤而显示为空
  useEffect(() => {
    setRejectionsOnly(rejectionsOnlyDefault);
  }, [adapterId, rejectionsOnlyDefault, requestId]);

  const stageFilter = requestId ? stage : rejectionsOnly ? REJECTION_STAGE_FILTER : stage;

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const result = await backendApi.listRequestDiagnostics({ adapterId, stage: stageFilter, requestId, limit });
      if (!mounted.current) return;
      setRecords(result.records || []);
      setStats({ capacity: result.capacity, records_total: result.records_total });
      setAvailable(result.available !== false);
      setError(result.available === false ? '调度器未运行, 近期诊断不可用' : '');
    } catch (e) {
      if (!mounted.current) return;
      setRecords([]);
      setError('诊断查询失败, 请用刷新按钮重试: ' + (e instanceof Error ? e.message : '请求失败'));
    } finally {
      if (mounted.current) setLoading(false);
    }
  }, [adapterId, limit, requestId, stageFilter]);

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  useEffect(() => { void load(); }, [load, refreshKey]);

  // 有界轮询: 只在进行中的请求存在时自动刷新, 达到轮数上限即停止; 页面隐藏时不计轮数
  const hasInFlight = records.some(inFlight);
  const [pollTick, setPollTick] = useState(0);
  useEffect(() => {
    if (!polling || !allowPolling) return;
    if (rounds.current >= POLL_MAX_ROUNDS) {
      setPolling(false);
      setPollNote(`已自动刷新 ${POLL_MAX_ROUNDS} 次, 停止轮询; 可手动刷新`);
      return;
    }
    const timer = window.setTimeout(() => {
      if (document.hidden) {
        setPollTick((tick) => tick + 1);
        return;
      }
      rounds.current += 1;
      void load();
    }, POLL_INTERVAL_MS);
    return () => window.clearTimeout(timer);
  }, [allowPolling, load, pollTick, polling, records]);
  useEffect(() => {
    if (polling && !hasInFlight) {
      setPolling(false);
      setPollNote('没有进行中的请求, 已停止自动刷新');
    }
  }, [hasInFlight, polling]);

  const togglePolling = () => {
    rounds.current = 0;
    setPollNote('');
    setPolling((current) => !current);
  };

  const toggleDetail = async (id: string) => {
    if (expanded?.requestId === id) {
      setExpanded(null);
      return;
    }
    setExpanded({ requestId: id, detail: null });
    try {
      const detail = await backendApi.getRequestDiagnostic(id, adapterId);
      if (!mounted.current) return;
      setExpanded({ requestId: id, detail });
    } catch (e) {
      if (!mounted.current) return;
      setExpanded({ requestId: id, detail: { request_id: id, reason: e instanceof Error ? e.message : 'detail_failed' } });
    }
  };

  const filters = useMemo(() => (adapterOptions || []).filter(Boolean).sort(), [adapterOptions]);

  return (
    <div className="space-y-2 rounded-sm bg-glass p-3" data-testid="request-diagnostics-panel">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span className="font-medium text-text-primary">请求阶段诊断</span>
        {onAdapterChange && filters.length > 0 && (
          <select
            aria-label="诊断平台筛选"
            className="glass-input w-36 text-xs"
            value={adapterId}
            onChange={(event) => onAdapterChange(event.target.value)}
          >
            <option value="">全部平台</option>
            {filters.map((id) => <option key={id} value={id}>{id}</option>)}
          </select>
        )}
        {!onAdapterChange && adapterId && <Badge variant="info">{adapterId}</Badge>}
        {requestId && <Badge variant="info">仅此请求 {requestId}</Badge>}
        <label className="flex items-center gap-1 text-text-secondary">
          <input
            type="checkbox" className="h-3 w-3 accent-accent" checked={!requestId && rejectionsOnly}
            disabled={!!requestId} onChange={(event) => setRejectionsOnly(event.target.checked)}
            aria-label="仅看拒绝" data-testid="diagnostics-rejections-only"
          />
          仅看拒绝
        </label>
        {requestId && onClearRequest && (
          <Button type="button" variant="ghost" className="text-xs" onClick={onClearRequest} data-testid="diagnostics-clear-request">
            返回近期请求
          </Button>
        )}
        <Button type="button" variant="ghost" className="text-xs" onClick={() => void load()} disabled={loading} data-testid="diagnostics-refresh">
          <RefreshCw className={`h-3 w-3 mr-1 ${loading ? 'animate-spin' : ''}`} />
          刷新诊断
        </Button>
        {allowPolling && (
          <label className="flex items-center gap-1 text-text-secondary">
            <input type="checkbox" className="h-3 w-3 accent-accent" checked={polling} onChange={togglePolling} aria-label="有界自动刷新" />
            自动刷新 (最多 {POLL_MAX_ROUNDS} 次)
          </label>
        )}
        <span className="text-text-secondary">
          近期保留 {stats.records_total ?? 0} 条 / 每平台 {stats.capacity ?? '—'} 个请求
        </span>
      </div>

      {pollNote && <p className="text-xs text-text-secondary" data-testid="diagnostics-poll-note">{pollNote}</p>}
      {error && (
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <span className="text-error" data-testid="diagnostics-error">{error}</span>
          <Button type="button" variant="default" className="text-xs" onClick={() => void load()} disabled={loading}>实际重试</Button>
        </div>
      )}
      {!error && records.length === 0 && (
        <p className="text-xs text-text-secondary" data-testid="diagnostics-empty">
          {!available ? '调度器未运行, 近期诊断不可用' : !requestId && rejectionsOnly ? REJECTION_EMPTY_TEXT : '近期没有请求诊断记录'}
        </p>
      )}

      {records.length > 0 && (
        <div className="max-h-72 space-y-1 overflow-y-auto">
          {records.map((summary) => (
            <div key={`${summary.adapter_id}:${summary.request_id}`} className="rounded-sm bg-glass p-2 text-xs" data-testid="diagnostics-row">
              <div className="flex flex-wrap items-center gap-2">
                <span className="shrink-0 font-mono text-text-secondary">{summary.recorded_at.slice(11, 19)}</span>
                <span className="text-text-secondary">{summary.adapter_id}</span>
                <StageChips summary={summary} />
                {inFlight(summary) && <Badge variant="info">进行中</Badge>}
                {summary.statuses?.send === 'unknown' && <Badge variant="warning">不确定, 不自动重发</Badge>}
                <button
                  type="button"
                  className="ml-auto text-accent hover:underline"
                  aria-expanded={expanded?.requestId === summary.request_id}
                  onClick={() => void toggleDetail(summary.request_id)}
                >
                  {expanded?.requestId === summary.request_id ? '收起详情' : '展开详情'}
                </button>
              </div>
              <p className="mt-1 break-all text-text-primary">
                {summary.session_id} · {summary.reason_codes.filter(Boolean).map(reasonLabel).join('; ') || '无原因码'}
              </p>
              <p className="break-all text-text-secondary">
                请求 {summary.request_id}
                {summary.message_id ? ` · 消息 ${summary.message_id}` : ''}
                {summary.turn_id ? ` · 发送尝试 ${summary.turn_id}` : ''}
              </p>
              {summary.attachments && (
                <p className="break-all text-text-secondary">
                  附件: {summary.attachments.split(',').filter(Boolean).map(attachmentLabel).join('; ')}
                </p>
              )}
              {summary.notes && <p className="break-all text-text-secondary">说明: {summary.notes}</p>}
              {expanded?.requestId === summary.request_id && (
                <div className="mt-2 border-t border-border/50 pt-2">
                  {expanded.detail ? <DetailRecords detail={expanded.detail} /> : <p className="text-xs text-text-secondary">明细查询中...</p>}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
      <p className="text-xs text-text-secondary">
        按请求关联的近期阶段结果 (近期诊断有界保留, 浏览器刷新后可重新定位; 手动请求与已提交发送尝试以持久账本为准)
      </p>
    </div>
  );
}
