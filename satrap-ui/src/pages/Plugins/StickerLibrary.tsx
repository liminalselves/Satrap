import { createUuid } from '@/utils/uuid';
import { ConfirmModal } from '@/components/common/ConfirmModal';
import { useEffect, useRef, useState } from 'react';
import { errorText } from '@/utils/errorText';
import { controlApi } from '@/api/control';
import type { GroupChatSticker, GroupChatStickerPage, PlatformConfig } from '@/api/types';
import { Badge, Button, Card, CardHeader, Input, Skeleton } from '@/components/kit';
import { Modal } from '@/components/ui/Modal';
import { confirmDiscard } from '@/hooks/useDirtyGuard';
import { Image as ImageIcon, Loader2, Plus, RefreshCw, Search, Smile } from 'lucide-react';
import { TealPrimary } from './PluginBits';
import { Select as KitSelect } from '@/components/kit/Select';

type Draft = { original?: GroupChatSticker; mode: 'image' | 'native'; name: string; tags: string; collection: string; enabled: boolean; platform: string; nativeKey: string; file?: File };
const emptyDraft = (): Draft => ({ mode: 'image', name: '', tags: '', collection: '常用', enabled: true, platform: '', nativeKey: '' });

/** 缩略图按 sticker_id 缓存, 翻页或重渲染不重复请求 */
const previewCache = new Map<string, string | null>();

function StickerThumb({ item }: { item: GroupChatSticker }) {
  const [preview, setPreview] = useState<string | null | undefined>(() => previewCache.get(item.sticker_id));
  useEffect(() => {
    if (preview !== undefined) return;
    let active = true;
    controlApi.stickerPreview(item.sticker_id)
      .then((result) => {
        previewCache.set(item.sticker_id, result.preview);
        if (active) setPreview(result.preview);
      })
      .catch(() => { if (active) setPreview(null); });
    return () => { active = false; };
  }, [item.sticker_id, preview]);
  if (preview) {
    return <img src={preview} alt={item.name} loading="lazy" className="h-24 w-full rounded-xl bg-[rgb(0_0_0/0.16)] object-contain" />;
  }
  return (
    <div className="grid h-24 w-full place-items-center rounded-xl border border-dashed border-line bg-glass text-fg-4">
      {preview === undefined
        ? <Loader2 className="h-4 w-4 animate-spin-slow" aria-label="正在读取预览" />
        : <span className="flex flex-col items-center gap-1 text-[10.5px]"><ImageIcon className="h-4 w-4" />{item.kind === 'native' ? '平台原生表情' : '暂无预览'}</span>}
    </div>
  );
}

export function StickerLibrary({ onDirty }: { onDirty: (dirty: boolean) => void }) {
  const [data, setData] = useState<GroupChatStickerPage>();
  const [keyword, setKeyword] = useState('');
  const [query, setQuery] = useState('');
  const [cursors, setCursors] = useState<string[]>([]);
  const [refresh, setRefresh] = useState(0);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [draft, setDraft] = useState<Draft>();
  const [formError, setFormError] = useState('');
  const [platforms, setPlatforms] = useState<PlatformConfig[]>([]);
  const [native, setNative] = useState<Array<{ key: string; name: string }>>([]);
  const [nativeLoading, setNativeLoading] = useState(false);
  const [preview, setPreview] = useState<{ title: string; image?: string | null; error?: string }>();
  const [deleting, setDeleting] = useState<GroupChatSticker>();
  const mounted = useRef(true);
  const previewRequest = useRef(0);
  const intent = useRef('');
  const deleteKey = useRef('');
  const cursor = cursors[cursors.length - 1];
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; previewRequest.current += 1; }; }, []);
  useEffect(() => { onDirty(!!draft); return () => onDirty(false); }, [draft, onDirty]);
  useEffect(() => {
    let disposed = false;
    setLoading(true); setError('');
    controlApi.listStickers({ keyword: query, cursor, limit: 20 }).then((result) => { if (!disposed) setData(result); })
      .catch((error) => { if (!disposed) setError(errorText(error)); }).finally(() => { if (!disposed) setLoading(false); });
    return () => { disposed = true; };
  }, [query, cursor, refresh]);
  useEffect(() => {
    if (draft?.mode !== 'native' || draft.original) return;
    let disposed = false;
    controlApi.listPlatforms().then((result) => { if (!disposed) setPlatforms(result.platforms || []); })
      .catch((error) => { if (!disposed) setFormError(errorText(error)); });
    return () => { disposed = true; };
  }, [draft?.mode, draft?.original]);
  useEffect(() => {
    let disposed = false;
    setNative([]);
    if (!draft?.platform || draft.mode !== 'native') return;
    setNativeLoading(true); setFormError('');
    controlApi.nativeStickerCatalog(draft.platform).then((result) => { if (!disposed) setNative(result.items); })
      .catch((error) => { if (!disposed) setFormError(errorText(error)); }).finally(() => { if (!disposed) setNativeLoading(false); });
    return () => { disposed = true; };
  }, [draft?.platform, draft?.mode]);
  const change = (values: Partial<Draft>) => { intent.current = ''; setFormError(''); setDraft((current) => current ? { ...current, ...values } : current); };
  const closeDraft = () => { if (!busy && confirmDiscard()) setDraft(undefined); };
  const save = async () => {
    if (!draft || busy) return;
    setBusy(true); setFormError('');
    try {
      const tags = draft.tags.split(/[,，]/).map((tag) => tag.trim()).filter(Boolean);
      const metadata = { name: draft.name.trim(), tags, collection: draft.collection.trim(), idempotency_key: intent.current || (intent.current = createUuid()) };
      if (!metadata.name || !metadata.collection) throw new Error('请填写表情名称和集合');
      if (draft.original) {
        await controlApi.updateSticker(draft.original.sticker_id, { ...metadata, enabled: draft.enabled, expected_revision: draft.original.content_revision });
      } else if (draft.mode === 'native') {
        if (!draft.platform || !draft.nativeKey) throw new Error('请选择平台和目录中的表情');
        await controlApi.addNativeSticker({ ...metadata, platform_id: draft.platform, native_key: draft.nativeKey });
      } else {
        if (!draft.file) throw new Error('请选择图片');
        if (draft.file.size > 10 * 1024 * 1024) throw new Error('单张图片不能超过 10 MiB');
        await controlApi.uploadSticker(draft.file, metadata);
      }
      if (!mounted.current) return;
      setDraft(undefined); setNotice('表情已保存，请在对应群的「表情设置」中启用集合'); setCursors([]); setRefresh((value) => value + 1);
    } catch (error) { if (mounted.current) setFormError(errorText(error)); }
    finally { if (mounted.current) setBusy(false); }
  };
  const inspect = async (item: GroupChatSticker) => {
    const request = ++previewRequest.current;
    setPreview({ title: item.name });
    try {
      const result = await controlApi.stickerPreview(item.sticker_id);
      if (mounted.current && request === previewRequest.current) setPreview({ title: item.name, image: result.preview });
    } catch (error) { if (mounted.current && request === previewRequest.current) setPreview({ title: item.name, error: errorText(error) }); }
  };
  const remove = async () => {
    if (!deleting || busy) return;
    setBusy(true); setFormError('');
    try {
      await controlApi.deleteSticker(deleting.sticker_id, deleting.content_revision, deleteKey.current);
      if (!mounted.current) return;
      setDeleting(undefined); setNotice('表情已删除，未提交的回复草稿将不能使用它'); setRefresh((value) => value + 1);
    } catch (error) { if (mounted.current) setFormError(errorText(error)); }
    finally { if (mounted.current) setBusy(false); }
  };
  return (
    <section className="space-y-4" aria-label="表情库">
      <Card padding="lg" tint="orange">
        <CardHeader
          icon={<Smile className="h-4 w-4" />}
          title="表情库"
          description="上传图片并填写名称和标签。每个群单独启用集合，默认没有可用的用户表情。原生表情由所选平台的已确认目录提供。"
          actions={<>
            <Button size="sm" icon={<Plus className="h-3.5 w-3.5" />} onClick={() => { intent.current = ''; setFormError(''); setDraft(emptyDraft()); }}>添加图片表情</Button>
            <Button size="sm" variant="ghost" onClick={() => { intent.current = ''; setFormError(''); setDraft({ ...emptyDraft(), mode: 'native' }); }}>添加平台表情</Button>
          </>}
        />
        <form className="flex flex-wrap gap-2" onSubmit={(event) => { event.preventDefault(); setQuery(keyword); setCursors([]); setRefresh((value) => value + 1); }}>
          <div className="min-w-0 flex-1"><Input leading={<Search className="h-4 w-4" />} aria-label="表情名称或标签" placeholder="搜索名称或标签" value={keyword} onChange={(event) => setKeyword(event.target.value)} /></div>
          <Button type="submit" disabled={loading}>搜索表情</Button>
          <Button type="button" icon={<RefreshCw className={loading ? 'h-3.5 w-3.5 animate-spin-slow' : 'h-3.5 w-3.5'} />} onClick={() => setRefresh((value) => value + 1)} disabled={loading}>刷新表情</Button>
        </form>
      </Card>

      {notice && <p role="status" className="flex items-center gap-2 rounded-xl border border-[rgb(var(--rgb-green)/0.2)] bg-[rgb(var(--rgb-green)/0.07)] px-3.5 py-2 text-[12.5px] text-green">{notice}</p>}
      {error && <p role="alert" className="text-[12.5px] text-red">{error}</p>}

      {loading ? (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-4">
          {Array.from({ length: 8 }).map((_, index) => <Card key={index} padding="sm" className="space-y-2"><Skeleton className="h-24 w-full" /><Skeleton className="w-1/3" /><Skeleton className="w-2/3" /><Skeleton className="w-1/2" /></Card>)}
        </div>
      ) : data && !data.items.length ? (
        <Card padding="none"><p className="py-10 text-center text-[13px] text-fg-3">暂无表情，可以先添加图片</p></Card>
      ) : (
        <div className="stagger grid grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-4">
          {data?.items.map((item, index) => (
            <Card key={item.sticker_id} padding="sm" className="group min-w-0 space-y-2" style={{ ['--i' as string]: index }}>
              <StickerThumb item={item} />
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <p className="break-words text-[13.5px] font-medium text-fg">{item.name} · {item.enabled ? '已启用' : '已停用'}</p>
                  <p className="break-words text-[11.5px] text-fg-3">集合：{item.collection} · {item.kind === 'image' ? '图片' : `${item.adapter_type} 平台表情`}</p>
                </div>
                <Badge tone={item.enabled ? 'green' : 'neutral'} dot>{item.enabled ? '已启用' : '已停用'}</Badge>
              </div>
              <div className="flex flex-wrap gap-1.5">
                {item.tags.length ? item.tags.map((tag) => <Badge key={tag} tone="neutral">{tag}</Badge>) : <span className="text-[11px] text-fg-4">无标签</span>}
              </div>
              <div className="flex flex-wrap gap-2 border-t border-line pt-2.5">
                <Button size="sm" onClick={() => inspect(item)}>预览</Button>
                <Button size="sm" onClick={() => { intent.current = ''; setFormError(''); setDraft({ ...emptyDraft(), original: item, mode: item.kind, name: item.name, tags: item.tags.join(', '), collection: item.collection, enabled: item.enabled }); }}>编辑</Button>
                <Button size="sm" variant="danger" onClick={() => { deleteKey.current = createUuid(); setFormError(''); setDeleting(item); }}>删除</Button>
              </div>
            </Card>
          ))}
        </div>
      )}

      <div className="flex justify-between gap-2">
        <Button disabled={!cursors.length || loading} onClick={() => setCursors((values) => values.slice(0, -1))}>上一页</Button>
        <Button disabled={!data?.has_more || !data.next_cursor || loading} onClick={() => { if (data?.next_cursor) setCursors((values) => [...values, data.next_cursor!]); }}>下一页</Button>
      </div>

      <Modal open={!!draft} title={draft?.original ? '编辑表情' : draft?.mode === 'native' ? '添加平台表情' : '添加图片表情'} onClose={closeDraft}>
        {draft && <form className="space-y-4" onSubmit={(event) => { event.preventDefault(); void save(); }}>
          {!draft.original && draft.mode === 'image' && (
            <label className="block text-[13px] text-fg-2">图片（PNG / JPEG / WebP / GIF，最多 10 MiB）
              <input className="mt-2 block w-full text-[12px]" aria-label="表情图片" type="file" accept="image/png,image/jpeg,image/webp,image/gif" disabled={busy} onChange={(event) => change({ file: event.target.files?.[0] })} />
            </label>
          )}
          {!draft.original && draft.mode === 'native' && <>
            <label className="block text-[13px] text-fg-2">平台
              <KitSelect className="field mt-1 h-9 w-full" aria-label="表情来源平台" value={draft.platform} disabled={busy} onChange={(value) => change({ platform: value, nativeKey: '' })}>
                <option value="">请选择平台</option>
                {platforms.map((platform) => <option key={platform.id} value={platform.id}>{platform.id} · {platform.type}</option>)}
              </KitSelect>
            </label>
            <label className="block text-[13px] text-fg-2">平台表情
              <KitSelect className="field mt-1 h-9 w-full" aria-label="平台表情目录" value={draft.nativeKey} disabled={busy || nativeLoading} onChange={(value) => change({ nativeKey: value })}>
                <option value="">请选择已确认的表情</option>
                {native.map((item) => <option key={item.key} value={item.key}>{item.name}</option>)}
              </KitSelect>
            </label>
            {nativeLoading && <p role="status" className="text-[12px] text-fg-3">正在读取平台目录…</p>}
            {draft.platform && !nativeLoading && !native.length && <p className="text-[11.5px] text-fg-3">该平台暂无已确认的原生表情。可以先在群内发送平台表情后重新打开目录，或使用图片表情。</p>}
          </>}
          <label className="block text-[13px] text-fg-2">表情名称
            <input className="field mt-1 h-9 w-full" aria-label="表情名称" value={draft.name} maxLength={80} disabled={busy} onChange={(event) => change({ name: event.target.value })} />
          </label>
          <label className="block text-[13px] text-fg-2">所属集合
            <input className="field mt-1 h-9 w-full" aria-label="表情集合" value={draft.collection} maxLength={64} disabled={busy} onChange={(event) => change({ collection: event.target.value })} />
          </label>
          <label className="block text-[13px] text-fg-2">标签（用逗号分开，最多 12 个）
            <input className="field mt-1 h-9 w-full" aria-label="表情标签" value={draft.tags} disabled={busy} onChange={(event) => change({ tags: event.target.value })} />
          </label>
          {draft.original && (
            <label className="flex items-center gap-2 text-[13px] text-fg-2">
              <input type="checkbox" className="h-4 w-4 accent-[rgb(var(--rgb-teal))]" checked={draft.enabled} disabled={busy} onChange={(event) => change({ enabled: event.target.checked })} />启用此表情
            </label>
          )}
          {formError && <p role="alert" className="text-[12.5px] text-red">{formError}</p>}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" disabled={busy} onClick={closeDraft}>取消</Button>
            <TealPrimary type="submit" disabled={busy}>{busy ? '正在保存…' : '保存表情'}</TealPrimary>
          </div>
        </form>}
      </Modal>

      <ConfirmModal open={!!deleting} title="删除表情" busy={busy} error={formError}
        message={`删除「${deleting?.name}」后，所有群均无法再选择它。已经提交的平台消息不会撤回。`}
        confirmText={busy ? '正在删除…' : '确认删除表情'} onCancel={() => setDeleting(undefined)} onConfirm={() => void remove()} />

      <Modal open={!!preview} title={preview?.title || '表情预览'} onClose={() => { previewRequest.current += 1; setPreview(undefined); }}>
        {preview?.error ? <p role="alert" className="text-[12.5px] text-red">{preview.error}</p>
          : preview?.image ? <img src={preview.image} alt={preview.title} className="mx-auto max-h-64 max-w-full object-contain" />
            : preview?.image === null ? <p className="text-[13px] text-fg-2">平台原生表情没有本地图片预览</p>
              : <p role="status" className="text-[13px] text-fg-2">正在读取预览…</p>}
      </Modal>
    </section>
  );
}
