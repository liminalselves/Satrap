import { NavLink } from 'react-router-dom';
import { ShieldCheck } from 'lucide-react';
import { Badge, StatusDot } from '@/components/kit';

/** 平台管理子导航: 平台列表与审批中心共用, 审批数量未知时明确标注 */
export function PlatformNavigation({ pending, incomplete = false }: { pending?: number; incomplete?: boolean }) {
  return (
    <nav className="flex flex-wrap items-center gap-1.5 animate-rise" aria-label="平台管理导航">
      <NavLink end to="/platforms"
        className={({ isActive }) => `rounded-[10px] border px-3 py-1.5 text-[13px] font-medium transition ${isActive
          ? 'border-[rgb(var(--rgb-pink)/0.3)] bg-[rgb(var(--rgb-pink)/0.12)] text-pink'
          : 'border-transparent text-fg-3 hover:bg-glass-hover hover:text-fg'}`}>
        平台列表
      </NavLink>
      <NavLink to="/platforms/approvals"
        className={({ isActive }) => `flex items-center gap-2 rounded-[10px] border px-3 py-1.5 text-[13px] transition ${isActive
          ? 'border-[rgb(var(--rgb-pink)/0.3)] bg-[rgb(var(--rgb-pink)/0.12)] text-pink'
          : 'border-transparent text-fg-3 hover:bg-glass-hover hover:text-fg'}`}>
        <ShieldCheck className="h-3.5 w-3.5" />
        审批中心
        {pending !== undefined && <Badge tone="orange" dot>{pending}{incomplete ? '+' : ''}</Badge>}
        {pending === undefined && incomplete && <Badge>数量未确认</Badge>}
      </NavLink>
      <span className="ml-auto hidden items-center gap-2 text-[11.5px] text-fg-4 sm:flex">
        <StatusDot tone="green" live />实时健康探测
      </span>
    </nav>
  );
}
