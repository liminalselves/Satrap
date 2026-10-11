import { useCallback, useEffect, useState } from 'react';
import { controlApi } from '@/api/control';
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
      <p className={result.status === 'error' ? 'text-error' : 'text-text-secondary'}>{result.target}：{result.status === 'applied' ? '运行实例已应用' : result.status === 'next_activation' ? '下次激活时应用' : '应用失败'}{result.error && ` · ${result.error}`}</p>
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
    setBusy(true); setError('');
    try { setRuntime((await controlApi.reconcilePlugins({ plugin_name: name })).runtime); }
    catch (err) { setError(pluginError(err)); }
    finally { setBusy(false); }
  }, [name]);
  const save = async () => {
    if (!data) return;
    setBusy(true); setError(''); setSaved(false);
    try {
      const result = await controlApi.saveGlobalPluginConfig(name, draft, data.revision);
      globalPluginRead(name).invalidate();
      setData(result); setDraft(result.overrides); setRuntime(result.runtime || []); setSaved(true);
    } catch (err) { setError(pluginError(err)); }
    finally { setBusy(false); }
  };
  return <Card className="space-y-4">
    <p className="text-sm text-text-secondary">全局参数供各使用位置继承。命名配置和会话自定义值会覆盖对应字段。</p>
    {error && <p role="alert" className="text-error">{error}</p>}
    {saved && <p role="status" className="text-success">全局参数已保存</p>}
    {options.modelError && <p role="alert" className="text-error">模型选项读取失败: {options.modelError}</p>}
    {options.knowledgeError && <p role="alert" className="text-error">知识库选项读取失败: {options.knowledgeError}</p>}
    {data ? <>
      {Object.keys(data.schema).length ? <PluginConfigFields schema={data.schema} values={draft} modelOptions={options.models} knowledgeBases={options.bases} modelState={options.modelState} knowledgeState={options.knowledgeState} disabled={busy} onChange={(key, value) => { setSaved(false); setDraft((previous) => ({ ...previous, [key]: value })); }} /> : <p className="text-text-tertiary">此插件没有可配置的全局参数</p>}
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
