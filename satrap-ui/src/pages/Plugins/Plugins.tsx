import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import {
  ArrowUpRight, CheckCircle2, LayoutGrid, List, Plus, Puzzle, RefreshCw, Search, ShieldAlert, Sparkles,
} from 'lucide-react';
import { cn } from '@/utils/cn';
import { Badge, Button, Card, CountUp, EmptyState, IconButton, Input, PageHeader, Skeleton } from '@/components/kit';
import { startVisiblePoll } from '@/utils/visiblePoll';
import { useCachedState } from '@/hooks/useCachedState';
import { errorText } from '@/utils/errorText';
import { sourceStateLabels } from '@/api/contracts';
import { pluginCatalogHealthRead, pluginHealthCoverage, type CatalogPlugin } from '@/api/pluginsContracts';
import { InstallPluginModal } from './InstallPluginModal';
import { CapabilityChips, ExecutionChip, HealthChip, PluginIcon, SourceChip, TealPrimary, TintTabs } from './PluginBits';
import {
  capabilityCount, displayName, executionEnabled, hasIssue, isEffectivelyEnabled, pluginCapabilities, pluginHealth,
  SOURCE_LABELS,
} from './pluginModel';

type Filter = 'all' | 'enabled' | 'disabled' | 'issue';
type View = 'grid' | 'list';

function usePluginCatalog() {
  const [plugins, setPlugins] = useCachedState<CatalogPlugin[]>('plugins:catalog', []);
  const [loaded, setLoaded] = useCachedState('plugins:loaded', false);
  const [fetching, setFetching] = useState(true);
  const [error, setError] = useState('');
  const [revision, setRevision] = useState(0);
  const refresh = useCallback(() => setRevision((value) => value + 1), []);
  useEffect(() => {
    setFetching(true);
    setError('');
    let force = revision > 0;
    const poll = startVisiblePoll(async (signal) => {
      const forced = force; force = false;
      try {
        const data = await pluginCatalogHealthRead.read({ force: forced, signal });
        if (!signal.aborted) { setPlugins(data); setLoaded(true); setError(''); }
      } finally { if (!signal.aborted) setFetching(false); }
    }, { interval: 30000, onError: (reason: unknown) => setError(errorText(reason, '读取插件失败')) });
    return () => poll.stop();
  }, [revision, setPlugins, setLoaded]);
  // loading 只在从未取到过目录时为真, 返回页面时先显示上次的目录
  return { plugins, loading: !loaded && fetching, fetching, error, refresh };
}

function PluginCard({ plugin, index }: { plugin: CatalogPlugin; index: number }) {
  const health = pluginHealth(plugin);
  const tint = health === 'attention' ? 'orange' : health === 'healthy' ? 'green' : 'teal';
  const caps = pluginCapabilities(plugin);
  const enabled = executionEnabled(plugin);
  return (
    <Link
      to={`/plugins/${encodeURIComponent(plugin.name)}`}
      aria-label={`${displayName(plugin)} ${plugin.version || '未声明版本'}，打开插件详情`}
      className="block min-w-0 rounded-[var(--radius-lg)] focus-visible:outline-none"
      style={{ ['--i' as string]: index }}
    >
      <Card interactive tint={tint} padding="md" className="group relative flex h-full flex-col gap-3">
        <span aria-hidden className={cn('absolute inset-y-4 left-0 w-[3px] rounded-r-full',
          health === 'attention' ? 'bg-[rgb(var(--rgb-orange))]' : health === 'healthy' ? 'bg-[rgb(var(--rgb-teal))]' : 'bg-[var(--text-4)]')} />
        <div className="flex items-start gap-3">
          <PluginIcon plugin={plugin} showHealth />
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-1.5">
              <h3 className="truncate text-[15px] font-semibold text-fg">{displayName(plugin)}</h3>
              {plugin.source === 'builtin' && <Sparkles className="h-3.5 w-3.5 flex-none text-teal" aria-label="内置插件" />}
            </div>
            <p className="truncate font-mono text-[11px] text-fg-4">{plugin.name}</p>
            <div className="mt-1 flex flex-wrap items-center gap-1.5">
              <SourceChip source={plugin.source} />
              <Badge tone="neutral" className="font-mono">v{plugin.version || '未声明'}</Badge>
              <ExecutionChip enabled={enabled} />
            </div>
          </div>
          <ArrowUpRight className="h-3.5 w-3.5 -translate-x-1 translate-y-1 text-fg-4 opacity-0 transition-all duration-300 group-hover:translate-x-0 group-hover:translate-y-0 group-hover:text-teal group-hover:opacity-100" />
        </div>

        <p className="line-clamp-2 min-h-[40px] text-[12.5px] leading-5 text-fg-2">{plugin.description || '暂无说明'}</p>

        <CapabilityChips caps={caps} max={4} />

        <div className="mt-auto flex flex-wrap items-center justify-between gap-2 border-t border-line pt-3">
          <span className="flex items-center gap-2 text-[11px] text-fg-3">
            <span className="num">{plugin.usage_count ?? 0} 个使用位置</span>
            <span className="text-fg-4">·</span>
            <span className="num">{capabilityCount(caps)} 项能力</span>
          </span>
          <HealthChip plugin={plugin} />
        </div>
      </Card>
    </Link>
  );
}

function PluginRow({ plugin, index }: { plugin: CatalogPlugin; index: number }) {
  const caps = pluginCapabilities(plugin);
  const enabled = executionEnabled(plugin);
  return (
    <Link
      to={`/plugins/${encodeURIComponent(plugin.name)}`}
      aria-label={`${displayName(plugin)} ${plugin.version || '未声明版本'}，打开插件详情`}
      style={{ ['--i' as string]: index }}
      className="group grid cursor-pointer grid-cols-[minmax(0,1fr)_auto] items-center gap-3 border-b border-line px-4 py-3 transition-colors last:border-0 hover:bg-glass-hover sm:grid-cols-[minmax(0,1.5fr)_minmax(0,1fr)_auto_auto]"
    >
      <div className="flex min-w-0 items-center gap-3">
        <PluginIcon plugin={plugin} size="sm" showHealth />
        <div className="min-w-0">
          <div className="flex items-center gap-1.5">
            <span className="truncate text-[13.5px] font-medium text-fg">{displayName(plugin)}</span>
            <span className="hidden font-mono text-[10.5px] text-fg-4 sm:inline">{plugin.name}</span>
          </div>
          <p className="truncate text-[11.5px] text-fg-3">{plugin.description || '暂无说明'}</p>
        </div>
      </div>
      <div className="hidden min-w-0 sm:block"><CapabilityChips caps={caps} max={3} /></div>
      <div className="hidden w-24 flex-col items-end sm:flex">
        <span className="num text-[12px] text-fg-2">{plugin.usage_count ?? 0} 个位置</span>
        <span className="num text-[10.5px] text-fg-4">{capabilityCount(caps)} 项能力</span>
      </div>
      <div className="flex items-center gap-2">
        <span className="hidden flex-col items-end gap-1 md:flex">
          <HealthChip plugin={plugin} />
          <ExecutionChip enabled={enabled} />
        </span>
        <ArrowUpRight className="h-4 w-4 text-fg-4 transition group-hover:text-teal" />
      </div>
    </Link>
  );
}

export function Plugins() {
  const navigate = useNavigate();
  const { plugins, loading, fetching, error, refresh } = usePluginCatalog();
  const [filter, setFilter] = useState<Filter>('all');
  const [query, setQuery] = useState('');
  const [source, setSource] = useState('');
  const [platform, setPlatform] = useState('');
  const [view, setView] = useState<View>('grid');
  const [installOpen, setInstallOpen] = useState(false);

  const coverage = useMemo(() => pluginHealthCoverage(plugins), [plugins]);
  const counts = useMemo(() => ({
    all: plugins.length,
    enabled: plugins.filter((plugin) => isEffectivelyEnabled(plugin)).length,
    disabled: plugins.filter((plugin) => !isEffectivelyEnabled(plugin)).length,
    issue: plugins.filter((plugin) => hasIssue(plugin)).length,
    unknown: plugins.filter((plugin) => pluginHealth(plugin) === 'unknown').length,
  }), [plugins]);

  const platforms = useMemo(() => {
    const values = new Set<string>();
    for (const plugin of plugins) {
      const declared = plugin.applicability?.platforms;
      if (Array.isArray(declared)) declared.forEach((value) => values.add(value));
    }
    return [...values].sort();
  }, [plugins]);

  const filtered = useMemo(() => {
    const term = query.trim().toLowerCase();
    return plugins.filter((plugin) => {
      const sessions = plugin.applicability?.session_types;
      const declared = plugin.applicability?.platforms;
      const sessionMatches = !sessions || sessions.includes('platform');
      const platformMatches = !platform || (sessionMatches && (declared === '*' || (Array.isArray(declared) && declared.includes(platform))));
      const sourceMatches = !source || plugin.source === source;
      const queryMatches = !term || `${plugin.name} ${displayName(plugin)} ${plugin.description || ''} ${plugin.author || ''}`.toLowerCase().includes(term);
      const filterMatches = filter === 'all'
        || (filter === 'enabled' && isEffectivelyEnabled(plugin))
        || (filter === 'disabled' && !isEffectivelyEnabled(plugin))
        || (filter === 'issue' && hasIssue(plugin));
      return platformMatches && sourceMatches && queryMatches && filterMatches;
    });
  }, [plugins, platform, source, query, filter]);

  const clearFilters = () => {
    setFilter('all');
    setQuery('');
    setSource('');
    setPlatform('');
  };

  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow={<Badge tone="teal"><Puzzle className="h-3 w-3" />编排 · 插件运行时</Badge>}
        title="插件管理"
        description="查看内置与用户插件，管理全局配置、使用位置、技能与表情库。"
        actions={<>
          <Button icon={<RefreshCw className={cn('h-4 w-4', fetching && 'animate-spin-slow')} />} disabled={fetching} onClick={refresh}>刷新</Button>
          <TealPrimary icon={<Plus className="h-4 w-4" />} onClick={() => setInstallOpen(true)}>安装插件</TealPrimary>
        </>}
      />

      <Card padding="none" className="animate-rise overflow-hidden">
        <div className="grid grid-cols-2 gap-2 p-3 sm:grid-cols-4">
          {[
            { label: '已安装插件', value: counts.all, icon: <Puzzle className="h-4 w-4" />, tint: 'teal', hint: '含内置与用户插件' },
            { label: '执行开启', value: counts.enabled, icon: <CheckCircle2 className="h-4 w-4" />, tint: 'green', hint: `${counts.disabled} 个执行停用` },
            { label: '需关注', value: counts.issue, icon: <ShieldAlert className="h-4 w-4" />, tint: counts.issue ? 'orange' : 'green', hint: '运行异常或降级' },
            { label: '健康未知', value: counts.unknown, icon: <ShieldAlert className="h-4 w-4" />, tint: counts.unknown ? 'purple' : 'green', hint: '运行服务未提供观测' },
          ].map((metric) => (
            <div key={metric.label} className={cn('flex items-center gap-3 rounded-xl border border-line bg-glass px-3.5 py-3', `tint-${metric.tint}`)}>
              <div className="grid h-9 w-9 flex-none place-items-center rounded-xl bg-[rgb(var(--tint)/0.14)] text-tint shadow-[inset_0_1px_0_var(--glass-highlight)]">
                {metric.icon}
              </div>
              <div className="min-w-0">
                <div className="text-[11px] text-fg-3">{metric.label}</div>
                <div className="display text-[20px] font-semibold leading-tight text-fg"><CountUp value={metric.value} /></div>
                <div className="truncate text-[10.5px] text-fg-4">{metric.hint}</div>
              </div>
            </div>
          ))}
        </div>
        <div className="flex flex-wrap items-center gap-2 border-t border-line px-3.5 py-2.5 text-[11.5px] text-fg-3">
          <span className="text-fg-4">健康覆盖</span>
          {coverage.sources.length ? coverage.sources.map((entry) => (
            <Badge key={`${entry.service}:${entry.state}`} tone={entry.state === 'available' ? 'green' : entry.state === 'stale' ? 'orange' : 'red'} dot live={entry.state === 'available'}>
              {entry.service} · {sourceStateLabels[entry.state] || entry.state}
            </Badge>
          )) : <Badge tone="neutral">{coverage.known ? '暂无需要观测的运行来源' : '查看服务未提供健康数据'}</Badge>}
          {coverage.incomplete && <Badge tone="orange" dot>覆盖不完整，未知项不计为 0</Badge>}
          {!coverage.known && <span className="text-fg-4">暂无健康观测，状态显示为未知。</span>}
        </div>
      </Card>

      <div className="flex flex-wrap items-center gap-3 animate-rise" style={{ animationDelay: '60ms' }}>
        <div className="max-w-full overflow-x-auto">
          <TintTabs<Filter>
            label="插件筛选"
            value={filter}
            onChange={setFilter}
            items={[
              { value: 'all', label: '全部', count: counts.all },
              { value: 'enabled', label: '执行开启', count: counts.enabled },
              { value: 'disabled', label: '执行停用', count: counts.disabled },
              { value: 'issue', label: '异常', count: counts.issue },
            ]}
          />
        </div>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          <div className="w-56 max-w-full min-w-[180px]">
            <Input leading={<Search className="h-4 w-4" />} value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索名称、作者或简介…" aria-label="搜索插件" />
          </div>
          <select aria-label="插件来源" className="field h-9 w-32" value={source} onChange={(event) => setSource(event.target.value)}>
            <option value="">全部来源</option>
            <option value="builtin">{SOURCE_LABELS.builtin}</option>
            <option value="user">{SOURCE_LABELS.user}</option>
          </select>
          <select aria-label="适用平台" className="field h-9 w-32" value={platform} onChange={(event) => setPlatform(event.target.value)}>
            <option value="">全部平台</option>
            {platforms.map((value) => <option key={value} value={value}>{value}</option>)}
          </select>
          <div className="flex rounded-[10px] border border-line bg-glass p-0.5">
            <IconButton label="卡片视图" size="sm" active={view === 'grid'} onClick={() => setView('grid')}><LayoutGrid className="h-4 w-4" /></IconButton>
            <IconButton label="列表视图" size="sm" active={view === 'list'} onClick={() => setView('list')}><List className="h-4 w-4" /></IconButton>
          </div>
        </div>
      </div>

      <div className="flex flex-wrap items-center justify-between gap-2 text-[12px] text-fg-3">
        <span className="num">共 {plugins.length} 个插件，显示 {filtered.length} 个</span>
        {(filter !== 'all' || query || source || platform) && (
          <button type="button" onClick={clearFilters} className="text-[11.5px] text-teal transition hover:underline">清除全部筛选</button>
        )}
      </div>

      {error ? (
        <Card padding="none" className="animate-rise">
          <EmptyState
            tint="orange"
            icon={<ShieldAlert className="h-6 w-6" />}
            title="无法读取插件目录"
            description={error}
            actions={<>
              <Button onClick={refresh} icon={<RefreshCw className="h-4 w-4" />}>重试</Button>
              <Button variant="ghost" onClick={() => setInstallOpen(true)}>仍要安装插件</Button>
            </>}
          />
        </Card>
      ) : loading ? (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
          {Array.from({ length: 6 }).map((_, index) => (
            <Card key={index} padding="md" className="space-y-3" style={{ ['--i' as string]: index }}>
              <div className="flex gap-3"><Skeleton className="h-11 w-11" /><div className="flex-1 space-y-2"><Skeleton className="w-1/2" /><Skeleton className="w-1/3" /></div></div>
              <Skeleton className="w-full" /><Skeleton className="w-4/5" />
              <div className="flex gap-1.5"><Skeleton className="h-5 w-14" /><Skeleton className="h-5 w-14" /></div>
            </Card>
          ))}
        </div>
      ) : filtered.length ? (
        view === 'grid' ? (
          <div key={`${filter}-${query}-${source}-${platform}`} className="stagger grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
            {filtered.map((plugin, index) => <PluginCard key={plugin.name} plugin={plugin} index={index} />)}
            <button
              type="button"
              onClick={() => setInstallOpen(true)}
              style={{ ['--i' as string]: filtered.length }}
              className="group grid min-h-[220px] place-items-center rounded-[var(--radius-lg)] border-2 border-dashed border-[var(--glass-border-strong)] text-fg-3 transition-all duration-300 hover:border-[rgb(var(--rgb-teal)/0.5)] hover:bg-[rgb(var(--rgb-teal)/0.05)] hover:text-teal"
            >
              <span className="flex flex-col items-center gap-2 px-4 text-center text-sm font-medium">
                <span className="grid h-11 w-11 place-items-center rounded-xl border border-current/20 transition-transform duration-300 group-hover:rotate-90 group-hover:scale-110">
                  <Plus className="h-5 w-5" />
                </span>
                安装新插件
                <span className="text-[11px] font-normal text-fg-4">ZIP 压缩包</span>
              </span>
            </button>
          </div>
        ) : (
          <Card padding="none" className="animate-rise overflow-hidden">
            <div className="grid grid-cols-[minmax(0,1fr)_auto] gap-3 border-b border-line bg-glass px-4 py-2.5 text-[11px] font-medium text-fg-4 sm:grid-cols-[minmax(0,1.5fr)_minmax(0,1fr)_auto_auto]">
              <span>插件</span>
              <span className="hidden sm:block">声明能力</span>
              <span className="hidden w-24 text-right sm:block">使用位置</span>
              <span className="text-right">健康 / 执行</span>
            </div>
            <div className="stagger">
              {filtered.map((plugin, index) => <PluginRow key={plugin.name} plugin={plugin} index={index} />)}
            </div>
            <div className="border-t border-line px-4 py-2.5">
              <Button variant="ghost" size="sm" icon={<Plus className="h-3.5 w-3.5" />} onClick={() => setInstallOpen(true)}>安装新插件</Button>
            </div>
          </Card>
        )
      ) : (
        <Card padding="none">
          <EmptyState
            tint="teal"
            icon={<Puzzle className="h-6 w-6" />}
            title={plugins.length ? '没有匹配的插件' : '尚未发现插件'}
            description={plugins.length ? '调整筛选条件，或清除搜索关键字后再试' : '安装一个插件后，它可以为 Agent 提供工具、技能或平台能力'}
            actions={plugins.length
              ? <Button onClick={clearFilters}>清除筛选</Button>
              : <TealPrimary icon={<Plus className="h-4 w-4" />} onClick={() => setInstallOpen(true)}>安装第一个插件</TealPrimary>}
          />
        </Card>
      )}

      {installOpen && (
        <InstallPluginModal
          onClose={() => setInstallOpen(false)}
          onInstalled={(name) => { setInstallOpen(false); refresh(); navigate(`/plugins/${encodeURIComponent(name)}`); }}
        />
      )}
    </div>
  );
}
