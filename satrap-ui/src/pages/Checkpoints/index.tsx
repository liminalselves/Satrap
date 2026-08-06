import { useState } from 'react';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { Input } from '@/components/ui/Input';
import { Badge } from '@/components/ui/Badge';
import { Modal } from '@/components/ui/Modal';
import { Table, TableHeader, TableBody, TableRow, TableHead, TableCell } from '@/components/ui/Table';
import { toast } from '@/components/ui/Toast';
import { checkpointApi } from '@/api/checkpoint';
import { formatTime } from '@/utils/format';
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
  const [revertMode, setRevertMode] = useState<'rollback' | 'retry'>('rollback');

  const fetchData = async () => {
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
  };

  const handleCreate = async () => {
    try {
      await checkpointApi.create(conversationId, newCheckpointName || undefined);
      toast('success', '检查点已创建');
      setShowCreateModal(false);
      setNewCheckpointName('');
      fetchData();
    } catch {
      toast('error', '创建失败');
    }
  };

  const handleFork = async () => {
    try {
      const result = await checkpointApi.fork(conversationId, newBranchName, selectedCheckpoint || undefined);
      toast('success', `已分支: ${result.conversation_id}`);
      setShowForkModal(false);
      setNewBranchName('');
      fetchData();
    } catch {
      toast('error', 'Fork 失败');
    }
  };

  const handleRevert = async () => {
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
  };

  const getKindBadge = (kind: string) => {
    const variants: Record<string, 'success' | 'warning' | 'error' | 'info'> = {
      manual: 'info',
      auto: 'success',
      stable: 'warning',
      fork: 'info',
    };
    return <Badge variant={variants[kind] || 'default'}>{kind}</Badge>;
  };

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-text-primary">检查点管理</h1>
        <p className="text-text-secondary mt-1">管理对话的检查点、分支和变更记录</p>
      </div>

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
          <div className="grid grid-cols-3 gap-4">
            <Card>
              <div className="text-center">
                <p className="text-3xl font-bold text-text-primary">{checkpoints.length}</p>
                <p className="text-sm text-text-secondary">检查点</p>
              </div>
            </Card>
            <Card>
              <div className="text-center">
                <p className="text-3xl font-bold text-text-primary">{branches.length}</p>
                <p className="text-sm text-text-secondary">分支</p>
              </div>
            </Card>
            <Card>
              <div className="text-center">
                <p className="text-3xl font-bold text-text-primary">{mutations.length}</p>
                <p className="text-sm text-text-secondary">变更记录</p>
              </div>
            </Card>
          </div>

          {/* 检查点列表 */}
          <Card>
            <h3 className="text-lg font-semibold text-text-primary mb-4">检查点列表</h3>
            {checkpoints.length === 0 ? (
              <p className="text-text-secondary text-center py-8">暂无检查点</p>
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>类型</TableHead>
                    <TableHead>名称</TableHead>
                    <TableHead>水位</TableHead>
                    <TableHead>版本</TableHead>
                    <TableHead>来源</TableHead>
                    <TableHead>时间</TableHead>
                    <TableHead>ID</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {checkpoints.map((cp) => (
                    <TableRow key={cp.checkpoint_id}>
                      <TableCell>{getKindBadge(cp.checkpoint_kind)}</TableCell>
                      <TableCell>{cp.name || '-'}</TableCell>
                      <TableCell>{cp.position}</TableCell>
                      <TableCell>{cp.state_revision}</TableCell>
                      <TableCell>{cp.source}</TableCell>
                      <TableCell>{formatTime(cp.created_at)}</TableCell>
                      <TableCell className="font-mono text-xs">{cp.checkpoint_id.slice(0, 8)}...</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </Card>

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
                    className="flex items-center justify-between p-3 rounded-glass-sm bg-bg-glass"
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
          <Card>
            <h3 className="text-lg font-semibold text-text-primary mb-4">变更记录</h3>
            {mutations.length === 0 ? (
              <p className="text-text-secondary text-center py-8">暂无变更记录</p>
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>类型</TableHead>
                    <TableHead>来源</TableHead>
                    <TableHead>原因</TableHead>
                    <TableHead>时间</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {mutations.map((m) => (
                    <TableRow key={m.checkpoint_id}>
                      <TableCell>{getKindBadge(m.checkpoint_kind)}</TableCell>
                      <TableCell>{m.source}</TableCell>
                      <TableCell>{m.reason || '-'}</TableCell>
                      <TableCell>{formatTime(m.created_at)}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </Card>
        </>
      )}

      {/* 创建检查点模态框 */}
      <Modal
        open={showCreateModal}
        onClose={() => setShowCreateModal(false)}
        title="创建检查点"
      >
        <div className="space-y-4">
          <div>
            <label className="block text-sm font-medium text-text-secondary mb-1">
              名称（可选）
            </label>
            <Input
              value={newCheckpointName}
              onChange={(e) => setNewCheckpointName(e.target.value)}
              placeholder="默认自动命名"
            />
          </div>
          <div className="flex gap-3 pt-4">
            <Button variant="primary" onClick={handleCreate} className="flex-1">
              创建
            </Button>
            <Button variant="default" onClick={() => setShowCreateModal(false)}>
              取消
            </Button>
          </div>
        </div>
      </Modal>

      {/* Fork 模态框 */}
      <Modal
        open={showForkModal}
        onClose={() => setShowForkModal(false)}
        title="Fork 分支"
      >
        <div className="space-y-4">
          <div>
            <label className="block text-sm font-medium text-text-secondary mb-1">
              分支名
            </label>
            <Input
              value={newBranchName}
              onChange={(e) => setNewBranchName(e.target.value)}
              placeholder="如: retry_v2"
              required
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-text-secondary mb-1">
              从检查点（默认最新）
            </label>
            <select
              className="glass-input w-full"
              value={selectedCheckpoint}
              onChange={(e) => setSelectedCheckpoint(e.target.value)}
            >
              <option value="">最新检查点</option>
              {checkpoints.map((cp) => (
                <option key={cp.checkpoint_id} value={cp.checkpoint_id}>
                  {cp.name || cp.checkpoint_id.slice(0, 8)} ({cp.checkpoint_kind})
                </option>
              ))}
            </select>
          </div>
          <div className="flex gap-3 pt-4">
            <Button variant="primary" onClick={handleFork} className="flex-1" disabled={!newBranchName}>
              Fork
            </Button>
            <Button variant="default" onClick={() => setShowForkModal(false)}>
              取消
            </Button>
          </div>
        </div>
      </Modal>

      {/* 回滚/重试模态框 */}
      <Modal
        open={showRevertModal}
        onClose={() => setShowRevertModal(false)}
        title="回滚 / 重试"
      >
        <div className="space-y-4">
          <div>
            <label className="block text-sm font-medium text-text-secondary mb-1">
              选择检查点
            </label>
            <select
              className="glass-input w-full"
              value={selectedCheckpoint}
              onChange={(e) => setSelectedCheckpoint(e.target.value)}
            >
              <option value="">请选择...</option>
              {checkpoints.map((cp) => (
                <option key={cp.checkpoint_id} value={cp.checkpoint_id}>
                  {cp.name || cp.checkpoint_id.slice(0, 8)} ({cp.checkpoint_kind})
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className="block text-sm font-medium text-text-secondary mb-2">
              操作方式
            </label>
            <div className="flex gap-4">
              <label className="flex items-center gap-2 cursor-pointer">
                <input
                  type="radio"
                  checked={revertMode === 'rollback'}
                  onChange={() => setRevertMode('rollback')}
                  className="text-accent"
                />
                <span className="text-text-primary">回滚（删除未来检查点）</span>
              </label>
              <label className="flex items-center gap-2 cursor-pointer">
                <input
                  type="radio"
                  checked={revertMode === 'retry'}
                  onChange={() => setRevertMode('retry')}
                  className="text-accent"
                />
                <span className="text-text-primary">重试（保留未来检查点）</span>
              </label>
            </div>
          </div>
          <div className="flex gap-3 pt-4">
            <Button
              variant="primary"
              onClick={handleRevert}
              className="flex-1"
              disabled={!selectedCheckpoint}
            >
              执行
            </Button>
            <Button variant="default" onClick={() => setShowRevertModal(false)}>
              取消
            </Button>
          </div>
        </div>
      </Modal>
    </div>
  );
}
