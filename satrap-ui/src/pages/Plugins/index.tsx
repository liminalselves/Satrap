import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { RefreshCw } from 'lucide-react';
import { controlApi } from '@/api/control';
import type { ManagedPlugin } from '@/api/types';
import { PageHeader } from '@/components/common/PageHeader';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';

export const capabilityLabels: Record<string, string> = {
  tools: '工具', skills: '技能', mcp: 'MCP', handlers: '前处理', commands: '命令',
};
const sourceLabels = { builtin: '内置', user: '用户' };
const sessionLabels: Record<string, string> = { chat: 'Chat', platform: '平台会话', embedded: '嵌入会话' };

function usePluginCatalog() {
  const [plugins, setPlugins] = useState<ManagedPlugin[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [revision, setRevision] = useState(0);
  const refresh = useCallback(() => setRevision((value) => value + 1), []);
  useEffect(() => {
    let disposed = false;
    setLoading(true);
    setError('');
    controlApi.listPlugins().then((data) => { if (!disposed) setPlugins(data); })
      .catch((err: unknown) => { if (!disposed) setError(err instanceof Error ? err.message : '读取插件失败'); })
      .finally(() => { if (!disposed) setLoading(false); });
    return () => { disposed = true; };
  }, [revision]);
  return { plugins, loading, error, refresh };
}

export function PluginCapabilities({ plugin }: { plugin: Pick<ManagedPlugin, 'capabilities'> }) {
  return <div className="space-y-4">{Object.entries(capabilityLabels).map(([kind, label]) => {
    const items = Object.entries(plugin.capabilities[kind] || {});
    return <section key={kind}>
      <h3 className="font-medium text-text-primary">{label} · {items.length}</h3>
      {items.length ? <ul className="mt-2 space-y-2">{items.map(([name, description]) => <li key={name} className="rounded-lg bg-glass p-3">
        <span className="break-all font-medium">{name}</span>
        {description && <p className="mt-1 whitespace-pre-wrap text-sm text-text-secondary">{description}</p>}
      </li>)}</ul> : <p className="mt-1 text-sm text-text-tertiary">未声明</p>}
    </section>;
  })}</div>;
}

export function PluginOverview({ plugin }: { plugin: ManagedPlugin }) {
  return <div className="grid gap-4 lg:grid-cols-2">
    <Card className="space-y-4">
      <h2 className="text-lg font-semibold">插件信息</h2>
      <dl className="space-y-3 text-sm">
        <div><dt className="text-text-tertiary">作者</dt><dd>{plugin.author || '未声明'}</dd></div>
        <div><dt className="text-text-tertiary">版本 / 来源</dt><dd>{plugin.version || '未声明'} · {sourceLabels[plugin.source]}</dd></div>
        <div><dt className="text-text-tertiary">Satrap 版本要求</dt><dd>{plugin.compatibility?.satrap || '未声明'}</dd></div>
        <div><dt className="text-text-tertiary">适用会话</dt><dd>{plugin.applicability?.session_types?.map((type) => sessionLabels[type] || type).join('、') || '未限制'}</dd></div>
        <div><dt className="text-text-tertiary">适用平台</dt><dd>{Array.isArray(plugin.applicability?.platforms) ? plugin.applicability.platforms.join('、') : '未限制'}</dd></div>
      </dl>
      <p className="whitespace-pre-wrap text-text-secondary">{plugin.description || '暂无说明'}</p>
      <p className="text-sm text-text-tertiary">{plugin.usage_count} 个使用位置。能力声明来自插件元数据，实际加载结果由各运行实例决定。</p>
    </Card>
    <Card><h2 className="mb-4 text-lg font-semibold">声明的能力</h2><PluginCapabilities plugin={plugin} /></Card>
  </div>;
}

export function Plugins() {
  const { plugins, loading, error, refresh } = usePluginCatalog();
  const [query, setQuery] = useState('');
  const [source, setSource] = useState('');
  const [platform, setPlatform] = useState('');
  const platforms = useMemo(() => [...new Set(plugins.flatMap((plugin) => Array.isArray(plugin.applicability?.platforms) ? plugin.applicability.platforms : []))].sort(), [plugins]);
  const filtered = plugins.filter((plugin) => {
    const allowed = plugin.applicability?.platforms;
    const sessions = plugin.applicability?.session_types;
    return (!source || plugin.source === source)
      && (!platform || ((!sessions || sessions.includes('platform')) && (!Array.isArray(allowed) || allowed.includes(platform))))
      && `${plugin.name} ${plugin.description} ${plugin.author}`.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase());
  });
  return <div className="space-y-6">
    <PageHeader title="插件管理" description="查看内置与用户插件，管理配置和使用位置" actions={<Button onClick={refresh} disabled={loading}><RefreshCw size={16} className="mr-2" />刷新</Button>} />
    <div className="flex flex-wrap gap-3">
      <input aria-label="搜索插件" className="glass-input min-w-0 flex-1" placeholder="搜索名称、作者或简介" value={query} onChange={(event) => setQuery(event.target.value)} />
      <select aria-label="插件来源" className="glass-input" value={source} onChange={(event) => setSource(event.target.value)}><option value="">全部来源</option><option value="builtin">内置</option><option value="user">用户</option></select>
      <select aria-label="适用平台" className="glass-input" value={platform} onChange={(event) => setPlatform(event.target.value)}><option value="">全部平台</option>{platforms.map((item) => <option key={item} value={item}>{item}</option>)}</select>
    </div>
    {error && <p role="alert" className="text-error">{error}</p>}
    {loading ? <p role="status">正在读取插件…</p> : <>
      <p className="text-sm text-text-tertiary">共 {plugins.length} 个插件，显示 {filtered.length} 个</p>
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">{filtered.map((plugin) => <Link key={plugin.name} to={`/plugins/${encodeURIComponent(plugin.name)}`}>
        <Card interactive className="h-full space-y-3">
          <div className="flex flex-wrap items-center justify-between gap-2"><h2 className="break-all text-lg font-semibold">{plugin.name}</h2><span className="text-xs text-text-tertiary">{sourceLabels[plugin.source]}</span></div>
          <p className="text-sm text-text-secondary">{plugin.description || '暂无说明'}</p>
          <p className="text-xs text-text-tertiary">版本 {plugin.version || '未声明'} · {plugin.usage_count} 个使用位置</p>
          <p className="text-xs text-text-secondary">{Object.entries(capabilityLabels).filter(([kind]) => Object.keys(plugin.capabilities[kind] || {}).length).map(([, label]) => label).join(' · ') || '未声明能力'}</p>
        </Card>
      </Link>)}</div>
      {!filtered.length && <p className="text-text-secondary">{plugins.length ? '没有匹配的插件' : '尚未发现插件'}</p>}
    </>}
  </div>;
}

export function PluginDetail() {
  const { name } = useParams();
  const { plugins, loading, error, refresh } = usePluginCatalog();
  const plugin = plugins.find((item) => item.name === name);
  return <div className="space-y-6">
    <Link to="/plugins" className="text-sm text-accent">← 返回插件列表</Link>
    <PageHeader title={name || '插件详情'} actions={<Button onClick={refresh} disabled={loading}>刷新</Button>} />
    {error && <p role="alert" className="text-error">{error}</p>}
    {loading ? <p role="status">正在读取插件…</p> : plugin ? <PluginOverview plugin={plugin} /> : !error && <p role="alert">插件不存在或元数据无效</p>}
  </div>;
}
