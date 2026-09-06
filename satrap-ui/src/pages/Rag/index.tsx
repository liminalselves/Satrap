import { useEffect, useState } from 'react';
import { RagManager } from '@/components/common/RagManager';
import { controlApi } from '@/api/control';
import type { RuntimeSession } from '@/api/types';
import type { ChatHistoryItem } from '@/api/chat';
import { Button } from '@/components/ui/Button';

export function Rag() {
  const [sessions, setSessions] = useState<RuntimeSession[]>([]);
  const [selected, setSelected] = useState('');
  const [error, setError] = useState('');
  const [chatSessions, setChatSessions] = useState<ChatHistoryItem[]>([]);
  const [chatSessionId, setChatSessionId] = useState('');
  const [search, setSearch] = useState('');
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  useEffect(() => {
    controlApi.listSessionInstances().then((result) => setSessions(result.sessions)).catch((reason) => setError(reason.message));
  }, []);
  const session = sessions.find((item) => `${item.platform_id}:${item.session_id}` === selected);
  const platformId = session?.platform_id || (selected.startsWith('platform:') ? selected.slice(9) : 'local');
  const sessionId = session?.session_id || (platformId === 'chat' ? chatSessionId || undefined : undefined);
  useEffect(() => {
    if (platformId !== 'chat') return;
    let cancelled = false;
    const timer = window.setTimeout(() => {
      controlApi.queryChatHistory({ search, page, page_size: 50 }).then((result) => {
        if (!cancelled) { setChatSessions(result.items); setTotal(result.total); setError(''); }
      }).catch((reason) => { if (!cancelled) setError(reason.message); });
    }, 250);
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [platformId, search, page]);
  return <div className="space-y-6">
    <div><h1 className="text-2xl font-semibold">知识库管理</h1><p className="mt-1 text-sm text-text-secondary">统一管理全局与会话知识库，供模型通过工具检索</p></div>
    {error && <p className="text-sm text-error">{error}</p>}
    <select aria-label="管理范围" className="glass-input w-full" value={selected} onChange={(event) => setSelected(event.target.value)}>
      <option value="">全局与 local 平台</option>
      {[...new Set(['chat', ...sessions.map((item) => item.platform_id)])].filter((item) => item !== 'local').map((item) => <option key={item} value={`platform:${item}`}>全局与 {item} 平台</option>)}
      {sessions.map((item) => <option key={`${item.platform_id}:${item.session_id}`} value={`${item.platform_id}:${item.session_id}`}>{item.platform_id} / {item.session_id}</option>)}
    </select>
    {platformId === 'chat' && !session && <div className="space-y-2">
      <input aria-label="搜索 Chat 会话" className="glass-input w-full" placeholder="搜索 Chat 会话" value={search} onChange={(event) => { setSearch(event.target.value); setPage(1); }} />
      <select aria-label="Chat 会话" className="glass-input w-full" value={chatSessionId} onChange={(event) => setChatSessionId(event.target.value)}>
        <option value="">全部 Chat 会话知识库</option>
        {chatSessionId && !chatSessions.some((item) => item.conversation_id === chatSessionId) && <option value={chatSessionId}>{chatSessionId}</option>}
        {chatSessions.map((item) => <option key={item.conversation_id} value={item.conversation_id}>{item.title || item.conversation_id}</option>)}
      </select>
      <div className="flex items-center gap-3 text-sm"><Button size="sm" disabled={page <= 1} onClick={() => setPage((current) => current - 1)}>上一页</Button><span>第 {page} 页 · {total} 个会话</span><Button size="sm" disabled={page * 50 >= total} onClick={() => setPage((current) => current + 1)}>下一页</Button></div>
    </div>}
    <RagManager key={`${platformId}:${sessionId || ''}`} context={{ platformId, sessionId, via: 'control' }} />
  </div>;
}
