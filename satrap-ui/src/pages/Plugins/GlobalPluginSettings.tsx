import { useCallback, useEffect, useState } from 'react';
import { AlertTriangle, CheckCircle2, RefreshCw, RotateCcw, Save, SlidersHorizontal } from 'lucide-react';
import { controlApi } from '@/api/control';
import { createPluginApplication, type PluginApplicationResult } from '@/api/pluginApplications';
import { ApplicationUnconfirmedError } from '@/api/runtimeApplication';
import { useRuntimeApplication } from '@/hooks/useRuntimeApplication';
import { RuntimeApplicationProgress } from '@/components/common/RuntimeApplicationProgress';
import { globalPluginRead } from '@/api/pluginReads';
import { usePluginConfigOptions } from '@/hooks/usePluginConfigOptions';
import type { GlobalPluginConfig, PluginRuntimeResult } from '@/api/types';
import { PluginConfigFields } from '@/components/common/PluginConfigFields';
import { Badge, Button, Card, CardHeader, Skeleton } from '@/components/kit';
import { cn } from '@/utils/cn';
import { confirmDiscard } from '@/hooks/useDirtyGuard';
import { pluginError } from './InstallPluginModal';
import { TealPrimary } from './PluginBits';
import { timestampText } from './pluginModel';

type GlobalConfigWithMeta = GlobalPluginConfig & { updated_at?: number | null };

export function PluginRuntimeStatus({ results, onRetry, busy }: { results: PluginRuntimeResult[]; onRetry: () => void; busy: boolean }) {
  return (
    <div className="space-y-2 rounded-xl border border-line bg-glass p-3.5">
      <h3 className="text-[13px] font-semibold text-fg">运行应用结果</h3>
      {results.map((result) => (
        <div key={result.target} className={cn('rounded-lg border border-line px-3 py-2 text-[12.5px]', result.status === 'error' && 'tint-red border-[rgb(var(--tint)/0.22)]')}>
          <p className={result.status === 'error' ? 'break-words text-red' : 'break-words text-fg-2'}>
            {result.target}：{result.status === 'applied' ? '运行实例已应用' : result.status === 'next_activation' ? '下次激活时应用' : result.confirmation === 'unconfirmed' ? '生效状态未确认' : '应用失败'}{result.error && ` · ${result.error}`}
          </p>
          {result.sessions.map((session, index) => (
            <p className="ml-3 break-all text-[11px] text-fg-3" key={index}>
              {String(session.conversation_id || session.session_id || index + 1)}：{session.ok ? '已应用' : String(session.error || '应用失败')}
            </p>
          ))}
        </div>
      ))}
      {results.some((result) => result.status === 'error') && <Button size="sm" onClick={onRetry} disabled={busy}>重试运行应用</Button>}
    </div>
  );
}

export function GlobalPluginSettings({ name, onDirty }: { name: string; onDirty: (dirty: boolean) => void }) {
  const [data, setData] = useState<GlobalConfigWithMeta>();
  const [draft, setDraft] = useState<Record<string, unknown>>({});
  const options = usePluginConfigOptions(data?.schema, true);
  const [error, setError] = useState('');
  const [saved, setSaved] = useState(false);
  const [busy, setBusy] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const [runtime, setRuntime] = useState<PluginRuntimeResult[]>([]);
  const application = useRuntimeApplication<PluginApplicationResult>();
  const locked = busy || Boolean(application.view?.busy);
  useEffect(() => {
    if (application.view?.result) { setRuntime(application.view.result.runtime); setError(''); }
  }, [application.view?.result]);
  const dirty = !!data && JSON.stringify(draft) !== JSON.stringify(data.overrides);
  useEffect(() => { onDirty(dirty); }, [dirty, onDirty]);
  useEffect(() => {
    let disposed = false;
    const controller = new AbortController();
    setData(undefined);
    setError('');
    setSaved(false);
    globalPluginRead(name).read({ signal: controller.signal }).then((config) => {
      if (disposed) return;
      setData(config as GlobalConfigWithMeta); setDraft(config.overrides);
    }).catch((err) => { if (!disposed) setError(pluginError(err)); });
    return () => { disposed = true; controller.abort(); };
  }, [name, refresh]);
  const retry = useCallback(async () => {
    if (busy || application.view?.busy) return;
    setBusy(true); setError('');
    setRuntime([]);
    try { setRuntime((await application.apply(createPluginApplication({ plugin_name: name }), 'plugins')).runtime); }
    catch (err) { if (!(err instanceof ApplicationUnconfirmedError && err.code === 'query_stopped')) setError(pluginError(err)); }
    finally { setBusy(false); }
  }, [name, busy, application.view?.busy, application.apply]);
  const save = async () => {
    if (!data || locked) return;
    setBusy(true); setError(''); setSaved(false);
    application.clear();
    setRuntime([]);
    let stored = false;
    try {
      const operation = createPluginApplication({ plugin_name: name });
      const result = await controlApi.saveGlobalPluginConfig(name, draft, data.revision,
        { applyRuntime: false, baseUrl: operation.baseUrl, signal: application.getSignal() });
      stored = true;
      globalPluginRead(name).invalidate();
      setData(result as GlobalConfigWithMeta); setDraft(result.overrides); setRuntime(result.runtime || []); setSaved(true);
      if (result.runtime === undefined) setRuntime((await application.apply(operation, 'plugins')).runtime);
    } catch (err) {
      if (!(err instanceof ApplicationUnconfirmedError && err.code === 'query_stopped')) setError((stored ? '全局参数已保存, 运行时应用未确认: ' : '') + pluginError(err));
    }
    finally { setBusy(false); }
  };
  const reread = () => {
    if (dirty && !confirmDiscard()) return;
    setRuntime([]);
    setRefresh((value) => value + 1);
  };
  const fieldCount = data ? Object.keys(data.schema).length : 0;
  const overrideCount = data ? Object.keys(data.overrides).length : 0;

  return (
    <div className="space-y-4">
      <Card padding="lg" tint="teal">
        <CardHeader
          icon={<SlidersHorizontal className="h-4 w-4" />}
          title="全局参数"
          description="全局参数供各使用位置继承；命名配置与会话自定义值会覆盖对应字段。"
          actions={saved
            ? <Badge tone="green" dot>已保存</Badge>
            : dirty
              ? <Badge tone="orange" dot>有未保存的修改</Badge>
              : <Badge tone="neutral">已同步</Badge>}
        />
        <div className="mb-4 grid gap-2 sm:grid-cols-3">
          <div className="rounded-xl border border-line bg-glass px-3 py-2">
            <div className="text-[10.5px] text-fg-4">已覆盖字段</div>
            <div className="num mt-0.5 text-[15px] font-semibold text-fg">{overrideCount} <span className="text-[11px] font-normal text-fg-4">/ {fieldCount}</span></div>
          </div>
          <div className="rounded-xl border border-line bg-glass px-3 py-2">
            <div className="text-[10.5px] text-fg-4">最近更新</div>
            <div className="mt-0.5 truncate text-[12.5px] text-fg-2">{timestampText(data?.updated_at) ?? '未知'}</div>
          </div>
          <div className="rounded-xl border border-line bg-glass px-3 py-2">
            <div className="text-[10.5px] text-fg-4">继承规则</div>
            <div className="mt-0.5 truncate text-[12.5px] text-fg-2">全局 → 命名配置 → 会话</div>
          </div>
        </div>

        {error && (
          <div role="alert" className="tint-red mb-3 flex items-start gap-2 rounded-xl border border-[rgb(var(--tint)/0.25)] bg-[rgb(var(--tint)/0.08)] px-3.5 py-2.5 text-[13px] text-fg-2">
            <AlertTriangle className="mt-0.5 h-4 w-4 flex-none text-red" /><span className="min-w-0 break-words">{error}</span>
          </div>
        )}
        {saved && <p role="status" className="mb-3 flex items-center gap-2 text-[12.5px] text-green"><CheckCircle2 className="h-3.5 w-3.5" />全局参数已保存</p>}
        {options.modelError && <p role="alert" className="mb-2 text-[12.5px] text-red">模型选项读取失败: {options.modelError}</p>}
        {options.knowledgeError && <p role="alert" className="mb-2 text-[12.5px] text-red">知识库选项读取失败: {options.knowledgeError}</p>}

        {data ? <>
          {fieldCount ? (
            <PluginConfigFields
              schema={data.schema}
              values={draft}
              modelOptions={options.models}
              knowledgeBases={options.bases}
              modelState={options.modelState}
              knowledgeState={options.knowledgeState}
              disabled={locked}
              onChange={(key, value) => { setSaved(false); setDraft((previous) => ({ ...previous, [key]: value })); }}
            />
          ) : (
            <div className="rounded-xl border border-dashed border-line px-4 py-10 text-center">
              <p className="text-[13.5px] font-medium text-fg-2">此插件没有可配置的全局参数</p>
              <p className="mt-1 text-[12px] text-fg-3">启用后即可在各使用位置按默认能力运行，不需要额外配置。</p>
            </div>
          )}
          <div className="mt-5 flex flex-wrap items-center gap-2 border-t border-line pt-4">
            <TealPrimary icon={<Save className="h-4 w-4" />} loading={busy} disabled={!dirty || locked} onClick={() => void save()}>
              {locked ? '处理中…' : '保存全局参数'}
            </TealPrimary>
            <Button icon={<RotateCcw className="h-4 w-4" />} disabled={locked || !Object.keys(draft).length} onClick={() => { setDraft({}); setSaved(false); }}>
              恢复默认值
            </Button>
            <Button icon={<RefreshCw className={cn('h-4 w-4', busy && 'animate-spin-slow')} />} disabled={locked} onClick={reread}>
              重新读取
            </Button>
            <span className="ml-auto flex items-center gap-2 text-[11.5px] text-fg-4">
              {dirty && <><span className="dot tint-orange" data-live="1" />有未保存的修改</>}
              {!dirty && !busy && <><CheckCircle2 className="h-3.5 w-3.5 text-green" />配置与已保存值一致</>}
            </span>
          </div>
        </> : error ? <Button onClick={() => setRefresh((value) => value + 1)}>重新读取</Button> : (
          <div className="space-y-3">
            <div className="grid gap-4 sm:grid-cols-2">
              {Array.from({ length: 4 }).map((_, index) => (
                <div key={index} className="space-y-2"><Skeleton className="w-1/3" /><Skeleton className="h-[38px] w-full" /></div>
              ))}
            </div>
            <p role="status" className="text-[12px] text-fg-3">正在读取全局参数…</p>
          </div>
        )}
      </Card>

      {application.view && (
        <RuntimeApplicationProgress
          id={application.view.application.id}
          label={application.view.application.label}
          snapshot={application.view.snapshot}
          resultOk={application.view.result?.ok}
          busy={application.view.busy}
          error={application.view.error}
          onQuery={() => void application.query()}
          completionText="运行应用已结束"
        />
      )}
      {runtime.length > 0 && <PluginRuntimeStatus results={runtime} onRetry={() => void retry()} busy={locked} />}
    </div>
  );
}
