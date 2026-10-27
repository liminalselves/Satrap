import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Database, FileCode2, FileText, Info, KeyRound, Palette, ScrollText, Server, SlidersHorizontal, Users,
  type LucideIcon,
} from 'lucide-react';
import * as yaml from 'js-yaml';
import { cn } from '@/utils/cn';
import { Badge, Button, PageHeader } from '@/components/kit';
import type { Tint } from '@/components/kit';
import { toast } from '@/components/ui/Toast';
import { useBackendStore } from '@/stores/useBackendStore';
import { usePrefsStore } from '@/stores/usePrefsStore';
import { controlApi } from '@/api/control';
import { errorText } from '@/utils/errorText';
import type { AdministratorGroup, AdministratorOverride } from '@/api/administrators';
import { extractCommonConfig, mergeCommonConfig, initialCommonConfig, type CommonConfig } from './types';
import { AppearanceSection } from './AppearanceSection';
import { CommonConfigSection } from './CommonConfigSection';
import { BackendControlSection } from './BackendControlSection';
import { AccessPasswordPanel } from './AccessPasswordPanel';
import { AdministratorsPanel } from './AdministratorsPanel';
import { LogRetentionPanel } from './LogRetentionPanel';
import { DataMaintenancePanel } from './DataMaintenancePanel';
import { RawConfigSection } from './RawConfigSection';
import { AboutSection } from './AboutSection';

type SectionId = 'appearance' | 'common' | 'backend' | 'password' | 'admins' | 'logs' | 'data' | 'raw' | 'about';
type DirtyId = 'common' | 'admins' | 'logs' | 'raw';
type SectionActions = { save: () => Promise<boolean>; undo: () => void };

const sections: { id: SectionId; label: string; icon: LucideIcon; tint: Tint; description: string }[] = [
  { id: 'appearance', label: '外观', icon: Palette, tint: 'purple', description: '主题 · 动效 · 侧栏' },
  { id: 'common', label: '常用配置', icon: SlidersHorizontal, tint: 'accent', description: '监听 · 会话 · 限流' },
  { id: 'backend', label: '后端控制', icon: Server, tint: 'green', description: '启动 · 重启 · 服务' },
  { id: 'password', label: '访问密码', icon: KeyRound, tint: 'accent', description: '启用 · 修改 · 清除' },
  { id: 'admins', label: '管理员', icon: Users, tint: 'teal', description: '管理组 · 例外 · 权限' },
  { id: 'logs', label: '日志保留', icon: ScrollText, tint: 'orange', description: '策略 · 清理 · 结果' },
  { id: 'data', label: '数据维护', icon: Database, tint: 'pink', description: '孤儿数据 · 回收区' },
  { id: 'raw', label: '原始配置', icon: FileCode2, tint: 'purple', description: 'YAML · 校验 · 差异' },
  { id: 'about', label: '关于', icon: Info, tint: 'orange', description: '版本 · 环境 · 技术栈' },
];

const dirtyOrder: DirtyId[] = ['common', 'admins', 'logs', 'raw'];
const saveOrder: DirtyId[] = ['admins', 'logs', 'common', 'raw'];
const dirtyLabels: Record<DirtyId, string> = { common: '常用配置', admins: '管理员', logs: '日志保留', raw: '原始配置' };

function dumpConfig(value: Record<string, unknown>): string {
  return yaml.dump(value, { indent: 2, lineWidth: -1 });
}

export function Settings() {
  const isRunning = useBackendStore((state) => state.isRunning);
  const runState = useBackendStore((state) => state.runState);
  const controlBackend = useBackendStore((state) => state.controlBackend);
  const motion = usePrefsStore((state) => state.motion);

  const [config, setConfig] = useState<Record<string, unknown>>({});
  const [rawSaved, setRawSaved] = useState('');
  const [rawDraft, setRawDraft] = useState('');
  const [commonSaved, setCommonSaved] = useState<CommonConfig>(initialCommonConfig);
  const [commonDraft, setCommonDraft] = useState<CommonConfig>(initialCommonConfig);
  const [configRevision, setConfigRevision] = useState<string | undefined>();
  const [configPath, setConfigPath] = useState('');
  const [configExists, setConfigExists] = useState(false);
  const [controlAvailable, setControlAvailable] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [savingAll, setSavingAll] = useState(false);
  const [active, setActive] = useState<SectionId>('appearance');
  const [dirty, setDirty] = useState<Record<DirtyId, boolean>>({ common: false, admins: false, logs: false, raw: false });
  const actionsRef = useRef<Partial<Record<DirtyId, SectionActions>>>({});
  const navTargetRef = useRef<SectionId | null>(null);
  const scrollTimerRef = useRef<number | undefined>(undefined);
  const navFallbackRef = useRef<number | undefined>(undefined);
  const rawSavedRef = useRef(rawSaved);
  const rawDraftRef = useRef(rawDraft);
  const commonSavedRef = useRef(commonSaved);
  const commonDraftRef = useRef(commonDraft);

  rawSavedRef.current = rawSaved;
  rawDraftRef.current = rawDraft;
  commonSavedRef.current = commonSaved;
  commonDraftRef.current = commonDraft;

  const backendRunning = runState === 'running';
  const controlOnline = controlAvailable;
  const dirtyCount = dirtyOrder.filter((id) => dirty[id]).length;
  const commonDirty = JSON.stringify(commonDraft) !== JSON.stringify(commonSaved);

  const loadConfig = useCallback(async () => {
    setLoading(true);
    try {
      const result = await controlApi.getConfig();
      if (result.ok && result.config) {
        const loaded = { ...result.config } as Record<string, unknown>;
        // 兼容旧字段: llm_timeout 已更名为 session_timeout
        if (!('session_timeout' in loaded) && 'llm_timeout' in loaded) {
          loaded.session_timeout = loaded.llm_timeout as number | null;
        }
        delete loaded.llm_timeout;
        const text = dumpConfig(loaded);
        setConfig(loaded);
        setConfigRevision(result.revision);
        setRawSaved(text);
        setRawDraft(text);
        setCommonSaved(extractCommonConfig(loaded));
        setCommonDraft(extractCommonConfig(loaded));
        setConfigPath(result.path || '');
        setConfigExists(result.exists !== false);
        setControlAvailable(true);
      } else {
        setControlAvailable(false);
      }
    } catch {
      setControlAvailable(false);
    } finally {
      setLoading(false);
    }
  }, []);

  /** 原始配置的“重新读取”: 保留有未保存修改的草稿, 只刷新已保存版本与修订号 */
  const reloadRaw = useCallback(async () => {
    try {
      const result = await controlApi.getConfig();
      if (!result.ok || !result.config) return;
      const loaded = { ...result.config } as Record<string, unknown>;
      if (!('session_timeout' in loaded) && 'llm_timeout' in loaded) {
        loaded.session_timeout = loaded.llm_timeout as number | null;
      }
      delete loaded.llm_timeout;
      const text = dumpConfig(loaded);
      const rawClean = rawDraftRef.current === rawSavedRef.current;
      const commonClean = JSON.stringify(commonDraftRef.current) === JSON.stringify(commonSavedRef.current);
      setConfig(loaded);
      setConfigRevision(result.revision);
      setConfigPath(result.path || '');
      setConfigExists(result.exists !== false);
      setControlAvailable(true);
      setRawSaved(text);
      if (rawClean) setRawDraft(text);
      const extracted = extractCommonConfig(loaded, commonDraftRef.current);
      setCommonSaved(extracted);
      if (commonClean) setCommonDraft(extracted);
    } catch {
      // 控制服务不可用时保留当前草稿, 由横幅提示
    }
  }, []);

  useEffect(() => {
    void loadConfig();
  }, [loadConfig]);

  const reportDirty = useCallback((id: DirtyId, value: boolean) => {
    setDirty((current) => (current[id] === value ? current : { ...current, [id]: value }));
  }, []);

  const dirtyReporters = useMemo(() => ({
    common: (value: boolean) => reportDirty('common', value),
    admins: (value: boolean) => reportDirty('admins', value),
    logs: (value: boolean) => reportDirty('logs', value),
    raw: (value: boolean) => reportDirty('raw', value),
  }), [reportDirty]);

  const registerers = useMemo(() => ({
    common: (value: SectionActions) => { actionsRef.current.common = value; },
    admins: (value: SectionActions) => { actionsRef.current.admins = value; },
    logs: (value: SectionActions) => { actionsRef.current.logs = value; },
    raw: (value: SectionActions) => { actionsRef.current.raw = value; },
  }), []);

  const applyServerConfig = useCallback((nextConfig: Record<string, unknown>, revision: string | undefined, source: 'common' | 'raw' | 'default') => {
    const text = dumpConfig(nextConfig);
    const rawClean = rawDraftRef.current === rawSavedRef.current;
    const commonClean = JSON.stringify(commonDraftRef.current) === JSON.stringify(commonSavedRef.current);
    setConfig(nextConfig);
    if (revision) setConfigRevision(revision);
    setConfigExists(true);
    setRawSaved(text);
    if (source === 'raw' || source === 'default' || rawClean) setRawDraft(text);
    const extracted = extractCommonConfig(nextConfig, commonDraftRef.current);
    setCommonSaved(extracted);
    if (source === 'common' || source === 'default' || commonClean) setCommonDraft(extracted);
  }, []);

  /** 保存完整配置 (常用配置与原始配置共用同一 CAS 入口) */
  const persistConfig = useCallback(async (payload: Record<string, unknown>, source: 'common' | 'raw'): Promise<boolean> => {
    setSaving(true);
    try {
      const result = await controlApi.saveConfig(payload, configRevision);
      if (!result.ok) {
        toast('error', result.error || '保存失败');
        return false;
      }
      const saved = (result.config || payload) as Record<string, unknown>;
      applyServerConfig(saved, result.revision, source);
      if (isRunning) {
        const restarted = await controlBackend('restart');
        toast(
          restarted.ok ? 'success' : 'warning',
          restarted.ok ? '配置已保存, 后端已重启' : `配置已保存, 但重启失败: ${restarted.error || '未知错误'}`,
        );
      } else {
        toast('success', '配置已保存');
      }
      return true;
    } catch (cause) {
      toast('error', '保存失败: ' + errorText(cause, '未知错误'));
      return false;
    } finally {
      setSaving(false);
    }
  }, [applyServerConfig, configRevision, controlBackend, isRunning]);

  const saveCommon = useCallback(async (): Promise<boolean> => {
    if (!controlOnline || !configExists) { toast('error', '控制服务未运行或配置文件缺失'); return false; }
    return persistConfig(mergeCommonConfig(config, commonDraft), 'common');
  }, [commonDraft, config, configExists, controlOnline, persistConfig]);

  const saveRaw = useCallback(async (): Promise<boolean> => {
    if (!controlOnline || !configExists) { toast('error', '控制服务未运行或配置文件缺失'); return false; }
    let parsed: unknown;
    try {
      parsed = yaml.load(rawDraft, { schema: yaml.JSON_SCHEMA, json: true });
    } catch (cause) {
      toast('error', 'YAML 格式错误: ' + (cause instanceof Error ? cause.message : '未知错误'));
      return false;
    }
    if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
      toast('error', '配置必须是对象格式');
      return false;
    }
    return persistConfig(parsed as Record<string, unknown>, 'raw');
  }, [controlOnline, configExists, persistConfig, rawDraft]);

  const createDefault = useCallback(async (): Promise<boolean> => {
    setLoading(true);
    try {
      const result = await controlApi.createDefaultConfig();
      if (!result.ok || !result.config) {
        toast('error', result.error || '创建默认配置失败');
        return false;
      }
      applyServerConfig(result.config, result.revision, 'default');
      setConfigPath(result.path || '');
      setControlAvailable(true);
      toast('success', '默认配置已创建');
      return true;
    } catch (cause) {
      toast('error', '创建默认配置失败: ' + errorText(cause, '未知错误'));
      return false;
    } finally {
      setLoading(false);
    }
  }, [applyServerConfig]);

  const handleAdminSaved = useCallback((groups: AdministratorGroup[], overrides: AdministratorOverride[]) => {
    const nextConfig: Record<string, unknown> = {
      ...config,
      administrator_groups: groups,
      administrator_overrides: overrides,
    };
    const text = dumpConfig(nextConfig);
    setConfig(nextConfig);
    setRawSaved(text);
    setRawDraft((current) => (rawDraftRef.current === rawSavedRef.current ? text : current));
  }, [config]);

  const undoCommon = useCallback(() => {
    setCommonDraft(commonSaved);
    toast('success', '已撤销常用配置的修改');
  }, [commonSaved]);

  const undoRaw = useCallback(() => {
    setRawDraft(rawSaved);
    toast('success', '已恢复到已保存的配置');
  }, [rawSaved]);

  useEffect(() => { registerers.common({ save: saveCommon, undo: undoCommon }); }, [registerers, saveCommon, undoCommon]);
  useEffect(() => { registerers.raw({ save: saveRaw, undo: undoRaw }); }, [registerers, saveRaw, undoRaw]);

  const saveAll = async () => {
    if (savingAll) return;
    setSavingAll(true);
    try {
      for (const id of saveOrder) {
        if (!dirty[id]) continue;
        await actionsRef.current[id]?.save();
      }
    } finally {
      setSavingAll(false);
    }
  };

  const undoAll = () => {
    for (const id of dirtyOrder) {
      if (dirty[id]) actionsRef.current[id]?.undo();
    }
  };

  const navigateTo = (id: SectionId) => {
    // 点击导航后锁定目标分节, 直到平滑滚动结束, 避免途中其它分节抢占当前状态
    navTargetRef.current = id;
    if (scrollTimerRef.current) window.clearTimeout(scrollTimerRef.current);
    if (navFallbackRef.current) window.clearTimeout(navFallbackRef.current);
    setActive(id);
    document.getElementById(`sec-${id}`)?.scrollIntoView({ behavior: motion === 'full' ? 'smooth' : 'auto', block: 'start' });
    // 已经位于目标位置时不会产生 scroll 事件, 用兜底定时器解除锁定
    navFallbackRef.current = window.setTimeout(() => { navTargetRef.current = null; }, 1200);
  };

  useEffect(() => {
    // 页面在布局的 <main> 内滚动, window 不会产生 scroll 事件; 取首个分节最近的可滚动祖先
    const first = document.getElementById(`sec-${sections[0].id}`);
    let scroller: HTMLElement | null = first?.parentElement ?? null;
    while (scroller && !/(auto|scroll)/.test(getComputedStyle(scroller).overflowY)) scroller = scroller.parentElement;
    const target: HTMLElement | Window = scroller ?? window;
    const computeActive = () => {
      const top = scroller ? scroller.getBoundingClientRect().top : 0;
      const height = scroller ? scroller.clientHeight : window.innerHeight;
      // 滚到底部时较短的末尾分节无法到达判定线, 直接归属最后一节
      const atBottom = scroller
        ? scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 2
        : window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 2;
      if (atBottom) { setActive(sections[sections.length - 1].id); return; }
      const band = top + height * 0.25;
      let current: SectionId = sections[0].id;
      for (const section of sections) {
        const element = document.getElementById(`sec-${section.id}`);
        if (element && element.getBoundingClientRect().top <= band) current = section.id;
      }
      setActive(current);
    };
    const onScroll = () => {
      if (navTargetRef.current) {
        if (scrollTimerRef.current) window.clearTimeout(scrollTimerRef.current);
        scrollTimerRef.current = window.setTimeout(() => {
          const target = navTargetRef.current;
          navTargetRef.current = null;
          if (target) setActive(target);
        }, 150);
        return;
      }
      computeActive();
    };
    target.addEventListener('scroll', onScroll, { passive: true });
    return () => {
      target.removeEventListener('scroll', onScroll);
      if (scrollTimerRef.current) window.clearTimeout(scrollTimerRef.current);
      if (navFallbackRef.current) window.clearTimeout(navFallbackRef.current);
    };
  }, []);

  return (
    <div className="space-y-6 pb-28">
      <PageHeader
        title="系统设置"
        description="外观、运行参数、访问控制与运维数据。所有修改在保存前会保留为草稿"
        actions={<>
          <Badge tone={controlOnline ? 'green' : 'red'} dot live={controlOnline}>控制服务{controlOnline ? '在线' : '离线'}</Badge>
          <Badge tone={backendRunning ? 'green' : 'orange'} dot live={backendRunning}>{backendRunning ? '后端运行中' : '后端未运行'}</Badge>
          {dirtyCount > 0 && (
            <Button variant="primary" size="sm" loading={savingAll} onClick={() => void saveAll()}>保存全部 ({dirtyCount})</Button>
          )}
        </>}
      />

      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-2xl border border-line bg-glass px-4 py-2.5 text-[11.5px] text-fg-3 backdrop-blur-md animate-rise" style={{ animationDelay: '40ms' }}>
        <span className="flex min-w-0 items-center gap-1.5">
          <FileText className="h-3.5 w-3.5 flex-none" />
          <code className="max-w-[240px] truncate font-mono text-[11.5px] text-fg-2" title={configPath || undefined}>{configPath || '尚未读取配置路径'}</code>
        </span>
        <Badge tone="accent">修订 <span className="num font-mono">{configRevision || '—'}</span></Badge>
        <span className="num">常用配置与原始配置共享同一文件</span>
        <span className={cn('ml-auto flex items-center gap-1.5', dirtyCount > 0 ? 'text-orange' : 'text-green')}>
          <span className={cn('dot', dirtyCount > 0 ? 'tint-orange' : 'tint-green')} data-live={dirtyCount > 0 ? '1' : undefined} />
          {dirtyCount > 0 ? `${dirtyCount} 个分节有未保存修改` : '所有修改已保存'}
        </span>
      </div>

      <div className="flex gap-1.5 overflow-x-auto pb-1 lg:hidden" aria-label="设置分节快速导航">
        {sections.map((section) => {
          const Icon = section.icon;
          const isDirty = section.id in dirty && dirty[section.id as DirtyId];
          return (
            <button key={section.id} type="button" onClick={() => navigateTo(section.id)}
              className={cn('flex flex-none items-center gap-1.5 rounded-full border px-3 py-1.5 text-[12px] transition-colors',
                active === section.id
                  ? 'tint-orange border-[rgb(var(--tint)/0.4)] bg-[rgb(var(--tint)/0.14)] text-tint'
                  : 'border-line bg-glass text-fg-3 hover:text-fg')}>
              <Icon className="h-3.5 w-3.5" />{section.label}
              {isDirty && <span className="h-1.5 w-1.5 rounded-full bg-orange" />}
            </button>
          );
        })}
      </div>

      <div className="grid gap-6 lg:grid-cols-[220px_1fr]">
        <nav aria-label="设置分节" className="hidden lg:block">
          <div className="sticky top-0 space-y-0.5 animate-rise">
            {sections.map((section) => {
              const Icon = section.icon;
              const selected = active === section.id;
              const isDirty = section.id in dirty && dirty[section.id as DirtyId];
              return (
                <button key={section.id} type="button" onClick={() => navigateTo(section.id)}
                  aria-current={selected ? 'true' : undefined}
                  className={cn('group relative flex h-9 w-full items-center gap-2.5 rounded-[10px] px-3 text-left text-[13.5px] transition-colors tint-orange',
                    selected ? 'bg-[rgb(var(--tint)/0.12)] font-medium text-tint' : 'text-fg-3 hover:bg-glass-hover hover:text-fg')}>
                  <span className={cn('absolute left-0 top-1/2 w-[3px] -translate-y-1/2 rounded-r-full bg-tint transition-all duration-300', selected ? 'h-4' : 'h-0')} />
                  <Icon className="h-4 w-4 flex-none" />
                  <span className="min-w-0 flex-1 truncate">{section.label}</span>
                  {isDirty && <span aria-hidden="true" title="有未保存修改" className="h-1.5 w-1.5 flex-none rounded-full bg-orange shadow-[0_0_8px_var(--fg-orange)]" />}
                </button>
              );
            })}

            <div className="mt-4 rounded-xl border border-line bg-glass p-3 text-[11px] text-fg-4">
              <p className="flex items-center gap-1.5 text-fg-3"><span className={cn('dot', controlOnline ? 'tint-green' : 'tint-red')} data-live={controlOnline ? '1' : undefined} />控制服务 {controlOnline ? '在线' : '离线'}</p>
              <p className="mt-1.5 flex items-center gap-1.5"><span className={cn('dot', backendRunning ? 'tint-green' : 'tint-orange')} data-live={backendRunning ? '1' : undefined} />{backendRunning ? '后端运行中' : '后端未运行'}</p>
              <p className="mt-2 border-t border-line pt-2 text-fg-4">保存配置会写入本地文件; 配置修订用于检测其它会话的并发修改。</p>
            </div>
          </div>
        </nav>

        <div className="stagger min-w-0 space-y-5">
          <AppearanceSection i={0} />

          <CommonConfigSection
            i={1}
            draft={commonDraft}
            saved={commonSaved}
            revision={configRevision || '—'}
            path={configPath}
            exists={configExists}
            controlOnline={controlOnline}
            backendRunning={backendRunning}
            loading={loading}
            saving={saving}
            onChange={setCommonDraft}
            onSave={saveCommon}
            onReload={loadConfig}
            onCreateDefault={createDefault}
            onDirty={dirtyReporters.common}
            register={registerers.common}
          />

          <BackendControlSection
            i={2}
            host={commonDraft.host}
            port={commonDraft.port}
            commonDirty={commonDirty}
          />

          <AccessPasswordPanel i={3} active={active === 'password'} />

          <AdministratorsPanel
            i={4}
            active={active === 'admins'}
            backendRunning={backendRunning}
            onDirty={dirtyReporters.admins}
            onSaved={handleAdminSaved}
            register={registerers.admins}
          />

          <LogRetentionPanel
            i={5}
            active={active === 'logs'}
            onDirty={dirtyReporters.logs}
            register={registerers.logs}
          />

          <DataMaintenancePanel
            i={6}
            active={active === 'data'}
            backendRunning={backendRunning}
            controlAvailable={controlOnline}
          />

          <RawConfigSection
            i={7}
            active={active === 'raw'}
            draft={rawDraft}
            saved={rawSaved}
            revision={configRevision || '—'}
            path={configPath}
            backendRunning={backendRunning}
            controlOnline={controlOnline}
            onChange={setRawDraft}
            onSave={saveRaw}
            onReload={() => { void reloadRaw(); }}
            onDirty={dirtyReporters.raw}
            register={registerers.raw}
          />

          <AboutSection
            i={8}
            active={active === 'about'}
            configPath={configPath}
            revision={configRevision || '—'}
            backendRunning={backendRunning}
            controlOnline={controlOnline}
          />
        </div>
      </div>

      <div className={cn('pointer-events-none fixed bottom-5 left-1/2 z-50 -translate-x-1/2 transition-all duration-500 [transition-timing-function:var(--ease-spring)] md:left-[calc(50%+124px)]',
        dirtyCount > 0 ? 'translate-y-0 opacity-100' : 'translate-y-24 opacity-0')}>
        <div className="surface surface-strong pointer-events-auto flex max-w-[calc(100vw-24px)] flex-wrap items-center gap-2 !rounded-2xl py-2.5 pl-5 pr-2.5 !shadow-[var(--shadow-3)] sm:gap-4" role="region" aria-label="未保存的修改">
          <span className="flex flex-none items-center gap-2 text-sm text-fg-2">
            <span className="dot tint-orange" data-live="1" />
            {dirtyCount} 个分节有未保存修改
          </span>
          <span className="hidden max-w-[240px] flex-wrap gap-1 sm:flex">
            {dirtyOrder.filter((id) => dirty[id]).map((id) => <Badge key={id} tone="orange">{dirtyLabels[id]}</Badge>)}
          </span>
          <Button variant="ghost" size="sm" onClick={undoAll} disabled={savingAll || saving}>全部撤销</Button>
          <Button variant="primary" size="sm" loading={savingAll} onClick={() => void saveAll()}>保存全部</Button>
        </div>
      </div>
    </div>
  );
}
