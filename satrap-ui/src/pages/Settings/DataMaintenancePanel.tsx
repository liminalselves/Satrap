import { useCallback, useMemo, useState } from 'react';
import { RefreshCw, RotateCcw, ShieldAlert, Trash2 } from 'lucide-react';
import { storageApi, type StorageAuditItem } from '@/api/storage';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { toast } from '@/components/ui/Toast';

const CATEGORY_LABELS: Record<string, string> = {
  orphan_session_directory: '孤儿目录',
  orphan_database_rows: '孤儿记录',
  broken_user_reference: '断裂引用',
  detached_platform: '脱离平台',
  trash_entry: '回收项',
  invalid_manifest: '无效清单',
  unsafe_entry: '不安全路径',
};

function formatBytes(value: number): string {
  // 格式化数据大小
  if (value < 1024) return value + ' B';
  if (value < 1024 * 1024) return (value / 1024).toFixed(1) + ' KiB';
  if (value < 1024 * 1024 * 1024) return (value / 1024 / 1024).toFixed(1) + ' MiB';
  return (value / 1024 / 1024 / 1024).toFixed(1) + ' GiB';
}

export function DataMaintenancePanel({
  backendRunning,
  controlAvailable,
}: {
  backendRunning: boolean;
  controlAvailable: boolean;
}) {
  const [items, setItems] = useState<StorageAuditItem[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(false);
  const [scanned, setScanned] = useState(false);
  const [retentionDays, setRetentionDays] = useState(30);

  const scan = useCallback(async () => {
    if (!backendRunning && !controlAvailable) {
      toast('warning', '数据审计需要平台后端或控制服务运行');
      return;
    }
    setLoading(true);
    try {
      const result = await storageApi.audit(backendRunning);
      setItems(result.items);
      setSelected(new Set());
      setScanned(true);
      toast('success', '扫描完成, 发现 ' + result.summary.count + ' 项');
    } catch (error) {
      toast('error', '扫描失败: ' + (error instanceof Error ? error.message : '未知错误'));
    } finally {
      setLoading(false);
    }
  }, [backendRunning, controlAvailable]);

  const safeItems = useMemo(
    () => items.filter((item) => item.auto_safe),
    [items],
  );
  const totalSize = useMemo(
    () => items.reduce((total, item) => total + item.size_bytes, 0),
    [items],
  );
  const selectedTrash = useMemo(
    () => items.filter(
      (item) => selected.has(item.item_id)
        && item.category === 'trash_entry'
        && Boolean(item.details.archive_id),
    ),
    [items, selected],
  );

  const toggle = useCallback((itemId: string) => {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(itemId)) next.delete(itemId);
      else next.add(itemId);
      return next;
    });
  }, []);

  const cleanup = useCallback(async () => {
    const targets = [...selected].filter(
      (itemId) => items.find((item) => item.item_id === itemId)?.auto_safe,
    );
    if (targets.length === 0) {
      toast('warning', '请选择可安全清理的项目');
      return;
    }
    if (!confirm('确定将选中的 ' + targets.length + ' 项孤儿数据移入回收区或清理引用吗?')) return;
    setLoading(true);
    try {
      const result = await storageApi.cleanup(targets, backendRunning);
      const failed = result.results.filter((item) => !item.ok);
      toast(
        failed.length === 0 ? 'success' : 'warning',
        failed.length === 0 ? '孤儿数据清理完成' : '有 ' + failed.length + ' 项清理失败',
      );
      await scan();
    } catch (error) {
      toast('error', '清理失败: ' + (error instanceof Error ? error.message : '未知错误'));
    } finally {
      setLoading(false);
    }
  }, [backendRunning, items, scan, selected]);

  const restore = useCallback(async (item: StorageAuditItem) => {
    const archiveId = item.details.archive_id;
    if (!archiveId) {
      toast('warning', '该回收项不是完整回收包, 无法恢复');
      return;
    }
    setLoading(true);
    try {
      await storageApi.restore(item.platform_id, archiveId, backendRunning);
      toast('success', '会话已恢复: ' + item.session_id);
      await scan();
    } catch (error) {
      toast('error', '恢复失败: ' + (error instanceof Error ? error.message : '未知错误'));
    } finally {
      setLoading(false);
    }
  }, [backendRunning, scan]);

  const purge = useCallback(async (item: StorageAuditItem) => {
    const archiveId = item.details.archive_id;
    if (!archiveId) return;
    if (!confirm('永久删除回收项 "' + archiveId + '"? 此操作无法恢复')) return;
    setLoading(true);
    try {
      await storageApi.purge(item.platform_id, archiveId, backendRunning);
      toast('success', '回收项已永久删除');
      await scan();
    } catch (error) {
      toast('error', '永久删除失败: ' + (error instanceof Error ? error.message : '未知错误'));
    } finally {
      setLoading(false);
    }
  }, [backendRunning, scan]);

  const purgeBatch = useCallback(async (byRetention: boolean) => {
    if (!byRetention && selectedTrash.length === 0) {
      toast('warning', '请选择需要永久删除的回收项');
      return;
    }
    const description = byRetention
      ? '永久删除所有存放超过 ' + retentionDays + ' 天的回收项'
      : '永久删除选中的 ' + selectedTrash.length + ' 个回收项';
    if (!confirm(description + '? 此操作无法恢复')) return;
    setLoading(true);
    try {
      const result = await storageApi.purgeBatch(
        byRetention
          ? { older_than_days: retentionDays }
          : {
            archive_refs: selectedTrash.map((item) => ({
              platform_id: item.platform_id,
              archive_id: item.details.archive_id || '',
            })),
          },
        backendRunning,
      );
      const failed = result.results.filter((item) => !item.ok);
      toast(
        failed.length === 0 ? 'success' : 'warning',
        failed.length === 0
          ? '回收区清理完成, 共删除 ' + result.results.length + ' 项'
          : '有 ' + failed.length + ' 项删除失败',
      );
      await scan();
    } catch (error) {
      toast('error', '回收区清理失败: ' + (error instanceof Error ? error.message : '未知错误'));
    } finally {
      setLoading(false);
    }
  }, [backendRunning, retentionDays, scan, selectedTrash]);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <p className="font-medium text-text-primary">孤儿数据与回收区</p>
          <p className="text-sm text-text-secondary">
            扫描操作只读; 自动清理只处理明确安全的孤儿目录、领域记录和断裂引用
          </p>
        </div>
        <div className="flex gap-2">
          <Button variant="default" onClick={scan} disabled={loading || (!backendRunning && !controlAvailable)}>
            <RefreshCw className={'h-4 w-4 mr-2 ' + (loading ? 'animate-spin' : '')} />
            扫描
          </Button>
          <Button variant="danger" onClick={cleanup} disabled={loading || selected.size === 0}>
            <Trash2 className="h-4 w-4 mr-2" />
            清理选中项
          </Button>
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-2 rounded-sm border border-border-glass p-3">
        <span className="text-sm text-text-secondary">回收区保留</span>
        <input
          type="number"
          min={0}
          value={retentionDays}
          onChange={(event) => setRetentionDays(Math.max(0, Number(event.target.value) || 0))}
          className="w-20 rounded-sm border border-border-glass bg-surface px-2 py-1 text-sm"
        />
        <span className="text-sm text-text-secondary">天</span>
        <Button variant="ghost" size="sm" onClick={() => purgeBatch(true)} disabled={loading}>
          清理过期回收项
        </Button>
        <Button
          variant="danger"
          size="sm"
          onClick={() => purgeBatch(false)}
          disabled={loading || selectedTrash.length === 0}
        >
          永久删除选中回收项 ({selectedTrash.length})
        </Button>
        <span className="text-xs text-text-tertiary">不会自动执行</span>
      </div>

      {!backendRunning && !controlAvailable && (
        <div className="rounded-sm border border-warning/40 bg-warning/10 p-4 text-warning">
          平台后端和控制服务均未运行, 当前不能执行数据审计和维护
        </div>
      )}

      {scanned && (
        <div className="flex flex-wrap gap-2">
          <Badge variant={items.length > 0 ? 'warning' : 'success'}>项目 {items.length}</Badge>
          <Badge variant="default">可安全清理 {safeItems.length}</Badge>
          <Badge variant="default">占用 {formatBytes(totalSize)}</Badge>
        </div>
      )}

      <div className="max-h-[32rem] overflow-auto rounded-sm border border-border-glass">
        {items.length === 0 ? (
          <div className="flex flex-col items-center justify-center gap-2 py-14 text-text-secondary">
            <ShieldAlert className="h-8 w-8" />
            <p>{scanned ? '没有发现需要维护的数据' : '点击扫描检查数据状态'}</p>
          </div>
        ) : (
          <table className="w-full min-w-[920px] text-sm">
            <thead className="sticky top-0 bg-surface/95 text-left text-text-tertiary backdrop-blur">
              <tr>
                <th className="px-3 py-3">选择</th>
                <th className="px-3 py-3">类别</th>
                <th className="px-3 py-3">平台 / 会话</th>
                <th className="px-3 py-3">原因</th>
                <th className="px-3 py-3">大小</th>
                <th className="px-3 py-3">操作</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.item_id} className="border-t border-border-glass">
                  <td className="px-3 py-3">
                    <input
                      type="checkbox"
                      checked={selected.has(item.item_id)}
                      disabled={!item.auto_safe && item.category !== 'trash_entry'}
                      onChange={() => toggle(item.item_id)}
                    />
                  </td>
                  <td className="px-3 py-3">
                    <Badge variant={item.auto_safe ? 'warning' : 'default'}>
                      {CATEGORY_LABELS[item.category] || item.category}
                    </Badge>
                  </td>
                  <td className="px-3 py-3 font-mono text-text-primary">
                    <div>{item.platform_id || '-'}</div>
                    <div className="max-w-[260px] truncate text-text-secondary" title={item.session_id}>
                      {item.session_id || '-'}
                    </div>
                  </td>
                  <td className="px-3 py-3">
                    <div className="text-text-primary">{item.reason}</div>
                    <div className="max-w-[360px] truncate text-xs text-text-tertiary" title={item.path}>
                      {item.path}
                    </div>
                  </td>
                  <td className="px-3 py-3 text-text-secondary">{formatBytes(item.size_bytes)}</td>
                  <td className="px-3 py-3">
                    {item.category === 'trash_entry' && item.details.archive_id && (
                      <div className="flex gap-2">
                        {Boolean(item.details.manifest?.archive_version) && (
                          <Button variant="ghost" size="sm" onClick={() => restore(item)} disabled={loading}>
                            <RotateCcw className="h-4 w-4 mr-1" />
                            恢复
                          </Button>
                        )}
                        <Button variant="ghost" size="sm" onClick={() => purge(item)} disabled={loading}>
                          <Trash2 className="h-4 w-4 mr-1 text-error" />
                          永久删除
                        </Button>
                      </div>
                    )}
                    {!item.auto_safe && item.category !== 'trash_entry' && (
                      <span className="text-text-tertiary">需人工检查</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
