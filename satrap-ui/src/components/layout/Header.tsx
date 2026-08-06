import { useBackendStore } from '@/stores/useBackendStore';
import { Badge } from '@/components/ui/Badge';
import { RefreshCw, Sun, Moon } from 'lucide-react';
import { Button } from '@/components/ui/Button';
import { useTheme } from '@/hooks/useTheme';

export function Header() {
  const { health, refreshHealth, loading } = useBackendStore();
  const { theme, toggleTheme } = useTheme();

  return (
    <header className="h-14 glass border-b border-glass-border flex items-center justify-between px-4 sticky top-0 z-40">
      <div className="flex items-center gap-3">
        <h2 className="text-base font-semibold text-text-primary">管理面板</h2>
        {health && (
          <Badge variant={health.running ? 'success' : 'error'}>
            {health.running ? '后端运行中' : '后端未运行'}
          </Badge>
        )}
      </div>

      <div className="flex items-center gap-2">
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

        <Button
          variant="subtle"
          size="sm"
          onClick={refreshHealth}
          disabled={loading}
        >
          <RefreshCw className={`h-4 w-4 mr-1 ${loading ? 'animate-spin' : ''}`} />
          刷新状态
        </Button>
      </div>
    </header>
  );
}
