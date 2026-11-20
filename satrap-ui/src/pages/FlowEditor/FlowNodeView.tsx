import { memo } from 'react';
import { Handle, Position, type NodeProps } from '@xyflow/react';
import { AlertCircle, AlertTriangle, Cpu, HelpCircle, History, Loader2, Puzzle, Wrench } from 'lucide-react';
import type { FlowNodeTypePayload, FlowPortPayload } from '@/api/types';
import { cn } from '@/utils/cn';
import { outputType, parsePluginList, platformWakeText, portSide, resolveConfig, SEND_MODE_READING, toolName, type ConversationKinds, type FlowNode } from './flowModel';
import { nodeIcon, nodeTint, PORT_STYLE } from './portStyle';
import { useFlowUi } from './uiState';

type Side = 'top' | 'bottom' | 'left' | 'right';
const POS: Record<Side, Position> = { top: Position.Top, bottom: Position.Bottom, left: Position.Left, right: Position.Right };

const lineCount = (value: unknown) => String(value ?? '').split('\n').filter((item) => item.trim()).length;

function summary(def: FlowNodeTypePayload, v: Record<string, unknown>, id: string, kinds: ConversationKinds): string[] {
  switch (def.type) {
    case 'source': return [`${kinds.length > 1 ? '群聊或私聊' : kinds[0] === 'private' ? '私聊' : '群聊'}消息档案 · 直连最多 ${v.max_messages} 条`];
    case 'bucket': return [v.include_outbound ? '包含机器人发言' : '仅入站消息'];
    case 'react_workflow': {
      const plugins = parsePluginList(v.plugins);
      return [String(v.model_name || '未选模型') + (v.thinking && v.thinking !== 'default' ? ` · 思考 ${v.thinking}` : ''), plugins.length ? plugins.join(', ') : '无插件'];
    }
    case 'state': return [v.schema === 'sections' ? `${lineCount(v.sections)} 段` : '自由文本', `上限 ${v.max_chars} 字 · /new ${v.on_new === 'clear' ? '清空' : '保留'}`];
    case 'assemble': return [[v.after_coverage && '快照未覆盖消息', Number(v.latest_n) > 0 && `最新 ${v.latest_n} 条`].filter(Boolean).join(' + ') || '只读快照', v.dedupe ? '按消息 ID 去重' : '不去重'];
    case 'output': return [SEND_MODE_READING[String(v.send_mode)] ?? String(v.send_mode)];
    case 'wake': return [v.use_platform_policy ? platformWakeText(kinds) : [v.mention && (kinds.includes('group') ? '@' : '每条'), Number(v.new_messages) > 0 && `${v.new_messages} 条`, lineCount(v.wake_words) > 0 && '唤醒词'].filter(Boolean).join(' / ') || '无条件'];
    case 'threshold': return [[Number(v.messages) > 0 && `${v.messages} 条`, Number(v.chars) > 0 && `${v.chars} 字`, Number(v.idle_seconds) > 0 && `${Math.round(Number(v.idle_seconds) / 60)} 分钟`].filter(Boolean).join(' 或 ') || '无条件'];
    case 'tool': return [toolName(id, v)];
    case 'cond_message': return [v.kind === 'mention' ? '被 @' : v.kind === 'keyword' ? `关键词 ${lineCount(v.keywords)} 个` : `成员 ${lineCount(v.senders)} 人`];
    case 'cond_time': case 'quiet_hours.in_hours': return [`${v.start}–${v.end}`];
    case 'cond_state': return [v.check === 'nonempty' ? '已有内容' : `版本 ≥ ${v.min_version}`];
    case 'llm_budget.within': return [`< ${v.max_calls} 次 / ${v.window_minutes} 分钟`];
    default: {
      const keys = Object.keys(def.schema).slice(0, 2);
      return keys.map((key) => `${def.schema[key].label || key}: ${typeof v[key] === 'boolean' ? (v[key] ? '是' : '否') : String(v[key] ?? '').split('\n').filter(Boolean).join(', ') || '—'}`);
    }
  }
}

function Ports({ def, id, io }: { def: FlowNodeTypePayload; id: string; io: 'in' | 'out' }) {
  const { connecting, nodes, edges, types, portTypes } = useFlowUi();
  const ports = io === 'in' ? def.inputs : def.outputs;
  const bySide = new Map<Side, FlowPortPayload[]>();
  for (const port of ports) {
    const side = portSide(port, io);
    bySide.set(side, [...(bySide.get(side) ?? []), port]);
  }
  const labelled = def.role === 'workflow';
  return (
    <>
      {[...bySide.entries()].flatMap(([side, list]) => list.map((port, index) => {
        const inEdge = io === 'in' && port.types.length > 1 ? edges.find((edge) => edge.target === id && edge.targetHandle === port.name) : undefined;
        const type = io === 'out' ? outputType(id, port.name, nodes, edges, types) ?? port.types[0]
          : inEdge ? outputType(inEdge.source, inEdge.sourceHandle ?? '', nodes, edges, types) ?? port.types[0] : port.types[0];
        const style = PORT_STYLE[type];
        const signal = portTypes[type]?.signal ?? false;
        const ok = !connecting || (io === 'in' && port.types.includes(connecting));
        const pct = `${((index + 1) / (list.length + 1)) * 100}%`;
        const position = side === 'top' || side === 'bottom' ? { left: pct } : { top: pct };
        const showLabel = labelled && (side === 'left' || side === 'right');
        return (
          <Handle
            key={port.name} id={port.name} type={io === 'in' ? 'target' : 'source'} position={POS[side]}
            style={{ ...position, ['--port' as string]: style.rgb }}
            data-port={`${id}.${port.name}`}
            className={cn('flow-handle', signal && 'flow-handle-signal', port.required && 'flow-handle-required',
              connecting && io === 'in' && (ok ? 'flow-handle-ok' : 'flow-handle-dim'))}
            title={`${port.label} · ${io === 'in' ? `接受 ${port.types.map((item) => portTypes[item]?.label ?? item).join(' / ')}` : `输出 ${portTypes[type]?.label ?? type}`}${port.required ? ' · 必需' : ''}${port.multiple ? ' · 可多条' : ''}`}
          >
            {showLabel && <span className={cn('flow-port-label', side === 'left' ? 'flow-port-label-left' : 'flow-port-label-right')}>{port.label}</span>}
          </Handle>
        );
      }))}
    </>
  );
}

function Badges({ id }: { id: string }) {
  const { issues } = useFlowUi();
  const own = issues.filter((issue) => issue.node === id);
  const errors = own.filter((issue) => issue.level === 'error').length;
  const warnings = own.length - errors;
  if (errors) return <span className="flex flex-none items-center gap-1 rounded-md bg-[rgb(var(--rgb-red)/0.16)] px-1.5 py-0.5 text-[11px] text-red" aria-label={`${errors} 个错误`}><AlertCircle className="h-3 w-3" />{errors}</span>;
  if (warnings) return <span className="flex flex-none items-center gap-1 rounded-md bg-[rgb(var(--rgb-orange)/0.16)] px-1.5 py-0.5 text-[11px] text-orange" aria-label={`${warnings} 条提示`}><AlertTriangle className="h-3 w-3" />{warnings}</span>;
  return null;
}

export const FlowNodeView = memo(function FlowNodeView({ id, data, selected }: NodeProps<FlowNode>) {
  const { issues, running, types, defaults, pluginDefaults, analysis, nodes, kinds, traced } = useFlowUi();
  const def = types.get(data.type);
  const hasError = issues.some((issue) => issue.node === id && issue.level === 'error');
  const traceClass = traced && (traced.target === id ? 'flow-trace-target' : traced.nodes.has(id) ? 'flow-trace-on' : 'flow-trace-dim');

  if (!def) {
    return (
      <div className={cn('flow-node surface tint-red w-[220px] rounded-[14px] !p-3', traceClass)} data-flow-node={id}>
        <div className="flex items-center gap-2 text-[13px] font-semibold text-red"><HelpCircle className="h-4 w-4" />{data.title}</div>
        <div className="mt-1 text-[12px] text-fg-3">未知节点类型 {data.type}, 插件可能已卸载</div>
      </div>
    );
  }

  const values = resolveConfig(data, types, defaults, pluginDefaults).values;
  const Icon = nodeIcon(def);
  const isRunning = running.has(id);
  const common = cn('flow-node', selected && 'flow-node-selected', isRunning && 'flow-node-running', hasError && 'flow-node-error', traceClass);

  if (def.role === 'note') {
    return (
      <div className={cn('flow-note', `flow-note-${String(values.color ?? 'orange')}`, selected && 'flow-node-selected', traced && 'flow-trace-dim')} data-flow-node={id}>
        <div className="mb-1 text-[12px] font-semibold">{data.title}</div>
        <div className="whitespace-pre-wrap text-[12px] leading-5 text-fg-2">{String(values.text ?? '') || <span className="text-fg-4">在右侧编辑内容</span>}</div>
      </div>
    );
  }

  if (def.role === 'logic') {
    return (
      <div className={cn(common, 'flow-logic', `flow-logic-${def.type}`)} title={def.description} data-flow-node={id}>
        <Ports def={def} id={id} io="in" />
        {def.inputs.length > 1 && def.inputs.map((port, index) => (
          <span key={port.name} className="flow-gate-pin" style={{ top: `${((index + 1) / (def.inputs.length + 1)) * 100}%` }}>{port.label}</span>
        ))}
        <span className="font-mono text-[12px] font-bold tracking-wider">{def.label}</span>
        {data.title !== def.label && <span className="max-w-[96px] truncate text-[11px] opacity-85">{data.title}</span>}
        <Badges id={id} />
        <Ports def={def} id={id} io="out" />
      </div>
    );
  }

  if (def.role === 'trigger' || def.role === 'condition') {
    const drives = [...(analysis.drives.get(id) ?? [])];
    return (
      <div className={cn(common, 'flow-tile', def.role === 'trigger' ? 'flow-tile-trigger' : 'flow-tile-cond')} data-flow-node={id}>
        <Ports def={def} id={id} io="in" />
        <div className="flex items-center gap-2">
          <span className="grid h-6 w-6 flex-none place-items-center rounded-md bg-[rgb(var(--tile)/0.2)] text-[rgb(var(--tile))]">
            {isRunning ? <Loader2 className="h-3.5 w-3.5 animate-spin-slow" /> : <Icon className="h-3.5 w-3.5" />}
          </span>
          <div className="min-w-0 flex-1">
            <div className="truncate text-[12px] font-semibold text-fg">{data.title}</div>
            <div className="truncate text-[11px] text-fg-3">{summary(def, values, id, kinds)[0]}</div>
          </div>
          <Badges id={id} />
        </div>
        {(def.plugin || (def.role === 'trigger' && drives.length > 1)) && (
          <div className="mt-1 flex items-center gap-2 text-[10px] text-fg-4">
            {def.plugin && <span className="flex items-center gap-0.5"><Puzzle className="h-2.5 w-2.5" />{def.plugin}</span>}
            {def.role === 'trigger' && drives.length > 1 && <span>驱动 {drives.length} 个 WF</span>}
          </div>
        )}
        <Ports def={def} id={id} io="out" />
      </div>
    );
  }

  const wf = analysis.workflows.get(id);
  const lines = summary(def, values, id, kinds);
  const topInputs = def.inputs.filter((port) => portSide(port, 'in') === 'top');
  const readsPrevious = wf?.writesState.some((stateId) => {
    const state = nodes.find((node) => node.id === stateId);
    return state ? resolveConfig(state.data, types, defaults, pluginDefaults).values.writer_reads_previous : false;
  });
  return (
    <div className={cn(common, 'surface', `tint-${nodeTint(def)}`, wf ? 'flow-node-wf w-[244px]' : 'w-[224px]', 'rounded-[14px] !p-0')} data-flow-node={id}>
      <Ports def={def} id={id} io="in" />
      {topInputs.length > 1 && (
        <div className="pointer-events-none flex justify-around px-2 pt-1.5 text-[10px] leading-3 text-fg-4">
          {topInputs.map((port) => <span key={port.name}>{port.label}</span>)}
        </div>
      )}
      <div className="flex items-center gap-2.5 px-3 pt-3">
        <div className="grid h-8 w-8 flex-none place-items-center rounded-lg bg-[rgb(var(--tint)/0.16)] text-tint">
          {isRunning ? <Loader2 className="h-4 w-4 animate-spin-slow" /> : <Icon className="h-4 w-4" />}
        </div>
        <div className="min-w-0 flex-1">
          <div className="truncate text-[13px] font-semibold text-fg">{data.title}</div>
          <div className="flex items-center gap-1 truncate text-[11px] text-tint">
            {def.plugin && <Puzzle className="h-3 w-3" />}
            {def.plugin ? `插件 · ${def.plugin}` : wf ? (wf.background ? 'LLM · 后台' : wf.triggered ? 'LLM · 前台' : 'LLM · 未触发') : def.label}
            {def.callsModel && <span className="ml-1 flex items-center gap-0.5 text-fg-3" title="调用插件自己的模型"><Cpu className="h-3 w-3" />插件模型</span>}
          </div>
        </div>
        <Badges id={id} />
      </div>
      <div className="space-y-0.5 px-3 pb-3 pt-2">
        {lines.map((line, index) => <div key={index} className={cn('truncate text-[12px]', index === 0 ? 'text-fg-2' : 'text-fg-3')}>{line}</div>)}
        {(readsPrevious || (wf && wf.handoffTools.length > 0)) && (
          <div className="mt-1 flex flex-wrap gap-1">
            {readsPrevious && (
              <span className="flex items-center gap-1 rounded-md border border-[rgb(var(--rgb-orange)/0.4)] bg-[rgb(var(--rgb-orange)/0.1)] px-1.5 py-px text-[10px] text-orange" title="运行时自动拿到所写状态槽的上一版">
                <History className="h-2.5 w-2.5" />读上一版
              </span>
            )}
            {wf?.handoffTools.map((tool) => (
              <span key={tool} className="flex items-center gap-1 rounded-md border border-[rgb(var(--rgb-orange)/0.4)] bg-[rgb(var(--rgb-orange)/0.1)] px-1.5 py-px font-mono text-[10px] text-orange" title="运行时额外获得的交接工具">
                <Wrench className="h-2.5 w-2.5" />{tool}
              </span>
            ))}
          </div>
        )}
      </div>
      <Ports def={def} id={id} io="out" />
    </div>
  );
});
