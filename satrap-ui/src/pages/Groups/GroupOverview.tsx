import { useState } from 'react';
import { Link } from 'react-router-dom';
import { ApiError } from '@/api/client';
import { groupApi, type GroupAction } from '@/api/groups';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { useGroupContext } from './GroupLayout';

function errorText(error: unknown): string {
  if (error instanceof ApiError) return `${error.message}${error.code ? ` (${error.code})` : ''}`;
  return error instanceof Error ? error.message : '操作失败';
}

export function GroupOverview() {
  const { adapterId, groupId, account, isRunning, historical, config } = useGroupContext();
  const path = `/platforms/${encodeURIComponent(adapterId)}/groups/${encodeURIComponent(groupId)}`;
  const query = `?account=${encodeURIComponent(account)}`;
  const binding = config.effective.session.binding as { provider?: string; config_name?: string } | undefined;
  const checks = [
    ['账号已确认', Boolean(account) && !historical, '/platforms'],
    ['机器人仍在此群', config.group.membership === 'joined', `${path}/members${query}`],
    ['本群响应已开启', config.effective.policy.enabled === true, `${path}/policy${query}`],
    ['会话绑定已指定', Boolean(binding?.config_name), `${path}/session${query}`],
    ['模型配置引用可用', null, `${path}/session${query}`],
  ] as const;
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
  const submitSend = async () => {
    if (!canSend || busy || !sendText.trim()) return;
    const id = sendId || crypto.randomUUID(); setSendId(id); setBusy('send'); setError('');
    try {
      setSendResult(await groupApi.send(adapterId, groupId, {
        expected_self_id: account, action_id: id, message: sendText,
      }));
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
    const id = wakeId || crypto.randomUUID(); setWakeId(id); setBusy('wake'); setError('');
    try {
      const result = await groupApi.wake(adapterId, groupId, {
        expected_self_id: account, request_id: id, prompt: wakeText,
      });
      setWakeResult(`${result.status}${result.reason ? ` · ${result.reason}` : ''}`);
    } catch (caught) { setError(errorText(caught)); }
    finally { setBusy(''); }
  };
  const inspectWake = async () => {
    if (!wakeId) return;
    try {
      const result = await groupApi.wakeStatus(adapterId, groupId, account, wakeId);
      setWakeResult(`${result.status}${result.reason ? ` · ${result.reason}` : ''}`); setError('');
    } catch (caught) { setError(errorText(caught)); }
  };
  return <div className="space-y-4">
    {error && <Card role="alert" className="border border-error text-error">{error}</Card>}
    <div className="grid gap-4 lg:grid-cols-2">
      <Card className="space-y-3">
        <h2 className="text-lg font-semibold">接入检查</h2>
        {checks.map(([label, okay, target]) => <div key={label} className="flex items-center justify-between gap-3 text-sm">
          <span>{label}</span><Link className={okay ? 'text-success' : 'text-warning'} to={target}>
            {okay === null ? '待核实' : okay ? '通过' : '需处理'} →</Link>
        </div>)}
      </Card>
      <Card className="space-y-3">
        <h2 className="text-lg font-semibold">有效设置</h2>
        <p className="text-sm text-text-secondary">响应: {config.effective.policy.enabled ? '开启' : '关闭'} · 来源: {config.sources.policy.enabled?.source_label || '未知'}</p>
        <p className="text-sm text-text-secondary">唤醒模式: {String(config.effective.policy.wake_mode || '平台默认')} · 来源: {config.sources.policy.wake_mode?.source_label || '平台默认'}</p>
        <p className="text-sm text-text-secondary">会话: {binding?.provider || '未知'} / {binding?.config_name || '未指定'} · 范围: {String(config.effective.session.scope || '未知')}</p>
        <p className="text-sm text-text-secondary">会话路由代次: {config.route_generation}</p>
        <div className="flex gap-3 text-sm"><Link className="text-accent hover:underline" to={`${path}/policy${query}`}>编辑响应策略 →</Link>
          <Link className="text-accent hover:underline" to={`${path}/events${query}`}>查看诊断 →</Link></div>
      </Card>
    </div>
    <div className="grid gap-4 lg:grid-cols-2">
      <Card className="space-y-3">
        <h2 className="text-lg font-semibold">手动唤醒</h2>
        <p className="text-sm text-text-secondary">使用管理身份将文本提交到此群的会话队列。需要已启用本群响应; 不会伪装成群成员</p>
        <textarea className="glass-input min-h-24 w-full" value={wakeText} maxLength={8192} disabled={!canWake || !!busy || !!wakeId}
          placeholder="留空时尝试唤醒待处理消息" onChange={(event) => setWakeText(event.target.value)} />
        <div className="flex flex-wrap gap-2"><Button onClick={submitWake} disabled={!canWake || !!busy}>提交唤醒</Button>
          <Button size="sm" variant="subtle" onClick={inspectWake} disabled={!wakeId}>按请求 ID 查询</Button>
          <Button size="sm" variant="subtle" onClick={() => { setWakeId(''); setWakeResult(''); }} disabled={!wakeId || !!busy}>新请求</Button></div>
        {wakeId && <p role="status" className="break-all text-sm">请求 ID: {wakeId} · {wakeResult || '结果待查询'}</p>}
      </Card>
      <Card className="space-y-3">
        <h2 className="text-lg font-semibold">手动发送消息</h2>
        <p className="text-sm text-text-secondary">向当前已加入的群发送纯文本。响应关闭时仍可由管理面板发送; 结果未知时先核实平台, 不自动重发</p>
        <textarea className="glass-input min-h-24 w-full" value={sendText} maxLength={1000} disabled={!canSend || !!busy || !!sendId}
          placeholder="输入 1 到 1000 字符" onChange={(event) => setSendText(event.target.value)} />
        <div className="flex flex-wrap gap-2"><Button onClick={submitSend} disabled={!canSend || !!busy || !sendText.trim()}>发送</Button>
          <Button size="sm" variant="subtle" onClick={inspectSend} disabled={!sendId}>按操作 ID 查询</Button>
          <Button size="sm" variant="subtle" onClick={() => { setSendId(''); setSendResult(null); }} disabled={!sendId || !!busy}>新操作</Button></div>
        {sendId && <p role="status" className="break-all text-sm">操作 ID: {sendId} · {sendResult?.state || '结果待查询'}
          {sendResult?.state === 'unknown' ? ' · 请先核实平台状态' : ''}</p>}
      </Card>
    </div>
  </div>;
}
