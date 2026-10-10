import { useEffect, useRef, useState } from 'react';
import axios from 'axios';
import { controlApi } from '@/api/control';
import type { GroupChatSticker, GroupChatStickerPage, PlatformConfig } from '@/api/types';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { Modal } from '@/components/ui/Modal';
import { confirmDiscard } from '@/hooks/useDirtyGuard';

const errorText = (error: unknown) => axios.isAxiosError<{ error?: string }>(error) ? error.response?.data?.error || error.message : error instanceof Error ? error.message : String(error);
type Draft = { original?: GroupChatSticker; mode: 'image' | 'native'; name: string; tags: string; collection: string; enabled: boolean; platform: string; nativeKey: string; file?: File };
const emptyDraft = (): Draft => ({ mode: 'image', name: '', tags: '', collection: '常用', enabled: true, platform: '', nativeKey: '' });

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
      const metadata = { name: draft.name.trim(), tags, collection: draft.collection.trim(), idempotency_key: intent.current || (intent.current = crypto.randomUUID()) };
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
  return <Card className="min-w-0 space-y-4">
    <p className="text-sm text-text-secondary">上传图片并填写名称和标签。每个群单独启用集合，默认没有可用的用户表情。原生表情由所选平台的已确认目录提供。</p>
    <div className="flex flex-wrap gap-2"><Button onClick={() => { intent.current = ''; setFormError(''); setDraft(emptyDraft()); }}>添加图片表情</Button><Button onClick={() => { intent.current = ''; setFormError(''); setDraft({ ...emptyDraft(), mode: 'native' }); }}>添加平台表情</Button></div>
    <form className="flex flex-wrap gap-2" onSubmit={(event) => { event.preventDefault(); setQuery(keyword); setCursors([]); setRefresh((value) => value + 1); }}>
      <input className="glass-input min-w-0 flex-1" aria-label="表情名称或标签" placeholder="搜索名称或标签" value={keyword} onChange={(event) => setKeyword(event.target.value)} />
      <Button type="submit" disabled={loading}>搜索表情</Button><Button type="button" onClick={() => setRefresh((value) => value + 1)} disabled={loading}>刷新表情</Button>
    </form>
    {loading && <p role="status">正在读取表情…</p>}{error && <p role="alert" className="text-error">{error}</p>}{notice && <p role="status" className="text-success">{notice}</p>}
    {data && !data.items.length && <p className="text-sm text-text-secondary">暂无表情，可以先添加图片</p>}
    <div className="grid gap-3 sm:grid-cols-2">{data?.items.map((item) => <div key={item.sticker_id} className="min-w-0 space-y-2 rounded-lg bg-glass p-3">
      <p className="break-words font-medium">{item.name} · {item.enabled ? '已启用' : '已停用'}</p>
      <p className="break-words text-xs text-text-secondary">集合：{item.collection} · {item.kind === 'image' ? '图片' : `${item.adapter_type} 平台表情`}</p>
      <p className="break-words text-xs">标签：{item.tags.join('、') || '无'}</p>
      <div className="flex flex-wrap gap-2"><Button size="sm" onClick={() => inspect(item)}>预览</Button><Button size="sm" onClick={() => { intent.current = ''; setFormError(''); setDraft({ ...emptyDraft(), original: item, mode: item.kind, name: item.name, tags: item.tags.join(', '), collection: item.collection, enabled: item.enabled }); }}>编辑</Button><Button size="sm" variant="danger" onClick={() => { deleteKey.current = crypto.randomUUID(); setFormError(''); setDeleting(item); }}>删除</Button></div>
    </div>)}</div>
    <div className="flex justify-between gap-2"><Button disabled={!cursors.length || loading} onClick={() => setCursors((values) => values.slice(0, -1))}>上一页</Button><Button disabled={!data?.has_more || !data.next_cursor || loading} onClick={() => { if (data?.next_cursor) setCursors((values) => [...values, data.next_cursor!]); }}>下一页</Button></div>
    <Modal open={!!draft} title={draft?.original ? '编辑表情' : draft?.mode === 'native' ? '添加平台表情' : '添加图片表情'} onClose={closeDraft}>
      {draft && <form className="space-y-4" onSubmit={(event) => { event.preventDefault(); void save(); }}>
        {!draft.original && draft.mode === 'image' && <label className="block text-sm">图片（PNG / JPEG / WebP / GIF，最多 10 MiB）<input className="mt-2 block w-full text-sm" aria-label="表情图片" type="file" accept="image/png,image/jpeg,image/webp,image/gif" disabled={busy} onChange={(event) => change({ file: event.target.files?.[0] })} /></label>}
        {!draft.original && draft.mode === 'native' && <>
          <label className="block text-sm">平台<select className="glass-input mt-1 w-full" aria-label="表情来源平台" value={draft.platform} disabled={busy} onChange={(event) => change({ platform: event.target.value, nativeKey: '' })}><option value="">请选择平台</option>{platforms.map((platform) => <option key={platform.id} value={platform.id}>{platform.id} · {platform.type}</option>)}</select></label>
          <label className="block text-sm">平台表情<select className="glass-input mt-1 w-full" aria-label="平台表情目录" value={draft.nativeKey} disabled={busy || nativeLoading} onChange={(event) => change({ nativeKey: event.target.value })}><option value="">请选择已确认的表情</option>{native.map((item) => <option key={item.key} value={item.key}>{item.name}</option>)}</select></label>
          {nativeLoading && <p role="status">正在读取平台目录…</p>}{draft.platform && !nativeLoading && !native.length && <p className="text-xs text-text-secondary">该平台暂无已确认的原生表情。可以先在群内发送平台表情后重新打开目录，或使用图片表情。</p>}
        </>}
        <label className="block text-sm">表情名称<input className="glass-input mt-1 w-full" aria-label="表情名称" value={draft.name} maxLength={80} disabled={busy} onChange={(event) => change({ name: event.target.value })} /></label>
        <label className="block text-sm">所属集合<input className="glass-input mt-1 w-full" aria-label="表情集合" value={draft.collection} maxLength={64} disabled={busy} onChange={(event) => change({ collection: event.target.value })} /></label>
        <label className="block text-sm">标签（用逗号分开，最多 12 个）<input className="glass-input mt-1 w-full" aria-label="表情标签" value={draft.tags} disabled={busy} onChange={(event) => change({ tags: event.target.value })} /></label>
        {draft.original && <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={draft.enabled} disabled={busy} onChange={(event) => change({ enabled: event.target.checked })} />启用此表情</label>}
        {formError && <p role="alert" className="text-error">{formError}</p>}
        <div className="flex justify-end gap-2"><Button type="button" disabled={busy} onClick={closeDraft}>取消</Button><Button type="submit" disabled={busy}>{busy ? '正在保存…' : '保存表情'}</Button></div>
      </form>}
    </Modal>
    <Modal open={!!deleting} title="删除表情" onClose={() => { if (!busy) setDeleting(undefined); }}><div className="space-y-4"><p className="break-words">删除「{deleting?.name}」后，所有群均无法再选择它。已经提交的平台消息不会撤回。</p>{formError && <p role="alert" className="text-error">{formError}</p>}<div className="flex justify-end gap-2"><Button disabled={busy} onClick={() => setDeleting(undefined)}>取消</Button><Button disabled={busy} variant="danger" onClick={remove}>{busy ? '正在删除…' : '确认删除表情'}</Button></div></div></Modal>
    <Modal open={!!preview} title={preview?.title || '表情预览'} onClose={() => { previewRequest.current += 1; setPreview(undefined); }}>
      {preview?.error ? <p role="alert" className="text-error">{preview.error}</p> : preview?.image ? <img src={preview.image} alt={preview.title} className="mx-auto max-h-64 max-w-full object-contain" /> : preview?.image === null ? <p>平台原生表情没有本地图片预览</p> : <p role="status">正在读取预览…</p>}
    </Modal>
  </Card>;
}
