import { ReactNode, memo } from 'react';
import { Button, ButtonProps } from '@/components/ui/Button';
import { cn } from '@/utils/cn';

export interface ActionButton {
  key: string;
  icon?: ReactNode;
  label?: string;
  variant?: ButtonProps['variant'];
  size?: ButtonProps['size'];
  onClick: () => void;
  disabled?: boolean;
  title?: string;
  className?: string;
}

export interface ActionButtonsProps {
  actions: ActionButton[];
  className?: string;
}

export const ActionButtons = memo(function ActionButtons({
  actions,
  className,
}: ActionButtonsProps) {
  return (
    <div className={cn('flex items-center gap-2', className)}>
      {actions.map((action) => (
        <Button
          key={action.key}
          variant={action.variant || 'ghost'}
          size={action.size || 'sm'}
          onClick={action.onClick}
          disabled={action.disabled}
          title={action.title}
          className={action.className}
        >
          {action.icon}
          {action.label && <span className={action.icon ? 'ml-1' : ''}>{action.label}</span>}
        </Button>
      ))}
    </div>
  );
});
