import { useCallback, useEffect, useMemo, useState } from 'react';
import { Copy, FileText, RefreshCw, Save, Server, Wand2 } from 'lucide-react';
import { cn } from '@/utils/cn';
import { Badge, Button, Field, IconButton, Input, Skeleton, toast } from '@/components/kit';
import { Banner, ConfirmDialog, SectionCard } from './shared';
import { initialCommonConfig, type CommonConfig } from './types';

interface Props {
  i: number;
  draft: CommonConfig;
  saved: CommonConfig;
  revision: string;
  path: string;
  exists: boolean;
  controlOnline: boolean;
  backendRunning: boolean;
  loading: boolean;
  saving: boolean;
  onChange: (value: CommonConfig) => void;
  onSave: () => Promise<boolean>;
  onReload: () => void;
  onCreateDefault: () => Promise<boolean>;
  onDirty: (dirty: boolean) => void;
  register: (actions: { save: () => Promise<boolean>; undo: () => void }) => void;
}

type Errors = Partial<Record<keyof CommonConfig | 'rateBurst', string>>;

const RESERVED_PORTS: Record<number, string> = {
  19871: '控制服务',
  19872: 'Chat 服务',
};

function validate(value: CommonConfig): Errors {
  const errors: Errors = {};
  if (!value.host.trim()) errors.host = '请填写监听地址';
  else if (!/^(\d{1,3}\.){3}\d{1,3}$/.test(value.host.trim()) && !/^[a-zA-Z0-9.-]+$/.test(value.host.trim())) {
    errors.host = '请填写有效的 IP 或主机名';
  }
  if (!Number.isInteger(value.port) || value.port < 1 || value.port > 65535) errors.port = '端口需在 1–65535 之间';
  else if (RESERVED_PORTS[value.port]) errors.port = `该端口已被${RESERVED_PORTS[value.port]}占用`;
  if (!value.defaultSessionType.trim()) errors.defaultSessionType = '请填写默认会话类型';
  if (!Number.isInteger(value.maxSessions) || value.maxSessions < 1 || value.maxSessions > 100000) errors.maxSessions = '需为 1–100000 的整数';
  if (!Number.isInteger(value.idleTimeout) || value.idleTimeout < 1 || value.idleTimeout > 604800) errors.idleTimeout = '需为 1–604800 秒';
  if (value.sessionTimeout !== null && (!Number.isFinite(value.sessionTimeout) || value.sessionTimeout <= 0)) errors.sessionTimeout = '留空表示无限制, 或填写正数';
  if (!Number.isInteger(value.rateLimit) || value.rateLimit < 1) errors.rateLimit = '需为大于 0 的整数';
  if (!Number.isInteger(value.rateBurst) || value.rateBurst < 1) errors.rateBurst = '需为大于 0 的整数';
  else if (value.rateBurst < value.rateLimit) errors.rateBurst = '突发数小于速率, 稳态吞吐会被压低';
  return errors;
}

export function CommonConfigSection(props: Props) {
  const { i, draft, saved, revision, path, exists, controlOnline, backendRunning, loading, saving, onChange, onSave, onReload, onCreateDefault, onDirty, register } = props;
  const [refreshing, setRefreshing] = useState(false);
  const [creating, setCreating] = useState(false);
  const [confirmReload, setConfirmReload] = useState(false);
  const dirty = JSON.stringify(draft) !== JSON.stringify(saved);
  const errors = useMemo(() => validate(draft), [draft]);
  const errorList = Object.values(errors).filter(Boolean) as string[];
  const disabled = !controlOnline || !exists;
  const backendUrl = `http://${draft.host || '127.0.0.1'}:${draft.port || 19870}`;

  useEffect(() => onDirty(dirty), [dirty, onDirty]);

  const set = <K extends keyof CommonConfig>(key: K, value: CommonConfig[K]) => onChange({ ...draft, [key]: value });

  const performSave = useCallback(async (): Promise<boolean> => {
    if (disabled) { toast.error('控制服务未运行或配置文件缺失'); return false; }
    if (errorList.length > 0) {
      toast.error('请先修正配置错误', errorList[0]);
      return false;
    }
    return onSave();
  }, [disabled, errorList, onSave]);

  const undo = useCallback(() => {
    onChange(saved);
    toast.success('已撤销常用配置的修改');
  }, [onChange, saved]);

  useEffect(() => { register({ save: performSave, undo }); }, [register, performSave, undo]);

  const reload = useCallback(() => {
    setRefreshing(true);
    setConfirmReload(false);
    onReload();
    window.setTimeout(() => {
      setRefreshing(false);
      toast.success('已重新读取配置文件');
    }, 500);
  }, [onReload]);

  const createDefault = async () => {
    setCreating(true);
    try { await onCreateDefault(); } finally { setCreating(false); }
  };

  return (
    <SectionCard id="common" i={i} tint="accent" icon={<Server className="h-4 w-4" />}
      title="常用配置" description="监听地址、会话上限与限流参数。修改后保存到配置文件"
      actions={<>
        {dirty && <Badge tone="orange" dot>有未保存修改</Badge>}
        {loading && <Badge tone="neutral" dot>读取中…</Badge>}
        <Button variant="ghost" size="sm" icon={<RefreshCw className={cn('h-4 w-4', refreshing && 'animate-spin-slow')} />}
          loading={refreshing} disabled={loading || saving} onClick={() => (dirty ? setConfirmReload(true) : reload())}>重新读取</Button>
      </>}>

      <div className="mb-4 flex flex-wrap items-center gap-x-4 gap-y-2 rounded-xl border border-line bg-glass px-3.5 py-2.5">
        <Badge tone={!exists ? 'orange' : controlOnline ? 'green' : 'red'} dot live={controlOnline && exists}>
          {!exists ? '配置文件缺失' : controlOnline ? '配置服务在线' : '控制服务离线'}
        </Badge>
        <span className="flex min-w-0 items-center gap-1.5 text-xs text-fg-3">
          <FileText className="h-3.5 w-3.5 flex-none" />
          <code className="truncate font-mono text-[11.5px] text-fg-2" title={path || undefined}>{path || '尚未读取配置路径'}</code>
          {path && <IconButton label="复制配置文件路径" size="sm" className="!h-6 !w-6" onClick={() => { void navigator.clipboard?.writeText(path); toast.success('路径已复制'); }}>
            <Copy className="h-3 w-3" />
          </IconButton>}
        </span>
        <span className="num text-xs text-fg-3">修订 <span className="font-mono text-fg-2">{revision}</span></span>
        <span className="ml-auto flex items-center gap-1.5 text-xs text-fg-3">
          <span className={`dot ${backendRunning ? 'tint-green' : 'tint-orange'}`} data-live={backendRunning ? '1' : undefined} />
          {backendRunning ? '保存后自动重启后端' : '后端未运行'}
        </span>
      </div>

      {!controlOnline && (
        <Banner tone="warning" title="控制服务未运行" className="mb-4">
          请先运行 <code className="rounded bg-glass-hover px-1.5 py-0.5 font-mono text-[12px] text-fg">python -m satrap.core.backend.control_server</code> 启动控制服务, 才能读写配置文件。
        </Banner>
      )}
      {!exists && controlOnline && (
        <Banner tone="warning" title="尚未创建配置文件" className="mb-4"
          actions={<Button size="sm" variant="primary" loading={creating} onClick={() => void createDefault()}>创建默认配置</Button>}>
          可以生成一份完整的默认配置后再编辑。当前字段展示的是内存中的默认值。
        </Banner>
      )}

      {loading ? (
        <div className="grid gap-4 sm:grid-cols-2" aria-busy="true" aria-label="正在读取配置">
          {Array.from({ length: 8 }).map((_, index) => (
            <div key={index}>
              <Skeleton className="h-3 w-24" />
              <Skeleton className="mt-2 h-9 w-full" />
              <Skeleton className="mt-1.5 h-2.5 w-2/3" />
            </div>
          ))}
        </div>
      ) : (<>
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="API Host" required error={errors.host} hint="监听地址, 默认只接受本机访问">
            <Input value={draft.host} disabled={disabled} onChange={(event) => set('host', event.target.value)} placeholder="127.0.0.1" />
          </Field>
          <Field label="API Port" required error={errors.port} hint={errors.port ? undefined : '后端 HTTP API 监听端口'}>
            <Input type="number" min={1} max={65535} value={draft.port} disabled={disabled}
              onChange={(event) => set('port', Number(event.target.value))} />
          </Field>
          <Field label="默认会话类型" required error={errors.defaultSessionType} hint="新会话未指定时使用的会话类, 例如 agent">
            <Input value={draft.defaultSessionType} disabled={disabled} onChange={(event) => set('defaultSessionType', event.target.value)} />
          </Field>
          <Field label="最大会话数" error={errors.maxSessions} hint="同时存在的会话上限, 超出后最久未活跃的会话会被回收">
            <Input type="number" min={1} value={draft.maxSessions} disabled={disabled}
              onChange={(event) => set('maxSessions', Number(event.target.value))} />
          </Field>
          <Field label="会话闲置超时 (秒)" error={errors.idleTimeout} hint="超过该时间没有消息的会话会进入休眠">
            <Input type="number" min={1} value={draft.idleTimeout} disabled={disabled}
              onChange={(event) => set('idleTimeout', Number(event.target.value))} />
          </Field>
          <Field label="单轮会话限时 (秒)" error={errors.sessionTimeout} hint="留空表示无限制; 包含模型调用、工具执行与审批等待">
            <Input type="number" min={0.1} step="any" placeholder="无限制" disabled={disabled}
              value={draft.sessionTimeout === null ? '' : draft.sessionTimeout}
              onChange={(event) => set('sessionTimeout', event.target.value === '' ? null : Number(event.target.value))} />
          </Field>
          <Field label="限流速率 (次/秒)" error={errors.rateLimit} hint="每个平台用户每秒允许发起的新请求数">
            <Input type="number" min={1} value={draft.rateLimit} disabled={disabled}
              onChange={(event) => set('rateLimit', Number(event.target.value))} />
          </Field>
          <Field label="限流突发数" error={errors.rateBurst} hint={errors.rateBurst ? undefined : '允许短时间超过速率的请求数, 应不小于限流速率'}>
            <Input type="number" min={1} value={draft.rateBurst} disabled={disabled}
              onChange={(event) => set('rateBurst', Number(event.target.value))} />
          </Field>
        </div>
      </>)}

      <div className="mt-4 flex flex-wrap items-center gap-2 rounded-xl border border-line bg-glass px-3.5 py-2.5 text-xs text-fg-3">
        <span className="font-medium text-fg-2">后端地址</span>
        <code className="font-mono text-[12px] text-fg">{backendUrl}</code>
        <span className="text-fg-4">·</span>
        <span>只有后端运行时可访问</span>
        <span className="ml-auto text-fg-4">配置文件保存后由控制服务原子写入</span>
      </div>

      {errorList.length > 0 && (
        <p className="mt-3 flex items-center gap-1.5 text-xs text-red" role="alert">{errorList.length} 项需要修正后才能保存</p>
      )}

      <div className="mt-5 flex flex-wrap items-center gap-2 border-t border-line pt-5">
        <Button variant="primary" icon={<Save className="h-4 w-4" />} loading={saving} disabled={disabled || loading || errorList.length > 0}
          onClick={() => void performSave()}>
          保存配置
        </Button>
        <Button variant="ghost" icon={<Wand2 className="h-4 w-4" />} disabled={disabled || loading || saving}
          onClick={() => onChange(initialCommonConfig)}>
          填入默认值
        </Button>
        <span className="ml-auto text-xs text-fg-4">
          {backendRunning ? '保存并重启会中断进行中的会话' : '后端未运行, 保存后下次启动生效'}
        </span>
      </div>

      <ConfirmDialog open={confirmReload} onClose={() => setConfirmReload(false)} danger confirmLabel="放弃修改并读取"
        title="放弃未保存的修改?" description="重新读取会丢弃当前表单中的所有改动。" onConfirm={reload}>
        <p className="text-sm text-fg-2">已保存的配置文件不会被修改。</p>
      </ConfirmDialog>
    </SectionCard>
  );
}
