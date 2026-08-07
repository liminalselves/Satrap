import { useEffect, useState } from 'react';
import { useConfigStore } from '@/stores/useConfigStore';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { Badge } from '@/components/ui/Badge';
import { Modal } from '@/components/ui/Modal';
import { Input } from '@/components/ui/Input';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/Tabs';
import { toast } from '@/components/ui/Toast';
import { MODEL_TYPES } from '@/utils/constants';
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
  dimensions?: number;
  max_batch_size?: number;
  top_k?: number;
  min_score?: number;
}

const FIELD_META: Record<ModelType, { key: keyof ModelFormData; label: string; type: 'text' | 'password' | 'number' }[]> = {
  llm: [
    { key: 'model', label: '模型', type: 'text' },
    { key: 'base_url', label: 'Base URL', type: 'text' },
    { key: 'api_key', label: 'API Key', type: 'password' },
    { key: 'temperature', label: 'Temperature', type: 'number' },
    { key: 'top_p', label: 'Top P', type: 'number' },
    { key: 'max_tokens', label: 'Max Tokens', type: 'number' },
  ],
  embedding: [
    { key: 'model', label: '模型', type: 'text' },
    { key: 'base_url', label: 'Base URL', type: 'text' },
    { key: 'api_key', label: 'API Key', type: 'password' },
    { key: 'dimensions', label: 'Dimensions', type: 'number' },
    { key: 'max_batch_size', label: 'Max Batch Size', type: 'number' },
  ],
  rerank: [
    { key: 'model', label: '模型', type: 'text' },
    { key: 'base_url', label: 'Base URL', type: 'text' },
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

  const [activeTab] = useState<ModelType>('llm');
  const [showModal, setShowModal] = useState(false);
  const [editingName, setEditingName] = useState<string | null>(null);
  const [showApiKey, setShowApiKey] = useState<Record<string, boolean>>({});
  const [formData, setFormData] = useState<ModelFormData>({ name: '' });

  useEffect(() => {
    fetchAllModels();
  }, [fetchAllModels]);

  const getConfigs = () => {
    switch (activeTab) {
      case 'llm': return llmConfigs;
      case 'embedding': return embeddingConfigs;
      case 'rerank': return rerankConfigs;
    }
  };

  const configs = getConfigs();

  const handleAdd = () => {
    setEditingName(null);
    setFormData({ name: 'default' });
    setShowModal(true);
  };

  const handleEdit = (name: string, config: LLMConfig | EmbeddingConfig | ReRankConfig) => {
    setEditingName(name);
    setFormData({ ...config, name });
    setShowModal(true);
  };

  const handleDelete = async (name: string) => {
    if (!confirm(`确定要删除配置 "${name}" 吗？`)) return;
    const ok = await deleteModel(activeTab, name);
    if (ok) {
      toast('success', '已删除');
    } else {
      toast('error', '删除失败');
    }
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const { name, ...config } = formData;
    
    // 过滤空值
    const cleanConfig = Object.fromEntries(
      Object.entries(config).filter(([_, v]) => v !== undefined && v !== '')
    );

    let ok: boolean;
    if (editingName) {
      ok = await updateModel(activeTab, editingName, cleanConfig);
    } else {
      ok = await createModel(activeTab, name, cleanConfig);
    }

    if (ok) {
      toast('success', editingName ? '已更新' : '已创建');
      setShowModal(false);
    } else {
      toast('error', '操作失败');
    }
  };

  const toggleApiKeyVisibility = (name: string) => {
    setShowApiKey((prev) => ({ ...prev, [name]: !prev[name] }));
  };

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-text-primary">模型配置</h1>
          <p className="text-text-secondary mt-1">管理 LLM、Embedding 和 ReRank 模型配置</p>
        </div>
        <Button variant="primary" onClick={handleAdd}>
          <Plus className="h-4 w-4 mr-2" />
          新增配置
        </Button>
      </div>

      <Tabs defaultValue="llm">
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
              <Card className="text-center py-12">
                <p className="text-text-secondary">暂无配置，点击上方按钮新增</p>
              </Card>
            ) : (
              <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                {Object.entries(configs).map(([name, config]) => (
                  <Card key={name} className="relative">
                    <div className="flex items-start justify-between mb-3">
                      <div>
                        <h3 className="font-semibold text-text-primary">{name}</h3>
                        <p className="text-sm text-text-secondary mt-1">
                          {config.model || config.base_url || '-'}
                        </p>
                      </div>
                      <Badge variant="info">{t.label}</Badge>
                    </div>

                    {config.api_key && (
                      <div className="flex items-center gap-2 mb-3 text-sm">
                        <span className="text-text-tertiary">API Key:</span>
                        <code className="flex-1 px-2 py-1 rounded bg-glass text-text-secondary">
                          {showApiKey[name] ? config.api_key : '••••••••'}
                        </code>
                        <button
                          onClick={() => toggleApiKeyVisibility(name)}
                          className="text-text-tertiary hover:text-text-primary"
                        >
                          {showApiKey[name] ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                        </button>
                      </div>
                    )}

                    <div className="flex gap-2 mt-4">
                      <Button
                        variant="default"
                        size="sm"
                        className="flex-1"
                        onClick={() => handleEdit(name, config)}
                      >
                        <Edit2 className="h-3 w-3 mr-1" />
                        编辑
                      </Button>
                      <Button
                        variant="danger"
                        size="sm"
                        onClick={() => handleDelete(name)}
                      >
                        <Trash2 className="h-3 w-3" />
                      </Button>
                    </div>
                  </Card>
                ))}
              </div>
            )}
          </TabsContent>
        ))}
      </Tabs>

      {/* 编辑/新增模态框 */}
      <Modal
        open={showModal}
        onClose={() => setShowModal(false)}
        title={editingName ? `编辑 ${MODEL_TYPES.find(t => t.value === activeTab)?.label}` : `新增 ${MODEL_TYPES.find(t => t.value === activeTab)?.label}`}
        size="lg"
      >
        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-sm font-medium text-text-secondary mb-1">
              配置名称
            </label>
            <Input
              value={formData.name}
              onChange={(e) => setFormData({ ...formData, name: e.target.value })}
              disabled={!!editingName}
              required
            />
          </div>

          {FIELD_META[activeTab].map((field) => (
            <div key={field.key}>
              <label className="block text-sm font-medium text-text-secondary mb-1">
                {field.label}
              </label>
              {field.type === 'password' ? (
                <Input
                  type="password"
                  value={formData[field.key] as string || ''}
                  onChange={(e) => setFormData({ ...formData, [field.key]: e.target.value })}
                  placeholder={editingName ? '留空则不修改' : ''}
                />
              ) : field.type === 'number' ? (
                <Input
                  type="number"
                  step="any"
                  value={formData[field.key] as number || ''}
                  onChange={(e) => setFormData({ ...formData, [field.key]: e.target.value ? Number(e.target.value) : undefined })}
                />
              ) : (
                <Input
                  type="text"
                  value={formData[field.key] as string || ''}
                  onChange={(e) => setFormData({ ...formData, [field.key]: e.target.value })}
                />
              )}
            </div>
          ))}

          <div className="flex gap-3 pt-4">
            <Button type="submit" variant="primary" className="flex-1">
              {editingName ? '保存修改' : '创建'}
            </Button>
            <Button type="button" variant="default" onClick={() => setShowModal(false)}>
              取消
            </Button>
          </div>
        </form>
      </Modal>
    </div>
  );
}
