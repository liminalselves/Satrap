import type { MouseEvent } from 'react';
import { toast } from '@/components/ui/Toast';
import { cn } from '@/utils/cn';
import { copyTextToClipboard } from '@/utils/clipboard';

interface CopyButtonProps {
  text: string;
  title: string;
  className?: string;
}

const resetTimers = new WeakMap<HTMLButtonElement, number>();

function showCopiedState(button: HTMLButtonElement, title: string): void {
  const previousTimer = resetTimers.get(button);
  if (previousTimer !== undefined) window.clearTimeout(previousTimer);
  button.dataset.copyState = 'copied';
  button.title = '已复制';
  button.setAttribute('aria-label', '已复制');
  const timer = window.setTimeout(() => {
    button.dataset.copyState = 'idle';
    button.title = title;
    button.setAttribute('aria-label', title);
    resetTimers.delete(button);
  }, 1500);
  resetTimers.set(button, timer);
}

export function CopyButton({ text, title, className }: CopyButtonProps) {
  const handleCopy = async (event: MouseEvent<HTMLButtonElement>) => {
    const button = event.currentTarget;
    try {
      await copyTextToClipboard(text);
      showCopiedState(button, title);
    } catch (error) {
      toast('error', '复制失败: ' + (error instanceof Error ? error.message : '浏览器不支持复制'));
    }
  };

  return (
    <button
      type="button"
      onClick={handleCopy}
      className={cn('copy-action-button rounded text-text-tertiary hover:text-accent focus-visible:text-accent', className)}
      title={title}
      aria-label={title}
      data-copy-state="idle"
    />
  );
}
