import { useEffect, useState } from 'react';
import { pluginSettingsApi, type PluginSettingsContext, type SessionPluginSettings } from '@/api/pluginSettings';
import { PluginConfigFields } from './PluginConfigFields';
import type { ConfigOption } from './PluginConfigFields';
import { ragApi } from '@/api/rag';
import { Modal } from '@/components/ui/Modal';
import { Button } from '@/components/ui/Button';

interface Props {
  context: PluginSettingsContext | null;
  plugins: string[];
  onClose: () => void;
}

export function SessionPluginSettingsModal({ context, plugins, onClose }: Props) {
  const [plugin, setPlugin] = useState('');
  const [data, setData] = useState<SessionPluginSettings | null>(null);
  const [overrides, setOverrides] = useState<Record<string, unknown>>({});
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [saving, setSaving] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const [knowledgeBases, setKnowledgeBases] = useState<ConfigOption[]>([]);
  const selected = plugins.includes(plugin) ? plugin : plugins[0] || '';
  const platformId = context?.platformId;
  const sessionId = context?.sessionId;

  useEffect(() => {
    setData(null);
    setKnowledgeBases([]);
    setError('');
    setNotice('');
    if (!platformId || !sessionId || !selected) return;
    let cancelled = false;
    pluginSettingsApi.get({ platformId, sessionId }, selected).then(async (result) => {
      if (cancelled) return;
      setData(result);
      setOverrides(result.overrides);
      if (Object.values(result.schema).some((field) => field.type.startsWith('knowledge_base'))) {
        const libraries = await ragApi.list({ platformId, sessionId });
        if (!cancelled) setKnowledgeBases(libraries.knowledge_bases.map((item) => ({ value: item.id, label: item.name, scope: item.scope })));
      }
    }).catch((reason) => { if (!cancelled) setError(reason.response?.data?.error || reason.message || String(reason)); });
    return () => { cancelled = true; };
  }, [platformId, sessionId, selected, refresh]);

  const save = async () => {
    if (!context || !data) return;
    setSaving(true);
    setError('');
    try {
      const result = await pluginSettingsApi.save(context, selected, overrides, data.revision);
      setData(result);
      setOverrides(result.overrides);
      setNotice(result.runtime?.ok === false ? '参数已保存, 运行时应用失败, 请检查配置' : result.runtime?.ok ? '已保存并应用' : '已保存, 将在下一轮或下次激活时应用');
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : String(reason);
      setError(message);
    } finally { setSaving(false); }
  };

  return <Modal open={context !== null} title="会话插件参数" onClose={() => { if (!saving) onClose(); }} size="lg">
    <div className="space-y-4">
      <p className="text-sm text-text-secondary">仅保存当前会话主动修改的参数，其余参数继续继承。{sessionId}</p>
      <select className="glass-input w-full" value={selected} disabled={saving} onChange={(event) => setPlugin(event.target.value)}>
        {plugins.length === 0 && <option value="">没有可配置的插件</option>}
        {plugins.map((name) => <option key={name} value={name}>{name}</option>)}
      </select>
      {error && <p role="alert" className="text-sm text-error">{error}</p>}
      {notice && <p role="status" className="text-sm text-text-secondary">{notice}</p>}
      {data ? <PluginConfigFields schema={data.schema} values={overrides} inherited={data.inherited} sources={data.inherited_sources || data.sources} modelOptions={data.model_options} knowledgeBases={knowledgeBases} sessionMode disabled={saving}
        onChange={(key, value) => setOverrides((current) => ({ ...current, [key]: value }))}
        onReset={(key) => setOverrides((current) => { const next = { ...current }; delete next[key]; return next; })} />
        : selected && !error && <p className="text-sm text-text-tertiary">加载中...</p>}
      <div className="flex justify-end gap-2">
        <Button variant="ghost" disabled={saving} onClick={() => setRefresh((value) => value + 1)}>重新加载</Button>
        <Button variant="ghost" disabled={!data || saving} onClick={() => setOverrides({})}>全部恢复继承</Button>
        <Button variant="primary" disabled={!data || saving} onClick={save}>{saving ? '保存中...' : '保存'}</Button>
      </div>
    </div>
  </Modal>;
}
