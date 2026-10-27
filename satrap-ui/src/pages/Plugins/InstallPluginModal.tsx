import { useEffect, useRef, useState } from 'react';
import axios from 'axios';
import { AlertTriangle, CheckCircle2, FileArchive, Loader2, Package, ShieldAlert, Upload } from 'lucide-react';
import { errorText } from '@/utils/errorText';
import { controlApi } from '@/api/control';
import type { EdictumAvailablePlugin, PluginInstallPreview } from '@/api/types';
import { Badge, Button, Modal } from '@/components/kit';
import { PLUGIN_CAPABILITY_LABELS } from '@/components/common/PluginCapabilities';
import { cn } from '@/utils/cn';
import { CapabilityChips, TealPrimary } from './PluginBits';
import { capabilityGroups, externalUrl, platformText, sessionText } from './pluginModel';

export function pluginError(error: unknown): string {
  return errorText(error);
}

function installError(error: unknown): string {
  if (axios.isAxiosError(error)) {
    const data = error.response?.data as { code?: string; installed_version?: string; error?: string } | undefined;
    if (error.response?.status === 409 && data?.code === 'plugin_already_installed') {
      return `插件已安装${data.installed_version ? `（版本 ${data.installed_version}）` : ''}，不能重复安装。请先卸载现有版本后再试。`;
    }
  }
  return pluginError(error);
}

function ExternalLink({ value }: { value?: string }) {
  const href = externalUrl(value);
  if (!href) return null;
  return (
    <a href={href} target="_blank" rel="noopener noreferrer" className="break-all text-teal transition hover:underline">
      {value}
    </a>
  );
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
      setError(installError(err));
      setPreview(undefined);
      // 失败后凭据不再使用, 重新上传可再次校验冲突与元数据
      if (token.current) void controlApi.discardPluginPreview(token.current).catch(() => {});
      token.current = '';
    } finally { if (active.current) setBusy(false); }
  };

  const plugin = preview?.plugin as (EdictumAvailablePlugin & {
    display_name?: string; license?: string; homepage?: string; repository?: string;
  } | undefined);
  const caps = Object.fromEntries(capabilityGroups(plugin?.capabilities ?? {}).map((group) => [group.kind, group.items.length])) as Record<string, number>;

  return (
    <Modal
      open
      onClose={() => { if (!busy) onClose(); }}
      title="从 ZIP 安装插件"
      description="上传压缩包后先校验 meta.yaml 与安装冲突，再写入用户插件目录。"
      icon={<Package className="h-[18px] w-[18px]" />}
      size="lg"
      className="tint-teal"
      footer={<>
        <span className="mr-auto hidden text-[11px] text-fg-4 sm:block">最大 20 MiB · 预览凭据 10 分钟内有效</span>
        <Button variant="ghost" disabled={busy} onClick={onClose}>取消</Button>
        <TealPrimary icon={<CheckCircle2 className="h-4 w-4" />} loading={busy && Boolean(preview)} disabled={!preview || busy} onClick={() => void install()}>安装</TealPrimary>
      </>}
    >
      <div className="space-y-4">
        <label
          className={cn(
            'group flex cursor-pointer flex-col items-center rounded-2xl border-2 border-dashed px-6 py-8 text-center transition-colors',
            filename ? 'border-[rgb(var(--rgb-teal)/0.4)] bg-[rgb(var(--rgb-teal)/0.06)]' : 'border-[var(--glass-border-strong)] hover:border-[rgb(var(--rgb-teal)/0.5)] hover:bg-[rgb(var(--rgb-teal)/0.04)]',
          )}
          onDragOver={(event) => event.preventDefault()}
          onDrop={(event) => {
            event.preventDefault();
            if (busy) return;
            if (event.dataTransfer.files.length !== 1) { setError('每次请选择一个 ZIP 文件'); return; }
            void selectFile(event.dataTransfer.files[0]);
          }}
        >
          <span className="grid h-12 w-12 place-items-center rounded-2xl border border-[rgb(var(--rgb-teal)/0.25)] bg-[rgb(var(--rgb-teal)/0.12)] text-teal transition-transform duration-300 group-hover:-translate-y-0.5">
            {filename ? <FileArchive className="h-5 w-5" /> : <Upload className="h-5 w-5" />}
          </span>
          <span className="mt-3 text-sm font-medium text-fg">{filename || '拖入或选择一个 ZIP 文件'}</span>
          <span className="mt-1 text-[12px] text-fg-3">根目录或单层目录包含 meta.yaml</span>
          <input
            aria-label="插件压缩包"
            type="file"
            accept=".zip,application/zip"
            disabled={busy}
            className="mt-3 max-w-full text-[12px] text-fg-3 file:mr-3 file:rounded-lg file:border-0 file:bg-[rgb(var(--rgb-teal)/0.14)] file:px-3 file:py-1.5 file:text-[12px] file:font-medium file:text-teal"
            onChange={(event) => {
              const file = event.target.files?.[0];
              event.target.value = '';
              void selectFile(file);
            }}
          />
        </label>

        {busy && <p role="status" className="flex items-center gap-2 text-[12.5px] text-fg-2"><Loader2 className="h-3.5 w-3.5 animate-spin-slow text-teal" />{preview ? '正在安装…' : '正在校验压缩包…'}</p>}
        {error && (
          <div role="alert" className="tint-red flex items-start gap-2 rounded-xl border border-[rgb(var(--tint)/0.25)] bg-[rgb(var(--tint)/0.08)] px-3.5 py-2.5 text-[13px] leading-5 text-fg-2">
            <AlertTriangle className="mt-0.5 h-4 w-4 flex-none text-red" />
            <div className="min-w-0 break-words">{error}</div>
          </div>
        )}

        {plugin && preview && (
          <div className="space-y-3 rounded-2xl border border-line bg-glass p-4">
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="break-all text-base font-semibold text-fg">{plugin.display_name?.trim() || plugin.name}</h3>
              <Badge tone="teal" className="font-mono">v{plugin.version || '未声明版本'}</Badge>
            </div>
            <p className="break-all font-mono text-[11.5px] text-fg-4">{plugin.name}</p>
            <p className="text-[12.5px] text-fg-3">作者 {plugin.author || '未声明'} · 要求 Satrap {plugin.compatibility?.satrap || '未声明'}</p>
            <p className="whitespace-pre-wrap text-[13px] leading-5 text-fg-2">{plugin.description || '暂无说明'}</p>
            <div className="grid grid-cols-2 gap-2 text-[12px] sm:grid-cols-3">
              <div className="rounded-lg border border-line bg-glass px-2.5 py-1.5">
                <div className="text-[10.5px] text-fg-4">适用会话</div>
                <div className="mt-0.5 text-fg-2">{sessionText(plugin.applicability)}</div>
              </div>
              <div className="rounded-lg border border-line bg-glass px-2.5 py-1.5">
                <div className="text-[10.5px] text-fg-4">适用平台</div>
                <div className="mt-0.5 truncate text-fg-2">{platformText(plugin.applicability)}</div>
              </div>
              <div className="rounded-lg border border-line bg-glass px-2.5 py-1.5">
                <div className="text-[10.5px] text-fg-4">文件规模</div>
                <div className="num mt-0.5 text-fg-2">{preview.file_count} 个文件 · {(preview.expanded_bytes / 1024).toFixed(1)} KiB</div>
              </div>
            </div>
            <div className="flex flex-wrap gap-1.5">
              {Object.entries(caps).map(([kind, count]) => (
                <Badge key={kind} tone="neutral">{PLUGIN_CAPABILITY_LABELS[kind] || kind} ({count})</Badge>
              ))}
              {!Object.keys(caps).length && <Badge tone="neutral">未声明能力</Badge>}
            </div>
            {(plugin.license || plugin.homepage || plugin.repository) && (
              <div className="space-y-1 border-t border-line pt-2.5 text-[11.5px] text-fg-3">
                {plugin.license && <p>许可证 <span className="text-fg-2">{plugin.license}</span></p>}
                {plugin.homepage && <p className="break-all">主页 <ExternalLink value={plugin.homepage} /> {!externalUrl(plugin.homepage) && <span>{plugin.homepage}</span>}</p>}
                {plugin.repository && <p className="break-all">仓库 <ExternalLink value={plugin.repository} /> {!externalUrl(plugin.repository) && <span>{plugin.repository}</span>}</p>}
              </div>
            )}
            <p className="flex items-start gap-2 text-[11.5px] leading-5 text-fg-3">
              <ShieldAlert className="mt-0.5 h-3.5 w-3.5 flex-none text-fg-4" />
              安装到用户插件目录，安装后可选择使用位置并启用。当前控制服务仅支持 ZIP 压缩包安装。
            </p>
          </div>
        )}
        {!preview && !busy && (
          <div className="flex items-start gap-2 rounded-xl border border-line bg-glass px-3.5 py-2.5 text-[11.5px] leading-5 text-fg-3">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 flex-none text-fg-4" />
            若同名插件已安装，控制服务会返回安装冲突及现有版本，不会覆盖现有插件。
          </div>
        )}
        {plugin && <CapabilityChips caps={(plugin.capabilities ?? {}) as Record<string, Record<string, string>>} max={6} />}
      </div>
    </Modal>
  );
}
