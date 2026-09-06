import { useRef, useState } from 'react';
import { ragApi, ragErrorMessage, type KnowledgeBase, type RagContext, type UploadCapabilities } from '@/api/rag';
import { Button } from '@/components/ui/Button';
import { Modal } from '@/components/ui/Modal';

interface Props {
  context: RagContext;
  target: KnowledgeBase;
  capabilities: UploadCapabilities;
  onClose: () => void;
  onComplete: (result: Record<string, unknown>) => void;
}

interface UploadItem {
  id: number;
  file: File;
  source: string;
  status: 'pending' | 'invalid' | 'uploading' | 'indexed' | 'skipped' | 'failed';
  progress: number;
  error: string;
  chunks?: number;
}

export function RagImportModal({ context, target, capabilities, onClose, onComplete }: Props) {
  const fileInput = useRef<HTMLInputElement>(null);
  const nextId = useRef(0);
  const running = useRef(false);
  const [mode, setMode] = useState<'file' | 'text'>('file');
  const [items, setItems] = useState<UploadItem[]>([]);
  const [source, setSource] = useState('');
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState(0);
  const [error, setError] = useState('');
  const updateItem = (id: number, patch: Partial<UploadItem>) => setItems((current) => current.map((item) => item.id === id ? { ...item, ...patch } : item));
  const choose = (files: FileList | null) => {
    if (running.current || !files?.length) return;
    const selected = Array.from(files).map((file): UploadItem => {
      const ext = file.name.slice(file.name.lastIndexOf('.')).toLowerCase();
      let error = '';
      if (!capabilities.extensions.includes(ext)) error = '不支持此文件类型';
      else if (capabilities.missing_parsers[ext]) error = '缺少解析依赖 ' + capabilities.missing_parsers[ext] + '，请在后端安装后重试';
      else if (!file.size) error = '不能上传空文件';
      else if (file.size > capabilities.max_file_bytes) error = '文件超过 ' + capabilities.max_file_bytes / 1024 / 1024 + ' MiB';
      return { id: nextId.current++, file, source: file.name, status: error ? 'invalid' : 'pending', progress: 0, error };
    });
    setItems((current) => [...current, ...selected]);
    if (fileInput.current) fileInput.current.value = '';
  };
  const submit = async (retry = false) => {
    if (running.current) return;
    running.current = true;
    setBusy(true); setError(''); setProgress(0);
    try {
      if (mode === 'text') {
        const result = await ragApi.upload(context, target.id, new File([text], 'pasted-text.txt', { type: 'text/plain' }), source.trim(), setProgress);
        if (result.ok === false) throw new Error(String(result.error || '导入失败'));
        onComplete({ ...result, source: result.source || source.trim() });
        onClose();
      } else {
        const queue = items.filter((item) => item.status === (retry ? 'failed' : 'pending'));
        let indexed = 0, skipped = 0, failed = 0;
        for (const item of queue) {
          updateItem(item.id, { status: 'uploading', progress: 0, error: '' });
          try {
            const result = await ragApi.upload(context, target.id, item.file, item.source.trim(), (progress) => updateItem(item.id, { progress }));
            if (result.ok === false) throw new Error(String(result.error || '导入失败'));
            const status = result.status === 'skipped' ? 'skipped' : 'indexed';
            updateItem(item.id, { status, progress: 100, chunks: Number(result.chunks || 0) });
            if (status === 'skipped') skipped++; else indexed++;
          } catch (reason) {
            failed++;
            updateItem(item.id, { status: 'failed', error: ragErrorMessage(reason) });
          }
        }
        onComplete({ summary: '本次导入：成功 ' + indexed + '，跳过 ' + skipped + '，失败 ' + failed });
      }
    } catch (reason) {
      setError(ragErrorMessage(reason));
    } finally { running.current = false; setBusy(false); }
  };
  const pending = items.filter((item) => item.status === 'pending').length;
  const failed = items.filter((item) => item.status === 'failed').length;
  return <Modal open onClose={() => { if (!busy) onClose(); }} title="导入文档" size="lg">
    <div className="space-y-4">
      <p className="text-sm">目标知识库：<strong>{target.name}</strong> · {target.scope === 'global' ? '全局' : '会话 ' + target.session_id}</p>
      <div className="flex gap-2">
        <Button disabled={busy} variant={mode === 'file' ? 'primary' : 'ghost'} onClick={() => { setMode('file'); setError(''); }}>上传文件</Button>
        <Button disabled={busy} variant={mode === 'text' ? 'primary' : 'ghost'} onClick={() => { setMode('text'); setError(''); }}>粘贴文本</Button>
      </div>
      {mode === 'file' ? <div role="group" aria-label="文件拖放区域" className="space-y-3 rounded-xl border border-dashed border-glass-border p-4"
        onDragOver={(event) => event.preventDefault()}
        onDrop={(event) => { event.preventDefault(); choose(event.dataTransfer.files); }}>
        <p className="text-sm">可一次选择或拖入多个文件，按顺序导入；单个失败不影响其余文件</p>
        <input ref={fileInput} className="sr-only" aria-label="导入文件" type="file" multiple disabled={busy} accept={capabilities.extensions.join(',')} onChange={(event) => choose(event.target.files)} />
        <Button variant="subtle" disabled={busy} onClick={() => fileInput.current?.click()}>选择文件</Button>
        <p className="text-xs text-text-secondary break-words">{capabilities.extensions.join(' / ')}</p>
        <p className="text-xs text-text-secondary">每个文件最大 {capabilities.max_file_bytes / 1024 / 1024} MiB</p>
        <p className="text-xs text-text-secondary">文本使用 {capabilities.text_encoding} 编码；PDF 需包含文字，暂不支持扫描件 OCR。</p>
        <ul className="max-h-72 space-y-3 overflow-y-auto">{items.map((item) => <li key={item.id} className="space-y-1 border-t border-glass-border pt-2 text-sm">
          <div className="flex items-center justify-between gap-2">
            <span className="min-w-0 break-all">{item.file.name} · {(item.file.size / 1024).toFixed(1)} KiB</span>
            <Button variant="ghost" size="sm" disabled={busy} aria-label={'移除 ' + item.file.name} onClick={() => setItems((current) => current.filter((entry) => entry.id !== item.id))}>移除</Button>
          </div>
          <input aria-label={'来源：' + item.file.name} className="glass-input w-full" maxLength={1000} disabled={busy || item.status === 'indexed' || item.status === 'skipped' || item.status === 'invalid'}
            value={item.source} placeholder="来源名称（默认使用文件名）" onChange={(event) => updateItem(item.id, { source: event.target.value })} />
          <p className="text-xs text-text-secondary">{item.status === 'pending' ? '待导入' : item.status === 'uploading' ? item.progress < 100 ? '上传中 ' + item.progress + '%' : '正在解析文档并构建索引…' : item.status === 'indexed' ? '已导入 · ' + item.chunks + ' 块' : item.status === 'skipped' ? '已跳过重复来源' : item.status === 'invalid' ? '文件不可导入' : '导入失败'}</p>
          {item.error && <p role="alert" className="whitespace-pre-wrap break-words text-xs text-error">{item.error}</p>}
        </li>)}</ul>
        {!!items.length && <p role="status" className="text-xs">共 {items.length} 个 · 成功 {items.filter((item) => item.status === 'indexed').length} · 跳过 {items.filter((item) => item.status === 'skipped').length} · 失败 {failed} · 不可导入 {items.filter((item) => item.status === 'invalid').length} · 待导入 {pending}</p>}
      </div> : <textarea aria-label="导入文本" className="glass-input w-full" rows={8} disabled={busy} placeholder="粘贴需要入库的文本"
        value={text} onChange={(event) => setText(event.target.value)} />}
      {mode === 'text' && <input aria-label="文档来源" className="glass-input w-full" maxLength={1000} disabled={busy} placeholder="文档来源名称（必填）"
        value={source} onChange={(event) => setSource(event.target.value)} />}
      <p className="text-xs text-text-secondary">同名来源将{target.config.duplicate_policy === 'replace' ? '替换已有文档' : '跳过导入'}。文本上限 {capabilities.max_text_chars.toLocaleString()} 字符。</p>
      {error && <p role="alert" className="text-sm text-error">{error}</p>}
      {busy && mode === 'text' && <p role="status" className="text-sm">{progress < 100 ? '上传中 ' + progress + '%' : '正在解析文档并构建索引，请稍候…'}</p>}
      <div className="flex justify-end gap-2">
        <Button variant="ghost" disabled={busy} onClick={onClose}>关闭</Button>
        {mode === 'file' && failed > 0 && <Button variant="subtle" disabled={busy} onClick={() => void submit(true)}>重试失败文件</Button>}
        <Button disabled={busy || (mode === 'file' ? !pending : !text.trim() || !source.trim() || text.length > capabilities.max_text_chars)} onClick={() => void submit()}>导入到此库</Button>
      </div>
      {mode === 'text' && text.length > capabilities.max_text_chars && <p role="alert" className="text-sm text-error">文本超过字符上限，请拆分后导入</p>}
    </div>
  </Modal>;
}
