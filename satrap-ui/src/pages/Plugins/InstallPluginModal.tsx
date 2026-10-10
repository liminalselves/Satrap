import { useEffect, useRef, useState } from 'react';
import axios from 'axios';
import { controlApi } from '@/api/control';
import type { PluginInstallPreview } from '@/api/types';
import { Modal } from '@/components/ui/Modal';
import { Button } from '@/components/ui/Button';
import { PLUGIN_CAPABILITY_LABELS } from '@/components/common/PluginCapabilities';

export function pluginError(error: unknown): string {
  return axios.isAxiosError(error) ? error.response?.data?.error || error.response?.data?.detail || error.message : error instanceof Error ? error.message : '操作失败';
}

export function InstallPluginModal({ onClose, onInstalled }: { onClose: () => void; onInstalled: (name: string) => void }) {
  const [preview, setPreview] = useState<PluginInstallPreview>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [filename, setFilename] = useState('');
  const token = useRef('');
  const active = useRef(true);
  useEffect(() => {
    active.current = true;
    return () => {
      active.current = false;
      if (token.current) void controlApi.discardPluginPreview(token.current).catch(() => {});
    };
  }, []);

  const selectFile = async (file?: File) => {
    if (!file || busy) return;
    setError('');
    if (!file.name.toLowerCase().endsWith('.zip') || file.size === 0 || file.size > 20 * 1024 * 1024) {
      setError('请选择非空且不超过 20 MiB 的 ZIP 文件');
      return;
    }
    setBusy(true);
    setFilename(file.name);
    try {
      if (token.current) await controlApi.discardPluginPreview(token.current);
      token.current = '';
      setPreview(undefined);
      const result = await controlApi.previewPluginInstall(file);
      if (!active.current) {
        await controlApi.discardPluginPreview(result.token);
        return;
      }
      token.current = result.token;
      setPreview(result);
    } catch (err) { if (active.current) setError(pluginError(err)); }
    finally { if (active.current) setBusy(false); }
  };

  const install = async () => {
    if (!preview || busy) return;
    setBusy(true);
    setError('');
    try {
      const result = await controlApi.installPlugin(preview.token);
      token.current = '';
      if (active.current) onInstalled(result.plugin.name);
    } catch (err) {
      setError(pluginError(err));
      setPreview(undefined);
      // 失败后凭据不再使用, 重新上传可再次校验冲突与元数据
      if (token.current) void controlApi.discardPluginPreview(token.current).catch(() => {});
      token.current = '';
    } finally { if (active.current) setBusy(false); }
  };

  return <Modal open onClose={() => { if (!busy) onClose(); }} title="从 ZIP 安装插件" size="lg">
    <div className="space-y-4">
      <label className="block cursor-pointer rounded-lg border border-dashed border-glass-border p-6 text-center" onDragOver={(event) => event.preventDefault()} onDrop={(event) => {
        event.preventDefault();
        if (event.dataTransfer.files.length !== 1) { setError('每次请选择一个 ZIP 文件'); return; }
        void selectFile(event.dataTransfer.files[0]);
      }}>
        <span className="block">选择或拖入一个 ZIP 文件</span>
        <span className="mt-1 block text-xs text-text-tertiary">最大 20 MiB，根目录或单层目录包含 meta.yaml</span>
        <input aria-label="插件压缩包" type="file" accept=".zip,application/zip" disabled={busy} className="mt-3 max-w-full text-sm" onChange={(event) => { void selectFile(event.target.files?.[0]); event.target.value = ''; }} />
      </label>
      {filename && <p className="break-all text-sm text-text-secondary">{filename}</p>}
      {busy && <p role="status">{preview ? '正在安装…' : '正在校验压缩包…'}</p>}
      {error && <p role="alert" className="text-error">{error}</p>}
      {preview && <div className="space-y-3 rounded-lg bg-glass p-4">
        <h3 className="break-all font-semibold">{preview.plugin.name} · {preview.plugin.version || '未声明版本'}</h3>
        <p className="text-sm">作者：{preview.plugin.author || '未声明'}</p>
        <p className="whitespace-pre-wrap text-sm text-text-secondary">{preview.plugin.description || '暂无说明'}</p>
        <p className="text-sm">Satrap 版本要求：{preview.plugin.compatibility?.satrap || '未声明'}</p>
        <p className="text-sm">适用会话：{preview.plugin.applicability?.session_types?.join('、') || '未限制'}</p>
        <p className="text-sm">适用平台：{Array.isArray(preview.plugin.applicability?.platforms) ? preview.plugin.applicability.platforms.join('、') : '未限制'}</p>
        <p className="text-sm">能力：{Object.entries(preview.plugin.capabilities).filter(([, items]) => Object.keys(items).length).map(([kind, items]) => `${PLUGIN_CAPABILITY_LABELS[kind] || kind} (${Object.keys(items).length})`).join('、') || '未声明'}</p>
        <p className="text-xs text-text-tertiary">{preview.file_count} 个文件 · 解压后 {(preview.expanded_bytes / 1024).toFixed(1)} KiB</p>
        <p className="text-sm">安装到用户插件目录，安装后可选择使用位置并启用。预览有效期 10 分钟。</p>
      </div>}
      <div className="flex justify-end gap-2"><Button onClick={onClose} disabled={busy}>取消</Button><Button variant="primary" disabled={!preview || busy} onClick={() => void install()}>安装</Button></div>
    </div>
  </Modal>;
}
