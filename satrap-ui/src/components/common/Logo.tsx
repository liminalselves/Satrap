import logoUrl from '@/assets/logo.svg';
import { cn } from '@/utils/cn';

/** Satrap 标志: 自带渐变与透明背景, 不再叠加底色方块 */
export function Logo({ className, label = 'Satrap' }: { className?: string; label?: string }) {
  return <img src={logoUrl} alt={label} draggable={false} className={cn('pointer-events-none select-none object-contain', className)} />;
}
