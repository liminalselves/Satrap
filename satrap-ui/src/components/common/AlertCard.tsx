import { ReactNode, memo } from 'react';
import { Card, CardProps } from '@/components/ui/Card';
import { cn } from '@/utils/cn';

export interface AlertCardProps extends Omit<CardProps, 'children'> {
  icon: ReactNode;
  title: string;
  description?: ReactNode;
  iconClassName?: string;
}

export const AlertCard = memo(function AlertCard({
  icon,
  title,
  description,
  iconClassName,
  variant = 'warning',
  className,
  ...props
}: AlertCardProps) {
  return (
    <Card variant={variant} className={className} {...props}>
      <div className="flex items-center gap-4">
        <div className={cn('p-3 rounded-lg bg-glass-warning', iconClassName)}>
          {icon}
        </div>
        <div className="flex-1">
          <h3 className="text-lg font-semibold text-text-primary">{title}</h3>
          {description && <div className="text-text-secondary mt-1">{description}</div>}
        </div>
      </div>
    </Card>
  );
});
