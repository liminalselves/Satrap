import { useEffect, useState } from 'react';
import { skillsApi, type ManagedSkill, type SkillDraft } from '@/api/skills';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { Modal } from '@/components/ui/Modal';
import { useConfirm } from '@/hooks/useConfirm';
import { confirmDiscard } from '@/hooks/useDirtyGuard';
import { errorText } from '@/utils/errorText';

type Draft = SkillDraft & { original?: ManagedSkill; mode: 'text' | 'url' };
const sessionTypes = [['chat', 'Chat'], ['platform', '平台会话'], ['embedded', '嵌入会话']];

export function SkillLibrary({ onDirty }: { onDirty: (dirty: boolean) => void }) {
  const confirm = useConfirm();
  const [items, setItems] = useState<ManagedSkill[]>([]);
  const [revision, setRevision] = useState(0);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [draft, setDraft] = useState<Draft>();
  const [formError, setFormError] = useState('');
  useEffect(() => { onDirty(!!draft); return () => onDirty(false); }, [draft, onDirty]);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError('');
    skillsApi.list(controller.signal).then((data) => { if (!controller.signal.aborted) setItems(data); })
      .catch((error) => { if (!controller.signal.aborted) setError(errorText(error)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [revision]);
  const close = () => { if (!busy && confirmDiscard()) { setDraft(undefined); setFormError(''); } };
  const save = async () => {
    if (!draft || busy) return;
    setBusy(true); setFormError('');
    try {
      const payload: SkillDraft = { name: draft.name, description: draft.description, enabled: draft.enabled, scopes: draft.scopes,
        ...(draft.mode === 'url' ? { url: draft.url } : { markdown: draft.markdown, filename: draft.filename || draft.original?.source }),
        ...(draft.original ? { expected_revision: draft.original.revision } : {}) };
      if (draft.original) await skillsApi.update(draft.original.name, payload);
      else await skillsApi.create(payload);
      setDraft(undefined); setRevision((value) => value + 1);
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
  return <section className="space-y-4" aria-label="技能库">
    <div className="flex flex-wrap items-center justify-between gap-3">
      <p className="text-sm text-text-secondary">导入 Markdown 后，在使用位置启用 skills 插件。Agent 会按任务读取说明，也可使用 /skill use 强制加载。</p>
      <div className="flex gap-2"><Button disabled={busy || loading} onClick={() => setRevision((value) => value + 1)}>刷新技能</Button>
        <Button variant="primary" disabled={busy} onClick={() => { setFormError(''); setDraft({ mode: 'text', name: '', description: '', markdown: '', enabled: true, scopes: sessionTypes.map(([value]) => value) }); }}>导入技能</Button></div>
    </div>
    {error && <p role="alert" className="text-error">{error}</p>}
    {loading ? <p role="status">正在读取技能…</p> : !items.length ? <p className="text-text-secondary">尚未导入外部技能</p> : <div className="grid gap-3 lg:grid-cols-2">{items.map((item) => <Card key={item.name} className="space-y-3">
      <div className="flex justify-between gap-2"><h2 className="break-all font-medium">{item.name}</h2><span>{item.enabled ? '已启用' : '已停用'}</span></div>
      <p>{item.description}</p><p className="break-all text-xs text-text-tertiary">来源: {item.source} · 版本 {item.revision}</p>
      <p className="text-xs text-text-secondary">标识: user.{item.name} · {sessionTypes.filter(([value]) => item.scopes.includes(value)).map(([, label]) => label).join('、')}</p>
      <div className="flex gap-2"><Button disabled={busy} onClick={() => { setFormError(''); setDraft({ ...item, original: item, mode: 'text' }); }}>查看和编辑</Button><Button variant="danger" disabled={busy} onClick={() => remove(item)}>删除</Button></div>
    </Card>)}</div>}
    <Modal open={!!draft} onClose={close} title={draft?.original ? '编辑技能' : '导入技能'} size="xl">
      {draft && <form className="space-y-4" onSubmit={(event) => { event.preventDefault(); void save(); }}>
        <p className="text-sm text-text-secondary">名称和简介可从 front matter 读取；普通 Markdown 请在这里补充。单个文件上限 256 KiB。</p>
        <label className="block">技能名称<input className="glass-input mt-1 w-full" value={draft.name || ''} disabled={busy || !!draft.original} onChange={(event) => setDraft({ ...draft, name: event.target.value })} /></label>
        <label className="block">用途简介<input className="glass-input mt-1 w-full" maxLength={512} value={draft.description || ''} disabled={busy} onChange={(event) => setDraft({ ...draft, description: event.target.value })} /></label>
        {!draft.original && <label className="block">导入方式<select aria-label="导入方式" className="glass-input ml-2" disabled={busy} value={draft.mode} onChange={(event) => setDraft({ ...draft, mode: event.target.value as Draft['mode'] })}><option value="text">上传文件或粘贴 Markdown</option><option value="url">HTTPS Markdown 地址</option></select></label>}
        {draft.mode === 'url' ? <label className="block">Markdown 地址<input className="glass-input mt-1 w-full" type="url" required disabled={busy} value={draft.url || ''} onChange={(event) => setDraft({ ...draft, url: event.target.value })} /></label> : <>
          {!draft.original && <label className="block">Markdown 文件<input type="file" accept=".md,.markdown" disabled={busy} onChange={async (event) => {
            const file = event.target.files?.[0]; if (!file) return;
            try {
              if (file.size > 256 * 1024 || !/\.(md|markdown)$/i.test(file.name)) throw new Error('请选择不超过 256 KiB 的 Markdown 文件');
              const markdown = new TextDecoder('utf-8', { fatal: true }).decode(await file.arrayBuffer());
              setDraft((current) => current ? { ...current, markdown, filename: file.name } : current);
              setFormError('');
            } catch (error) { setFormError(errorText(error)); }
          }} /></label>}
          <label className="block">Markdown 正文<textarea aria-label="Markdown 正文" className="glass-input mt-1 min-h-64 w-full font-mono" required disabled={busy} value={draft.markdown || ''} onChange={(event) => setDraft({ ...draft, markdown: event.target.value })} /></label>
        </>}
        <label className="flex items-center gap-2"><input type="checkbox" disabled={busy} checked={draft.enabled ?? true} onChange={(event) => setDraft({ ...draft, enabled: event.target.checked })} />允许使用此技能</label>
        <fieldset disabled={busy} className="flex flex-wrap gap-3"><legend>适用会话</legend>{sessionTypes.map(([value, label]) => <label key={value} className="flex gap-1"><input type="checkbox" checked={draft.scopes?.includes(value) || false} onChange={(event) => setDraft({ ...draft, scopes: event.target.checked ? [...(draft.scopes || []), value] : draft.scopes?.filter((scope) => scope !== value) })} />{label}</label>)}</fieldset>
        {formError && <p role="alert" className="text-error">{formError}</p>}
        <div className="flex justify-end gap-2"><Button type="button" disabled={busy} onClick={close}>取消</Button><Button type="submit" variant="primary" disabled={busy || !draft.scopes?.length}>{busy ? '正在保存…' : '保存技能'}</Button></div>
      </form>}
    </Modal>
  </section>;
}
