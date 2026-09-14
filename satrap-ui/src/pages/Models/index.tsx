import { useEffect, useState, useCallback, useMemo } from 'react';
import { useConfigStore } from '@/stores/useConfigStore';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { Badge } from '@/components/ui/Badge';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/Tabs';
import { toast } from '@/components/ui/Toast';
import {
  DEFAULT_THINKING_LEVELS,
  MODEL_TYPES,
  THINKING_FIELD_OPTIONS,
  THINKING_LEVEL_OPTIONS,
} from '@/utils/constants';
import { PageHeader, FormModal, FormField, EmptyState } from '@/components/common';
import { Plus, Edit2, Trash2, Eye, EyeOff } from 'lucide-react';
import type { LLMConfig, EmbeddingConfig, ReRankConfig } from '@/api/types';

type ModelType = 'llm' | 'embedding' | 'rerank';

interface ModelFormData {
  name: string;
  model?: string;
  base_url?: string;
  api_key?: string;
  temperature?: number;
  top_p?: number;
  max_tokens?: number;
  thinking_field_name?: string | null;
  thinking_fields?: string[];
  thinking_levels?: string[];
  omit_none_thinking_fields?: boolean;
  supports_visual_input?: boolean;
  dimensions?: number | null;
  max_batch_size?: number;
  top_k?: number;
  min_score?: number;
}

type ModelConfig = LLMConfig | EmbeddingConfig | ReRankConfig;

const FIELD_META: Record<ModelType, FormField[]> = {
  llm: [
    { key: "supports_visual_input", label: "图像与视频输入", type: "checkbox", placeholder: "声明此模型支持图片和原生视频输入" },
    { key: 'model', label: '模型' },
    { key: 'base_url', label: 'Base URL' },
    { key: 'api_key', label: 'API Key', type: 'password' },
    { key: 'temperature', label: 'Temperature', type: 'number' },
    { key: 'top_p', label: 'Top P', type: 'number' },
    { key: 'max_tokens', label: 'Max Tokens', type: 'number' },
    {
      key: 'thinking_field_name',
      label: '上下文思考字段名',
      placeholder: 'reasoning_content',
    },
    {
      key: 'thinking_fields',
      label: '思考请求字段',
      type: 'checkbox-group',
      options: [...THINKING_FIELD_OPTIONS],
    },
    {
      key: 'thinking_levels',
      label: '可用思考强度',
      type: 'checkbox-group',
      options: [...THINKING_LEVEL_OPTIONS],
    },
    {
      key: 'omit_none_thinking_fields',
      label: '关闭思考兼容',
      type: 'checkbox',
      placeholder: '关闭思考时不发送值为 none 的字段',
    },
  ],
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
};

export function Models() {
  const {
    llmConfigs,
    embeddingConfigs,
    rerankConfigs,
    fetchAllModels,
    createModel,
    updateModel,
    deleteModel,
  } = useConfigStore();

  const [activeTab, setActiveTab] = useState<ModelType>('llm');
  const [showModal, setShowModal] = useState(false);
  const [editingName, setEditingName] = useState<string | null>(null);
  const [showApiKey, setShowApiKey] = useState<Record<string, boolean>>({});
  const [formData, setFormData] = useState<ModelFormData>({ name: '' });

  useEffect(() => {
    fetchAllModels();
  }, [fetchAllModels]);

  // 获取当前类型的配置
  const configs = useMemo(() => {
    switch (activeTab) {
      case 'llm': return llmConfigs;
      case 'embedding': return embeddingConfigs;
      case 'rerank': return rerankConfigs;
    }
  }, [activeTab, llmConfigs, embeddingConfigs, rerankConfigs]);

  const handleAdd = useCallback(() => {
    setEditingName(null);
    setFormData({
      name: 'default',
      ...(activeTab === 'llm'
        ? {
          thinking_fields: [],
          thinking_levels: [...DEFAULT_THINKING_LEVELS],
          omit_none_thinking_fields: false,
          supports_visual_input: false,
        }
        : {}),
    });
    setShowModal(true);
  }, [activeTab]);

  const handleEdit = useCallback((name: string, config: ModelConfig) => {
    setEditingName(name);
    setFormData({
      ...config,
      name,
      ...(activeTab === 'llm' && (config as LLMConfig).thinking_levels === undefined
        ? { thinking_levels: [...DEFAULT_THINKING_LEVELS] }
        : {}),
    });
    setShowModal(true);
  }, [activeTab]);

  const handleDelete = useCallback(async (name: string) => {
    if (!confirm(`确定要删除配置 "${name}" 吗？`)) return;
    const ok = await deleteModel(activeTab, name);
    toast(ok ? 'success' : 'error', ok ? '已删除' : '删除失败');
  }, [activeTab, deleteModel]);

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
  const formFields: FormField[] = useMemo(() => {
    const fields = formData.thinking_fields ?? [];
    const hasThinkingLevelField = fields.includes('thinking_level') || fields.includes('reasoning_effort');
    return [
      { key: 'name', label: '配置名称', required: true },
      ...FIELD_META[activeTab].filter((field) => (
        field.key !== 'thinking_levels' || hasThinkingLevelField
      )),
    ];
  }, [activeTab, formData.thinking_fields]);

  // 当前类型标签
  const currentTypeLabel = useMemo(() => 
    MODEL_TYPES.find(t => t.value === activeTab)?.label || activeTab,
    [activeTab]
  );

  return (
    <div className="space-y-6">
      <PageHeader
        title="模型配置"
        description="管理 LLM、Embedding 和 ReRank 模型配置"
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
                  />
                ))}
              </div>
            )}
          </TabsContent>
        ))}
      </Tabs>

      {/* 编辑/新增模态框 */}
      <FormModal
        open={showModal}
        onClose={() => setShowModal(false)}
        title={editingName ? `编辑 ${currentTypeLabel}` : `新增 ${currentTypeLabel}`}
        fields={formFields}
        values={formData as unknown as Record<string, unknown>}
        onChange={handleFieldChange}
        onSubmit={handleSubmit}
        submitText={editingName ? '保存修改' : '创建'}
        size="lg"
      />
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
}

function ModelCard({
  name,
  config,
  typeLabel,
  showApiKey,
  onToggleApiKey,
  onEdit,
  onDelete,
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
