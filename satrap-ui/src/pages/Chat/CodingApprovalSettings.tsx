import { useEffect, useRef, useState } from 'react';
import { chatApi, type CodingApprovalMode, type CodingApprovalState } from '@/api/chat';
import { Select } from '@/components/ui/Select';

const MODES = [
  { value: 'user', label: '请求批准 (user)' },
  { value: 'auto', label: '帮我审批 (auto)' },
  { value: 'full-access', label: '完全访问 (full-access)' },
];

export function CodingApprovalSettings({ conversationId }: { conversationId: string }) {
  const [state, setState] = useState<CodingApprovalState | null>(null);
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);
  const generation = useRef(0);

  useEffect(() => {
    const version = ++generation.current;
    const controller = new AbortController();
    setState(null);
    setError('');
    setSaving(false);
    if (conversationId) {
      chatApi.getCodingApproval(conversationId, controller.signal).then((result) => {
        if (version === generation.current) setState(result);
      }).catch((reason: unknown) => {
        if (!controller.signal.aborted && version === generation.current) {
          setError(reason instanceof Error ? reason.message : String(reason));
        }
      });
    }
    return () => { generation.current++; controller.abort(); };
  }, [conversationId]);

  const save = async (mode: CodingApprovalMode) => {
    if (!state || !state.available || saving) return;
    const version = generation.current;
    setSaving(true);
    setError('');
    try {
      const result = await chatApi.setCodingApproval(conversationId, mode, state.revision);
      if (version === generation.current) setState(result);
    } catch (reason) {
      if (version !== generation.current) return;
      setError(reason instanceof Error ? reason.message : String(reason));
      try {
        const latest = await chatApi.getCodingApproval(conversationId);
        if (version === generation.current) setState(latest);
      } catch { /* 保留原始保存错误, 不显示未经服务端确认的新模式 */ }
    } finally {
      if (version === generation.current) setSaving(false);
    }
  };

  return (
    <div className="space-y-2" role="group" aria-labelledby="coding-approval-label">
      <label id="coding-approval-label" className="block text-sm font-medium text-text-primary">编程审批模式</label>
      <Select
        value={state?.mode ?? ''}
        disabled={!state?.available || saving}
        onChange={(event) => { void save(event.target.value as CodingApprovalMode); }}
        options={state ? MODES : [{ value: '', label: conversationId ? '读取当前模式...' : '创建会话后可设置' }]}
      />
      <p className="text-xs text-text-tertiary">
        {saving ? '正在保存...' : !conversationId ? '先创建或打开一个对话'
          : state && !state.available ? '请先在插件中启用 satrap_coding'
          : state?.mode === 'full-access' ? '允许直接执行本机命令和访问工作区外文件; 计划模式与固定保护仍生效'
          : state?.mode === 'auto' ? '需要审批的操作由独立模型审查, 不确定或失败时转人工'
          : '需要审批的操作交给你确认, 会话沙箱内普通文件修改可直接执行'}
      </p>
      {state?.plan_mode && <p className="text-xs text-text-secondary">计划模式已开启, 文件修改和 Shell 仍被禁止</p>}
      {error && <p role="alert" className="text-sm text-error">{error}</p>}
    </div>
  );
}
