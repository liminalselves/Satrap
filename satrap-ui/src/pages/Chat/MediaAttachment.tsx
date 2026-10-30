import { useEffect, useRef, useState } from 'react';
import { FileText, X } from 'lucide-react';
import { chatApi, type Attachment } from '@/api/chat';

export function MediaAttachment({ attachment, conversationId, onRemove }: {
  attachment: Attachment;
  conversationId: string;
  onRemove?: () => void;
}) {
  const [source, setSource] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const revision = useRef(0);
  const pending = useRef<AbortController | null>(null);
  const isVideo = attachment.type.startsWith('video/');
  const isMedia = isVideo || attachment.type.startsWith('image/');

  useEffect(() => {
    revision.current += 1;
    setSource('');
    setError('');
    setLoading(false);
    return () => { revision.current += 1; pending.current?.abort(); pending.current = null; };
  }, [conversationId, attachment.url]);

  async function preview() {
    if (pending.current) return;
    if (source) { setSource(''); return; }
    const current = revision.current;
    const controller = new AbortController();
    pending.current = controller;
    setLoading(true);
    setError('');
    try {
      const result = await chatApi.previewMedia(conversationId, attachment.url, controller.signal);
      if (current === revision.current) setSource(result.data_url);
    } catch (failure) {
      if (current === revision.current) setError(failure instanceof Error ? failure.message : '预览失败');
    } finally {
      if (pending.current === controller) pending.current = null;
      if (current === revision.current) setLoading(false);
    }
  }

  return <div className="surface max-w-full space-y-2 rounded-xl px-2.5 py-1.5 text-xs">
    <div className="flex items-center gap-2">
      <FileText className="h-3 w-3 flex-none text-fg-4" />
      <span className="max-w-[200px] truncate text-fg" title={attachment.name}>{attachment.name}</span>
      {isMedia && <button type="button" onClick={() => void preview()} disabled={loading || !conversationId}
        className="rounded-md px-1.5 py-0.5 font-medium text-accent transition-colors hover:bg-glass-hover disabled:opacity-50">{loading ? '加载中…' : source ? '收起' : '预览'}</button>}
      {onRemove && <button type="button" onClick={onRemove} aria-label={`移除 ${attachment.name}`}
        className="rounded-md p-0.5 text-fg-4 transition-colors hover:bg-glass-hover hover:text-red"><X className="h-3 w-3" /></button>}
    </div>
    {error && <p role="alert" className="text-red">{error}</p>}
    {source && (isVideo
      ? <video src={source} controls preload="metadata" aria-label={attachment.name}
        onError={() => setError('无法播放视频, 文件可能损坏或浏览器不支持此编码')} className="max-h-72 max-w-full rounded-lg" />
      : <img src={source} alt={attachment.name} onError={() => setError('无法显示图片, 请检查文件格式')}
        className="max-h-72 max-w-full rounded-lg object-contain" />)}
  </div>;
}
