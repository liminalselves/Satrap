import { useCallback, useEffect, useRef, useState } from 'react';
import { isAxiosError } from 'axios';
import { AlertTriangle, RefreshCw, Send } from 'lucide-react';
import { Badge } from '@/components/kit/Badge';
import { Button } from '@/components/kit/Button';
import { Field, Input, Textarea } from '@/components/kit/Field';
import { Modal } from '@/components/kit/Modal';
import { toast } from '@/components/kit/Toast';
import { RequestDiagnosticsPanel } from '@/components/diagnostics/RequestDiagnosticsPanel';
import { backendApi, type WakeStatusResult } from '@/api/backend';
import { ApiError } from '@/api/client';
import { controlApi } from '@/api/control';
import { createUuid } from '@/utils/uuid';
import type { PlatformConfig } from '@/api/types';

const SETTLED_STATUSES = new Set(['sent', 'partial', 'failed']);
const STATUS_LABELS: Record<string, string> = {
  accepted: '已受理', executing: '执行中', sent: '已送达', partial: '部分送达',
  failed: '失败', unknown: '状态未知',
};

// POST /api/platforms/wake 的稳定拒绝原因码: 后端以 400/409 返回, 前端按码给出可操作文案
const REJECTION_LABELS: Record<string, string> = {
  queue_full: '平台事件队列已满, 请稍后重试',
  request_capacity: '状态存储已满, 请稍后重试',
  store_unavailable: '状态存储不可用, 请稍后重试',
  request_id_conflict: '该 request_id 已用于另一份请求, 请重新提交',
  adapter_changed: '平台连接已变更, 请重试',
  adapter_unavailable: '平台未启用或未连接',
  backend_unavailable: '后端未就绪, 请稍后重试',
  source_unavailable: '目标群或机器人账号不可用',
  message_lookup_failed_or_scope_mismatch: '消息回源失败或不属于该群与成员',
  message_convert_failed: '消息解析失败',
  invalid_fields_or_operator: '请求字段非法',
  invalid_request_id: '请求标识非法',
  invalid_prompt: '唤醒正文非法',
  invalid_message_id_or_conflicting_prompt: '消息 ID 非法或与正文冲突',
  explicit_group_and_route_user_required: '必须指定目标群与会话成员',
};

function rejectionMessage(error: unknown): string {
  if (error instanceof ApiError && error.code) {
    return REJECTION_LABELS[error.code] || `请求被拒绝 (${error.code})`;
  }
  return error instanceof Error ? error.message : '请求失败';
}

function statusTone(status: string): 'green' | 'orange' | 'red' | 'accent' | 'neutral' {
  if (status === 'sent') return 'green';
  if (status === 'partial' || status === 'unknown') return 'orange';
  if (status === 'failed') return 'red';
  if (status === 'accepted' || status === 'executing') return 'accent';
  return 'neutral';
}

function WakeStatusPanel({ requestId, adapterId, onSettled }: { requestId: string; adapterId: string; onSettled: () => void }) {
  const [record, setRecord] = useState<WakeStatusResult | null>(null);
  const [note, setNote] = useState('');
  // 手动刷新计数: 超时后由用户按钮触发一次真实重取, 而不仅仅提示"稍后手动刷新"
  const [manualKey, setManualKey] = useState(0);
  useEffect(() => {
    let cancelled = false;
    let timer = 0;
    const started = Date.now();
    const tick = async () => {
      try {
        const result = await backendApi.getWakeStatus(requestId, adapterId);
        if (cancelled) return;
        setRecord(result);
        if (SETTLED_STATUSES.has(result.status)) {
          onSettled();
          return;
        }
      } catch (error) {
        if (cancelled) return;
        // apiClient 会把 axios 错误包装成 ApiError (保留 status), 不能再按 axios 错误对象判断
        const status = isAxiosError(error) ? error.response?.status : (error as { status?: number } | null)?.status;
        if (status === 404) setNote('未找到该请求记录');
        else if (status === 503) setNote('状态存储降级中, 请稍后刷新; 不确定的结果不会自动重发');
        else setNote('状态查询失败, 将重试');
      }
      if (!cancelled && Date.now() - started < 60000) timer = window.setTimeout(tick, 2000);
      else if (!cancelled) { setNote('查询窗口已结束, 可点刷新继续查看'); }
    };
    tick();
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [requestId, adapterId, onSettled, manualKey]);

  const unknown = record?.status === 'unknown';
  return (
    <div className="tint-green space-y-1 rounded-xl border border-line bg-glass p-3 text-xs" data-testid="wake-status-panel" aria-live="polite">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-fg-3">request_id</span>
        <span className="font-mono break-all text-fg-2">{requestId}</span>
        {record && <Badge tone={statusTone(record.status)} dot>{STATUS_LABELS[record.status] || record.status}</Badge>}
        {unknown && <Badge tone="orange">不确定, 不自动重发</Badge>}
        <button type="button" className="ml-auto text-accent hover:underline" onClick={() => setManualKey((key) => key + 1)}>
          刷新状态
        </button>
      </div>
      {record?.detail && <div className="text-fg-2">明细: {record.detail}</div>}
      {record?.target && <div className="text-fg-3">目标: {record.target}</div>}
      {note && <div className="text-fg-3">{note}</div>}
      {unknown && (
        <div className="text-fg-3">
          结果不确定: 可能已发送也可能未发送, 系统不会自动重发; 请按下方阶段诊断确认后再决定是否重新提交。
        </div>
      )}
      {!record && !note && <div className="text-fg-3">查询中...</div>}
    </div>
  );
}

export interface ManualWakeModalProps {
  open: boolean;
  onClose: () => void;
  backendRunning: boolean;
}

export function ManualWakeModal({ open, onClose, backendRunning }: ManualWakeModalProps) {
  const [platforms, setPlatforms] = useState<PlatformConfig[]>([]);
  const [platformError, setPlatformError] = useState('');
  const [saving, setSaving] = useState(false);
  const [form, setForm] = useState({ adapter_id: '', group_id: '', user_id: '', prompt: '', message_id: '' });
  const [tracking, setTracking] = useState<{ requestId: string; adapterId: string } | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const request = useRef({ content: '', id: '' });

  useEffect(() => {
    let cancelled = false;
    controlApi.listPlatforms().then((result) => {
      if (!result.ok) throw new Error(result.error || '读取平台失败');
      if (!cancelled) {
        setPlatforms((result.platforms || []).filter((item) => item.enable !== false && ['onebot', 'aiocqhttp'].includes(item.type)));
      }
    }).catch((error: unknown) => {
      if (!cancelled) setPlatformError(error instanceof Error ? error.message : '读取平台失败');
    });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    if (open) return;
    setTracking(null);
  }, [open]);

  const handleSettled = useCallback(() => setRefreshKey((key) => key + 1), []);

  const set = (key: keyof typeof form, value: string) => setForm((current) => ({ ...current, [key]: value }));

  const submit = async () => {
    if (!form.adapter_id || !/^[1-9]\d*$/.test(form.group_id) || !/^[1-9]\d*$/.test(form.user_id)) {
      toast.error('请选择平台并填写有效的群号和会话成员 ID');
      return;
    }
    if (form.prompt.trim() && form.message_id.trim()) {
      toast.error('唤醒正文与消息 ID 只能选择一项');
      return;
    }
    const content = JSON.stringify(form);
    if (request.current.content !== content) request.current = { content, id: createUuid() };
    setSaving(true);
    try {
      const result = await backendApi.wakePlatform({ ...form, message_id: form.message_id.trim(), request_id: request.current.id });
      if (result.status === 'accepted') {
        toast.success('唤醒请求已入队, 下方跟踪最终状态');
        setTracking({ requestId: request.current.id, adapterId: form.adapter_id });
      } else if (result.status === 'already_pending') {
        toast.info('此请求已受理, 未重复提交');
        setTracking({ requestId: request.current.id, adapterId: form.adapter_id });
      } else if (result.status === 'no_pending') {
        toast.info('此群与成员范围内没有待处理消息');
        return;
      } else {
        throw new Error(result.reason || '请求被拒绝');
      }
      setRefreshKey((key) => key + 1);
    } catch (error) {
      toast.error('唤醒失败, 输入已保留', rejectionMessage(error));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      open={open}
      onClose={() => { if (!saving) onClose(); }}
      title="手动唤醒群聊"
      description="向 OneBot 群补全待处理消息, 或提交指定正文/消息 ID; 结果通过 request_id 跟踪"
      icon={<Send className="h-[18px] w-[18px]" />}
      size="xl"
      className="tint-green"
      footer={<>
        <span className="mr-auto text-[11px] text-fg-4">
          {backendRunning ? '请求具备幂等标识, 同一输入在途时不会重复入队' : '后端已停止, 无法提交唤醒'}
        </span>
        <Button variant="ghost" onClick={onClose} disabled={saving}>关闭</Button>
        <Button variant="tinted" tint="green" icon={<Send className="h-4 w-4" />} loading={saving} disabled={!backendRunning} onClick={() => void submit()}>提交唤醒</Button>
      </>}
    >
      <div className="space-y-3">
        {platformError && (
          <div role="alert" className="tint-red flex items-start gap-2 rounded-xl border border-[rgb(var(--tint)/0.25)] bg-[rgb(var(--tint)/0.08)] px-3 py-2 text-xs text-fg-2">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 flex-none text-tint" />{platformError}
          </div>
        )}
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="平台实例" required hint="仅列出已启用的 OneBot / aiocqhttp 平台">
            <select className="field w-full" aria-label="平台实例" value={form.adapter_id} onChange={(event) => set('adapter_id', event.target.value)} disabled={saving}>
              <option value="">请选择 OneBot 平台</option>
              {platforms.map((item) => <option key={item.id} value={item.id}>{`${item.id} · ${item.type}`}</option>)}
            </select>
          </Field>
          <div className="grid grid-cols-2 gap-3">
            <Field label="目标群号" required>
              <Input value={form.group_id} onChange={(event) => set('group_id', event.target.value)} inputMode="numeric" placeholder="如: 8823" />
            </Field>
            <Field label="会话成员 ID（用于选择上下文，不代表操作者）" required hint="用于选择上下文">
              <Input value={form.user_id} onChange={(event) => set('user_id', event.target.value)} inputMode="numeric" placeholder="如: 3301" />
            </Field>
          </div>
        </div>
        <Field label="唤醒正文（可选）" hint="正文和消息 ID 均为空时, 处理此范围待处理的文字和图片">
          <Textarea rows={3} value={form.prompt} onChange={(event) => set('prompt', event.target.value)} placeholder="例如: 请总结上面几条消息并回复" />
        </Field>
        <Field label="指定消息 ID（可选，须属于目标群和成员）" hint="与唤醒正文二选一">
          <Input value={form.message_id} onChange={(event) => set('message_id', event.target.value)} placeholder="平台消息 ID" />
        </Field>

        {tracking && (
          <WakeStatusPanel requestId={tracking.requestId} adapterId={tracking.adapterId} onSettled={handleSettled} />
        )}

        <div className="space-y-2 border-t border-line pt-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <h3 className="text-[13px] font-semibold text-fg">请求阶段诊断</h3>
              <p className="mt-0.5 text-[11px] text-fg-3">
                {tracking ? '聚焦当前 request_id, 自动跟踪阶段记录' : '近期手动与自动唤醒的决策、限流、模型和发送记录'}
              </p>
            </div>
            {tracking && (
              <Button size="sm" variant="ghost" icon={<RefreshCw className="h-3.5 w-3.5" />} onClick={() => { setTracking(null); setRefreshKey((key) => key + 1); }}>
                返回近期请求
              </Button>
            )}
          </div>
          <RequestDiagnosticsPanel
            adapterId={tracking?.adapterId || form.adapter_id}
            adapterOptions={platforms.map((item) => item.id)}
            requestId={tracking?.requestId || ''}
            refreshKey={refreshKey}
            allowPolling={!!tracking}
            rejectionsOnlyDefault={!tracking}
            onClearRequest={() => setTracking(null)}
          />
        </div>
      </div>
    </Modal>
  );
}
