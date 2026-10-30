import { useEffect, useRef, useState } from 'react';
import { Link2, MessageSquare, Pencil, Plus, RefreshCw, Trash2, Unlink, User, Users } from 'lucide-react';
import { useSearchParams } from 'react-router-dom';
import { cn } from '@/utils/cn';
import { confirmDiscard } from '@/hooks/useDirtyGuard';
import { useConfirm } from '@/hooks/useConfirm';
import { controlApi } from '@/api/control';
import type { ConversationPlatform, ConversationUser, ConversationUserCatalog } from '@/api/types';
import { Button, IconButton } from '@/components/kit/Button';
import { Badge } from '@/components/kit/Badge';
import { Modal } from '@/components/kit/Modal';
import { Field, Input } from '@/components/kit';
import { errorText } from '@/utils/errorText';
import { formatTime } from '@/utils/format';
import { InlineEmpty, KeyValue, Notice, Pager, SearchField, StatTile } from './common';
import { Select as KitSelect } from '@/components/kit/Select';

const PAGE = 40;

export function UserView({ platform, platformType, platforms, onType, onPlatform, onDirty, onOpen }: {
  platform: string; platformType: string; platforms: ConversationPlatform[];
  onType: (value: string) => void; onPlatform: (value: string) => void; onDirty: (dirty: boolean) => void;
  onOpen: (platform: string, conversation: string) => void;
}) {
  const [search, setSearch] = useSearchParams();
  const typeLabel = (user: Pick<ConversationUser, 'platform_id' | 'platform_type'>) => (
    platforms.find((item) => item.id === user.platform_id)?.type_label
      || platforms.find((item) => item.type === user.platform_type)?.type_label
      || user.platform_type
  );
  const selected = search.get('user') || '';
  const userPlatform = search.get('user_platform') || platform;
  const confirm = useConfirm(`${userPlatform}:${selected}`);
  const [catalog, setCatalog] = useState<ConversationUserCatalog>({ items: [], total: 0, new_revision: '' });
  const [detail, setDetail] = useState<ConversationUser | null>(null);
  const [query, setQuery] = useState('');
  const [submitted, setSubmitted] = useState('');
  const [page, setPage] = useState(0);
  const [refresh, setRefresh] = useState(0);
  const [loading, setLoading] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState('');
  const [navigation, setNavigation] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [editor, setEditor] = useState<'create' | 'update' | null>(null);
  const [form, setForm] = useState({ platform: '', user: '', nickname: '' });
  const [initial, setInitial] = useState('');
  const [formError, setFormError] = useState('');
  const [session, setSession] = useState('');
  const mounted = useRef(true);
  const currentSearch = useRef(search.toString());
  currentSearch.current = search.toString();
  const offset = page * PAGE;

  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => {
    const timer = window.setTimeout(() => { setSubmitted(query); setPage(0); }, 300);
    return () => window.clearTimeout(timer);
  }, [query]);
  useEffect(() => { setPage(0); }, [platform, platformType]);
  const dirty = editor !== null && JSON.stringify(form) !== initial;
  useEffect(() => { onDirty(dirty || busy); }, [dirty, busy, onDirty]);
  useEffect(() => () => onDirty(false), [onDirty]);
  useEffect(() => {
    if (navigation === null || editor || busy) return;
    setSearch(new URLSearchParams(navigation)); setNavigation(null); setRefresh((value) => value + 1);
  }, [navigation, editor, busy, setSearch]);
  useEffect(() => {
    let disposed = false;
    setLoading(true); setError('');
    controlApi.listConversationUsers(platform, submitted, offset, platformType).then((data) => { if (!disposed) setCatalog(data); })
      .catch((reason) => { if (!disposed) { setError(errorText(reason)); setCatalog({ items: [], total: 0, new_revision: '' }); } })
      .finally(() => { if (!disposed) setLoading(false); });
    return () => { disposed = true; };
  }, [platform, platformType, submitted, offset, refresh]);
  useEffect(() => {
    let disposed = false;
    setDetail(null); setSession(''); setDetailLoading(false);
    if (!selected || !userPlatform) return;
    setDetailLoading(true);
    controlApi.getConversationUser(userPlatform, selected).then((data) => { if (!disposed) setDetail(data.user); })
      .catch((reason) => { if (!disposed) setError(errorText(reason)); })
      .finally(() => { if (!disposed) setDetailLoading(false); });
    return () => { disposed = true; };
  }, [selected, userPlatform, refresh]);

  const openEditor = (mode: 'create' | 'update') => {
    const values = mode === 'update' && detail ? { platform: detail.platform_id, user: detail.user_id, nickname: detail.user_nickname || '' }
      : { platform: platform || platforms.find((item) => !platformType || item.type === platformType)?.id || '', user: '', nickname: '' };
    setForm(values); setInitial(JSON.stringify(values)); setEditor(mode); setFormError(''); setError('');
  };
  const close = () => { if (!busy && (!dirty || confirmDiscard())) { setEditor(null); setFormError(''); setError(''); } };
  const mutate = async (targetPlatform: string, identity: string, data: Record<string, unknown>) => {
    const startingSearch = currentSearch.current;
    setBusy(true); setError(''); setFormError('');
    try {
      const response = await controlApi.mutateConversationUser(targetPlatform, identity, data);
      if (!mounted.current) return;
      if (startingSearch !== currentSearch.current) { setRefresh((value) => value + 1); return; }
      setDetail(response.user); setEditor(null); setSession('');
      const next = new URLSearchParams(search);
      if (response.user) { next.set('user', identity); next.set('user_platform', targetPlatform); }
      else { next.delete('user'); next.delete('user_platform'); }
      setNavigation(next.toString());
    } catch (reason) {
      if (mounted.current) { setError(errorText(reason)); setFormError(errorText(reason)); }
    }
    finally { if (mounted.current) setBusy(false); }
  };

  const resetFilters = () => {
    setQuery(''); setSubmitted(''); setPage(0); setError('');
    setSearch({ view: 'users' });
  };

  return (
    <>
      <div className="surface tint-green flex min-h-[420px] min-w-0 flex-col overflow-hidden !rounded-[16px]">
        <div className="flex-none space-y-2 border-b border-line p-3">
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0">
              <h2 className="flex items-center gap-2 text-[14px] font-semibold text-fg"><Users className="h-4 w-4 text-tint" />用户资料</h2>
              <p className="mt-0.5 text-[11px] text-fg-4">共 {catalog.total} 个用户 · 平台实例与用户 ID 共同标识</p>
            </div>
            <Button size="sm" variant="tinted" tint="green" icon={<Plus className="h-3.5 w-3.5" />}
              disabled={busy || loading || !catalog.new_revision || !platforms.length}
              onClick={() => openEditor('create')}>添加用户资料</Button>
          </div>
          <SearchField value={query} onChange={setQuery} placeholder="搜索用户 ID、昵称或平台实例" ariaLabel="搜索用户" />
          <div className="grid grid-cols-2 gap-1.5">
            <label className="min-w-0 text-[10.5px] text-fg-4">平台类型
              <KitSelect aria-label="平台类型" className="field mt-1 !h-8 w-full text-[11px]" value={platformType}
                onChange={(value) => onType(value)}>
                <option value="">全部类型</option>
                {[...new Map(platforms.map((item) => [item.type, item.type_label])).entries()].map(([value, label]) => (
                  <option key={value} value={value}>{label}</option>
                ))}
              </KitSelect>
            </label>
            <label className="min-w-0 text-[10.5px] text-fg-4">平台实例
              <KitSelect aria-label="记录平台" className="field mt-1 !h-8 w-full text-[11px]" value={platform}
                onChange={(value) => onPlatform(value)}>
                <option value="">全部实例</option>
                {platform && !platforms.some((item) => item.id === platform) && <option value={platform}>{platform}</option>}
                {platforms.filter((item) => !platformType || item.type === platformType).map((item) => (
                  <option key={item.id} value={item.id}>{item.label} · {item.type_label}</option>
                ))}
              </KitSelect>
            </label>
          </div>
          <div className="flex items-center justify-between gap-2">
            <span className="num text-[10px] text-fg-4">按用户 ID 与平台实例区分；列表关联不影响平台路由</span>
            <div className="flex items-center gap-1">
              <Button size="sm" variant="ghost" onClick={resetFilters}>重置筛选</Button>
              <IconButton label="刷新用户列表" size="sm" disabled={loading} onClick={() => setRefresh((value) => value + 1)}>
                <RefreshCw className={cn('h-3.5 w-3.5', loading && 'animate-spin-slow')} />
              </IconButton>
            </div>
          </div>
        </div>
        <div className="scroll min-h-0 flex-1 space-y-1 p-2">
          {error && !editor && <div className="p-1"><Notice tone="error">{error}</Notice></div>}
          {catalog.warnings?.map((warning) => <div key={warning} className="p-1"><Notice tone="warning">{warning}</Notice></div>)}
          {loading && <div className="space-y-2 p-1"><div className="skeleton h-16" /><div className="skeleton h-16" /><div className="skeleton h-16" /></div>}
          {!loading && !catalog.items.length && (
            <InlineEmpty icon={<User className="h-6 w-6" />} title="暂无匹配的用户"
              description="调整搜索或平台筛选，或手动添加一份用户资料。"
              action={<Button size="sm" variant="tinted" tint="green" onClick={() => openEditor('create')}>添加用户资料</Button>} />
          )}
          {!loading && catalog.items.map((user) => {
            const active = selected === user.user_id && userPlatform === (user.platform_id || platform);
            return (
              <button key={JSON.stringify([user.platform_id, user.user_id])} type="button"
                aria-current={active ? 'true' : undefined}
                onClick={() => {
                  const next = new URLSearchParams(search);
                  next.set('view', 'users');
                  next.set('user', user.user_id); next.set('user_platform', user.platform_id);
                  next.delete('conversation'); next.delete('record_platform');
                  setSearch(next);
                }}
                className={cn('group relative flex w-full items-start gap-2.5 rounded-xl px-2.5 py-2.5 text-left transition-colors',
                  active ? 'bg-[rgb(var(--rgb-green)/0.14)] text-fg' : 'text-fg-2 hover:bg-glass-hover hover:text-fg')}>
                {active && <span className="absolute left-0 top-1/2 h-5 w-[3px] -translate-y-1/2 rounded-r-full bg-tint" />}
                <span className="grid h-9 w-9 flex-none place-items-center rounded-xl border border-[rgb(var(--rgb-green)/0.24)] bg-[rgb(var(--rgb-green)/0.16)] text-[13px] font-semibold text-green">
                  {(user.user_nickname || user.user_id).slice(0, 1).toUpperCase()}
                </span>
                <span className="min-w-0 flex-1">
                  <span className="flex items-center gap-1.5">
                    <span className="truncate text-[13px] font-medium text-fg">{user.user_nickname || user.user_id}</span>
                    {!user.has_profile && <Badge tone="orange">未保存资料</Badge>}
                  </span>
                  <span className="mt-0.5 block truncate text-[11px] text-fg-3">
                    {typeLabel(user)} <span className="font-mono">{user.user_id}</span>
                  </span>
                  <span className="mt-0.5 flex items-center gap-2 whitespace-nowrap text-[10.5px] text-fg-4">
                    <span>{user.platform_label}</span>
                    <span>{user.conversation_count} 个关联对话</span>
                    {user.warning && <span className="text-orange">有提示</span>}
                  </span>
                </span>
              </button>
            );
          })}
        </div>
        <div className="flex-none border-t border-line px-3 py-2">
          <Pager page={page} pageCount={Math.max(1, Math.ceil(catalog.total / PAGE))} total={catalog.total} pageSize={PAGE}
            label="个用户" onPage={setPage} disabled={loading} />
        </div>
      </div>

      <div className="min-w-0">
        {!selected ? (
          <div className="surface tint-green flex h-full min-h-[320px] flex-col items-center justify-center !rounded-[16px] p-6 text-center">
            <div className="grid h-12 w-12 place-items-center rounded-2xl bg-[rgb(var(--rgb-green)/0.14)] text-green"><Users className="h-6 w-6" /></div>
            <h3 className="mt-3 text-[15px] font-semibold text-fg">选择一个用户</h3>
            <p className="mt-1 max-w-sm text-[12.5px] leading-5 text-fg-3">用户资料只保存本地昵称与列表关联，不修改平台账号；群共享对话仍按群组织。</p>
          </div>
        ) : (
          <div className="surface tint-green flex min-h-[420px] min-w-0 flex-col overflow-hidden !rounded-[16px]">
            <div className="scroll min-h-0 flex-1 p-4">
              {detailLoading ? <p role="status" className="text-[13px] text-fg-3">正在读取用户详情…</p>
                : !detail ? <p className="text-[13px] text-fg-3">{selected ? '此用户已没有资料或路由关联，请刷新列表' : '从列表选择一个用户'}</p>
                  : <>
                    <div className="flex flex-wrap items-start gap-3">
                      <div className="grid h-14 w-14 flex-none place-items-center rounded-[17px] border border-[rgb(var(--rgb-green)/0.3)] bg-[linear-gradient(140deg,rgb(var(--rgb-green)/0.35),rgb(var(--rgb-green)/0.08))] text-[19px] font-semibold text-green shadow-[inset_0_1px_0_var(--glass-highlight)]">
                        {(detail.user_nickname || detail.user_id).slice(0, 1).toUpperCase()}
                      </div>
                      <div className="min-w-0 flex-1">
                        <h2 className="break-all text-[18px] font-semibold text-fg">{detail.user_nickname || detail.user_id}</h2>
                        <div className="mt-1 flex flex-wrap items-center gap-1.5">
                          <Badge tone={detail.has_profile ? 'green' : 'orange'}>{detail.has_profile ? '已保存资料' : '仅路由记录'}</Badge>
                          <span className="break-all text-[11px] text-fg-3">{typeLabel(detail)} <span className="font-mono">{detail.user_id}</span> · {detail.platform_label}</span>
                        </div>
                        <p className="mt-1 text-[11.5px] leading-5 text-fg-4">{detail.has_profile
                          ? `昵称保存在本地，不修改平台账号资料。保存的平台来源：${detail.user_platform || '未知'}`
                          : '仅存在路由记录，尚未保存用户资料'}</p>
                      </div>
                      <Button size="sm" variant="tinted" tint="green" icon={<Pencil className="h-3.5 w-3.5" />} disabled={busy}
                        onClick={() => openEditor('update')}>{detail.has_profile ? '编辑用户资料' : '添加此用户资料'}</Button>
                    </div>

                    {detail.warning && <Notice tone="warning" className="mt-3">{detail.warning}</Notice>}

                    <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-4">
                      <StatTile label="关联对话" value={<span className="num">{detail.conversations.length}</span>} tint="green" />
                      <StatTile label="路由关联" value={<span className="num">{detail.conversations.filter((item) => item.routed).length}</span>} />
                      <StatTile label="列表关联" value={<span className="num">{detail.conversations.filter((item) => item.manual).length}</span>} />
                      <StatTile label="平台账号" value={<span className="truncate text-[12px]">{typeLabel(detail)} <span className="font-mono">{detail.user_id}</span></span>} />
                    </div>

                    <section className="mt-4">
                      <div className="mb-2 flex items-center justify-between gap-2">
                        <h3 className="text-[13px] font-semibold text-fg">关联对话</h3>
                        <span className="text-[10.5px] text-fg-4">只显示明确归属或手动关联的对话</span>
                      </div>
                      <p className="mb-2 text-[11px] leading-5 text-fg-4">这里只显示有明确用户归属或手动关联的对话。群共享对话仍按群组织。</p>
                      {!detail.conversations.length && (
                        <div className="rounded-xl border border-dashed border-[var(--glass-border-strong)] px-4 py-6 text-center text-[12px] text-fg-4">此用户暂无关联对话</div>
                      )}
                      <div className="space-y-2">
                        {detail.conversations.map((item) => (
                          <div key={item.conversation_id} className="flex flex-wrap items-center gap-2 rounded-xl border border-line bg-glass p-3">
                            <div className="min-w-0 flex-1">
                              <p className="flex flex-wrap items-center gap-1.5 text-[12.5px] font-medium text-fg">
                                <span className="break-all">{item.title}</span>
                                {item.routed && <Badge tone="green">路由关联</Badge>}
                                {item.manual && <Badge tone="orange">列表关联</Badge>}
                                {!item.exists && <Badge tone="red">对话不存在</Badge>}
                              </p>
                              <p className="mt-0.5 truncate font-mono text-[10.5px] text-fg-4">{item.conversation_id}{item.last_activity_at ? ` · ${formatTime(item.last_activity_at)}` : ' · 活动时间未知'}</p>
                            </div>
                            <Button size="sm" variant="ghost" disabled={!item.exists} onClick={() => onOpen(detail.platform_id, item.conversation_id)} icon={<MessageSquare className="h-3.5 w-3.5" />}>查看对话</Button>
                          </div>
                        ))}
                      </div>
                    </section>

                    {detail.has_profile && (
                      <details className="mt-4 rounded-xl border border-line bg-glass px-3 py-2">
                        <summary className="cursor-pointer text-[12px] font-medium text-fg-2">高级资料操作</summary>
                        <div className="space-y-3 pt-3">
                          <Notice tone="info">列表关联不会改写平台路由；本地按用户选择会话的调用可能使用此列表。移除列表关联后，路由关联仍然保留。</Notice>
                          <div className="flex flex-wrap items-end gap-2">
                            <div className="min-w-[180px] flex-1">
                              <Field label="当前平台的对话 ID" hint="只添加本地列表关联，不修改平台路由">
                                <Input aria-label="关联对话 ID" value={session} disabled={busy} onChange={(event) => setSession(event.target.value)} placeholder="输入对话 ID" />
                              </Field>
                            </div>
                            <Button size="sm" variant="tinted" tint="green" icon={<Link2 className="h-3.5 w-3.5" />} disabled={busy || !session.trim()}
                              onClick={() => void mutate(detail.platform_id, detail.user_id, { action: 'associate', session_id: session.trim(), expected_revision: detail.revision })}>添加关联</Button>
                          </div>
                          {detail.user_session.length > 0 && (
                            <div className="space-y-1.5">
                              <p className="text-[11px] text-fg-4">已保存的会话关联</p>
                              {detail.user_session.map((identity) => (
                                <div key={identity} className="flex items-center justify-between gap-2 rounded-lg border border-line bg-glass px-2.5 py-1.5">
                                  <span className="break-all font-mono text-[10.5px] text-fg-3">{identity}</span>
                                  <Button size="sm" variant="ghost" disabled={busy}
                                    onClick={async () => {
                                      if (await confirm('确定移除此列表关联吗？平台路由和对话内容会保留；本地按用户选择会话的调用可能受影响。')) {
                                        void mutate(detail.platform_id, detail.user_id, { action: 'dissociate', session_id: identity, expected_revision: detail.revision });
                                      }
                                    }}
                                    icon={<Unlink className="h-3.5 w-3.5" />} className="!text-fg-3 hover:!text-red">移除关联</Button>
                                </div>
                              ))}
                            </div>
                          )}
                          <div className="rounded-xl border border-[rgb(var(--rgb-red)/0.22)] bg-[rgb(var(--rgb-red)/0.05)] p-3">
                            <p className="text-[11.5px] leading-5 text-fg-3">删除用户资料会删除昵称和列表关联，保留对话、消息和平台路由。后续消息可能重新创建资料。</p>
                            <Button size="sm" variant="danger" className="mt-2" disabled={busy}
                              onClick={async () => {
                                if (await confirm('确定删除此用户资料吗？昵称和列表关联将删除，对话、消息和平台路由会保留，后续消息可能重新创建资料。')) {
                                  void mutate(detail.platform_id, detail.user_id, { action: 'delete', expected_revision: detail.revision });
                                }
                              }}
                              icon={<Trash2 className="h-3.5 w-3.5" />}>删除用户资料</Button>
                          </div>
                        </div>
                      </details>
                    )}

                    <div className="mt-3 grid gap-1.5 rounded-xl border border-line bg-glass p-3">
                      <KeyValue label="平台实例" mono>{detail.platform_id}</KeyValue>
                      <KeyValue label="用户 ID" mono>{detail.user_id}</KeyValue>
                      <KeyValue label="最近活动">{detail.conversations[0]?.last_activity_at ? formatTime(detail.conversations[0].last_activity_at) : '未知'}</KeyValue>
                      <KeyValue label="资料修订" mono><span className="break-all text-[11px]">{detail.revision}</span></KeyValue>
                    </div>
                  </>}
            </div>
          </div>
        )}
      </div>

      <Modal open={editor !== null} onClose={close} size="md"
        title={editor === 'create' || !detail?.has_profile ? '添加用户资料' : '编辑用户资料'}
        description="昵称保存在本地，不修改平台账号资料；平台实例与用户 ID 确定资料归属。"
        icon={<User className="h-[18px] w-[18px]" />} className="tint-green"
        footer={<><Button variant="ghost" onClick={close} disabled={busy}>取消</Button>
          <Button variant="tinted" tint="green" disabled={busy || !form.platform || !form.user.trim() || (editor === 'update' && detail?.has_profile && !dirty)}
            onClick={() => void mutate(form.platform, form.user.trim(), {
              action: editor === 'create' || !detail?.has_profile ? 'create' : 'update',
              nickname: form.nickname,
              expected_revision: editor === 'update' ? detail?.revision : catalog.new_revision,
            })}>{busy ? '保存中…' : '保存资料'}</Button></>}>
        <div className="space-y-3">
          <p className="text-[12px] leading-5 text-fg-3">昵称保存在本地，不修改平台账号资料。平台实例和用户 ID 确定资料归属。</p>
          {(formError || (error && editor !== null)) && <p role="alert" className="text-[12px] text-red">{formError || error}</p>}
          <Field label="平台实例" required>
            <KitSelect aria-label="资料平台实例" className="field !h-9 w-full" value={form.platform} disabled={busy || editor === 'update'}
              onChange={(value) => setForm({ ...form, platform: value })}>
              <option value="">请选择平台实例</option>
              {platforms.map((item) => <option key={item.id} value={item.id}>{item.label} · {item.type_label}</option>)}
            </KitSelect>
          </Field>
          <Field label="用户 ID" required hint="与平台上的用户 ID 完全一致">
            <Input aria-label="资料用户 ID" value={form.user} disabled={busy || editor === 'update'} placeholder="例如 1042"
              onChange={(event) => setForm({ ...form, user: event.target.value })} />
          </Field>
          <Field label="本地昵称" hint="仅用于展示与搜索">
            <Input aria-label="本地昵称" value={form.nickname} disabled={busy} placeholder="例如 林可"
              onChange={(event) => setForm({ ...form, nickname: event.target.value })} />
          </Field>
        </div>
      </Modal>
    </>
  );
}
