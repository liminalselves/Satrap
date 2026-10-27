/**
 * 全局玻璃反光引擎
 *
 * 不需要每个组件注册: 自动追踪 DOM 中带 .surface / .tile / .ctl / .nav-item 的可见元素,
 * 单个 pointermove + rAF 写入 --mx / --my 与 data-near; 旧组件仍由 useGlassReflect 驱动.
 * 元素矩形按帧缓存, 滚动或尺寸变化时失效.
 */

const SELECTOR = '.surface, .tile, .ctl, .nav-item';
const NEAR_RANGE = 120;

type Tracked = { el: HTMLElement; rect: DOMRect | null; near: boolean };

export function startReflectEngine(root: HTMLElement = document.body) {
  const tracked = new Map<HTMLElement, Tracked>();
  const visible = new Set<Tracked>();
  let px = -9999;
  let py = -9999;
  let frame = 0;
  let rectsDirty = true;

  const io = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      const item = tracked.get(entry.target as HTMLElement);
      if (!item) continue;
      if (entry.isIntersecting) visible.add(item);
      else {
        visible.delete(item);
        reset(item);
      }
    }
  });

  const add = (el: HTMLElement) => {
    if (tracked.has(el)) return;
    const item = { el, rect: null, near: false };
    tracked.set(el, item);
    io.observe(el);
  };
  const remove = (el: HTMLElement) => {
    const item = tracked.get(el);
    if (!item) return;
    io.unobserve(el);
    visible.delete(item);
    tracked.delete(el);
  };
  const scan = (node: Node, fn: (el: HTMLElement) => void) => {
    if (!(node instanceof HTMLElement)) return;
    if (node.matches(SELECTOR)) fn(node);
    node.querySelectorAll<HTMLElement>(SELECTOR).forEach(fn);
  };

  const mo = new MutationObserver((records) => {
    for (const record of records) {
      record.addedNodes.forEach((n) => scan(n, add));
      record.removedNodes.forEach((n) => scan(n, remove));
    }
    rectsDirty = true;
  });

  function reset(item: Tracked) {
    if (!item.near) return;
    item.near = false;
    item.el.removeAttribute('data-near');
    item.el.style.removeProperty('--mx');
    item.el.style.removeProperty('--my');
  }

  function tick() {
    frame = 0;
    for (const item of visible) {
      if (rectsDirty || !item.rect) item.rect = item.el.getBoundingClientRect();
      const r = item.rect;
      const dx = Math.max(r.left - px, 0, px - r.right);
      const dy = Math.max(r.top - py, 0, py - r.bottom);
      const near = dx < NEAR_RANGE && dy < NEAR_RANGE;
      if (near) {
        item.el.style.setProperty('--mx', `${px - r.left}px`);
        item.el.style.setProperty('--my', `${py - r.top}px`);
        if (!item.near) {
          item.near = true;
          item.el.setAttribute('data-near', '1');
        }
      } else reset(item);
    }
    rectsDirty = false;
    document.documentElement.style.setProperty('--gx', `${px}px`);
    document.documentElement.style.setProperty('--gy', `${py}px`);
  }

  const schedule = () => { if (!frame) frame = requestAnimationFrame(tick); };
  const onMove = (e: PointerEvent) => {
    if (e.pointerType === 'touch') return;
    px = e.clientX;
    py = e.clientY;
    schedule();
  };
  const onLeave = () => { px = py = -9999; schedule(); };
  const invalidate = () => { rectsDirty = true; schedule(); };

  scan(root, add);
  mo.observe(root, { childList: true, subtree: true });
  window.addEventListener('pointermove', onMove, { passive: true });
  document.addEventListener('pointerleave', onLeave);
  window.addEventListener('scroll', invalidate, { passive: true, capture: true });
  window.addEventListener('resize', invalidate);
  // 过渡动画期间元素会移动, 低频刷新矩形
  const timer = window.setInterval(() => { rectsDirty = true; }, 250);

  return () => {
    mo.disconnect();
    io.disconnect();
    cancelAnimationFrame(frame);
    clearInterval(timer);
    window.removeEventListener('pointermove', onMove);
    document.removeEventListener('pointerleave', onLeave);
    window.removeEventListener('scroll', invalidate, { capture: true });
    window.removeEventListener('resize', invalidate);
  };
}
