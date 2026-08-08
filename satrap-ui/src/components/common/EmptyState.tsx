import { ReactNode, memo } from 'react';
import { Card } from '@/components/ui/Card';
import { cn } from '@/utils/cn';

export interface EmptyStateProps {
  icon?: ReactNode;
  title: string;
  description?: string;
  action?: ReactNode;
  className?: string;
}

export const EmptyState = memo(function EmptyState({
  icon,
  title,
  description,
  action,
  className,
}: EmptyStateProps) {
  return (
    <Card className={cn('text-center py-12', className)}>
      {icon && <div className="flex justify-center mb-4 text-text-tertiary">{icon}</div>}
      <p className="text-text-secondary">{title}</p>
      {description && <p className="text-text-tertiary text-sm mt-2">{description}</p>}
      {action && <div className="mt-4">{action}</div>}
    </Card>
  );
});
