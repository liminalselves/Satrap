import { useEffect, useRef, useState, type ReactNode } from 'react';
import { AlertCircle, AlertTriangle, ArrowRight, CheckCircle2, CloudOff, Info, Puzzle, Spline, Trash2, Wrench } from 'lucide-react';
import type { EdictumAvailablePlugin, EdictumPluginConfigField, FlowIssue, FlowPortType } from '@/api/types';
import { Badge, Button, Input, Tabs } from '@/components/kit';
import { PluginConfigFields } from '@/components/common/PluginConfigFields';
import { cn } from '@/utils/cn';
import type { FlowAnalysis } from './analysis';
import {
  formatPluginList, parsePluginList, portSide, resolveConfig, SOURCE_LABEL, toolName,
  type EdgeReading, type FlowDefaults, type FlowEdge, type FlowNode, type NodeData, type PluginDefaults, type TypeIndex,
} from './flowModel';
import { nodeIcon, nodeTint, PORT_STYLE } from './portStyle';

/** 检查状态: idle 未检查, checking 请求中, ready 有结果, unavailable 检查服务不可用 */
export type ValidateState = 'idle' | 'checking' | 'ready' | 'unavailable';

/** 后端问题分类的显示顺序; 未列出的分类排在最后 */
const CATEGORIES = ['结构', '触发', '逻辑', '端口', '状态槽', 'workflow', '配置', '成本', '环境'];

function fmt(value: unknown): string {
  if (typeof value === 'boolean') return value ? '开' : '关';
  const text = String(value ?? '');
  return text ? (text.length > 24 ? `${text.slice(0, 24)}…` : text.replace(/\n/g, ', ')) : '空';
}

function Section({ title, children, right }: { title: string; children: ReactNode; right?: ReactNode }) {
  return (
    <section className="space-y-3 border-t border-line pt-4">
      <div className="flex items-center justify-between">
        <h4 className="text-[12px] font-semibold uppercase tracking-wide text-fg-3">{title}</h4>
        {right}
      </div>
      {children}
    </section>
  );
}

export function IssueRow({ issue, onFocus }: { issue: FlowIssue; onFocus?: (issue: FlowIssue) => void }) {
  const error = issue.level === 'error';
  const target = issue.node || issue.edge;
  return (
    <button
      type="button" disabled={!target || !onFocus} onClick={() => onFocus?.(issue)} data-issue={issue.code}
      className={cn('flex w-full items-start gap-2 rounded-lg border px-2.5 py-2 text-left text-[12px] leading-5 transition-colors enabled:hover:bg-glass-hover',
        error ? 'border-[rgb(var(--rgb-red)/0.35)]' : 'border-[rgb(var(--rgb-orange)/0.3)]')}
    >
      {error ? <AlertCircle className="mt-0.5 h-3.5 w-3.5 flex-none text-red" /> : <AlertTriangle className="mt-0.5 h-3.5 w-3.5 flex-none text-orange" />}
      <span className="min-w-0">
        <span className="block text-fg-2">{issue.message}</span>
        {issue.hint && <span className="block text-fg-3">{issue.hint}</span>}
        <span className="block font-mono text-[10px] text-fg-4">{issue.code}</span>
      </span>
    </button>
  );
}

/** 按分类分组, 同组内 error 在前 */
export function groupIssues(issues: FlowIssue[]): Array<{ category: string; items: FlowIssue[] }> {
  const order = (category: string) => { const index = CATEGORIES.indexOf(category); return index < 0 ? CATEGORIES.length : index; };
  const categories = [...new Set(issues.map((issue) => issue.category))].sort((a, b) => order(a) - order(b));
  return categories.map((category) => ({
    category,
    items: issues.filter((issue) => issue.category === category)
      .sort((a, b) => Number(b.level === 'error') - Number(a.level === 'error')),
  }));
}

function FlowOverview({ issues, state, analysis, nodes, onFocus, onSelect }: {
  issues: FlowIssue[]; state: ValidateState; analysis: FlowAnalysis; nodes: FlowNode[];
  onFocus: (issue: FlowIssue) => void; onSelect: (id: string) => void;
}) {
  const [filter, setFilter] = useState<'all' | 'error' | 'warning'>('all');
  const errors = issues.filter((issue) => issue.level === 'error').length;
  const shown = issues.filter((issue) => filter === 'all' || issue.level === filter);
  const title = (id: string) => nodes.find((node) => node.id === id)?.data.title ?? id;
  return (
    <div className="space-y-4">
      <div>
        <h3 className="text-[15px] font-semibold text-fg">流程概览</h3>
        <p className="mt-0.5 text-[13px] text-fg-3">只有 WF 会调用 LLM。触发磁贴和逻辑门接在 WF 左侧, 决定它什么时候运行。</p>
      </div>
      <div className="space-y-1.5">
        {[...analysis.workflows.entries()].map(([id, wf]) => (
          <button key={id} type="button" onClick={() => onSelect(id)} className="w-full rounded-lg border border-line px-2.5 py-1.5 text-left text-[12px] hover:bg-glass-hover">
            <div className="flex items-center gap-2">
              <span className="font-medium text-fg">{title(id)}</span>
              <Badge tone={wf.background ? 'teal' : wf.triggered ? 'pink' : 'red'}>{wf.background ? '后台' : wf.triggered ? '前台' : '不会运行'}</Badge>
            </div>
            {wf.handoffTools.length > 0 && <div className="mt-0.5 truncate font-mono text-[11px] text-orange">{wf.handoffTools.join(', ')}</div>}
          </button>
        ))}
      </div>
      <div className="border-t border-line pt-4" aria-label="流程检查">
        <h3 className="mb-2 text-[13px] font-semibold text-fg">流程检查</h3>
        {state === 'unavailable' ? (
          <div role="status" className="flex items-start gap-2 rounded-lg border border-[rgb(var(--rgb-orange)/0.35)] bg-[rgb(var(--rgb-orange)/0.1)] px-3 py-2.5 text-[13px] text-orange">
            <CloudOff className="mt-0.5 h-4 w-4 flex-none" />检查服务不可用。仍可保存, 保存时后端会完整检查。
          </div>
        ) : state === 'idle' || (state === 'checking' && issues.length === 0) ? (
          <p className="text-[13px] text-fg-3">正在检查…</p>
        ) : issues.length === 0 ? (
          <div className="flex items-center gap-2 rounded-lg border border-[rgb(var(--rgb-green)/0.35)] bg-[rgb(var(--rgb-green)/0.1)] px-3 py-2.5 text-[13px] text-green"><CheckCircle2 className="h-4 w-4" />没有发现问题</div>
        ) : (
          <div className="space-y-3">
            <Tabs label="问题筛选" size="sm" value={filter} onChange={setFilter} items={[
              { value: 'all', label: '全部', count: issues.length },
              { value: 'error', label: '错误', count: errors },
              { value: 'warning', label: '提示', count: issues.length - errors },
            ]} />
            {groupIssues(shown).map(({ category, items }) => (
              <div key={category} className="space-y-1.5">
                <div className="text-[11px] font-semibold text-fg-3">{category}</div>
                {items.map((issue, index) => <IssueRow key={`${issue.code}-${index}`} issue={issue} onFocus={onFocus} />)}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function EdgeInspector({ edge, type, portLabel, reading, nodes, types, issues, focus, onLabel, onDelete, onSelect }: {
  edge: FlowEdge; type: FlowPortType | null; portLabel: (type: FlowPortType) => string; reading: EdgeReading | null;
  nodes: FlowNode[]; types: TypeIndex; issues: FlowIssue[]; focus: number;
  onLabel: (id: string, label: string) => void; onDelete: (id: string) => void; onSelect: (id: string) => void;
}) {
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => { if (focus) ref.current?.focus(); }, [focus, edge.id]);
  const end = (id: string, port: string | null | undefined, io: 'inputs' | 'outputs') => {
    const node = nodes.find((item) => item.id === id);
    const spec = node && types.get(node.data.type)?.[io].find((item) => item.name === port);
    return (
      <button type="button" onClick={() => onSelect(id)} className="min-w-0 flex-1 rounded-lg border border-line px-2 py-1.5 text-left hover:bg-glass-hover">
        <div className="truncate text-[13px] text-fg">{node?.data.title ?? id}</div>
        <div className="truncate text-[11px] text-fg-3">{spec?.label ?? port}</div>
      </button>
    );
  };
  const style = type ? PORT_STYLE[type] : undefined;
  const own = issues.filter((issue) => issue.edge === edge.id);
  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <Spline className="h-4 w-4 text-fg-3" />
        <h3 className="text-[15px] font-semibold text-fg">连线</h3>
        {type && style && <span className="ml-auto rounded px-1.5 text-[11px]" style={{ color: `rgb(${style.rgb})`, background: `rgb(${style.rgb} / 0.14)` }}>{portLabel(type)}</span>}
      </div>
      <div className="flex items-center gap-2">{end(edge.source, edge.sourceHandle, 'outputs')}<ArrowRight className="h-4 w-4 flex-none text-fg-4" />{end(edge.target, edge.targetHandle, 'inputs')}</div>
      {style && <p className="text-[12px] text-fg-3">{style.desc}</p>}
      {own.length > 0 && <div className="space-y-1.5">{own.map((issue, index) => <IssueRow key={index} issue={issue} />)}</div>}
      <Section title="读取方式">
        {reading ? (
          <div className="rounded-lg border border-line p-2.5 text-[12px]">
            <div className="text-fg">{reading.text}</div>
            <div className="mt-1 flex items-center gap-1 text-fg-3">
              由 <button type="button" onClick={() => onSelect(reading.owner)} className="text-accent hover:underline">{nodes.find((node) => node.id === reading.owner)?.data.title ?? reading.owner}</button> 的配置决定, 随配置自动更新
            </div>
          </div>
        ) : <p className="text-[12px] text-fg-3">原样传递, 没有额外的读取规则</p>}
        <p className="text-xs text-fg-3">连线本身不带逻辑。同一个输出接到不同节点时, 每个接收节点按自己的配置读取。</p>
      </Section>
      <Section title="注释">
        <Input ref={ref} value={edge.data?.label ?? ''} placeholder="可选, 显示在读取方式后面" onChange={(event) => onLabel(edge.id, event.target.value)} aria-label="连线注释" />
        <p className="text-xs text-fg-3">只用于说明, 随流程保存, 不影响运行。双击连线可直接编辑。</p>
      </Section>
      <div className="border-t border-line pt-4">
        <Button size="sm" variant="ghost" icon={<Trash2 className="h-3.5 w-3.5" />} onClick={() => onDelete(edge.id)} className="text-red">删除连线</Button>
      </div>
    </div>
  );
}

/** WF 插件多选: 存档里是每行一个插件名的字符串 */
function WorkflowPlugins({ value, plugins, background, onChange }: {
  value: unknown; plugins: EdictumAvailablePlugin[]; background: boolean; onChange: (value: string) => void;
}) {
  const list = parsePluginList(value);
  const known = new Set(plugins.map((plugin) => plugin.name));
  const missing = list.filter((name) => !known.has(name));
  const toggle = (name: string) => onChange(formatPluginList(list.includes(name) ? list.filter((item) => item !== name) : [...list, name]));
  return (
    <div className="space-y-1.5" data-field="plugins" role="group" aria-label="WF 插件">
      {plugins.length === 0 && <p className="text-[12px] text-fg-3">没有可用插件</p>}
      {[...plugins.map((plugin) => ({ name: plugin.name, plugin })), ...missing.map((name) => ({ name, plugin: undefined }))].map(({ name, plugin }) => {
        const on = list.includes(name);
        const foreground = plugin?.flow_workflow?.foreground_only;
        const provides = plugin?.flow_workflow?.provides ?? [];
        return (
          <button key={name} type="button" role="checkbox" aria-checked={on} onClick={() => toggle(name)}
            className={cn('flex w-full items-start gap-2.5 rounded-lg border px-2.5 py-2 text-left transition-colors',
              on ? 'border-[rgb(var(--rgb-purple)/0.5)] bg-[rgb(var(--rgb-purple)/0.12)]' : 'border-line hover:bg-glass-hover')}>
            <span className={cn('mt-0.5 grid h-4 w-4 flex-none place-items-center rounded border text-[10px]', on ? 'border-purple bg-[rgb(var(--rgb-purple))] text-white' : 'border-[var(--glass-border-strong)]')}>{on ? '✓' : ''}</span>
            <span className="min-w-0">
              <span className="block font-mono text-[12.5px] text-fg">{name}</span>
              <span className={cn('block text-[11px] leading-4', plugin ? 'text-fg-3' : 'text-orange')}>
                {plugin ? plugin.description : '插件未安装'}
                {foreground ? <span className={cn(background && on && 'text-red')}> · 仅前台</span> : ''}
                {provides.length ? ` · 提供 ${provides.join(', ')}` : ''}
              </span>
            </span>
          </button>
        );
      })}
    </div>
  );
}

export interface InspectorProps {
  /** 选中的边 */
  edge: FlowEdge | null;
  /** 选中边的实际类型 */
  edgeType: FlowPortType | null;
  /** 选中边的读取说明 */
  reading: EdgeReading | null;
  /** 双击边时递增, 让注释输入框获得焦点 */
  focusLabel: number;
  /** 选中的节点 */
  node: FlowNode | null;
  /** 检查器需要高亮的字段 */
  focusField: string | null;
  /** 全部节点 */
  nodes: FlowNode[];
  /** 节点类型表 */
  types: TypeIndex;
  /** 端口类型显示名 */
  portLabel: (type: FlowPortType) => string;
  /** 检查问题 */
  issues: FlowIssue[];
  /** 检查状态 */
  validateState: ValidateState;
  /** 流程默认 */
  defaults: FlowDefaults;
  /** 插件全局默认 */
  pluginDefaults: PluginDefaults;
  /** 结构分析 */
  analysis: FlowAnalysis;
  /** 可装在 WF 上的插件 */
  plugins: EdictumAvailablePlugin[];
  /** 顶部选择的对话类型 */
  kinds: Array<'group' | 'private'>;
  /** 可选 LLM 配置名 */
  models: Array<{ name: string; ctx: number; tools: boolean }>;
  onEdgeLabel: (id: string, label: string) => void;
  onEdgeDelete: (id: string) => void;
  onPatch: (id: string, patch: Partial<NodeData>) => void;
  onConfig: (id: string, key: string, value: unknown) => void;
  onReset: (id: string, key: string) => void;
  onDelete: (id: string) => void;
  onSelect: (id: string) => void;
  onFocusIssue: (issue: FlowIssue) => void;
  onPromote: (type: string, key: string, value: unknown) => void;
}

export function Inspector(props: InspectorProps) {
  const { edge, node, nodes, types, issues, defaults, pluginDefaults, analysis, focusField } = props;
  const formRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!focusField) return;
    const target = formRef.current?.querySelector<HTMLElement>(`[data-field="${CSS.escape(focusField)}"]`);
    if (!target) return;
    target.scrollIntoView({ block: 'center', behavior: 'smooth' });
    target.classList.add('flow-field-focus');
    const timer = window.setTimeout(() => target.classList.remove('flow-field-focus'), 1800);
    return () => window.clearTimeout(timer);
  }, [focusField, node?.id]);

  if (edge) {
    return <EdgeInspector edge={edge} type={props.edgeType} portLabel={props.portLabel} reading={props.reading} nodes={nodes} types={types} issues={issues}
      focus={props.focusLabel} onLabel={props.onEdgeLabel} onDelete={props.onEdgeDelete} onSelect={props.onSelect} />;
  }
  if (!node) return <FlowOverview issues={issues} state={props.validateState} analysis={analysis} nodes={nodes} onFocus={props.onFocusIssue} onSelect={props.onSelect} />;

  const def = types.get(node.data.type);
  const own = issues.filter((issue) => issue.node === node.id);
  if (!def) {
    return (
      <div className="space-y-3">
        <h3 className="text-[15px] font-semibold text-red">未知节点类型</h3>
        <p className="text-[12px] text-fg-3">类型 <span className="font-mono">{node.data.type}</span> 不在当前节点表里, 提供它的插件可能已卸载或停用。</p>
        {own.map((issue, index) => <IssueRow key={index} issue={issue} />)}
        <Button size="sm" variant="ghost" icon={<Trash2 className="h-3.5 w-3.5" />} onClick={() => props.onDelete(node.id)} className="text-red">删除节点</Button>
      </div>
    );
  }
  const Icon = nodeIcon(def);
  const resolved = resolveConfig(node.data, types, defaults, pluginDefaults);
  const wf = analysis.workflows.get(node.id);
  const drives = [...(analysis.drives.get(node.id) ?? [])];
  const title = (id: string) => nodes.find((item) => item.id === id)?.data.title ?? id;
  const isWorkflow = def.role === 'workflow';
  // plugins 字段单独渲染成多选, 其余交给通用渲染器
  const schema: Record<string, EdictumPluginConfigField> = Object.fromEntries(
    Object.entries(def.schema).filter(([key]) => !(isWorkflow && key === 'plugins')));
  const sourceText = Object.fromEntries(Object.keys(def.schema).map((key) => [key, SOURCE_LABEL[resolved.sources[key] ?? 'type']]));
  const overrides = Object.keys(node.data.config).filter((key) => key in def.schema && def.schema[key].type !== 'textarea' && key !== 'tool_name');
  const sendMode = node.data.type === 'output' ? String(resolved.values.send_mode ?? '') : '';
  const fieldIssues = own.filter((issue) => issue.field);
  const modelOptions = { llm: props.models.map((model) => ({ value: model.name, label: model.ctx ? `${model.name} · ${Math.round(model.ctx / 1000)}k` : model.name })) };

  return (
    <div className={cn('space-y-4', `tint-${nodeTint(def)}`)} ref={formRef}>
      <div className="flex items-start gap-3">
        <div className="grid h-9 w-9 flex-none place-items-center rounded-lg bg-[rgb(var(--tint)/0.16)] text-tint"><Icon className="h-[18px] w-[18px]" /></div>
        <div className="min-w-0 flex-1">
          <Input value={node.data.title} onChange={(event) => props.onPatch(node.id, { title: event.target.value })} className="h-8 text-[14px] font-semibold" aria-label="节点名称" />
          <p className="mt-1 text-xs leading-5 text-fg-3">{def.description}</p>
          <div className="mt-1.5 flex flex-wrap gap-1.5">
            <Badge tone="neutral"><span className="font-mono text-[10px]">{node.id}</span></Badge>
            <Badge tone="neutral">v{node.data.version}{node.data.version !== def.version ? ` → v${def.version}` : ''}</Badge>
            {def.plugin && <Badge tone="teal"><Puzzle className="h-3 w-3" />{def.plugin}</Badge>}
          </div>
        </div>
      </div>

      {own.filter((issue) => !issue.field).length > 0 && (
        <div className="space-y-1.5">{own.filter((issue) => !issue.field).map((issue, index) => <IssueRow key={index} issue={issue} />)}</div>
      )}

      {wf && (
        <div className="space-y-1 rounded-lg border border-line p-2.5 text-[12px] text-fg-2">
          <div><span className="text-fg-3">运行位置 </span>{wf.background ? '后台' : wf.triggered ? '前台' : <span className="text-red">不会被触发</span>}</div>
          {wf.handoffTools.length > 0 && (
            <div className="flex flex-wrap items-center gap-1"><span className="text-fg-3">交接工具 </span>
              {wf.handoffTools.map((tool) => <span key={tool} className="flex items-center gap-1 rounded border border-[rgb(var(--rgb-orange)/0.4)] px-1 font-mono text-[10px] text-orange"><Wrench className="h-2.5 w-2.5" />{tool}</span>)}
            </div>
          )}
          {wf.writesState.length > 0 && <div><span className="text-fg-3">写入 </span>{wf.writesState.map(title).join(', ')}</div>}
        </div>
      )}
      {def.role === 'trigger' && (
        <div className="space-y-1 rounded-lg border border-line p-2.5 text-[12px] text-fg-2">
          <div><span className="text-fg-3">驱动 </span>{drives.length ? drives.map(title).join(', ') : <span className="text-orange">尚未到达任何 WF</span>}</div>
          {node.data.type === 'tool' && <div><span className="text-fg-3">工具名 </span><span className="font-mono">{toolName(node.id, resolved.values)}</span></div>}
          <div className="text-fg-3">左侧"条件"全部为真才触发; 需要"或""非"时先经过逻辑门</div>
        </div>
      )}

      {def.role !== 'note' && (
        <Section title="端口">
          <div className="space-y-1 text-[12px]">
            {(['inputs', 'outputs'] as const).flatMap((io) => def[io].map((port) => {
              const style = PORT_STYLE[port.types[0]];
              return (
                <div key={io + port.name} className="flex items-center gap-1.5">
                  <span className="h-2 w-2 flex-none rounded-full" style={{ background: `rgb(${style.rgb})` }} />
                  <span className="w-8 flex-none text-fg-4">{io === 'inputs' ? '输入' : '输出'}</span>
                  <span className="text-fg-2">{port.label}</span>
                  <span className="truncate text-fg-4">
                    {port.derived ? '触发或条件' : port.types.map(props.portLabel).join(' / ')}
                    {' · '}{({ top: '上', bottom: '下', left: '左', right: '右' })[portSide(port, io === 'inputs' ? 'in' : 'out')]}
                    {port.required ? ' · 必需' : ''}
                  </span>
                </div>
              );
            }))}
          </div>
        </Section>
      )}

      {fieldIssues.length > 0 && <div className="space-y-1.5">{fieldIssues.map((issue, index) => <IssueRow key={index} issue={issue} />)}</div>}

      <Section title="配置">
        <PluginConfigFields
          schema={schema}
          values={node.data.config}
          inherited={resolved.inherited}
          sourceText={sourceText}
          modelOptions={modelOptions}
          singleColumn
          emptyText="这个节点没有可配置项"
          onChange={(key, value) => props.onConfig(node.id, key, value)}
          onReset={(key) => props.onReset(node.id, key)}
        />
        {sendMode === 'group_chat_reply' && (
          <p className="flex items-start gap-1.5 text-[12px] text-orange"><Info className="mt-0.5 h-3.5 w-3.5 flex-none" />
            {props.kinds.includes('private') ? '私聊不能用群聊富消息, 请改用工具发送' : '仅群聊前台运行可用, 需要上游 WF 装 group_chat 插件'}
          </p>
        )}
      </Section>

      {isWorkflow && 'plugins' in def.schema && (
        <Section title="插件" right={<span className={cn('text-[11px]', resolved.sources.plugins === 'node' ? 'text-teal' : 'text-fg-4')}>{SOURCE_LABEL[resolved.sources.plugins ?? 'type']}</span>}>
          <WorkflowPlugins value={resolved.values.plugins} plugins={props.plugins} background={Boolean(wf?.background)}
            onChange={(value) => props.onConfig(node.id, 'plugins', value)} />
          <p className="text-xs text-fg-3">插件只装在这个 WF 上, 运行时以实际授权为准。</p>
        </Section>
      )}

      {overrides.length > 0 && (
        <div className="rounded-lg border border-line p-2.5 text-[12px]">
          <div className="mb-1.5 flex items-start gap-1.5 text-fg-3"><Info className="mt-0.5 h-3.5 w-3.5 flex-none" />把本节点的值设为流程默认, 同一流程里其他{def.label}节点也会使用</div>
          <div className="space-y-1">
            {overrides.map((key) => (
              <div key={key} className="flex items-center gap-1.5">
                <span className="flex-1 truncate text-fg-2">{def.schema[key].label || key} <span className="text-fg-4">= {fmt(resolved.values[key])}</span></span>
                <Button size="sm" variant="ghost" onClick={() => props.onPromote(def.type, key, resolved.values[key])}>设为流程默认</Button>
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="border-t border-line pt-4">
        <Button size="sm" variant="ghost" icon={<Trash2 className="h-3.5 w-3.5" />} onClick={() => props.onDelete(node.id)} className="text-red">删除节点</Button>
      </div>
    </div>
  );
}
