import { useEffect, useRef, useState } from 'react';
import axios from 'axios';
import { useSearchParams } from 'react-router-dom';
import { controlApi } from '@/api/control';
import type { ConversationPlatform, ConversationUser, ConversationUserCatalog } from '@/api/types';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { Badge } from '@/components/ui/Badge';
import { Modal } from '@/components/ui/Modal';
import { confirmDiscard } from '@/hooks/useDirtyGuard';

const errorText = (error: unknown) => axios.isAxiosError<{ error?: string }>(error) ? error.response?.data?.error || error.message : error instanceof Error ? error.message : String(error);

export function UserView({ platform, platformType, platforms, query, offset, refresh, onDirty, onOffset, onRefresh, onOpen }: {
  platform: string; platformType: string; platforms: ConversationPlatform[]; query: string; offset: number; refresh: number;
  onDirty: (dirty: boolean) => void; onOffset: (offset: number) => void; onRefresh: () => void; onOpen: (platform: string, conversation: string) => void;
}) {
  const [search, setSearch] = useSearchParams();
  const selected = search.get('user') || '';
  const userPlatform = search.get('user_platform') || platform;
  const [catalog, setCatalog] = useState<ConversationUserCatalog>({ items: [], total: 0, new_revision: '' });
  const [detail, setDetail] = useState<ConversationUser | null>(null);
  const [loading, setLoading] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState('');
  const [navigation, setNavigation] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [editor, setEditor] = useState<'create' | 'update' | null>(null);
  const [form, setForm] = useState({ platform: '', user: '', nickname: '' });
  const [initial, setInitial] = useState('');
  const [session, setSession] = useState('');
  const mounted = useRef(true);
  const currentSearch = useRef(search.toString());
  currentSearch.current = search.toString();
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const dirty = editor !== null && JSON.stringify(form) !== initial;
  useEffect(() => { onDirty(dirty || busy); }, [dirty, busy, onDirty]);
  useEffect(() => () => onDirty(false), [onDirty]);
  useEffect(() => {
    if (navigation === null || editor || busy) return;
    setSearch(new URLSearchParams(navigation)); setNavigation(null); onRefresh();
  }, [navigation, editor, busy, setSearch, onRefresh]);
  useEffect(() => {
    let disposed = false;
    setLoading(true); setError('');
    controlApi.listConversationUsers(platform, query, offset, platformType).then((data) => { if (!disposed) setCatalog(data); })
      .catch((error) => { if (!disposed) { setError(errorText(error)); setCatalog({ items: [], total: 0, new_revision: '' }); } })
      .finally(() => { if (!disposed) setLoading(false); });
    return () => { disposed = true; };
  }, [platform, platformType, query, offset, refresh]);
  useEffect(() => {
    let disposed = false;
    setDetail(null); setSession(''); setDetailLoading(false);
    if (!selected || !userPlatform) return;
    setDetailLoading(true);
    controlApi.getConversationUser(userPlatform, selected).then((data) => { if (!disposed) setDetail(data.user); })
      .catch((error) => { if (!disposed) setError(errorText(error)); })
      .finally(() => { if (!disposed) setDetailLoading(false); });
    return () => { disposed = true; };
  }, [selected, userPlatform, refresh]);
  const openEditor = (mode: 'create' | 'update') => {
    const values = mode === 'update' && detail ? { platform: detail.platform_id, user: detail.user_id, nickname: detail.user_nickname || '' }
      : { platform: platform || platforms.find((item) => !platformType || item.type === platformType)?.id || '', user: '', nickname: '' };
    setForm(values); setInitial(JSON.stringify(values)); setEditor(mode); setError('');
  };
  const close = () => { if (!busy && (!dirty || confirmDiscard())) setEditor(null); };
  const mutate = async (targetPlatform: string, identity: string, data: Record<string, unknown>) => {
    const startingSearch = currentSearch.current;
    setBusy(true); setError('');
    try {
      const response = await controlApi.mutateConversationUser(targetPlatform, identity, data);
      if (!mounted.current) return;
      if (startingSearch !== currentSearch.current) { onRefresh(); return; }
      setDetail(response.user); setEditor(null); setSession('');
      const next = new URLSearchParams(search);
      if (response.user) { next.set('user', identity); next.set('user_platform', targetPlatform); }
      else { next.delete('user'); next.delete('user_platform'); }
      setNavigation(next.toString());
    } catch (error) { if (mounted.current) setError(errorText(error)); }
    finally { if (mounted.current) setBusy(false); }
  };
  return <div className="space-y-4">
    <div className="flex flex-wrap items-center justify-between gap-2"><p className="text-sm">共 {catalog.total} 个用户。平台实例与用户 ID 共同标识用户。</p><Button disabled={busy || loading || !catalog.new_revision || !platforms.length} onClick={() => openEditor('create')}>添加用户资料</Button></div>
    {error && !editor && <p role="alert" className="text-error">{error}</p>}
    {catalog.warnings?.map((warning) => <p key={warning} role="alert" className="text-warning">{warning}</p>)}
    <div className="grid items-start gap-4 xl:grid-cols-[22rem_minmax(0,1fr)]">
      <Card className="min-w-0 space-y-2">
        {loading && <p role="status">正在读取用户…</p>}
        {!loading && !catalog.items.length && <p className="text-sm text-text-secondary">暂无匹配的用户</p>}
        {catalog.items.map((user) => <button key={JSON.stringify([user.platform_id, user.user_id])} className={`w-full space-y-1 rounded-lg p-3 text-left ${selected === user.user_id && userPlatform === user.platform_id ? 'bg-accent/10' : 'bg-glass hover:bg-glass-hover'}`} onClick={() => {
          const next = new URLSearchParams(search); next.set('user', user.user_id); next.set('user_platform', user.platform_id); next.delete('conversation'); next.delete('record_platform'); setSearch(next);
        }}><p className="break-all font-medium">{user.user_nickname || user.user_id}</p><p className="break-all text-xs text-text-tertiary">{user.platform_label} · {user.user_id}</p><p className="text-xs">{user.conversation_count} 个关联对话 {user.has_profile ? '' : '· 未保存用户资料'}</p></button>)}
        <div className="flex justify-between gap-2"><Button size="sm" disabled={!offset || loading} onClick={() => onOffset(Math.max(0, offset - 40))}>上一页用户</Button><Button size="sm" disabled={offset + 40 >= catalog.total || loading} onClick={() => onOffset(offset + 40)}>下一页用户</Button></div>
      </Card>
      <Card className="min-w-0 space-y-4">
        {detailLoading ? <p role="status">正在读取用户详情…</p> : !detail ? <p className="text-text-secondary">{selected ? '此用户已没有资料或路由关联，请刷新列表' : '从列表选择一个用户'}</p> : <>
          <h2 className="break-all text-lg font-semibold">{detail.user_nickname || detail.user_id}</h2>
          <p className="break-all text-sm text-text-secondary">平台实例：{detail.platform_id} · 用户 ID：{detail.user_id}</p>
          <p className="text-xs text-text-tertiary">{detail.has_profile ? `保存的平台来源：${detail.user_platform || '未知'}` : '仅存在路由记录，尚未保存用户资料'}</p>
          {detail.warning && <p role="alert" className="text-warning">{detail.warning}</p>}
          <Button disabled={busy} onClick={() => openEditor('update')}>{detail.has_profile ? '编辑用户资料' : '添加此用户资料'}</Button>
          <h3 className="font-medium">关联对话</h3>
          <p className="text-xs text-text-secondary">这里只显示有明确用户归属或手动关联的对话。群共享对话仍按群组织。</p>
          {!detail.conversations.length && <p className="text-sm">此用户暂无关联对话</p>}
          {detail.conversations.map((item) => <div key={item.conversation_id} className="space-y-2 rounded-lg bg-glass p-3">
            <p className="break-all text-sm">{item.title}</p><p className="break-all text-xs text-text-tertiary">{item.conversation_id}</p>
            <div className="flex flex-wrap items-center gap-2">{item.routed && <Badge variant="info">路由关联</Badge>}{item.manual && <Badge variant="warning">列表关联</Badge>}{!item.exists && <Badge variant="warning">对话不存在</Badge>}
              <Button size="sm" disabled={busy || !item.exists} onClick={() => onOpen(detail.platform_id, item.conversation_id)}>查看对话</Button>
            </div>
          </div>)}
          {detail.has_profile && <details className="space-y-3 rounded-lg bg-glass p-3"><summary className="cursor-pointer text-sm">高级资料操作</summary>
            <p className="mt-3 text-xs text-text-secondary">列表关联不会改写平台路由；本地按用户选择会话的调用可能使用此列表。移除列表关联后，路由关联仍然保留。</p>
            <label className="block text-sm">当前平台的对话 ID<input aria-label="关联对话 ID" className="glass-input mt-1 w-full" value={session} disabled={busy} onChange={(event) => setSession(event.target.value)} /></label>
            <Button size="sm" disabled={busy || !session.trim()} onClick={() => void mutate(detail.platform_id, detail.user_id, { action: 'associate', session_id: session.trim(), expected_revision: detail.revision })}>添加关联</Button>
            {detail.user_session.map((identity) => <div key={identity} className="flex flex-wrap items-center justify-between gap-2"><span className="break-all text-xs">{identity}</span><Button size="sm" disabled={busy} onClick={() => { if (window.confirm('确定移除此列表关联吗？平台路由和对话内容会保留；本地按用户选择会话的调用可能受影响。')) void mutate(detail.platform_id, detail.user_id, { action: 'dissociate', session_id: identity, expected_revision: detail.revision }); }}>移除关联</Button></div>)}
            <p className="text-xs text-text-secondary">删除用户资料会删除昵称和列表关联，保留对话、消息和平台路由。后续消息可能重新创建资料。</p>
            <Button size="sm" variant="danger" disabled={busy} onClick={() => { if (window.confirm('确定删除此用户资料吗？昵称和列表关联将删除，对话、消息和平台路由会保留，后续消息可能重新创建资料。')) void mutate(detail.platform_id, detail.user_id, { action: 'delete', expected_revision: detail.revision }); }}>删除用户资料</Button>
          </details>}
        </>}
      </Card>
    </div>
    <Modal open={editor !== null} onClose={close} title={editor === 'create' || !detail?.has_profile ? '添加用户资料' : '编辑用户资料'}>
      <div className="space-y-4">
        <p className="text-xs text-text-secondary">昵称保存在本地，不修改平台账号资料。平台实例和用户 ID 确定资料归属。</p>
        {error && <p role="alert" className="text-error">{error}</p>}
        <label className="block text-sm">平台实例<select aria-label="资料平台实例" className="glass-input mt-1 w-full" value={form.platform} disabled={busy || editor === 'update'} onChange={(event) => setForm({ ...form, platform: event.target.value })}><option value="">请选择平台实例</option>{platforms.map((item) => <option key={item.id} value={item.id}>{item.label} · {item.type_label}</option>)}</select></label>
        <label className="block text-sm">用户 ID<input aria-label="资料用户 ID" className="glass-input mt-1 w-full" value={form.user} disabled={busy || editor === 'update'} onChange={(event) => setForm({ ...form, user: event.target.value })} /></label>
        <label className="block text-sm">本地昵称<input aria-label="本地昵称" className="glass-input mt-1 w-full" value={form.nickname} disabled={busy} onChange={(event) => setForm({ ...form, nickname: event.target.value })} /></label>
        <div className="flex justify-end gap-2"><Button disabled={busy} onClick={close}>取消</Button><Button variant="primary" disabled={busy || !form.platform || !form.user.trim() || (editor === 'update' && detail?.has_profile && !dirty)} onClick={() => void mutate(form.platform, form.user.trim(), { action: editor === 'create' || !detail?.has_profile ? 'create' : 'update', nickname: form.nickname, expected_revision: editor === 'update' ? detail?.revision : catalog.new_revision })}>{busy ? '保存中…' : '保存资料'}</Button></div>
      </div>
    </Modal>
  </div>;
}
