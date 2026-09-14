import { useState, useCallback, useMemo } from 'react';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { Input } from '@/components/ui/Input';
import { Badge } from '@/components/ui/Badge';
import { Modal } from '@/components/ui/Modal';
import { toast } from '@/components/ui/Toast';
import { checkpointApi } from '@/api/checkpoint';
import { useBackendStore } from '@/stores/useBackendStore';
import { formatTime } from '@/utils/format';
import { PageHeader, DataTable, FormModal, Column, FormField, StatCard, StatCardGrid } from '@/components/common';
import { Search, GitBranch, RotateCcw, RefreshCw, Plus, GitCommitHorizontal } from 'lucide-react';
import type { Checkpoint } from '@/api/types';

export function Checkpoints() {
  const { health } = useBackendStore();
  const [platformId, setPlatformId] = useState('local');
  const [conversationId, setConversationId] = useState('');
  const [loading, setLoading] = useState(false);
  const [checkpoints, setCheckpoints] = useState<Checkpoint[]>([]);
  const [branches, setBranches] = useState<Checkpoint[]>([]);
  const [mutations, setMutations] = useState<Checkpoint[]>([]);
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [showForkModal, setShowForkModal] = useState(false);
  const [showRevertModal, setShowRevertModal] = useState(false);
  const [showLineageModal, setShowLineageModal] = useState(false);
  const [selectedCheckpoint, setSelectedCheckpoint] = useState<string>('');
  const [newCheckpointName, setNewCheckpointName] = useState('');
  const [newCheckpointDescription, setNewCheckpointDescription] = useState('');
  const [newBranchName, setNewBranchName] = useState('');
  const [revertMode, setRevertMode] = useState<'rollback' | 'retry'>('rollback');
  const [lineage, setLineage] = useState<Checkpoint[]>([]);
  const [lineageLoading, setLineageLoading] = useState(false);

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

  const platformIds = useMemo(
    () => Array.from(new Set(['local', 'chat', ...Object.keys(health?.adapters || {})])),
    [health?.adapters],
  );

  const handleCreate = useCallback(async () => {
    try {
      await checkpointApi.create(
        platformId,
        conversationId,
        newCheckpointName || undefined,
        newCheckpointDescription || undefined,
      );
      toast('success', '检查点已创建');
      setShowCreateModal(false);
      setNewCheckpointName('');
      setNewCheckpointDescription('');
      fetchData();
    } catch (e) {
      toast('error', '创建失败: ' + (e instanceof Error ? e.message : '未知错误'));
    }
  }, [conversationId, fetchData, newCheckpointDescription, newCheckpointName, platformId]);

  const handleFork = useCallback(async () => {
    try {
      const result = await checkpointApi.fork(
        platformId,
        conversationId,
        newBranchName,
        selectedCheckpoint || undefined,
      );
      toast('success', `已分支: ${result.conversation_id}`);
      setShowForkModal(false);
      setNewBranchName('');
      fetchData();
    } catch (e) {
      toast('error', 'Fork 失败: ' + (e instanceof Error ? e.message : '未知错误'));
    }
  }, [conversationId, fetchData, newBranchName, platformId, selectedCheckpoint]);

  const handleRevert = useCallback(async () => {
    if (!selectedCheckpoint) return;
    if (revertMode === 'rollback' && !window.confirm('回滚会丢弃该检查点之后的状态, 确定继续吗?')) {
      return;
    }
    try {
      if (revertMode === 'rollback') {
        await checkpointApi.rollback(platformId, conversationId, selectedCheckpoint);
        toast('success', '已回滚');
      } else {
        await checkpointApi.retry(platformId, conversationId, selectedCheckpoint);
        toast('success', '已重试');
      }
      setShowRevertModal(false);
      fetchData();
    } catch (e) {
      toast('error', '操作失败: ' + (e instanceof Error ? e.message : '未知错误'));
    }
  }, [conversationId, fetchData, platformId, revertMode, selectedCheckpoint]);

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
    {
      key: 'description',
      title: '描述',
      render: (cp) => cp.description || '-',
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
    {
      key: 'lineage',
      title: '操作',
      render: (cp) => (
        <Button variant="ghost" size="sm" onClick={() => handleTraceLineage(cp.checkpoint_id)}>
          <GitCommitHorizontal className="h-4 w-4 mr-1" />
          血缘
        </Button>
      ),
    },
  ], [getKindBadge, handleTraceLineage]);

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
    { key: 'description', label: '描述（可选）', type: 'textarea', placeholder: '说明这个检查点的用途' },
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
      key: 'mode',
      label: '操作方式',
      type: 'select',
      options: [
        { value: 'rollback', label: '回滚到检查点' },
        { value: 'retry', label: '从检查点重试' },
      ],
    },
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
          <select
            className="glass-input min-w-48"
            value={platformId}
            onChange={(event) => setPlatformId(event.target.value)}
            aria-label="平台实例"
          >
            {platformIds.map((item) => <option key={item} value={item}>{item}</option>)}
          </select>
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
            <Button
              variant="default"
              onClick={() => {
                setNewCheckpointName('');
                setNewCheckpointDescription('');
                setShowCreateModal(true);
              }}
            >
              <Plus className="h-4 w-4 mr-2" />
              创建检查点
            </Button>
            <Button
              variant="default"
              onClick={() => {
                setSelectedCheckpoint('');
                setRevertMode('rollback');
                setShowRevertModal(true);
              }}
              disabled={checkpoints.length === 0}
            >
              <RotateCcw className="h-4 w-4 mr-2" />
              回滚 / 重试
            </Button>
            <Button
              variant="default"
              onClick={() => {
                setSelectedCheckpoint('');
                setNewBranchName('');
                setShowForkModal(true);
              }}
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
        values={{ name: newCheckpointName, description: newCheckpointDescription }}
        onChange={(key, value) => {
          if (key === 'name') setNewCheckpointName(value as string);
          if (key === 'description') setNewCheckpointDescription(value as string);
        }}
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
          if (key === 'mode') setRevertMode(value as 'rollback' | 'retry');
        }}
        onSubmit={handleRevert}
        submitText="执行"
      />

      <Modal
        open={showLineageModal}
        onClose={() => setShowLineageModal(false)}
        title="检查点血缘"
        size="lg"
      >
        {lineageLoading ? (
          <p className="py-8 text-center text-text-secondary">加载中...</p>
        ) : lineage.length === 0 ? (
          <p className="py-8 text-center text-text-secondary">没有血缘记录</p>
        ) : (
          <div className="space-y-3 pb-1">
            {lineage.map((item, index) => (
              <div key={item.checkpoint_id} className="relative rounded-sm bg-glass p-3">
                <div className="flex items-center justify-between gap-3">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <Badge variant="info">{index + 1}</Badge>
                      <span className="font-medium text-text-primary">{item.name || item.checkpoint_id}</span>
                    </div>
                    <p className="mt-1 text-sm text-text-secondary">{item.description || '无描述'}</p>
                  </div>
                  <div className="shrink-0 text-right text-xs text-text-secondary">
                    <div>{item.checkpoint_kind}</div>
                    <div>{formatTime(item.created_at)}</div>
                  </div>
                </div>
                <div className="mt-2 break-all font-mono text-xs text-text-secondary">
                  {item.checkpoint_id}
                </div>
              </div>
            ))}
          </div>
        )}
      </Modal>
    </div>
  );
}
