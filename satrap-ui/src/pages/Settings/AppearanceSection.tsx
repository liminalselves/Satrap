import { useId } from 'react';
import { Droplets, Palette, PanelLeftClose, Sparkles, Waves } from 'lucide-react';
import { cn } from '@/utils/cn';
import { useTheme } from '@/hooks/useTheme';
import { usePrefsStore, useVisualEffects } from '@/stores/usePrefsStore';
import { Badge, Switch, toast } from '@/components/kit';
import { Logo } from '@/components/common/Logo';
import { InlineSwitchRow, SectionCard } from './shared';

/** 外观偏好: 只保存在浏览器, 不写入后端配置 */
export function AppearanceSection({ i }: { i: number }) {
  const { theme, toggleTheme } = useTheme();
  const motion = usePrefsStore((state) => state.motion);
  const setMotion = usePrefsStore((state) => state.setMotion);
  const sidebarCollapsed = usePrefsStore((state) => state.sidebarCollapsed);
  const toggleSidebar = usePrefsStore((state) => state.toggleSidebar);
  const glassPref = usePrefsStore((state) => state.glass);
  const ambientPref = usePrefsStore((state) => state.ambient);
  const setGlass = usePrefsStore((state) => state.setGlass);
  const setAmbient = usePrefsStore((state) => state.setAmbient);
  const effects = useVisualEffects();
  const autoHint = (pref: boolean | null) => (pref === null
    ? `当前按设备自动${effects.lowPower ? '关闭 (检测为低性能设备)' : '开启'}。`
    : '已手动设置。');
  const groupId = useId();

  return (
    <SectionCard id="appearance" i={i} tint="purple" icon={<Palette className="h-4 w-4" />}
      title="外观" description="主题、动效与侧栏偏好, 只保存在当前浏览器, 不会写入后端配置"
      actions={<Badge tone="green" dot>自动保存</Badge>}>
      <div className="grid gap-3 sm:grid-cols-2" role="radiogroup" aria-labelledby={groupId}>
        <p id={groupId} className="sr-only">主题</p>
        {(['dark', 'light'] as const).map((value) => {
          const selected = theme === value;
          return (
            <button key={value} type="button" role="radio" aria-checked={selected}
              onClick={(event) => { if (!selected) toggleTheme({ x: event.clientX, y: event.clientY }); }}
              className={cn('group relative overflow-hidden rounded-xl border p-1.5 text-left transition-all duration-300',
                selected
                  ? 'border-[rgb(var(--rgb-purple)/0.55)] shadow-[0_0_0_3px_rgb(var(--rgb-purple)/0.14),0_12px_32px_-16px_rgb(var(--rgb-purple)/0.7)]'
                  : 'border-line hover:border-[var(--glass-border-strong)]')}>
              <div className={cn('relative h-28 overflow-hidden rounded-lg', value === 'dark' ? 'bg-[#101014]' : 'bg-[#eef1f6]')}>
                <div className="absolute -left-6 -top-8 h-24 w-24 rounded-full bg-[#64b4ff] opacity-50 blur-2xl" />
                <div className="absolute -bottom-10 right-0 h-24 w-24 rounded-full bg-[#b48ce6] opacity-50 blur-2xl" />
                <div className={cn('absolute bottom-2 left-2 top-2 w-11 rounded-md border', value === 'dark' ? 'border-white/10 bg-white/5' : 'border-black/5 bg-white/70')}>
                  <Logo className="mx-auto mt-2 h-5 w-5" label="" />
                  <div className="mt-2 space-y-1 px-1.5">
                    {[0, 1, 2].map((n) => <div key={n} className={cn('h-1 rounded-full', value === 'dark' ? 'bg-white/20' : 'bg-black/10')} style={{ width: `${90 - n * 18}%` }} />)}
                  </div>
                </div>
                <div className={cn('absolute left-[4.1rem] right-2 top-2 h-4 rounded-md border', value === 'dark' ? 'border-white/10 bg-white/5' : 'border-black/5 bg-white/70')} />
                <div className="absolute left-[4.1rem] top-8 grid w-[calc(100%-4.8rem)] grid-cols-3 gap-1.5">
                  {['#0078d4', '#8b62b3', '#009999'].map((color) => (
                    <div key={color} className={cn('h-10 rounded-md border', value === 'dark' ? 'border-white/10' : 'border-black/5')}
                      style={{ background: `linear-gradient(135deg, ${color}33, transparent)` }} />
                  ))}
                </div>
                <div className={cn('absolute bottom-2 left-[4.1rem] right-2 h-7 rounded-md border', value === 'dark' ? 'border-white/10 bg-white/5' : 'border-black/5 bg-white/70')} />
              </div>
              <div className="flex items-center justify-between px-2 pb-1 pt-2.5 text-sm font-medium text-fg">
                {value === 'dark' ? '暗色' : '亮色'}
                <span className={cn('grid h-[18px] w-[18px] place-items-center rounded-full border-2 transition', selected ? 'border-purple' : 'border-[var(--glass-border-strong)]')}>
                  {selected && <span className="h-2 w-2 rounded-full bg-purple animate-pop" />}
                </span>
              </div>
            </button>
          );
        })}
      </div>

      <div className="mt-4 divide-y divide-[var(--glass-border)] rounded-xl border border-line bg-glass px-4">
        <InlineSwitchRow label={<span className="flex items-center gap-2">完整动效 <Sparkles className="h-3.5 w-3.5 text-purple" /></span>}
          hint="关闭后停止背景流动、悬浮位移与进场动画。系统开启“减少动态效果”时自动降级。">
          <Switch label="完整动效" checked={motion === 'full'} onChange={(value) => {
            setMotion(value ? 'full' : 'reduced');
            toast.success(value ? '已开启完整动效' : '已减少动效');
          }} />
        </InlineSwitchRow>
        <InlineSwitchRow label={<span className="flex items-center gap-2">亚克力效果 <Droplets className="h-3.5 w-3.5 text-accent" /></span>}
          hint={<>面板背后的毛玻璃模糊, 低性能设备上最耗资源。{autoHint(glassPref)}{glassPref !== null && <AutoReset onClick={() => setGlass(null)} />}</>}>
          <Switch label="亚克力效果" checked={effects.glass} onChange={(value) => {
            setGlass(value);
            toast.success(value ? '已开启亚克力效果' : '已关闭亚克力效果');
          }} />
        </InlineSwitchRow>
        <InlineSwitchRow label={<span className="flex items-center gap-2">动态背景 <Waves className="h-3.5 w-3.5 text-teal" /></span>}
          hint={<>背景中缓慢流动的彩色光斑; 关闭后换成静态渐变。{autoHint(ambientPref)}{ambientPref !== null && <AutoReset onClick={() => setAmbient(null)} />}</>}>
          <Switch label="动态背景" checked={effects.ambient} onChange={(value) => {
            setAmbient(value);
            toast.success(value ? '已开启动态背景' : '已关闭动态背景');
          }} />
        </InlineSwitchRow>
        <InlineSwitchRow label={<span className="flex items-center gap-2">收起侧栏 <PanelLeftClose className="h-3.5 w-3.5 text-fg-3" /></span>}
          hint="为内容区腾出更多空间, 也可以随时按 Ctrl+B 切换。">
          <Switch label="收起侧栏" checked={sidebarCollapsed} onChange={() => {
            toggleSidebar();
            toast.success(sidebarCollapsed ? '侧栏已展开' : '侧栏已收起', '快捷键 Ctrl+B');
          }} />
        </InlineSwitchRow>
      </div>
      <p className="mt-3 text-[12px] text-fg-4">主题切换会从点击位置圆形扩散。以上偏好保存在当前浏览器的 localStorage。</p>
    </SectionCard>
  );
}

function AutoReset({ onClick }: { onClick: () => void }) {
  return <button type="button" onClick={onClick} className="ml-1 text-accent hover:underline">恢复自动</button>;
}
