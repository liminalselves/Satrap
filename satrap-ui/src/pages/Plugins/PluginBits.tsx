// 插件管理页共用的展示组件: 图标、健康、能力与区段标签
import type { ComponentProps, KeyboardEvent, ReactNode } from 'react';
import {
  BarChart3, BookOpen, Brain, Clock, Image as ImageIcon, Languages, MessagesSquare, Mic, Plug, Search,
  ShieldCheck, Sparkles, type LucideIcon,
} from 'lucide-react';
import { cn } from '@/utils/cn';
import { Badge, Button, StatusDot } from '@/components/kit';
import type { CatalogPlugin } from '@/api/pluginsContracts';
import {
  CAPABILITY_LABELS, CAPABILITY_TINT, HEALTH_LABELS, HEALTH_TINTS, pluginAccent, pluginHealth, pluginIcon,
  type Capabilities, type PluginIconKey,
} from './pluginModel';

const ICONS: Record<PluginIconKey, LucideIcon> = {
  messages: MessagesSquare,
  book: BookOpen,
  brain: Brain,
  plug: Plug,
  search: Search,
  clock: Clock,
  shield: ShieldCheck,
  languages: Languages,
  image: ImageIcon,
  chart: BarChart3,
  mic: Mic,
};

export function PluginIcon({ plugin, size = 'md', showHealth = false, className }: {
  plugin: Pick<CatalogPlugin, 'name' | 'icon' | 'capabilities' | 'health'>;
  size?: 'sm' | 'md' | 'lg';
  showHealth?: boolean;
  className?: string;
}) {
  const Icon = ICONS[pluginIcon(plugin)] ?? Plug;
  const box = { sm: 'h-9 w-9 rounded-[11px]', md: 'h-11 w-11 rounded-[14px]', lg: 'h-16 w-16 rounded-[20px]' }[size];
  const glyph = { sm: 'h-4 w-4', md: 'h-5 w-5', lg: 'h-7 w-7' }[size];
  const health = pluginHealth(plugin);
  return (
    <div className={cn('relative flex-none', className)}>
      <div className={cn(
        'relative grid place-items-center overflow-hidden border border-[rgb(var(--tint)/0.3)] text-tint',
        'bg-[linear-gradient(135deg,rgb(var(--tint)/0.3),rgb(var(--tint)/0.06))]',
        'shadow-[inset_0_1px_0_var(--glass-highlight),0_10px_28px_-12px_rgb(var(--tint)/0.8)]',
        `tint-${pluginAccent(plugin)}`,
        box,
      )}>
        <span aria-hidden className="pointer-events-none absolute -right-4 -top-5 h-12 w-12 rounded-full bg-[rgb(var(--tint)/0.22)] blur-xl" />
        <Icon className={cn('relative', glyph)} />
      </div>
      {showHealth && health !== 'unknown' && (
        <span className="absolute -bottom-0.5 -right-0.5 rounded-full bg-[var(--bg-base)] p-[3px]">
          <StatusDot
            tone={health === 'healthy' ? 'green' : health === 'attention' ? 'orange' : 'neutral'}
            live={health === 'healthy'}
            className="!h-2 !w-2"
          />
        </span>
      )}
    </div>
  );
}

export function HealthChip({ plugin, className }: { plugin: Pick<CatalogPlugin, 'health'>; className?: string }) {
  const state = pluginHealth(plugin);
  return (
    <Badge tone={HEALTH_TINTS[state]} dot live={state === 'healthy'} className={className}>
      {HEALTH_LABELS[state]}
    </Badge>
  );
}

export function ExecutionChip({ enabled }: { enabled?: boolean }) {
  if (enabled === undefined) return null;
  return <Badge tone={enabled ? 'teal' : 'neutral'} dot live={enabled}>{enabled ? '执行已开启' : '执行已停用'}</Badge>;
}

export function SourceChip({ source }: { source: string }) {
  return <Badge tone={source === 'builtin' ? 'teal' : 'purple'}>{source === 'builtin' ? '内置' : '用户'}</Badge>;
}

export function BuiltinMark() {
  return <Sparkles className="h-3.5 w-3.5 flex-none text-teal" aria-label="内置插件" />;
}

export function capabilityGroups(caps: Capabilities) {
  return (Object.keys(CAPABILITY_LABELS) as (keyof typeof CAPABILITY_LABELS)[])
    .map((kind) => ({ kind, label: CAPABILITY_LABELS[kind], items: Object.entries(caps[kind] ?? {}) }))
    .filter((group) => group.items.length > 0);
}

export function CapabilityChips({ caps, max = 4, className }: { caps: Capabilities; max?: number; className?: string }) {
  const groups = capabilityGroups(caps);
  const shown = groups.slice(0, max);
  return (
    <div className={cn('flex flex-wrap gap-1.5', className)}>
      {shown.map((group) => (
        <Badge key={group.kind} tone={CAPABILITY_TINT[group.kind]}>
          {group.label}
          <span className="num opacity-70">{group.items.length}</span>
        </Badge>
      ))}
      {groups.length > max && <Badge tone="neutral">+{groups.length - max}</Badge>}
    </div>
  );
}

export function CapabilityDeclarations({ caps }: { caps: Capabilities }) {
  const groups = capabilityGroups(caps);
  if (!groups.length) {
    return <p className="rounded-xl border border-dashed border-line px-4 py-6 text-center text-[13px] text-fg-3">此插件未声明任何能力</p>;
  }
  return (
    <div className="space-y-4">
      {groups.map((group) => (
        <section key={group.kind} className={cn('rounded-2xl border border-line bg-glass p-4', `tint-${CAPABILITY_TINT[group.kind]}`)}>
          <div className="mb-3 flex items-center gap-2">
            <span className="grid h-6 w-6 place-items-center rounded-lg bg-[rgb(var(--tint)/0.16)] text-tint">
              <span className="h-1.5 w-1.5 rounded-full bg-tint shadow-[0_0_8px_rgb(var(--tint))]" />
            </span>
            <h4 className="text-[13.5px] font-semibold text-fg">{group.label}</h4>
            <span className="num rounded-full bg-glass-hover px-1.5 text-[11px] text-fg-3">{group.items.length}</span>
          </div>
          <ul className="space-y-2">
            {group.items.map(([name, description]) => (
              <li key={name} className="flex flex-col gap-0.5 rounded-lg bg-[rgb(var(--tint)/0.05)] px-3 py-2 sm:flex-row sm:items-baseline sm:gap-3">
                <code className="flex-none break-all font-mono text-[12.5px] font-medium text-fg">{name}</code>
                <span className="min-w-0 break-words text-[12.5px] leading-5 text-fg-2">{description || '未提供说明'}</span>
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}

/* ===== 区段标签: 使用编排组青色, 并保持按钮可访问名与文字一致 ===== */

export interface TintTabItem<T extends string> {
  value: T;
  label: ReactNode;
  icon?: ReactNode;
  count?: number;
  ariaLabel?: string;
}

export function TintTabs<T extends string>({ items, value, onChange, size = 'md', className, label, stretch }: {
  items: TintTabItem<T>[];
  value: T;
  onChange: (value: T) => void;
  size?: 'sm' | 'md';
  className?: string;
  label: string;
  stretch?: boolean;
}) {
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const index = items.findIndex((item) => item.value === value);
    let next = -1;
    if (event.key === 'ArrowRight') next = (index + 1) % items.length;
    if (event.key === 'ArrowLeft') next = (index - 1 + items.length) % items.length;
    if (event.key === 'Home') next = 0;
    if (event.key === 'End') next = items.length - 1;
    if (next < 0) return;
    event.preventDefault();
    onChange(items[next].value);
    const target = event.currentTarget.querySelectorAll<HTMLButtonElement>('[data-tint-tab]')[next];
    target?.focus();
  };
  return (
    <div
      role="group"
      aria-label={label}
      onKeyDown={onKeyDown}
      className={cn(
        'tint-teal inline-flex max-w-full items-center gap-0.5 overflow-x-auto rounded-[12px] border border-line bg-glass p-1 backdrop-blur-md [scrollbar-width:none] [&::-webkit-scrollbar]:hidden',
        className,
      )}
    >
      {items.map((item) => {
        const selected = item.value === value;
        return (
          <button
            key={item.value}
            type="button"
            data-tint-tab
            data-value={item.value}
            aria-pressed={selected}
            aria-label={item.ariaLabel}
            onClick={() => onChange(item.value)}
            className={cn(
              'relative inline-flex flex-none items-center gap-2 rounded-[9px] font-medium transition-colors duration-200',
              size === 'sm' ? 'h-7 px-2.5 text-[12.5px]' : 'h-8 px-3.5 text-sm',
              selected
                ? 'border border-[rgb(var(--tint)/0.28)] bg-[rgb(var(--tint)/0.14)] text-tint shadow-[0_4px_16px_-4px_rgb(var(--tint)/0.45),inset_0_1px_0_var(--glass-highlight)]'
                : 'text-fg-3 hover:text-fg',
              stretch && 'flex-1 justify-center',
            )}
          >
            {item.icon}
            <span className="whitespace-nowrap">{item.label}</span>
            {item.count !== undefined && (
              <span aria-hidden className={cn('num rounded-full px-1.5 text-[11px] leading-[18px]', selected ? 'bg-[rgb(var(--tint)/0.2)]' : 'bg-glass-hover text-fg-3')}>{item.count}</span>
            )}
          </button>
        );
      })}
    </div>
  );
}

/** 主要操作按钮: 使用编排组青色而不是全局蓝色 */
export function TealPrimary({ className, children, ...props }: ComponentProps<typeof Button>) {
  return (
    <Button
      {...props}
      variant="primary"
      className={cn(
        '!border-[rgb(var(--rgb-teal)/0.9)] !bg-[linear-gradient(180deg,rgb(var(--rgb-teal)/0.95),rgb(var(--rgb-teal)/0.82))]',
        '!shadow-[inset_0_1px_0_rgba(255,255,255,0.22),0_6px_20px_-6px_rgb(var(--rgb-teal)/0.65)]',
        'hover:!bg-[linear-gradient(180deg,rgb(var(--rgb-teal)),rgb(var(--rgb-teal)/0.9))]',
        className,
      )}
    >
      {children}
    </Button>
  );
}
