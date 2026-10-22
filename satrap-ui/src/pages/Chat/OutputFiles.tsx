import { useState } from 'react';
import { Download } from 'lucide-react';
import { chatApi, type OutputFile } from '@/api/chat';
import { formatBytes } from '@/utils/format';

export function OutputFiles({ conversationId, files }: { conversationId: string; files: OutputFile[] }) {
  const [error, setError] = useState('');
  const [busy, setBusy] = useState('');
  const download = async (file: OutputFile) => {
    setError('');
    setBusy(file.file_id);
    try { await chatApi.downloadFile(conversationId, file); }
    catch (reason) { setError(reason instanceof Error ? reason.message : '下载失败'); }
    finally { setBusy(''); }
  };
  return <div className="my-2 space-y-2">
    {files.map((file) => <button key={file.file_id} type="button" disabled={Boolean(busy)} onClick={() => void download(file)}
      className="glass-card flex max-w-full items-center gap-2 rounded-md px-3 py-2 text-left text-sm" aria-label={`下载 ${file.name}`}>
      <Download className="h-4 w-4 shrink-0" />
      <span className="min-w-0 break-all">{file.name}</span>
      <span className="shrink-0 text-xs text-text-tertiary">{busy === file.file_id ? '下载中…' : formatBytes(file.size)}</span>
    </button>)}
    {error && <p role="alert" className="text-sm text-red">{error}</p>}
  </div>;
}
