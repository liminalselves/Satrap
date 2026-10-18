import { useId } from 'react';
import type { ApprovalMode } from '@/api/chat';
import { Select } from '@/components/ui/Select';
import type { ApprovalControl } from './useApprovalMode';

const MODES = [
  { value: 'user', label: '请求批准', description: '需要审批的操作先问你, 由你决定是否执行' },
  { value: 'auto', label: '帮我审批', description: 'AI 先审查是否可以执行, 拿不准或审查失败时再问你' },
  { value: 'full-access', label: '完全访问', description: '需要审批的操作也会直接执行, 包括本机命令和工作区外文件访问' },
];

export function ApprovalSettings({ approval, compact = false }: { approval: ApprovalControl; compact?: boolean }) {
  const labelId = useId();
  const { conversationId, state, isDraft, error, saving, save } = approval;
  const description = saving ? (isDraft ? '正在应用所选审批模式...' : '正在保存...')
    : state && !state.available ? '请先在插件中启用 permissions (执行审批)'
    : !conversationId && !isDraft ? '打开或创建对话后可切换审批模式'
    : (MODES.find((mode) => mode.value === state?.mode)?.description ?? '正在读取当前审批模式...')
      + (isDraft ? '; 从第一条消息开始生效' : '');

  return (
    <div className={compact ? 'flex flex-wrap items-center gap-2' : 'space-y-2'} role="group" aria-labelledby={labelId}>
      <label id={labelId} className={compact ? 'text-sm text-text-secondary' : 'block text-sm font-medium text-text-primary'}>
        {compact ? '审批' : '执行审批模式'}
      </label>
      <div className={compact ? 'w-28' : undefined} title={description}>
        <Select
          value={state?.mode ?? ''}
          disabled={!state?.available || saving}
          onChange={(event) => { void save(event.target.value as ApprovalMode); }}
          options={state ? MODES : [{ value: '', label: conversationId ? '读取中...' : '未创建对话' }]}
        />
      </div>
      {!compact && <p className="text-xs text-text-tertiary">{description}; 计划模式和受保护文件的限制仍然有效</p>}
      {state?.plan_mode && <p className="text-xs text-text-secondary">计划模式已开启{!compact && ', 不能修改文件或运行本机命令'}</p>}
      {error && <p role="alert" className="text-xs text-error max-w-xs">{error}</p>}
    </div>
  );
}
