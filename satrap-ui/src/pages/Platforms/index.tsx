import { useEffect, useState } from 'react';
import { useBackendStore } from '@/stores/useBackendStore';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { Badge } from '@/components/ui/Badge';
import { Modal } from '@/components/ui/Modal';
import { Input } from '@/components/ui/Input';
import { Select } from '@/components/ui/Select';
import { toast } from '@/components/ui/Toast';
import { PLATFORM_TYPES } from '@/utils/constants';
import { Plus, Edit2, Trash2, RefreshCw } from 'lucide-react';
import type { PlatformConfig } from '@/api/types';

export function Platforms() {
  const { health, refreshHealth } = useBackendStore();
  const [platforms, setPlatforms] = useState<PlatformConfig[]>([]);
  const [showModal, setShowModal] = useState(false);
  const [editingPlatform, setEditingPlatform] = useState<PlatformConfig | null>(null);
  const [formData, setFormData] = useState({
    id: '',
    type: 'misskey',
    settings: {} as Record<string, unknown>,
  });

  // 从 health 中提取适配器信息
  const adapters = health?.adapters || {};

  useEffect(() => {
    // 从后端配置中读取平台列表
    // 这里简化处理，实际应从配置文件读取
    const configPlatforms = health?.running ? [] : [];
    setPlatforms(configPlatforms);
  }, [health]);

  const handleAdd = () => {
    setEditingPlatform(null);
    setFormData({ id: '', type: 'misskey', settings: {} });
    setShowModal(true);
  };

  const handleEdit = (platform: PlatformConfig) => {
    setEditingPlatform(platform);
    setFormData({
      id: platform.id,
      type: platform.type,
      settings: platform.settings,
    });
    setShowModal(true);
  };

  const handleDelete = async (id: string) => {
    if (!confirm(`确定要删除平台 "${id}" 吗？`)) return;
    // TODO: 调用 API 删除
    toast('success', '已删除');
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    // TODO: 调用 API 保存
    toast('success', editingPlatform ? '已更新' : '已创建');
    setShowModal(false);
  };

  const renderSettingsForm = () => {
    if (formData.type === 'onebot') {
      return (
        <>
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-sm font-medium text-text-secondary mb-1">Host</label>
              <Input
                value={formData.settings.host as string || ''}
                onChange={(e) => setFormData({
                  ...formData,
                  settings: { ...formData.settings, host: e.target.value }
                })}
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-text-secondary mb-1">Port</label>
              <Input
                type="number"
                value={formData.settings.port as number || 8080}
                onChange={(e) => setFormData({
                  ...formData,
                  settings: { ...formData.settings, port: Number(e.target.value) }
                })}
              />
            </div>
          </div>
          <div>
            <label className="block text-sm font-medium text-text-secondary mb-1">Access Token</label>
            <Input
              type="password"
              value={formData.settings.access_token as string || ''}
              onChange={(e) => setFormData({
                ...formData,
                settings: { ...formData.settings, access_token: e.target.value }
              })}
            />
          </div>
        </>
      );
    }

    // misskey 默认
    return (
      <>
        <div>
          <label className="block text-sm font-medium text-text-secondary mb-1">Base URL</label>
          <Input
            value={formData.settings.base_url as string || ''}
            onChange={(e) => setFormData({
              ...formData,
              settings: { ...formData.settings, base_url: e.target.value }
            })}
          />
        </div>
        <div>
          <label className="block text-sm font-medium text-text-secondary mb-1">API Token</label>
          <Input
            type="password"
            value={formData.settings.api_token as string || ''}
            onChange={(e) => setFormData({
              ...formData,
              settings: { ...formData.settings, api_token: e.target.value }
            })}
          />
        </div>
      </>
    );
  };

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-text-primary">平台状态</h1>
          <p className="text-text-secondary mt-1">管理平台适配器配置和运行状态</p>
        </div>
        <div className="flex gap-2">
          <Button variant="ghost" onClick={refreshHealth}>
            <RefreshCw className="h-4 w-4 mr-2" />
            刷新
          </Button>
          <Button variant="primary" onClick={handleAdd}>
            <Plus className="h-4 w-4 mr-2" />
            添加平台
          </Button>
        </div>
      </div>

      {/* 运行中的适配器 */}
      {health?.running && Object.keys(adapters).length > 0 && (
        <Card>
          <h3 className="text-lg font-semibold text-text-primary mb-4">运行中的适配器</h3>
          <div className="space-y-3">
            {Object.entries(adapters).map(([id, info]) => (
              <div
                key={id}
                className="flex items-center justify-between p-4 rounded-glass-sm bg-bg-glass"
              >
                <div className="flex items-center gap-4">
                  <div>
                    <span className="font-medium text-text-primary">{id}</span>
                    <Badge variant="info" className="ml-2">
                      {info.config_type || info.type || 'unknown'}
                    </Badge>
                  </div>
                </div>
                <div className="flex items-center gap-3">
                  <Badge variant={info.status === 'running' ? 'success' : info.status === 'error' ? 'error' : 'warning'}>
                    {info.status}
                  </Badge>
                  <Badge variant={info.started ? 'success' : 'default'}>
                    {info.started ? '已启动' : '未启动'}
                  </Badge>
                  {info.last_error && (
                    <span className="text-error text-sm">{info.last_error}</span>
                  )}
                </div>
              </div>
            ))}
          </div>
        </Card>
      )}

      {/* 配置中的平台 */}
      <Card>
        <h3 className="text-lg font-semibold text-text-primary mb-4">配置中的平台</h3>
        {platforms.length === 0 ? (
          <div className="text-center py-8 text-text-secondary">
            暂无平台配置
          </div>
        ) : (
          <div className="space-y-3">
            {platforms.map((platform) => (
              <div
                key={platform.id}
                className="flex items-center justify-between p-4 rounded-glass-sm bg-bg-glass"
              >
                <div>
                  <span className="font-medium text-text-primary">{platform.id}</span>
                  <Badge variant="default" className="ml-2">{platform.type}</Badge>
                </div>
                <div className="flex items-center gap-2">
                  <Button variant="ghost" size="sm" onClick={() => handleEdit(platform)}>
                    <Edit2 className="h-4 w-4" />
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => handleDelete(platform.id)}
                    className="text-error"
                  >
                    <Trash2 className="h-4 w-4" />
                  </Button>
                </div>
              </div>
            ))}
          </div>
        )}
      </Card>

      {/* 编辑/新增模态框 */}
      <Modal
        open={showModal}
        onClose={() => setShowModal(false)}
        title={editingPlatform ? '编辑平台' : '添加平台'}
        size="lg"
      >
        <form onSubmit={handleSubmit} className="space-y-4">
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-sm font-medium text-text-secondary mb-1">
                平台名称
              </label>
              <Input
                value={formData.id}
                onChange={(e) => setFormData({ ...formData, id: e.target.value })}
                disabled={!!editingPlatform}
                required
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-text-secondary mb-1">
                类型
              </label>
              <Select
                options={PLATFORM_TYPES.map((t) => ({ value: t, label: t }))}
                value={formData.type}
                onChange={(e) => setFormData({ ...formData, type: e.target.value })}
              />
            </div>
          </div>

          <div className="border-t border-border-glass pt-4">
            <h4 className="text-sm font-medium text-text-primary mb-3">平台设置</h4>
            {renderSettingsForm()}
          </div>

          <div className="flex gap-3 pt-4">
            <Button type="submit" variant="primary" className="flex-1">
              {editingPlatform ? '保存修改' : '创建'}
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
