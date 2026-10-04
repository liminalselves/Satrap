import { useEffect, useState, useCallback, useMemo } from 'react';
import { Link } from 'react-router-dom';
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
import { normalizePlatformSettings, platformSettingsSummary, TALK_VALUE_HINT, validatePlatformPolicy } from '@/utils/adminMigration';
import { formNumericLimits, formRangeSuffix, formRangeText } from '@/utils/wakePolicyContract';
import { confirmDiscard, useDirtyGuard } from '@/hooks/useDirtyGuard';
import { RequestDiagnosticsPanel } from '@/components/diagnostics/RequestDiagnosticsPanel';
import { fromGroupRows, fromTimeRows, toGroupRows, toTimeRows } from '@/utils/wakeOverrides';
import { WakeOverrideEditor } from './WakeOverrideEditor';
import { WakeDryRunPanel } from './WakeDryRunPanel';
import { PlatformConnectionTest } from './PlatformConnectionTest';
import { AdapterStatus } from '@/components/common/AdapterStatus';
import { AgentRoutingEditor, agentOptions, bindingError } from './AgentRoutingEditor';
import type { FormField } from '@/components/common';
import type { GroupOverrideRow, RowIssue, TimeRuleRow } from '@/utils/wakeOverrides';
import type { AdapterDeclaration, AgentBinding, PlatformConfig } from '@/api/types';

export function Platforms() {
  const { health, isRunning, refreshHealth } = useBackendStore();
  const { sessionClasses, fetchSessionClasses, asrConfigs, fetchModels, edictumConfigs, fetchEdictumConfigs: loadEdictumConfigs } = useConfigStore();
  const [revision, setRevision] = useState('');
  const [draftRevision, setDraftRevision] = useState('');
  const [platforms, setPlatforms] = useState<PlatformConfig[]>([]);
  const [adapterTypes, setAdapterTypes] = useState<AdapterDeclaration[]>([]);
  const [defaultSessionType, setDefaultSessionType] = useState('');
  const [testingPlatformId, setTestingPlatformId] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [showModal, setShowModal] = useState(false);
  const [editingPlatform, setEditingPlatform] = useState<PlatformConfig | null>(null);
  const [rawSettings, setRawSettings] = useState('{}');
  // 群/时段覆盖的草稿行由表单持有: 不完整行不会被静默丢弃, 也参与脏状态比较
  const [groupDraftRows, setGroupDraftRows] = useState<GroupOverrideRow[]>([]);
  const [timeDraftRows, setTimeDraftRows] = useState<TimeRuleRow[]>([]);
  const [openSnapshot, setOpenSnapshot] = useState('');
  const [formData, setFormData] = useState({
    enable: true,
    id: '',
    type: 'misskey',
    session_provider: 'session_class',
    session_type: '',
    session_bindings: {} as Record<string, AgentBinding>,
    settings: {} as Record<string, unknown>,
  });

  // 从 health 中提取适配器信息
  const adapters = useMemo(() => health?.adapters || {}, [health?.adapters]);
  const routeDeclaration = adapterTypes.find((item) => item.type === formData.type);
  const routeKinds = useMemo(() => routeDeclaration?.status === 'available' ? routeDeclaration.conversation_kinds
    : Object.values(adapters).find((item) => (item.config_type || item.type) === formData.type)?.conversation_kinds || {},
  [adapters, formData.type, routeDeclaration]);
  const routeOptions = useMemo(() => agentOptions(sessionClasses, edictumConfigs), [sessionClasses, edictumConfigs]);
  const inheritedName = formData.session_type || (formData.session_provider === 'session_class'
    ? (sessionClasses[formData.type] ? formData.type : defaultSessionType) : '');

  // 诊断面板: 平台筛选来自运行中的适配器与已配置平台
  const [diagnosticsAdapter, setDiagnosticsAdapter] = useState('');
  // 刷新/删除平台后让诊断面板一起重取, 避免展示旧适配器的记录
  const [diagnosticsRefreshKey, setDiagnosticsRefreshKey] = useState(0);
  const diagnosticsAdapters = useMemo(
    () => Array.from(new Set([...Object.keys(adapters), ...platforms.map((item) => item.id)])),
    [adapters, platforms],
  );

  const loadPlatforms = useCallback(async () => {
    setLoading(true);
    try {
      const result = await controlApi.listPlatforms();
      if (!result.ok) throw new Error(result.error || '读取失败');
      setPlatforms(result.platforms || []);
      setAdapterTypes(result.adapter_types || []);
      setDefaultSessionType(result.default_session_type || '');
      setRevision(result.revision || '');
    } catch (e) {
      toast('error', '读取平台配置失败: ' + (e instanceof Error ? e.message : '控制服务未运行'));
    } finally {
      setLoading(false);
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
    const initial = { enable: true, id: '', type: 'misskey', session_provider: 'session_class', session_type: '', session_bindings: {} as Record<string, AgentBinding>, settings: {} as Record<string, unknown> };
    const groups: GroupOverrideRow[] = [];
    const times: TimeRuleRow[] = [];
    setEditingPlatform(null);
    setFormData(initial);
    setRawSettings('{}');
    setGroupDraftRows(groups);
    setTimeDraftRows(times);
    setOpenSnapshot(JSON.stringify({ form: initial, groups, times }));
    setDraftRevision(revision);
    setShowModal(true);
  }, [revision]);

  const handleEdit = useCallback((platform: PlatformConfig) => {
    const initial = {
      enable: platform.enable ?? true,
      id: platform.id,
      type: platform.type,
      session_provider: platform.session_provider || 'session_class',
      session_type: platform.session_type || '',
      session_bindings: platform.session_bindings || {},
      settings: platform.settings,
    };
    const groups = toGroupRows(platform.settings?.wake_group_overrides);
    const times = toTimeRows(platform.settings?.wake_time_rules);
    setEditingPlatform(platform);
    setFormData(initial);
    setRawSettings(JSON.stringify(Object.fromEntries(Object.entries(platform.settings).filter(([key]) => key !== 'message_archive_retention_days')), null, 2));
    setGroupDraftRows(groups);
    setTimeDraftRows(times);
    setOpenSnapshot(JSON.stringify({ form: initial, groups, times }));
    setDraftRevision(revision);
    setShowModal(true);
  }, [revision]);

  // 群/时段草稿行的字段级校验: 保存与试算使用同一结果
  const groupConversion = useMemo(() => fromGroupRows(groupDraftRows), [groupDraftRows]);
  const timeConversion = useMemo(() => fromTimeRows(timeDraftRows), [timeDraftRows]);
  const draftIssues = useMemo<RowIssue[]>(
    () => [...groupConversion.issues, ...timeConversion.issues],
    [groupConversion, timeConversion],
  );
  const draftError = draftIssues.length > 0 ? `群级/时段规则有误: ${draftIssues.map((issue) => issue.message).join('; ')}` : '';

  // 可提交草稿: 草稿行合入 settings; 试算与保存读取同一个值
  const settingsDraft = useMemo(
    () => ({
      ...formData.settings,
      wake_group_overrides: groupConversion.value,
      wake_time_rules: timeConversion.value,
    }),
    [formData.settings, groupConversion.value, timeConversion.value],
  );

  // 脏状态: 表单内容与草稿行都与打开快照一致才算未修改; 关闭模态/路由切换/浏览器刷新三处拦截
  const formDirty = showModal && JSON.stringify({ form: formData, groups: groupDraftRows, times: timeDraftRows }) !== openSnapshot;
  useDirtyGuard(formDirty);
  const guardedClose = useCallback(() => {
    if (!formDirty || confirmDiscard()) setShowModal(false);
  }, [formDirty]);

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
    if (formData.session_type) {
      const selected = routeOptions.find((item) => item.provider === formData.session_provider && item.name === formData.session_type);
      if (!selected?.enabled) {
        toast('error', '平台默认 Agent 配置已删除或停用, 请重新选择');
        return;
      }
    }
    const routeError = bindingError(formData.session_bindings, routeKinds, routeOptions, routeDeclaration?.status === 'available');
    if (routeError) {
      toast('error', routeError);
      return;
    }
    if (formData.type === 'onebot' || formData.type === 'aiocqhttp') {
      // 草稿行的字段级错误优先于范围校验: 保留的行必须先补全或删除
      if (draftError) {
        toast('error', draftError);
        return;
      }
      // 数值/布尔/枚举字段按策略字段契约校验, 与后端同口径; 校验对象是归一化后的设置
      const policyError = validatePlatformPolicy(normalizePlatformSettings(formData.type, settingsDraft));
      if (policyError) {
        toast('error', policyError);
        return;
      }
    }
    setSaving(true);
    try {
      if (!draftRevision) throw new Error('请刷新平台配置后重新打开编辑表单');
      let settings = formData.type === 'onebot' || formData.type === 'aiocqhttp' ? settingsDraft : formData.settings;
      if (formData.type !== 'onebot' && formData.type !== 'aiocqhttp' && formData.type !== 'misskey') {
        const parsed = JSON.parse(rawSettings) as unknown;
        if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
          throw new Error('Settings 必须是 JSON 对象');
        }
        if ('message_archive_retention_days' in parsed) throw new Error('档案保留天数请使用专用输入框，并从 Settings JSON 中移除该项');
        settings = { ...parsed as Record<string, unknown>, message_archive_retention_days: formData.settings.message_archive_retention_days };
      }
      if (!editingPlatform && (formData.type === 'onebot' || formData.type === 'aiocqhttp')) {
        settings = { context_scope: 'group_member', ...settings };
      }
      const normalizedSettings = normalizePlatformSettings(formData.type, settings);
      const archiveError = validatePlatformPolicy('message_archive_retention_days' in normalizedSettings ? { message_archive_retention_days: normalizedSettings.message_archive_retention_days } : {});
      if (archiveError) throw new Error(archiveError);
      const platform: PlatformConfig = {
        enable: formData.enable,
        id: formData.id.trim(),
        type: formData.type,
        session_provider: formData.session_provider,
        session_type: formData.session_type || undefined,
        ...(Object.keys(formData.session_bindings).length ? { session_bindings: formData.session_bindings }
          : editingPlatform?.session_bindings ? { session_bindings: {} } : {}),
        settings: normalizedSettings,
      };
      const result = editingPlatform
        ? await controlApi.updatePlatform(editingPlatform.id, platform, draftRevision)
        : await controlApi.createPlatform(platform, draftRevision);
      if (!result.ok) throw new Error(result.error || '保存失败');
      setPlatforms(result.platforms || []);
      setRevision(result.revision || '');
      // 保存成功才重建草稿行: 其余情况保留用户的输入 (含未完成行)
      setGroupDraftRows(toGroupRows(platform.settings?.wake_group_overrides));
      setTimeDraftRows(toTimeRows(platform.settings?.wake_time_rules));
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
  }, [editingPlatform, formData, health?.running, rawSettings, applySavedPlatform, draftError, draftRevision, settingsDraft, routeDeclaration, routeKinds, routeOptions]);

  const handleFieldChange = useCallback((key: string, value: unknown) => {
    // 草稿行由表单持有, 不落回 settings, 避免未完成行被规范化吞掉
    if (key === 'settings.wake_group_overrides') {
      setGroupDraftRows(value as GroupOverrideRow[]);
      return;
    }
    if (key === 'settings.wake_time_rules') {
      setTimeDraftRows(value as TimeRuleRow[]);
      return;
    }
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

  // 试算预览使用的归一化草稿; 草稿行有误或存在无法解析的配置时禁用试算
  const previewSettings = useMemo(() => {
    if (formData.type !== 'onebot' && formData.type !== 'aiocqhttp') return undefined;
    if (draftError) return undefined;
    try {
      return normalizePlatformSettings(formData.type, settingsDraft);
    } catch {
      return undefined;
    }
  }, [draftError, formData.type, settingsDraft]);

  // talk_value 与显式阈值的优先级结论由后端试算给出, 表单只给静态语义提示

  // 表单字段
  const formFields = useMemo<FormField[]>(() => {
    const typeOptions = new Set<string>(PLATFORM_TYPES);
    adapterTypes.forEach((item) => typeOptions.add(item.type));
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
        label: '平台默认 Provider',
        type: 'select',
        options: [
          { value: 'session_class', label: 'SessionClassProvider' },
          { value: 'edictum', label: 'EdictumProvider' },
        ],
      },
      {
        key: 'session_type',
        label: formData.session_provider === 'edictum' ? '平台默认 Agent（Edictum 命名配置）' : '平台默认 Agent（默认会话类）',
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
          ...(formData.session_type && !routeOptions.some((item) => item.provider === formData.session_provider && item.name === formData.session_type)
            ? [{ value: formData.session_type, label: `${formData.session_type}（已删除）` }] : []),
        ],
      },
      {
        key: 'session_bindings', label: 'Agent 路由', type: 'custom',
        render: () => <AgentRoutingEditor bindings={formData.session_bindings} kinds={routeKinds} options={routeOptions}
          defaultProvider={formData.session_provider} defaultName={inheritedName}
          declarationError={!Object.keys(routeKinds).length ? routeDeclaration?.error || '未取得适配器的对话类型声明, 已保存的路由保留' : undefined}
          onChange={(next) => handleFieldChange('session_bindings', next)} />,
      },
      { key: 'settings.message_archive_retention_days', label: `平台消息档案保留天数${formRangeSuffix('message_archive_retention_days')}`, type: 'number', ...formNumericLimits('message_archive_retention_days'), placeholder: '默认 30 天; 包括消息正文与删除备份, 独立于 Agent 和插件' },
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
        { key: 'settings.wake_message_threshold', label: `自动参与消息阈值${formRangeSuffix('wake_message_threshold')}`, type: 'number', ...formNumericLimits('wake_message_threshold'), placeholder: '默认 3' },
        { key: 'settings.wake_score_threshold', label: `必要性评分阈值${formRangeSuffix('wake_score_threshold')}`, type: 'number', ...formNumericLimits('wake_score_threshold'), placeholder: '默认 0.65, 分值越高参与越少' },
        { key: 'settings.wake_max_wait', label: `频率模式最长等待秒数（可选）${formRangeSuffix('wake_max_wait')}`, type: 'number', ...formNumericLimits('wake_max_wait'), placeholder: '默认 0 关闭; 仍遵守冷却和限流' },
        { key: 'settings.wake_group_overrides', label: '群级唤醒覆盖（可选）', type: 'custom', render: () => <WakeOverrideEditor kind="group" rows={groupDraftRows} onChange={setGroupDraftRows} issues={groupConversion.issues} /> },
        { key: 'settings.wake_time_rules', label: '时段自动参与规则（本机时区）', type: 'custom', render: () => <WakeOverrideEditor kind="time" rows={timeDraftRows} onChange={setTimeDraftRows} issues={timeConversion.issues} /> },
        { key: 'settings.wake_cooldown', label: `自动参与冷却秒数${formRangeSuffix('wake_cooldown')}`, type: 'number', ...formNumericLimits('wake_cooldown'), placeholder: '默认 30, 明确唤醒不受此限制' },
        { key: 'settings.wake_talk_value', label: `发言频率偏好 talk_value（${formRangeText('wake_talk_value')}, 留空不设置）`, type: 'number', ...formNumericLimits('wake_talk_value'), placeholder: TALK_VALUE_HINT },
        { key: 'settings.message_text_limit', label: `每条消息文本上限${formRangeSuffix('message_text_limit')}`, type: 'number', ...formNumericLimits('message_text_limit'), placeholder: '默认 2000 字符, 长消息优先按换行分段' },
        { key: 'settings.input_text_limit', label: `单条消息输入文本预算${formRangeSuffix('input_text_limit')}`, type: 'number', ...formNumericLimits('input_text_limit'), placeholder: '默认 20000 字符; 仅平台级, 超出部分截断并标注' },
        { key: 'settings.input_media_limit', label: `单条消息输入媒体上限${formRangeSuffix('input_media_limit')}`, type: 'number', ...formNumericLimits('input_media_limit'), placeholder: '默认 8 张/段; 仅平台级, 不进入群/时段覆盖' },
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
        { key: 'settings.command_operators', label: '命令操作员名单（留空则拒绝全部受保护命令）', type: 'textarea', rows: 2, placeholder: '每行一个平台用户 ID; 只有名单内的成员能执行 /approve 与 /plan, 群管理员身份不作数' },
        { key: 'settings.wake_words', label: '唤醒词（留空不启用词语触发）', type: 'textarea', rows: 3, placeholder: '每行一个唤醒词, 匹配当前消息正文' },
        { key: 'settings.wake_dry_run_preview', label: '唤醒规则试算预览', type: 'custom', render: () => <WakeDryRunPanel settings={previewSettings} blockedReason={draftError} /> },
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
      { key: 'settings_json', label: '其它 Settings JSON', type: 'textarea', rows: 12, placeholder: '其它适配器设置; 档案保留天数使用上方专用输入框' },
    ];
  }, [adapters, adapterTypes, asrConfigs, draftError, edictumConfigs, editingPlatform, formData.session_bindings, formData.session_provider, formData.session_type, formData.type, groupConversion, groupDraftRows, handleFieldChange, inheritedName, platforms, previewSettings, routeDeclaration, routeKinds, routeOptions, sessionClasses, timeConversion, timeDraftRows]);

  // 表单值
  const formValues = useMemo(() => ({
    enable: formData.enable,
    id: formData.id,
    type: formData.type,
    session_provider: formData.session_provider,
    session_type: formData.session_type,
    session_bindings: formData.session_bindings,
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
    'settings.command_operators': Array.isArray(formData.settings.command_operators) ? formData.settings.command_operators.join('\n') : formData.settings.command_operators ?? '',
    'settings.wake_group_overrides': groupDraftRows,
    'settings.wake_time_rules': timeDraftRows,
    'settings.wake_dry_run_preview': undefined,
    'settings.wake_mode': formData.settings.wake_mode ?? 'explicit',
    'settings.wake_message_threshold': formData.settings.wake_message_threshold ?? '',
    'settings.wake_score_threshold': formData.settings.wake_score_threshold ?? '',
    'settings.wake_max_wait': formData.settings.wake_max_wait ?? '',
    'settings.wake_cooldown': formData.settings.wake_cooldown ?? '',
    'settings.message_text_limit': formData.settings.message_text_limit ?? '',
    // 0 是有效取值 (关闭自动参与), 只有未设置才回填空字符串
    'settings.wake_talk_value': formData.settings.wake_talk_value ?? '',
    'settings.input_text_limit': formData.settings.input_text_limit ?? '',
    'settings.input_media_limit': formData.settings.input_media_limit ?? '',
    'settings.message_archive_retention_days': formData.settings.message_archive_retention_days ?? '',
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
  }), [editingPlatform, formData, groupDraftRows, rawSettings, timeDraftRows]);

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
                fetchSessionClasses();
                setDiagnosticsRefreshKey((key) => key + 1);
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
      {isRunning && Object.keys(adapters).length > 0 && (
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
                  <AdapterStatus info={info} application={health?.platform_config?.find((item) => item.id === id)} />
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
                {item.status === 'failed' && <Button className="mt-2" size="sm" variant="subtle" disabled={loading || saving || !isRunning}
                  onClick={() => { void applySavedPlatform(item.id, revision); }}>重试应用 {item.id}</Button>}
              </div>
            ))}
          </div>
        </Card>
      )}

      {/* 请求阶段诊断: 普通事件与手动请求共用同一近期诊断接口 */}
      <Card>
        <h3 className="text-lg font-semibold text-text-primary mb-2">请求阶段诊断</h3>
        <RequestDiagnosticsPanel
          adapterId={diagnosticsAdapter}
          adapterOptions={diagnosticsAdapters}
          onAdapterChange={setDiagnosticsAdapter}
          refreshKey={diagnosticsRefreshKey}
        />
      </Card>

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
                  {!!Object.keys(platform.session_bindings || {}).length && <div className="mt-2 flex flex-wrap gap-2">
                    {Object.entries(platform.session_bindings || {}).map(([kind, binding]) => <Badge key={kind} variant="info">
                      {adapterTypes.find((item) => item.type === platform.type)?.conversation_kinds[kind] || kind}: {binding.mode === 'inherit' ? '继承平台默认' : binding.config_name}
                    </Badge>)}
                  </div>}
                </div>
                <div className="flex shrink-0 items-center gap-2">
                <Button size="sm" variant="default" onClick={() => setTestingPlatformId(platform.id)}
                  aria-label={`测试连接 ${platform.id}`}>测试连接</Button>
                {(platform.type === 'onebot' || platform.type === 'aiocqhttp') && (
                  <Link className="glass-button px-3 py-1.5 text-sm" to={`/platforms/${encodeURIComponent(platform.id)}/groups`}>
                    管理群聊
                  </Link>
                )}
                <Link className="glass-button px-3 py-1.5 text-sm" to={`/platforms/${encodeURIComponent(platform.id)}/friends`}>
                  管理好友
                </Link>
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
              </div>
            ))}
          </div>
        )}
      </Card>

      <PlatformConnectionTest platform={platforms.find((item) => item.id === testingPlatformId) || null}
        revision={revision}
        identity={`${health?.runtime_id || ''}:${adapters[testingPlatformId || '']?.client_self_id || ''}:${adapters[testingPlatformId || '']?.status || ''}:${health?.platform_config?.find((item) => item.id === testingPlatformId)?.active_revision || ''}`}
        onClose={() => setTestingPlatformId(null)} />

      {/* 编辑/新增模态框: 策略字段提示统一由契约校验器给 toast, 关闭浏览器原生校验 */}
      <FormModal
        open={showModal}
        onClose={guardedClose}
        title={editingPlatform ? '编辑平台' : '添加平台'}
        fields={formFields}
        values={formValues}
        onChange={handleFieldChange}
        onSubmit={handleSubmit}
        submitText={editingPlatform ? '保存修改' : '创建'}
        loading={saving}
        size="lg"
        noValidate
      />
    </div>
  );
}
