import {
  Archive, Bot, Clock3, Database, Gauge, GitMerge, Layers, MessageSquareText, MessagesSquare, MoonStar,
  Puzzle, Send, SquareDot, StickyNote, Timer, Wrench, Zap, type LucideIcon,
} from 'lucide-react';
import type { Tint } from '@/components/kit';
import type { FlowNodeTypePayload, FlowPortType } from '@/api/types';

/** 端口类型的颜色和线型; 显示名取后端 port_types */
export const PORT_STYLE: Record<FlowPortType, { rgb: string; desc: string; dash?: string }> = {
  messages: { rgb: 'var(--rgb-teal)', desc: '有序档案消息, 带 message_id' },
  state: { rgb: 'var(--rgb-orange)', desc: '状态槽的读引用, 带版本和覆盖位置' },
  context: { rgb: 'var(--rgb-accent)', desc: '已拼装的模型输入' },
  text: { rgb: 'var(--rgb-purple)', desc: 'WF 的输出文本' },
  event: { rgb: 'var(--rgb-pink)', desc: '一次触发, 带触发时的消息, 批次或上游输出', dash: '6 5' },
  condition: { rgb: 'var(--rgb-green)', desc: '在触发时刻求值的真/假', dash: '2 4' },
  handoff: { rgb: 'var(--rgb-orange)', desc: '把交接工具挂到调用方 WF 上', dash: '9 3 2 3' },
};

const ICONS: Record<string, LucideIcon> = {
  source: MessagesSquare, bucket: Database, state: Archive, assemble: Layers, react_workflow: Bot, output: Send,
  wake: Zap, threshold: Timer, tool: Wrench, cond_message: MessageSquareText, cond_time: Clock3, cond_state: SquareDot,
  and: GitMerge, or: GitMerge, not: GitMerge, note: StickyNote,
  'quiet_hours.in_hours': MoonStar, 'llm_budget.within': Gauge,
};

const ROLE_TINT: Record<string, Tint> = {
  source: 'accent', transform: 'teal', workflow: 'purple', state: 'orange', sink: 'green',
  trigger: 'pink', condition: 'green', note: 'orange',
};

export function nodeIcon(def: FlowNodeTypePayload | undefined): LucideIcon {
  if (!def) return Puzzle;
  return ICONS[def.type] ?? (def.role === 'condition' ? SquareDot : def.role === 'workflow' ? Bot : Puzzle);
}

export function nodeTint(def: FlowNodeTypePayload | undefined): Tint {
  if (!def) return 'accent';
  if (def.type === 'assemble') return 'accent';
  if (def.role === 'logic') return def.type === 'or' ? 'purple' : def.type === 'not' ? 'pink' : 'accent';
  return ROLE_TINT[def.role] ?? 'accent';
}
