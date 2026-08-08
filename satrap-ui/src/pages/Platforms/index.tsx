import { useEffect, useState, useCallback, useMemo } from 'react';
import { useBackendStore } from '@/stores/useBackendStore';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { Badge } from '@/components/ui/Badge';
import { toast } from '@/components/ui/Toast';
import { PLATFORM_TYPES } from '@/utils/constants';
import { PageHeader, FormModal, ActionButtons, EmptyState } from '@/components/common';
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
  const adapters = useMemo(() => health?.adapters || {}, [health?.adapters]);

  useEffect(() => {
    // 从后端配置中读取平台列表
    const configPlatforms = health?.running ? [] : [];
    setPlatforms(configPlatforms);
  }, [health]);

  const handleAdd = useCallback(() => {
    setEditingPlatform(null);
    setFormData({ id: '', type: 'misskey', settings: {} });
    setShowModal(true);
  }, []);

  const handleEdit = useCallback((platform: PlatformConfig) => {
    setEditingPlatform(platform);
    setFormData({
      id: platform.id,
      type: platform.type,
      settings: platform.settings,
    });
    setShowModal(true);
  }, []);

  const handleDelete = useCallback(async (id: string) => {
    if (!confirm(`确定要删除平台 "${id}" 吗？`)) return;
    // TODO: 调用 API 删除
    toast('success', '已删除');
  }, []);

  const handleSubmit = useCallback(async () => {
    // TODO: 调用 API 保存
    toast('success', editingPlatform ? '已更新' : '已创建');
    setShowModal(false);
  }, [editingPlatform]);

  const handleFieldChange = useCallback((key: string, value: unknown) => {
    if (key.startsWith('settings.')) {
      const settingKey = key.replace('settings.', '');
      setFormData((prev) => ({
        ...prev,
        settings: { ...prev.settings, [settingKey]: value },
      }));
    } else {
      setFormData((prev) => ({ ...prev, [key]: value }));
    }
  }, []);

  // 表单字段
  const formFields = useMemo(() => {
    const baseFields = [
      { key: 'id', label: '平台名称', required: true, disabled: !!editingPlatform },
      {
        key: 'type',
        label: '类型',
        type: 'select' as const,
        options: PLATFORM_TYPES.map((t) => ({ value: t, label: t })),
      },
    ];

    if (formData.type === 'onebot') {
      return [
        ...baseFields,
        { key: 'settings.host', label: 'Host' },
        { key: 'settings.port', label: 'Port', type: 'number' as const },
        { key: 'settings.access_token', label: 'Access Token', type: 'password' as const },
      ];
    }

    // misskey 默认
    return [
      ...baseFields,
      { key: 'settings.base_url', label: 'Base URL' },
      { key: 'settings.api_token', label: 'API Token', type: 'password' as const },
    ];
  }, [formData.type, editingPlatform]);

  // 表单值
  const formValues = useMemo(() => ({
    id: formData.id,
    type: formData.type,
    'settings.host': formData.settings.host,
    'settings.port': formData.settings.port,
    'settings.access_token': formData.settings.access_token,
    'settings.base_url': formData.settings.base_url,
    'settings.api_token': formData.settings.api_token,
  }), [formData]);

  return (
    <div className="space-y-6">
      <PageHeader
        title="平台状态"
        description="管理平台适配器配置和运行状态"
        actions={
          <>
            <Button variant="ghost" onClick={refreshHealth}>
              <RefreshCw className="h-4 w-4 mr-2" />
              刷新
            </Button>
            <Button variant="primary" onClick={handleAdd}>
              <Plus className="h-4 w-4 mr-2" />
              添加平台
            </Button>
          </>
        }
      />

      {/* 运行中的适配器 */}
      {health?.running && Object.keys(adapters).length > 0 && (
        <Card>
          <h3 className="text-lg font-semibold text-text-primary mb-4">运行中的适配器</h3>
          <div className="space-y-3">
            {Object.entries(adapters).map(([id, info]) => (
              <div
                key={id}
                className="flex items-center justify-between p-4 rounded-sm bg-glass"
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
          <EmptyState title="暂无平台配置" />
        ) : (
          <div className="space-y-3">
            {platforms.map((platform) => (
              <div
                key={platform.id}
                className="flex items-center justify-between p-4 rounded-sm bg-glass"
              >
                <div>
                  <span className="font-medium text-text-primary">{platform.id}</span>
                  <Badge variant="default" className="ml-2">{platform.type}</Badge>
                </div>
                <ActionButtons
                  actions={[
                    {
                      key: 'edit',
                      icon: <Edit2 className="h-4 w-4" />,
                      onClick: () => handleEdit(platform),
                      title: '编辑',
                    },
                    {
                      key: 'delete',
                      icon: <Trash2 className="h-4 w-4" />,
                      onClick: () => handleDelete(platform.id),
                      title: '删除',
                      className: 'text-error',
                    },
                  ]}
                />
              </div>
            ))}
          </div>
        )}
      </Card>

      {/* 编辑/新增模态框 */}
      <FormModal
        open={showModal}
        onClose={() => setShowModal(false)}
        title={editingPlatform ? '编辑平台' : '添加平台'}
        fields={formFields}
        values={formValues}
        onChange={handleFieldChange}
        onSubmit={handleSubmit}
        submitText={editingPlatform ? '保存修改' : '创建'}
        size="lg"
      />
    </div>
  );
}
