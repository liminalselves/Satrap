import { useEffect, useRef, useState, type KeyboardEvent, type PointerEvent } from 'react';
import { cn } from '@/utils/cn';

export interface ThinkOption {
  value: string;
  label: string;
}

/** 仪表盘图标: 弧线按档位填充, 指针随档位旋转 */
function Gauge({ ratio }: { ratio: number }) {
  const radius = 8;
  const arc = Math.PI * radius * 1.5;   // 270° 弧
  const angle = -135 + ratio * 270;
  return (
    <svg width="18" height="18" viewBox="0 0 22 22" aria-hidden>
      <g transform="rotate(135 11 11)">
        <circle cx="11" cy="11" r={radius} fill="none" stroke="var(--glass-border-strong)" strokeWidth="2.4" strokeLinecap="round" strokeDasharray={`${arc} 100`} />
        <circle cx="11" cy="11" r={radius} fill="none" stroke="rgb(var(--rgb-accent))" strokeWidth="2.4" strokeLinecap="round"
          strokeDasharray={`${Math.max(0.001, arc * ratio)} 100`} style={{ transition: 'stroke-dasharray 500ms var(--ease-out)' }} />
      </g>
      <g style={{ transform: `rotate(${angle}deg)`, transformOrigin: '11px 11px', transition: 'transform 500ms var(--ease-spring)' }}>
        <line x1="11" y1="11" x2="11" y2="6.2" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
      </g>
      <circle cx="11" cy="11" r="2.2" fill="currentColor" />
    </svg>
  );
}

/** 分档滑条: 胶囊轨道 + 档位圆点 + 白色旋钮, 可拖动, 可点击, 可键盘调节 */
function LevelSlider({ index, count, label, onChange }: { index: number; count: number; label: string; onChange: (index: number) => void }) {
  const trackRef = useRef<HTMLDivElement>(null);
  const [dragging, setDragging] = useState(false);
  const steps = Math.max(1, count - 1);
  const ratio = index / steps;

  const pick = (clientX: number) => {
    const rect = trackRef.current?.getBoundingClientRect();
    if (!rect) return;
    const position = Math.min(1, Math.max(0, (clientX - rect.left - 18) / (rect.width - 36)));
    onChange(Math.round(position * steps));
  };
  const onKey = (event: KeyboardEvent) => {
    const next = event.key === 'ArrowRight' || event.key === 'ArrowUp' ? index + 1
      : event.key === 'ArrowLeft' || event.key === 'ArrowDown' ? index - 1
        : event.key === 'Home' ? 0 : event.key === 'End' ? steps : null;
    if (next === null) return;
    event.preventDefault();
    onChange(Math.min(steps, Math.max(0, next)));
  };

  return (
    <div
      ref={trackRef}
      role="slider"
      tabIndex={0}
      aria-label="思考强度"
      aria-valuemin={0}
      aria-valuemax={steps}
      aria-valuenow={index}
      aria-valuetext={label}
      onPointerDown={(event: PointerEvent) => { event.currentTarget.setPointerCapture(event.pointerId); setDragging(true); pick(event.clientX); }}
      onPointerMove={(event: PointerEvent) => { if (dragging) pick(event.clientX); }}
      onPointerUp={() => setDragging(false)}
      onKeyDown={onKey}
      className="relative h-11 cursor-pointer touch-none select-none rounded-full border border-line bg-glass p-[3px] shadow-[inset_0_1px_3px_rgba(0,0,0,0.12)]"
    >
      <div
        aria-hidden
        className="absolute bottom-[3px] left-[3px] top-[3px] rounded-full bg-[linear-gradient(90deg,rgb(var(--rgb-accent)/0.85),rgb(var(--rgb-accent)))] shadow-[0_6px_18px_-6px_rgb(var(--rgb-accent)/0.8)]"
        style={{ width: `calc(${ratio} * (100% - 42px) + 36px)`, transition: dragging ? 'none' : 'width 420ms var(--ease-spring)' }}
      />
      {Array.from({ length: count }, (_, step) => (
        <span
          key={step}
          aria-hidden
          className={cn('absolute top-1/2 h-2 w-2 -translate-x-1/2 -translate-y-1/2 rounded-full transition-colors duration-300', step <= index ? 'bg-white/45' : 'bg-[var(--glass-border-strong)]')}
          style={{ left: `calc(${step / steps} * (100% - 42px) + 21px)` }}
        />
      ))}
      <span
        aria-hidden
        className={cn('absolute top-[3px] h-[36px] w-[36px] rounded-full bg-white shadow-[0_2px_8px_rgba(0,0,0,0.25),0_0_0_0.5px_rgba(0,0,0,0.06)]', dragging && 'scale-110')}
        style={{ left: `calc(${ratio} * (100% - 42px) + 3px)`, transition: dragging ? 'transform 200ms' : 'left 420ms var(--ease-spring), transform 200ms' }}
      />
    </div>
  );
}

/**
 * 思考强度转轮: 小仪表盘按钮, 点开为分档滑条
 * 档位由当前模型声明, 数量不固定; 只有一档时不渲染
 */
export function ThinkDial({ value, options, onChange, disabled }: {
  value: string; options: ThinkOption[]; onChange: (value: string) => void; disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const found = options.findIndex((option) => option.value === value);
  const index = found < 0 ? 0 : found;
  const current = options[index];
  const ratio = options.length > 1 ? index / (options.length - 1) : 0;
  const off = value === 'off';

  useEffect(() => {
    if (!open) return;
    const close = (event: globalThis.PointerEvent) => { if (!ref.current?.contains(event.target as Node)) setOpen(false); };
    const escape = (event: globalThis.KeyboardEvent) => { if (event.key === 'Escape') setOpen(false); };
    window.addEventListener('pointerdown', close);
    window.addEventListener('keydown', escape);
    return () => { window.removeEventListener('pointerdown', close); window.removeEventListener('keydown', escape); };
  }, [open]);

  if (options.length < 2 || !current) return null;

  return (
    <div ref={ref} className="relative shrink-0 mb-0.5">
      <button
        type="button"
        onClick={() => setOpen((state) => !state)}
        disabled={disabled}
        aria-expanded={open}
        aria-haspopup="dialog"
        aria-label={`思考强度: ${current.label}`}
        title={`思考强度: ${current.label}`}
        className={cn('flex h-8 items-center gap-1.5 rounded-full border px-2 text-xs font-medium transition-all duration-200 disabled:opacity-50',
          off ? 'border-transparent text-fg-3 hover:bg-glass-hover hover:text-fg'
            : 'border-[rgb(var(--rgb-accent)/0.3)] bg-[rgb(var(--rgb-accent)/0.12)] text-accent',
          open && 'bg-glass-hover')}
      >
        <span className={off ? 'text-fg-3' : 'text-fg'}><Gauge ratio={ratio} /></span>
        <span className="hidden sm:inline">{off ? '思考' : current.label}</span>
      </button>

      {open && (
        <div role="dialog" aria-label="思考强度" className="surface surface-strong absolute bottom-[calc(100%+10px)] left-0 z-[60] w-[300px] max-w-[calc(100vw-32px)] !rounded-[22px] p-4 !shadow-[var(--shadow-3)] animate-pop" style={{ transformOrigin: 'bottom left' }}>
          <div className="mb-1 text-center text-[11px] font-medium tracking-[0.1em] text-fg-4">思考强度</div>
          <div className="relative mb-4 h-9 overflow-hidden text-center">
            {options.map((option, step) => (
              <div key={option.value} aria-hidden={step !== index}
                className="absolute inset-0 text-2xl font-semibold text-fg transition-all duration-500 [transition-timing-function:var(--ease-spring)]"
                style={{ transform: `translateY(${(step - index) * 100}%)`, opacity: step === index ? 1 : 0 }}>
                {option.label}
              </div>
            ))}
          </div>
          <LevelSlider index={index} count={options.length} label={current.label} onChange={(step) => onChange(options[step].value)} />
          <div className="mt-2 flex justify-between px-1 text-[11px] text-fg-4">
            {options.map((option, step) => (
              <button key={option.value} type="button" onClick={() => onChange(option.value)}
                className={cn('transition-colors hover:text-fg', step === index && 'font-medium text-accent')}>{option.label}</button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
