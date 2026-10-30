import { useEffect, useMemo, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { ChevronRight } from 'lucide-react';
import { cn } from '@/utils/cn';
import { buildOutline, type DocPage, type OutlineItem } from './docModel';

/** 判定线: 标题越过滚动区顶部这个距离即视为当前小节; 需大于标题的 scroll-mt-24 (96px), 否则跳转后的标题恰好落在线下 */
const SPY_OFFSET = 128;

/** 跟随滚动的当前标题; 滚动容器是布局的 <main> */
function useActiveHeading(ids: string[]): string {
  const [active, setActive] = useState(ids[0] ?? '');
  useEffect(() => {
    const main = document.querySelector('main');
    if (!main || !ids.length) return;
    const update = () => {
      const top = main.getBoundingClientRect().top + SPY_OFFSET;
      const atBottom = main.scrollTop + main.clientHeight >= main.scrollHeight - 2;
      let current = ids[0];
      for (const id of ids) {
        const element = document.getElementById(id);
        if (element && element.getBoundingClientRect().top <= top) current = id;
      }
      setActive(atBottom ? ids[ids.length - 1] : current);
    };
    update();
    main.addEventListener('scroll', update, { passive: true });
    return () => main.removeEventListener('scroll', update);
  }, [ids]);
  return active;
}

function OutlineLink({ item, active, depth, onSelect }: { item: OutlineItem; active: boolean; depth: number; onSelect: (id: string) => void }) {
  return (
    <a href={`#${encodeURIComponent(item.id)}`} aria-current={active ? 'location' : undefined}
      onClick={(event) => { event.preventDefault(); onSelect(item.id); }}
      className={cn('block truncate rounded-md py-1.5 pr-2 text-[13px] transition-colors',
        depth === 0 ? 'pl-7' : 'pl-11 text-[12.5px]',
        active ? 'bg-[rgb(var(--rgb-accent)/0.12)] font-medium text-accent' : 'text-fg-2 hover:bg-glass-hover hover:text-fg')}
      title={item.text}>
      {item.text}
    </a>
  );
}

/** 文档大纲: 当前页的二、三级标题, 其余页面作为可展开的同级条目 */
export function DocOutline({ pages, current }: { pages: DocPage[]; current: DocPage }) {
  const navigate = useNavigate();
  const outline = useMemo(() => buildOutline(current.body), [current.body]);
  const ids = useMemo(() => outline.flatMap((item) => [item.id, ...item.children.map((child) => child.id)]), [outline]);
  const active = useActiveHeading(ids);
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  const open = !collapsed[current.slug];

  const select = (id: string) => navigate(`/docs/${current.slug}#${encodeURIComponent(id)}`, { replace: true });

  return (
    <nav aria-label="文档大纲" className="text-fg">
      <div className="border-b border-line px-1 pb-3 text-[15px] font-semibold">大纲</div>
      <ul className="mt-3 space-y-0.5">
        {pages.map((page) => {
          const isCurrent = page.slug === current.slug;
          return (
            <li key={page.slug}>
              {isCurrent ? (
                <button type="button" aria-expanded={open}
                  onClick={() => setCollapsed((state) => ({ ...state, [page.slug]: open }))}
                  className="flex w-full items-center gap-1 rounded-md px-1 py-1.5 text-left text-[13.5px] font-medium text-accent hover:bg-glass-hover">
                  <ChevronRight className={cn('h-3.5 w-3.5 flex-none transition-transform', open && 'rotate-90', !outline.length && 'invisible')} />
                  <span className="truncate" title={page.title}>{page.title}</span>
                </button>
              ) : (
                <Link to={`/docs/${page.slug}`}
                  className="flex items-center gap-1 rounded-md px-1 py-1.5 text-[13.5px] text-fg-2 hover:bg-glass-hover hover:text-fg">
                  <ChevronRight className="h-3.5 w-3.5 flex-none" />
                  <span className="truncate" title={page.title}>{page.title}</span>
                </Link>
              )}
              {isCurrent && open && outline.length > 0 && (
                <ul className="mt-0.5 space-y-0.5">
                  {outline.map((item) => (
                    <li key={item.id}>
                      <OutlineLink item={item} depth={0} active={active === item.id} onSelect={select} />
                      {item.children.length > 0 && (
                        <ul className="space-y-0.5">
                          {item.children.map((child) => (
                            <li key={child.id}><OutlineLink item={child} depth={1} active={active === child.id} onSelect={select} /></li>
                          ))}
                        </ul>
                      )}
                    </li>
                  ))}
                </ul>
              )}
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
