import { NavLink } from 'react-router-dom';
import {
  LayoutDashboard,
  Cpu,
  MessageSquare,
  Globe,
  ScrollText,
  GitBranch,
  Users,
  Settings,
} from 'lucide-react';
import { cn } from '@/utils/cn';
import { useStandaloneGlassReflect } from '@/hooks/useGlassReflect';

// 为每个导航项分配颜色
const navItems = [
  { path: '/', icon: LayoutDashboard, label: '仪表盘', color: 'accent' },
  { path: '/models', icon: Cpu, label: '模型配置', color: 'purple' },
  { path: '/sessions', icon: MessageSquare, label: '会话管理', color: 'teal' },
  { path: '/platforms', icon: Globe, label: '平台状态', color: 'pink' },
  { path: '/logs', icon: ScrollText, label: '日志监控', color: 'orange' },
  { path: '/checkpoints', icon: GitBranch, label: '检查点', color: 'green' },
  { path: '/users', icon: Users, label: '用户管理', color: 'accent' },
  { path: '/settings', icon: Settings, label: '系统设置', color: 'purple' },
] as const;

// 导航项组件 - 每个项独立跟踪反射
function NavItem({ item }: { item: typeof navItems[number] }) {
  const reflectRef = useStandaloneGlassReflect<HTMLAnchorElement>({
    reflectRange: 100,
  });

  return (
    <NavLink
      ref={reflectRef}
      to={item.path}
      className={({ isActive }) =>
        cn(
          'glass-nav-item',
          `nav-${item.color}`,
          isActive && 'active'
        )
      }
    >
      <item.icon className="h-4 w-4" />
      {item.label}
    </NavLink>
  );
}

export function Sidebar() {
  const sidebarRef = useStandaloneGlassReflect<HTMLElement>({
    reflectRange: 150,
  });

  return (
    <aside ref={sidebarRef} className="w-64 sticky top-2 glass-sidebar flex flex-col">
      <div className="p-4 border-b border-glass-border">
        <h1 className="text-lg font-semibold text-text-primary flex items-center gap-2">
          <div className="w-8 h-8 rounded-md bg-accent flex items-center justify-center shadow-glow-accent">
            <span className="text-white font-semibold text-sm">S</span>
          </div>
          Satrap
        </h1>
      </div>

      <nav className="flex-1 py-2 overflow-y-auto custom-scrollbar">
        {navItems.map((item) => (
          <NavItem key={item.path} item={item} />
        ))}
      </nav>

      <div className="p-4 border-t border-glass-border">
        <div className="text-xs text-text-tertiary text-center">
          Satrap Admin v1.0
        </div>
      </div>
    </aside>
  );
}
