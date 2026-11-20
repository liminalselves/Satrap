import { useCallback, useEffect, useState } from 'react';
import { RotateCcw, Save, Workflow } from 'lucide-react';
import { controlApi, RevisionConflictError } from '@/api/control';
import type { FlowNodeTypePayload, PluginFlowNodeDefaults } from '@/api/types';
import { PluginConfigFields } from '@/components/common/PluginConfigFields';
import { Badge, Button, Card, CardHeader, Modal, Skeleton, toast } from '@/components/kit';
import { usePluginConfigOptions } from '@/hooks/usePluginConfigOptions';
import { errorText } from '@/utils/errorText';

type Draft = Record<string, Record<string, unknown>>;

const toDraft = (data: PluginFlowNodeDefaults): Draft =>
  Object.fromEntries(Object.entries(data.nodes).map(([node, entry]) => [node, { ...entry.config }]));

/** 一个节点的默认编辑区; 拆开是为了每个节点按自己的 schema 读取模型/知识库选项 */
function NodeSection({ def, values, onChange, onReset }: {
  def: FlowNodeTypePayload; values: Record<string, unknown>;
  onChange: (key: string, value: unknown) => void; onReset: (key: string) => void;
}) {
  const options = usePluginConfigOptions(def.schema, true);
  return (
    <section aria-label={def.label} className="space-y-3 rounded-xl border border-line bg-glass p-3.5">
      <div className="flex flex-wrap items-center gap-2">
        <h4 className="text-[13px] font-semibold text-fg">{def.label}</h4>
        <Badge tone="neutral"><span className="font-mono text-[10px]">{def.type}</span></Badge>
        <Badge tone="neutral">v{def.version}</Badge>
      </div>
      {def.description && <p className="text-[12px] text-fg-3">{def.description}</p>}
      <PluginConfigFields
        schema={def.schema} values={values} namedMode
        modelOptions={options.models} knowledgeBases={options.bases} modelState={options.modelState} knowledgeState={options.knowledgeState}
        emptyText="这个节点没有可配置项"
        onChange={onChange} onReset={onReset}
      />
    </section>
  );
}

/**
 * 插件节点全局默认: 对所有流程生效, 节点和流程默认可再覆盖
 *
 * 保存时整张表替换, 用读取时的 revision 防止覆盖别处修改
 */
export function FlowNodeDefaults({ name, nodes, onDirty }: { name: string; nodes: FlowNodeTypePayload[]; onDirty: (dirty: boolean) => void }) {
  const [data, setData] = useState<PluginFlowNodeDefaults>();
  const [draft, setDraft] = useState<Draft>({});
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [conflict, setConflict] = useState(false);
  const dirty = !!data && JSON.stringify(draft) !== JSON.stringify(toDraft(data));
  useEffect(() => { onDirty(dirty); }, [dirty, onDirty]);

  const load = useCallback(async (signal?: AbortSignal) => {
    setError('');
    try {
      const result = await controlApi.getPluginFlowNodeDefaults(name, signal);
      if (signal?.aborted) return;
      setData(result);
      setDraft(toDraft(result));
    } catch (reason) {
      if (!signal?.aborted) setError(errorText(reason, '读取节点默认失败'));
    }
  }, [name]);
  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const local = (type: string) => type.slice(type.lastIndexOf('.') + 1);
  const setField = (node: string, key: string, value: unknown) => setDraft((current) => ({ ...current, [node]: { ...current[node], [key]: value } }));
  const resetField = (node: string, key: string) => setDraft((current) => {
    const next = { ...current, [node]: { ...current[node] } };
    delete next[node][key];
    if (!Object.keys(next[node]).length) delete next[node];
    return next;
  });

  const save = async (revision?: string) => {
    if (!data) return;
    setBusy(true);
    setError('');
    try {
      const versions = new Map(nodes.map((def) => [local(def.type), def.version]));
      const payload = Object.fromEntries(Object.entries(draft).filter(([, config]) => Object.keys(config).length)
        .map(([node, config]) => [node, { version: versions.get(node), config }]));
      const result = await controlApi.savePluginFlowNodeDefaults(name, payload, revision ?? data.revision);
      setData(result);
      setDraft(toDraft(result));
      toast.success('节点默认已保存', '新激活的流程会话使用新默认');
    } catch (reason) {
      if (reason instanceof RevisionConflictError) setConflict(true);
      else setError(errorText(reason, '保存节点默认失败'));
    } finally {
      setBusy(false);
    }
  };

  const overwrite = async () => {
    setConflict(false);
    try {
      const latest = await controlApi.getPluginFlowNodeDefaults(name);
      await save(latest.revision);
    } catch (reason) {
      setError(errorText(reason, '读取最新版本失败'));
    }
  };

  return (
    <Card padding="lg" className="space-y-4">
      <CardHeader
        icon={<Workflow className="h-4 w-4" />} title="流程节点默认"
        description="这个插件提供的流程节点在所有流程里的默认值。合并顺序: 类型默认 < 插件全局 < 流程默认 < 节点"
      />
      {error && <p role="alert" className="text-[12.5px] text-red">{error}</p>}
      {!data && !error ? <Skeleton className="h-32 w-full" /> : data && (
        <>
          <div className="space-y-3">
            {nodes.map((def) => (
              <NodeSection key={def.type} def={def} values={draft[local(def.type)] ?? {}}
                onChange={(key, value) => setField(local(def.type), key, value)} onReset={(key) => resetField(local(def.type), key)} />
            ))}
          </div>
          <div className="flex flex-wrap items-center justify-end gap-2">
            {dirty && <span className="mr-auto text-[12px] text-orange">有未保存的修改</span>}
            <Button size="sm" variant="ghost" icon={<RotateCcw className="h-3.5 w-3.5" />} disabled={!dirty || busy} onClick={() => setDraft(toDraft(data))}>放弃修改</Button>
            <Button size="sm" variant="tinted" tint="teal" icon={<Save className="h-3.5 w-3.5" />} loading={busy} disabled={!dirty} onClick={() => void save()}>保存节点默认</Button>
          </div>
        </>
      )}
      <Modal
        open={conflict} onClose={() => setConflict(false)} title="节点默认已被别处修改" size="sm" className="tint-orange"
        footer={<>
          <Button variant="ghost" onClick={() => { setConflict(false); void load(); }}>重新加载</Button>
          <Button variant="danger" onClick={() => void overwrite()}>覆盖</Button>
        </>}
      >
        <p className="text-[13px] leading-6 text-fg-2">重新加载会丢弃本页修改; 覆盖会用本页内容替换别处保存的版本。</p>
      </Modal>
    </Card>
  );
}
