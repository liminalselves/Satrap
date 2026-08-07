import { useBackendStore } from '@/stores/useBackendStore';
import { Badge } from '@/components/ui/Badge';
import { Sun, Moon } from 'lucide-react';
import { useTheme } from '@/hooks/useTheme';
import { useStandaloneGlassReflect } from '@/hooks/useGlassReflect';

export function Header() {
  const { health } = useBackendStore();
  const { theme, toggleTheme } = useTheme();
  const headerRef = useStandaloneGlassReflect<HTMLElement>({
    reflectRange: 150,
    reflectSize: 150,
  });

  return (
    <header ref={headerRef} className="h-14 glass-header flex items-center justify-between px-4 sticky top-2 z-40">
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
      </div>
    </header>
  );
}
