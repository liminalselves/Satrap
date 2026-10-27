import { useCallback, useEffect, useMemo, useState } from 'react';
import { Eye, KeyRound, RefreshCw, ShieldCheck, Trash2 } from 'lucide-react';
import { cn } from '@/utils/cn';
import { getPasswordStatus, setDashboardPassword } from '@/api/auth';
import { getControlApiUrl } from '@/utils/constants';
import { Badge, Button, Field, Skeleton, toast } from '@/components/kit';
import { Banner, ConfirmDialog, PasswordInput, SectionCard } from './shared';

function strengthOf(value: string): { score: number; label: string; text: string; tint: string } {
  let score = 0;
  if (value.length >= 8) score++;
  if (value.length >= 12) score++;
  if (/[a-z]/.test(value) && /[A-Z]/.test(value)) score++;
  if (/\d/.test(value)) score++;
  if (/[^a-zA-Z0-9]/.test(value)) score++;
  if (score <= 1) return { score, label: '太弱', text: 'text-red', tint: 'tint-red' };
  if (score === 2) return { score, label: '一般', text: 'text-orange', tint: 'tint-orange' };
  if (score === 3) return { score, label: '良好', text: 'text-accent', tint: 'tint-accent' };
  return { score, label: '很强', text: 'text-green', tint: 'tint-green' };
}

export function AccessPasswordPanel({ i, active }: { i: number; active: boolean }) {
  const [required, setRequired] = useState<boolean | null>(null);
  const [loading, setLoading] = useState(false);
  const [current, setCurrent] = useState('');
  const [next, setNext] = useState('');
  const [confirmValue, setConfirmValue] = useState('');
  const [error, setError] = useState('');
  const [loadError, setLoadError] = useState('');
  const [busy, setBusy] = useState(false);
  const [clearOpen, setClearOpen] = useState(false);
  const strength = useMemo(() => strengthOf(next), [next]);
  const baseUrl = getControlApiUrl();

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setRequired(await getPasswordStatus(baseUrl));
      setLoadError('');
    } catch (cause) {
      setLoadError(cause instanceof Error ? cause.message : '读取失败');
    } finally {
      setLoading(false);
    }
  }, [baseUrl]);

  useEffect(() => {
    if (active) void load();
  }, [active, load]);

  const submit = async (mode: 'set' | 'clear') => {
    if (busy) return;
    if (required === null) { setError('尚未读取密码状态'); return; }
    if (required && !current) { setError('请输入当前密码'); return; }
    if (mode === 'set') {
      if (next.length < 8) { setError('新密码至少需要 8 个字符'); return; }
      if (next !== confirmValue) { setError('两次输入的新密码不一致'); return; }
    }
    setBusy(true);
    setError('');
    try {
      const enabled = await setDashboardPassword(baseUrl, required ? current : null, mode === 'set' ? next : null);
      setRequired(enabled);
      setCurrent('');
      setNext('');
      setConfirmValue('');
      setClearOpen(false);
      toast.success(
        mode === 'set' ? '访问密码已更新, 下次进入前端需要密码' : '访问密码已清除, 前端恢复免密进入',
      );
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '保存失败');
    } finally {
      setBusy(false);
    }
  };

  const canSubmit = !busy && required !== null && Boolean(next) && (required !== true || Boolean(current));

  return (
    <SectionCard id="password" i={i} tint="accent" icon={<KeyRound className="h-4 w-4" />}
      title="访问密码" description="保护管理面板与 Chat 页面。密码以 PBKDF2-SHA256 加盐哈希保存在配置文件"
      actions={<>
        <Badge tone={required === null ? 'neutral' : required ? 'green' : 'orange'} dot live={required === true}>
          {required === null ? '读取中' : required ? '已启用密码' : '免密进入'}
        </Badge>
        <Button variant="ghost" size="sm" icon={<RefreshCw className={cn('h-4 w-4', loading && 'animate-spin-slow')} />}
          onClick={() => void load()} disabled={busy || loading}>刷新密码状态</Button>
      </>}>
      <section aria-label="访问密码设置" className="space-y-4">
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-xl border border-line bg-glass px-3.5 py-2.5 text-xs text-fg-3">
          <span className="flex items-center gap-2">
            {required === null ? <Skeleton className="h-4 w-20" /> : <>
              <ShieldCheck className={cn('h-4 w-4', required ? 'text-green' : 'text-orange')} />
              当前状态: <span className={cn('font-medium', required ? 'text-green' : 'text-orange')}>{required ? '已启用密码' : '免密进入'}</span>
            </>}
          </span>
          <span className="text-fg-4">API 与 api-token 访问不受此密码影响</span>
        </div>

        {error && <Banner tone="error">{error}</Banner>}
        {loadError && <Banner tone="error">读取访问密码状态失败: {loadError}</Banner>}

        {required && (
          <div className="max-w-md">
            <Field label="当前密码" required hint="修改或清除密码时需要验证当前密码">
              <PasswordInput autoComplete="current-password" value={current} disabled={busy}
                onChange={(event) => { setCurrent(event.target.value); setError(''); }} placeholder="输入当前密码" />
            </Field>
          </div>
        )}

        <div className="grid gap-4 sm:grid-cols-2">
          <Field label={required ? '新密码' : '设置密码'} required hint="至少 8 个字符, 区分大小写">
            <PasswordInput autoComplete="new-password" value={next} disabled={busy}
              onChange={(event) => { setNext(event.target.value); setError(''); }} placeholder="至少 8 个字符" />
          </Field>
          <Field label="确认新密码" required error={confirmValue && next !== confirmValue ? '两次输入的新密码不一致' : undefined}
            hint={confirmValue && next === confirmValue ? '两次输入一致' : '再次输入以确认'}>
            <PasswordInput autoComplete="new-password" value={confirmValue} disabled={busy}
              onChange={(event) => { setConfirmValue(event.target.value); setError(''); }} />
          </Field>
        </div>

        {next && (
          <div className="max-w-xl">
            <div className="flex items-center gap-2">
              <span className="text-[11px] text-fg-4">密码强度</span>
              <span className={cn('text-[11px] font-medium', strength.text)}>{strength.label}</span>
              <div className="ml-auto flex gap-1">
                {[0, 1, 2, 3, 4].map((n) => (
                  <span key={n} className={cn('h-1.5 w-8 rounded-full transition-colors', n < strength.score ? cn(strength.tint, 'bg-tint') : 'bg-glass-hover')} />
                ))}
              </div>
            </div>
            <p className="mt-1.5 text-[11.5px] text-fg-4">建议混合大小写、数字与符号, 并避免使用与其他服务相同的密码。</p>
          </div>
        )}

        <div className="flex flex-wrap items-center gap-2 border-t border-line pt-5">
          <Button variant="primary" icon={<KeyRound className="h-4 w-4" />} loading={busy} disabled={!canSubmit}
            onClick={() => void submit('set')}>{required ? '修改密码' : '启用密码'}</Button>
          {required === true && (
            <Button variant="danger" icon={<Trash2 className="h-4 w-4" />} disabled={busy || !current}
              onClick={() => setClearOpen(true)}>清除密码</Button>
          )}
          <span className="ml-auto flex items-center gap-1.5 text-[11.5px] text-fg-4">
            <Eye className="h-3.5 w-3.5" />忘记密码可在服务器配置文件中删除 api.dashboard_password 字段
          </span>
        </div>

        <Banner tone="info" compact>
          密码哈希保存在 <code className="font-mono text-[12px]">api.dashboard_password</code>, 明文不会出现在存储或日志中。保存后当前会话保持登录, 其他设备的会话需要重新输入。
        </Banner>
      </section>

      <ConfirmDialog open={clearOpen} onClose={() => setClearOpen(false)} danger confirmLabel="清除密码" loading={busy}
        title="清除访问密码?" description="清除后, 任何能访问管理面板地址的人都可以免密进入。"
        onConfirm={() => void submit('clear')}>
        <p className="text-sm text-fg-2">当前密码验证通过后, 配置文件中的哈希字段会被删除。API 与 api-token 访问不受影响。</p>
      </ConfirmDialog>
    </SectionCard>
  );
}
