import { useCallback, useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { ApiError } from '@/api/client';
import { groupApi, type GroupDiagnostic, type GroupEvent, type GroupPolicyValue } from '@/api/groups';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { useDirtyGuard } from '@/hooks/useDirtyGuard';
import { useGroupContext } from './GroupLayout';

const labels: Record<string, string> = {
  group_increase: '成员入群', group_decrease: '成员退群', group_recall: '消息撤回',
  group_ban: '群禁言', group_upload: '群文件上传', group_admin: '管理员变更',
  group_card: '群昵称变更', group_request: '加群请求或邀请',
};

function errorText(error: unknown): string {
  if (error instanceof ApiError) return `${error.message}${error.code ? ` (${error.code})` : ''}`;
  return error instanceof Error ? error.message : '读取失败';
}

export function GroupEvents() {
  const { adapterId, groupId, account, isRunning, historical, config, setConfig, reload } = useGroupContext();
  const [params, setParams] = useSearchParams();
  const eventLimit = params.get('eventLimit') === '100' ? 100 : 50;
  const stage = params.get('stage') || '';
  const requestId = params.get('requestId') || '';
  const eventConfig = config.explicit.events;
  const fromConfig = useCallback(() => Object.fromEntries(Object.entries(eventConfig || {}).map(([key, item]) =>
    [key, item.mode === 'value' ? String(item.value) : 'inherit'])), [eventConfig]);
  const [draft, setDraft] = useState<Record<string, string>>(fromConfig);
  const [baseline, setBaseline] = useState(JSON.stringify(fromConfig()));
  const [events, setEvents] = useState<GroupEvent[]>([]);
  const [diagnostics, setDiagnostics] = useState<GroupDiagnostic[]>([]);
  const [detail, setDetail] = useState<Array<Record<string, unknown>> | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [conflict, setConflict] = useState(false);
  const dirty = JSON.stringify(draft) !== baseline;
  useDirtyGuard(dirty);
  useEffect(() => {
    if (dirty) return;
    const fresh = fromConfig(); setDraft(fresh); setBaseline(JSON.stringify(fresh));
  }, [config.revision, config.account, dirty, fromConfig]);
  useEffect(() => {
    if (!isRunning || !account) return;
    let live = true;
    Promise.all([
      groupApi.events(adapterId, groupId, account, eventLimit),
      groupApi.diagnostics(adapterId, groupId, account, stage, requestId),
    ]).then(([eventResult, diagnosticResult]) => {
      if (!live) return;
      setEvents(eventResult.items); setDiagnostics(diagnosticResult.records); setError('');
    }).catch((caught) => { if (live) setError(errorText(caught)); });
    return () => { live = false; };
  }, [adapterId, groupId, account, isRunning, eventLimit, stage, requestId, refreshKey]);
  useEffect(() => {
    if (!isRunning || error) return;
    const interval = window.setInterval(() => {
      if (document.visibilityState === 'visible') setRefreshKey((value) => value + 1);
    }, 5_000);
    return () => window.clearInterval(interval);
  }, [isRunning, error]);
  const changeParam = (key: string, value: string) => {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value); else next.delete(key);
    setParams(next);
  };
  const save = async () => {
    if (historical || busy || !dirty) return;
    const values: Record<string, GroupPolicyValue> = {};
    for (const [key, value] of Object.entries(draft)) {
      if (value !== 'inherit') values[key] = { mode: 'value', value: value === 'true' };
    }
    setBusy(true); setError('');
    try {
      const saved = await groupApi.saveConfig(adapterId, groupId, {
        expected_self_id: account, expected_revision: config.revision,
        base_revision: config.base_revision, section: 'events', values,
      }, isRunning);
      setConfig(saved);
      const fresh = Object.fromEntries(Object.entries(saved.explicit.events || {}).map(([key, item]) =>
        [key, item.mode === 'value' ? String(item.value) : 'inherit']));
      setDraft(fresh); setBaseline(JSON.stringify(fresh)); setConflict(false);
    } catch (caught) {
      setError(errorText(caught));
      if (caught instanceof ApiError && caught.status === 409) setConflict(true);
    } finally { setBusy(false); }
  };
  const openDetail = async (id: string) => {
    try {
      const result = await groupApi.diagnostic(adapterId, groupId, account, id);
      setDetail(result.records); changeParam('requestId', id); setError('');
    } catch (caught) { setError(errorText(caught)); }
  };
  return <div className="space-y-4">
    {error && <Card role="alert" className="border border-error text-error">{error}</Card>}
    {conflict && <Card role="alert" className="border border-warning">群事件设置已变化, 草稿仍保留。
      <Button size="sm" onClick={async () => { await reload(); setConflict(false); }}>查看最新值</Button>
    </Card>}
    <Card className="space-y-3">
      <h2 className="text-lg font-semibold">接收事件设置</h2>
      <p className="text-sm text-text-secondary">设置业务事件可见性。机器人自身入退群维护和 OneBot 请求账本仍会处理必要协议事件</p>
      <div className="grid gap-3 md:grid-cols-2">
        {config.capabilities.event_kinds.map((kind) => <label key={kind} className="block text-sm">{labels[kind] || kind}
          <span className="ml-2 text-text-secondary">当前{config.effective.events[kind] ? '显示' : '隐藏'} · {config.sources.events[kind]}</span>
          <select className="glass-input mt-1 w-full" value={draft[kind] || 'inherit'} disabled={historical || busy}
            onChange={(event) => setDraft((previous) => ({ ...previous, [kind]: event.target.value }))}>
            <option value="inherit">继承默认显示</option><option value="true">显示</option><option value="false">隐藏</option>
          </select>
        </label>)}
      </div>
      <Button onClick={save} disabled={historical || busy || !dirty}>保存事件设置</Button>
    </Card>
    <Card className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2"><h2 className="text-lg font-semibold">近期群事件</h2>
        <div className="flex gap-2"><select className="glass-input" value={eventLimit}
          onChange={(event) => changeParam('eventLimit', event.target.value)}>
          <option value="50">最近 50 条</option><option value="100">最近 100 条</option>
        </select><Button size="sm" variant="subtle" disabled={!isRunning} onClick={() => setRefreshKey((value) => value + 1)}>刷新</Button></div>
      </div>
      <p className="text-sm text-text-secondary">仅保留进程内有界事件, 重启后可能消失。请求 flag 和原始事件不会在此展示</p>
      {!isRunning && <p className="text-sm text-warning">后端离线, 近期事件暂不可读取</p>}
      {isRunning && events.length === 0 && <p className="text-sm text-text-secondary">近期没有可见事件</p>}
      {events.map((item) => <div key={item.id} className="rounded-lg border border-glass-border p-3 text-sm">
        <time>{new Date(item.time * 1000).toLocaleString()}</time> · <strong>{labels[item.kind] || item.kind}</strong>
        {item.sub_type && <span> · {item.sub_type}</span>}
        {item.user_id && <span> · 成员 {item.user_id}</span>}
        {item.operator_id && <span> · 操作者 {item.operator_id}</span>}
        {item.message_id && <span> · 消息 {item.message_id}</span>}
      </div>)}
    </Card>
    <Card className="space-y-3">
      <h2 className="text-lg font-semibold">请求阶段诊断</h2>
      <div className="flex flex-wrap gap-2"><input className="glass-input max-w-xs" placeholder="阶段筛选, 如 ingress" value={stage}
        onChange={(event) => changeParam('stage', event.target.value)} />
        <input className="glass-input max-w-xs" placeholder="请求 ID" value={requestId}
          onChange={(event) => changeParam('requestId', event.target.value)} />
        <Button size="sm" variant="subtle" disabled={!isRunning} onClick={() => setRefreshKey((value) => value + 1)}>刷新</Button>
      </div>
      {isRunning && diagnostics.length === 0 && <p className="text-sm text-text-secondary">此群暂无符合条件的诊断记录</p>}
      {diagnostics.map((item) => <div key={item.request_id} className="rounded-lg border border-glass-border p-3 text-sm">
        <div className="flex flex-wrap justify-between gap-2"><strong>{item.request_id}</strong><time>{item.recorded_at}</time></div>
        <p>阶段: {item.stages.join(' → ') || '未知'} · 原因: {item.reason_codes.join(', ') || '无'}</p>
        <p>发送: {item.send_status || '未记录'}</p>
        <Button size="sm" variant="subtle" onClick={() => openDetail(item.request_id)}>查看阶段明细</Button>
      </div>)}
      {detail && <pre className="max-h-80 overflow-auto whitespace-pre-wrap rounded-lg border border-glass-border p-3 text-xs">{JSON.stringify(detail, null, 2)}</pre>}
    </Card>
  </div>;
}
