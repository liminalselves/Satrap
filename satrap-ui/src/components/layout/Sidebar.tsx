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

const navItems = [
  { path: '/', icon: LayoutDashboard, label: '仪表盘' },
  { path: '/models', icon: Cpu, label: '模型配置' },
  { path: '/sessions', icon: MessageSquare, label: '会话管理' },
  { path: '/platforms', icon: Globe, label: '平台状态' },
  { path: '/logs', icon: ScrollText, label: '日志监控' },
  { path: '/checkpoints', icon: GitBranch, label: '检查点' },
  { path: '/users', icon: Users, label: '用户管理' },
  { path: '/settings', icon: Settings, label: '系统设置' },
];

export function Sidebar() {
  return (
    <aside className="w-64 h-screen sticky top-0 glass-sidebar flex flex-col">
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
          <NavLink
            key={item.path}
            to={item.path}
            className={({ isActive }) =>
              cn(
                'glass-nav-item',
                isActive && 'active'
              )
            }
          >
            <item.icon className="h-4 w-4" />
            {item.label}
          </NavLink>
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
