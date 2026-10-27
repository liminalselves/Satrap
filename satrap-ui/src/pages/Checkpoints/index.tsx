import { errorText } from '@/utils/errorText';
import { useConfirm } from '@/hooks/useConfirm';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import {
  ArrowUpRight, GitBranch, GitCommitHorizontal, History, Plus, RefreshCw, RotateCcw, ScrollText, Search,
} from 'lucide-react';
import { Button, IconButton } from '@/components/kit/Button';
import { Badge, type Tone } from '@/components/kit/Badge';
import { Modal } from '@/components/kit/Modal';
import { Field, Input, Textarea } from '@/components/kit';
import { EmptyState, PageHeader } from '@/components/kit/Feedback';
import { toast } from '@/components/ui/Toast';
import { checkpointApi } from '@/api/checkpoint';
import { controlApi } from '@/api/control';
import { useBackendStore } from '@/stores/useBackendStore';
import { formatTime, formatRelativeTime } from '@/utils/format';
import type { Checkpoint } from '@/api/types';
import { KeyValue, StatTile } from '../Conversations/common';

const kindMeta: Record<string, { label: string; tone: Tone }> = {
  manual: { label: '手动', tone: 'accent' },
  auto: { label: '自动', tone: 'green' },
  stable: { label: '稳定', tone: 'orange' },
  fork: { label: '分支', tone: 'purple' },
};

export function Checkpoints() {
  const confirm = useConfirm();
  const [search] = useSearchParams();
  const health = useBackendStore((state) => state.health);
  const [platformId, setPlatformId] = useState(search.get('platform') || 'local');
  const [conversationId, setConversationId] = useState(search.get('conversation') || '');
  const [loading, setLoading] = useState(false);
  const [checkpoints, setCheckpoints] = useState<Checkpoint[]>([]);
  const [branches, setBranches] = useState<Checkpoint[]>([]);
  const [mutations, setMutations] = useState<Checkpoint[]>([]);
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [showForkModal, setShowForkModal] = useState(false);
  const [forkError, setForkError] = useState('');
  const [showRevertModal, setShowRevertModal] = useState(false);
  const [showLineageModal, setShowLineageModal] = useState(false);
  const [selectedCheckpoint, setSelectedCheckpoint] = useState('');
  const [newCheckpointName, setNewCheckpointName] = useState('');
  const [newCheckpointDescription, setNewCheckpointDescription] = useState('');
  const [newBranchName, setNewBranchName] = useState('');
  const [lineage, setLineage] = useState<Checkpoint[]>([]);
  const [lineageLoading, setLineageLoading] = useState(false);
  const [busy, setBusy] = useState(false);

  const fetchData = useCallback(async () => {
    if (!conversationId.trim()) {
      toast('warning', '请输入对话 ID');
      return;
    }
    setLoading(true);
    try {
      const [cpData, mutationData] = await Promise.all([
        checkpointApi.list(platformId, conversationId),
        checkpointApi.listMutations(platformId, conversationId),
      ]);
      setCheckpoints(cpData.checkpoints);
      setBranches(cpData.branches);
      setMutations(mutationData.mutations);
    } catch (e) {
      toast('error', '获取数据失败: ' + (e instanceof Error ? e.message : '未知错误'));
    } finally {
      setLoading(false);
    }
  }, [conversationId, platformId]);

  useEffect(() => {
    if (search.get('conversation') === conversationId && conversationId && (search.get('platform') || 'local') === platformId) void fetchData();
  }, [conversationId, fetchData, platformId, search]);

  const platformIds = useMemo(
    () => Array.from(new Set(['local', 'chat', platformId, ...Object.keys(health?.adapters || {})])),
    [health?.adapters, platformId],
  );

  const handleCreate = useCallback(async () => {
    setBusy(true);
    try {
      await checkpointApi.create(platformId, conversationId, newCheckpointName || undefined, newCheckpointDescription || undefined);
      toast('success', '检查点已创建');
      setShowCreateModal(false);
      setNewCheckpointName('');
      setNewCheckpointDescription('');
      fetchData();
    } catch (e) {
      toast('error', '创建失败: ' + (e instanceof Error ? e.message : '未知错误'));
    } finally {
      setBusy(false);
    }
  }, [conversationId, fetchData, newCheckpointDescription, newCheckpointName, platformId]);

  const handleFork = useCallback(async () => {
    if (!newBranchName.trim()) { setForkError('请输入分支名'); return; }
    setBusy(true);
    try {
      const result = await checkpointApi.fork(platformId, conversationId, newBranchName, selectedCheckpoint || undefined);
      toast('success', `已分支: ${result.conversation_id}`);
      setShowForkModal(false);
      setNewBranchName('');
      fetchData();
    } catch (e) {
      toast('error', 'Fork 失败: ' + (e instanceof Error ? e.message : '未知错误'));
    } finally {
      setBusy(false);
    }
  }, [conversationId, fetchData, newBranchName, platformId, selectedCheckpoint]);

  const handleRevert = useCallback(async () => {
    if (!selectedCheckpoint) return;
    if (!(await confirm('确定从此检查点恢复消息上下文吗？当前上下文会先备份，展示历史不会改变。'))) return;
    setBusy(true);
    try {
      const current = await controlApi.conversationData(platformId, conversationId, 'context');
      await controlApi.conversationData(platformId, conversationId, 'context', { action: 'checkpoint', checkpoint_id: selectedCheckpoint, expected_revision: current.revision });
      toast('success', '消息上下文已恢复，恢复前的数据已备份');
      setShowRevertModal(false);
      fetchData();
    } catch (e) {
      toast('error', '操作失败: ' + errorText(e, '未知错误'));
    } finally {
      setBusy(false);
    }
  }, [conversationId, fetchData, platformId, selectedCheckpoint, confirm]);

  const handleTraceLineage = useCallback(async (checkpointId: string) => {
    setShowLineageModal(true);
    setLineage([]);
    setLineageLoading(true);
    try {
      const result = await checkpointApi.traceLineage(platformId, checkpointId);
      setLineage(result.lineage);
    } catch (e) {
      toast('error', '获取血缘失败: ' + (e instanceof Error ? e.message : '未知错误'));
      setShowLineageModal(false);
    } finally {
      setLineageLoading(false);
    }
  }, [platformId]);

  const kindBadge = (kind: string) => {
    const meta = kindMeta[kind] ?? { label: kind, tone: 'neutral' as const };
    return <Badge tone={meta.tone}>{meta.label}</Badge>;
  };

  const checkpointOptions = useMemo(() => checkpoints.map((cp) => ({
    value: cp.checkpoint_id,
    label: `${cp.name || cp.checkpoint_id.slice(0, 8)} (${cp.checkpoint_kind})`,
  })), [checkpoints]);

  return (
    <div className="tint-green space-y-4">
      <PageHeader
        title="版本与恢复"
        description="查看已保存的检查点、分支和变更记录；恢复只作用于模型消息上下文。"
        actions={
          <>
            <Badge tone="green" dot live>检查点已启用</Badge>
            <Link to={`/conversations?${new URLSearchParams({ platform: platformId, conversation: conversationId })}`} className="inline-flex">
              <Button variant="ghost" icon={<ArrowUpRight className="h-4 w-4" />}>返回对话记录</Button>
            </Link>
          </>
        }
      />

      <div className="surface tint-green animate-rise !rounded-[16px] p-3">
        <form className="flex flex-wrap items-end gap-2" onSubmit={(event) => { event.preventDefault(); void fetchData(); }}>
          <label className="min-w-[140px] flex-1 text-[10.5px] text-fg-4 sm:max-w-[180px]">平台实例
            <select aria-label="平台实例" className="field mt-1 !h-9 w-full text-[12px]" value={platformId}
              onChange={(event) => setPlatformId(event.target.value)}>
              {platformIds.map((item) => <option key={item} value={item}>{item}</option>)}
            </select>
          </label>
          <div className="min-w-[180px] flex-[2] text-[10.5px] text-fg-4">对话 ID
            <Input aria-label="对话 ID" placeholder="输入对话 ID (如 conv-xxx)" value={conversationId}
              onChange={(event) => setConversationId(event.target.value)} className="mt-1 !h-9 text-[13px]" />
          </div>
          <Button type="submit" variant="tinted" tint="green" loading={loading} icon={<Search className="h-4 w-4" />}>查询</Button>
          <IconButton label="刷新检查点数据" size="sm" disabled={loading || !conversationId.trim()} onClick={() => void fetchData()}>
            <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin-slow' : ''}`} />
          </IconButton>
        </form>
        <p className="mt-1.5 text-[11px] leading-5 text-fg-4">检查点保存的是消息上下文快照；展示历史、长期记忆与平台消息档案不受影响。</p>
      </div>

      {conversationId && (
        <>
          <div className="flex flex-wrap items-center gap-1.5">
            <Button variant="tinted" tint="green" icon={<Plus className="h-4 w-4" />} disabled={busy}
              onClick={() => { setNewCheckpointName(''); setNewCheckpointDescription(''); setShowCreateModal(true); }}>创建检查点</Button>
            <Button variant="ghost" icon={<RotateCcw className="h-4 w-4" />} disabled={busy || checkpoints.length === 0}
              onClick={() => { setSelectedCheckpoint(''); setShowRevertModal(true); }}>恢复消息上下文</Button>
            <Button variant="ghost" icon={<GitBranch className="h-4 w-4" />} disabled={busy || checkpoints.length === 0}
              onClick={() => { setSelectedCheckpoint(''); setNewBranchName(''); setForkError(''); setShowForkModal(true); }}>Fork 分支</Button>
            <span className="num ml-auto text-[10.5px] text-fg-4">{platformId} · {conversationId}</span>
          </div>

          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            <StatTile label="检查点" value={<span className="num">{checkpoints.length}</span>} hint="可恢复到任一快照" tint="green" />
            <StatTile label="分支" value={<span className="num">{branches.length}</span>} hint="Fork 出的独立会话" />
            <StatTile label="变更记录" value={<span className="num">{mutations.length}</span>} hint="审计写入" />
            <StatTile label="最近快照" value={<span className="text-[12.5px]">{checkpoints[0] ? formatRelativeTime(checkpoints[0].created_at) : '—'}</span>} hint={checkpoints[0] ? formatTime(checkpoints[0].created_at) : '暂无'} />
          </div>

          <div className="grid items-start gap-3 xl:grid-cols-[minmax(0,1fr)_340px]">
            <div className="surface tint-green flex min-w-0 flex-col overflow-hidden !rounded-[16px]">
              <div className="flex flex-none items-center justify-between gap-2 border-b border-line p-3">
                <div className="min-w-0">
                  <h2 className="flex items-center gap-2 text-[14px] font-semibold text-fg"><History className="h-4 w-4 text-tint" />检查点</h2>
                  <p className="mt-0.5 text-[11px] text-fg-4">共 {checkpoints.length} 个快照 · 按创建时间排序</p>
                </div>
                {loading && <RefreshCw className="h-4 w-4 animate-spin-slow text-fg-4" />}
              </div>
              <div className="scroll min-h-0 flex-1 p-2">
                {loading && <div className="space-y-2 p-1"><div className="skeleton h-20" /><div className="skeleton h-20" /></div>}
                {!loading && checkpoints.length === 0 && (
                  <EmptyState tint="green" icon={<GitCommitHorizontal className="h-6 w-6" />} title="暂无检查点"
                    description="创建检查点后会在这里显示快照、水位与版本；可用于恢复模型消息上下文。" className="py-10" />
                )}
                {!loading && checkpoints.map((cp) => (
                  <div key={cp.checkpoint_id} className="mb-2 rounded-xl border border-line bg-glass p-3 last:mb-0">
                    <div className="flex flex-wrap items-center gap-1.5">
                      {kindBadge(cp.checkpoint_kind)}
                      <span className="min-w-0 truncate text-[13px] font-medium text-fg">{cp.name || '未命名检查点'}</span>
                      <span className="num ml-auto text-[10px] text-fg-4">{formatTime(cp.created_at)}</span>
                    </div>
                    {cp.description && <p className="mt-1 whitespace-pre-wrap break-words text-[11.5px] leading-5 text-fg-3">{cp.description}</p>}
                    <div className="mt-2 grid gap-1 rounded-lg border border-line bg-[rgb(0_0_0/0.1)] p-2 [[data-theme=light]_&]:bg-white/40">
                      <KeyValue label="水位" mono>#{cp.position}</KeyValue>
                      <KeyValue label="版本" mono>{cp.state_revision}</KeyValue>
                      <KeyValue label="来源">{cp.source || '—'}</KeyValue>
                      <KeyValue label="ID" mono>{cp.checkpoint_id}</KeyValue>
                    </div>
                    <div className="mt-2 flex justify-end">
                      <Button size="sm" variant="ghost" onClick={() => void handleTraceLineage(cp.checkpoint_id)} icon={<GitCommitHorizontal className="h-3.5 w-3.5" />}>血缘</Button>
                    </div>
                  </div>
                ))}
              </div>
            </div>

            <div className="min-w-0 space-y-3">
              <div className="surface tint-green flex flex-col overflow-hidden !rounded-[16px]">
                <div className="flex flex-none items-center justify-between gap-2 border-b border-line p-3">
                  <h2 className="flex items-center gap-2 text-[13.5px] font-semibold text-fg"><GitBranch className="h-4 w-4 text-tint" />分支</h2>
                  <span className="num text-[10.5px] text-fg-4">{branches.length} 个</span>
                </div>
                <div className="scroll max-h-[320px] min-h-0 flex-1 p-2">
                  {branches.length === 0 && <p className="px-2 py-6 text-center text-[11.5px] text-fg-4">暂无分支</p>}
                  {branches.map((branch) => (
                    <div key={branch.scope_id} className="mb-1.5 flex flex-wrap items-center justify-between gap-2 rounded-xl border border-line bg-glass px-3 py-2 last:mb-0">
                      <div className="min-w-0">
                        <p className="flex items-center gap-1.5 text-[12px] font-medium text-fg">
                          <span className="truncate">{branch.name || '未命名分支'}</span>
                          <Badge tone="purple">Fork</Badge>
                        </p>
                        <p className="mt-0.5 break-all font-mono text-[10px] text-fg-4">{branch.scope_id}</p>
                      </div>
                      <span className="num flex-none text-[10px] text-fg-4">{formatTime(branch.created_at)}</span>
                    </div>
                  ))}
                </div>
              </div>

              <div className="surface tint-green flex flex-col overflow-hidden !rounded-[16px]">
                <div className="flex flex-none items-center justify-between gap-2 border-b border-line p-3">
                  <h2 className="flex items-center gap-2 text-[13.5px] font-semibold text-fg"><ScrollText className="h-4 w-4 text-tint" />变更记录</h2>
                  <span className="num text-[10.5px] text-fg-4">{mutations.length} 条</span>
                </div>
                <div className="scroll max-h-[320px] min-h-0 flex-1 p-2">
                  {mutations.length === 0 && <p className="px-2 py-6 text-center text-[11.5px] text-fg-4">暂无变更记录</p>}
                  {mutations.map((item) => (
                    <div key={item.checkpoint_id} className="mb-1.5 rounded-xl border border-line bg-glass px-3 py-2 last:mb-0">
                      <div className="flex flex-wrap items-center gap-1.5">
                        {kindBadge(item.checkpoint_kind)}
                        <span className="truncate text-[11.5px] font-medium text-fg">{item.reason || '未记录原因'}</span>
                        <span className="num ml-auto text-[10px] text-fg-4">{formatTime(item.created_at)}</span>
                      </div>
                      <p className="mt-0.5 truncate font-mono text-[10px] text-fg-4">{item.source || '—'} · {item.checkpoint_id}</p>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          </div>
        </>
      )}

      <Modal open={showCreateModal} onClose={() => { if (!busy) setShowCreateModal(false); }} title="创建检查点"
        description="为当前消息上下文保存一个可恢复的快照。" icon={<Plus className="h-[18px] w-[18px]" />} className="tint-green"
        footer={<>
          <Button variant="ghost" disabled={busy} onClick={() => setShowCreateModal(false)}>取消</Button>
          <Button variant="tinted" tint="green" loading={busy} onClick={() => void handleCreate()}>创建</Button>
        </>}>
        <div className="space-y-3">
          <Field label="名称（可选）" hint="留空时由系统自动命名">
            <Input value={newCheckpointName} disabled={busy} placeholder="默认自动命名" onChange={(event) => setNewCheckpointName(event.target.value)} />
          </Field>
          <Field label="描述（可选）">
            <Textarea rows={3} value={newCheckpointDescription} disabled={busy} placeholder="说明这个检查点的用途"
              onChange={(event) => setNewCheckpointDescription(event.target.value)} />
          </Field>
        </div>
      </Modal>

      <Modal open={showForkModal} onClose={() => { if (!busy) setShowForkModal(false); }} title="Fork 分支"
        description="从当前会话或指定检查点创建一个独立分支。" icon={<GitBranch className="h-[18px] w-[18px]" />} className="tint-green"
        footer={<>
          <Button variant="ghost" disabled={busy} onClick={() => setShowForkModal(false)}>取消</Button>
          <Button variant="tinted" tint="green" loading={busy} onClick={() => void handleFork()}>Fork</Button>
        </>}>
        <div className="space-y-3">
          <Field label="分支名" required hint="例如 retry_v2" error={forkError}>
            <Input value={newBranchName} disabled={busy} placeholder="如: retry_v2" onChange={(event) => { setNewBranchName(event.target.value); setForkError(''); }} />
          </Field>
          <Field label="从检查点" hint="默认使用最新检查点">
            <select className="field !h-9 w-full text-[12.5px]" aria-label="从检查点" value={selectedCheckpoint}
              disabled={busy} onChange={(event) => setSelectedCheckpoint(event.target.value)}>
              <option value="">最新检查点</option>
              {checkpointOptions.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
            </select>
          </Field>
        </div>
      </Modal>

      <Modal open={showRevertModal} onClose={() => { if (!busy) setShowRevertModal(false); }} title="从检查点恢复消息上下文"
        description="当前上下文会先备份，展示历史不会改变。" icon={<RotateCcw className="h-[18px] w-[18px]" />} className="tint-orange"
        footer={<>
          <Button variant="ghost" disabled={busy} onClick={() => setShowRevertModal(false)}>取消</Button>
          <Button variant="danger" loading={busy} disabled={!selectedCheckpoint} onClick={() => void handleRevert()}>执行</Button>
        </>}>
        <div className="space-y-3">
          <Field label="选择检查点" required>
            <select className="field !h-9 w-full text-[12.5px]" aria-label="选择检查点" value={selectedCheckpoint}
              disabled={busy} onChange={(event) => setSelectedCheckpoint(event.target.value)}>
              <option value="">请选择...</option>
              {checkpointOptions.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
            </select>
          </Field>
          <p className="text-[11.5px] leading-5 text-fg-3">恢复会通过管理接口保存，并带上当前上下文修订号；若修订号已变化，操作会失败并保留原数据。</p>
        </div>
      </Modal>

      <Modal open={showLineageModal} onClose={() => setShowLineageModal(false)} title="检查点血缘" size="lg"
        icon={<GitCommitHorizontal className="h-[18px] w-[18px]" />} className="tint-purple">
        {lineageLoading ? (
          <div className="space-y-2"><div className="skeleton h-16" /><div className="skeleton h-16" /></div>
        ) : lineage.length === 0 ? (
          <p className="py-8 text-center text-[12.5px] text-fg-3">没有血缘记录</p>
        ) : (
          <div className="space-y-2 pb-1">
            {lineage.map((item, index) => (
              <div key={item.checkpoint_id} className="rounded-xl border border-line bg-glass p-3">
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div className="min-w-0">
                    <div className="flex items-center gap-1.5">
                      <Badge tone="purple">{index + 1}</Badge>
                      <span className="truncate text-[12.5px] font-medium text-fg">{item.name || item.checkpoint_id}</span>
                    </div>
                    <p className="mt-1 text-[11.5px] leading-5 text-fg-3">{item.description || '无描述'}</p>
                  </div>
                  <div className="flex-none text-right text-[10.5px] text-fg-4">
                    <div>{item.checkpoint_kind}</div>
                    <div>{formatTime(item.created_at)}</div>
                  </div>
                </div>
                <p className="mt-2 break-all font-mono text-[10px] text-fg-4">{item.checkpoint_id}</p>
              </div>
            ))}
          </div>
        )}
      </Modal>
    </div>
  );
}
