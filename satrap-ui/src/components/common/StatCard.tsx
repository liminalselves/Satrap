import { ReactNode, memo } from 'react';
import { Card, CardProps } from '@/components/ui/Card';
import { cn } from '@/utils/cn';

export interface StatCardProps extends Omit<CardProps, 'children'> {
  icon: ReactNode;
  label: string;
  value: ReactNode;
  iconClassName?: string;
}

export const StatCard = memo(function StatCard({
  icon,
  label,
  value,
  iconClassName,
  variant = 'default',
  interactive = true,
  className,
  ...props
}: StatCardProps) {
  return (
    <Card variant={variant} interactive={interactive} className={className} {...props}>
      <div className="flex items-center gap-4">
        <div className={cn('h-6 w-6', iconClassName)}>{icon}</div>
        <div>
          <p className="text-sm text-text-secondary">{label}</p>
          <p className="text-xl font-semibold text-text-primary">{value}</p>
        </div>
      </div>
    </Card>
  );
});

export interface StatCardGridProps {
  children: ReactNode;
  className?: string;
}

export function StatCardGrid({ children, className }: StatCardGridProps) {
  return (
    <div className={cn('grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4', className)}>
      {children}
    </div>
  );
}
