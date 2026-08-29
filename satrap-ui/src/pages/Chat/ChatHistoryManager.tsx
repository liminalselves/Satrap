import { useCallback, useEffect, useMemo, useState } from 'react';
import { ArchiveRestore, RefreshCw, Trash2 } from 'lucide-react';

import { chatApi } from '@/api/chat';
import type {
  ChatHistoryArchive,
  ChatHistoryDeleteRequest,
  ChatHistoryQuery,
  ChatHistoryResult,
  ProjectItem,
} from '@/api/chat';
import { controlApi } from '@/api/control';
import { Badge } from '@/components/ui/Badge';
import { Button } from '@/components/ui/Button';
import { Modal } from '@/components/ui/Modal';

interface ChatHistoryManagerProps {
  open: boolean;
  onClose: () => void;
  projects: ProjectItem[];
  models: string[];
  activeConversationId?: string;
  preferCold?: boolean;
  onOpenConversation: (conversationId: string) => void;
  onChanged: () => void | Promise<void>;
}

const EMPTY_RESULT: ChatHistoryResult = {
  items: [], total: 0, page: 1, page_size: 50, storage_size_bytes: 0, mode: 'hot',
};

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(1)} GB`;
}

export function ChatHistoryManager({
  open,
  onClose,
  projects,
  models,
  activeConversationId,
  preferCold = false,
  onOpenConversation,
  onChanged,
}: ChatHistoryManagerProps) {
  const [tab, setTab] = useState<'history' | 'trash'>('history');
  const [result, setResult] = useState<ChatHistoryResult>(EMPTY_RESULT);
  const [trash, setTrash] = useState<ChatHistoryArchive[]>([]);
  const [trashSize, setTrashSize] = useState(0);
  const [selected, setSelected] = useState<Set<string>>(() => new Set());
  const [search, setSearch] = useState('');
  const [projectId, setProjectId] = useState('');
  const [model, setModel] = useState('');
  const [turnCount, setTurnCount] = useState<'all' | 'empty' | 'single'>('all');
  const [olderThanDays, setOlderThanDays] = useState('');
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);
  const [working, setWorking] = useState(false);
  const [error, setError] = useState('');

  const query = useMemo<ChatHistoryQuery>(() => ({
    search: search.trim() || undefined,
    project_id: projectId || undefined,
    model: model || undefined,
    turn_count: turnCount,
    older_than_days: olderThanDays.trim() ? Number(olderThanDays) : undefined,
    page,
    page_size: 50,
  }), [model, olderThanDays, page, projectId, search, turnCount]);

  const loadHistory = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      let next: ChatHistoryResult;
      if (preferCold) {
        next = await controlApi.queryChatHistory(query);
      } else {
        try {
          next = await chatApi.queryHistory(query);
        } catch {
          next = await controlApi.queryChatHistory(query);
        }
      }
      setResult(next);
      const available = new Set(next.items.map((item) => item.conversation_id));
      setSelected((current) => new Set([...current].filter((id) => available.has(id))));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '历史读取失败');
    } finally {
      setLoading(false);
    }
  }, [preferCold, query]);

  const loadTrash = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const response = result.mode === 'cold' || preferCold
        ? await controlApi.listChatHistoryTrash()
        : await chatApi.listHistoryTrash();
      setTrash(response.items);
      setTrashSize(response.storage_size_bytes);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '回收站读取失败');
    } finally {
      setLoading(false);
    }
  }, [preferCold, result.mode]);

  useEffect(() => {
    if (!open || tab !== 'history') return;
    void loadHistory();
  }, [loadHistory, open, tab]);

  useEffect(() => {
    if (!open || tab !== 'trash') return;
    void loadTrash();
  }, [loadTrash, open, tab]);

  useEffect(() => {
    setPage(1);
  }, [search, projectId, model, turnCount, olderThanDays]);

  const deleteHistory = useCallback(async (
    mode: ChatHistoryDeleteRequest['mode'],
    explicitIds?: string[],
  ) => {
    const targetIds = explicitIds ?? [...selected];
    const selectedItems = result.items.filter((item) => targetIds.includes(item.conversation_id));
    let estimatedCount = mode === 'selected' ? targetIds.length : result.total;
    if (mode === 'selected' && targetIds.length === 0) return;
    if (mode === 'empty' || mode === 'single') {
      try {
        const previewQuery: ChatHistoryQuery = { turn_count: mode, page: 1, page_size: 1 };
        const preview = result.mode === 'cold'
          ? await controlApi.queryChatHistory(previewQuery)
          : await chatApi.queryHistory(previewQuery);
        estimatedCount = preview.total;
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : '清理预览失败');
        return;
      }
    }
    if (estimatedCount === 0) {
      setError('没有符合条件的会话');
      return;
    }
    if (!window.confirm(`确定将 ${estimatedCount} 个匹配会话移入回收站吗？`)) return;
    const force = mode === 'selected'
      && selectedItems.some((item) => item.generating)
      && window.confirm('所选会话中有正在生成的任务, 是否强制停止并继续删除？');
    setWorking(true);
    setError('');
    try {
      const payload: ChatHistoryDeleteRequest = {
        mode,
        conversation_ids: mode === 'selected' ? targetIds : undefined,
        filters: mode === 'filtered' ? { ...query, page: undefined, page_size: undefined } : undefined,
        force,
      };
      const response = result.mode === 'cold'
        ? await controlApi.deleteChatHistory(payload)
        : await chatApi.deleteHistory(payload);
      const failed = response.results.filter((item) => !item.ok);
      if (failed.length > 0) {
        setError(`${response.deleted_count} 个会话已回收, ${failed.length} 个失败: ${failed[0].error || '未知错误'}`);
      }
      setSelected(new Set());
      await loadHistory();
      await onChanged();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '删除失败');
    } finally {
      setWorking(false);
    }
  }, [loadHistory, onChanged, query, result.items, result.mode, result.total, selected]);

  const restoreArchive = useCallback(async (archiveId: string) => {
    setWorking(true);
    setError('');
    try {
      if (result.mode === 'cold' || preferCold) {
        await controlApi.restoreChatHistory(archiveId);
      } else {
        await chatApi.restoreHistory(archiveId);
      }
      await loadTrash();
      await onChanged();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '恢复失败');
    } finally {
      setWorking(false);
    }
  }, [loadTrash, onChanged, preferCold, result.mode]);

  const purgeArchive = useCallback(async (archiveId: string) => {
    if (!window.confirm('永久删除后无法恢复, 是否继续？')) return;
    setWorking(true);
    setError('');
    try {
      if (result.mode === 'cold' || preferCold) {
        await controlApi.purgeChatHistory(archiveId);
      } else {
        await chatApi.purgeHistory(archiveId);
      }
      await loadTrash();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '永久删除失败');
    } finally {
      setWorking(false);
    }
  }, [loadTrash, preferCold, result.mode]);

  const allSelected = result.items.length > 0
    && result.items.every((item) => selected.has(item.conversation_id));
  const pageCount = Math.max(1, Math.ceil(result.total / result.page_size));
  const hasCustomFilter = Boolean(
    search.trim()
    || projectId
    || model
    || turnCount !== 'all'
    || olderThanDays.trim(),
  );

  return (
    <Modal open={open} onClose={onClose} title="会话历史管理" size="2xl">
      <div className="space-y-4">
        <div className="flex items-center justify-between gap-3">
          <div className="flex gap-2">
            <Button variant={tab === 'history' ? 'primary' : 'ghost'} size="sm" onClick={() => setTab('history')}>历史</Button>
            <Button variant={tab === 'trash' ? 'primary' : 'ghost'} size="sm" onClick={() => setTab('trash')}>回收站</Button>
          </div>
          <div className="flex items-center gap-2 text-xs text-text-tertiary">
            <Badge variant={result.mode === 'hot' ? 'success' : 'warning'}>{result.mode === 'hot' ? '热管理' : '冷管理'}</Badge>
            <span>{tab === 'history' ? `${result.total} 个会话 · ${formatBytes(result.storage_size_bytes)}` : `${trash.length} 个回收项 · ${formatBytes(trashSize)}`}</span>
            <Button variant="ghost" size="sm" onClick={() => void (tab === 'history' ? loadHistory() : loadTrash())} disabled={loading || working}>
              <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
            </Button>
          </div>
        </div>

        {error && <div className="rounded-lg border border-error/30 bg-error/10 px-3 py-2 text-sm text-error">{error}</div>}

        {tab === 'history' ? (
          <>
            <div className="grid grid-cols-1 gap-2 md:grid-cols-5">
              <input className="glass-input md:col-span-2" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索标题或会话 ID" />
              <select className="glass-input" value={projectId} onChange={(event) => setProjectId(event.target.value)}>
                <option value="">全部项目</option>
                <option value="__none__">无项目</option>
                {projects.map((project) => <option key={project.project_id} value={project.project_id}>{project.name}</option>)}
              </select>
              <select className="glass-input" value={model} onChange={(event) => setModel(event.target.value)}>
                <option value="">全部模型</option>
                {models.map((name) => <option key={name} value={name}>{name}</option>)}
              </select>
              <select className="glass-input" value={turnCount} onChange={(event) => setTurnCount(event.target.value as typeof turnCount)}>
                <option value="all">全部轮数</option>
                <option value="empty">0 轮</option>
                <option value="single">仅 1 轮</option>
              </select>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <label className="flex items-center gap-2 text-sm text-text-secondary">
                超过
                <input className="glass-input w-20" type="number" min="0" value={olderThanDays} onChange={(event) => setOlderThanDays(event.target.value)} placeholder="天数" />
                天未使用
              </label>
              <div className="ml-auto flex flex-wrap gap-2">
                <Button variant="danger" size="sm" disabled={working} onClick={() => void deleteHistory('empty')}>清理 0 轮</Button>
                <Button variant="danger" size="sm" disabled={working} onClick={() => void deleteHistory('single')}>清理 1 轮</Button>
                <Button variant="danger" size="sm" disabled={working || result.total === 0 || !hasCustomFilter} onClick={() => void deleteHistory('filtered')}>清理筛选结果</Button>
                <Button variant="danger" size="sm" disabled={working || selected.size === 0} onClick={() => void deleteHistory('selected')}>删除所选 ({selected.size})</Button>
              </div>
            </div>
            <div className="h-[28rem] overflow-auto rounded-xl border border-glass-border custom-scrollbar">
              <table className="w-full min-w-[850px] text-sm">
                <thead className="sticky top-0 z-10 bg-[var(--glass-bg)] text-left text-text-secondary">
                  <tr>
                    <th className="p-3"><input type="checkbox" checked={allSelected} onChange={(event) => setSelected(event.target.checked ? new Set(result.items.map((item) => item.conversation_id)) : new Set())} /></th>
                    <th className="p-3">标题</th><th className="p-3">模型</th><th className="p-3">轮数</th><th className="p-3">状态</th><th className="p-3">最近使用</th><th className="p-3">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {result.items.map((item) => (
                    <tr key={item.conversation_id} className="border-t border-glass-border hover:bg-glass-active">
                      <td className="p-3"><input type="checkbox" checked={selected.has(item.conversation_id)} onChange={(event) => setSelected((current) => { const next = new Set(current); if (event.target.checked) next.add(item.conversation_id); else next.delete(item.conversation_id); return next; })} /></td>
                      <td className="max-w-72 p-3"><div className="truncate font-medium text-text-primary" title={item.title}>{item.title}</div><div className="truncate font-mono text-xs text-text-tertiary" title={item.conversation_id}>{item.conversation_id}</div></td>
                      <td className="p-3">{item.model}</td><td className="p-3">{item.turn_count}</td>
                      <td className="p-3"><div className="flex gap-1">{item.conversation_id === activeConversationId && <Badge variant="info">当前</Badge>}{item.generating ? <Badge variant="warning">生成中</Badge> : item.active ? <Badge variant="success">已加载</Badge> : <Badge variant="default">已持久化</Badge>}</div></td>
                      <td className="whitespace-nowrap p-3">{new Date(item.last_at * 1000).toLocaleString()}</td>
                      <td className="p-3"><div className="flex gap-1">{result.mode === 'hot' && <Button variant="ghost" size="sm" onClick={() => onOpenConversation(item.conversation_id)}>打开</Button>}<Button variant="ghost" size="sm" className="text-error" onClick={() => void deleteHistory('selected', [item.conversation_id])} title="移入回收站"><Trash2 className="h-4 w-4" /></Button></div></td>
                    </tr>
                  ))}
                  {!loading && result.items.length === 0 && <tr><td colSpan={7} className="p-10 text-center text-text-tertiary">暂无匹配历史</td></tr>}
                </tbody>
              </table>
            </div>
            <div className="flex items-center justify-end gap-2 text-sm text-text-secondary">
              <Button variant="ghost" size="sm" disabled={page <= 1} onClick={() => setPage((value) => value - 1)}>上一页</Button>
              <span>{page} / {pageCount}</span>
              <Button variant="ghost" size="sm" disabled={page >= pageCount} onClick={() => setPage((value) => value + 1)}>下一页</Button>
            </div>
          </>
        ) : (
          <div className="h-[32rem] space-y-2 overflow-auto custom-scrollbar pr-1">
            {trash.map((item) => (
              <div key={item.archive_id} className="glass-card flex items-center justify-between gap-4 rounded-xl p-3">
                <div className="min-w-0"><div className="truncate font-medium text-text-primary">{item.title}</div><div className="mt-1 text-xs text-text-tertiary">{item.turn_count} 轮 · {item.model} · {formatBytes(item.size_bytes)} · {new Date(item.deleted_at * 1000).toLocaleString()}</div></div>
                <div className="flex shrink-0 gap-1"><Button variant="ghost" size="sm" disabled={working} onClick={() => void restoreArchive(item.archive_id)} title="恢复"><ArchiveRestore className="h-4 w-4" /></Button><Button variant="ghost" size="sm" disabled={working} className="text-error" onClick={() => void purgeArchive(item.archive_id)} title="永久删除"><Trash2 className="h-4 w-4" /></Button></div>
              </div>
            ))}
            {!loading && trash.length === 0 && <div className="py-16 text-center text-text-tertiary">回收站为空</div>}
          </div>
        )}
      </div>
    </Modal>
  );
}
