import { createUuid } from '@/utils/uuid';
import { useState } from 'react';
import { Link } from 'react-router-dom';
import {
  AlertTriangle, Check, ChevronRight, FileJson, Send, ShieldCheck, SlidersHorizontal, XCircle, Zap,
} from 'lucide-react';
import { groupApi, type GroupAction } from '@/api/groups';
import { Card } from '@/components/kit/Card';
import { Badge } from '@/components/kit/Badge';
import { Button } from '@/components/kit/Button';
import { Textarea } from '@/components/kit/Field';
import { toast } from '@/components/kit/Toast';
import { useGroupContext } from './GroupLayout';
import { errorText } from '@/utils/errorText';
import { groupActionStateLabels as actionStateLabels } from './groupActionLabels';
import { Note, SectionHead, membershipLabels } from './shared';

const wakeStatusLabels: Record<string, string> = {
  accepted: '已受理', already_pending: '已有相同待处理请求', no_pending: '无待处理消息',
  rejected: '已拒绝', executing: '发送中', sent: '已发送', partial: '部分发送',
  failed: '发送失败', unknown: '结果未确认',
};

export function GroupOverview() {
  const { adapterId, groupId, account, isRunning, historical, config } = useGroupContext();
  const path = `/platforms/${encodeURIComponent(adapterId)}/groups/${encodeURIComponent(groupId)}`;
  const query = `?account=${encodeURIComponent(account)}`;
  const binding = config.effective.session.binding as { provider?: string; config_name?: string } | undefined;
  const checks: Array<{ label: string; okay: boolean | null; to: string; note: string }> = [
    { label: '账号已确认', okay: Boolean(account) && !historical, to: '/platforms', note: '当前连接账号与管理账号一致' },
    { label: '机器人仍在此群', okay: config.group.membership === 'joined', to: `${path}/members${query}`, note: membershipLabels[config.group.membership] || '待确认' },
    { label: '本群响应已开启', okay: config.effective.policy.enabled === true, to: `${path}/policy${query}`, note: config.sources.policy.enabled?.source_label || '平台默认' },
    { label: '会话绑定可用', okay: config.capabilities.binding_available ?? null, to: `${path}/session${query}`, note: '命名配置与模型引用均有效' },
    { label: '模型配置引用可用', okay: config.capabilities.model_reference_available ?? null, to: '/models', note: '模型配置需在模型管理中维护' },
  ];
  const [sendText, setSendText] = useState('');
  const [sendId, setSendId] = useState('');
  const [sendResult, setSendResult] = useState<GroupAction | null>(null);
  const [wakeText, setWakeText] = useState('');
  const [wakeId, setWakeId] = useState('');
  const [wakeResult, setWakeResult] = useState('');
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const canSend = isRunning && !historical && config.group.membership === 'joined';
  const canWake = canSend && config.effective.policy.enabled === true;
  const resetSend = () => { setSendId(''); setSendResult(null); setSendText(''); setError(''); };
  const submitSend = async () => {
    if (!canSend || busy || !sendText.trim()) return;
    const id = sendId || createUuid(); setSendId(id); setBusy('send'); setError('');
    try {
      const result = await groupApi.send(adapterId, groupId, {
        expected_self_id: account, action_id: id, message: sendText,
      });
      setSendResult(result);
      if (result.state === 'succeeded') toast.success('消息已发送', '平台返回成功');
    } catch (caught) { setError(errorText(caught)); }
    finally { setBusy(''); }
  };
  const inspectSend = async () => {
    if (!sendId) return;
    try { setSendResult(await groupApi.action(adapterId, groupId, account, sendId)); setError(''); }
    catch (caught) { setError(errorText(caught)); }
  };
  const submitWake = async () => {
    if (!canWake || busy) return;
    const id = wakeId || createUuid(); setWakeId(id); setBusy('wake'); setError('');
    try {
      const result = await groupApi.wake(adapterId, groupId, {
        expected_self_id: account, request_id: id, prompt: wakeText,
      });
      setWakeResult(`${wakeStatusLabels[result.status] || result.status}${result.reason ? ` · ${result.reason}` : ''}`);
      toast.success('唤醒请求已受理', wakeStatusLabels[result.status] || result.status);
    } catch (caught) { setError(errorText(caught)); }
    finally { setBusy(''); }
  };
  const inspectWake = async () => {
    if (!wakeId) return;
    try {
      const result = await groupApi.wakeStatus(adapterId, groupId, account, wakeId);
      setWakeResult(`${wakeStatusLabels[result.status] || result.status}${result.reason ? ` · ${result.reason}` : ''}`); setError('');
    } catch (caught) { setError(errorText(caught)); }
  };
  return <div className="space-y-4 animate-fade">
    {error && <Note tone="red" title="操作失败">{error}</Note>}
    <div className="grid gap-4 lg:grid-cols-2">
      <Card padding="lg">
        <SectionHead tint="pink" title="接入检查" icon={<ShieldCheck className="h-4 w-4" />} description="逐项确认此群是否具备响应条件" />
        <ul className="divide-y divide-[var(--glass-border)]">
          {checks.map((check) => (
            <li key={check.label} className="flex items-center gap-3 py-2.5">
              <span className={`grid h-6 w-6 flex-none place-items-center rounded-full ${
                check.okay === null ? 'bg-glass-hover text-fg-3' : check.okay ? 'bg-[rgb(var(--rgb-green)/0.12)] text-green' : 'bg-[rgb(var(--rgb-orange)/0.12)] text-orange'}`}>
                {check.okay === null ? <AlertTriangle className="h-3.5 w-3.5" /> : check.okay ? <Check className="h-3.5 w-3.5" /> : <XCircle className="h-3.5 w-3.5" />}
              </span>
              <span className="min-w-0 flex-1">
                <span className="block text-[13px] text-fg">{check.label}</span>
                <span className="block text-[11.5px] text-fg-3">{check.note}</span>
              </span>
              <Badge tone={check.okay === null ? 'neutral' : check.okay ? 'green' : 'orange'}>
                {check.okay === null ? '待核实' : check.okay ? '通过' : '需处理'}
              </Badge>
              <Link to={check.to} className="text-fg-4 transition hover:text-pink" aria-label={`前往${check.label}`}>
                <ChevronRight className="h-4 w-4" />
              </Link>
            </li>
          ))}
        </ul>
      </Card>
      <Card padding="lg">
        <SectionHead tint="pink" title="有效设置" icon={<SlidersHorizontal className="h-4 w-4" />} description="叠加平台、群类型与本群覆盖后的结果" />
        <div className="space-y-2.5 text-[12.5px]">
          <div className="flex items-center justify-between gap-3 rounded-xl border border-line bg-glass px-3 py-2">
            <span className="text-fg-3">本群响应</span>
            <span className="flex items-center gap-1.5">
              <Badge tone={config.effective.policy.enabled ? 'green' : 'neutral'} dot>{config.effective.policy.enabled ? '开启' : '关闭'}</Badge>
              <span className="text-fg-4">{config.sources.policy.enabled?.source_label || '平台默认'}</span>
            </span>
          </div>
          <div className="flex items-center justify-between gap-3 rounded-xl border border-line bg-glass px-3 py-2">
            <span className="text-fg-3">自动参与模式</span>
            <span className="flex items-center gap-1.5"><Badge tone="pink">{String(config.effective.policy.wake_mode ?? '平台默认')}</Badge><span className="text-fg-4">{config.sources.policy.wake_mode?.source_label || '平台默认'}</span></span>
          </div>
          <div className="flex items-center justify-between gap-3 rounded-xl border border-line bg-glass px-3 py-2">
            <span className="text-fg-3">会话绑定</span>
            <span className="text-right font-mono text-[11.5px] text-fg-2">{binding?.provider || '未知'} / {binding?.config_name || '未指定'}<br /><span className="font-sans text-[10.5px] text-fg-4">范围 {String(config.effective.session.scope ?? '未知')} · 路由代次 {config.route_generation}</span></span>
          </div>
          <div className="flex flex-wrap gap-2 pt-1">
            <Link to={`${path}/policy${query}`} className="ctl ctl-tinted ctl-sm tint-pink">编辑响应策略</Link>
            <Link to={`${path}/events${query}`} className="ctl ctl-ghost ctl-sm"><FileJson className="h-3.5 w-3.5" />查看诊断</Link>
            <Link to="/models" className="ctl ctl-ghost ctl-sm">配置模型</Link>
          </div>
        </div>
      </Card>
    </div>

    <div className="grid gap-4 lg:grid-cols-2">
      <Card padding="lg">
        <SectionHead tint="pink" title="手动唤醒" icon={<Zap className="h-4 w-4" />} description="使用管理身份将文本提交到此群会话队列, 不伪装成群成员" />
        <Textarea rows={4} maxLength={8192} value={wakeText} disabled={!canWake || !!busy || !!wakeId}
          placeholder="留空时尝试唤醒待处理消息" onChange={(event) => setWakeText(event.target.value)} />
        <div className="mt-2 flex flex-wrap items-center gap-1.5">
          <Button size="sm" variant="tinted" tint="pink" icon={<Zap className="h-3.5 w-3.5" />} loading={busy === 'wake'} disabled={!canWake || !!busy || !!wakeId} onClick={submitWake}>提交唤醒</Button>
          <Button size="sm" variant="ghost" disabled={!wakeId} onClick={inspectWake}>按请求 ID 查询</Button>
          <Button size="sm" variant="ghost" disabled={!wakeId || !!busy} onClick={() => { setWakeId(''); setWakeResult(''); setWakeText(''); }}>新请求</Button>
        </div>
        {!canWake && <p className="mt-2 text-[11.5px] text-orange">需要已启用本群响应且机器人在群内, 后端在线。</p>}
        {wakeId && <p role="status" className="mt-2 break-all text-[11.5px] text-fg-3">请求 ID <span className="num font-mono text-fg-2">{wakeId}</span> · {wakeResult || '结果待查询'}</p>}
      </Card>
      <Card padding="lg">
        <SectionHead tint="pink" title="手动发送消息" icon={<Send className="h-4 w-4" />} description="响应关闭时仍可由管理面板发送; 结果未知时先核实平台" />
        <Textarea rows={4} maxLength={1000} value={sendText} disabled={!canSend || !!busy || !!sendId}
          placeholder="输入 1 到 1000 字符" onChange={(event) => setSendText(event.target.value)} />
        <div className="mt-2 flex flex-wrap items-center gap-1.5">
          <Button size="sm" variant="tinted" tint="pink" icon={<Send className="h-3.5 w-3.5" />} loading={busy === 'send'} disabled={!canSend || !!busy || !sendText.trim() || !!sendId} onClick={submitSend}>发送</Button>
          <Button size="sm" variant="ghost" disabled={!sendId} onClick={inspectSend}>按操作 ID 查询</Button>
          <Button size="sm" variant="ghost" disabled={!sendId || !!busy || !sendResult || ['unknown', 'executing', 'pending'].includes(sendResult.state)} onClick={resetSend}>新操作</Button>
        </div>
        {sendId && <p role="status" className="mt-2 break-all text-[11.5px] text-fg-3">
          操作 ID <span className="num font-mono text-fg-2">{sendId}</span> · {actionStateLabels[sendResult?.state || ''] || sendResult?.state || '结果待查询'}
          {sendResult?.state === 'unknown' ? ' · 请先核实平台状态' : ''}
        </p>}
        {sendResult?.state === 'unknown' && <Button size="sm" variant="ghost" className="mt-2" onClick={resetSend}>我已核实平台状态, 可以创建新操作</Button>}
        {!canSend && <p className="mt-2 text-[11.5px] text-orange">机器人未确认加入此群或后端离线, 无法发送。</p>}
      </Card>
    </div>
    <Note tone="neutral" icon={<AlertTriangle className="h-4 w-4" />}>
      所有管理动作都固定目标为此群, 不会扩散到其它会话; 结果未知时请先用操作 ID 查询, 不自动重发。
    </Note>
  </div>;
}
