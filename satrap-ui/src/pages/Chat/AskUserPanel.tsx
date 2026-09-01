import { useRef, type KeyboardEvent } from 'react';
import { CheckCircle2, Loader2, Wrench } from 'lucide-react';
import { Button } from '@/components/ui/Button';
import { cn } from '@/utils/cn';

export interface PendingUserInput {
  requestId: string;
  question: string;
  options: string[];
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
    <div className="glass-card glass-card-accent rounded-xl px-4 py-3 mb-2" data-ask-user-panel>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2 text-sm font-medium text-accent mb-1.5">
            <Wrench className="h-4 w-4 shrink-0" />
            工具正在等待你的回答
          </div>
          <p className="text-sm text-text-primary whitespace-pre-wrap">
            {request.question}
          </p>
        </div>
        <span className="shrink-0 text-xs text-text-tertiary">1 / {queueLength}</span>
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
                  'w-full rounded-lg px-3 py-2 text-left text-sm flex items-start gap-3 border transition-colors',
                  selected
                    ? 'bg-accent-subtle border-accent text-text-primary'
                    : 'bg-transparent border-transparent text-text-secondary hover:bg-glass-hover hover:text-text-primary',
                )}
              >
                <span className="w-5 shrink-0 text-text-tertiary tabular-nums">{index + 1}.</span>
                <span className="flex-1 whitespace-pre-wrap">{option}</span>
                {selected && <CheckCircle2 className="h-4 w-4 shrink-0 text-accent mt-0.5" />}
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
        className="mt-3 w-full rounded-lg bg-glass border border-glass-border px-3 py-2 text-sm text-text-primary placeholder:text-text-tertiary outline-none focus:border-accent resize-y min-h-[42px] max-h-[160px]"
      />

      <div className="mt-3 flex items-center justify-between gap-3">
        <span className="text-xs text-text-tertiary">Tab / 方向键选择, Enter 提交</span>
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
