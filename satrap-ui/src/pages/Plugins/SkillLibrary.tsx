import { useEffect, useMemo, useState } from 'react';
import {
  AlertTriangle, BookOpen, FileText, Pencil, Plug, Plus, RefreshCw, Search, Trash2, Upload,
} from 'lucide-react';
import { skillsApi, type ManagedSkill, type SkillDraft, type PluginSkill } from '@/api/skills';
import { Badge, Button, Card, CardHeader, EmptyState, IconButton, Input, Skeleton, Textarea } from '@/components/kit';
import { Modal } from '@/components/ui/Modal';
import { useConfirm } from '@/hooks/useConfirm';
import { confirmDiscard } from '@/hooks/useDirtyGuard';
import { errorText } from '@/utils/errorText';
import { cn } from '@/utils/cn';
import { TealPrimary, TintTabs } from './PluginBits';
import { Select as KitSelect } from '@/components/kit/Select';

type Draft = SkillDraft & { original?: ManagedSkill; mode: 'text' | 'url' };
const sessionTypes = [['chat', 'Chat'], ['platform', '平台会话'], ['embedded', '嵌入会话']] as const;

function ScopeChips({ scopes }: { scopes: string[] }) {
  return (
    <div className="flex flex-wrap gap-1.5">
      {sessionTypes.map(([scope, label]) => (
        <span key={scope} className={cn('rounded-full border px-2 py-0.5 text-[11px]',
          scopes.includes(scope) ? 'border-[rgb(var(--rgb-teal)/0.25)] bg-[rgb(var(--rgb-teal)/0.1)] text-teal' : 'border-line bg-glass text-fg-4')}>
          {label}
        </span>
      ))}
    </div>
  );
}

function ManagedSkillCard({ item, busy, onEdit, onDelete }: { item: ManagedSkill; busy: boolean; onEdit: () => void; onDelete: () => void }) {
  return (
    <Card padding="sm" tint={item.enabled ? 'teal' : undefined} className={cn('group flex h-full flex-col gap-3', !item.enabled && 'opacity-80')}>
      <div className="flex items-start gap-3">
        <div className="grid h-10 w-10 flex-none place-items-center rounded-xl border border-[rgb(var(--rgb-teal)/0.22)] bg-[rgb(var(--rgb-teal)/0.12)] text-teal shadow-[inset_0_1px_0_var(--glass-highlight)]">
          <BookOpen className="h-[18px] w-[18px]" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="break-all font-mono text-[13.5px] font-semibold text-fg">{item.name}</h3>
            <Badge tone={item.enabled ? 'green' : 'neutral'} dot>{item.enabled ? '已启用' : '已停用'}</Badge>
          </div>
          <p className="mt-0.5 break-all font-mono text-[11px] text-fg-4">user.{item.name} · rev {item.revision}</p>
        </div>
        <div className="flex flex-none items-center gap-1 opacity-0 transition-opacity group-hover:opacity-100 focus-within:opacity-100">
          <IconButton label="编辑技能" size="sm" disabled={busy} onClick={onEdit}><Pencil className="h-3.5 w-3.5" /></IconButton>
          <IconButton label="删除技能" size="sm" disabled={busy} onClick={onDelete}><Trash2 className="h-3.5 w-3.5" /></IconButton>
        </div>
      </div>
      <p className="line-clamp-2 min-h-[40px] text-[13px] leading-5 text-fg-2">{item.description || '未提供用途简介'}</p>
      <div className="mt-auto flex flex-wrap items-center justify-between gap-2 border-t border-line pt-2.5">
        <ScopeChips scopes={item.scopes} />
        <span className="flex min-w-0 items-center gap-1 text-[10.5px] text-fg-4"><FileText className="h-3 w-3 flex-none" /><span className="truncate">{item.source}</span></span>
      </div>
      <div className="flex gap-2">
        <Button size="sm" disabled={busy} onClick={onEdit}>查看和编辑</Button>
        <Button size="sm" variant="danger" disabled={busy} onClick={onDelete}>删除</Button>
      </div>
    </Card>
  );
}

function PluginSkillCard({ item, onPreview }: { item: PluginSkill; onPreview: () => void }) {
  return (
    <Card padding="sm" className="group flex h-full flex-col gap-3">
      <div className="flex items-start gap-3">
        <div className="grid h-10 w-10 flex-none place-items-center rounded-xl border border-[rgb(var(--rgb-purple)/0.22)] bg-[rgb(var(--rgb-purple)/0.12)] text-purple shadow-[inset_0_1px_0_var(--glass-highlight)]">
          <Plug className="h-[18px] w-[18px]" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="break-all font-mono text-[13.5px] font-semibold text-fg">{item.name}</h3>
            <Badge tone="purple">只读</Badge>
          </div>
          <span className="mt-0.5 block break-all text-[11px] text-fg-4">来自 {item.plugin} · 只读</span>
        </div>
      </div>
      <p className="line-clamp-2 min-h-[40px] text-[13px] leading-5 text-fg-2">{item.description || '未提供用途简介'}</p>
      {item.tools.length > 0 && <div className="flex flex-wrap gap-1.5">{item.tools.map((tool) => <Badge key={tool} tone="orange">{tool}</Badge>)}</div>}
      <div className="mt-auto flex items-center justify-between gap-2 border-t border-line pt-2.5">
        <span className="min-w-0 truncate font-mono text-[10.5px] text-fg-4">{item.source}</span>
        <Button size="sm" variant="ghost" onClick={onPreview}>查看说明</Button>
      </div>
    </Card>
  );
}

export function SkillLibrary({ onDirty }: { onDirty: (dirty: boolean) => void }) {
  const confirm = useConfirm();
  const [items, setItems] = useState<ManagedSkill[]>([]);
  const [revision, setRevision] = useState(0);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [draft, setDraft] = useState<Draft>();
  const [formError, setFormError] = useState('');
  const [source, setSource] = useState<'managed' | 'plugins'>('managed');
  const [query, setQuery] = useState('');
  const [scope, setScope] = useState<'all' | 'chat' | 'platform' | 'embedded'>('all');
  const [status, setStatus] = useState<'all' | 'enabled' | 'disabled'>('all');
  const [pluginItems, setPluginItems] = useState<PluginSkill[]>([]);
  const [pluginNotices, setPluginNotices] = useState<string[]>([]);
  const [pluginError, setPluginError] = useState('');
  const [pluginLoading, setPluginLoading] = useState(false);
  const [preview, setPreview] = useState<PluginSkill>();
  useEffect(() => { onDirty(!!draft); return () => onDirty(false); }, [draft, onDirty]);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError('');
    skillsApi.list(controller.signal).then((data) => { if (!controller.signal.aborted) setItems(data); })
      .catch((error) => { if (!controller.signal.aborted) setError(errorText(error)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [revision]);
  useEffect(() => {
    if (source !== 'plugins') return;
    const controller = new AbortController();
    setPluginLoading(true); setPluginError('');
    skillsApi.pluginCatalog(controller.signal).then((data) => {
      if (!controller.signal.aborted) { setPluginItems(data.items); setPluginNotices(data.notices); }
    }).catch((error) => { if (!controller.signal.aborted) setPluginError(errorText(error)); })
      .finally(() => { if (!controller.signal.aborted) setPluginLoading(false); });
    return () => controller.abort();
  }, [revision, source]);
  const close = () => { if (!busy && confirmDiscard()) { setDraft(undefined); setFormError(''); } };
  const save = async () => {
    if (!draft || busy) return;
    setBusy(true); setFormError('');
    try {
      const payload: SkillDraft = {
        name: draft.name, description: draft.description, enabled: draft.enabled, scopes: draft.scopes,
        ...(draft.mode === 'url' ? { url: draft.url } : { markdown: draft.markdown, filename: draft.filename || draft.original?.source }),
        ...(draft.original ? { expected_revision: draft.original.revision } : {}),
      };
      if (draft.original) await skillsApi.update(draft.original.name, payload);
      else await skillsApi.create(payload);
      setDraft(undefined); setSource('managed'); setRevision((value) => value + 1);
    } catch (error) { setFormError(errorText(error)); }
    finally { setBusy(false); }
  };
  const remove = async (item: ManagedSkill) => {
    if (!(await confirm(`删除技能 ${item.name}?`))) return;
    setBusy(true); setError('');
    try { await skillsApi.delete(item); setRevision((value) => value + 1); }
    catch (error) { setError(errorText(error)); }
    finally { setBusy(false); }
  };

  const counts = useMemo(() => ({
    enabled: items.filter((item) => item.enabled).length,
    disabled: items.filter((item) => !item.enabled).length,
  }), [items]);
  const filtered = useMemo(() => {
    const term = query.trim().toLowerCase();
    return items.filter((item) => {
      const queryMatches = !term || `${item.name} ${item.description} ${item.source}`.toLowerCase().includes(term);
      const scopeMatches = scope === 'all' || item.scopes.includes(scope);
      const statusMatches = status === 'all' || (status === 'enabled' ? item.enabled : !item.enabled);
      return queryMatches && scopeMatches && statusMatches;
    });
  }, [items, query, scope, status]);
  const filteredPlugins = useMemo(() => {
    const term = query.trim().toLowerCase();
    return pluginItems.filter((item) => !term || `${item.plugin} ${item.name} ${item.description}`.toLowerCase().includes(term));
  }, [pluginItems, query]);

  return (
    <section className="space-y-4" aria-label="技能库">
      <Card padding="lg" tint="teal">
        <CardHeader
          icon={<BookOpen className="h-4 w-4" />}
          title="技能库"
          description="导入 Markdown 技能后，在插件的使用位置启用 skills 能力。Agent 会按任务读取说明，也可以用 /skill use 强制加载。"
          actions={<>
            <Button size="sm" icon={<RefreshCw className={cn('h-3.5 w-3.5', (loading || pluginLoading) && 'animate-spin-slow')} />} disabled={busy || loading || pluginLoading} onClick={() => setRevision((value) => value + 1)}>刷新技能</Button>
            <TealPrimary size="sm" icon={<Plus className="h-3.5 w-3.5" />} disabled={busy} onClick={() => { setFormError(''); setDraft({ mode: 'text', name: '', description: '', markdown: '', enabled: true, scopes: sessionTypes.map(([value]) => value) }); }}>导入技能</TealPrimary>
          </>}
        />
        <div className="flex flex-wrap items-center gap-2.5">
          <TintTabs<'managed' | 'plugins'>
            label="技能来源"
            size="sm"
            value={source}
            onChange={(value) => { setSource(value); setQuery(''); }}
            items={[
              { value: 'managed', label: '导入的技能', count: items.length },
              { value: 'plugins', label: '插件技能', count: pluginItems.length },
            ]}
          />
          {source === 'managed' && (
            <TintTabs<'all' | 'enabled' | 'disabled'>
              label="启用状态"
              size="sm"
              value={status}
              onChange={setStatus}
              items={[
                { value: 'all', label: '全部', count: items.length },
                { value: 'enabled', label: '已启用', count: counts.enabled },
                { value: 'disabled', label: '已停用', count: counts.disabled },
              ]}
            />
          )}
          <div className="ml-auto flex flex-wrap items-center gap-2">
            <div className="w-56 max-w-full">
              <Input leading={<Search className="h-4 w-4" />} value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索名称或简介…" aria-label="搜索技能" />
            </div>
            <KitSelect aria-label="适用会话筛选" className="field h-8 w-36 text-[12.5px]" value={scope} onChange={(value) => setScope(value as typeof scope)}>
              <option value="all">全部会话</option>
              {sessionTypes.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
            </KitSelect>
          </div>
        </div>
      </Card>

      {source === 'managed' ? (
        <>
          {error && <p role="alert" className="text-[12.5px] text-red">{error}</p>}
          {loading ? (
            <div className="grid gap-3 lg:grid-cols-2">
              {Array.from({ length: 4 }).map((_, index) => (
                <Card key={index} padding="sm" className="space-y-3">
                  <div className="flex gap-3"><Skeleton className="h-10 w-10" /><div className="flex-1 space-y-2"><Skeleton className="w-1/3" /><Skeleton className="w-1/2" /></div></div>
                  <Skeleton className="w-full" /><Skeleton className="w-2/3" />
                </Card>
              ))}
            </div>
          ) : filtered.length ? (
            <div className="stagger grid gap-3 lg:grid-cols-2">
              {filtered.map((item, index) => (
                <div key={item.name} style={{ ['--i' as string]: index }}>
                  <ManagedSkillCard item={item} busy={busy}
                    onEdit={() => { setFormError(''); setDraft({ ...item, original: item, mode: 'text' }); }}
                    onDelete={() => void remove(item)} />
                </div>
              ))}
            </div>
          ) : (
            <Card padding="none">
              <EmptyState
                tint="teal"
                icon={<BookOpen className="h-6 w-6" />}
                title={items.length ? '没有匹配的技能' : '尚未导入外部技能'}
                description={items.length ? '尝试更换关键字、会话范围或启用状态筛选' : '导入 Markdown 技能后，Agent 可以按任务读取更专业的操作说明'}
                actions={items.length
                  ? <Button onClick={() => { setQuery(''); setScope('all'); setStatus('all'); }}>清除筛选</Button>
                  : <TealPrimary icon={<Plus className="h-4 w-4" />} onClick={() => { setFormError(''); setDraft({ mode: 'text', name: '', description: '', markdown: '', enabled: true, scopes: sessionTypes.map(([value]) => value) }); }}>导入第一个技能</TealPrimary>}
              />
            </Card>
          )}
        </>
      ) : (
        <>
          <div className="rounded-xl border border-line bg-glass px-4 py-3 text-[12.5px] leading-5 text-fg-3">
            这里展示其他插件带入的技能资料，仅供查看。实际可用技能取决于当前会话的插件、能力开关与替代关系。
          </div>
          {pluginNotices.length > 0 && (
            <div className="space-y-2">
              {pluginNotices.map((notice) => (
                <div key={notice} className="tint-orange flex items-start gap-2 rounded-xl border border-[rgb(var(--tint)/0.22)] bg-[rgb(var(--tint)/0.07)] px-3.5 py-2.5 text-[12.5px] text-fg-2">
                  <AlertTriangle className="mt-0.5 h-3.5 w-3.5 flex-none text-orange" />{notice}
                </div>
              ))}
            </div>
          )}
          {pluginError && <p role="alert" className="text-[12.5px] text-red">{pluginError}</p>}
          {pluginLoading ? (
            <div className="grid gap-3 lg:grid-cols-2">
              {Array.from({ length: 4 }).map((_, index) => <Card key={index} padding="sm" className="space-y-3"><Skeleton className="w-1/3" /><Skeleton className="w-full" /><Skeleton className="w-1/2" /></Card>)}
            </div>
          ) : filteredPlugins.length ? (
            <div className="stagger grid gap-3 lg:grid-cols-2">
              {filteredPlugins.map((item, index) => (
                <div key={`${item.plugin}/${item.name}`} style={{ ['--i' as string]: index }}>
                  <PluginSkillCard item={item} onPreview={() => setPreview(item)} />
                </div>
              ))}
            </div>
          ) : !pluginError ? (
            <Card padding="none">
              <EmptyState tint="teal" icon={<Plug className="h-6 w-6" />} title="没有匹配的插件技能" description="清除搜索条件，或检查来源插件是否已安装并启用" />
            </Card>
          ) : null}
        </>
      )}

      <Modal open={!!preview} onClose={() => setPreview(undefined)} title={preview ? `${preview.plugin} / ${preview.name}` : '插件技能'} size="xl">
        {preview && (
          <div className="space-y-4">
            <p className="text-[13px] leading-5 text-fg-2">插件提供的技能说明，只读预览。修改请前往来源插件。</p>
            <p className="text-[13px] text-fg-2">{preview.description || '未提供用途简介'}</p>
            {preview.tools.length > 0 && (
              <div className="flex flex-wrap items-center gap-1.5">
                <span className="text-[11.5px] text-fg-4">所需工具</span>
                {preview.tools.map((tool) => <Badge key={tool} tone="orange">{tool}</Badge>)}
                <span className="text-[11px] text-fg-4">仍受原权限限制</span>
              </div>
            )}
            <p className="break-all font-mono text-[11px] text-fg-4">{preview.source}</p>
            {preview.instructions !== null ? (
              <pre className="scroll max-h-[52vh] whitespace-pre-wrap break-words rounded-xl border border-line bg-[rgb(0_0_0/0.22)] p-4 font-mono text-[12.5px] leading-6 text-fg-2 [[data-theme=light]_&]:bg-[rgb(15_23_42/0.04)]">{preview.instructions}</pre>
            ) : (
              <p className="rounded-xl border border-dashed border-line px-4 py-5 text-[13px] leading-5 text-fg-3">
                该能力由插件声明，正文可能在运行时生成。请在启用来源插件的会话中使用 <code className="rounded bg-glass-hover px-1 font-mono text-fg-2">/skill list</code> 和 <code className="rounded bg-glass-hover px-1 font-mono text-fg-2">/skill show</code> 查看。
              </p>
            )}
            <div className="flex justify-end"><Button onClick={() => setPreview(undefined)}>关闭</Button></div>
          </div>
        )}
      </Modal>

      <Modal open={!!draft} onClose={close} title={draft?.original ? '编辑技能' : '导入技能'} size="xl">
        {draft && (
          <form className="space-y-4" onSubmit={(event) => { event.preventDefault(); void save(); }}>
            <p className="text-[12.5px] leading-5 text-fg-3">名称和简介可从 front matter 读取；普通 Markdown 请在这里补充。单个文件上限 256 KiB。</p>
            <div className="grid gap-4 sm:grid-cols-2">
              <label className="block text-[13px] font-medium text-fg-2">
                技能名称
                <input aria-label="技能名称" className="field mt-1 h-9 w-full" value={draft.name || ''} disabled={busy || !!draft.original} onChange={(event) => setDraft({ ...draft, name: event.target.value })} />
              </label>
              <label className="block text-[13px] font-medium text-fg-2">
                用途简介
                <input aria-label="用途简介" className="field mt-1 h-9 w-full" maxLength={512} value={draft.description || ''} disabled={busy} onChange={(event) => setDraft({ ...draft, description: event.target.value })} />
              </label>
            </div>
            {!draft.original && (
              <label className="block text-[13px] font-medium text-fg-2">
                导入方式
                <KitSelect aria-label="导入方式" className="field mt-1 h-9 w-full" disabled={busy} value={draft.mode} onChange={(value) => setDraft({ ...draft, mode: value as Draft['mode'] })}>
                  <option value="text">上传文件或粘贴 Markdown</option>
                  <option value="url">HTTPS Markdown 地址</option>
                </KitSelect>
              </label>
            )}
            {draft.mode === 'url' ? (
              <label className="block text-[13px] font-medium text-fg-2">
                Markdown 地址
                <input aria-label="Markdown 地址" className="field mt-1 h-9 w-full" type="url" required disabled={busy} value={draft.url || ''} onChange={(event) => setDraft({ ...draft, url: event.target.value })} placeholder="https://skills.example.com/review.md" />
              </label>
            ) : (
              <div className="space-y-3">
                {!draft.original && (
                  <label className="flex cursor-pointer items-center gap-3 rounded-xl border-2 border-dashed border-[var(--glass-border-strong)] px-4 py-3 transition-colors hover:border-[rgb(var(--rgb-teal)/0.5)] hover:bg-[rgb(var(--rgb-teal)/0.04)]">
                    <span className="grid h-9 w-9 flex-none place-items-center rounded-xl border border-[rgb(var(--rgb-teal)/0.25)] bg-[rgb(var(--rgb-teal)/0.12)] text-teal"><Upload className="h-4 w-4" /></span>
                    <span className="min-w-0 flex-1">
                      <span className="block text-[13px] font-medium text-fg">上传 Markdown 文件</span>
                      <span className="block truncate text-[11.5px] text-fg-3">{draft.filename || '支持 .md / .markdown，最大 256 KiB'}</span>
                    </span>
                    <input
                      aria-label="Markdown 文件"
                      type="file"
                      accept=".md,.markdown"
                      disabled={busy}
                      className="max-w-[220px] text-[11px] text-fg-3"
                      onChange={async (event) => {
                        const file = event.target.files?.[0]; if (!file) return;
                        try {
                          if (file.size > 256 * 1024 || !/\.(md|markdown)$/i.test(file.name)) throw new Error('请选择不超过 256 KiB 的 Markdown 文件');
                          const markdown = new TextDecoder('utf-8', { fatal: true }).decode(await file.arrayBuffer());
                          setDraft((current) => current ? { ...current, markdown, filename: file.name } : current);
                          setFormError('');
                        } catch (error) { setFormError(errorText(error)); }
                      }}
                    />
                  </label>
                )}
                <label className="block text-[13px] font-medium text-fg-2">
                  Markdown 正文
                  <Textarea aria-label="Markdown 正文" className="mt-1 min-h-64 w-full font-mono text-[12.5px]" required disabled={busy} value={draft.markdown || ''} onChange={(event) => setDraft({ ...draft, markdown: event.target.value })} />
                </label>
              </div>
            )}
            <label className="flex items-center gap-2 text-[13px] text-fg-2">
              <input type="checkbox" aria-label="允许使用此技能" className="h-4 w-4 accent-[rgb(var(--rgb-teal))]" disabled={busy} checked={draft.enabled ?? true} onChange={(event) => setDraft({ ...draft, enabled: event.target.checked })} />
              允许使用此技能
            </label>
            <fieldset disabled={busy} className="flex flex-wrap gap-3">
              <legend className="text-[13px] font-medium text-fg-2">适用会话</legend>
              {sessionTypes.map(([value, label]) => (
                <label key={value} className="flex items-center gap-1.5 text-[12.5px] text-fg-2">
                  <input type="checkbox" className="h-3.5 w-3.5 accent-[rgb(var(--rgb-teal))]" checked={draft.scopes?.includes(value) || false} onChange={(event) => setDraft({ ...draft, scopes: event.target.checked ? [...(draft.scopes || []), value] : draft.scopes?.filter((scope) => scope !== value) })} />{label}
                </label>
              ))}
            </fieldset>
            {formError && <p role="alert" className="text-[12.5px] text-red">{formError}</p>}
            <div className="flex justify-end gap-2">
              <Button type="button" variant="ghost" disabled={busy} onClick={close}>取消</Button>
              <Button type="submit" variant="primary" disabled={busy || !draft.scopes?.length}>{busy ? '正在保存…' : '保存技能'}</Button>
            </div>
          </form>
        )}
      </Modal>
    </section>
  );
}
