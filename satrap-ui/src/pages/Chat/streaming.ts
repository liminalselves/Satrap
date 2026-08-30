import type { ToolCall } from '@/api/chat';

export type LocalMessageSegment =
  | { type: 'thinking'; content: string }
  | { type: 'tool'; tool: ToolCall }
  | { type: 'content'; content: string };

export interface StreamingDelta {
  type: 'thinking' | 'content';
  delta: string;
}

export interface StreamingMessage {
  content: string;
  thinking?: string;
  segments?: LocalMessageSegment[];
}

/** 合并连续的同类流式增量, 限制单帧内的临时对象数量 */
export function enqueueStreamingDelta(queue: StreamingDelta[], incoming: StreamingDelta): void {
  if (!incoming.delta) return;
  const last = queue[queue.length - 1];
  if (last?.type === incoming.type) {
    last.delta += incoming.delta;
    return;
  }
  queue.push({ ...incoming });
}

/** 按到达顺序把一帧内积累的增量应用到消息 */
export function applyStreamingDeltas<T extends StreamingMessage>(
  message: T,
  deltas: readonly StreamingDelta[],
): T {
  if (deltas.length === 0) return message;
  const segments = [...(message.segments ?? [])];
  let content = message.content;
  let thinking = message.thinking;

  for (const item of deltas) {
    const last = segments[segments.length - 1];
    if (last?.type === item.type) {
      segments[segments.length - 1] = { ...last, content: last.content + item.delta };
    } else {
      segments.push({ type: item.type, content: item.delta });
    }
    if (item.type === 'content') {
      content += item.delta;
    } else {
      thinking = (thinking ?? '') + item.delta;
    }
  }

  return {
    ...message,
    content,
    ...(thinking !== undefined ? { thinking } : {}),
    segments,
  };
}
