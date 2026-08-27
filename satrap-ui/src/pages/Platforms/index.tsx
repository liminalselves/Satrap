import { useEffect, useState, useCallback, useMemo } from 'react';
import { useBackendStore } from '@/stores/useBackendStore';
import { useConfigStore } from '@/stores/useConfigStore';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { Badge } from '@/components/ui/Badge';
import { toast } from '@/components/ui/Toast';
import { PLATFORM_TYPES } from '@/utils/constants';
import { PageHeader, FormModal, ActionButtons, EmptyState } from '@/components/common';
import { Plus, Edit2, Trash2, RefreshCw } from 'lucide-react';
import { controlApi } from '@/api/control';
import { edictumApi } from '@/api/edictum';
import { normalizePlatformSettings } from '@/utils/adminMigration';
import type { FormField } from '@/components/common';
import type { EdictumSessionConfig, PlatformConfig } from '@/api/types';

export function Platforms() {
  const { health, refreshHealth, reloadConfig } = useBackendStore();
  const { sessionClasses, fetchSessionClasses } = useConfigStore();
  const [platforms, setPlatforms] = useState<PlatformConfig[]>([]);
  const [edictumConfigs, setEdictumConfigs] = useState<Record<string, EdictumSessionConfig>>({});
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [showModal, setShowModal] = useState(false);
  const [editingPlatform, setEditingPlatform] = useState<PlatformConfig | null>(null);
  const [rawSettings, setRawSettings] = useState('{}');
  const [formData, setFormData] = useState({
    id: '',
    type: 'misskey',
    session_provider: 'session_class',
    session_type: '',
    settings: {} as Record<string, unknown>,
  });

  // 从 health 中提取适配器信息
  const adapters = useMemo(() => health?.adapters || {}, [health?.adapters]);

  const loadPlatforms = useCallback(async () => {
    setLoading(true);
    try {
      const result = await controlApi.listPlatforms();
      if (!result.ok) throw new Error(result.error || '读取失败');
      setPlatforms(result.platforms || []);
    } catch (e) {
      toast('error', '读取平台配置失败: ' + (e instanceof Error ? e.message : '控制服务未运行'));
    } finally {
      setLoading(false);
    }
  }, []);

  const loadEdictumConfigs = useCallback(async () => {
    try {
      setEdictumConfigs(await edictumApi.list());
    } catch {
      setEdictumConfigs({});
    }
  }, []);

  useEffect(() => {
    loadPlatforms();
    loadEdictumConfigs();
    fetchSessionClasses();
  }, [fetchSessionClasses, loadEdictumConfigs, loadPlatforms]);

  const handleAdd = useCallback(() => {
    setEditingPlatform(null);
    setFormData({ id: '', type: 'misskey', session_provider: 'session_class', session_type: '', settings: {} });
    setRawSettings('{}');
    setShowModal(true);
  }, []);

  const handleEdit = useCallback((platform: PlatformConfig) => {
    setEditingPlatform(platform);
    setFormData({
      id: platform.id,
      type: platform.type,
      session_provider: platform.session_provider || 'session_class',
      session_type: platform.session_type || '',
      settings: platform.settings,
    });
    setRawSettings(JSON.stringify(platform.settings, null, 2));
    setShowModal(true);
  }, []);

  const handleDelete = useCallback(async (id: string) => {
    if (!confirm(`确定要删除平台 "${id}" 吗?`)) return;
    try {
      const result = await controlApi.deletePlatform(id);
      if (!result.ok) throw new Error(result.error || '删除失败');
      setPlatforms(result.platforms || []);
      if (health?.running) {
        const reloaded = await reloadConfig();
        toast(reloaded ? 'success' : 'warning', reloaded ? '平台已删除并热加载' : '平台已删除, 但热加载失败');
      } else {
        toast('success', '平台已删除');
      }
    } catch (e) {
      toast('error', '删除失败: ' + (e instanceof Error ? e.message : '未知错误'));
    }
  }, [health?.running, reloadConfig]);

  const handleSubmit = useCallback(async () => {
    if (!formData.id.trim()) {
      toast('error', '平台名称不能为空');
      return;
    }
    if (formData.session_provider === 'edictum' && !formData.session_type.trim()) {
      toast('error', 'EdictumProvider 必须绑定一个命名配置');
      return;
    }
    setSaving(true);
    try {
      let settings = formData.settings;
      if (formData.type !== 'onebot' && formData.type !== 'misskey') {
        const parsed = JSON.parse(rawSettings) as unknown;
        if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
          throw new Error('Settings 必须是 JSON 对象');
        }
        settings = parsed as Record<string, unknown>;
      }
      const platform: PlatformConfig = {
        id: formData.id.trim(),
        type: formData.type,
        session_provider: formData.session_provider,
        session_type: formData.session_type || undefined,
        settings: normalizePlatformSettings(formData.type, settings),
      };
      const result = editingPlatform
        ? await controlApi.updatePlatform(editingPlatform.id, platform)
        : await controlApi.createPlatform(platform);
      if (!result.ok) throw new Error(result.error || '保存失败');
      setPlatforms(result.platforms || []);
      setShowModal(false);
      if (health?.running) {
        const reloaded = await reloadConfig();
        toast(reloaded ? 'success' : 'warning', reloaded ? '平台已保存并热加载' : '平台已保存, 但热加载失败');
      } else {
        toast('success', editingPlatform ? '平台已更新' : '平台已创建');
      }
    } catch (e) {
      toast('error', '保存失败: ' + (e instanceof Error ? e.message : '未知错误'));
    } finally {
      setSaving(false);
    }
  }, [editingPlatform, formData, health?.running, rawSettings, reloadConfig]);

  const handleFieldChange = useCallback((key: string, value: unknown) => {
    if (key.startsWith('settings.')) {
      const settingKey = key.replace('settings.', '');
      setFormData((prev) => ({
        ...prev,
        settings: { ...prev.settings, [settingKey]: value },
      }));
    } else {
      if (key === 'settings_json') {
        setRawSettings(value as string);
      } else if (key === 'session_provider') {
        setFormData((prev) => ({ ...prev, session_provider: String(value), session_type: '' }));
      } else {
        setFormData((prev) => ({ ...prev, [key]: value }));
      }
    }
  }, []);

  // 表单字段
  const formFields = useMemo<FormField[]>(() => {
    const typeOptions = new Set<string>(PLATFORM_TYPES);
    platforms.forEach((platform) => typeOptions.add(platform.type));
    Object.values(adapters).forEach((info) => {
      const type = String(info.config_type || info.type || '').trim();
      if (type) typeOptions.add(type);
    });
    const baseFields: FormField[] = [
      { key: 'id', label: '平台名称', required: true, disabled: !!editingPlatform },
      {
        key: 'type',
        label: '类型',
        type: 'select',
        options: Array.from(typeOptions).map((type) => ({ value: type, label: type })),
      },
      {
        key: 'session_provider',
        label: '会话 Provider',
        type: 'select',
        options: [
          { value: 'session_class', label: 'SessionClassProvider' },
          { value: 'edictum', label: 'EdictumProvider' },
        ],
      },
      {
        key: 'session_type',
        label: formData.session_provider === 'edictum' ? 'Edictum 命名配置' : '默认会话类',
        type: 'select',
        options: [
          {
            value: '',
            label: formData.session_provider === 'edictum'
              ? '请选择 Edictum 命名配置'
              : '自动（同名会话类或全局默认）',
          },
          ...(formData.session_provider === 'edictum'
            ? Object.entries(edictumConfigs).map(([name, config]) => ({
                value: name,
                label: config.enabled ? name : `${name}（已停用）`,
              }))
            : Object.entries(sessionClasses).map(([name, config]) => ({
                value: name,
                label: config.enabled ? name : `${name}（已停用）`,
              }))),
        ],
      },
    ];

    if (formData.type === 'onebot') {
      return [
        ...baseFields,
        { key: 'settings.host', label: 'Host' },
        { key: 'settings.port', label: 'Port', type: 'number' },
        { key: 'settings.access_token', label: 'Access Token', type: 'password' },
        { key: 'settings.secret', label: 'Secret', type: 'password' },
        { key: 'settings.self_id', label: 'Self ID' },
        { key: 'settings.enable_private', label: '私聊', type: 'checkbox', placeholder: '启用私聊' },
        { key: 'settings.enable_group', label: '群聊', type: 'checkbox', placeholder: '启用群聊' },
      ];
    }

    if (formData.type === 'misskey') {
      return [
        ...baseFields,
        { key: 'settings.base_url', label: 'Base URL' },
        { key: 'settings.api_token', label: 'API Token', type: 'password' },
        { key: 'settings.chat_enabled', label: 'Chat', type: 'checkbox', placeholder: '启用 Chat' },
        { key: 'settings.room_enabled', label: 'Room', type: 'checkbox', placeholder: '启用 Room' },
        { key: 'settings.max_message_length', label: '最大消息长度', type: 'number' },
        {
          key: 'settings.misskey_default_visibility',
          label: '默认可见性',
          type: 'select',
          options: ['public', 'home', 'followers', 'specified'].map((value) => ({ value, label: value })),
        },
        { key: 'settings.misskey_local_only', label: 'Local Only', type: 'checkbox', placeholder: '仅本地可见' },
      ];
    }

    return [
      ...baseFields,
      { key: 'settings_json', label: 'Settings JSON', type: 'textarea', rows: 12 },
    ];
  }, [adapters, edictumConfigs, editingPlatform, formData.session_provider, formData.type, platforms, sessionClasses]);

  // 表单值
  const formValues = useMemo(() => ({
    id: formData.id,
    type: formData.type,
    session_provider: formData.session_provider,
    session_type: formData.session_type,
    'settings.host': formData.settings.host,
    'settings.port': formData.settings.port,
    'settings.access_token': formData.settings.access_token,
    'settings.secret': formData.settings.secret,
    'settings.self_id': formData.settings.self_id,
    'settings.enable_private': formData.settings.enable_private ?? true,
    'settings.enable_group': formData.settings.enable_group ?? true,
    'settings.base_url': formData.settings.base_url,
    'settings.api_token': formData.settings.api_token,
    'settings.chat_enabled': formData.settings.chat_enabled ?? formData.settings.misskey_enable_chat ?? true,
    'settings.room_enabled': formData.settings.room_enabled ?? false,
    'settings.max_message_length': formData.settings.max_message_length ?? 3000,
    'settings.misskey_default_visibility': formData.settings.misskey_default_visibility ?? 'public',
    'settings.misskey_local_only': formData.settings.misskey_local_only ?? false,
    settings_json: rawSettings,
  }), [formData, rawSettings]);

  return (
    <div className="space-y-6">
      <PageHeader
        title="平台状态"
        description="管理平台适配器配置和运行状态"
        actions={
          <>
            <Button
              variant="ghost"
              onClick={() => {
                refreshHealth();
                loadPlatforms();
                loadEdictumConfigs();
              }}
              disabled={loading}
            >
              <RefreshCw className={`h-4 w-4 mr-2 ${loading ? 'animate-spin' : ''}`} />
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
                    <Badge variant="default" className="ml-2">
                      {(info.session_provider || 'session_class')}: {info.session_type || '自动'}
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
                  <Badge variant="info" className="ml-2">
                    {(platform.session_provider || 'session_class')}: {platform.session_type || '自动'}
                  </Badge>
                  <p className="mt-1 max-w-2xl truncate text-xs text-text-secondary">
                    {JSON.stringify(platform.settings)}
                  </p>
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
        loading={saving}
        size="lg"
      />
    </div>
  );
}
