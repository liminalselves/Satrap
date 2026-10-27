import { ArrowLeft, Moon, PanelLeft, Settings2, Sparkles, Sun } from 'lucide-react';
import { Badge } from '@/components/kit/Badge';
import { cn } from '@/utils/cn';

export type ChatTheme = 'dark' | 'light';

export function ChatHeader({
  theme,
  title,
  subtitle,
  rounds,
  listOpen,
  onToggleList,
  onBack,
  onToggleTheme,
  onSettings,
}: {
  theme: ChatTheme;
  title: string;
  subtitle?: string;
  rounds?: number;
  listOpen: boolean;
  onToggleList: () => void;
  onBack: () => void;
  onToggleTheme: (origin?: { x: number; y: number }) => void;
  onSettings: () => void;
}) {
  return (
    <header className="surface flex h-14 flex-none items-center gap-2 !rounded-2xl px-2.5 md:px-3">
      <button
        type="button"
        title="返回管理面板"
        aria-label="返回管理面板"
        onClick={onBack}
        className="ctl ctl-ghost ctl-icon"
      >
        <ArrowLeft className="h-4 w-4" />
      </button>
      <button
        type="button"
        title={listOpen ? '收起会话列表' : '展开会话列表'}
        aria-label={listOpen ? '收起会话列表' : '展开会话列表'}
        aria-expanded={listOpen}
        onClick={onToggleList}
        className="ctl ctl-ghost ctl-icon"
      >
        <PanelLeft className="h-4 w-4" />
      </button>

      <div className="ml-0.5 flex min-w-0 items-center gap-2.5">
        <div className="grid h-7 w-7 flex-none place-items-center rounded-[9px] bg-[linear-gradient(135deg,#2b88d8,#0078d4_45%,#8b62b3)] shadow-[0_4px_14px_-4px_rgb(var(--rgb-accent)/0.8)]">
          <Sparkles className="h-3.5 w-3.5 text-white" />
        </div>
        <div className="min-w-0">
          <div className="truncate text-[14px] font-semibold text-fg">{title}</div>
          {subtitle && <div className="truncate text-[11px] text-fg-3">{subtitle}</div>}
        </div>
      </div>

      <div className="ml-auto flex flex-none items-center gap-1">
        {typeof rounds === 'number' && rounds > 0 && (
          <Badge tone="teal" className="mr-0.5 hidden sm:inline-flex">
            <span className="num">{rounds} 轮</span>
          </Badge>
        )}
        <button
          type="button"
          title="对话设置"
          aria-label="对话设置"
          onClick={onSettings}
          className="ctl ctl-ghost ctl-icon"
        >
          <Settings2 className="h-4 w-4" />
        </button>
        <button
          type="button"
          title={theme === 'dark' ? '切换到亮色模式' : '切换到暗色模式'}
          aria-label="切换主题"
          onClick={(event) => onToggleTheme({ x: event.clientX, y: event.clientY })}
          className={cn('ctl ctl-ghost ctl-icon')}
        >
          {theme === 'dark' ? <Sun className="h-4 w-4 text-orange" /> : <Moon className="h-4 w-4 text-accent" />}
        </button>
      </div>
    </header>
  );
}
