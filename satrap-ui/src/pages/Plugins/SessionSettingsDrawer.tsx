import { useEffect, useState } from 'react';
import { Settings2, X } from 'lucide-react';
import { pluginSettingsApi, type PluginSettingsContext, type SessionPluginSettings } from '@/api/pluginSettings';
import { PluginConfigFields } from '@/components/common/PluginConfigFields';
import type { ConfigOption } from '@/components/common/PluginConfigFields';
import { ragApi } from '@/api/rag';
import { Button, Drawer, IconButton } from '@/components/kit';
import { errorText } from '@/utils/errorText';
import { Select as KitSelect } from '@/components/kit/Select';

interface Props {
  context: PluginSettingsContext | null;
  plugins: string[];
  onClose: () => void;
}

export function SessionSettingsDrawer({ context, plugins, onClose }: Props) {
  const [plugin, setPlugin] = useState('');
  const [data, setData] = useState<SessionPluginSettings | null>(null);
  const [overrides, setOverrides] = useState<Record<string, unknown>>({});
  const [toolOverrides, setToolOverrides] = useState<Record<string, boolean>>({});
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
      setToolOverrides(result.tool_overrides || {});
      if (Object.values(result.schema).some((field) => field.type.startsWith('knowledge_base'))) {
        const libraries = await ragApi.list({ platformId, sessionId });
        if (!cancelled) setKnowledgeBases(libraries.knowledge_bases.map((item) => ({ value: item.id, label: item.name, scope: item.scope })));
      }
    }).catch((reason) => { if (!cancelled) setError(errorText(reason, '读取会话插件参数失败')); });
    return () => { cancelled = true; };
  }, [platformId, sessionId, selected, refresh]);

  const save = async () => {
    if (!context || !data) return;
    setSaving(true);
    setError('');
    setNotice('');
    try {
      const result = await pluginSettingsApi.save(context, selected, overrides, data.revision, data.tool_descriptions ? {
        tool_overrides: toolOverrides, expected_tool_revision: data.tool_revision || 0,
      } : undefined);
      setData(result);
      setOverrides(result.overrides);
      setToolOverrides(result.tool_overrides || {});
      setNotice(result.runtime?.ok === false ? '配置已保存, 运行时应用失败, 请检查配置' : result.runtime?.ok ? '已保存并应用' : '已保存, 将在下一轮或下次激活时应用');
    } catch (reason) {
      setError(errorText(reason, '保存会话插件参数失败'));
    } finally { setSaving(false); }
  };

  return (
    <Drawer open={context !== null} onClose={() => { if (!saving) onClose(); }} label="会话插件参数" width={720}>
      <div className="flex items-start gap-3 border-b border-line px-5 py-4">
        <div className="grid h-9 w-9 flex-none place-items-center rounded-xl bg-[rgb(var(--rgb-teal)/0.14)] text-teal"><Settings2 className="h-4 w-4" /></div>
        <div className="min-w-0 flex-1">
          <h2 className="text-lg font-semibold text-fg">会话插件参数</h2>
          <p className="mt-0.5 text-[12.5px] text-fg-3">仅保存当前会话主动修改的参数，其余参数继续继承。{sessionId}</p>
        </div>
        <IconButton label="关闭" size="sm" onClick={onClose} className="-mr-1"><X className="h-4 w-4" /></IconButton>
      </div>

      <div className="scroll min-h-0 flex-1 space-y-4 px-5 py-4">
        <label className="block text-[13px] font-medium text-fg-2">
          插件
          <KitSelect className="field mt-1 h-9 w-full" value={selected} disabled={saving} onChange={(value) => setPlugin(value)}>
            {plugins.length === 0 && <option value="">没有可配置的插件</option>}
            {plugins.map((name) => <option key={name} value={name}>{name}</option>)}
          </KitSelect>
        </label>
        {error && <p role="alert" className="text-[12.5px] text-red">{error}</p>}
        {notice && <p role="status" className="text-[12.5px] text-fg-2">{notice}</p>}
        {data ? (
          <PluginConfigFields
            schema={data.schema}
            values={overrides}
            inherited={data.inherited}
            sources={data.inherited_sources || data.sources}
            modelOptions={data.model_options}
            knowledgeBases={knowledgeBases}
            sessionMode
            disabled={saving}
            onChange={(key, value) => setOverrides((current) => ({ ...current, [key]: value }))}
            onReset={(key) => setOverrides((current) => { const next = { ...current }; delete next[key]; return next; })}
          />
        ) : selected && !error && <p role="status" className="text-[12.5px] text-fg-3">加载中...</p>}

        {data?.tool_descriptions && (
          <div className="space-y-3">
            <p className="text-[13px] font-medium text-fg-2">当前会话的工具开关</p>
            {Object.entries(data.tool_descriptions).map(([name, description]) => (
              <div key={name} className="space-y-1 rounded-xl border border-line bg-glass px-3 py-2">
                <label className="flex items-center gap-2 text-[12.5px] text-fg-2">
                  <input
                    type="checkbox"
                    className="h-4 w-4 accent-[rgb(var(--rgb-teal))]"
                    disabled={saving}
                    aria-label={name}
                    checked={toolOverrides[name] ?? data.inherited_tools?.[name] ?? true}
                    onChange={(event) => setToolOverrides((current) => ({ ...current, [name]: event.target.checked }))}
                  />
                  <span>{description}</span>
                </label>
                <p className="text-[11px] text-fg-3">{name} · {Object.prototype.hasOwnProperty.call(toolOverrides, name) ? '当前会话覆盖' : '继承上层工具配置'}</p>
                {Object.prototype.hasOwnProperty.call(toolOverrides, name) && (
                  <Button size="sm" variant="ghost" disabled={saving} onClick={() => setToolOverrides((current) => {
                    const next = { ...current }; delete next[name]; return next;
                  })}>恢复工具继承</Button>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="flex justify-end gap-2 border-t border-line px-5 py-3.5">
        <Button variant="ghost" disabled={saving} onClick={() => setRefresh((value) => value + 1)}>重新加载</Button>
        <Button variant="ghost" disabled={!data || saving} onClick={() => { setOverrides({}); setToolOverrides({}); }}>全部恢复继承</Button>
        <Button variant="primary" disabled={!data || saving} onClick={save}>{saving ? '保存中...' : '保存'}</Button>
      </div>
    </Drawer>
  );
}
