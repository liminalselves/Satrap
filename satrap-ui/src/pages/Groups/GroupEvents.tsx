import { useCallback, useEffect, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { Bell, BellOff, FileJson, RefreshCw, ScanLine, X } from 'lucide-react';
import { ApiError } from '@/api/client';
import { groupApi, type GroupDiagnostic, type GroupEvent, type GroupPolicyValue } from '@/api/groups';
import { Card } from '@/components/kit/Card';
import { Badge } from '@/components/kit/Badge';
import { Button, IconButton } from '@/components/kit/Button';
import { Drawer } from '@/components/kit/Drawer';
import { EmptyState } from '@/components/kit/Feedback';
import { Input } from '@/components/kit/Field';
import { toast } from '@/components/kit/Toast';
import { useDirtyGuard } from '@/hooks/useDirtyGuard';
import { reasonLabel, stageLabel, statusLabel } from '@/components/diagnostics/RequestDiagnosticsPanel';
import { errorText } from '@/utils/errorText';
import { formatTime } from '@/utils/format';
import { useGroupContext } from './GroupLayout';
import { VersionedConfigRead } from '@/api/configVersions';
import { getApiBaseUrl } from '@/utils/constants';
import { startVisiblePoll } from '@/utils/visiblePoll';
import { CopyValue, EmptyCard, Note, SectionHead, groupEventLabels } from './shared';
import { Select as KitSelect } from '@/components/kit/Select';

// ISO 时间串只保留到秒, 与诊断面板的展示口径一致
const recordedAtText = (value: string) => value.replace('T', ' ').slice(0, 19);

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
  const eventRead = useMemo(() => new VersionedConfigRead([], async (signal) => {
    const [eventResult, diagnosticResult] = await Promise.allSettled([
      groupApi.events(adapterId, groupId, account, eventLimit, signal),
      groupApi.diagnostics(adapterId, groupId, account, stage, requestId, signal),
    ]);
    if (eventResult.status === 'rejected') throw eventResult.reason;
    if (diagnosticResult.status === 'rejected') throw diagnosticResult.reason;
    return { events: eventResult.value.items, diagnostics: diagnosticResult.value.records };
  }, getApiBaseUrl), [adapterId, groupId, account, eventLimit, stage, requestId]);
  useEffect(() => {
    if (dirty) return;
    const fresh = fromConfig(); setDraft(fresh); setBaseline(JSON.stringify(fresh));
  }, [config.revision, config.account, dirty, fromConfig]);
  useEffect(() => {
    if (!isRunning || !account) return;
    const poll = startVisiblePoll(async (signal) => {
      try {
        const result = await eventRead.read({ signal });
        if (signal.aborted) return;
        setEvents(result.events); setDiagnostics(result.diagnostics); setError('');
      } catch (caught) {
        if (signal.aborted) return;
        setError(errorText(caught)); throw caught;
      }
    }, { interval: 5000 });
    return () => poll.stop();
  }, [account, isRunning, eventRead, refreshKey]);
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
      toast.success('事件设置已保存', '业务事件可见性已更新');
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

  return <div className="space-y-4 animate-fade">
    {error && <Note tone="red" title="读取失败">{error}</Note>}
    {conflict && <Note tone="orange" title="群事件设置已变化, 草稿仍保留"
      action={<Button size="sm" onClick={async () => { await reload(); setConflict(false); }}>查看最新值</Button>}>
      不会自动覆盖其他管理员的修改。
    </Note>}

    <Card padding="lg">
      <SectionHead tint="pink" title="接收事件设置" icon={<Bell className="h-4 w-4" />}
        description="设置业务事件可见性; 机器人自身入退群维护与 OneBot 请求账本仍会处理必要协议事件"
        actions={<Button size="sm" variant="tinted" tint="pink" loading={busy} onClick={save} disabled={historical || !dirty}>保存事件设置</Button>} />
      <div className="grid gap-2 lg:grid-cols-2">
        {config.capabilities.event_kinds.map((kind) => (
          <label key={kind} className="flex flex-wrap items-center gap-2 rounded-xl border border-line bg-glass px-3 py-2">
            <span className="w-24 flex-none text-[12.5px] text-fg-2">{groupEventLabels[kind] || kind}</span>
            <Badge tone={config.effective.events[kind] ? 'green' : 'neutral'}>{config.effective.events[kind] ? '显示' : '隐藏'}</Badge>
            <span className="text-[10.5px] text-fg-4">{config.sources.events[kind]}</span>
            <KitSelect className="field ml-auto !h-7 !w-[118px] !text-[11.5px]" value={draft[kind] || 'inherit'} disabled={historical || busy}
              aria-label={groupEventLabels[kind] || kind}
              onChange={(value) => setDraft((previous) => ({ ...previous, [kind]: value }))}>
              <option value="inherit">继承默认显示</option><option value="true">显示</option><option value="false">隐藏</option>
            </KitSelect>
          </label>
        ))}
      </div>
      {config.capabilities.event_kinds.length === 0 && <p className="text-[12px] text-fg-3">当前平台未返回可配置的事件类型。</p>}
    </Card>

    <Card padding="none">
      <div className="flex flex-wrap items-center gap-3 border-b border-line p-4">
        <div>
          <h3 className="text-[14px] font-semibold text-fg">近期群事件</h3>
          <p className="text-[11.5px] text-fg-3">仅保留进程内有界事件, 重启后可能消失; 请求 flag 与原始事件不会展示</p>
        </div>
        <div className="ml-auto flex items-center gap-2">
          <KitSelect className="field !h-8 !w-[122px] !text-[12px]" value={String(eventLimit)} aria-label="事件条数"
            onChange={(value) => changeParam('eventLimit', value)}>
            <option value="50">最近 50 条</option><option value="100">最近 100 条</option>
          </KitSelect>
          <Button size="sm" variant="ghost" icon={<RefreshCw className={`h-3.5 w-3.5 ${refreshKey > 0 ? 'transition-transform' : ''}`} />}
            disabled={!isRunning} onClick={() => { setRefreshKey((value) => value + 1); toast.success('事件已刷新'); }}>刷新</Button>
        </div>
      </div>
      {!isRunning && <div className="p-4"><Note tone="orange">后端离线, 近期事件暂不可读取。</Note></div>}
      {isRunning && events.length === 0 ? (
        <EmptyState tint="pink" icon={<BellOff className="h-6 w-6" />} title="近期没有可见事件" description="事件仅在进程内保留, 可调整接收事件设置。" />
      ) : (
        <ol className="relative space-y-0 px-4 py-2 before:absolute before:bottom-4 before:left-[27px] before:top-4 before:w-px before:bg-[var(--glass-border-strong)]" key={refreshKey}>
          {events.map((event) => (
            <li key={event.id} className="relative flex gap-3 py-2">
              <span className="relative z-[1] mt-1.5 h-2.5 w-2.5 flex-none rounded-full border-2 border-[var(--bg-base)] bg-pink shadow-[0_0_8px_rgb(var(--rgb-pink)/0.7)]" />
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[12.5px]">
                  <span className="num font-mono text-[11px] text-fg-4">{formatTime(event.time)}</span>
                  <span className="font-medium text-fg">{groupEventLabels[event.kind] || event.kind}</span>
                  {event.sub_type && <Badge tone="pink">{event.sub_type}</Badge>}
                  <span className="text-fg-3">
                    {event.user_id && <>成员 <span className="num font-mono">{event.user_id}</span></>}
                    {event.operator_id && <> · 操作者 <span className="num font-mono">{event.operator_id}</span></>}
                    {event.message_id && <> · 消息 <span className="num font-mono">{event.message_id}</span></>}
                  </span>
                </div>
              </div>
            </li>
          ))}
        </ol>
      )}
    </Card>

    <Card padding="lg">
      <SectionHead tint="pink" title="请求阶段诊断" icon={<ScanLine className="h-4 w-4" />}
        description="按阶段或请求 ID 过滤; 只做翻译, 不改变服务端结论"
        actions={<Button size="sm" variant="ghost" icon={<RefreshCw className="h-3.5 w-3.5" />} disabled={!isRunning}
          onClick={() => { setRefreshKey((value) => value + 1); toast.success('诊断记录已刷新'); }}>刷新</Button>} />
      <div className="mb-3 grid gap-2 sm:grid-cols-[1fr_1fr_auto]">
        <Input value={stage} placeholder="阶段筛选, 如 ingress" aria-label="阶段筛选" className="!h-8 !text-[12px]"
          onChange={(event) => changeParam('stage', event.target.value)} />
        <Input value={requestId} placeholder="请求 ID" aria-label="请求 ID" className="!h-8 !text-[12px]"
          onChange={(event) => changeParam('requestId', event.target.value)} />
        <Button size="sm" variant="ghost" onClick={() => { changeParam('stage', ''); changeParam('requestId', ''); }}>清除</Button>
      </div>
      {diagnostics.length === 0 ? (
        <EmptyCard>此群暂无符合条件的诊断记录</EmptyCard>
      ) : (
        <div className="space-y-2">
          {diagnostics.map((item) => (
            <div key={item.request_id} className="rounded-xl border border-line bg-glass p-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="num font-mono text-[12px] font-medium text-fg">{item.request_id}</span>
                <span className="num font-mono text-[10.5px] text-fg-4">{recordedAtText(item.recorded_at)}</span>
              </div>
              <div className="mt-1.5 flex flex-wrap items-center gap-1">
                {item.stages.map((entry) => (
                  <span key={entry} className="flex items-center gap-1 rounded-full border border-line px-1.5 py-0.5 text-[10.5px] text-fg-2">
                    {stageLabel(entry)}
                    <span className={`h-1.5 w-1.5 rounded-full ${item.statuses[entry] === 'ok' || item.statuses[entry] === 'sent' ? 'bg-green'
                      : item.statuses[entry] === 'failed' || item.statuses[entry] === 'blocked' ? 'bg-red'
                        : item.statuses[entry] === 'skipped' ? 'bg-fg-4' : 'bg-orange'}`} />
                  </span>
                ))}
              </div>
              <p className="mt-1.5 text-[11.5px] text-fg-3">阶段: {item.stages.map(stageLabel).join(' → ') || '未知'} · 原因: {item.reason_codes.map(reasonLabel).join('; ') || '无'} · 发送: {item.send_status ? statusLabel(item.send_status) : '未记录'}</p>
              <Button size="sm" variant="ghost" className="mt-1" icon={<FileJson className="h-3.5 w-3.5" />} onClick={() => openDetail(item.request_id)}>查看阶段明细</Button>
            </div>
          ))}
        </div>
      )}
    </Card>

    <Drawer open={detail !== null} onClose={() => setDetail(null)} label="阶段明细" width={600}>
      <div className="flex items-center gap-3 border-b border-line px-5 py-4 tint-pink">
        <span className="grid h-9 w-9 place-items-center rounded-xl bg-[rgb(var(--tint)/0.14)] text-tint"><FileJson className="h-[18px] w-[18px]" /></span>
        <div className="min-w-0 flex-1"><h2 className="text-[15px] font-semibold text-fg">阶段明细</h2><p className="num truncate font-mono text-[11px] text-fg-4">{requestId}</p></div>
        <IconButton label="关闭" shortcut="Esc" size="sm" onClick={() => setDetail(null)}><X className="h-4 w-4" /></IconButton>
      </div>
      <div className="scroll min-h-0 flex-1 p-5">
        <div className="mb-2 flex gap-2"><CopyValue value={JSON.stringify(detail, null, 2)} label="复制明细" /><Badge tone="pink">只读</Badge></div>
        <pre className="scroll whitespace-pre-wrap rounded-xl border border-line bg-[rgb(0_0_0/0.22)] p-3.5 font-mono text-[12px] leading-[1.7] text-fg-2 [[data-theme=light]_&]:bg-[rgb(15_23_42/0.04)]">{JSON.stringify(detail, null, 2)}</pre>
      </div>
    </Drawer>
  </div>;
}
