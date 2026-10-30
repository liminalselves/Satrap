import { useRef, type KeyboardEvent } from 'react';
import { CheckCircle2, Loader2, Wrench } from 'lucide-react';
import { Button } from '@/components/ui/Button';
import { cn } from '@/utils/cn';
import type { ApprovalPresentation } from '@/api/chat';

export interface PendingUserInput {
  requestId: string;
  question: string;
  options: string[];
  approval?: ApprovalPresentation | null;
}

interface AskUserPanelProps {
  request: PendingUserInput;
  answer: string;
  queueLength: number;
  submitting: boolean;
  onAnswerChange: (answer: string) => void;
  onSubmit: (answer: string) => void;
}

export function AskUserPanel({
  request,
  answer,
  queueLength,
  submitting,
  onAnswerChange,
  onSubmit,
}: AskUserPanelProps) {
  const optionRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const selectedIndex = request.options.indexOf(answer);

  if (request.approval) {
    const { tool, description, action } = request.approval;
    const isShell = tool === 'shell';
    return (
      <div className="surface surface-strong tint-accent mb-2 rounded-2xl px-4 py-3" data-ask-user-panel data-approval>
        <div className="flex items-center justify-between gap-3 text-sm font-medium text-accent">
          <span className="flex items-center gap-2"><Wrench className="h-4 w-4" />等待批准执行 · {tool}</span>
          <span className="num text-xs text-fg-4">1 / {queueLength}</span>
        </div>
        <details className="mt-3 text-sm text-fg">
          <summary className="cursor-pointer select-none text-fg-2">{isShell ? '查看待执行命令' : '查看本次操作'}</summary>
          <div className="mt-2 space-y-2">
            {isShell && typeof action.command === 'string' ? (
              <>
                <pre className="max-h-64 overflow-auto rounded-lg border border-line bg-glass p-3 whitespace-pre-wrap break-words">{action.command}</pre>
                {typeof action.cwd === 'string' && <p className="break-all text-fg-2">工作目录: {action.cwd}</p>}
                {typeof action.interpreter === 'string' && <p className="break-all text-fg-2">Shell: {action.interpreter}</p>}
              </>
            ) : <p className="whitespace-pre-wrap break-words text-fg-2">{description}</p>}
            <details>
              <summary className="cursor-pointer select-none text-fg-3">查看完整操作参数</summary>
              <pre className="mt-2 max-h-64 overflow-auto rounded-lg border border-line bg-glass p-3 whitespace-pre-wrap break-all">{JSON.stringify(action, null, 2)}</pre>
            </details>
          </div>
        </details>
        <div className="mt-3 flex items-center justify-end gap-2">
          <Button variant="ghost" size="sm" disabled={submitting} onClick={() => onSubmit('拒绝')}>拒绝</Button>
          <Button variant="primary" size="sm" disabled={submitting} onClick={() => onSubmit('批准本次')}>
            {submitting ? <Loader2 className="h-4 w-4 animate-spin" /> : '批准本次'}
          </Button>
        </div>
      </div>
    );
  }

  const handleOptionKeyDown = (
    event: KeyboardEvent<HTMLButtonElement>,
    index: number,
  ) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      onSubmit(request.options[index]);
      return;
    }
    if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
    event.preventDefault();
    const offset = event.key === 'ArrowDown' ? 1 : -1;
    const targetIndex = (index + offset + request.options.length) % request.options.length;
    onAnswerChange(request.options[targetIndex]);
    optionRefs.current[targetIndex]?.focus();
  };

  return (
    <div className="surface surface-strong tint-accent mb-2 rounded-2xl px-4 py-3" data-ask-user-panel>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="mb-1.5 flex items-center gap-2 text-sm font-medium text-accent">
            <Wrench className="h-4 w-4 flex-none" />
            工具正在等待你的回答
          </div>
          <p className="whitespace-pre-wrap text-sm text-fg">
            {request.question}
          </p>
        </div>
        <span className="num flex-none text-xs text-fg-4">1 / {queueLength}</span>
      </div>

      {request.options.length > 0 && (
        <div className="mt-3 space-y-1" role="listbox" aria-label="推荐回答选项">
          {request.options.map((option, index) => {
            const selected = selectedIndex === index;
            return (
              <button
                key={`${index}-${option}`}
                ref={(element) => { optionRefs.current[index] = element; }}
                type="button"
                role="option"
                aria-selected={selected}
                disabled={submitting}
                onClick={() => onAnswerChange(option)}
                onKeyDown={(event) => handleOptionKeyDown(event, index)}
                className={cn(
                  'flex w-full items-start gap-3 rounded-xl border px-3 py-2 text-left text-sm transition-colors',
                  selected
                    ? 'border-[rgb(var(--rgb-accent)/0.55)] bg-[rgb(var(--rgb-accent)/0.14)] text-fg'
                    : 'border-transparent bg-transparent text-fg-2 hover:bg-glass-hover hover:text-fg',
                )}
              >
                <span className="num w-5 flex-none text-fg-4">{index + 1}.</span>
                <span className="flex-1 whitespace-pre-wrap">{option}</span>
                {selected && <CheckCircle2 className="mt-0.5 h-4 w-4 flex-none text-accent" />}
              </button>
            );
          })}
        </div>
      )}

      <textarea
        value={answer}
        disabled={submitting}
        onChange={(event) => onAnswerChange(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === 'Enter' && !event.shiftKey) {
            event.preventDefault();
            onSubmit(answer);
          }
        }}
        placeholder="输入自定义回答..."
        rows={2}
        className="field mt-3 min-h-[42px] max-h-[160px] w-full resize-y"
      />

      <div className="mt-3 flex items-center justify-between gap-3">
        <span className="text-xs text-fg-4">Tab / 方向键选择, Enter 提交</span>
        <div className="flex items-center gap-2">
          <Button
            variant="ghost"
            size="sm"
            disabled={submitting}
            onClick={() => onSubmit('用户选择跳过此问题')}
          >
            跳过
          </Button>
          <Button
            variant="primary"
            size="sm"
            disabled={!answer.trim() || submitting}
            onClick={() => onSubmit(answer)}
          >
            {submitting ? <Loader2 className="h-4 w-4 animate-spin" /> : '提交'}
          </Button>
        </div>
      </div>
    </div>
  );
}
