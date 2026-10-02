import { useCallback, useEffect, useState } from 'react';
import { controlApi } from '@/api/control';
import { ragApi } from '@/api/rag';
import type { GlobalPluginConfig, PluginRuntimeResult } from '@/api/types';
import { PluginConfigFields, type ConfigOption, type ModelOptions } from '@/components/common/PluginConfigFields';
import { Button } from '@/components/ui/Button';
import { Card } from '@/components/ui/Card';
import { confirmDiscard } from '@/hooks/useDirtyGuard';
import { pluginError } from './InstallPluginModal';

export function PluginRuntimeStatus({ results, onRetry, busy }: { results: PluginRuntimeResult[]; onRetry: () => void; busy: boolean }) {
  return <div className="space-y-2 rounded-lg bg-glass p-4">
    <h3 className="font-medium">运行应用结果</h3>
    {results.map((result) => <div key={result.target} className="text-sm">
      <p className={result.status === 'error' ? 'text-error' : 'text-text-secondary'}>{result.target}：{result.status === 'applied' ? '运行实例已应用' : result.status === 'next_activation' ? '下次激活时应用' : '应用失败'}{result.error && ` · ${result.error}`}</p>
      {result.sessions.map((session, index) => <p className="ml-3 break-all text-xs text-text-tertiary" key={index}>{String(session.conversation_id || session.session_id || index + 1)}：{session.ok ? '已应用' : String(session.error || '应用失败')}</p>)}
    </div>)}
    {results.some((result) => result.status === 'error') && <Button size="sm" onClick={onRetry} disabled={busy}>重试运行应用</Button>}
  </div>;
}

export function GlobalPluginSettings({ name, onDirty }: { name: string; onDirty: (dirty: boolean) => void }) {
  const [data, setData] = useState<GlobalPluginConfig>();
  const [draft, setDraft] = useState<Record<string, unknown>>({});
  const [models, setModels] = useState<ModelOptions>({});
  const [bases, setBases] = useState<ConfigOption[]>([]);
  const [error, setError] = useState('');
  const [saved, setSaved] = useState(false);
  const [busy, setBusy] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const [runtime, setRuntime] = useState<PluginRuntimeResult[]>([]);
  const dirty = !!data && JSON.stringify(draft) !== JSON.stringify(data.overrides);
  useEffect(() => { onDirty(dirty); }, [dirty, onDirty]);
  useEffect(() => {
    let disposed = false;
    setData(undefined);
    setError('');
    setSaved(false);
    Promise.all([controlApi.getGlobalPluginConfig(name), controlApi.pluginModelOptions(), ragApi.list({ platformId: 'local', via: 'control' })]).then(([config, options, knowledge]) => {
      if (disposed) return;
      setData(config); setDraft(config.overrides); setModels(options.options);
      setBases(knowledge.knowledge_bases.filter((base) => base.scope === 'global').map((base) => ({ value: base.id, label: base.name, scope: base.scope })));
    }).catch((err) => { if (!disposed) setError(pluginError(err)); });
    return () => { disposed = true; };
  }, [name, refresh]);
  const retry = useCallback(async () => {
    setBusy(true); setError('');
    try { setRuntime((await controlApi.reconcilePlugins()).runtime); }
    catch (err) { setError(pluginError(err)); }
    finally { setBusy(false); }
  }, []);
  const save = async () => {
    if (!data) return;
    setBusy(true); setError(''); setSaved(false);
    try {
      const result = await controlApi.saveGlobalPluginConfig(name, draft, data.revision);
      setData(result); setDraft(result.overrides); setRuntime(result.runtime || []); setSaved(true);
    } catch (err) { setError(pluginError(err)); }
    finally { setBusy(false); }
  };
  return <Card className="space-y-4">
    <p className="text-sm text-text-secondary">全局参数供各使用位置继承。命名配置和会话自定义值会覆盖对应字段。</p>
    {error && <p role="alert" className="text-error">{error}</p>}
    {saved && <p role="status" className="text-success">全局参数已保存</p>}
    {data ? <>
      {Object.keys(data.schema).length ? <PluginConfigFields schema={data.schema} values={draft} modelOptions={models} knowledgeBases={bases} disabled={busy} onChange={(key, value) => { setSaved(false); setDraft((previous) => ({ ...previous, [key]: value })); }} /> : <p className="text-text-tertiary">此插件没有可配置的全局参数</p>}
      <div className="flex flex-wrap gap-2">
        <Button variant="primary" disabled={!dirty || busy} onClick={() => void save()}>{busy ? '处理中…' : '保存全局参数'}</Button>
        <Button disabled={busy || !Object.keys(draft).length} onClick={() => { setDraft({}); setSaved(false); }}>恢复默认值</Button>
        <Button disabled={busy} onClick={() => { if (!dirty || confirmDiscard()) { setRuntime([]); setRefresh((value) => value + 1); } }}>重新读取</Button>
      </div>
      {dirty && <p className="text-xs text-text-tertiary">有未保存的修改</p>}
    </> : error ? <Button onClick={() => setRefresh((value) => value + 1)}>重新读取</Button> : <p role="status">正在读取全局参数…</p>}
    {runtime.length > 0 && <PluginRuntimeStatus results={runtime} onRetry={() => void retry()} busy={busy} />}
  </Card>;
}
