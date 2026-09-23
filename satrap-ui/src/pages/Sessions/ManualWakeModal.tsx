import { useCallback, useEffect, useRef, useState } from 'react';
import { isAxiosError } from 'axios';
import { FormModal } from '@/components/common';
import { Badge } from '@/components/ui/Badge';
import { toast } from '@/components/ui/Toast';
import { backendApi } from '@/api/backend';
import { controlApi } from '@/api/control';
import type { WakeRejectionRecord, WakeStatusResult } from '@/api/backend';
import type { FormField } from '@/components/common';
import type { PlatformConfig } from '@/api/types';

const SETTLED_STATUSES = new Set(['sent', 'partial', 'failed']);
const STATUS_LABELS: Record<string, string> = {
  accepted: '已受理', executing: '执行中', sent: '已送达', partial: '部分送达',
  failed: '失败', unknown: '状态未知',
};

function statusVariant(status: string): 'success' | 'warning' | 'error' | 'info' | 'default' {
  if (status === 'sent') return 'success';
  if (status === 'partial' || status === 'unknown') return 'warning';
  if (status === 'failed') return 'error';
  return 'info';
}

function WakeStatusPanel({ requestId, adapterId, onSettled }: { requestId: string; adapterId: string; onSettled: () => void }) {
  const [record, setRecord] = useState<WakeStatusResult | null>(null);
  const [note, setNote] = useState('');
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
        if (isAxiosError(error) && error.response?.data && typeof error.response.data === 'object') {
          const reason = (error.response.data as { reason?: string }).reason;
          setNote(reason === 'not_found' ? '未找到该请求记录' : reason === 'store_degraded' ? '状态存储降级中' : '状态存储不可用');
        } else {
          setNote('状态查询失败, 将重试');
        }
      }
      if (!cancelled && Date.now() - started < 60000) timer = window.setTimeout(tick, 2000);
      else if (!cancelled) setNote('查询超时, 请稍后手动刷新');
    };
    tick();
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [requestId, adapterId, onSettled]);

  return (
    <div className="rounded-sm bg-glass p-3 text-xs space-y-1" data-testid="wake-status-panel">
      <div className="flex items-center gap-2">
        <span className="text-text-secondary">request_id</span>
        <span className="font-mono break-all">{requestId}</span>
        {record && <Badge variant={statusVariant(record.status)}>{STATUS_LABELS[record.status] || record.status}</Badge>}
      </div>
      {record?.detail && <div className="text-text-secondary">明细: {record.detail}</div>}
      {record?.target && <div className="text-text-secondary">目标: {record.target}</div>}
      {note && <div className="text-text-secondary">{note}</div>}
      {!record && !note && <div className="text-text-secondary">查询中...</div>}
    </div>
  );
}

function WakeRejectionsPanel({ refreshKey }: { refreshKey: number }) {
  const [records, setRecords] = useState<WakeRejectionRecord[]>([]);
  useEffect(() => {
    let cancelled = false;
    backendApi.listWakeRejections(undefined, 10)
      .then((result) => { if (!cancelled) setRecords(result.records || []); })
      .catch(() => { if (!cancelled) setRecords([]); });
    return () => { cancelled = true; };
  }, [refreshKey]);
  if (records.length === 0) return <div className="text-xs text-text-secondary">近期没有唤醒决策/限流拒绝记录</div>;
  return (
    <div className="max-h-40 space-y-1 overflow-y-auto rounded-sm bg-glass p-2 text-xs" data-testid="wake-rejections-panel">
      {records.map((item, index) => (
        <div key={index} className="flex items-start gap-2">
          <span className="shrink-0 font-mono text-text-secondary">{item.recorded_at.slice(11, 19)}</span>
          <Badge variant="default">{item.stage === 'rate_limit' ? '限流' : '决策'}</Badge>
          <span className="break-all text-text-primary">{item.session_id} · {item.reason}</span>
        </div>
      ))}
    </div>
  );
}

export function ManualWakeModal({ onClose }: { onClose: () => void }) {
  const [platforms, setPlatforms] = useState<PlatformConfig[]>([]);
  const [saving, setSaving] = useState(false);
  const [form, setForm] = useState({ adapter_id: '', group_id: '', user_id: '', prompt: '', message_id: '' });
  const [tracking, setTracking] = useState<{ requestId: string; adapterId: string } | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const request = useRef({ content: '', id: '' });
  useEffect(() => {
    let cancelled = false;
    controlApi.listPlatforms().then((result) => {
      if (!result.ok) throw new Error(result.error || '读取平台失败');
      if (!cancelled) setPlatforms((result.platforms || []).filter((item) => item.enable !== false && ['onebot', 'aiocqhttp'].includes(item.type)));
    }).catch((error) => toast('error', error.message));
    return () => { cancelled = true; };
  }, []);

  const handleSettled = useCallback(() => setRefreshKey((key) => key + 1), []);

  const submit = async () => {
    if (!form.adapter_id || !/^[1-9]\d*$/.test(form.group_id) || !/^[1-9]\d*$/.test(form.user_id)) {
      toast('error', '请选择平台并填写有效的群号和会话成员 ID');
      return;
    }
    if (form.prompt.trim() && form.message_id.trim()) {
      toast('error', '唤醒正文与消息 ID 只能选择一项');
      return;
    }
    const content = JSON.stringify(form);
    if (request.current.content !== content) request.current = { content, id: crypto.randomUUID() };
    setSaving(true);
    try {
      const result = await backendApi.wakePlatform({ ...form, message_id: form.message_id.trim(), request_id: request.current.id });
      if (result.status === 'accepted') {
        toast('success', '唤醒请求已入队, 下方跟踪最终状态');
        setTracking({ requestId: request.current.id, adapterId: form.adapter_id });
      } else if (result.status === 'already_pending') {
        toast('info', '此请求已受理, 未重复提交');
        setTracking({ requestId: request.current.id, adapterId: form.adapter_id });
      } else if (result.status === 'no_pending') {
        toast('info', '此群与成员范围内没有待处理正文');
        return;
      } else {
        throw new Error(result.reason || '请求被拒绝');
      }
      setRefreshKey((key) => key + 1);
    } catch (error) {
      toast('error', '唤醒失败, 输入已保留: ' + (error instanceof Error ? error.message : '请求失败'));
    } finally {
      setSaving(false);
    }
  };

  const fields: FormField[] = [
    { key: 'adapter_id', label: '平台实例', type: 'select', options: [{ value: '', label: '请选择 OneBot 平台' }, ...platforms.map((item) => ({ value: item.id, label: item.id }))] },
    { key: 'group_id', label: '目标群号', type: 'text' },
    { key: 'user_id', label: '会话成员 ID（用于选择上下文，不代表操作者）', type: 'text' },
    { key: 'prompt', label: '唤醒正文（可选）', type: 'textarea', rows: 4, placeholder: '正文和消息 ID 均为空时, 处理此范围待处理正文' },
    { key: 'message_id', label: '指定消息 ID（可选，须属于目标群和成员）', type: 'text' },
  ];
  if (tracking) {
    fields.push({
      key: 'wake_status', label: '请求状态跟踪', type: 'custom',
      render: () => <WakeStatusPanel requestId={tracking.requestId} adapterId={tracking.adapterId} onSettled={handleSettled} />,
    });
  }
  fields.push({
    key: 'wake_rejections', label: '近期拒绝记录', type: 'custom',
    render: () => <WakeRejectionsPanel refreshKey={refreshKey} />,
  });

  return <FormModal open onClose={onClose} title="手动唤醒群聊" size="lg" loading={saving} submitText="提交唤醒"
    values={form} onChange={(key, value) => setForm((current) => ({ ...current, [key]: String(value) }))} onSubmit={submit}
    fields={fields} />;
}
