import { NavLink } from 'react-router-dom';

export function PlatformNavigation({ pending, incomplete = false }: { pending?: number; incomplete?: boolean }) {
  return <nav className="flex flex-wrap gap-2" aria-label="平台管理导航">
    <NavLink end to="/platforms" className={({ isActive }) => `glass-button px-4 py-2 text-sm ${isActive ? 'glass-button-primary' : ''}`}>平台列表</NavLink>
    <NavLink to="/platforms/approvals" className={({ isActive }) => `glass-button px-4 py-2 text-sm ${isActive ? 'glass-button-primary' : ''}`}>
      审批中心{pending !== undefined ? ` · 待审批 ${pending}${incomplete ? '+' : ''}` : incomplete ? ' · 数量未确认' : ''}
    </NavLink>
  </nav>;
}
