import { useEffect, useMemo, useRef, useState } from 'react';
import axios from 'axios';
import { Link } from 'react-router-dom';
import { controlApi } from '@/api/control';
import type { GroupChatStickerSettings, PlatformArchiveRecord } from '@/api/types';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { confirmDiscard, useDirtyGuard } from '@/hooks/useDirtyGuard';

const errorText = (error: unknown) => axios.isAxiosError<{ error?: string }>(error) ? error.response?.data?.error || error.message : error instanceof Error ? error.message : String(error);

export function StickerSettings({ record, onDirty }: { record: PlatformArchiveRecord; onDirty: (value: boolean) => void }) {
  const identity = useMemo(() => ({ platform_id: record.platform_id, self_id: record.self_id, conversation_kind: record.conversation_kind, chat_id: record.chat_id }), [record.platform_id, record.self_id, record.conversation_kind, record.chat_id]);
  const [data, setData] = useState<GroupChatStickerSettings>();
  const [selected, setSelected] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [refresh, setRefresh] = useState(0);
  const intent = useRef('');
  const mounted = useRef(true);
  const dirty = !!data && JSON.stringify([...selected].sort()) !== JSON.stringify([...data.collections].sort());
  useDirtyGuard(dirty);
  useEffect(() => { onDirty(dirty); return () => onDirty(false); }, [dirty, onDirty]);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => {
    let disposed = false;
    setLoading(true); setError('');
    controlApi.groupStickerSettings(identity).then((result) => { if (!disposed) { setData(result); setSelected(result.collections); } })
      .catch((error) => { if (!disposed) setError(errorText(error)); }).finally(() => { if (!disposed) setLoading(false); });
    return () => { disposed = true; };
  }, [identity, refresh]);
  const save = async () => {
    if (!data || busy || loading) return;
    setBusy(true); setError(''); setNotice('');
    try {
      const result = await controlApi.saveGroupStickerSettings(record, selected, data.revision, intent.current || (intent.current = crypto.randomUUID()));
      if (mounted.current) { setData(result); setSelected(result.collections); setNotice('此对话的表情集合已保存，后续查询立即使用新设置'); }
    } catch (error) { if (mounted.current) setError(`${errorText(error)}；选择已保留，刷新会重新读取已保存设置`); }
    finally { if (mounted.current) setBusy(false); }
  };
  return <Card className="min-w-0 space-y-4">
    <p className="break-all font-medium">{record.label || record.chat_id} · 表情设置</p>
    <p className="break-all text-xs text-text-tertiary">{record.platform_id} / {record.self_id} / {record.conversation_kind_label} / {record.chat_id}</p>
    <p className="text-sm text-text-secondary">选择允许机器人在此对话使用的表情集合。默认不启用。只有与平台兼容的表情会提供给模型，平台不支持时不会发送。</p>
    <Link className="text-sm text-accent" to="/plugins/group_chat?tab=stickers">管理表情库</Link>
    {loading && <p role="status">正在读取表情集合…</p>}{error && <p role="alert" className="text-error">{error}</p>}{notice && <p role="status" className="text-success">{notice}</p>}
    <div className="space-y-2">{data?.available_collections.map((name) => <label key={name} className="flex items-center gap-2 break-words rounded-lg bg-glass p-3 text-sm"><input type="checkbox" aria-label={`启用表情集合 ${name}`} checked={selected.includes(name)} disabled={busy || loading} onChange={(event) => { intent.current = ''; setNotice(''); setSelected((values) => event.target.checked ? [...values, name] : values.filter((value) => value !== name)); }} />{name}</label>)}</div>
    {data && !data.available_collections.length && <p className="text-sm text-text-secondary">表情库暂无集合，请先添加表情</p>}
    <div className="flex flex-wrap gap-2"><Button disabled={!data || busy || loading || !dirty} onClick={save}>{busy ? '正在保存…' : '保存群表情设置'}</Button><Button disabled={busy || loading} onClick={() => { if (dirty && !confirmDiscard()) return; intent.current = ''; setNotice(''); setRefresh((value) => value + 1); }}>重新读取设置</Button></div>
  </Card>;
}
