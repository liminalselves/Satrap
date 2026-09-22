import { useEffect, useState, useCallback, useMemo } from 'react';
import { isAxiosError } from 'axios';
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
import { backendApi } from '@/api/backend';
import { edictumApi } from '@/api/edictum';
import { normalizePlatformSettings, platformSettingsSummary } from '@/utils/adminMigration';
import type { FormField } from '@/components/common';
import type { EdictumSessionConfig, PlatformConfig } from '@/api/types';

export function Platforms() {
  const { health, refreshHealth } = useBackendStore();
  const { sessionClasses, fetchSessionClasses, asrConfigs, fetchModels } = useConfigStore();
  const [revision, setRevision] = useState('');
  const [draftRevision, setDraftRevision] = useState('');
  const [platforms, setPlatforms] = useState<PlatformConfig[]>([]);
  const [edictumConfigs, setEdictumConfigs] = useState<Record<string, EdictumSessionConfig>>({});
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [showModal, setShowModal] = useState(false);
  const [editingPlatform, setEditingPlatform] = useState<PlatformConfig | null>(null);
  const [rawSettings, setRawSettings] = useState('{}');
  const [formData, setFormData] = useState({
    enable: true,
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
      setRevision(result.revision || '');
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
    fetchModels('asr').catch(() => undefined);
    refreshHealth();
  }, [fetchModels, fetchSessionClasses, loadEdictumConfigs, loadPlatforms, refreshHealth]);

  const handleAdd = useCallback(() => {
    setEditingPlatform(null);
    setFormData({ enable: true, id: '', type: 'misskey', session_provider: 'session_class', session_type: '', settings: {} });
    setRawSettings('{}');
    setDraftRevision(revision);
    setShowModal(true);
  }, [revision]);

  const handleEdit = useCallback((platform: PlatformConfig) => {
    setEditingPlatform(platform);
    setFormData({
      enable: platform.enable ?? true,
      id: platform.id,
      type: platform.type,
      session_provider: platform.session_provider || 'session_class',
      session_type: platform.session_type || '',
      settings: platform.settings,
    });
    setRawSettings(JSON.stringify(platform.settings, null, 2));
    setDraftRevision(revision);
    setShowModal(true);
  }, [revision]);

  const applySavedPlatform = useCallback(async (id: string, savedRevision?: string) => {
    try {
      if (!savedRevision) throw new Error('保存结果缺少修订');
      const result = await backendApi.reloadConfig(savedRevision);
      await refreshHealth();
      const application = result.platforms?.find((item) => item.id === id);
      if (application?.status === 'applied') toast('success', '配置已保存并生效');
      else if (application?.status === 'failed') toast('warning', `配置已保存, 应用失败: ${application.error || '请检查运行状态'}`);
      else toast('warning', '配置已保存, 平台变更待重启生效');
    } catch {
      toast('warning', '配置已保存, 无法确认运行时生效状态');
    }
  }, [refreshHealth]);

  const handleDelete = useCallback(async (id: string) => {
    if (!confirm(`确定要删除平台 "${id}" 吗?`)) return;
    try {
      if (!revision) throw new Error('请先刷新平台配置后再删除');
      const result = await controlApi.deletePlatform(id, revision);
      if (!result.ok) throw new Error(result.error || '删除失败');
      setPlatforms(result.platforms || []);
      setRevision(result.revision || '');
      if (health?.running) {
        await applySavedPlatform(id, result.revision);
      } else {
        toast('success', '平台已删除');
      }
    } catch (e) {
      toast('error', '删除失败: ' + (isAxiosError(e) && e.response?.status === 409 ? '配置已被其他操作修改, 请刷新后重试' : e instanceof Error ? e.message : '未知错误'));
    }
  }, [health?.running, applySavedPlatform, revision]);

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
      if (!draftRevision) throw new Error('请刷新平台配置后重新打开编辑表单');
      let settings = formData.settings;
      if (formData.type !== 'onebot' && formData.type !== 'aiocqhttp' && formData.type !== 'misskey') {
        const parsed = JSON.parse(rawSettings) as unknown;
        if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
          throw new Error('Settings 必须是 JSON 对象');
        }
        settings = parsed as Record<string, unknown>;
      }
      if (!editingPlatform && (formData.type === 'onebot' || formData.type === 'aiocqhttp')) {
        settings = { context_scope: 'group_member', ...settings };
      }
      const platform: PlatformConfig = {
        enable: formData.enable,
        id: formData.id.trim(),
        type: formData.type,
        session_provider: formData.session_provider,
        session_type: formData.session_type || undefined,
        settings: normalizePlatformSettings(formData.type, settings),
      };
      const result = editingPlatform
        ? await controlApi.updatePlatform(editingPlatform.id, platform, draftRevision)
        : await controlApi.createPlatform(platform, draftRevision);
      if (!result.ok) throw new Error(result.error || '保存失败');
      setPlatforms(result.platforms || []);
      setRevision(result.revision || '');
      setShowModal(false);
      if (health?.running) {
        await applySavedPlatform(platform.id, result.revision);
      } else {
        toast('success', editingPlatform ? '平台已更新' : '平台已创建');
      }
    } catch (e) {
      toast('error', '保存失败: ' + (isAxiosError(e) && e.response?.status === 409 ? '配置已被其他操作修改, 当前草稿已保留; 请复制草稿并刷新后合并' : e instanceof Error ? e.message : '未知错误'));
    } finally {
      setSaving(false);
    }
  }, [editingPlatform, formData, health?.running, rawSettings, applySavedPlatform, draftRevision]);

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
      { key: 'enable', label: '平台启用', type: 'checkbox', placeholder: '启用该平台实例' },
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

    if (formData.type === 'onebot' || formData.type === 'aiocqhttp') {
      return [
        ...baseFields,
        { key: 'settings.host', label: 'Host' },
        { key: 'settings.port', label: 'Port', type: 'number' },
        { key: 'settings.access_token', label: 'Access Token', type: 'password' },
        { key: 'settings.secret', label: 'Secret', type: 'password' },
        { key: 'settings.self_id', label: 'Self ID' },
        { key: 'settings.enable_private', label: '私聊', type: 'checkbox', placeholder: '启用私聊' },
        { key: 'settings.enable_group', label: '群聊', type: 'checkbox', placeholder: '启用群聊' },
        { key: 'settings.context_scope', label: '上下文范围（切换保留旧历史, 使用独立映射）', type: 'select', options: [
          { value: 'legacy_user', label: '旧用户映射（同成员跨群共享）' },
          { value: 'group_member', label: '群成员隔离（推荐）' },
          { value: 'group', label: '群共享（成员共享上下文）' },
        ] },
        { key: 'settings.group_whitelist', label: '群白名单（留空允许所有群）', type: 'textarea', rows: 3, placeholder: '每行一个群 ID' },
        { key: 'settings.wake_aliases', label: '机器人名字/别名（可选）', type: 'textarea', rows: 2, placeholder: '每行一个, 留空关闭; 仅匹配当前正文' },
        { key: 'settings.wake_mode', label: '自动参与模式', type: 'select', options: [{ value: 'explicit', label: '仅明确唤醒（默认）' }, { value: 'frequency', label: '按正文消息数量触发' }, { value: 'necessity', label: '按本地回复必要性评分' }] },
        { key: 'settings.wake_message_threshold', label: '自动参与消息阈值（1–32）', type: 'number', placeholder: '默认 3' },
        { key: 'settings.wake_score_threshold', label: '必要性评分阈值（0–1）', type: 'number', placeholder: '默认 0.65, 分值越高参与越少' },
        { key: 'settings.wake_max_wait', label: '频率模式最长等待秒数（可选）', type: 'number', placeholder: '默认 0 关闭; 大于 0 且小于 120, 仍遵守冷却和限流' },
        { key: 'settings.wake_group_overrides', label: '群级唤醒覆盖（JSON，可选）', type: 'textarea', rows: 3, placeholder: '{"123": {"wake_mode": "frequency", "wake_message_threshold": 5}}' },
        { key: 'settings.wake_time_rules', label: '时段自动参与规则（本机时区，JSON）', type: 'textarea', rows: 3, placeholder: '[{"start":"23:00","end":"07:00","settings":{"wake_mode":"explicit"}}]' },
        { key: 'settings.wake_cooldown', label: '自动参与冷却秒数', type: 'number', placeholder: '默认 30, 明确唤醒不受此限制' },
        { key: 'settings.message_text_limit', label: '每条消息文本上限（64–32000）', type: 'number', placeholder: '默认 2000 字符, 长消息优先按换行分段' },
        { key: 'settings.reply_with_quote', label: '群聊回复引用原消息', type: 'checkbox', placeholder: '默认关闭; 仅对有来源消息 ID 的群聊回复添加引用' },
        { key: 'settings.reply_with_mention', label: '群聊回复 @发送者', type: 'checkbox', placeholder: '默认关闭; 已有 @ 时不重复, 私聊不受影响' },
        { key: 'settings.quote_lookup', label: '回源被引用消息原文', type: 'checkbox', placeholder: '默认开启; 唤醒后按预算 get_msg 获取引用原文作为上下文, 关闭后仅标记引用' },
        { key: 'settings.forward_lookup', label: '回源合并转发内容', type: 'checkbox', placeholder: '默认开启; 唤醒后按预算 get_forward_msg 获取转发节点作为上下文, 关闭后仅保留占位' },
        { key: 'settings.wake_on_quote_self', label: '引用机器人消息时唤醒', type: 'checkbox', placeholder: '默认关闭; 开启后用同一回源预算确认被引用者是机器人再唤醒' },
        { key: 'settings.notice_types', label: '订阅的通知/请求类型（留空全部）', type: 'textarea', rows: 2, placeholder: '每行一个, 如 notice.group_increase / request.friend / notice; 群通知仍受白名单限制, 默认不触发模型' },
        { key: 'settings.asr_model', label: '语音转写使用的 ASR 配置（留空关闭）', type: 'select', options: [{ value: '', label: '不转写语音' }, ...Object.keys(asrConfigs).map((name) => ({ value: name, label: name }))] },
        { key: 'settings.voice_transcribe', label: '语音转写来源（ASR 路径依次尝试实现转码 get_record → 直接下载 → 本地 av 转码）', type: 'select', options: [{ value: 'asr', label: 'ASR 配置 (默认)' }, { value: 'asr_then_platform', label: 'ASR 失败后回退平台原生转写' }, { value: 'platform', label: '仅平台原生转写 (fetch_ptt_text)' }, { value: 'off', label: '关闭语音转写' }] },
        { key: 'settings.attachment_extract', label: '提取文件附件正文', type: 'checkbox', placeholder: '默认开启; 仅对已唤醒消息中的受支持文档类型下载并提取, 上限 32 MiB / 20000 字符' },
        { key: 'settings.media_insecure_tls', label: '媒体下载跳过 TLS 证书校验', type: 'checkbox', placeholder: '默认关闭; 仅对下方登记的主机生效 (自签证书场景), 公网下载始终校验' },
        { key: 'settings.media_plaintext_http', label: '允许公网明文 HTTP 媒体下载', type: 'checkbox', placeholder: '默认关闭; 开启后公网 http:// 附件地址也允许下载, 明文传输可被窃听篡改; 登记主机不受影响' },
        { key: 'settings.media_trusted_hosts', label: '允许访问私网的媒体主机（可选）', type: 'textarea', rows: 2, placeholder: '每行一个主机名; SnowLuma 提供的内网下载地址需在此登记, 否则出站防护会拒绝' },
        { key: 'settings.wake_words', label: '唤醒词（留空不启用词语触发）', type: 'textarea', rows: 3, placeholder: '每行一个唤醒词, 匹配当前消息正文' },
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
  }, [adapters, asrConfigs, edictumConfigs, editingPlatform, formData.session_provider, formData.type, platforms, sessionClasses]);

  // 表单值
  const formValues = useMemo(() => ({
    enable: formData.enable,
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
    'settings.context_scope': formData.settings.context_scope ?? (editingPlatform ? 'legacy_user' : 'group_member'),
    'settings.enable_group': formData.settings.enable_group ?? true,
    'settings.reply_with_quote': formData.settings.reply_with_quote ?? false,
    'settings.reply_with_mention': formData.settings.reply_with_mention ?? false,
    'settings.quote_lookup': formData.settings.quote_lookup ?? true,
    'settings.forward_lookup': formData.settings.forward_lookup ?? true,
    'settings.wake_on_quote_self': formData.settings.wake_on_quote_self ?? false,
    'settings.group_whitelist': Array.isArray(formData.settings.group_whitelist) ? formData.settings.group_whitelist.join('\n') : formData.settings.group_whitelist ?? '',
    'settings.wake_aliases': Array.isArray(formData.settings.wake_aliases) ? formData.settings.wake_aliases.join('\n') : formData.settings.wake_aliases ?? '',
    'settings.notice_types': Array.isArray(formData.settings.notice_types) ? formData.settings.notice_types.join('\n') : formData.settings.notice_types ?? '',
    'settings.asr_model': formData.settings.asr_model ?? '',
    'settings.voice_transcribe': formData.settings.voice_transcribe ?? 'asr',
    'settings.attachment_extract': formData.settings.attachment_extract ?? true,
    'settings.media_trusted_hosts': Array.isArray(formData.settings.media_trusted_hosts) ? formData.settings.media_trusted_hosts.join('\n') : formData.settings.media_trusted_hosts ?? '',
    'settings.wake_group_overrides': typeof formData.settings.wake_group_overrides === 'string' ? formData.settings.wake_group_overrides : JSON.stringify(formData.settings.wake_group_overrides ?? {}, null, 2),
    'settings.wake_time_rules': typeof formData.settings.wake_time_rules === 'string' ? formData.settings.wake_time_rules : JSON.stringify(formData.settings.wake_time_rules ?? [], null, 2),
    'settings.wake_mode': formData.settings.wake_mode ?? 'explicit',
    'settings.wake_message_threshold': formData.settings.wake_message_threshold ?? '',
    'settings.wake_score_threshold': formData.settings.wake_score_threshold ?? '',
    'settings.wake_max_wait': formData.settings.wake_max_wait ?? '',
    'settings.wake_cooldown': formData.settings.wake_cooldown ?? '',
    'settings.message_text_limit': formData.settings.message_text_limit ?? '',
    'settings.media_insecure_tls': formData.settings.media_insecure_tls ?? false,
    'settings.media_plaintext_http': formData.settings.media_plaintext_http ?? false,
    'settings.wake_words': Array.isArray(formData.settings.wake_words) ? formData.settings.wake_words.join('\n') : formData.settings.wake_words ?? '',
    'settings.base_url': formData.settings.base_url,
    'settings.api_token': formData.settings.api_token,
    'settings.chat_enabled': formData.settings.chat_enabled ?? formData.settings.misskey_enable_chat ?? true,
    'settings.room_enabled': formData.settings.room_enabled ?? false,
    'settings.max_message_length': formData.settings.max_message_length ?? 3000,
    'settings.misskey_default_visibility': formData.settings.misskey_default_visibility ?? 'public',
    'settings.misskey_local_only': formData.settings.misskey_local_only ?? false,
    settings_json: rawSettings,
  }), [editingPlatform, formData, rawSettings]);

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

      {!!health?.platform_config?.length && (
        <Card>
          <h3 className="text-lg font-semibold text-text-primary mb-4">配置生效状态</h3>
          <div className="space-y-3">
            {health.platform_config.map((item) => (
              <div key={item.id} className="rounded-sm bg-glass p-3 text-sm break-words">
                <span className="font-medium">{item.id || '配置文件'}</span>
                <Badge className="ml-2" variant={item.status === 'applied' ? 'success' : item.status === 'failed' ? 'error' : 'warning'}>
                  {item.status === 'applied' ? '已生效' : item.status === 'failed' ? '应用失败' : '待重启'}
                </Badge>
                <p className="mt-1 text-xs text-text-secondary">保存版本 {item.saved_revision?.slice(0, 12) || '未知'} · 生效版本 {item.active_revision?.slice(0, 12) || '无'}</p>
                {(item.error || item.reason) && <p className="mt-1 text-text-secondary">{item.error || item.reason}</p>}
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
                    {platformSettingsSummary(platform.type, platform.settings)}
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
