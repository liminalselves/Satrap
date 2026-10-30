import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertTriangle, CheckCircle2, Database, FileWarning, HardDrive, RefreshCw, RotateCcw, Save, ShieldAlert,
  Trash2, XCircle,
} from 'lucide-react';
import { cn } from '@/utils/cn';
import { Badge, Button, Field, Input, Skeleton, Switch, toast, type Tone } from '@/components/kit';
import { storageApi, type StorageAuditItem, type StorageAuditResult } from '@/api/storage';
import { settingsApi, statsApi, type StorageMaintenance, type TelemetrySettings } from '@/api/contracts';
import { errorText } from '@/utils/errorText';
import { formatBytes, formatTime } from '@/utils/format';
import { Banner, ConfirmDialog, InlineSwitchRow, SectionCard } from './shared';
import { useActivationLoad } from './useActivationLoad';

const categoryTone: Record<string, Tone> = {
  orphan_session_directory: 'orange',
  orphan_database_rows: 'orange',
  broken_user_reference: 'accent',
  detached_platform: 'pink',
  trash_entry: 'teal',
  invalid_manifest: 'purple',
  unsafe_entry: 'red',
};

const categoryLabels: Record<string, string> = {
  orphan_session_directory: '孤儿目录',
  orphan_database_rows: '孤儿记录',
  broken_user_reference: '断裂引用',
  detached_platform: '脱离平台',
  trash_entry: '回收项',
  invalid_manifest: '无效清单',
  unsafe_entry: '不安全路径',
};

type ConfirmState =
  | { kind: 'cleanup' }
  | { kind: 'purge'; item: StorageAuditItem }
  | { kind: 'batch'; byRetention: boolean }
  | null;

interface MaintenanceResult {
  title: string;
  detail: string;
  entries: { label: string; ok: boolean; error?: string }[];
}

interface Props {
  i: number;
  active: boolean;
  backendRunning: boolean;
  controlAvailable: boolean;
}

const isRetention = (value: number) => Number.isInteger(value) && value >= 1 && value <= 3660;
const isServiceMb = (value: number) => Number.isInteger(value) && value >= 16 && value <= 4096;

function isMaintenance(value: unknown): value is StorageMaintenance {
  if (!value || typeof value !== 'object') return false;
  const item = value as Partial<StorageMaintenance>;
  return typeof item.trash_retention_days === 'number' && typeof item.auto_purge_enabled === 'boolean'
    && typeof item.revision === 'string';
}

function isTelemetry(value: unknown): value is { settings: TelemetrySettings; revision: string } {
  if (!value || typeof value !== 'object') return false;
  const item = value as { settings?: Partial<TelemetrySettings>; revision?: unknown };
  const settings = item.settings;
  return typeof item.revision === 'string' && !!settings
    && typeof settings.usage_enabled === 'boolean' && typeof settings.activity_enabled === 'boolean'
    && typeof settings.usage_retention_days === 'number' && typeof settings.activity_retention_days === 'number'
    && typeof settings.aggregate_retention_days === 'number' && typeof settings.max_service_mb === 'number';
}

const isConflict = (error: unknown) => typeof error === 'object' && error !== null
  && (error as { status?: number }).status === 409;

export function DataMaintenancePanel({ i, active, backendRunning, controlAvailable }: Props) {
  const [items, setItems] = useState<StorageAuditItem[] | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [scanned, setScanned] = useState(false);
  const [summary, setSummary] = useState<{ count: number; size_bytes: number; unmeasured_count: number } | null>(null);
  const [busyKind, setBusyKind] = useState<'scan' | 'cleanup' | 'restore' | 'purge' | 'batch' | null>(null);
  const [confirm, setConfirm] = useState<ConfirmState>(null);
  const [result, setResult] = useState<MaintenanceResult | null>(null);
  const allRef = useRef<HTMLInputElement>(null);

  const [policy, setPolicy] = useState<StorageMaintenance | null>(null);
  const [policyDays, setPolicyDays] = useState('');
  const [policyAuto, setPolicyAuto] = useState(false);
  const [policyLoading, setPolicyLoading] = useState(false);
  const [policySaving, setPolicySaving] = useState(false);
  const [policyError, setPolicyError] = useState('');
  const [policyNote, setPolicyNote] = useState('');

  const [telemetry, setTelemetry] = useState<{ settings: TelemetrySettings; revision: string } | null>(null);
  const [telemetryDraft, setTelemetryDraft] = useState<TelemetrySettings | null>(null);
  const [telemetryLoading, setTelemetryLoading] = useState(false);
  const [telemetrySaving, setTelemetrySaving] = useState(false);
  const [telemetryError, setTelemetryError] = useState('');
  const [telemetryNote, setTelemetryNote] = useState('');

  const requestId = useRef(0);
  const policyLoadId = useRef(0);
  const telemetryLoadId = useRef(0);

  const list = useMemo(() => items ?? [], [items]);
  const available = backendRunning || controlAvailable;
  const busy = busyKind !== null;
  const scanning = busyKind === 'scan';
  const safeItems = useMemo(() => list.filter((item) => item.auto_safe), [list]);
  const selectable = useMemo(() => list.filter((item) => item.auto_safe || item.category === 'trash_entry'), [list]);
  const selectedSafe = useMemo(() => list.filter((item) => selected.has(item.item_id) && item.auto_safe), [list, selected]);
  const selectedTrash = useMemo(() => list.filter((item) => selected.has(item.item_id)
    && item.category === 'trash_entry' && Boolean(item.details.archive_id)), [list, selected]);
  const allSelected = selectable.length > 0 && selectable.every((item) => selected.has(item.item_id));

  const maintenanceDirty = policy !== null
    && (policyDays !== String(policy.trash_retention_days) || policyAuto !== policy.auto_purge_enabled);
  const validPolicyDays = /^\d+$/.test(policyDays) && isRetention(Number(policyDays));
  const policyDaysNumber = validPolicyDays ? Number(policyDays) : policy?.trash_retention_days ?? 30;

  const telemetryDirty = telemetry !== null && telemetryDraft !== null
    && JSON.stringify(telemetryDraft) !== JSON.stringify(telemetry.settings);
  const telemetryValid = telemetryDraft !== null
    && isRetention(telemetryDraft.usage_retention_days)
    && isRetention(telemetryDraft.activity_retention_days)
    && isRetention(telemetryDraft.aggregate_retention_days)
    && isServiceMb(telemetryDraft.max_service_mb);

  useEffect(() => {
    if (!allRef.current) return;
    allRef.current.indeterminate = selected.size > 0 && !allSelected;
  }, [selected, allSelected]);

  const loadPolicy = useCallback(async () => {
    const id = ++policyLoadId.current;
    setPolicyLoading(true);
    try {
      const value = await settingsApi.maintenance();
      if (id !== policyLoadId.current) return;
      if (!isMaintenance(value)) {
        setPolicy(null);
        setPolicyError('');
        return;
      }
      setPolicy(value);
      setPolicyDays(String(value.trash_retention_days));
      setPolicyAuto(value.auto_purge_enabled);
      setPolicyError('');
    } catch (cause) {
      if (id !== policyLoadId.current) return;
      setPolicy(null);
      setPolicyError(errorText(cause, '读取回收区策略失败'));
    } finally {
      if (id === policyLoadId.current) setPolicyLoading(false);
    }
  }, []);

  const loadTelemetry = useCallback(async () => {
    const id = ++telemetryLoadId.current;
    setTelemetryLoading(true);
    try {
      const value = await statsApi.telemetry();
      if (id !== telemetryLoadId.current) return;
      if (!isTelemetry(value)) {
        setTelemetry(null);
        setTelemetryDraft(null);
        setTelemetryError('');
        return;
      }
      setTelemetry(value);
      setTelemetryDraft(value.settings);
      setTelemetryError('');
    } catch (cause) {
      if (id !== telemetryLoadId.current) return;
      setTelemetry(null);
      setTelemetryDraft(null);
      setTelemetryError(errorText(cause, '读取统计采集设置失败'));
    } finally {
      if (id === telemetryLoadId.current) setTelemetryLoading(false);
    }
  }, []);

  useActivationLoad(active, async () => { await Promise.all([loadPolicy(), loadTelemetry()]); });
  useEffect(() => () => { policyLoadId.current++; telemetryLoadId.current++; }, []);

  const savePolicy = async () => {
    if (!policy || !validPolicyDays || policySaving || !maintenanceDirty) return;
    setPolicySaving(true);
    setPolicyError('');
    setPolicyNote('');
    try {
      const value = await settingsApi.saveMaintenance({ trash_retention_days: Number(policyDays), auto_purge_enabled: policyAuto }, policy.revision);
      if (isMaintenance(value)) {
        setPolicy(value);
        setPolicyDays(String(value.trash_retention_days));
        setPolicyAuto(value.auto_purge_enabled);
        toast.success('回收区策略已保存', `保留 ${value.trash_retention_days} 天 · 自动清理${value.auto_purge_enabled ? '开启' : '关闭'}`);
      } else {
        await loadPolicy();
        toast.success('回收区策略已保存');
      }
    } catch (cause) {
      if (isConflict(cause)) {
        setPolicyNote('策略已在其它位置更新, 已重新读取最新值, 请核对后再次保存。');
        await loadPolicy();
        toast.warning('回收区策略修订冲突', '已重新读取最新策略');
      } else {
        setPolicyError(errorText(cause, '保存回收区策略失败'));
      }
    } finally {
      setPolicySaving(false);
    }
  };

  const saveTelemetry = async () => {
    if (!telemetry || !telemetryDraft || telemetrySaving || !telemetryValid || !telemetryDirty) return;
    setTelemetrySaving(true);
    setTelemetryError('');
    setTelemetryNote('');
    try {
      const value = await statsApi.saveTelemetry(telemetryDraft, telemetry.revision);
      if (isTelemetry(value)) {
        setTelemetry(value);
        setTelemetryDraft(value.settings);
      } else {
        await loadTelemetry();
      }
      toast.success('统计采集设置已保存', '新的保留期从下一次采集与清理周期生效');
    } catch (cause) {
      if (isConflict(cause)) {
        setTelemetryNote('设置已在其它位置更新, 已重新读取最新值, 请核对后再次保存。');
        await loadTelemetry();
        toast.warning('统计采集设置修订冲突', '已重新读取最新设置');
      } else {
        setTelemetryError(errorText(cause, '保存统计采集设置失败'));
      }
    } finally {
      setTelemetrySaving(false);
    }
  };

  const scan = useCallback(async () => {
    if (!available) { toast.warning('数据审计需要平台后端或控制服务运行'); return; }
    const id = ++requestId.current;
    setBusyKind('scan');
    try {
      const value: StorageAuditResult = await storageApi.audit(backendRunning);
      if (id !== requestId.current) return;
      const nextItems = Array.isArray(value.items) ? value.items : [];
      setItems(nextItems);
      setSummary(value.summary && typeof value.summary.size_bytes === 'number'
        ? value.summary
        : { count: nextItems.length, size_bytes: nextItems.reduce((sum, item) => sum + (item.size_bytes ?? 0), 0), unmeasured_count: nextItems.filter((item) => item.size_bytes === null).length });
      setSelected(new Set());
      setScanned(true);
      toast.success('扫描完成', `发现 ${nextItems.length} 项可维护数据`);
    } catch (cause) {
      if (id !== requestId.current) return;
      toast.error('扫描失败', errorText(cause, '未知错误'));
    } finally {
      if (id === requestId.current) setBusyKind(null);
    }
  }, [available, backendRunning]);

  const toggle = (itemId: string) => {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(itemId)) next.delete(itemId);
      else next.add(itemId);
      return next;
    });
  };

  const toggleAll = () => {
    if (allSelected) setSelected(new Set());
    else setSelected(new Set(selectable.map((item) => item.item_id)));
  };

  const runCleanup = async () => {
    const targets = selectedSafe;
    setConfirm(null);
    if (targets.length === 0) { toast.warning('请选择可安全清理的项目'); return; }
    const id = ++requestId.current;
    setBusyKind('cleanup');
    try {
      const value = await storageApi.cleanup(targets.map((item) => item.item_id), backendRunning);
      if (id !== requestId.current) return;
      const failed = value.results.filter((entry) => !entry.ok);
      setResult({
        title: failed.length > 0 ? '孤儿数据清理完成, 部分项目失败' : '孤儿数据清理完成',
        detail: `成功 ${value.results.length - failed.length} 项, 失败 ${failed.length} 项`,
        entries: value.results.map((entry) => ({
          label: targets.find((item) => item.item_id === entry.item_id)?.session_id || entry.item_id,
          ok: entry.ok,
          error: entry.error,
        })),
      });
      if (failed.length > 0) toast.warning(`有 ${failed.length} 项清理失败`, '失败项已保留, 可稍后重试');
      else toast.success('孤儿数据清理完成', `已处理 ${value.results.length} 项`);
      await scan();
    } catch (cause) {
      if (id === requestId.current) toast.error('清理失败', errorText(cause, '未知错误'));
    } finally {
      if (id === requestId.current) setBusyKind(null);
    }
  };

  const restore = async (item: StorageAuditItem) => {
    const archiveId = item.details.archive_id;
    if (!archiveId) { toast.warning('该回收项不是完整回收包, 无法恢复'); return; }
    if (!item.details.manifest?.archive_version) { toast.warning('回收包清单缺少版本号, 无法恢复'); return; }
    const id = ++requestId.current;
    setBusyKind('restore');
    try {
      await storageApi.restore(item.platform_id, archiveId, backendRunning);
      if (id !== requestId.current) return;
      setResult({ title: '会话已恢复', detail: `${item.platform_id} / ${item.session_id}`, entries: [{ label: archiveId, ok: true }] });
      toast.success('会话已恢复', `${item.platform_id} / ${item.session_id}`);
      await scan();
    } catch (cause) {
      if (id === requestId.current) toast.error('恢复失败', errorText(cause, '未知错误'));
    } finally {
      if (id === requestId.current) setBusyKind(null);
    }
  };

  const purge = async (item: StorageAuditItem) => {
    setConfirm(null);
    const archiveId = item.details.archive_id;
    if (!archiveId) return;
    const id = ++requestId.current;
    setBusyKind('purge');
    try {
      await storageApi.purge(item.platform_id, archiveId, backendRunning);
      if (id !== requestId.current) return;
      setResult({ title: '回收项已永久删除', detail: `${item.platform_id} / ${archiveId}`, entries: [{ label: archiveId, ok: true }] });
      toast.success('回收项已永久删除');
      await scan();
    } catch (cause) {
      if (id === requestId.current) toast.error('永久删除失败', errorText(cause, '未知错误'));
    } finally {
      if (id === requestId.current) setBusyKind(null);
    }
  };

  const purgeBatch = async (byRetention: boolean) => {
    setConfirm(null);
    if (!byRetention && selectedTrash.length === 0) { toast.warning('请选择需要永久删除的回收项'); return; }
    const days = policyDaysNumber;
    const id = ++requestId.current;
    setBusyKind('batch');
    try {
      const value = await storageApi.purgeBatch(
        byRetention
          ? { older_than_days: days }
          : { archive_refs: selectedTrash.map((item) => ({ platform_id: item.platform_id, archive_id: item.details.archive_id || '' })) },
        backendRunning,
      );
      if (id !== requestId.current) return;
      const failed = value.results.filter((entry) => !entry.ok);
      setResult({
        title: byRetention ? '回收区过期清理完成' : '选中的回收项已永久删除',
        detail: `共处理 ${value.results.length} 项${byRetention ? ` · 保留期 ${days} 天` : ''}`,
        entries: value.results.map((entry) => ({ label: entry.archive_id, ok: entry.ok, error: entry.error })),
      });
      if (failed.length > 0) toast.warning(`有 ${failed.length} 项删除失败`);
      else toast.success(byRetention ? '回收区清理完成' : '回收项已永久删除', `共删除 ${value.results.length} 项`);
      await scan();
    } catch (cause) {
      if (id === requestId.current) toast.error('回收区清理失败', errorText(cause, '未知错误'));
    } finally {
      if (id === requestId.current) setBusyKind(null);
    }
  };

  const confirmContent = (() => {
    if (!confirm) return null;
    if (confirm.kind === 'cleanup') {
      return {
        title: `清理选中的 ${selectedSafe.length} 项孤儿数据?`,
        description: '可安全清理的项目会移入回收区或删除孤立引用。',
        body: <p className="text-sm leading-6 text-fg-2">需人工检查的项目不会被处理。清理后会自动重新扫描, 失败的条目会保留并记录原因。</p>,
        label: '开始清理',
        action: () => void runCleanup(),
      };
    }
    if (confirm.kind === 'purge') {
      return {
        title: '永久删除这个回收项?',
        description: `回收包 ${confirm.item.details.archive_id ?? '未知'} 删除后无法恢复。`,
        body: <p className="text-sm leading-6 text-fg-2">{confirm.item.platform_id} / {confirm.item.session_id} · {formatBytes(confirm.item.size_bytes)}。会话数据、附件与索引都会一并删除。</p>,
        label: '永久删除',
        action: () => void purge(confirm.item),
      };
    }
    if (confirm.byRetention) {
      return {
        title: `永久删除 ${policyDaysNumber} 天前的回收项?`,
        description: '所有存放超过保留期的回收包都会被删除, 无法恢复。',
        body: <p className="text-sm leading-6 text-fg-2">按已保存的回收区保留天数 ({policyDaysNumber} 天) 执行; 自动清理是否开启不影响本次手动操作。</p>,
        label: '永久删除',
        action: () => void purgeBatch(true),
      };
    }
    return {
      title: `永久删除选中的 ${selectedTrash.length} 个回收项?`,
      description: '选中的回收包会被立即删除, 无法恢复。',
      body: <p className="text-sm leading-6 text-fg-2">仅回收项会被删除, 其他可清理项目不受影响。</p>,
      label: '永久删除',
      action: () => void purgeBatch(false),
    };
  })();

  return (
    <SectionCard id="data" i={i} tint="pink" icon={<Database className="h-4 w-4" />}
      title="数据维护" description="扫描孤儿数据与回收区, 配置回收区保留策略、统计采集与保留期"
      actions={<>
        <Badge tone={available ? 'green' : 'red'} dot live={available}>{backendRunning ? '后端在线' : controlAvailable ? '控制服务可用' : '服务不可用'}</Badge>
        <Button size="sm" icon={<RefreshCw className={cn('h-4 w-4', scanning && 'animate-spin-slow')} />} loading={scanning}
          disabled={busy || !available} onClick={() => void scan()}>扫描</Button>
        <Button size="sm" variant="danger" icon={<Trash2 className="h-4 w-4" />} disabled={busy || selectedSafe.length === 0}
          onClick={() => setConfirm({ kind: 'cleanup' })}>清理选中项{selectedSafe.length > 0 ? ` (${selectedSafe.length})` : ''}</Button>
      </>}>

      {!available && (
        <Banner tone="error" title="平台后端和控制服务均未运行" className="mb-4">
          当前不能执行数据审计和维护。请先启动其中一个服务后重试。
        </Banner>
      )}

      <div className="rounded-xl border border-line bg-glass p-3.5">
        <div className="flex flex-wrap items-center gap-2">
          <HardDrive className="h-4 w-4 text-pink" />
          <h3 className="text-[13px] font-medium text-fg">回收区保留策略</h3>
          {maintenanceDirty && <Badge tone="orange" dot>有未保存修改</Badge>}
          {policy && <Badge tone="neutral">修订 <span className="num font-mono">{policy.revision}</span></Badge>}
          <Button size="sm" variant="primary" className="ml-auto" icon={<Save className="h-3.5 w-3.5" />}
            loading={policySaving} disabled={!policy || !maintenanceDirty || !validPolicyDays || policyLoading}
            onClick={() => void savePolicy()}>保存策略</Button>
          <Button size="sm" variant="ghost" icon={<RefreshCw className={cn('h-3.5 w-3.5', policyLoading && 'animate-spin-slow')} />}
            onClick={() => void loadPolicy()} disabled={policyLoading || policySaving}>重新读取</Button>
        </div>
        {policyError && <Banner tone="error" className="mt-3" compact>{policyError}</Banner>}
        {policyNote && <Banner tone="warning" className="mt-3" compact>{policyNote}</Banner>}
        {!policy && !policyLoading && !policyError && (
          <p className="mt-2 text-[11.5px] text-fg-4">当前控制服务未返回回收区策略, 保留天数使用默认值且无法保存。</p>
        )}
        {policyLoading && !policy ? (
          <div className="mt-3 grid gap-3 sm:grid-cols-2">
            <Skeleton className="h-16 w-full" />
            <Skeleton className="h-16 w-full" />
          </div>
        ) : (
          <div className="mt-3 grid gap-4 sm:grid-cols-2">
            <Field label="回收区保留天数" error={policyDays && !validPolicyDays ? '请输入 1 至 3660 的整数' : undefined}
              hint="超过保留期的回收项可以手动批量清理; 自动清理开启时由定时任务执行。">
              <Input type="number" min={1} max={3660} step={1} value={policyDays}
                disabled={!policy || policySaving || policyLoading}
                aria-invalid={Boolean(policyDays) && !validPolicyDays}
                onChange={(event) => { setPolicyDays(event.target.value); setPolicyNote(''); }} />
            </Field>
            <div className="rounded-xl border border-line bg-glass px-4">
              <InlineSwitchRow label="自动清理过期回收项" hint="关闭后仍可手动清理, 回收项不会被自动删除。">
                <Switch label="自动清理过期回收项" checked={policyAuto} disabled={!policy || policySaving || policyLoading}
                  onChange={(value) => { setPolicyAuto(value); setPolicyNote(''); }} />
              </InlineSwitchRow>
            </div>
          </div>
        )}
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-2 rounded-xl border border-line bg-glass px-3.5 py-2.5">
        <span className="text-[13px] text-fg-2">回收区操作</span>
        <span className="num text-[11.5px] text-fg-4">当前保留 {policy ? `${policy.trash_retention_days} 天` : '—'}</span>
        <Button size="sm" variant="ghost" disabled={busy} onClick={() => setConfirm({ kind: 'batch', byRetention: true })}>清理过期回收项</Button>
        <Button size="sm" variant="danger" disabled={busy || selectedTrash.length === 0} onClick={() => setConfirm({ kind: 'batch', byRetention: false })}>
          永久删除选中回收项 ({selectedTrash.length})
        </Button>
        <span className="ml-auto flex items-center gap-1.5 text-[11px] text-fg-4"><AlertTriangle className="h-3.5 w-3.5" />手动操作需要确认</span>
      </div>

      {scanned && summary && (
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <Badge tone={(summary.count ?? list.length) > 0 ? 'orange' : 'green'} dot>项目 {summary.count ?? list.length}</Badge>
          <Badge tone="accent">可安全清理 {safeItems.length}</Badge>
          <Badge tone="teal">已统计文件 {formatBytes(summary.size_bytes)}</Badge>
          {summary.unmeasured_count > 0 && <Badge tone="purple">记录大小未单独统计 {summary.unmeasured_count} 项</Badge>}
          <span className="text-[11px] text-fg-4">文件大小不代表可释放空间, 数据库记录不按整库大小计入</span>
        </div>
      )}

      <div className="mt-4 overflow-hidden rounded-xl border border-line">
        {!scanning && list.length === 0 ? (
          <div className="flex flex-col items-center justify-center gap-2 px-4 py-12 text-center">
            <ShieldAlert className="h-7 w-7 text-fg-4" />
            <p className="text-sm font-medium text-fg">{scanned ? '没有发现需要维护的数据' : '点击扫描检查数据状态'}</p>
            <p className="text-[12px] text-fg-4">{scanned ? '存储目录、数据库引用与回收区均处于一致状态' : '扫描只读, 不会修改任何文件。发现可维护项后可以逐项确认'}</p>
          </div>
        ) : (
          <div className="scroll max-h-[30rem] overflow-auto">
            <table className="w-full min-w-[920px] text-left text-[13px]">
              <thead className="sticky top-0 z-[1] bg-[var(--glass-bg-strong)] backdrop-blur-xl">
                <tr className="border-b border-line text-[11.5px] font-medium text-fg-3">
                  <th className="w-12 px-3 py-2.5">
                    <input ref={allRef} type="checkbox" aria-label="全选可操作项目" checked={allSelected}
                      disabled={busy || selectable.length === 0} onChange={toggleAll} className="accent-[var(--fg-accent)]" />
                  </th>
                  <th className="px-3 py-2.5">类别</th>
                  <th className="px-3 py-2.5">平台 / 会话</th>
                  <th className="px-3 py-2.5">原因</th>
                  <th className="px-3 py-2.5 text-right">大小</th>
                  <th className="px-3 py-2.5">操作</th>
                </tr>
              </thead>
              <tbody>
                {scanning && Array.from({ length: 5 }).map((_, index) => (
                  <tr key={index} className="border-b border-line last:border-0">
                    <td className="px-3 py-3"><Skeleton className="h-4 w-4" /></td>
                    <td className="px-3 py-3"><Skeleton className="h-5 w-16" /></td>
                    <td className="px-3 py-3"><Skeleton className="h-4 w-28" /></td>
                    <td className="px-3 py-3"><Skeleton className="h-4 w-56" /></td>
                    <td className="px-3 py-3"><Skeleton className="ml-auto h-4 w-14" /></td>
                    <td className="px-3 py-3"><Skeleton className="h-7 w-20" /></td>
                  </tr>
                ))}
                {!scanning && list.map((item) => {
                  const trash = item.category === 'trash_entry';
                  const toggleable = item.auto_safe || trash;
                  return (
                    <tr key={item.item_id} className={cn('border-b border-line transition-colors last:border-0 hover:bg-glass-hover', !item.auto_safe && !trash && 'bg-[rgb(var(--rgb-orange)/0.02)]')}>
                      <td className="px-3 py-3">
                        <input type="checkbox" aria-label={`选择 ${item.session_id || item.item_id}`}
                          checked={selected.has(item.item_id)} disabled={busy || !toggleable}
                          onChange={() => toggle(item.item_id)} className="accent-[var(--fg-accent)]" />
                      </td>
                      <td className="px-3 py-3">
                        <Badge tone={categoryTone[item.category] || 'neutral'}>{categoryLabels[item.category] || item.category}</Badge>
                      </td>
                      <td className="px-3 py-3">
                        <div className="font-mono text-[12px] text-fg">{item.platform_id || '-'}</div>
                        <div className="max-w-[220px] truncate font-mono text-[11px] text-fg-3" title={item.session_id}>{item.session_id || '-'}</div>
                        {item.modified_at > 0 && <div className="num text-[10.5px] text-fg-4">{formatTime(item.modified_at)}</div>}
                      </td>
                      <td className="px-3 py-3">
                        <div className="max-w-[360px] text-fg-2">{item.reason}</div>
                        <div className="max-w-[360px] truncate font-mono text-[10.5px] text-fg-4" title={item.path}>{item.path}</div>
                        <div className="mt-0.5 max-w-[360px] text-[10.5px] text-fg-4">建议: {item.recommended_action}</div>
                        {item.details.tables && <div className="mt-0.5 text-[10.5px] text-fg-4">表: {item.details.tables.join(', ')}</div>}
                      </td>
                      <td className="num whitespace-nowrap px-3 py-3 text-right text-fg-2">
                        {item.size_bytes === null ? <span className="text-fg-4">未单独统计</span> : formatBytes(item.size_bytes)}
                      </td>
                      <td className="px-3 py-3">
                        {trash && item.details.archive_id ? (
                          <div className="flex flex-wrap gap-1.5">
                            {Boolean(item.details.manifest?.archive_version) && (
                              <Button size="sm" variant="ghost" disabled={busy} icon={<RotateCcw className="h-3.5 w-3.5" />} onClick={() => void restore(item)}>恢复</Button>
                            )}
                            <Button size="sm" variant="ghost" disabled={busy} icon={<Trash2 className="h-3.5 w-3.5 text-red" />} onClick={() => setConfirm({ kind: 'purge', item })}>永久删除</Button>
                          </div>
                        ) : item.auto_safe ? (
                          <span className="flex items-center gap-1 text-[11.5px] text-green"><CheckCircle2 className="h-3.5 w-3.5" />可安全清理</span>
                        ) : (
                          <span className="flex items-center gap-1 text-[11.5px] text-orange"><FileWarning className="h-3.5 w-3.5" />需人工检查</span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-fg-4">
        <span className="flex items-center gap-1.5"><HardDrive className="h-3.5 w-3.5" />扫描操作只读</span>
        <span>自动清理只处理明确安全的孤儿目录、领域记录和断裂引用</span>
        <span>需人工检查的项目无法批量选择</span>
      </div>

      {result && (
        <div className="mt-4 rounded-xl border border-line bg-glass p-3.5">
          <div className="flex flex-wrap items-center gap-2">
            {result.entries.some((entry) => !entry.ok)
              ? <AlertTriangle className="h-4 w-4 text-orange" />
              : <CheckCircle2 className="h-4 w-4 text-green" />}
            <h3 className="text-sm font-medium text-fg">{result.title}</h3>
            <span className="text-[11.5px] text-fg-4">{result.detail}</span>
            <button type="button" onClick={() => setResult(null)} className="ml-auto text-[11.5px] text-fg-4 hover:text-fg">关闭</button>
          </div>
          {result.entries.length > 0 && (
            <ul className="mt-2 space-y-1.5">
              {result.entries.map((entry, index) => (
                <li key={`${entry.label}-${index}`} className="flex items-start gap-2 text-[12px]">
                  {entry.ok ? <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 flex-none text-green" /> : <XCircle className="mt-0.5 h-3.5 w-3.5 flex-none text-red" />}
                  <span className="min-w-0 break-all font-mono text-[11.5px] text-fg-2">{entry.label}</span>
                  {entry.error && <span className="flex-none text-red">{entry.error}</span>}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      <div className="mt-4 rounded-xl border border-line bg-glass p-3.5">
        <div className="flex flex-wrap items-center gap-2">
          <Database className="h-4 w-4 text-accent" />
          <h3 className="text-[13px] font-medium text-fg">统计采集与保留</h3>
          {telemetryDirty && <Badge tone="orange" dot>有未保存修改</Badge>}
          {telemetry && <Badge tone="neutral">修订 <span className="num font-mono">{telemetry.revision}</span></Badge>}
          {telemetry && <Badge tone={telemetryDraft?.usage_enabled ? 'green' : 'neutral'} dot live={telemetryDraft?.usage_enabled}>{telemetryDraft?.usage_enabled ? '用量采集中' : '用量采集已关闭'}</Badge>}
          <Button size="sm" variant="primary" className="ml-auto" icon={<Save className="h-3.5 w-3.5" />}
            loading={telemetrySaving} disabled={!telemetry || !telemetryDraft || !telemetryDirty || !telemetryValid || telemetryLoading}
            onClick={() => void saveTelemetry()}>保存设置</Button>
          <Button size="sm" variant="ghost" icon={<RefreshCw className={cn('h-3.5 w-3.5', telemetryLoading && 'animate-spin-slow')} />}
            onClick={() => void loadTelemetry()} disabled={telemetryLoading || telemetrySaving}>重新读取</Button>
        </div>
        {telemetryError && <Banner tone="error" className="mt-3" compact>{telemetryError}</Banner>}
        {telemetryNote && <Banner tone="warning" className="mt-3" compact>{telemetryNote}</Banner>}
        {!telemetry && !telemetryLoading && !telemetryError && (
          <p className="mt-2 text-[11.5px] text-fg-4">当前控制服务未返回统计采集设置, 无法修改。未知值显示为 —。</p>
        )}
        {telemetryLoading && !telemetry ? (
          <div className="mt-3 grid gap-3 sm:grid-cols-2">
            <Skeleton className="h-16 w-full" />
            <Skeleton className="h-16 w-full" />
          </div>
        ) : telemetryDraft && (
          <>
            <div className="mt-3 grid gap-3 sm:grid-cols-2">
              <div className="rounded-xl border border-line bg-glass px-4">
                <InlineSwitchRow label="采集模型用量" hint="关闭后停止写入新的用量明细, 既有统计不会被删除。">
                  <Switch label="采集模型用量" checked={telemetryDraft.usage_enabled} disabled={telemetrySaving}
                    onChange={(value) => setTelemetryDraft({ ...telemetryDraft, usage_enabled: value })} />
                </InlineSwitchRow>
              </div>
              <div className="rounded-xl border border-line bg-glass px-4">
                <InlineSwitchRow label="采集活动事件" hint="关闭后覆盖缺口会记录在统计聚合的元信息中。">
                  <Switch label="采集活动事件" checked={telemetryDraft.activity_enabled} disabled={telemetrySaving}
                    onChange={(value) => setTelemetryDraft({ ...telemetryDraft, activity_enabled: value })} />
                </InlineSwitchRow>
              </div>
            </div>
            <div className="mt-3 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
              <Field label="用量明细保留 (天)" hint="1–3660 天"
                error={!isRetention(telemetryDraft.usage_retention_days) ? '请输入 1 至 3660 的整数' : undefined}>
                <Input type="number" min={1} max={3660} step={1} value={telemetryDraft.usage_retention_days} disabled={telemetrySaving}
                  onChange={(event) => setTelemetryDraft({ ...telemetryDraft, usage_retention_days: Number(event.target.value) })} />
              </Field>
              <Field label="活动事件保留 (天)" hint="1–3660 天"
                error={!isRetention(telemetryDraft.activity_retention_days) ? '请输入 1 至 3660 的整数' : undefined}>
                <Input type="number" min={1} max={3660} step={1} value={telemetryDraft.activity_retention_days} disabled={telemetrySaving}
                  onChange={(event) => setTelemetryDraft({ ...telemetryDraft, activity_retention_days: Number(event.target.value) })} />
              </Field>
              <Field label="日汇总保留 (天)" hint="1–3660 天"
                error={!isRetention(telemetryDraft.aggregate_retention_days) ? '请输入 1 至 3660 的整数' : undefined}>
                <Input type="number" min={1} max={3660} step={1} value={telemetryDraft.aggregate_retention_days} disabled={telemetrySaving}
                  onChange={(event) => setTelemetryDraft({ ...telemetryDraft, aggregate_retention_days: Number(event.target.value) })} />
              </Field>
              <Field label="单服务存储上限 (MB)" hint="16–4096 MB"
                error={!isServiceMb(telemetryDraft.max_service_mb) ? '请输入 16 至 4096 的整数' : undefined}>
                <Input type="number" min={16} max={4096} step={1} value={telemetryDraft.max_service_mb} disabled={telemetrySaving}
                  onChange={(event) => setTelemetryDraft({ ...telemetryDraft, max_service_mb: Number(event.target.value) })} />
              </Field>
            </div>
            <p className="mt-2 text-[11px] text-fg-4">保存会整体替换统计采集设置, 并使用 expected_revision 防止覆盖其它会话的修改。保留期限制 1–3660 天, 存储上限 16–4096 MB。</p>
          </>
        )}
      </div>

      <ConfirmDialog open={confirm !== null} onClose={() => setConfirm(null)} danger={confirmContent !== null} loading={busy}
        title={confirmContent?.title ?? ''} description={confirmContent?.description} confirmLabel={confirmContent?.label ?? '确认'}
        onConfirm={() => confirmContent?.action()}>
        {confirmContent?.body}
      </ConfirmDialog>
    </SectionCard>
  );
}
