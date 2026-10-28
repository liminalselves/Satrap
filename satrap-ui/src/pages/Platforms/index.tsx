import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useShallow } from 'zustand/react/shallow';
import { useConfigWatch } from '@/hooks/useConfigWatch';
import { useCachedState } from '@/hooks/useCachedState';
import { Link } from 'react-router-dom';
import { isAxiosError } from 'axios';
import {
  Activity, AlertTriangle, Cable, CheckCircle2, CircleDot, Clock3, FileJson, Gauge, Globe, ListChecks,
  Mail, Pencil, Plus, RefreshCw, Search, Server, ShieldCheck, Trash2, Users, Wifi, WifiOff,
} from 'lucide-react';
import { cn } from '@/utils/cn';
import { useBackendStore } from '@/stores/useBackendStore';
import { useConfigStore } from '@/stores/useConfigStore';
import {
  Badge, Button, Card, EmptyState, Input, Modal, PageHeader, Skeleton, Sparkline, Tabs,
} from '@/components/kit';
import { toast } from '@/components/ui/Toast';
import { PLATFORM_TYPES } from '@/utils/constants';
import { controlApi } from '@/api/control';
import { backendApi } from '@/api/backend';
import { statsApi, platformToggleApi, type Overview } from '@/api/contracts';
import { normalizePlatformSettings, platformSettingsSummary, TALK_VALUE_HINT, validatePlatformPolicy } from '@/utils/adminMigration';
import { formNumericLimits, formRangeSuffix, formRangeText } from '@/utils/wakePolicyContract';
import { confirmDiscard, useDirtyGuard } from '@/hooks/useDirtyGuard';
import { RequestDiagnosticsPanel } from '@/components/diagnostics/RequestDiagnosticsPanel';
import { fromGroupRows, fromTimeRows, toGroupRows, toTimeRows } from '@/utils/wakeOverrides';
import { WakeOverrideEditor } from './WakeOverrideEditor';
import { WakeDryRunPanel } from './WakeDryRunPanel';
import { PlatformConnectionTest } from './PlatformConnectionTest';
import { AgentRoutingEditor, agentOptions, bindingError } from './AgentRoutingEditor';
import { PlatformNavigation } from './PlatformNavigation';
import { PlatformEditor } from './PlatformEditor';
import { platformActionsApi, approvalLink, type ActionCatalog } from '@/api/platformActions';
import { startVisiblePoll } from '@/utils/visiblePoll';
import { errorText } from '@/utils/errorText';
import { createUuid } from '@/utils/uuid';
import { formatTime } from '@/utils/format';
import { Avatar, Metric, Note, PlatformIcon, SectionHead } from './shared';
import type { FormField } from '@/components/common';
import type { GroupOverrideRow, RowIssue, TimeRuleRow } from '@/utils/wakeOverrides';
import type {
  AdapterDeclaration, AdapterInfo, AgentBinding, PlatformConfig, PlatformConfigApplication,
} from '@/api/types';

type RuntimeStatus = 'running' | 'degraded' | 'stopped' | 'error';
type Filter = 'all' | 'online' | 'issues' | 'disabled';
type View = 'adapters' | 'apply' | 'diagnostics';
type ToggleRuntimeStatus = 'applied' | 'pending' | 'unconfirmed' | 'failed';

interface AdapterBot { self_id?: string | null; nickname?: string | null; avatar_url?: string | null; as_of?: number | null }
/** 后端健康契约新增的适配器字段; 旧服务缺少时保持 null/未知, 不用 0 冒充 */
interface AdapterHealthInfo extends AdapterInfo {
  connected_at?: number | null;
  disconnected_at?: number | null;
  reconnect_count?: number | null;
  last_heartbeat_at?: number | null;
  last_message_at?: number | null;
  latency_ms?: number | null;
  latency_kind?: 'rtt' | 'heartbeat' | string | null;
  protocol?: string | null;
  description?: string | null;
  bot?: AdapterBot | null;
}

const runtimeLabels: Record<RuntimeStatus, string> = {
  running: '在线', degraded: '降级', stopped: '已停止', error: '异常',
};
const runtimeTones: Record<RuntimeStatus, 'green' | 'orange' | 'neutral' | 'red'> = {
  running: 'green', degraded: 'orange', stopped: 'neutral', error: 'red',
};
const applyLabels: Record<string, string> = { applied: '已生效', pending_restart: '待重启', failed: '应用失败' };
const applyTones: Record<string, 'green' | 'orange' | 'red'> = { applied: 'green', pending_restart: 'orange', failed: 'red' };
/** 启停返回的运行时结论: 保存成功不等于运行时已应用 */
const toggleRuntimeLabels: Record<ToggleRuntimeStatus, string> = {
  applied: '运行时已应用', pending: '等待运行时应用', unconfirmed: '运行时状态未确认', failed: '运行时应用失败',
};
const toggleRuntimeTones: Record<ToggleRuntimeStatus, 'green' | 'orange' | 'red'> = {
  applied: 'green', pending: 'orange', unconfirmed: 'orange', failed: 'red',
};

function runtimeStatusOf(info?: AdapterHealthInfo): RuntimeStatus {
  if (!info) return 'stopped';
  if (info.status === 'error') return 'error';
  if (info.status === 'degraded') return 'degraded';
  if (info.status === 'running') return 'running';
  return 'stopped';
}

function durationText(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return '—';
  const total = Math.floor(seconds);
  if (total < 60) return `${total} 秒`;
  if (total < 3600) return `${Math.floor(total / 60)} 分 ${total % 60} 秒`;
  if (total < 86400) return `${Math.floor(total / 3600)} 小时 ${Math.floor((total % 3600) / 60)} 分`;
  return `${Math.floor(total / 86400)} 天 ${Math.floor((total % 86400) / 3600)} 小时`;
}

const sequence = (count: number): number[] => Array.from({ length: count }, () => 0);

export function Platforms() {
  useConfigWatch(['sessions', 'edictum', 'asr']);
  const { health, isRunning, refreshHealth } = useBackendStore(useShallow((state) => ({ health: state.health, isRunning: state.isRunning, refreshHealth: state.refreshHealth })));
  const { sessionClasses, fetchSessionClasses, asrConfigs, edictumConfigs, fetchEdictumConfigs: loadEdictumConfigs } = useConfigStore(useShallow((state) => ({
    sessionClasses: state.sessionClasses, fetchSessionClasses: state.fetchSessionClasses, asrConfigs: state.asrConfigs,
    edictumConfigs: state.edictumConfigs, fetchEdictumConfigs: state.fetchEdictumConfigs })));
  const [revision, setRevision] = useState('');
  const [approvalSummary, setApprovalSummary] = useCachedState<ActionCatalog | undefined>('platforms:approval-summary', undefined);
  const [approvalError, setApprovalError] = useState('');
  useEffect(() => {
    setApprovalError('');
    // 后端停止时旧计数不再可信; 运行中先沿用上次结果, 由轮询更新
    if (!isRunning) { setApprovalSummary(undefined); return; }
    const poll = startVisiblePoll(async (signal) => {
      try {
        const result = await platformActionsApi.summary(signal);
        if (!signal.aborted) { setApprovalSummary(result); setApprovalError(''); }
      } catch (error) { if (!signal.aborted) setApprovalError(errorText(error)); }
    }, { interval: 10000 });
    return () => poll.stop();
  }, [isRunning, setApprovalSummary]);
  const [draftRevision, setDraftRevision] = useState('');
  // 展示数据跨路由保留; revision 不缓存, 写操作仍需等本次读取返回
  const [platforms, setPlatforms] = useCachedState<PlatformConfig[]>('platforms:list', []);
  const [adapterTypes, setAdapterTypes] = useCachedState<AdapterDeclaration[]>('platforms:adapter-types', []);
  const [defaultSessionType, setDefaultSessionType] = useCachedState('platforms:default-session-type', '');
  const [testingPlatformId, setTestingPlatformId] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [togglingId, setTogglingId] = useState<string | null>(null);
  const [retryingId, setRetryingId] = useState<string | null>(null);
  const [showModal, setShowModal] = useState(false);
  const [editingPlatform, setEditingPlatform] = useState<PlatformConfig | null>(null);
  const [conflict, setConflict] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<PlatformConfig | null>(null);
  const [rawSettings, setRawSettings] = useState('{}');
  // 群/时段覆盖的草稿行由表单持有: 不完整行不会被静默丢弃, 也参与脏状态比较
  const [groupDraftRows, setGroupDraftRows] = useState<GroupOverrideRow[]>([]);
  const [timeDraftRows, setTimeDraftRows] = useState<TimeRuleRow[]>([]);
  const [openSnapshot, setOpenSnapshot] = useState('');
  const [stats, setStats] = useCachedState<Overview | null>('platforms:stats', null);
  const [statsError, setStatsError] = useState('');
  const [view, setView] = useState<View>('adapters');
  const [filter, setFilter] = useState<Filter>('all');
  const [query, setQuery] = useState('');
  // 最近一次启停的运行时结论, 按平台保留; 与保存状态分开展示
  const [toggleRuntime, setToggleRuntime] = useState<Record<string, { desired: boolean; status: ToggleRuntimeStatus; reason: string | null }>>({});
  const [formData, setFormData] = useState({
    enable: true,
    id: '',
    type: 'misskey',
    session_provider: 'session_class',
    session_type: '',
    session_bindings: {} as Record<string, AgentBinding>,
    settings: {} as Record<string, unknown>,
  });

  // 从 health 中提取适配器信息; 新契约字段可能缺失, 保持未知
  const adapters = useMemo(() => (health?.adapters || {}) as Record<string, AdapterHealthInfo>, [health?.adapters]);
  const adapterList = useMemo(() => Object.entries(adapters), [adapters]);
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
  }, [setPlatforms, setAdapterTypes, setDefaultSessionType]);

  // 14 分钟分钟桶: 每个适配器的消息曲线与近期总量; 失败时显示未知而不是 0
  const statsPoll = useRef<ReturnType<typeof startVisiblePoll> | null>(null);
  useEffect(() => {
    const poll = startVisiblePoll(async (signal) => {
      const to = Math.floor(Date.now() / 1000);
      try {
        const overview = await statsApi.overview({ from: to - 14 * 60, to, bucket: 'minute' }, signal);
        if (signal.aborted) return;
        setStats(overview); setStatsError('');
      } catch (error) {
        if (!signal.aborted) setStatsError(errorText(error));
      }
    }, { interval: 30000 });
    statsPoll.current = poll;
    return () => { poll.stop(); statsPoll.current = null; };
  }, [setStats]);

  useEffect(() => {
    loadPlatforms();
    refreshHealth();
  }, [loadPlatforms, refreshHealth]);

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
    setConflict(false);
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
    setConflict(false);
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
    setRetryingId(id);
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
    } finally {
      setRetryingId(null);
    }
  }, [refreshHealth]);

  const handleDelete = useCallback(async (id: string) => {
    try {
      if (!revision) throw new Error('请先刷新平台配置后再删除');
      const result = await controlApi.deletePlatform(id, revision);
      if (!result.ok) throw new Error(result.error || '删除失败');
      setPlatforms(result.platforms || []);
      setRevision(result.revision || '');
      setDeleteTarget(null);
      if (health?.running) {
        await applySavedPlatform(id, result.revision);
      } else {
        toast('success', '平台已删除');
      }
    } catch (e) {
      toast('error', '删除失败: ' + (isAxiosError(e) && e.response?.status === 409 ? '配置已被其他操作修改, 请刷新后重试' : e instanceof Error ? e.message : '未知错误'));
    }
  }, [health?.running, applySavedPlatform, revision, setPlatforms]);

  // 启停走新契约: saved 与 runtime.status 分开呈现, 不把保存成功说成运行中已生效
  const handleToggle = useCallback(async (platform: PlatformConfig) => {
    if (togglingId) return;
    if (!revision) { toast('error', '请先刷新平台配置后再切换启停'); return; }
    const desired = !(platform.enable ?? true);
    setTogglingId(platform.id);
    try {
      const result = await platformToggleApi.set(platform.id, desired, revision, createUuid());
      if (!result.saved) throw new Error(result.runtime?.reason_code || '保存失败');
      setRevision(result.revision || revision);
      setPlatforms((old) => old.map((item) => (item.id === platform.id ? { ...item, enable: desired } : item)));
      const runtime = result.runtime;
      const status: ToggleRuntimeStatus = runtime?.status === 'applied' || runtime?.status === 'pending'
        || runtime?.status === 'failed' ? runtime.status : 'unconfirmed';
      setToggleRuntime((old) => ({ ...old, [platform.id]: { desired, status, reason: runtime?.reason_code || null } }));
      await refreshHealth();
      if (!runtime) toast('warning', '配置已保存, 无法确认运行时状态');
      else if (runtime.status === 'applied') {
        toast('success', `${desired ? '平台已启用' : '平台已停用'}: ${desired ? '运行时已重新连接' : '运行时已停止接收新消息'}`);
      } else if (runtime.status === 'pending') {
        toast('warning', '配置已保存, 变更待运行时应用');
      } else if (runtime.status === 'failed') {
        toast('warning', '配置已保存, 运行时应用失败: ' + (runtime.reason_code || '请检查适配器连接'));
      } else {
        toast('warning', '配置已保存, 无法确认运行时是否已应用');
      }
    } catch (e) {
      toast('error', '切换失败: ' + (isAxiosError(e) && e.response?.status === 409
        ? '配置已被其他操作修改, 请刷新后重试' : errorText(e)));
    } finally {
      setTogglingId(null);
    }
  }, [refreshHealth, revision, togglingId, setPlatforms]);

  const handleSubmit = useCallback(async () => {
    setConflict(false);
    if (!formData.id.trim()) {
      toast('error', '平台名称不能为空');
      return;
    }
    if (formData.session_provider === 'edictum' && !formData.session_type.trim()) {
      toast('error', 'Edictum 必须绑定一个命名配置');
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
      if (isAxiosError(e) && e.response?.status === 409) setConflict(true);
      toast('error', '保存失败: ' + (isAxiosError(e) && e.response?.status === 409 ? '配置已被其他操作修改, 当前草稿已保留; 请复制草稿并刷新后合并' : e instanceof Error ? e.message : '未知错误'));
    } finally {
      setSaving(false);
    }
  }, [editingPlatform, formData, health?.running, rawSettings, applySavedPlatform, draftError, draftRevision, settingsDraft, routeDeclaration, routeKinds, routeOptions, setPlatforms]);

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
        label: '平台默认流程',
        type: 'select',
        options: [
          { value: 'session_class', label: 'SessionClass' },
          { value: 'edictum', label: 'Edictum' },
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
        { key: 'settings.wake_mode', label: '自动参与模式', type: 'select', options: [{ value: 'explicit', label: '仅明确唤醒（默认）' }, { value: 'frequency', label: '按正文消息数量触发' }, { value: 'necessity', label: '按本地回复必要性评分' }, { value: 'all', label: '每条消息触发' }] },
        ...(formData.settings.wake_mode === 'all' ? [{ key: 'settings.wake_mode_note', label: '每条消息触发说明', type: 'custom' as const, render: () => <p className="text-xs text-fg-3">无需 @ 或唤醒词, 数量/必要性阈值、冷却和最长等待不生效; 平台准入、限流和队列上限仍生效, 不保证每条消息单独调用模型或必然回复</p> }] : []),
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

  const applications = useMemo(() => (health?.platform_config || []) as PlatformConfigApplication[], [health?.platform_config]);
  const applicationOf = useCallback((id: string) => applications.find((item) => item.id === id), [applications]);
  const platformIds = useMemo(() => new Set(platforms.map((item) => item.id)), [platforms]);
  const orphanApplications = useMemo(() => applications.filter((item) => !platformIds.has(item.id)), [applications, platformIds]);
  const extraRuntime = useMemo(() => adapterList.filter(([id]) => !platformIds.has(id)), [adapterList, platformIds]);

  const runtimeOf = useCallback((id: string) => adapters[id], [adapters]);
  const statOf = useCallback((id: string) => stats?.by_adapter?.find((item) => item.platform_id === id), [stats]);

  const filteredPlatforms = useMemo(() => platforms.filter((platform) => {
    const text = query.trim().toLowerCase();
    if (text && !`${platform.id} ${platform.type}`.toLowerCase().includes(text)) return false;
    if (filter === 'disabled') return !(platform.enable ?? true);
    if (filter === 'online') return runtimeStatusOf(runtimeOf(platform.id)) === 'running';
    if (filter === 'issues') {
      const application = applicationOf(platform.id);
      return (application && application.status !== 'applied') || runtimeStatusOf(runtimeOf(platform.id)) === 'error';
    }
    return true;
  }), [applicationOf, filter, platforms, query, runtimeOf]);

  const online = adapterList.filter(([, info]) => info.status === 'running').length;
  const issueCount = applications.filter((item) => item.status !== 'applied').length
    + adapterList.filter(([, info]) => info.status === 'error').length;
  const latencies = adapterList.map(([, info]) => info.latency_ms).filter((value): value is number => typeof value === 'number');
  const averageLatency = latencies.length ? Math.round(latencies.reduce((sum, value) => sum + value, 0) / latencies.length) : null;
  const recentMessages = stats?.messages && stats.messages.inbound !== null && stats.messages.outbound !== null
    ? stats.messages.inbound + stats.messages.outbound
    : null;
  const unknown = <span className="text-fg-3">—</span>;

  const refreshAll = () => {
    void refreshHealth();
    void loadPlatforms();
    loadEdictumConfigs();
    void fetchSessionClasses();
    setDiagnosticsRefreshKey((key) => key + 1);
    statsPoll.current?.request();
  };

  const copyDraft = () => {
    void navigator.clipboard?.writeText(JSON.stringify({ form: formData, groups: groupDraftRows, times: timeDraftRows }, null, 2))
      .then(() => toast('success', '草稿已复制: 可粘贴到其他窗口后合并'))
      .catch(() => toast('error', '复制失败: 浏览器未授予剪贴板权限'));
  };

  return (
    <div className="space-y-5">
      <PageHeader
        title="平台状态"
        description="管理平台适配器配置、连接健康与运行状态"
        eyebrow={<div className="flex items-center gap-2 text-[11px] font-medium uppercase tracking-[0.14em] text-tint tint-pink"><Globe className="h-3.5 w-3.5" />平台 / Platforms</div>}
        actions={<>
          <Button variant="ghost" icon={<RefreshCw className={cn('h-4 w-4', loading && 'animate-spin-slow')} />} onClick={refreshAll} disabled={loading}>刷新</Button>
          <Button variant="primary" icon={<Plus className="h-4 w-4" />} onClick={handleAdd}>添加平台</Button>
        </>}
      />

      <PlatformNavigation pending={approvalError ? undefined : approvalSummary?.pending_count} incomplete={!!approvalError || !isRunning || !!(approvalSummary && !approvalSummary.complete)} />
      {approvalError && <p role="status" className="text-warning">审批数量暂未确认: {approvalError}</p>}

      <div className="stagger grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <Metric tint="pink" icon={<Wifi className="h-[18px] w-[18px]" />} label="在线适配器"
          value={`${online} / ${adapterList.length}`} hint={adapterList.length ? `共 ${platforms.length} 个平台配置` : '尚未读取到运行中的适配器'} />
        <Metric tint="pink" icon={<Activity className="h-[18px] w-[18px]" />} label="近 14 分钟消息"
          value={recentMessages === null ? unknown : recentMessages.toLocaleString()}
          hint={statsError ? '统计暂不可用' : stats ? `分钟桶 · 覆盖${stats.complete ? '完整' : '不完整'}` : '读取中'} />
        <Metric tint="teal" icon={<Gauge className="h-[18px] w-[18px]" />} label="平均延迟"
          value={averageLatency === null ? unknown : `${averageLatency} ms`} hint={latencies.length ? `${latencies.length} 个适配器有往返探测` : '没有可关联响应的探测'} />
        <Metric tint={issueCount ? 'orange' : 'green'} icon={issueCount ? <AlertTriangle className="h-[18px] w-[18px]" /> : <CheckCircle2 className="h-[18px] w-[18px]" />}
          label="需要处理" value={`${issueCount} 项`} hint={issueCount ? '配置未生效或连接异常' : '全部配置已生效'} />
      </div>

      {statsError && <Note tone="orange" title="统计暂不可用">近 14 分钟消息与适配器曲线无法读取: {statsError}</Note>}
      {stats && !stats.complete && (
        <Note tone="orange" title="统计覆盖不完整">
          {stats.warnings?.length ? stats.warnings.join('; ') : '部分来源未覆盖, 未知值以 — 显示。'}
          <span className="ml-2 inline-flex flex-wrap gap-1 align-middle">
            {stats.sources?.filter((source) => source.state !== 'available').map((source) => (
              <Badge key={source.service} tone="orange">{source.service}: {source.state}</Badge>
            ))}
          </span>
        </Note>
      )}

      <div className="flex flex-wrap items-center gap-3 animate-rise">
        <Tabs label="平台视图" value={view} onChange={setView} items={[
          { value: 'adapters' as const, label: '适配器', icon: <Server className="h-3.5 w-3.5" />, count: platforms.length },
          { value: 'apply' as const, label: '配置生效', icon: <ListChecks className="h-3.5 w-3.5" />, count: issueCount },
          { value: 'diagnostics' as const, label: '请求诊断', icon: <FileJson className="h-3.5 w-3.5" /> },
        ]} />
        {view === 'adapters' && (
          <div className="ml-auto flex flex-wrap items-center gap-2">
            <div className="w-56">
              <Input leading={<Search className="h-4 w-4" />} placeholder="筛选名称或类型…" value={query}
                onChange={(event) => setQuery(event.target.value)} aria-label="筛选平台" className="!h-8 !text-[13px]" />
            </div>
            <div className="flex items-center gap-1 rounded-[10px] border border-line bg-glass p-0.5">
              {([['all', '全部'], ['online', '在线'], ['issues', '需处理'], ['disabled', '停用']] as const).map(([value, label]) => (
                <button key={value} type="button" onClick={() => setFilter(value)} aria-pressed={filter === value}
                  className={cn('h-7 rounded-lg px-2.5 text-[12px] transition', filter === value ? 'bg-[rgb(var(--rgb-pink)/0.16)] font-medium text-pink' : 'text-fg-3 hover:text-fg')}>
                  {label}
                </button>
              ))}
            </div>
          </div>
        )}
        <span className="ml-auto hidden items-center gap-2 text-[11.5px] text-fg-4 sm:flex">
          <Clock3 className="h-3.5 w-3.5" />健康探测随状态通道刷新 · 诊断按需读取
        </span>
      </div>

      {view === 'adapters' && (loading && platforms.length === 0 ? (
        <div className="grid gap-4 xl:grid-cols-2">
          {[0, 1].map((index) => <div key={index} className="surface min-h-[220px] p-4"><Skeleton className="h-full min-h-[180px] rounded-xl" /></div>)}
        </div>
      ) : filteredPlatforms.length === 0 ? (
        <Card padding="none" className="animate-rise">
          <EmptyState tint="pink" icon={<Globe className="h-6 w-6" />} title={platforms.length ? '没有匹配的平台' : '暂无平台配置'}
            description={platforms.length ? '换一个关键字或清除筛选条件, 也可以添加一个新的适配器。' : '添加平台后, 启动后端即可连接。'}
            actions={<>
              {platforms.length > 0 && <Button onClick={() => { setQuery(''); setFilter('all'); }}>清除筛选</Button>}
              <Button variant="primary" icon={<Plus className="h-4 w-4" />} onClick={handleAdd}>添加平台</Button>
            </>} />
        </Card>
      ) : (
        <div className="stagger grid gap-4 xl:grid-cols-2">
          {filteredPlatforms.map((platform, index) => {
            const runtime = runtimeOf(platform.id);
            const application = applicationOf(platform.id);
            const status = runtimeStatusOf(runtime);
            const enabled = platform.enable ?? true;
            const adapterStats = statOf(platform.id);
            const series = adapterStats?.series || [];
            const knownSeries = series.some((bucket) => bucket.messages.inbound !== null || bucket.messages.outbound !== null);
            const spark = series.length ? series.map((bucket) => (bucket.messages.inbound ?? 0) + (bucket.messages.outbound ?? 0)) : sequence(14);
            // 只有确实有计数覆盖的分钟桶才求和; 全部未知时保持 null, 不显示成 0
            const knownBuckets = series.filter((bucket) => bucket.messages.inbound !== null || bucket.messages.outbound !== null);
            const recent = knownBuckets.length
              ? knownBuckets.reduce((sum, bucket) => sum + (bucket.messages.inbound ?? 0) + (bucket.messages.outbound ?? 0), 0)
              : null;
            const bot = runtime?.bot;
            const selfId = bot?.self_id || runtime?.client_self_id || '';
            const accountName = bot?.nickname || selfId || '机器人账号';
            const connectedAt = runtime?.connected_at;
            const uptime = connectedAt ? durationText(Date.now() / 1000 - connectedAt) : null;
            const supportsGroups = platform.type === 'onebot' || platform.type === 'aiocqhttp';
            // 生效的默认 Agent: 平台显式指定优先, 否则按会话类同名回退
            const effectiveAgent = platform.session_type
              || (platform.session_provider !== 'edictum' && sessionClasses[platform.type] ? platform.type : '');
            const agentOption = effectiveAgent
              ? routeOptions.find((option) => option.provider === (platform.session_provider || 'session_class') && option.name === effectiveAgent)
              : undefined;
            const toggleState = toggleRuntime[platform.id];
            return (
              <div key={platform.id} style={{ ['--i' as string]: index }}>
                <Card padding="none" tint={status === 'error' ? 'red' : status === 'degraded' ? 'orange' : 'pink'}
                  flowing={status === 'running' && (recent ?? 0) > 500}
                  className={cn('group flex flex-col overflow-hidden animate-rise', !enabled && 'opacity-80')}>
                  <div className="flex items-start gap-3 p-4 pb-3">
                    <PlatformIcon type={platform.type} size="lg" />
                    <h3 className="min-w-0 flex-1 truncate text-[15px] font-semibold text-fg">{platform.id}</h3>
                    <Badge tone="pink">{platform.type}</Badge>
                    {!enabled && <Badge>已停用</Badge>}
                    <Badge tone={runtimeTones[status]} dot live={status === 'running'}>{runtimeLabels[status]}</Badge>
                  </div>
                  <p className="mx-4 line-clamp-2 text-[12px] leading-5 text-fg-3">
                    {runtime?.description || platformSettingsSummary(platform.type, platform.settings)}
                  </p>

                  <div className="mx-4 mt-3 flex flex-wrap items-center gap-x-4 gap-y-1.5 rounded-xl border border-line bg-glass px-3 py-2">
                    {selfId ? (
                      <>
                        <span className="flex items-center gap-2">
                          <Avatar name={accountName} image={bot?.avatar_url} size="sm" online={status === 'running'} />
                          <span className="min-w-0">
                            <span className="block truncate text-[12.5px] font-medium text-fg">{accountName}</span>
                            <span className="num block font-mono text-[10.5px] text-fg-4">{selfId}</span>
                          </span>
                        </span>
                        <span className="hidden h-7 w-px bg-[var(--glass-border)] sm:block" />
                        <span className="text-[11.5px] text-fg-3">
                          {connectedAt ? <>已连接 <span className="num font-mono text-fg-2">{uptime}</span></> : runtime?.disconnected_at ? `最近断线 ${formatTime(runtime.disconnected_at)}` : '连接时间未确认'}
                        </span>
                        {typeof runtime?.reconnect_count === 'number' && runtime.reconnect_count > 0 && (
                          <Badge tone="orange">重连 {runtime.reconnect_count} 次</Badge>
                        )}
                        <span className="ml-auto flex items-center gap-1.5 text-[11.5px]" style={{ color: runtime?.latency_ms === null || runtime?.latency_ms === undefined ? 'var(--fg-4)' : 'var(--fg-2)' }}>
                          <Gauge className="h-3.5 w-3.5" />
                          <span className="num font-mono">{runtime?.latency_ms === null || runtime?.latency_ms === undefined ? '延迟未知' : `${runtime.latency_ms} ms`}</span>
                          {runtime?.latency_kind && <span className="text-[10px] text-fg-4">{runtime.latency_kind === 'rtt' ? '往返' : runtime.latency_kind === 'heartbeat' ? '心跳' : runtime.latency_kind}</span>}
                        </span>
                      </>
                    ) : (
                      <span className="flex items-center gap-2 text-[12px] text-fg-3"><CircleDot className="h-3.5 w-3.5 text-orange" />等待机器人账号确认</span>
                    )}
                  </div>

                  <div className="grid grid-cols-[1fr_1.2fr] gap-3 p-4 pt-3">
                    <div className="space-y-2">
                      <div className="flex items-baseline gap-1.5">
                        <span className="display num text-[24px] font-semibold leading-none text-fg">{recent === null ? '—' : recent.toLocaleString()}</span>
                        <span className="text-[11px] text-fg-3">条 / 近 14 分钟</span>
                      </div>
                      <div className="flex flex-wrap gap-1.5">
                        <Badge tone={enabled ? 'green' : 'neutral'} dot>{enabled ? '平台已启用' : '平台已停用'}</Badge>
                        <Badge tone={runtime?.started ? 'green' : 'neutral'}>{runtime?.started ? '适配器已启动' : '适配器未启动'}</Badge>
                        <Badge tone={agentOption ? (agentOption.enabled ? 'neutral' : 'orange') : 'neutral'}>
                          {agentOption ? (agentOption.enabled ? '会话配置启用' : '会话配置停用') : '会话配置未知'}
                        </Badge>
                        <Badge tone={application?.status === 'applied' ? 'green' : application?.status === 'failed' ? 'red' : application ? 'orange' : 'neutral'} dot={application?.status !== 'applied'}>
                          {application ? applyLabels[application.status] : '应用状态未知'}
                        </Badge>
                        {toggleState && (
                          <Badge tone={toggleRuntimeTones[toggleState.status]} dot>{toggleRuntimeLabels[toggleState.status]}</Badge>
                        )}
                      </div>
                    </div>
                    <div className="tint-pink">
                      <div className="mb-0.5 flex items-center justify-between text-[10.5px] text-fg-4">
                        <span>消息速率</span><span className="num font-mono">近 14 分钟</span>
                      </div>
                      {knownSeries && series.length > 1
                        ? <Sparkline data={spark} className="!h-10" />
                        : <div className="grid h-10 place-items-center text-[11px] text-fg-4">统计暂不可用</div>}
                    </div>
                  </div>

                  <div className="mx-4 mb-3 space-y-2 border-t border-line pt-3 text-[12px]">
                    <p className="flex items-start gap-2 text-fg-3">
                      <span className="mt-1 h-1.5 w-1.5 flex-none rounded-full bg-pink" />
                      <span className="min-w-0 truncate">
                        默认 Agent：<span className="font-mono text-fg-2">{platform.session_provider === 'edictum' ? 'Edictum' : 'SessionClass'} / {platform.session_type || '自动'}</span>
                      </span>
                    </p>
                    {!!Object.keys(platform.session_bindings || {}).length && (
                      <div className="flex flex-wrap gap-1.5 pl-3.5">
                        {Object.entries(platform.session_bindings || {}).map(([kind, binding]) => (
                          <Badge key={kind} tone="pink">
                            {adapterTypes.find((item) => item.type === platform.type)?.conversation_kinds[kind] || kind}: {binding.mode === 'inherit' ? '继承平台默认' : binding.config_name}
                          </Badge>
                        ))}
                      </div>
                    )}
                    {runtime?.last_error && (
                      <p className="flex items-start gap-1.5 rounded-lg bg-[rgb(var(--rgb-orange)/0.1)] px-2 py-1.5 text-[11.5px] text-orange">
                        <AlertTriangle className="mt-0.5 h-3.5 w-3.5 flex-none" />{runtime.last_error}
                      </p>
                    )}
                    {application && (
                      <p className="num font-mono text-[11px] text-fg-4">{`保存版本 ${application.saved_revision?.slice(0, 12) || '未知'} · 生效版本 ${application.active_revision?.slice(0, 12) || '无'}`}</p>
                    )}
                    {application?.status === 'failed' && (
                      <p className="flex flex-wrap items-center gap-1.5 rounded-lg bg-[rgb(var(--rgb-red)/0.1)] px-2 py-1.5 text-[11.5px] text-red">
                        <AlertTriangle className="mt-0.5 h-3.5 w-3.5 flex-none" />
                        <span className="min-w-0 flex-1">{application.error || '运行时配置应用失败'}</span>
                        <button type="button" className="font-medium underline-offset-2 hover:underline" disabled={retryingId === platform.id}
                          onClick={() => void applySavedPlatform(platform.id, revision)}>
                          {retryingId === platform.id ? '重试中…' : `重试应用 ${platform.id}`}
                        </button>
                      </p>
                    )}
                    {application?.status === 'pending_restart' && application.reason && <p className="text-[11.5px] text-orange">{application.reason}</p>}
                    {runtime?.last_heartbeat_at != null && <p className="text-[11px] text-fg-4">最近心跳 {formatTime(runtime.last_heartbeat_at)}</p>}
                    {runtime?.last_message_at != null && <p className="text-[11px] text-fg-4">最近消息 {formatTime(runtime.last_message_at)}</p>}
                    {runtime?.protocol && <p className="text-[11px] text-fg-4">协议 {runtime.protocol}</p>}
                  </div>

                  <div className="mt-auto flex flex-wrap items-center gap-1.5 border-t border-line bg-[rgb(0_0_0/0.06)] px-4 py-2.5 [[data-theme=light]_&]:bg-white/30">
                    <Button size="sm" variant="tinted" tint="pink" icon={<Cable className="h-3.5 w-3.5" />}
                      aria-label={`测试连接 ${platform.id}`} onClick={() => setTestingPlatformId(platform.id)}>连接测试</Button>
                    <Button size="sm" variant="ghost" icon={<Pencil className="h-3.5 w-3.5" />} title="编辑" onClick={() => handleEdit(platform)}>编辑</Button>
                    {supportsGroups && (
                      <Link className="ctl ctl-ghost ctl-sm" to={`/platforms/${encodeURIComponent(platform.id)}/groups`}>
                        <Users className="h-3.5 w-3.5" />群聊
                      </Link>
                    )}
                    <Link className="ctl ctl-ghost ctl-sm" to={`/platforms/${encodeURIComponent(platform.id)}/friends`}>
                      <Mail className="h-3.5 w-3.5" />好友
                    </Link>
                    <Link className="ctl ctl-ghost ctl-sm" to={approvalLink({ platform: platform.id })}>
                      <ShieldCheck className="h-3.5 w-3.5" />审批
                    </Link>
                    <Button size="sm" variant="ghost" icon={enabled ? <WifiOff className="h-3.5 w-3.5" /> : <Wifi className="h-3.5 w-3.5" />}
                      disabled={togglingId === platform.id} onClick={() => void handleToggle(platform)}>
                      {togglingId === platform.id ? '处理中…' : enabled ? '停用' : '启用'}
                    </Button>
                    <Button size="sm" variant="ghost" className="!text-red hover:!bg-[rgb(var(--rgb-red)/0.12)]" icon={<Trash2 className="h-3.5 w-3.5" />}
                      title="删除" onClick={() => setDeleteTarget(platform)}>删除</Button>
                    <span className="ml-auto flex items-center gap-1 text-[11px] text-fg-4">
                      <Clock3 className="h-3 w-3" />{revision ? `修订 ${revision}` : '修订未知'}
                    </span>
                  </div>
                </Card>
              </div>
            );
          })}
        </div>
      ))}

      {view === 'adapters' && extraRuntime.length > 0 && (
        <Card padding="md" className="tint-pink animate-rise">
          <SectionHead tint="pink" title="运行时观察到的其它适配器" description="存在于健康上报但未写入配置文件, 只读"
            actions={<Badge tone="pink">{extraRuntime.length}</Badge>} />
          <div className="space-y-2">
            {extraRuntime.map(([id, info]) => (
              <div key={id} className="flex flex-wrap items-center gap-3 rounded-xl border border-line bg-glass px-3 py-2.5">
                <PlatformIcon type={info.config_type || info.type || 'unknown'} size="sm" />
                <div className="min-w-0">
                  <div className="flex items-center gap-2 text-[13px] font-medium text-fg">{id}<Badge tone="pink">{info.config_type || info.type || 'unknown'}</Badge></div>
                  <p className="text-[11.5px] text-fg-3">{info.session_provider || 'session_class'} / {info.session_type || '自动'} · {info.bot?.self_id || info.client_self_id || '账号未确认'}</p>
                </div>
                <Badge tone="green" dot live className="ml-auto">{runtimeLabels[runtimeStatusOf(info)]}</Badge>
                <Button size="sm" variant="ghost" disabled>未纳入配置</Button>
              </div>
            ))}
          </div>
        </Card>
      )}

      {view === 'apply' && (
        <div className="space-y-4 animate-rise">
          <Note tone="pink" title="保存 ≠ 生效">
            配置保存后由运行时热更新。应用失败时保留已保存版本, 可以重试应用或检查适配器连接; 未确认时不要假设变更已生效。
          </Note>
          <Card padding="none">
            <div className="flex flex-wrap items-center justify-between gap-3 border-b border-line px-4 py-3">
              <div>
                <h3 className="text-[14px] font-semibold text-fg">配置生效状态</h3>
                <p className="text-[12px] text-fg-3">保存修订与运行中生效修订的对照</p>
              </div>
              <Badge tone={issueCount ? 'orange' : 'green'}>{issueCount} 项待处理</Badge>
            </div>
            {platforms.length === 0 ? (
              <EmptyState tint="pink" icon={<Globe className="h-6 w-6" />} title="暂无平台配置" description="添加平台后, 这里会展示保存修订与生效修订的对照。" />
            ) : (
              <div className="divide-y divide-[var(--glass-border)]">
                {platforms.map((platform) => {
                  const application = applicationOf(platform.id);
                  const enabled = platform.enable ?? true;
                  return (
                    <div key={platform.id} className="flex flex-wrap items-center gap-3 px-4 py-3">
                      <PlatformIcon type={platform.type} size="sm" />
                      <div className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="text-[13px] font-medium text-fg">{platform.id}</span>
                          <Badge tone={application?.status === 'applied' ? 'green' : application?.status === 'failed' ? 'red' : application ? 'orange' : 'neutral'} dot={application?.status !== 'applied'}>
                            {application ? applyLabels[application.status] : '应用状态未知'}
                          </Badge>
                          {!enabled && <Badge>已停用</Badge>}
                          {toggleRuntime[platform.id] && (
                            <Badge tone={toggleRuntimeTones[toggleRuntime[platform.id].status]} dot>
                              {toggleRuntimeLabels[toggleRuntime[platform.id].status]}
                            </Badge>
                          )}
                        </div>
                        <p className="mt-1 text-[11.5px] text-fg-3">
                          保存版本 <span className="num font-mono">{application?.saved_revision?.slice(0, 14) || '未知'}</span>
                          {' · '}生效版本 <span className="num font-mono">{application?.active_revision?.slice(0, 14) || '无'}</span>
                        </p>
                        {(application?.error || application?.reason) && (
                          <p className={cn('mt-1 text-[11.5px]', application?.status === 'failed' ? 'text-red' : 'text-orange')}>
                            {application?.error || application?.reason}
                          </p>
                        )}
                        {toggleRuntime[platform.id]?.reason && (
                          <p className="mt-1 text-[11.5px] text-orange">运行时原因: {toggleRuntime[platform.id].reason}</p>
                        )}
                      </div>
                      <div className="flex items-center gap-1.5">
                        <Button size="sm" variant="ghost" icon={<Cable className="h-3.5 w-3.5" />} aria-label={`测试连接 ${platform.id}`}
                          onClick={() => setTestingPlatformId(platform.id)}>检查</Button>
                        {application && application.status !== 'applied' && (
                          <Button size="sm" variant="tinted" tint="pink" loading={retryingId === platform.id}
                            onClick={() => void applySavedPlatform(platform.id, revision)}>重试应用</Button>
                        )}
                        <Button size="sm" variant="ghost" icon={<Pencil className="h-3.5 w-3.5" />} aria-label={`编辑 ${platform.id}`} title="编辑"
                          onClick={() => handleEdit(platform)} />
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </Card>

          {extraRuntime.length > 0 && (
            <Card padding="md" className="tint-orange">
              <SectionHead tint="orange" title="运行中但未纳入配置" description="这些适配器不会出现在配置文件与热更新流程中" />
              <div className="space-y-2">
                {extraRuntime.map(([id, info]) => (
                  <div key={id} className="flex flex-wrap items-center gap-3 rounded-xl border border-line bg-glass px-3 py-2.5 text-[12.5px]">
                    <PlatformIcon type={info.config_type || info.type || 'unknown'} size="sm" />
                    <span className="font-medium text-fg">{id}</span>
                    <Badge tone="pink">{info.config_type || info.type || 'unknown'}</Badge>
                    <span className="text-fg-3">{info.session_type || '自动'}</span>
                    <Badge tone="green" dot live className="ml-auto">{runtimeLabels[runtimeStatusOf(info)]}</Badge>
                  </div>
                ))}
              </div>
            </Card>
          )}

          {orphanApplications.length > 0 && (
            <Card padding="md" className="tint-orange">
              <SectionHead tint="orange" title="未纳入配置的应用记录" description="配置已被删除或重建, 仍可在运行时状态中看到最近一次应用结果" />
              <div className="space-y-2">
                {orphanApplications.map((application) => (
                  <div key={application.id} className="flex flex-wrap items-center gap-3 rounded-xl border border-line bg-glass px-3 py-2.5 text-[12.5px]">
                    <span className="font-medium text-fg">{application.id} 的配置</span>
                    <Badge tone={applyTones[application.status] || 'neutral'}>{applyLabels[application.status] || application.status}</Badge>
                    <span className="num font-mono text-[11px] text-fg-4">{`保存版本 ${application.saved_revision?.slice(0, 12) || '未知'} · 生效版本 ${application.active_revision?.slice(0, 12) || '无'}`}</span>
                    {application.error && <span className="text-[11.5px] text-red">{application.error}</span>}
                  </div>
                ))}
              </div>
            </Card>
          )}

          <Card padding="md" className="tint-pink">
            <SectionHead tint="pink" title="审批与诊断" description="待审批动作统一在审批中心处理; 请求诊断见对应标签页" />
            <div className="flex flex-wrap gap-2">
              <Link to={approvalLink()} className="ctl ctl-tinted tint-pink">
                <ShieldCheck className="h-4 w-4" />前往审批中心{approvalSummary ? ` · ${approvalSummary.pending_count}` : ''}
              </Link>
              <Button variant="ghost" icon={<FileJson className="h-4 w-4" />} onClick={() => setView('diagnostics')}>查看请求诊断</Button>
            </div>
          </Card>
        </div>
      )}

      <Card padding="md" className="tint-pink animate-rise">
        <SectionHead tint="pink" title="请求阶段诊断" icon={<ListChecks className="h-4 w-4" />}
          description="普通事件与手动请求共用近期诊断; 最新在前, 仅保留进程内记录" />
        <RequestDiagnosticsPanel
          adapterId={diagnosticsAdapter}
          adapterOptions={diagnosticsAdapters}
          onAdapterChange={setDiagnosticsAdapter}
          refreshKey={diagnosticsRefreshKey}
        />
      </Card>

      <PlatformConnectionTest platform={platforms.find((item) => item.id === testingPlatformId) || null}
        revision={revision}
        identity={`${health?.runtime_id || ''}:${adapters[testingPlatformId || '']?.client_self_id || adapters[testingPlatformId || '']?.bot?.self_id || ''}:${adapters[testingPlatformId || '']?.status || ''}:${applicationOf(testingPlatformId || '')?.active_revision || ''}`}
        onClose={() => setTestingPlatformId(null)} />

      <PlatformEditor
        open={showModal}
        onClose={guardedClose}
        title={editingPlatform ? `编辑平台 ${editingPlatform.id}` : '添加平台'}
        type={formData.type}
        initial={!!editingPlatform}
        revision={revision}
        draftRevision={draftRevision}
        dirty={formDirty}
        saving={saving}
        conflict={conflict}
        fields={formFields}
        values={formValues}
        onChange={handleFieldChange}
        onSubmit={() => void handleSubmit()}
        onCopyDraft={copyDraft}
      />

      <Modal open={deleteTarget !== null} onClose={() => setDeleteTarget(null)} size="sm"
        title={`删除平台 ${deleteTarget?.id ?? ''}?`} icon={<Trash2 className="h-[18px] w-[18px]" />}
        description="将从平台配置中移除; 运行中的适配器会在热更新后断开。" className="tint-red"
        footer={<>
          <Button variant="ghost" onClick={() => setDeleteTarget(null)}>取消</Button>
          <Button variant="danger" onClick={() => { if (deleteTarget) void handleDelete(deleteTarget.id); }}>确认删除</Button>
        </>}>
        <Note tone="red" title="该操作不可撤销">
          群聊、好友与审批记录不会被删除; 仅移除平台连接配置。若仍需保留历史数据, 建议先停用平台。
        </Note>
      </Modal>
    </div>
  );
}
