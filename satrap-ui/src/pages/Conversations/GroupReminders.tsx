import { useEffect, useRef, useState } from 'react';
import { reminderApi, type GroupReminder, type ReminderPage, type ReminderState } from '@/api/reminders';
import { controlApi } from '@/api/control';
import type { PlatformArchiveMessage, PlatformArchiveRecord } from '@/api/types';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { Modal } from '@/components/ui/Modal';
import { confirmDiscard } from '@/hooks/useDirtyGuard';
import { errorText } from '@/utils/errorText';
import { formatTime } from '@/utils/format';

export const reminderStateLabels: Record<ReminderState, string> = {
  scheduled: '待执行', waiting_delivery: '等待平台恢复', paused: '已暂停', sending: '正在发送', sent: '已发送',
  partial: '部分发送成功', failed: '发送失败', unknown: '无法确认是否送达', missed: '已错过', cancelled: '已取消',
};
const reasonLabels: Record<string, string> = {
  platform_removed: '平台实例已移除', platform_disabled: '平台已停用', scheduled_send_unsupported: '平台不支持后台发送',
  agent_without_reminders: '当前 Agent 未配置提醒', agent_unavailable: '当前 Agent 不可用', agent_disabled: '当前 Agent 已停用',
  group_chat_disabled: '群聊插件已停用', reminder_capability_disabled: '创建提醒能力已停用', group_chat_unavailable: '群聊插件不可用',
  reminders_disabled: '提醒功能已关闭', group_not_allowed: '当前群不在插件允许范围内', platform_offline: '平台离线',
  group_state_pending: '正在等待平台群配置就绪',
  account_changed: '机器人账号已变化', group_disabled: '当前群已停用', member_unverified: '无法确认相关成员仍属于当前群',
  member_unavailable: '暂时无法查询成员', catchup_expired: '已超过到期补发宽限', interrupted_send: '进程退出时发送未获确认',
  scheduled_target_changed: '发送前账号或权限发生变化', instance_changed: '平台实例已重新创建',
  conversation_cleanup: '已在对话数据清理时取消',
  action_rejected: '平台拒绝了这次发送', action_unconfirmed: '请求提交后没有获得平台确认', missing_message_id: '平台没有返回消息确认',
  scheduled_tracking_unavailable: '无法保存必要的发送记录，已停止发送', scheduled_tracking_incomplete: '发送记录不完整，无法确认全部结果',
  scheduled_send_interrupted: '发送过程中任务被中断', scheduled_finalize_unavailable: '无法保存最终发送结果',
  scheduled_send_exception: '发送过程中出现异常', scheduled_confirmation_missing: '平台没有返回消息确认',
};
// 统一时间格式; due_at_utc 为 ISO 字符串, 其余为 epoch 秒
const formatDate = (value: string | number) => formatTime(typeof value === 'number' ? value : Math.floor(new Date(value).getTime() / 1000));
type Draft = { text: string; mode: 'relative' | 'absolute'; delay: string; unit: string; due: string; mentions: string; key: string };

export function GroupReminders({ record, refresh, onDirty }: { record: PlatformArchiveRecord; refresh: number; onDirty: (dirty: boolean) => void }) {
  const [state, setState] = useState<ReminderState | ''>('');
  const [page, setPage] = useState<ReminderPage>();
  const [cursors, setCursors] = useState<string[]>([]);
  const cursor = cursors[cursors.length - 1];
  const [version, setVersion] = useState(0);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const mounted = useRef(true);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [draft, setDraft] = useState<Draft>();
  const [draftError, setDraftError] = useState('');
  const [action, setAction] = useState<{ reminder: GroupReminder; kind: 'cancel' | 'resume' }>();
  const [selected, setSelected] = useState<GroupReminder>();
  const [source, setSource] = useState<PlatformArchiveMessage>();
  const [sourceError, setSourceError] = useState('');
  const [sourceBusy, setSourceBusy] = useState(false);
  const sourceSequence = useRef(0);

  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const hasDraft = !!draft;
  useEffect(() => { onDirty(hasDraft); if (hasDraft) return () => onDirty(false); }, [hasDraft, onDirty]);
  useEffect(() => {
    let live = true;
    setLoading(true); setError(''); setPage(undefined);
    reminderApi.list(record, { state: state || undefined, cursor }).then((result) => { if (live) setPage(result); })
      .catch((reason) => { if (live) setError(errorText(reason)); }).finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [record, state, cursor, refresh, version]);

  const changeDraft = (field: keyof Omit<Draft, 'key'>, value: string) => {
    setDraft((old) => old ? { ...old, [field]: value, key: crypto.randomUUID() } : old);
    setDraftError('');
  };
  const create = async () => {
    if (!draft || busyRef.current) return;
    busyRef.current = true; setBusy(true); setDraftError('');
    try {
      const text = draft.text.trim();
      if (!text || text.length > 2000) throw new Error('提醒正文需要 1 至 2000 字');
      const mentions = [...new Set(draft.mentions.split(/[\s,，]+/).filter(Boolean))];
      if (mentions.length > 10 || mentions.includes('all')) throw new Error('最多提及 10 位成员，不能 @ 全体');
      const seconds = Number(draft.delay) * Number(draft.unit);
      if (draft.mode === 'relative' && (!Number.isInteger(seconds) || seconds < 10 || seconds > 31536000)) throw new Error('等待时间需要在 10 秒至一年之间');
      if (draft.mode === 'absolute' && !draft.due) throw new Error('请选择提醒的日期和时间');
      const result = await reminderApi.create(record, { text, mention_user_ids: mentions, idempotency_key: draft.key,
        ...(draft.mode === 'relative' ? { after_seconds: seconds } : { due_at: new Date(draft.due).toISOString() }) });
      if (!mounted.current) return;
      setNotice(`已安排在 ${formatDate(result.reminder.due_at_utc)} 提醒，实际发送结果可在任务记录中查看`);
      setDraft(undefined); setCursors([]); setVersion((value) => value + 1);
    } catch (reason) { if (mounted.current) setDraftError(errorText(reason)); }
    finally { busyRef.current = false; if (mounted.current) setBusy(false); }
  };
  const mutate = async () => {
    if (!action || busyRef.current) return;
    busyRef.current = true; setBusy(true); setError('');
    try {
      const result = await reminderApi[action.kind](record, action.reminder);
      if (!mounted.current) return;
      setNotice(result.status === 'too_late_to_cancel' ? '任务已经开始发送，无法保证撤回' : `任务状态：${reminderStateLabels[result.reminder.state]}`);
      setAction(undefined); setSelected(result.reminder); setCursors([]); setVersion((value) => value + 1);
    } catch (reason) { if (mounted.current) setError(`${errorText(reason)}；状态变化时请取消并刷新后再操作`); }
    finally { busyRef.current = false; if (mounted.current) setBusy(false); }
  };
  const inspectSource = async (reminder: GroupReminder) => {
    const sequence = ++sourceSequence.current;
    setSource(undefined); setSourceError(''); setSourceBusy(true);
    try {
      const result = await controlApi.platformArchiveMessage({ platform_id: record.platform_id, self_id: record.self_id,
        conversation_kind: record.conversation_kind, chat_id: record.chat_id }, reminder.source_message_id);
      if (mounted.current && sequence === sourceSequence.current) setSource(result.item);
    } catch (reason) { if (mounted.current && sequence === sourceSequence.current) setSourceError(errorText(reason)); }
    finally { if (mounted.current && sequence === sourceSequence.current) setSourceBusy(false); }
  };

  return <Card className="space-y-4">
    <div className="flex flex-wrap items-center justify-between gap-2"><h2 className="text-lg font-semibold">一次性提醒</h2>
      <div className="flex gap-2"><Button disabled={busy} onClick={() => { setDraftError(''); setDraft({ text: '', mode: 'relative', delay: '30', unit: '60', due: '', mentions: '', key: crypto.randomUUID() }); }}>创建提醒</Button>
        <Button disabled={busy || loading} onClick={() => { setCursors([]); setVersion((value) => value + 1); }}>刷新提醒</Button></div></div>
    <p className="text-sm text-text-secondary">到期发送固定文字，可提及已确认的群成员。创建和恢复需要平台在线并开启提醒；停用后不会自动恢复暂停任务。</p>
    <label className="block text-sm">任务状态<select className="glass-input mt-1 w-full" aria-label="提醒状态" value={state} onChange={(event) => { setState(event.target.value as ReminderState | ''); setCursors([]); }}>
      <option value="">全部状态</option>{Object.entries(reminderStateLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
    {error && <p role="alert" className="text-error">{error}</p>}{notice && <p role="status">{notice}</p>}
    {loading ? <p>正在读取提醒…</p> : page && <div className="space-y-2">{page.items.length === 0 && <p className="text-text-secondary">当前范围没有提醒</p>}
      {page.items.map((reminder) => <div key={reminder.reminder_id} className="rounded-xl border border-glass-border p-3">
        <div className="flex flex-wrap items-center justify-between gap-2"><strong>{reminderStateLabels[reminder.state]}</strong><span>{formatDate(reminder.due_at_utc)}</span></div>
        <p className="my-2 whitespace-pre-wrap break-words">{reminder.text}</p><p className="break-all text-xs text-text-tertiary">创建者：{reminder.creator_kind === 'operator' ? '管理界面' : reminder.creator_user_id} · {reminder.reminder_id}</p>
        {reminder.reason && <p className="mt-1 text-sm">{reasonLabels[reminder.reason] || '本次执行未完成，详细原因可查看运行日志'}</p>}
        {reminder.state === 'unknown' && <p className="mt-1 text-sm text-warning">可能已经送达，重新创建可能导致重复发送。</p>}
        <div className="mt-2 flex flex-wrap gap-2"><Button className="transition-colors" onClick={() => { sourceSequence.current++; setSource(undefined); setSourceError(''); setSourceBusy(false); setSelected(reminder); }}>查看提醒详情</Button>
          {['scheduled', 'waiting_delivery', 'paused'].includes(reminder.state) && <Button className="transition-colors" disabled={busy} onClick={() => setAction({ reminder, kind: 'cancel' })}>取消提醒</Button>}
          {reminder.state === 'paused' && <Button className="transition-colors" disabled={busy} onClick={() => setAction({ reminder, kind: 'resume' })}>恢复提醒</Button>}</div>
      </div>)}</div>}
    <div className="flex gap-2"><Button disabled={loading || !cursors.length} onClick={() => setCursors((old) => old.slice(0, -1))}>上一页提醒</Button>
      <Button disabled={loading || !page?.has_more || !page.next_cursor} onClick={() => setCursors((old) => [...old, page!.next_cursor!])}>下一页提醒</Button></div>
    <Modal open={!!draft} onClose={() => { if (!busy && confirmDiscard()) setDraft(undefined); }} title="创建一次性提醒">
      {draft && <form className="space-y-4" onSubmit={(event) => { event.preventDefault(); void create(); }}>
        <label className="block text-sm">提醒正文<textarea aria-label="提醒正文" className="glass-input mt-1 min-h-28 w-full" maxLength={2000} value={draft.text} onChange={(event) => changeDraft('text', event.target.value)} /></label>
        <label className="block text-sm">执行时间<select aria-label="提醒时间方式" className="glass-input mt-1 w-full" value={draft.mode} onChange={(event) => changeDraft('mode', event.target.value)}><option value="relative">多久后</option><option value="absolute">指定日期时间</option></select></label>
        {draft.mode === 'relative' ? <div className="flex gap-2"><input aria-label="提醒等待数量" type="number" min="1" className="glass-input min-w-0 flex-1" value={draft.delay} onChange={(event) => changeDraft('delay', event.target.value)} />
          <select aria-label="提醒等待单位" className="glass-input" value={draft.unit} onChange={(event) => changeDraft('unit', event.target.value)}><option value="1">秒</option><option value="60">分钟</option><option value="3600">小时</option><option value="86400">天</option></select></div>
          : <label className="block text-sm">日期和时间<input aria-label="提醒日期时间" type="datetime-local" className="glass-input mt-1 w-full" value={draft.due} onChange={(event) => changeDraft('due', event.target.value)} /><span className="text-xs text-text-tertiary">使用当前设备时区：{Intl.DateTimeFormat().resolvedOptions().timeZone}</span></label>}
        <label className="block text-sm">要提及的成员 ID（可选）<input aria-label="提醒提及成员" className="glass-input mt-1 w-full" value={draft.mentions} onChange={(event) => changeDraft('mentions', event.target.value)} /><span className="text-xs text-text-tertiary">多个 ID 用空格或逗号分隔，保存前会核验成员属于当前群。</span></label>
        {draftError && <p role="alert" className="text-error">{draftError}</p>}<Button type="submit" disabled={busy}>{busy ? '正在安排…' : '安排提醒'}</Button>
      </form>}
    </Modal>
    <Modal open={!!action} onClose={() => { if (!busy) setAction(undefined); }} title={action?.kind === 'resume' ? '恢复提醒' : '取消提醒'}>
      <p>{action?.kind === 'resume' ? '恢复会重新检查当前配置和群成员，保持原定时间；超过补发宽限会记为已错过。' : '取消尚未开始发送的任务；如果发送已经开始，无法保证撤回。'}</p>
      <p className="my-3 whitespace-pre-wrap break-words">{action?.reminder.text}</p>{error && <p role="alert" className="text-error">{error}</p>}<Button disabled={busy} onClick={() => void mutate()}>确认{action?.kind === 'resume' ? '恢复' : '取消'}</Button>
    </Modal>
    <Modal open={!!selected} onClose={() => { sourceSequence.current++; setSelected(undefined); setSource(undefined); setSourceError(''); }} title="提醒详情">
      {selected && <div className="space-y-3"><p>{reminderStateLabels[selected.state]} · 修订 {selected.revision}</p><p className="whitespace-pre-wrap break-words">{selected.text}</p>
        <p>计划执行：{formatDate(selected.due_at_utc)}</p><p>创建时间：{formatDate(selected.created_at)}</p>
        {selected.paused_at && <p>暂停时间：{formatDate(selected.paused_at)}</p>}{selected.settled_at && <p>结果记录时间：{formatDate(selected.settled_at)}</p>}
        <p className="break-all">提及成员：{selected.mention_user_ids.join('、') || '无'}</p>
        <p className="break-all">已确认消息 ID：{selected.delivery?.message_ids.join('、') || '无确认记录'}</p>
        {selected.reason && <p>原因：{reasonLabels[selected.reason] || '本次执行未完成，详细原因可查看运行日志'}</p>}
        {selected.source_message_id ? <><p className="break-all">来源消息：{selected.source_message_id}{selected.source_status === 'unavailable' ? '（已不可用，提醒仍保留）' : ''}</p>
          <Button variant="ghost" disabled={sourceBusy || selected.source_status === 'unavailable'} onClick={() => void inspectSource(selected)}>查看提醒来源</Button></> : <p>由管理界面创建</p>}
        {sourceBusy && <p>正在读取来源…</p>}{sourceError && <p role="alert">{sourceError}</p>}{source && <p className="whitespace-pre-wrap break-words">{source.text}</p>}
      </div>}
    </Modal>
  </Card>;
}
