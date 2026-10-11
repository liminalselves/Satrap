import { useCallback, useEffect, useState } from 'react';
import { controlApi } from '@/api/control';
import { createPluginApplication, type PluginApplicationResult } from '@/api/pluginApplications';
import { ApplicationUnconfirmedError } from '@/api/runtimeApplication';
import { useRuntimeApplication } from '@/hooks/useRuntimeApplication';
import { RuntimeApplicationProgress } from '@/components/common/RuntimeApplicationProgress';
import { globalPluginRead } from '@/api/pluginReads';
import { usePluginConfigOptions } from '@/hooks/usePluginConfigOptions';
import type { GlobalPluginConfig, PluginRuntimeResult } from '@/api/types';
import { PluginConfigFields } from '@/components/common/PluginConfigFields';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { confirmDiscard } from '@/hooks/useDirtyGuard';
import { pluginError } from './InstallPluginModal';

export function PluginRuntimeStatus({ results, onRetry, busy }: { results: PluginRuntimeResult[]; onRetry: () => void; busy: boolean }) {
  return <div className="space-y-2 rounded-lg bg-glass p-4">
    <h3 className="font-medium">运行应用结果</h3>
    {results.map((result) => <div key={result.target} className="text-sm">
      <p className={result.status === 'error' ? 'text-error' : 'text-text-secondary'}>{result.target}：{result.status === 'applied' ? '运行实例已应用' : result.status === 'next_activation' ? '下次激活时应用' : result.confirmation === 'unconfirmed' ? '生效状态未确认' : '应用失败'}{result.error && ` · ${result.error}`}</p>
      {result.sessions.map((session, index) => <p className="ml-3 break-all text-xs text-text-tertiary" key={index}>{String(session.conversation_id || session.session_id || index + 1)}：{session.ok ? '已应用' : String(session.error || '应用失败')}</p>)}
    </div>)}
    {results.some((result) => result.status === 'error') && <Button size="sm" onClick={onRetry} disabled={busy}>重试运行应用</Button>}
  </div>;
}

export function GlobalPluginSettings({ name, onDirty }: { name: string; onDirty: (dirty: boolean) => void }) {
  const [data, setData] = useState<GlobalPluginConfig>();
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
      setData(config); setDraft(config.overrides);
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
      setData(result); setDraft(result.overrides); setRuntime(result.runtime || []); setSaved(true);
      if (result.runtime === undefined) setRuntime((await application.apply(operation, 'plugins')).runtime);
    } catch (err) {
      if (!(err instanceof ApplicationUnconfirmedError && err.code === 'query_stopped')) setError((stored ? '全局参数已保存, 运行时应用未确认: ' : '') + pluginError(err));
    }
    finally { setBusy(false); }
  };
  return <Card className="space-y-4">
    <p className="text-sm text-text-secondary">全局参数供各使用位置继承。命名配置和会话自定义值会覆盖对应字段。</p>
    {error && <p role="alert" className="text-error">{error}</p>}
    {saved && <p role="status" className="text-success">全局参数已保存</p>}
    {options.modelError && <p role="alert" className="text-error">模型选项读取失败: {options.modelError}</p>}
    {options.knowledgeError && <p role="alert" className="text-error">知识库选项读取失败: {options.knowledgeError}</p>}
    {data ? <>
      {Object.keys(data.schema).length ? <PluginConfigFields schema={data.schema} values={draft} modelOptions={options.models} knowledgeBases={options.bases} modelState={options.modelState} knowledgeState={options.knowledgeState} disabled={locked} onChange={(key, value) => { setSaved(false); setDraft((previous) => ({ ...previous, [key]: value })); }} /> : <p className="text-text-tertiary">此插件没有可配置的全局参数</p>}
      <div className="flex flex-wrap gap-2">
        <Button variant="primary" disabled={!dirty || locked} onClick={() => void save()}>{locked ? '处理中…' : '保存全局参数'}</Button>
        <Button disabled={locked || !Object.keys(draft).length} onClick={() => { setDraft({}); setSaved(false); }}>恢复默认值</Button>
        <Button disabled={locked} onClick={() => { if (!dirty || confirmDiscard()) { setRuntime([]); setRefresh((value) => value + 1); } }}>重新读取</Button>
      </div>
      {dirty && <p className="text-xs text-text-tertiary">有未保存的修改</p>}
    </> : error ? <Button onClick={() => setRefresh((value) => value + 1)}>重新读取</Button> : <p role="status">正在读取全局参数…</p>}
    {application.view && <RuntimeApplicationProgress id={application.view.application.id} label={application.view.application.label}
      snapshot={application.view.snapshot} resultOk={application.view.result?.ok} busy={application.view.busy} error={application.view.error}
      onQuery={() => void application.query()} completionText="运行应用已结束" />}
    {runtime.length > 0 && <PluginRuntimeStatus results={runtime} onRetry={() => void retry()} busy={locked} />}
  </Card>;
}
