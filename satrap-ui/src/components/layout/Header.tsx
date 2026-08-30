import { useEffect } from 'react';
import { useNavigate, useLocation } from 'react-router-dom';
import { useBackendStore } from '@/stores/useBackendStore';
import { Badge } from '@/components/ui/Badge';
import { Sun, Moon, MessageSquare } from 'lucide-react';
import { useTheme } from '@/hooks/useTheme';
import { useGlassReflect } from '@/hooks/useGlassReflect';
import { cn } from '@/utils/cn';

export function Header() {
  const { isRunning, refreshControlStatus } = useBackendStore();
  const { theme, toggleTheme } = useTheme();
  const navigate = useNavigate();
  const location = useLocation();
  const isChat = location.pathname === '/chat';
  const headerRef = useGlassReflect<HTMLElement>({
    reflectRange: 150,
  });

  // 定期刷新控制服务状态
  useEffect(() => {
    refreshControlStatus();
    const interval = setInterval(refreshControlStatus, 5000);
    return () => clearInterval(interval);
  }, [refreshControlStatus]);

  return (
    <header ref={headerRef} className="h-14 glass-header flex items-center justify-between px-4 sticky top-2 z-40">
      <div className="flex items-center gap-3">
        <h2 className="text-base font-semibold text-text-primary">管理面板</h2>
        <Badge variant={isRunning ? 'success' : 'error'}>
          {isRunning ? '后端运行中' : '后端未运行'}
        </Badge>
      </div>

      <div className="flex items-center gap-2">
        {/* 对话入口 */}
        <button
          onClick={() => navigate('/chat')}
          className={cn('theme-toggle', isChat && 'text-accent')}
          title="对话"
        >
          <MessageSquare className={cn('h-4 w-4', isChat ? 'text-accent' : 'text-text-secondary')} />
        </button>

        {/* 主题切换按钮 */}
        <button
          onClick={toggleTheme}
          className="theme-toggle"
          title={theme === 'dark' ? '切换到亮色模式' : '切换到暗色模式'}
        >
          {theme === 'dark' ? (
            <Sun className="h-4 w-4 text-warning" />
          ) : (
            <Moon className="h-4 w-4 text-accent" />
          )}
        </button>
      </div>
    </header>
  );
}
