import { useConfirm } from '@/hooks/useConfirm';
import { useEffect, useState, useCallback, useMemo, useRef } from 'react';
import { useShallow } from 'zustand/react/shallow';
import { useConfigWatch } from '@/hooks/useConfigWatch';
import { useConfigStore } from '@/stores/useConfigStore';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { Badge } from '@/components/ui/Badge';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/Tabs';
import { toast } from '@/components/ui/Toast';
import { MODEL_TYPES } from '@/utils/constants';
import { LLMConfigModal } from '@/components/common/LLMConfigModal';
import { PageHeader, FormModal, FormField, EmptyState } from '@/components/common';
import { Plus, Edit2, Trash2, Eye, EyeOff, Mic } from 'lucide-react';
import { Modal } from '@/components/ui/Modal';
import { controlApi, type AsrTestResult } from '@/api/control';
import type { LLMConfig, EmbeddingConfig, ReRankConfig, ASRConfig, ModelType } from '@/api/types';

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

type ModelConfig = LLMConfig | EmbeddingConfig | ReRankConfig | ASRConfig;

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
  } = useConfigStore(useShallow((state) => ({ llmConfigs: state.llmConfigs, embeddingConfigs: state.embeddingConfigs,
    rerankConfigs: state.rerankConfigs, asrConfigs: state.asrConfigs, createModel: state.createModel, updateModel: state.updateModel, deleteModel: state.deleteModel })));

  const [activeTab, setActiveTab] = useState<ModelType>('llm');
  const [showModal, setShowModal] = useState(false);
  const [editingName, setEditingName] = useState<string | null>(null);
  const [showApiKey, setShowApiKey] = useState<Record<string, boolean>>({});
  const [formData, setFormData] = useState<ModelFormData>({ name: '' });
  const [asrTestName, setAsrTestName] = useState<string | null>(null);

  // 获取当前类型的配置
  const configs = useMemo(() => {
    switch (activeTab) {
      case 'llm': return llmConfigs;
      case 'embedding': return embeddingConfigs;
      case 'asr': return asrConfigs;
      case 'rerank': return rerankConfigs;
    }
  }, [activeTab, llmConfigs, embeddingConfigs, rerankConfigs, asrConfigs]);

  const handleAdd = useCallback(() => {
    setEditingName(null);
    setFormData({
      name: 'default',
    });
    setShowModal(true);
  }, []);

  const handleEdit = useCallback((name: string, config: ModelConfig) => {
    setEditingName(name);
    setFormData({
      ...config,
      name,
    });
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
      Object.entries(config).filter(([_, v]) => v !== undefined && v !== '')
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

  const toggleApiKeyVisibility = useCallback((name: string) => {
    setShowApiKey((prev) => ({ ...prev, [name]: !prev[name] }));
  }, []);

  const handleFieldChange = useCallback((key: string, value: unknown) => {
    setFormData((prev) => ({ ...prev, [key]: key === 'dimensions' && value === undefined ? null : value }));   // 显式传 null 清除旧维度, 省略字段会保留后端原值
  }, []);

  // 表单字段
  const formFields: FormField[] = useMemo(() => [
    { key: 'name', label: '配置名称', required: true }, ...FIELD_META[activeTab],
  ], [activeTab]);

  const handleLLMSave = async (config: LLMConfig) => {
    const ok = editingName ? await updateModel('llm', editingName, config) : await createModel('llm', config.name, config);
    toast(ok ? 'success' : 'error', ok ? (editingName ? '已更新' : '已创建') : '操作失败');
    return ok;
  };

  // 当前类型标签
  const currentTypeLabel = useMemo(() => 
    MODEL_TYPES.find(t => t.value === activeTab)?.label || activeTab,
    [activeTab]
  );

  return (
    <div className="space-y-6">
      <PageHeader
        title="模型配置"
        description="管理 LLM、Embedding、ReRank 和 ASR 模型配置"
        actions={
          <Button variant="primary" onClick={handleAdd}>
            <Plus className="h-4 w-4 mr-2" />
            新增配置
          </Button>
        }
      />

      <Tabs defaultValue="llm" onValueChange={(v: string) => setActiveTab(v as ModelType)}>
        <TabsList>
          {MODEL_TYPES.map((t) => (
            <TabsTrigger key={t.value} value={t.value}>
              {t.label}
            </TabsTrigger>
          ))}
        </TabsList>

        {MODEL_TYPES.map((t) => (
          <TabsContent key={t.value} value={t.value}>
            {Object.keys(configs).length === 0 ? (
              <EmptyState
                title="暂无配置"
                description="点击上方按钮新增配置"
              />
            ) : (
              <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                {Object.entries(configs).map(([name, config]) => (
                  <ModelCard
                    key={name}
                    name={name}
                    config={config}
                    typeLabel={t.label}
                    showApiKey={showApiKey[name]}
                    onToggleApiKey={() => toggleApiKeyVisibility(name)}
                    onEdit={() => handleEdit(name, config)}
                    onDelete={() => handleDelete(name)}
                    onTest={t.value === 'asr' ? () => setAsrTestName(name) : undefined}
                  />
                ))}
              </div>
            )}
          </TabsContent>
        ))}
      </Tabs>

      <AsrTestModal name={asrTestName} onClose={() => setAsrTestName(null)} />

      {/* 编辑/新增模态框 */}
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

// 模型卡片组件
interface ModelCardProps {
  name: string;
  config: ModelConfig;
  typeLabel: string;
  showApiKey: boolean;
  onToggleApiKey: () => void;
  onEdit: () => void;
  onDelete: () => void;
  onTest?: () => void;
}

function ModelCard({
  name,
  config,
  typeLabel,
  showApiKey,
  onToggleApiKey,
  onEdit,
  onDelete,
  onTest,
}: ModelCardProps) {
  return (
    <Card className="relative">
      <div className="flex items-start justify-between mb-3">
        <div>
          <h3 className="font-semibold text-text-primary">{name}</h3>
          <p className="text-sm text-text-secondary mt-1">
            {config.model || config.base_url || '-'}
          </p>
        </div>
        <Badge variant="info">{typeLabel}</Badge>
      </div>

      {config.api_key && (
        <div className="flex items-center gap-2 mb-3 text-sm">
          <span className="text-text-tertiary">API Key:</span>
          <code className="flex-1 px-2 py-1 rounded bg-glass text-text-secondary">
            {showApiKey ? config.api_key : '••••••••'}
          </code>
          <button
            onClick={onToggleApiKey}
            className="text-text-tertiary hover:text-text-primary"
          >
            {showApiKey ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
          </button>
        </div>
      )}

      <div className="flex gap-2 mt-4">
        <Button
          variant="default"
          size="sm"
          className="flex-1"
          onClick={onEdit}
        >
          <Edit2 className="h-3 w-3 mr-1" />
          编辑
        </Button>
        {onTest && (
          <Button variant="default" size="sm" onClick={onTest} aria-label={`测试转录 ${name}`}>
            <Mic className="h-3 w-3 mr-1" />
            测试转录
          </Button>
        )}
        <Button
          variant="danger"
          size="sm"
          onClick={onDelete}
        >
          <Trash2 className="h-3 w-3" />
        </Button>
      </div>
    </Card>
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
          <div className="rounded bg-glass p-3 text-sm space-y-1" role="status">
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
