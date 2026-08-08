import { useState, useCallback, useMemo } from 'react';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { Input } from '@/components/ui/Input';
import { Badge } from '@/components/ui/Badge';
import { toast } from '@/components/ui/Toast';
import { checkpointApi } from '@/api/checkpoint';
import { formatTime } from '@/utils/format';
import { PageHeader, DataTable, FormModal, Column, FormField, StatCard, StatCardGrid } from '@/components/common';
import { Search, GitBranch, RotateCcw, RefreshCw, Plus } from 'lucide-react';
import type { Checkpoint } from '@/api/types';

export function Checkpoints() {
  const [conversationId, setConversationId] = useState('');
  const [loading, setLoading] = useState(false);
  const [checkpoints, setCheckpoints] = useState<Checkpoint[]>([]);
  const [branches, setBranches] = useState<Checkpoint[]>([]);
  const [mutations, setMutations] = useState<Checkpoint[]>([]);
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [showForkModal, setShowForkModal] = useState(false);
  const [showRevertModal, setShowRevertModal] = useState(false);
  const [selectedCheckpoint, setSelectedCheckpoint] = useState<string>('');
  const [newCheckpointName, setNewCheckpointName] = useState('');
  const [newBranchName, setNewBranchName] = useState('');
  const [revertMode] = useState<'rollback' | 'retry'>('rollback');

  const fetchData = useCallback(async () => {
    if (!conversationId.trim()) {
      toast('warning', '请输入对话 ID');
      return;
    }

    setLoading(true);
    try {
      const [cpData, branchData, mutationData] = await Promise.all([
        checkpointApi.list(conversationId),
        checkpointApi.listBranches(conversationId),
        checkpointApi.listMutations(conversationId),
      ]);
      setCheckpoints(cpData.checkpoints);
      setBranches(branchData.branches);
      setMutations(mutationData.mutations);
    } catch (e) {
      toast('error', '获取数据失败: ' + (e instanceof Error ? e.message : '未知错误'));
    } finally {
      setLoading(false);
    }
  }, [conversationId]);

  const handleCreate = useCallback(async () => {
    try {
      await checkpointApi.create(conversationId, newCheckpointName || undefined);
      toast('success', '检查点已创建');
      setShowCreateModal(false);
      setNewCheckpointName('');
      fetchData();
    } catch {
      toast('error', '创建失败');
    }
  }, [conversationId, newCheckpointName, fetchData]);

  const handleFork = useCallback(async () => {
    try {
      const result = await checkpointApi.fork(conversationId, newBranchName, selectedCheckpoint || undefined);
      toast('success', `已分支: ${result.conversation_id}`);
      setShowForkModal(false);
      setNewBranchName('');
      fetchData();
    } catch {
      toast('error', 'Fork 失败');
    }
  }, [conversationId, newBranchName, selectedCheckpoint, fetchData]);

  const handleRevert = useCallback(async () => {
    if (!selectedCheckpoint) return;
    try {
      if (revertMode === 'rollback') {
        await checkpointApi.rollback(conversationId, selectedCheckpoint);
        toast('success', '已回滚');
      } else {
        await checkpointApi.retry(conversationId, selectedCheckpoint);
        toast('success', '已重试');
      }
      setShowRevertModal(false);
      fetchData();
    } catch {
      toast('error', '操作失败');
    }
  }, [conversationId, selectedCheckpoint, revertMode, fetchData]);

  const getKindBadge = useCallback((kind: string) => {
    const variants: Record<string, 'success' | 'warning' | 'error' | 'info'> = {
      manual: 'info',
      auto: 'success',
      stable: 'warning',
      fork: 'info',
    };
    return <Badge variant={variants[kind] || 'default'}>{kind}</Badge>;
  }, []);

  // 检查点表格列
  const checkpointColumns: Column<Checkpoint>[] = useMemo(() => [
    {
      key: 'kind',
      title: '类型',
      render: (cp) => getKindBadge(cp.checkpoint_kind),
    },
    {
      key: 'name',
      title: '名称',
      render: (cp) => cp.name || '-',
    },
    { key: 'position', title: '水位' },
    { key: 'state_revision', title: '版本' },
    { key: 'source', title: '来源' },
    {
      key: 'created_at',
      title: '时间',
      render: (cp) => formatTime(cp.created_at),
    },
    {
      key: 'id',
      title: 'ID',
      render: (cp) => (
        <span className="font-mono text-xs">{cp.checkpoint_id.slice(0, 8)}...</span>
      ),
    },
  ], [getKindBadge]);

  // 变更记录表格列
  const mutationColumns: Column<Checkpoint>[] = useMemo(() => [
    {
      key: 'kind',
      title: '类型',
      render: (m) => getKindBadge(m.checkpoint_kind),
    },
    { key: 'source', title: '来源' },
    {
      key: 'reason',
      title: '原因',
      render: (m) => m.reason || '-',
    },
    {
      key: 'created_at',
      title: '时间',
      render: (m) => formatTime(m.created_at),
    },
  ], [getKindBadge]);

  // 创建检查点表单字段
  const createFields: FormField[] = useMemo(() => [
    { key: 'name', label: '名称（可选）', placeholder: '默认自动命名' },
  ], []);

  // Fork 表单字段
  const forkFields: FormField[] = useMemo(() => [
    { key: 'branch_name', label: '分支名', placeholder: '如: retry_v2', required: true },
    {
      key: 'checkpoint',
      label: '从检查点（默认最新）',
      type: 'select',
      options: [
        { value: '', label: '最新检查点' },
        ...checkpoints.map((cp) => ({
          value: cp.checkpoint_id,
          label: `${cp.name || cp.checkpoint_id.slice(0, 8)} (${cp.checkpoint_kind})`,
        })),
      ],
    },
  ], [checkpoints]);

  // 回滚表单字段
  const revertFields: FormField[] = useMemo(() => [
    {
      key: 'checkpoint',
      label: '选择检查点',
      type: 'select',
      options: [
        { value: '', label: '请选择...' },
        ...checkpoints.map((cp) => ({
          value: cp.checkpoint_id,
          label: `${cp.name || cp.checkpoint_id.slice(0, 8)} (${cp.checkpoint_kind})`,
        })),
      ],
    },
  ], [checkpoints]);

  return (
    <div className="space-y-6">
      <PageHeader
        title="检查点管理"
        description="管理对话的检查点、分支和变更记录"
      />

      {/* 搜索栏 */}
      <Card>
        <div className="flex gap-4">
          <div className="flex-1">
            <Input
              placeholder="输入对话 ID (如 conv-xxx)"
              value={conversationId}
              onChange={(e) => setConversationId(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && fetchData()}
            />
          </div>
          <Button variant="primary" onClick={fetchData} disabled={loading}>
            <Search className="h-4 w-4 mr-2" />
            查询
          </Button>
        </div>
      </Card>

      {conversationId && (
        <>
          {/* 操作按钮 */}
          <div className="flex gap-3">
            <Button variant="default" onClick={() => setShowCreateModal(true)}>
              <Plus className="h-4 w-4 mr-2" />
              创建检查点
            </Button>
            <Button
              variant="default"
              onClick={() => setShowRevertModal(true)}
              disabled={checkpoints.length === 0}
            >
              <RotateCcw className="h-4 w-4 mr-2" />
              回滚 / 重试
            </Button>
            <Button
              variant="default"
              onClick={() => setShowForkModal(true)}
              disabled={checkpoints.length === 0}
            >
              <GitBranch className="h-4 w-4 mr-2" />
              Fork 分支
            </Button>
            <Button variant="ghost" onClick={fetchData}>
              <RefreshCw className="h-4 w-4 mr-2" />
              刷新
            </Button>
          </div>

          {/* 统计 */}
          <StatCardGrid className="grid-cols-3">
            <StatCard
              icon={<span className="text-3xl font-bold">{checkpoints.length}</span>}
              label="检查点"
              value=""
            />
            <StatCard
              icon={<span className="text-3xl font-bold">{branches.length}</span>}
              label="分支"
              value=""
            />
            <StatCard
              icon={<span className="text-3xl font-bold">{mutations.length}</span>}
              label="变更记录"
              value=""
            />
          </StatCardGrid>

          {/* 检查点列表 */}
          <DataTable
            columns={checkpointColumns}
            data={checkpoints}
            keyExtractor={(cp) => cp.checkpoint_id}
            emptyMessage="暂无检查点"
          />

          {/* 分支列表 */}
          <Card>
            <h3 className="text-lg font-semibold text-text-primary mb-4">分支</h3>
            {branches.length === 0 ? (
              <p className="text-text-secondary text-center py-8">暂无分支</p>
            ) : (
              <div className="space-y-2">
                {branches.map((b) => (
                  <div
                    key={b.scope_id}
                    className="flex items-center justify-between p-3 rounded-sm bg-glass"
                  >
                    <div>
                      <span className="font-medium text-text-primary">{b.scope_id}</span>
                      {b.name && <Badge variant="info" className="ml-2">{b.name}</Badge>}
                    </div>
                    <span className="text-sm text-text-secondary">{formatTime(b.created_at)}</span>
                  </div>
                ))}
              </div>
            )}
          </Card>

          {/* 变更记录 */}
          <DataTable
            columns={mutationColumns}
            data={mutations}
            keyExtractor={(m) => m.checkpoint_id}
            emptyMessage="暂无变更记录"
          />
        </>
      )}

      {/* 创建检查点模态框 */}
      <FormModal
        open={showCreateModal}
        onClose={() => setShowCreateModal(false)}
        title="创建检查点"
        fields={createFields}
        values={{ name: newCheckpointName }}
        onChange={(_, value) => setNewCheckpointName(value as string)}
        onSubmit={handleCreate}
        submitText="创建"
      />

      {/* Fork 模态框 */}
      <FormModal
        open={showForkModal}
        onClose={() => setShowForkModal(false)}
        title="Fork 分支"
        fields={forkFields}
        values={{ branch_name: newBranchName, checkpoint: selectedCheckpoint }}
        onChange={(key, value) => {
          if (key === 'branch_name') setNewBranchName(value as string);
          if (key === 'checkpoint') setSelectedCheckpoint(value as string);
        }}
        onSubmit={handleFork}
        submitText="Fork"
      />

      {/* 回滚/重试模态框 */}
      <FormModal
        open={showRevertModal}
        onClose={() => setShowRevertModal(false)}
        title="回滚 / 重试"
        fields={revertFields}
        values={{ checkpoint: selectedCheckpoint, mode: revertMode }}
        onChange={(key, value) => {
          if (key === 'checkpoint') setSelectedCheckpoint(value as string);
        }}
        onSubmit={handleRevert}
        submitText="执行"
      />
    </div>
  );
}
