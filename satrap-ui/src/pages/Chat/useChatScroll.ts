import { useCallback, useEffect, useLayoutEffect, useRef } from 'react';

/**
 * 消息更新只在用户仍位于底部时跟随, 切换对话与首次载入定位末尾
 * 参数:
 * - selection: 当前对话及服务/入口可见性, 切换时重新定位
 * - messages: 当前消息数组, 保留流式批量更新规则
 * - loaded: 历史是否载入, 首次完成载入时定位末尾
 * 返回:
 * - 消息滚动容器/内容引用与滚动处理函数; 媒体布局变化沿用当前跟随意图
 */
export function useChatScroll(selection: string, messages: unknown, loaded: boolean) {
  const containerRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const follow = useRef(true);
  const anchor = useRef<{ id: string; offset: number }>();
  const previous = useRef({ selection, loaded });
  const keepPosition = useCallback(() => {
    const container = containerRef.current;
    if (!container) return;
    if (follow.current) container.scrollTop = container.scrollHeight;
    else if (anchor.current) {
      const item = contentRef.current?.querySelector<HTMLElement>(`[data-message-id="${CSS.escape(anchor.current.id)}"]`);
      if (item) container.scrollTop += item.getBoundingClientRect().top - container.getBoundingClientRect().top - anchor.current.offset;
    }
  }, []);
  const onScroll = useCallback(() => {
    const container = containerRef.current;
    if (!container) return;
    follow.current = container.scrollHeight - container.clientHeight - container.scrollTop <= 64;
    if (follow.current) { anchor.current = undefined; return; }
    const items = contentRef.current?.querySelectorAll<HTMLElement>('[data-message-id]');
    if (!items?.length) return;
    const top = container.getBoundingClientRect().top;
    const readingLine = top + container.clientHeight / 2; // 以阅读区中央的消息为锚, 避免上方估算高度变化移动当前阅读内容
    let left = 0, right = items.length - 1;
    while (left < right) {
      const middle = Math.floor((left + right) / 2);
      if (items[middle].getBoundingClientRect().bottom < readingLine) left = middle + 1;
      else right = middle;
    }
    const id = items[left].dataset.messageId;
    if (id) anchor.current = { id, offset: items[left].getBoundingClientRect().top - top };
  }, []);
  useLayoutEffect(() => {
    if (previous.current.selection !== selection || !previous.current.loaded && loaded) {
      follow.current = true; anchor.current = undefined;
    }
    previous.current = { selection, loaded };
    keepPosition();
  }, [selection, messages, loaded, keepPosition]);
  useEffect(() => {
    const container = containerRef.current, content = contentRef.current;
    if (!container || !content) return;
    const observer = new ResizeObserver(keepPosition);
    observer.observe(container); observer.observe(content);
    return () => observer.disconnect();
  }, [selection, keepPosition]);
  return { containerRef, contentRef, onScroll };
}
