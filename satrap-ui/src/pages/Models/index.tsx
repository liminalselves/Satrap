import { useConfirm } from '@/hooks/useConfirm';
import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from 'react';
import { useShallow } from 'zustand/react/shallow';
import { useConfigWatch } from '@/hooks/useConfigWatch';
import { useConfigStore } from '@/stores/useConfigStore';
import { Card as ModelCardSurface } from '@/components/ui/Card';
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/Tabs';
import { Badge, Button, Card, EmptyState, IconButton, Input, PageHeader } from '@/components/kit';
import { toast } from '@/components/ui/Toast';
import { MODEL_TYPES } from '@/utils/constants';
import { LLMConfigModal } from '@/components/common/LLMConfigModal';
import { FormModal, type FormField } from '@/components/common';
import {
  AudioLines, Boxes, Cpu, Edit2, Eye, EyeOff, LayoutGrid, Layers, List, Mic, Plus, Search, Sparkles, Star, Trash2,
} from 'lucide-react';
import { Modal } from '@/components/ui/Modal';
import { controlApi, type AsrTestResult } from '@/api/control';
import { sourceStateLabels, statsApi, type ModelsResponse, type ModelMetrics, type SourceCoverage } from '@/api/contracts';
import { startVisiblePoll } from '@/utils/visiblePoll';
import { cn } from '@/utils/cn';
import type { Tint } from '@/components/kit';
import type { ASRConfig, EmbeddingConfig, LLMConfig, ModelConfig, ModelType, ReRankConfig } from '@/api/types';

interface ModelFormData {
  name: string;
  model?: string;
  base_url?: string;
  api_key?: string;
  dimensions?: number | null;
  max_batch_size?: number;
  top_k?: number;
  min_score?: number;
  language?: string;
  prompt?: string;
  timeout?: number;
}

const FIELD_META: Record<ModelType, FormField[]> = {
  llm: [],
  embedding: [
    { key: 'model', label: '模型' },
    { key: 'base_url', label: 'Base URL' },
    { key: 'api_key', label: 'API Key', type: 'password' },
    { key: 'dimensions', label: 'Dimensions', type: 'number', placeholder: '留空使用模型默认维度，不发送 dimensions 参数' },
    { key: 'max_batch_size', label: 'Max Batch Size', type: 'number' },
  ],
  rerank: [
    { key: 'model', label: '模型' },
    { key: 'base_url', label: 'Base URL' },
    { key: 'api_key', label: 'API Key', type: 'password' },
    { key: 'top_k', label: 'Top K', type: 'number' },
    { key: 'min_score', label: 'Min Score', type: 'number' },
  ],
  asr: [
    { key: 'model', label: '模型' },
    { key: 'base_url', label: 'Base URL' },
    { key: 'api_key', label: 'API Key', type: 'password' },
    { key: 'language', label: '识别语言', placeholder: '留空由服务端自动检测, 如 zh / en' },
    { key: 'prompt', label: '提示词', placeholder: '可选, 按服务端能力透传' },
    { key: 'timeout', label: '超时秒数', type: 'number', placeholder: '留空使用默认 60 秒' },
  ],
};

const KIND_META: Record<ModelType, { icon: ReactNode; tint: Tint }> = {
  llm: { icon: <Sparkles className="h-3.5 w-3.5" />, tint: 'purple' },
  embedding: { icon: <Layers className="h-3.5 w-3.5" />, tint: 'teal' },
  rerank: { icon: <Boxes className="h-3.5 w-3.5" />, tint: 'orange' },
  asr: { icon: <AudioLines className="h-3.5 w-3.5" />, tint: 'pink' },
};

const SERVICE_NAMES: Record<string, string> = { control: '控制服务', backend: '后端服务', chat: 'Chat 服务' };

function finite(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function callsLabel(calls: number | null): string {
  return calls === null ? '—' : calls.toLocaleString();
}

function sourceTone(state: SourceCoverage['state']): 'green' | 'orange' | 'red' {
  if (state === 'available') return 'green';
  if (state === 'stale' || state === 'auth_required') return 'orange';
  return 'red';
}

function tagsFor(type: ModelType, config: ModelConfig): string[] {
  switch (type) {
    case 'llm': {
      const llm = config as LLMConfig;
      return [
        llm.supports_visual_input ? '视觉' : '',
        llm.thinking_fields?.length ? '思考' : '',
        llm.lock_api_key ? '密钥锁定' : '',
      ].filter(Boolean).slice(0, 3);
    }
    case 'embedding': {
      const embedding = config as EmbeddingConfig;
      return embedding.dimensions ? [`${embedding.dimensions} 维`] : [];
    }
    case 'rerank': {
      const rerank = config as ReRankConfig;
      return rerank.top_k ? [`Top ${rerank.top_k}`] : [];
    }
    case 'asr': {
      const asr = config as ASRConfig;
      return asr.language ? [`语言 ${asr.language}`] : [];
    }
  }
}

function UsageCoverage({ usage }: { usage: ModelsResponse | null }) {
  if (!usage || usage.complete !== false) return null;
  const sources = Array.isArray(usage.sources) ? usage.sources.filter((source) => source && (source.state !== 'available' || source.complete === false)) : [];
  const warnings = Array.isArray(usage.warnings) ? usage.warnings : [];
  return (
    <div className="flex flex-wrap items-center gap-2 text-[11px] text-fg-3">
      <Badge tone="orange" dot>近 7 日用量数据不完整</Badge>
      {sources.map((source) => (
        <Badge key={source.service} tone={sourceTone(source.state)}>
          {SERVICE_NAMES[source.service] ?? source.service} · {sourceStateLabels[source.state] ?? source.state}
        </Badge>
      ))}
      {warnings.length > 0 && <span title={warnings.join('\n')} className="cursor-help underline decoration-dotted underline-offset-2">查看提示</span>}
    </div>
  );
}

export function Models() {
  const confirm = useConfirm();
  useConfigWatch(['llm', 'embedding', 'rerank', 'asr']);
  const {
    llmConfigs,
    embeddingConfigs,
    rerankConfigs,
    asrConfigs,
    createModel,
    updateModel,
    deleteModel,
  } = useConfigStore(useShallow((state) => ({
    llmConfigs: state.llmConfigs, embeddingConfigs: state.embeddingConfigs, rerankConfigs: state.rerankConfigs, asrConfigs: state.asrConfigs,
    createModel: state.createModel, updateModel: state.updateModel, deleteModel: state.deleteModel,
  })));

  const [activeTab, setActiveTab] = useState<ModelType>('llm');
  const [showModal, setShowModal] = useState(false);
  const [editingName, setEditingName] = useState<string | null>(null);
  const [formData, setFormData] = useState<ModelFormData>({ name: '' });
  const [asrTestName, setAsrTestName] = useState<string | null>(null);
  const [q, setQ] = useState('');
  const [view, setView] = useState<'grid' | 'list'>('grid');
  const [usage, setUsage] = useState<ModelsResponse | null>(null);

  useEffect(() => {
    const poll = startVisiblePoll(async (signal) => {
      try {
        const data = await statsApi.models({ limit: 100 }, signal);
        if (!signal.aborted) setUsage(data);
      } catch {
        // 用量统计不可用时保持卡片可用, 调用次数显示为未知
      }
    }, { interval: 60000 });
    return () => poll.stop();
  }, []);

  const counts = useMemo<Record<ModelType, number>>(() => ({
    llm: Object.keys(llmConfigs).length,
    embedding: Object.keys(embeddingConfigs).length,
    rerank: Object.keys(rerankConfigs).length,
    asr: Object.keys(asrConfigs).length,
  }), [llmConfigs, embeddingConfigs, rerankConfigs, asrConfigs]);

  const configs = useMemo<Record<string, ModelConfig>>(() => {
    switch (activeTab) {
      case 'llm': return llmConfigs;
      case 'embedding': return embeddingConfigs;
      case 'asr': return asrConfigs;
      case 'rerank': return rerankConfigs;
    }
  }, [activeTab, llmConfigs, embeddingConfigs, rerankConfigs, asrConfigs]);

  const usageModels = useMemo<ModelMetrics[]>(() => (Array.isArray(usage?.models) ? usage.models : []), [usage]);
  const usageByConfig = useMemo(() => {
    const map = new Map<string, ModelMetrics>();
    for (const item of usageModels) if (item?.model_config) map.set(item.model_config, item);
    return map;
  }, [usageModels]);

  const callsFor = useCallback((name: string, config: ModelConfig): number | null => {
    const direct = usageByConfig.get(name);
    const directCalls = direct ? finite(direct.usage?.calls) : null;
    if (directCalls !== null) return directCalls;
    const modelName = (config as { model?: string }).model;
    if (!modelName) return null;
    for (const item of usageModels) {
      if (item?.model === modelName) {
        const calls = finite(item.usage?.calls);
        if (calls !== null) return calls;
      }
    }
    return null;
  }, [usageByConfig, usageModels]);

  const list = useMemo(
    () => Object.entries(configs).filter(([name]) => name.toLowerCase().includes(q.trim().toLowerCase())),
    [configs, q],
  );

  const handleAdd = useCallback(() => {
    setEditingName(null);
    setFormData({ name: 'default' });
    setShowModal(true);
  }, []);

  const handleEdit = useCallback((name: string, config: ModelConfig) => {
    setEditingName(name);
    setFormData({ ...config, name });
    setShowModal(true);
  }, []);

  const handleDelete = useCallback(async (name: string) => {
    if (!(await confirm(`确定要删除配置 "${name}" 吗？`))) return;
    const ok = await deleteModel(activeTab, name);
    toast(ok ? 'success' : 'error', ok ? '已删除' : '删除失败');
  }, [activeTab, deleteModel, confirm]);

  const handleSubmit = useCallback(async () => {
    const { name, ...config } = formData;
    // 过滤空值
    const cleanConfig = Object.fromEntries(
      Object.entries(config).filter(([, value]) => value !== undefined && value !== ''),
    );

    let ok: boolean;
    if (editingName) {
      ok = await updateModel(activeTab, editingName, { ...cleanConfig, name });
    } else {
      ok = await createModel(activeTab, name, cleanConfig);
    }

    if (ok) {
      toast('success', editingName ? '已更新' : '已创建');
      setShowModal(false);
    } else {
      toast('error', '操作失败');
    }
  }, [formData, editingName, activeTab, updateModel, createModel]);

  const handleFieldChange = useCallback((key: string, value: unknown) => {
    // 显式传 null 清除旧维度, 省略字段会保留后端原值
    setFormData((prev) => ({ ...prev, [key]: key === 'dimensions' && value === undefined ? null : value }));
  }, []);

  const formFields: FormField[] = useMemo(() => [
    { key: 'name', label: '配置名称', required: true }, ...FIELD_META[activeTab],
  ], [activeTab]);

  const handleLLMSave = async (config: LLMConfig) => {
    const ok = editingName ? await updateModel('llm', editingName, config) : await createModel('llm', config.name, config);
    toast(ok ? 'success' : 'error', ok ? (editingName ? '已更新' : '已创建') : '操作失败');
    return ok;
  };

  const currentTypeLabel = useMemo(
    () => MODEL_TYPES.find((type) => type.value === activeTab)?.label || activeTab,
    [activeTab],
  );

  return (
    <div className="space-y-6">
      <PageHeader
        title="模型配置"
        description="管理 LLM、Embedding、ReRank 与 ASR 模型"
        actions={<Button variant="primary" icon={<Plus className="h-4 w-4" />} onClick={handleAdd}>新增配置</Button>}
      />

      <div className="flex flex-wrap items-center gap-3 animate-rise" style={{ animationDelay: '60ms' }}>
        <Tabs value={activeTab} onValueChange={(value: string) => setActiveTab(value as ModelType)}>
          <TabsList>
            {MODEL_TYPES.map((type) => (
              <TabsTrigger key={type.value} value={type.value}>
                {KIND_META[type.value].icon}
                {type.label}
                <span aria-hidden className="num rounded-full bg-glass-hover px-1.5 text-[11px] leading-[18px] text-fg-3">{counts[type.value]}</span>
              </TabsTrigger>
            ))}
          </TabsList>
        </Tabs>
        <div className="ml-auto flex w-full items-center gap-2 sm:w-auto">
          <div className="w-60 max-w-full flex-1 sm:flex-none">
            <Input leading={<Search className="h-4 w-4" />} placeholder="筛选模型…" value={q}
              onChange={(event) => setQ(event.target.value)} aria-label="筛选模型" />
          </div>
          <div className="flex rounded-[10px] border border-line bg-glass p-0.5">
            <IconButton label="卡片视图" size="sm" active={view === 'grid'} onClick={() => setView('grid')}><LayoutGrid className="h-4 w-4" /></IconButton>
            <IconButton label="列表视图" size="sm" active={view === 'list'} onClick={() => setView('list')}><List className="h-4 w-4" /></IconButton>
          </div>
        </div>
      </div>

      <UsageCoverage usage={usage} />

      {list.length === 0 ? (
        <Card padding="none" className="animate-rise">
          <EmptyState tint="purple" icon={<Cpu className="h-6 w-6" />}
            title={q ? `没有匹配 “${q}” 的模型` : `还没有 ${currentTypeLabel}`}
            description={q ? '换个关键字试试, 或清除筛选条件' : '添加一个模型配置后, Agent 与知识库即可选用它'}
            actions={q
              ? <Button onClick={() => setQ('')}>清除筛选</Button>
              : <Button variant="primary" icon={<Plus className="h-4 w-4" />} onClick={handleAdd}>添加第一个配置</Button>} />
        </Card>
      ) : view === 'grid' ? (
        <div key={activeTab} className="stagger grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
          {list.map(([name, config], index) => (
            <ModelCard
              key={`${activeTab}:${name}`}
              name={name}
              config={config}
              typeLabel={currentTypeLabel}
              modelType={activeTab}
              calls={callsFor(name, config)}
              index={index}
              onEdit={() => handleEdit(name, config)}
              onDelete={() => handleDelete(name)}
              onTest={activeTab === 'asr' ? () => setAsrTestName(name) : undefined}
            />
          ))}
          <button type="button" onClick={handleAdd}
            className="group grid min-h-[176px] place-items-center rounded-[var(--radius-lg)] border-2 border-dashed border-[var(--glass-border-strong)] text-fg-3 transition-all duration-300 hover:border-[rgb(var(--rgb-purple)/0.5)] hover:bg-[rgb(var(--rgb-purple)/0.06)] hover:text-purple"
            style={{ '--i': list.length } as CSSProperties}>
            <span className="flex flex-col items-center gap-2 text-sm font-medium">
              <span className="grid h-10 w-10 place-items-center rounded-xl border border-current/20 transition-transform duration-300 group-hover:rotate-90 group-hover:scale-110"><Plus className="h-5 w-5" /></span>
              新增 {currentTypeLabel}
            </span>
          </button>
        </div>
      ) : (
        <Card padding="none" className="animate-rise overflow-hidden">
          <div className="scroll overflow-x-auto">
            <table className="w-full min-w-[640px] text-sm">
              <thead>
                <tr className="border-b border-line text-left text-xs text-fg-3">
                  <th className="px-5 py-3 font-medium">名称</th>
                  <th className="px-5 py-3 font-medium">类型</th>
                  <th className="px-5 py-3 font-medium">模型 / 地址</th>
                  <th className="px-5 py-3 text-right font-medium">近 7 日调用</th>
                  <th className="px-5 py-3 font-medium">状态</th>
                  <th className="px-5 py-3 text-right font-medium">操作</th>
                </tr>
              </thead>
              <tbody className="stagger">
                {list.map(([name, config], index) => {
                  const isDefault = name === 'default';
                  return (
                    <tr key={`${activeTab}:${name}`} className="border-b border-line transition-colors last:border-0 hover:bg-glass-hover"
                      style={{ '--i': index } as CSSProperties}>
                      <td className="px-5 py-3 font-mono font-medium text-fg">
                        <span className="flex items-center gap-2">{name}{isDefault && <Star className="h-3.5 w-3.5 fill-[var(--fg-orange)] text-orange" aria-label="默认模型" />}</span>
                      </td>
                      <td className="px-5 py-3 text-fg-2">{currentTypeLabel}</td>
                      <td className="max-w-[220px] truncate px-5 py-3 font-mono text-xs text-fg-2">{config.model || config.base_url || '-'}</td>
                      <td className="num px-5 py-3 text-right text-fg-2">{callsLabel(callsFor(name, config))}</td>
                      <td className="px-5 py-3"><Badge tone={isDefault ? 'orange' : 'green'} dot>{isDefault ? '默认' : '可用'}</Badge></td>
                      <td className="px-5 py-3">
                        <div className="flex items-center justify-end gap-1.5">
                          <Button size="sm" variant="ghost" onClick={() => handleEdit(name, config)} title="编辑" icon={<Edit2 className="h-3 w-3" />}>编辑</Button>
                          {activeTab === 'asr' && (
                            <Button size="sm" variant="ghost" onClick={() => setAsrTestName(name)} aria-label={`测试转录 ${name}`} icon={<Mic className="h-3 w-3" />}>测试</Button>
                          )}
                          <IconButton label={`删除 ${name}`} size="sm" onClick={() => handleDelete(name)} className="hover:text-red"><Trash2 className="h-3.5 w-3.5" /></IconButton>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </Card>
      )}

      <AsrTestModal name={asrTestName} onClose={() => setAsrTestName(null)} />

      {showModal && activeTab === 'llm' ? <LLMConfigModal
        key={editingName ?? ''} modelName={editingName ?? ''} defaultName="default" allowRename
        initialConfig={editingName ? llmConfigs[editingName] : undefined} onClose={() => setShowModal(false)} onSave={handleLLMSave}
      /> : <FormModal
        open={showModal}
        onClose={() => setShowModal(false)}
        title={editingName ? `编辑 ${currentTypeLabel}` : `新增 ${currentTypeLabel}`}
        fields={formFields}
        values={formData as unknown as Record<string, unknown>}
        onChange={handleFieldChange}
        onSubmit={handleSubmit}
        submitText={editingName ? '保存修改' : '创建'}
        size="lg"
      />}
    </div>
  );
}

interface ModelCardProps {
  name: string;
  config: ModelConfig;
  typeLabel: string;
  modelType: ModelType;
  calls: number | null;
  index: number;
  onEdit: () => void;
  onDelete: () => void;
  onTest?: () => void;
}

function ModelCard({
  name,
  config,
  typeLabel,
  modelType,
  calls,
  index,
  onEdit,
  onDelete,
  onTest,
}: ModelCardProps) {
  const [apiKey, setApiKey] = useState<string | null>(null);
  const [readingKey, setReadingKey] = useState(false);
  const keyRequest = useRef(0);
  useEffect(() => {
    keyRequest.current += 1;
    setApiKey(null);
    setReadingKey(false);
    return () => { keyRequest.current += 1; };
  }, [modelType, name, config]);   // 配置刷新或卡片卸载后清除明文, 不进入全局配置缓存
  const toggleApiKey = async () => {
    if (apiKey !== null) { keyRequest.current += 1; setApiKey(null); return; }
    const request = ++keyRequest.current;
    setReadingKey(true);
    try {
      const value = await controlApi.revealModelApiKey(modelType, name);
      if (request === keyRequest.current) setApiKey(value);
    } catch {
      if (request === keyRequest.current) toast('error', '读取 API Key 失败，请重试');
    } finally {
      if (request === keyRequest.current) setReadingKey(false);
    }
  };
  const isDefault = name === 'default';
  const tags = tagsFor(modelType, config);
  const kind = KIND_META[modelType];

  return (
    <ModelCardSurface interactive className={cn('relative flex min-w-0 flex-col', `tint-${kind.tint}`)} style={{ '--i': index } as CSSProperties}>
      <div className="flex items-start gap-3">
        <div className="grid h-10 w-10 flex-none place-items-center rounded-xl bg-[rgb(var(--tint)/0.16)] text-[13px] font-bold text-tint shadow-[inset_0_1px_0_var(--glass-highlight)]">
          {name.slice(0, 1).toUpperCase()}
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <h3 className="truncate font-mono text-[14px] font-semibold text-fg" title={name}>{name}</h3>
            {isDefault && <Star className="h-3.5 w-3.5 flex-none fill-[var(--fg-orange)] text-orange" aria-label="默认模型" />}
          </div>
          <p className="truncate text-xs text-fg-3" title={config.model || config.base_url}>{config.model || config.base_url || '-'}</p>
        </div>
        <Badge tone={kind.tint}>{typeLabel}</Badge>
      </div>

      <div className="mt-4 flex min-h-[22px] flex-wrap gap-1.5">
        {tags.map((tag) => <Badge key={tag}>{tag}</Badge>)}
      </div>

      {config.api_key && (
        <div className="mb-1 mt-4 flex min-w-0 items-center gap-2 text-sm">
          <span className="shrink-0 whitespace-nowrap text-xs text-fg-3">API Key:</span>
          <input readOnly aria-label={`API Key ${name}`} autoComplete="off" spellCheck={false}
            className="w-0 min-w-0 flex-1 rounded bg-glass px-2 py-1 font-mono text-xs text-fg-2"
            value={apiKey ?? '•'.repeat(config.api_key.length)} />
          <button type="button" onClick={() => void toggleApiKey()} disabled={readingKey}
            aria-label={`${apiKey !== null ? '隐藏' : '显示'} API Key ${name}`} aria-busy={readingKey}
            className="shrink-0 text-fg-3 transition-colors hover:text-fg disabled:opacity-50">
            {apiKey !== null ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
          </button>
        </div>
      )}

      <div className="mt-auto pt-4">
        <div className="flex items-center justify-between border-t border-line pt-3.5">
          <span className="text-xs text-fg-3">近 7 日调用 <span className="num font-medium text-fg-2">{callsLabel(calls)}</span></span>
          <Badge tone={isDefault ? 'orange' : 'green'} dot>{isDefault ? '默认' : '可用'}</Badge>
        </div>
        <div className="mt-3 flex gap-2">
          <Button variant="default" size="sm" className="flex-1" onClick={onEdit} title="编辑" icon={<Edit2 className="h-3 w-3" />}>编辑</Button>
          {onTest && (
            <Button variant="default" size="sm" onClick={onTest} aria-label={`测试转录 ${name}`} icon={<Mic className="h-3 w-3" />}>测试转录</Button>
          )}
          <IconButton label={`删除 ${name}`} size="sm" onClick={onDelete} className="hover:text-red"><Trash2 className="h-3.5 w-3.5" /></IconButton>
        </div>
      </div>
    </ModelCardSurface>
  );
}

const ASR_TEST_MAX_BYTES = 8 * 1024 * 1024;

function readFileAsBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error('读取文件失败'));
    reader.onload = () => {
      const result = String(reader.result ?? '');
      const separator = result.indexOf(',');
      if (separator < 0) {
        reject(new Error('无法读取音频内容'));
        return;
      }
      resolve(result.slice(separator + 1));
    };
    reader.readAsDataURL(file);
  });
}

// ASR 转录测试: 由后端读取密钥并调用服务商, 浏览器只上传短音频
function AsrTestModal({ name, onClose }: { name: string | null; onClose: () => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<AsrTestResult | null>(null);
  const [error, setError] = useState<string>('');
  const requestSeq = useRef(0);

  useEffect(() => {
    requestSeq.current += 1;
    setFile(null);
    setResult(null);
    setError('');
    setRunning(false);
  }, [name]);

  const handleRun = useCallback(async () => {
    if (!name || !file) return;
    if (file.size > ASR_TEST_MAX_BYTES) {
      setError('音频超过 8 MiB 测试上限');
      return;
    }
    const seq = ++requestSeq.current;
    setRunning(true);
    setError('');
    setResult(null);
    try {
      const audio = await readFileAsBase64(file);
      const response = await controlApi.testAsrConfig(name, file.name, audio);
      // 切换配置或关闭弹窗后到达的迟到响应不再写入当前视图
      if (seq !== requestSeq.current) return;
      if (response.ok === false) {
        setError(response.error || '转录失败');
      } else {
        setResult(response);
      }
    } catch (e) {
      if (seq !== requestSeq.current) return;
      setError(e instanceof Error ? e.message : '转录请求失败');
    } finally {
      if (seq === requestSeq.current) setRunning(false);
    }
  }, [file, name]);

  return (
    <Modal open={name !== null} onClose={onClose} title={`测试 ASR 转录: ${name ?? ''}`} size="md">
      <div className="space-y-4">
        <p className="text-sm text-text-secondary">
          选择一段短音频 (不超过 8 MiB), 后端使用已保存的密钥调用转录接口; 该操作会产生一次真实调用。
        </p>
        <label className="block text-sm">
          <span className="text-text-secondary">音频文件</span>
          <input
            type="file"
            accept="audio/*,.wav,.mp3,.m4a,.ogg,.flac,.webm"
            className="mt-1 block w-full text-sm"
            aria-label="音频文件"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          />
        </label>
        {error && <p className="text-sm text-error" role="alert">{error}</p>}
        {result && (
          <div className="space-y-1 rounded bg-glass p-3 text-sm" role="status">
            <div className="text-text-tertiary">
              模型 {result.model || '-'} · 语言 {result.language || '自动'} · 音频 {result.duration ?? '-'} 秒 · 耗时 {result.elapsed_ms ?? '-'} ms{result.converted_from ? ` · 已由 ${result.converted_from} 本地转码为 wav` : ''}
            </div>
            <pre className="whitespace-pre-wrap break-words text-text-primary">{result.text || '(空转录结果)'}</pre>
          </div>
        )}
        <div className="flex justify-end gap-2">
          <Button variant="default" onClick={onClose}>关闭</Button>
          <Button variant="primary" onClick={handleRun} disabled={!file || running}>
            {running ? '转录中…' : '开始转录'}
          </Button>
        </div>
      </div>
    </Modal>
  );
}
