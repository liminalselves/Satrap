import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { useNavigate } from 'react-router-dom';
import { create } from 'zustand';
import { CornerDownLeft, MessageSquarePlus, Moon, PanelLeft, Play, RefreshCw, Search, Square } from 'lucide-react';
import { useBackendStore } from '@/stores/useBackendStore';
import { usePrefsStore } from '@/stores/usePrefsStore';
import { switchTheme } from '@/hooks/useTheme';
import { toast } from '@/components/ui/Toast';
import { cn } from '@/utils/cn';
import { hiddenNav, navGroups, type NavTint } from './nav';

export const useCommandPalette = create<{ open: boolean; setOpen: (open: boolean) => void }>((set) => ({
  open: false,
  setOpen: (open) => set({ open }),
}));

interface Command {
  id: string;
  section: '页面' | '操作';
  label: string;
  hint?: string;
  keywords?: string;
  icon: ReactNode;
  tint?: NavTint | 'red';
  shortcut?: string[];
  run: () => void;
}

/** 子序列模糊匹配, 返回命中位置用于高亮; 未命中返回 null */
export function fuzzyMatch(text: string, query: string): number[] | null {
  if (!query) return [];
  const source = text.toLowerCase();
  const needle = query.toLowerCase();
  const direct = source.indexOf(needle);
  if (direct >= 0) return Array.from({ length: needle.length }, (_, index) => direct + index);
  const hits: number[] = [];
  let cursor = 0;
  for (let index = 0; index < source.length && cursor < needle.length; index++) {
    if (source[index] === needle[cursor]) { hits.push(index); cursor++; }
  }
  return cursor === needle.length ? hits : null;
}

function Highlight({ text, hits }: { text: string; hits: number[] }) {
  if (!hits.length) return <>{text}</>;
  const marked = new Set(hits);
  return <>{Array.from(text).map((char, index) => marked.has(index) ? <mark key={index} className="bg-transparent font-semibold text-accent">{char}</mark> : char)}</>;
}

/** Ctrl/⌘+K 唤起的全局命令面板, Ctrl/⌘+B 切换侧栏 */
export function CommandPalette() {
  const { open, setOpen } = useCommandPalette();
  const navigate = useNavigate();
  const toggleSidebar = usePrefsStore((state) => state.toggleSidebar);
  const isRunning = useBackendStore((state) => state.isRunning);
  const busy = useBackendStore((state) => state.operation !== null);
  const [query, setQuery] = useState('');
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (!(event.ctrlKey || event.metaKey) || event.altKey || event.shiftKey) return;
      const key = event.key.toLowerCase();
      if (key === 'k') { event.preventDefault(); setOpen(!useCommandPalette.getState().open); }
      if (key === 'b') { event.preventDefault(); toggleSidebar(); }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [setOpen, toggleSidebar]);

  useEffect(() => {
    if (!open) return;
    setQuery('');
    setActive(0);
    const restore = document.activeElement as HTMLElement | null;
    requestAnimationFrame(() => inputRef.current?.focus());
    return () => restore?.focus?.();
  }, [open]);

  const commands = useMemo<Command[]>(() => {
    const pages: Command[] = [...navGroups, ...hiddenNav].flatMap((group) => group.items.map((item) => ({
      id: item.path, section: '页面' as const, label: item.label, hint: group.label, keywords: item.keywords,
      icon: <item.icon className="h-4 w-4" />, tint: group.tint, run: () => navigate(item.path),
    })));
    const backend = async (action: 'start' | 'stop' | 'restart') => {
      const result = await useBackendStore.getState().controlBackend(action);
      toast(result.ok ? 'success' : 'error', result.ok ? { start: '后端已启动', stop: '后端已停止', restart: '后端已重启' }[action] : result.error || '操作未成功');
    };
    const actions: Command[] = [
      { id: 'chat', section: '操作', label: '新建对话', keywords: 'chat new xjdh', icon: <MessageSquarePlus className="h-4 w-4" />, tint: 'accent', run: () => navigate('/chat') },
      ...(busy ? [] : isRunning ? [
        { id: 'restart', section: '操作' as const, label: '重启后端', keywords: 'restart cqhd', icon: <RefreshCw className="h-4 w-4" />, tint: 'orange' as const, run: () => void backend('restart') },
        { id: 'stop', section: '操作' as const, label: '停止后端', keywords: 'stop tzhd', icon: <Square className="h-4 w-4" />, tint: 'red' as const, run: () => void backend('stop') },
      ] : [
        { id: 'start', section: '操作' as const, label: '启动后端', keywords: 'start qdhd', icon: <Play className="h-4 w-4" />, tint: 'green' as const, run: () => void backend('start') },
      ]),
      { id: 'theme', section: '操作', label: '切换主题', keywords: 'theme dark light qhzt', icon: <Moon className="h-4 w-4" />, run: () => switchTheme() },
      { id: 'sidebar', section: '操作', label: '展开 / 收起侧栏', keywords: 'sidebar cl', icon: <PanelLeft className="h-4 w-4" />, shortcut: ['Ctrl', 'B'], run: toggleSidebar },
    ];
    return [...pages, ...actions];
  }, [navigate, isRunning, busy, toggleSidebar]);

  const results = useMemo(() => {
    const needle = query.trim();
    return commands.flatMap((command) => {
      const hits = fuzzyMatch(command.label, needle);
      if (hits) return [{ command, hits }];
      return needle && fuzzyMatch(command.keywords ?? '', needle) ? [{ command, hits: [] }] : [];
    });
  }, [commands, query]);

  useEffect(() => setActive(0), [query]);
  useEffect(() => {
    listRef.current?.querySelector(`[data-index="${active}"]`)?.scrollIntoView({ block: 'nearest' });
  }, [active]);

  if (!open) return null;

  const run = (command: Command) => { setOpen(false); requestAnimationFrame(command.run); };
  const onKeyDown = (event: React.KeyboardEvent) => {
    if (event.key === 'Escape') { event.preventDefault(); setOpen(false); }
    if (event.key === 'ArrowDown') { event.preventDefault(); setActive((value) => Math.min(results.length - 1, value + 1)); }
    if (event.key === 'ArrowUp') { event.preventDefault(); setActive((value) => Math.max(0, value - 1)); }
    if (event.key === 'Enter' && results[active]) { event.preventDefault(); run(results[active].command); }
  };

  let lastSection = '';

  return createPortal(
    <div className="fixed inset-0 z-[160] flex items-start justify-center px-4 pt-[12vh]">
      <div className="glass-overlay" onClick={() => setOpen(false)} aria-hidden />
      <div
        role="dialog"
        aria-modal="true"
        aria-label="命令面板"
        className="surface surface-strong relative w-full max-w-[600px] overflow-hidden !rounded-[20px] !shadow-[var(--shadow-3)]"
        style={{ animation: 'dialog-in 380ms var(--ease-out) both' }}
        onKeyDown={onKeyDown}
      >
        <div className="flex items-center gap-3 border-b border-glass-border px-4">
          <Search className="h-[18px] w-[18px] shrink-0 text-text-tertiary" />
          <input
            ref={inputRef}
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="输入页面名称或操作, 支持拼音首字母…"
            role="combobox"
            aria-label="搜索页面或操作"
            aria-expanded="true"
            aria-controls="command-palette-list"
            aria-activedescendant={results[active] ? `command-${results[active].command.id}` : undefined}
            className="h-14 flex-1 bg-transparent text-[15px] text-text-primary outline-none placeholder:text-text-disabled"
          />
          <kbd>Esc</kbd>
        </div>

        <div ref={listRef} id="command-palette-list" role="listbox" className="custom-scrollbar max-h-[min(56vh,440px)] overflow-y-auto p-2">
          {results.length === 0 && <div className="py-12 text-center text-sm text-text-tertiary">没有找到 “{query}” 相关的结果</div>}
          {results.map(({ command, hits }, index) => {
            const header = command.section !== lastSection ? command.section : null;
            lastSection = command.section;
            return (
              <div key={command.id}>
                {header && <div className="px-3 pb-1.5 pt-3 text-[11px] font-semibold tracking-[0.08em] text-text-disabled first:pt-1">{header}</div>}
                <div
                  id={`command-${command.id}`}
                  role="option"
                  aria-selected={index === active}
                  data-index={index}
                  onPointerMove={() => setActive(index)}
                  onClick={() => run(command)}
                  className={cn(
                    'relative flex h-11 cursor-pointer items-center gap-3 rounded-xl px-3 text-sm transition-colors duration-100',
                    command.tint && `tint-${command.tint}`,
                    index === active ? 'bg-glass-hover text-text-primary' : 'text-text-secondary',
                  )}
                >
                  {index === active && <span className="absolute inset-0 rounded-[inherit] border border-[rgb(var(--tint)/0.2)] bg-[linear-gradient(90deg,rgb(var(--tint)/0.12),transparent)]" aria-hidden />}
                  <span className="relative grid h-7 w-7 place-items-center rounded-lg bg-[rgb(var(--tint)/0.14)] text-[var(--tint-fg)]">{command.icon}</span>
                  <span className="relative flex-1 truncate"><Highlight text={command.label} hits={hits} /></span>
                  {command.hint && <span className="relative text-xs text-text-disabled">{command.hint}</span>}
                  {command.shortcut && <span className="relative flex gap-0.5">{command.shortcut.map((key) => <kbd key={key}>{key}</kbd>)}</span>}
                  {index === active && <CornerDownLeft className="relative h-3.5 w-3.5 text-text-tertiary" />}
                </div>
              </div>
            );
          })}
        </div>

        <div className="flex items-center gap-4 border-t border-glass-border px-4 py-2.5 text-[11px] text-text-disabled">
          <span className="flex items-center gap-1.5"><kbd>↑</kbd><kbd>↓</kbd>选择</span>
          <span className="flex items-center gap-1.5"><kbd>↵</kbd>执行</span>
          <span className="ml-auto flex items-center gap-1.5"><kbd>Ctrl</kbd><kbd>K</kbd>随时唤起</span>
        </div>
      </div>
    </div>,
    document.body,
  );
}
