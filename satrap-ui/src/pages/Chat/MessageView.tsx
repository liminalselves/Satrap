import { lazy, memo, Suspense, useMemo, useState, type CSSProperties, type ReactNode } from 'react';
import {
  Bot,
  Brain,
  CheckCircle2,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Gauge,
  GitFork,
  Loader2,
  RotateCcw,
  Wrench,
  XCircle,
} from 'lucide-react';
import type { ToolCall } from '@/api/chat';
import { CopyButton } from '@/components/ui/CopyButton';
import { Logo } from '@/components/common/Logo';
import { cn } from '@/utils/cn';
import { MediaAttachment } from './MediaAttachment';
import { OutputFiles } from './OutputFiles';
import type { LocalMessageSegment } from './streaming';
import type { ChatMessage } from './types';

const LazyMarkdownContent = lazy(() => import('./MarkdownContent'));

const MarkdownContent = memo(function MarkdownContent({ content }: { content: string }) {
  return (
    <Suspense fallback={<div className="markdown-body whitespace-pre-wrap">{content}</div>}>
      <LazyMarkdownContent content={content} />
    </Suspense>
  );
});

/** 连续的工具调用合并成一行, index 保留组内最后一段的原始下标 */
type SegmentGroup =
  | { type: 'thinking' | 'content'; index: number; content: string }
  | { type: 'tools'; index: number; tools: ToolCall[] };

function groupSegments(segments: LocalMessageSegment[]): SegmentGroup[] {
  const groups: SegmentGroup[] = [];
  segments.forEach((segment, index) => {
    const last = groups[groups.length - 1];
    if (segment.type === 'tool') {
      if (last?.type === 'tools') { last.tools.push(segment.tool); last.index = index; }
      else groups.push({ type: 'tools', index, tools: [segment.tool] });
    } else {
      groups.push({ type: segment.type, index, content: segment.content });
    }
  });
  return groups;
}

// 工具调用状态图标
function ToolStatusIcon({ success }: { success: boolean | null }) {
  if (success === null) return <Loader2 className="h-3 w-3 animate-spin text-orange" />;
  if (success) return <CheckCircle2 className="h-3 w-3 text-green" />;
  return <XCircle className="h-3 w-3 text-red" />;
}

function ToolChipRow({ tools }: { tools: ToolCall[] }) {
  return (
    <div className="flex w-full max-w-full flex-wrap gap-1.5">
      {tools.map((tool, index) => (
        <div
          key={tool.call_id || `${tool.seq}-${index}`}
          className="tint-teal inline-flex max-w-full items-center gap-2 rounded-full border border-[rgb(var(--tint)/0.2)] bg-[rgb(var(--tint)/0.08)] py-1 pl-1.5 pr-2.5 text-xs"
        >
          <span className="grid h-5 w-5 flex-none place-items-center rounded-full bg-[rgb(var(--tint)/0.16)] text-tint"><Wrench className="h-3 w-3" /></span>
          <span className="truncate font-mono font-medium text-fg">{tool.name}</span>
          <ToolStatusIcon success={tool.success} />
        </div>
      ))}
    </div>
  );
}

// 思考流折叠块
// 胶囊按钮与展开正文分离: 正文不套外框, 左侧竖线与按钮内的图标对齐
const ThinkingBlock = memo(function ThinkingBlock({ thinking, live }: { thinking: string; live?: boolean }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="chat-thinking tint-purple w-full">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
        className="flex items-center gap-2 rounded-full border border-[rgb(var(--tint)/0.2)] bg-[rgb(var(--tint)/0.08)] py-1 pl-2 pr-2.5 text-xs text-tint transition hover:bg-[rgb(var(--tint)/0.14)]"
      >
        <Brain className={cn('h-3.5 w-3.5', live && 'animate-pulse')} />
        {live ? '正在思考…' : '思考过程'}
        <ChevronDown className={cn('h-3 w-3 transition-transform duration-300', open && 'rotate-180')} />
      </button>
      <div
        className={cn('grid transition-all duration-500 [transition-timing-function:var(--ease-out)]', open ? 'mt-2 grid-rows-[1fr] opacity-100' : 'grid-rows-[0fr] opacity-0')}
        aria-hidden={!open}
      >
        <div className="overflow-hidden">
          {/* 竖线中线对齐按钮图标中线: 边框 1px + pl-2 + 图标半宽 7px */}
          <p className="ml-[15px] whitespace-pre-wrap break-words border-l-2 border-[rgb(var(--tint)/0.35)] py-0.5 pl-3 text-[13px] leading-6 text-fg-3">{thinking}</p>
        </div>
      </div>
    </div>
  );
});

const CONTEXT_STRATEGY_LABELS: Record<string, string> = {
  sliding: '滑动窗口',
  mid_truncate: '中间截取',
  summarize: '总结压缩',
};

const TOKEN_SOURCE_LABELS: Record<string, string> = {
  tokenizer: '本地 tokenizer',
  experience: '本地经验估算',
  api_calibrated: 'API 校准估算',
};

function formatTokenCount(value: number | null | undefined): string {
  return typeof value === 'number' ? value.toLocaleString() : '未知';
}

function formatTokenCountWithUnit(value: number | null | undefined): string {
  return typeof value === 'number' ? `${value.toLocaleString()} Token` : '未知';
}

function ContextStatsPanel({ stats }: { stats: NonNullable<ChatMessage['contextStats']> }) {
  const last = stats.last_request;
  const displayInput = last.api_input_tokens ?? last.effective_input_tokens;
  return (
    <details className="surface w-full max-w-xl px-3 py-2 text-xs">
      <summary className="flex cursor-pointer select-none items-center gap-1.5 text-fg-3">
        <Gauge className="h-3.5 w-3.5" />
        <span className="num">上下文 {formatTokenCount(displayInput)} Token</span>
        <span>·</span>
        <span>{CONTEXT_STRATEGY_LABELS[last.strategy] ?? last.strategy}</span>
        {last.compressed && <span className="text-orange">· 已压缩</span>}
      </summary>
      <div className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1.5 text-fg-2 sm:grid-cols-3">
        <span>API 输入: {formatTokenCount(last.api_input_tokens)}</span>
        <span>API 输出: {formatTokenCount(last.api_output_tokens)}</span>
        <span>本轮总量: {formatTokenCount(stats.total_api_tokens)}</span>
        <span>末次缓存命中: {formatTokenCountWithUnit(last.api_cached_tokens)}</span>
        <span>本轮累计缓存命中: {formatTokenCountWithUnit(stats.total_api_cached_tokens)}</span>
        <span>模型请求: {stats.request_count}</span>
        <span>完整历史: {last.original_turns} 轮</span>
        <span>实际发送: {last.prepared_turns} 轮</span>
        <span>发送前计数: {TOKEN_SOURCE_LABELS[last.preflight_token_source] ?? last.preflight_token_source}</span>
        <span>发送前估算: {formatTokenCount(last.effective_input_tokens)}</span>
        <span>历史预算: {formatTokenCount(last.history_budget)}</span>
      </div>
    </details>
  );
}

export function TypingDots() {
  return (
    <span className="inline-flex items-center gap-1 py-2" aria-label="正在生成">
      {[0, 1, 2].map((index) => (
        <span
          key={index}
          className="h-1.5 w-1.5 rounded-full bg-accent"
          style={{ animation: `typing-dot 1.2s ${index * 0.15}s ease-in-out infinite` }}
        />
      ))}
    </span>
  );
}

function ActionButton({ title, onClick, disabled, children }: {
  title: string;
  onClick: () => void;
  disabled?: boolean;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      title={title}
      aria-label={title}
      onClick={onClick}
      disabled={disabled}
      className="rounded-md p-1 text-fg-3 transition-colors hover:bg-glass-hover hover:text-accent disabled:opacity-30 disabled:hover:bg-transparent disabled:hover:text-fg-3"
    >
      {children}
    </button>
  );
}

// 消息气泡: 用户右侧强调气泡 / 助手左侧头像与正文; 保留 data-message-id 供滚动锚点使用
export const MessageBubble = memo(function MessageBubble({
  message,
  conversationId,
  isLast,
  onRetry,
  onFork,
  onSelectVariant,
}: {
  message: ChatMessage;
  conversationId: string;
  isLast?: boolean;
  onRetry?: () => void;
  onFork?: (turnIndex: number) => void;
  onSelectVariant?: (turnIndex: number, variantIndex: number) => void;
}) {
  const isUser = message.role === 'user';
  const variants = useMemo(
    () => [...(message.variants ?? [])].sort(
      (left, right) => left.variantIndex - right.variantIndex,
    ),
    [message.variants],
  );
  const variantOrder = message.variantIndices ?? variants.map((variant) => variant.variantIndex);
  const activeVariantPosition = Math.max(
    0,
    variantOrder.indexOf(message.activeVariant ?? 0),
  );

  const attachments = Boolean(message.attachments?.length);
  const segmented = !isUser && Boolean(message.segments?.length);
  const hasContent = Boolean(message.content) || Boolean(message.streaming);

  const actions = !message.streaming && (
    <div className={cn(
      'chat-message-actions flex items-center gap-0.5 opacity-0 transition-opacity focus-within:opacity-100 group-hover:opacity-100',
      isUser && 'justify-end',
    )}>
      {message.content && (
        <CopyButton text={message.content} title="复制消息" className="p-1.5" />
      )}
      {onFork && message.turnIndex !== undefined && (
        <ActionButton title="从这里分支" onClick={() => onFork(message.turnIndex!)}>
          <GitFork className="h-3.5 w-3.5" />
        </ActionButton>
      )}
      {!isUser && isLast && onRetry && (
        <ActionButton title="重试" onClick={onRetry}>
          <RotateCcw className="h-3.5 w-3.5" />
        </ActionButton>
      )}
      {!isUser && isLast && onSelectVariant && message.turnIndex !== undefined && variantOrder.length > 1 && (
        <span className="ml-1 flex items-center gap-0.5 rounded-lg border border-line bg-glass px-0.5">
          <ActionButton
            title="上一个回复"
            onClick={() => onSelectVariant(message.turnIndex!, variantOrder[Math.max(0, activeVariantPosition - 1)])}
            disabled={activeVariantPosition <= 0}
          >
            <ChevronLeft className="h-3.5 w-3.5" />
          </ActionButton>
          <span className="num px-0.5 text-[11px] text-fg-3">{activeVariantPosition + 1} / {variantOrder.length}</span>
          <ActionButton
            title="下一个回复"
            onClick={() => onSelectVariant(message.turnIndex!, variantOrder[Math.min(variantOrder.length - 1, activeVariantPosition + 1)])}
            disabled={activeVariantPosition >= variantOrder.length - 1}
          >
            <ChevronRight className="h-3.5 w-3.5" />
          </ActionButton>
        </span>
      )}
    </div>
  );

  if (isUser) {
    return (
      <div data-message-id={message.id} className="chat-message-item group flex justify-end">
        <div className="flex min-w-0 max-w-[85%] flex-col items-end gap-1.5 sm:max-w-[78%]">
          {attachments && (
            <div className="flex flex-wrap justify-end gap-2">
              {message.attachments!.map((att) => (
                <MediaAttachment key={att.url} attachment={att} conversationId={conversationId} />
              ))}
            </div>
          )}
          {hasContent && (
            <div className="rounded-[20px] rounded-br-md border border-[rgb(var(--rgb-accent)/0.35)] bg-[linear-gradient(135deg,rgb(var(--rgb-accent)/0.88),rgb(var(--rgb-accent)/0.66))] px-4 py-2.5 text-[14.5px] leading-7 text-white whitespace-pre-wrap break-words shadow-[0_10px_30px_-12px_rgb(var(--rgb-accent)/0.8),inset_0_1px_0_rgba(255,255,255,0.25)]">
              {message.content}
              {message.streaming && <span className="chat-caret" aria-hidden />}
            </div>
          )}
          {actions}
        </div>
      </div>
    );
  }

  return (
    <div data-message-id={message.id} className="chat-message-item group flex gap-3.5">
      <div className="relative mt-0.5 h-8 w-8 flex-none">
        <Logo className="h-8 w-8" />
        {message.streaming && <span className="chat-avatar-halo absolute -inset-1 rounded-full border border-[rgb(var(--rgb-accent)/0.5)]" />}
      </div>
      <div className="min-w-0 flex-1 space-y-2 pt-0.5">
        {attachments && (
          <div className="flex flex-wrap gap-2">
            {message.attachments!.map((att) => (
              <MediaAttachment key={att.url} attachment={att} conversationId={conversationId} />
            ))}
          </div>
        )}

        {segmented ? (
          <div className="flex min-w-0 flex-col items-start gap-2">
            {groupSegments(message.segments!).map((group) => {
              if (group.type === 'thinking') {
                return <ThinkingBlock key={`thinking-${group.index}`} thinking={group.content} live={message.streaming && group.index === message.segments!.length - 1} />;
              }
              if (group.type === 'tools') {
                return <ToolChipRow key={`tools-${group.index}`} tools={group.tools} />;
              }
              const isLastGroup = group.index === message.segments!.length - 1;
              return (
                <div key={`content-${group.index}`} className="min-w-0 max-w-full text-[14.5px] leading-7 text-fg break-words">
                  <MarkdownContent content={group.content} />
                  {message.streaming && isLastGroup && <span className="chat-caret" aria-hidden />}
                </div>
              );
            })}
            {message.streaming && !message.content && !message.thinking && <TypingDots />}
          </div>
        ) : (
          <div className="flex min-w-0 flex-col items-start gap-2">
            {message.thinking && (
              <ThinkingBlock thinking={message.thinking} live={message.streaming && !message.content} />
            )}
            {message.toolCalls && message.toolCalls.length > 0 && (
              <ToolChipRow tools={message.toolCalls} />
            )}
            {hasContent && (
              <div className="min-w-0 max-w-full text-[14.5px] leading-7 text-fg break-words">
                <MarkdownContent content={message.content} />
                {message.streaming && <span className="chat-caret" aria-hidden />}
              </div>
            )}
            {message.streaming && !message.content && !message.thinking && <TypingDots />}
            {!message.content && !message.thinking && !message.streaming && !message.toolCalls?.length && (
              <div className="flex items-center gap-2 text-xs text-fg-4"><Bot className="h-3.5 w-3.5" />无内容</div>
            )}
          </div>
        )}

        {Boolean(message.outputFiles?.length) && <OutputFiles conversationId={conversationId} files={message.outputFiles ?? []} />}

        {message.contextStats && <ContextStatsPanel stats={message.contextStats} />}

        {!message.streaming && (
          <div className="truncate text-[11px] text-fg-4" data-response-model={message.responseMetadata?.model ?? 'unknown'}>
            模型: {message.responseMetadata?.model ?? '未知'}
            {message.responseMetadata?.model_id ? ` / ${message.responseMetadata.model_id}` : ''}
          </div>
        )}

        {actions}
      </div>
    </div>
  );
});

const SUGGESTIONS: { title: string; desc: string; tint: 'accent' | 'orange' | 'purple' | 'teal' }[] = [
  { title: '解释一段代码', desc: '粘贴代码, 逐行说明它做了什么', tint: 'accent' },
  { title: '排查一个报错', desc: '附上堆栈, 定位可能原因', tint: 'orange' },
  { title: '起草技术方案', desc: '描述需求, 生成结构化方案', tint: 'purple' },
  { title: '总结知识库文档', desc: '基于 RAG 知识库回答问题', tint: 'teal' },
];

// 空会话欢迎页 (对齐预览)
export function EmptyWelcome({ onPromptClick }: { onPromptClick: (text: string) => void }) {
  return (
    <div className="mx-auto flex h-full max-w-2xl flex-col items-center justify-center px-4 pb-8 text-center md:px-6">
      <div className="relative mb-6 animate-rise">
        <div aria-hidden className="absolute -inset-10 rounded-full bg-[radial-gradient(circle,rgb(var(--rgb-accent)/0.35),rgb(var(--rgb-purple)/0.15)_45%,transparent_70%)] blur-xl" />
        <Logo className="relative h-20 w-20" />
      </div>
      <h1 className="display animate-rise text-[26px] font-semibold text-fg sm:text-[28px]" style={{ animationDelay: '60ms' }}>下午好, 今天想聊点什么?</h1>
      <p className="mt-2 animate-rise text-sm text-fg-3" style={{ animationDelay: '120ms' }}>输入你的问题, 或从下方建议开始</p>
      <div className="stagger mt-8 grid w-full grid-cols-1 gap-3 sm:grid-cols-2">
        {SUGGESTIONS.map((suggestion, index) => (
          <button
            key={suggestion.title}
            type="button"
            onClick={() => onPromptClick(suggestion.title)}
            className={cn('tile p-4 text-left', `tint-${suggestion.tint}`)}
            style={{ '--i': index + 3 } as CSSProperties}
          >
            <div className="text-sm font-medium text-fg">{suggestion.title}</div>
            <div className="mt-1 text-xs text-fg-3">{suggestion.desc}</div>
          </button>
        ))}
      </div>
    </div>
  );
}
