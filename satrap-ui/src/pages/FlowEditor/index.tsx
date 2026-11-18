import { useCallback, useEffect, useMemo, useRef, useState, type DragEvent } from 'react';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import {
  Background, BackgroundVariant, Controls, MarkerType, MiniMap, ReactFlow, ReactFlowProvider, useEdgesState, useNodesState, useReactFlow,
  type Connection, type IsValidConnection, type NodeTypes, type OnConnectStart, type Viewport,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import './flow.css';
import { ArrowLeft, FileJson, FileUp, LayoutTemplate, Redo2, Save, Settings2, Undo2, Workflow } from 'lucide-react';
import { createEdictumApplication, type ConfigReloadResult } from '@/api/backend';
import { controlApi, RevisionConflictError } from '@/api/control';
import { edictumApi } from '@/api/edictum';
import { ApplicationUnconfirmedError } from '@/api/runtimeApplication';
import type {
  EdictumAvailablePlugin, EdictumSessionConfig, FlowBudget, FlowDefinitionJSON, FlowIssue, FlowPortType, FlowTypeMetadata, PlatformConfig,
} from '@/api/types';
import { RuntimeApplicationProgress } from '@/components/common/RuntimeApplicationProgress';
import { Badge, Button, EmptyState, Modal, Select, Skeleton, Textarea, toast } from '@/components/kit';
import { useDirtyGuard } from '@/hooks/useDirtyGuard';
import { useRuntimeApplication } from '@/hooks/useRuntimeApplication';
import { useTheme } from '@/hooks/useTheme';
import { useBackendStore } from '@/stores/useBackendStore';
import { useConfigStore } from '@/stores/useConfigStore';
import { usePrefsStore } from '@/stores/usePrefsStore';
import { cn } from '@/utils/cn';
import { errorText } from '@/utils/errorText';
import { analyzeFlow } from './analysis';
import { IMPORT_MAX_BYTES, parseFlowImport, unknownNodeTypes, type FlowImport } from './flowImport';
import { FlowNodeView } from './FlowNodeView';
import { FlowSettings } from './FlowSettings';
import {
  edgeId, edgeReading, indexTypes, newNodeData, newNodeId, outputType, toDefinition, toReactFlow,
  type EdgeReading, type FlowDefaults, type FlowDraft, type FlowEdge, type FlowNode, type NodeData, type PluginDefaults,
} from './flowModel';
import { History } from './history';
import { Inspector } from './Inspector';
import { DRAG_MIME, Palette } from './Palette';
import { nodeTint, PORT_STYLE } from './portStyle';
import { pruneUnavailable, TEMPLATES } from './templates';
import { FlowUiContext, type FlowUi } from './uiState';
import { useFlowValidation } from './useFlowValidation';

const nodeTypes: NodeTypes = { flow: FlowNodeView };

type Kinds = 'group' | 'private' | 'both';
const KIND_VALUES: Record<Kinds, Array<'group' | 'private'>> = { group: ['group'], private: ['private'], both: ['group', 'private'] };

/**
 * 按平台绑定推断对话类型: 对话类型绑定直接给出类型; 平台默认绑定按群聊处理
 *
 * 没有平台绑定这个配置时返回群聊
 */
export function boundKinds(name: string, platforms: PlatformConfig[]): Kinds {
  const found = new Set<'group' | 'private'>();
  for (const platform of platforms) {
    const bindings = platform.session_bindings ?? {};
    for (const [kind, binding] of Object.entries(bindings)) {
      if (binding.mode === 'value' && binding.provider === 'edictum' && binding.config_name === name && (kind === 'group' || kind === 'private')) found.add(kind);
    }
    if (platform.session_provider === 'edictum' && platform.session_type === name) {
      for (const kind of ['group', 'private'] as const) if (bindings[kind]?.mode !== 'value') found.add(kind);
    }
  }
  if (found.size === 2) return 'both';
  return found.has('private') ? 'private' : 'group';
}

/** 编辑器加载完成后的上下文 */
interface Loaded {
  /** flow 类型元数据 */
  meta: FlowTypeMetadata;
  /** 可装在 WF 上的插件 */
  plugins: EdictumAvailablePlugin[];
  /** 插件全局默认 */
  pluginDefaults: PluginDefaults;
  /** 平台列表, 用于检查后台发送能力 */
  platforms: PlatformConfig[];
  /** 已保存的命名配置; 新建时为 null */
  config: EdictumSessionConfig | null;
}

const isTyping = (target: EventTarget | null) => target instanceof HTMLElement
  && (target.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName));

function decorateEdge(edge: FlowEdge, type: FlowPortType, signal: boolean, reading: EdgeReading | null, broken: boolean): FlowEdge {
  const style = PORT_STYLE[type];
  const color = `rgb(${style.rgb})`;
  const note = edge.data?.label;
  return {
    ...edge,
    type: signal ? 'default' : 'smoothstep',
    animated: type === 'event',
    label: [reading?.text, note && `“${note}”`].filter(Boolean).join('  ') || undefined,
    className: cn(note && 'flow-edge-has-note', broken && 'flow-edge-issue'),
    style: { stroke: color, strokeOpacity: signal ? 0.95 : 0.8, strokeWidth: signal ? 1.6 : 1.8, strokeDasharray: style.dash },
    markerEnd: { type: MarkerType.ArrowClosed, color, width: 14, height: 14 },
    labelBgPadding: [6, 3],
    labelBgBorderRadius: 6,
  };
}

async function loadPluginDefaults(meta: FlowTypeMetadata): Promise<PluginDefaults> {
  const plugins = [...new Set(meta.node_types.map((def) => def.plugin).filter((name): name is string => Boolean(name)))];
  const out: PluginDefaults = {};
  await Promise.all(plugins.map(async (plugin) => {
    try {
      const result = await controlApi.getPluginFlowNodeDefaults(plugin);
      for (const [node, entry] of Object.entries(result.nodes)) out[`${plugin}.${node}`] = { ...entry.config };
    } catch {
      // 读取失败时按类型默认显示; 运行时以后端合并结果为准
    }
  }));
  return out;
}

function Editor({ name, creating, loaded, reload }: { name: string; creating: boolean; loaded: Loaded; reload: () => Promise<void> }) {
  const navigate = useNavigate();
  const [search] = useSearchParams();
  const { theme } = useTheme();
  const isRunning = useBackendStore((state) => state.isRunning);
  const acceptEdictumConfig = useConfigStore((state) => state.acceptEdictumConfig);
  const savedViewport = usePrefsStore((state) => state.flowViewports[name]);
  const setFlowViewport = usePrefsStore((state) => state.setFlowViewport);
  const { meta, plugins, pluginDefaults, platforms } = loaded;
  const types = useMemo(() => indexTypes(meta), [meta]);
  const signalType = useCallback((type: FlowPortType) => meta.port_types[type]?.signal ?? false, [meta]);
  const portLabel = useCallback((type: FlowPortType) => meta.port_types[type]?.label ?? type, [meta]);

  const initial = useMemo<FlowDraft>(() => {
    const flow = loaded.config?.params?.flow as FlowDefinitionJSON | undefined;
    return toReactFlow(flow && Array.isArray(flow.nodes) ? flow : { nodes: [] }, types);
  }, [loaded.config, types]);
  const [nodes, setNodes, onNodesChange] = useNodesState<FlowNode>(initial.nodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState<FlowEdge>(initial.edges);
  const [defaults, setDefaults] = useState<FlowDefaults>(initial.defaults);
  const [budget, setBudget] = useState<FlowBudget | null>(initial.budget ?? null);
  const [revision, setRevision] = useState(loaded.config?.revision);
  const [baseline, setBaseline] = useState(() => JSON.stringify(toDefinition(initial, types)));
  const [selected, setSelected] = useState<string | null>(null);
  const [selectedEdge, setSelectedEdge] = useState<string | null>(null);
  const [focusField, setFocusField] = useState<string | null>(null);
  const [editLabel, setEditLabel] = useState(0);
  const [connecting, setConnecting] = useState<FlowPortType | null>(null);
  const [showJson, setShowJson] = useState(false);
  // 配置弹窗里正在编辑的 JSON; null 表示只读查看
  const [jsonDraft, setJsonDraft] = useState<string | null>(null);
  const [fileOver, setFileOver] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const [showSettings, setShowSettings] = useState(false);
  const [showTemplates, setShowTemplates] = useState(creating && initial.nodes.length === 0);
  const [kinds, setKinds] = useState<Kinds>(() => boundKinds(name, platforms));
  const [platformId, setPlatformId] = useState('');
  const [saving, setSaving] = useState(false);
  const [conflict, setConflict] = useState<RevisionConflictError | null>(null);
  const [confirmPrompt, setConfirmPrompt] = useState<{ message: string; resolve: (value: boolean) => void } | null>(null);
  const [saveErrors, setSaveErrors] = useState<FlowIssue[]>([]);
  const [, bump] = useState(0);
  const history = useRef(new History<FlowDraft>());
  const application = useRuntimeApplication<ConfigReloadResult>();
  const { screenToFlowPosition, fitView } = useReactFlow<FlowNode, FlowEdge>();

  const draft = useMemo<FlowDraft>(() => ({ nodes, edges, defaults, budget }), [nodes, edges, defaults, budget]);
  const draftRef = useRef(draft);
  draftRef.current = draft;
  const definition = useMemo(() => toDefinition(draft, types), [draft, types]);
  const dirty = creating || JSON.stringify(definition) !== baseline;
  const dirtyRef = useRef(dirty);
  dirtyRef.current = dirty;
  useDirtyGuard(() => dirtyRef.current);

  const { issues, state: validateState, validateNow } = useFlowValidation(definition, { kinds: KIND_VALUES[kinds], platformId });
  const analysis = useMemo(() => analyzeFlow(nodes, edges, types), [nodes, edges, types]);
  const readings = useMemo(() => new Map(edges.map((edge) => [edge.id, edgeReading(edge, nodes, edges, types, defaults, pluginDefaults, KIND_VALUES[kinds])])), [edges, nodes, types, defaults, pluginDefaults, kinds]);
  const edgeTypes = useMemo(() => new Map(edges.map((edge) => [edge.id, outputType(edge.source, edge.sourceHandle ?? '', nodes, edges, types)])), [edges, nodes, types]);
  const brokenEdges = useMemo(() => new Set(issues.filter((issue) => issue.edge && issue.level === 'error').map((issue) => issue.edge!)), [issues]);
  const shownEdges = useMemo(() => edges.map((edge) => {
    const type = edgeTypes.get(edge.id) ?? 'messages';
    return decorateEdge(edge, type, signalType(type), readings.get(edge.id) ?? null, brokenEdges.has(edge.id));
  }), [edges, edgeTypes, readings, brokenEdges, signalType]);
  const counts = useMemo(() => {
    const map = new Map<string, number>();
    for (const node of nodes) map.set(node.data.type, (map.get(node.data.type) ?? 0) + 1);
    return map;
  }, [nodes]);
  const errorCount = issues.filter((issue) => issue.level === 'error').length;
  const warnCount = issues.length - errorCount;
  const selectedNode = nodes.find((node) => node.id === selected) ?? null;
  const edgeSel = edges.find((edge) => edge.id === selectedEdge) ?? null;

  const record = useCallback((key?: string) => {
    history.current.record(draftRef.current, key);
    bump((value) => value + 1);
  }, []);
  const restore = useCallback((snapshot: FlowDraft) => {
    setNodes(snapshot.nodes);
    setEdges(snapshot.edges);
    setDefaults(snapshot.defaults);
    setBudget(snapshot.budget ?? null);
    bump((value) => value + 1);
  }, [setEdges, setNodes]);
  const undo = useCallback(() => { const snapshot = history.current.undo(draftRef.current); if (snapshot) restore(snapshot); }, [restore]);
  const redo = useCallback(() => { const snapshot = history.current.redo(draftRef.current); if (snapshot) restore(snapshot); }, [restore]);

  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if (!(event.ctrlKey || event.metaKey) || isTyping(event.target)) return;
      const key = event.key.toLowerCase();
      if (key === 'z' && !event.shiftKey) { event.preventDefault(); undo(); }
      else if ((key === 'z' && event.shiftKey) || key === 'y') { event.preventDefault(); redo(); }
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [redo, undo]);

  const defOf = useCallback((id: string) => types.get(nodes.find((node) => node.id === id)?.data.type ?? ''), [nodes, types]);

  const isValidConnection: IsValidConnection<FlowEdge> = useCallback((connection) => {
    if (connection.source === connection.target) return false;
    const target = defOf(connection.target)?.inputs.find((port) => port.name === connection.targetHandle);
    if (!target) return false;
    const type = outputType(connection.source, connection.sourceHandle ?? '', nodes, edges, types);
    if (!type || !target.types.includes(type)) return false;
    const id = edgeId(connection.source, connection.sourceHandle ?? '', connection.target, connection.targetHandle ?? '');
    return !edges.some((edge) => edge.id === id);
  }, [defOf, edges, nodes, types]);

  const onConnectStart: OnConnectStart = useCallback((_, params) => {
    if (params.handleType !== 'source' || !params.nodeId) return;
    setConnecting(outputType(params.nodeId, params.handleId ?? '', nodes, edges, types));
  }, [edges, nodes, types]);

  const onConnect = useCallback((connection: Connection) => {
    const target = defOf(connection.target)?.inputs.find((port) => port.name === connection.targetHandle);
    record();
    const sourceHandle = connection.sourceHandle ?? '', targetHandle = connection.targetHandle ?? '';
    const edge: FlowEdge = {
      id: edgeId(connection.source, sourceHandle, connection.target, targetHandle),
      source: connection.source, sourceHandle, target: connection.target, targetHandle, data: {},
    };
    // 单输入端口: 新连线替换旧连线
    setEdges((current) => [
      ...current.filter((item) => target?.multiple || !(item.target === connection.target && item.targetHandle === targetHandle)),
      edge,
    ]);
  }, [defOf, record, setEdges]);

  const addNode = useCallback((type: string, position?: { x: number; y: number }) => {
    const def = types.get(type);
    if (!def) return;
    if (def.maxPerFlow !== undefined && (counts.get(type) ?? 0) >= def.maxPerFlow) {
      toast.warning(`每个流程最多 ${def.maxPerFlow} 个${def.label}`);
      return;
    }
    record();
    const id = newNodeId(type, new Set(nodes.map((node) => node.id)));
    const at = position ?? screenToFlowPosition({ x: window.innerWidth / 2 - 120, y: window.innerHeight / 2 - 120 });
    const title = def.role === 'workflow' ? '新 WF' : undefined;
    setNodes((current) => [...current.map((node) => ({ ...node, selected: false })), { id, type: 'flow', position: at, data: newNodeData(def, title), selected: true }]);
    setSelected(id);
    setSelectedEdge(null);
  }, [counts, nodes, record, screenToFlowPosition, setNodes, types]);

  /** 用导入的 JSON 替换画布; 结构错误时不动画布, 语义问题交给检查面板 */
  const importFlow = useCallback((text: string, from: string): boolean => {
    let parsed: FlowImport;
    let next: FlowDraft;
    try {
      parsed = parseFlowImport(text);
      next = toReactFlow(parsed.definition, types);
    } catch (error) {
      toast.error(`无法导入${from}`, errorText(error));
      return false;
    }
    record();
    restore(next);
    setSelected(null);
    setSelectedEdge(null);
    setSaveErrors([]);
    setShowJson(false);
    setShowTemplates(false);
    const unknown = unknownNodeTypes(parsed.definition, types);
    if (unknown.length) toast.warning('已导入, 部分节点类型不可用', `未安装或未知: ${unknown.join(', ')}`);
    else toast.success('已导入流程', `${parsed.origin} · ${next.nodes.length} 个节点, 可撤销; 保存后生效`);
    window.setTimeout(() => { void fitView({ duration: 400, padding: 0.12 }); }, 30);
    return true;
  }, [fitView, record, restore, types]);

  const importFile = useCallback(async (file: File) => {
    if (!/\.json$/i.test(file.name) && file.type !== 'application/json') {
      toast.error('无法导入文件', `${file.name} 不是 .json 文件`);
      return;
    }
    if (file.size > IMPORT_MAX_BYTES) {
      toast.error('无法导入文件', `${file.name} 超过 ${IMPORT_MAX_BYTES / 1024 / 1024} MB`);
      return;
    }
    importFlow(await file.text(), ` ${file.name}`);
  }, [importFlow]);

  const onDrop = useCallback((event: DragEvent) => {
    event.preventDefault();
    setFileOver(false);
    const file = event.dataTransfer.files[0];
    if (file) { void importFile(file); return; }
    const type = event.dataTransfer.getData(DRAG_MIME);
    if (!type) return;
    const point = screenToFlowPosition({ x: event.clientX, y: event.clientY });
    addNode(type, { x: point.x - 110, y: point.y - 40 });
  }, [addNode, importFile, screenToFlowPosition]);

  const patchNode = useCallback((id: string, patch: Partial<NodeData>) => {
    record(`patch:${id}:${Object.keys(patch).join(',')}`);
    setNodes((current) => current.map((node) => (node.id === id ? { ...node, data: { ...node.data, ...patch } } : node)));
  }, [record, setNodes]);
  const setConfig = useCallback((id: string, key: string, value: unknown) => {
    record(`config:${id}:${key}`);
    setNodes((current) => current.map((node) => (node.id === id ? { ...node, data: { ...node.data, config: { ...node.data.config, [key]: value } } } : node)));
  }, [record, setNodes]);
  const resetConfig = useCallback((id: string, key: string) => {
    record();
    setNodes((current) => current.map((node) => {
      if (node.id !== id) return node;
      const config = { ...node.data.config };
      delete config[key];
      return { ...node, data: { ...node.data, config } };
    }));
  }, [record, setNodes]);
  const promote = useCallback((type: string, key: string, value: unknown) => {
    record();
    setDefaults((current) => ({ ...current, [type]: { ...current[type], [key]: value } }));
    // 同类型且值相同的节点改为继承
    setNodes((current) => current.map((node) => {
      if (node.data.type !== type || JSON.stringify(node.data.config[key]) !== JSON.stringify(value)) return node;
      const config = { ...node.data.config };
      delete config[key];
      return { ...node, data: { ...node.data, config } };
    }));
    toast.success('已设为流程默认', `${types.get(type)?.label ?? type} · ${types.get(type)?.schema[key]?.label || key}`);
  }, [record, setNodes, types]);
  const deleteNode = useCallback((id: string) => {
    record();
    setNodes((current) => current.filter((node) => node.id !== id));
    setEdges((current) => current.filter((edge) => edge.source !== id && edge.target !== id));
    setSelected(null);
  }, [record, setEdges, setNodes]);
  const setEdgeLabel = useCallback((id: string, label: string) => {
    record(`label:${id}`);
    setEdges((current) => current.map((edge) => (edge.id === id ? { ...edge, data: { ...edge.data, label: label || undefined } } : edge)));
  }, [record, setEdges]);
  const deleteEdge = useCallback((id: string) => {
    record();
    setEdges((current) => current.filter((edge) => edge.id !== id));
    setSelectedEdge(null);
  }, [record, setEdges]);

  const focusNode = useCallback((id: string, field: string | null = null) => {
    setSelected(id);
    setSelectedEdge(null);
    setFocusField(field);
    setNodes((current) => current.map((node) => ({ ...node, selected: node.id === id })));
    void fitView({ nodes: [{ id }], duration: 400, maxZoom: 1.1, padding: 0.8 });
  }, [fitView, setNodes]);
  const focusIssue = useCallback((issue: FlowIssue) => {
    if (issue.node) { focusNode(issue.node, issue.field ?? null); return; }
    const edge = issue.edge ? edges.find((item) => item.id === issue.edge) : undefined;
    if (!edge) return;
    setSelected(null);
    setSelectedEdge(edge.id);
    void fitView({ nodes: [{ id: edge.source }, { id: edge.target }], duration: 400, maxZoom: 1.1, padding: 0.6 });
  }, [edges, fitView, focusNode]);

  const applyTemplate = useCallback((id: string) => {
    const template = TEMPLATES.find((item) => item.id === id);
    if (!template) return;
    const available = new Set(meta.node_types.map((def) => def.type));
    const { definition: built, missing } = pruneUnavailable(template.build(meta.models[0]?.name ?? ''), available);
    record();
    restore(toReactFlow(built, types));
    setSelected(null);
    setSelectedEdge(null);
    setShowTemplates(false);
    if (missing.length) toast.warning('部分插件节点已省略', `未安装插件: ${missing.join(', ')}`);
    window.setTimeout(() => { void fitView({ duration: 400, padding: 0.12 }); }, 30);
  }, [fitView, meta, record, restore, types]);

  const askConfirm = useCallback((message: string) => new Promise<boolean>((resolve) => setConfirmPrompt({ message, resolve })), []);
  const settleConfirm = useCallback((value: boolean) => {
    setConfirmPrompt((current) => { current?.resolve(value); return null; });
  }, []);

  const save = useCallback(async (overwriteRevision?: string) => {
    if (saving) return;
    setSaving(true);
    setSaveErrors([]);
    try {
      const checked = await validateNow();
      const blocking = checked?.issues.filter((issue) => issue.level === 'error') ?? [];
      if (blocking.length) {
        setSaveErrors(blocking);
        setSelected(null);
        setSelectedEdge(null);
        toast.error(`还有 ${blocking.length} 个错误, 修复后才能保存`);
        return;
      }
      const flow = toDefinition(draftRef.current, types);
      const base = loaded.config;
      const payload = {
        name,
        edictum_type: 'flow',
        enabled: base?.enabled ?? search.get('enabled') !== '0',
        description: base?.description ?? search.get('description') ?? '',
        model_name: base?.model_name ?? search.get('model') ?? meta.models[0]?.name ?? '',
        params: { ...(base?.params ?? {}), flow },
        plugins: [],
      };
      if (isRunning && !creating) {
        const preview = await edictumApi.previewFullRuntimeChanges(name, payload);
        const affected = preview.edictum_sessions.filter((item) => item.action !== 'noop');
        if (affected.length > 0 && !(await askConfirm(`本次配置将立即协调 ${affected.length} 个活跃会话, 是否继续?`))) return;
      }
      const result = creating
        ? await edictumApi.create(payload)
        : await edictumApi.update(name, { params: payload.params, plugins: [] }, overwriteRevision ?? revision);
      const config = result.config || await edictumApi.get(name);
      acceptEdictumConfig(name, config);
      setRevision(result.revision ?? config.revision);
      setBaseline(JSON.stringify(flow));
      dirtyRef.current = false;
      toast.success(creating ? '流程已创建' : '流程已保存', checked && checked.issues.length ? `另有 ${checked.issues.length} 条提示` : undefined);
      if (creating) navigate(`/agents/${encodeURIComponent(name)}/flow`, { replace: true });
      if (isRunning) {
        try {
          const applied = await application.apply(createEdictumApplication('full', { config_name: name }), 'full');
          if (!applied.ok) toast.warning('流程已保存, 但后端热加载失败');
        } catch (error) {
          if (!(error instanceof ApplicationUnconfirmedError && error.code === 'query_stopped')) toast.warning('流程已保存, 运行时应用未确认', errorText(error));
        }
      }
    } catch (error) {
      if (error instanceof RevisionConflictError) setConflict(error);
      else toast.error('保存失败', errorText(error));
    } finally {
      setSaving(false);
    }
  }, [acceptEdictumConfig, application, askConfirm, creating, isRunning, loaded.config, meta.models, name, navigate, revision, saving, search, types, validateNow]);

  const resolveConflict = useCallback(async (mode: 'reload' | 'overwrite') => {
    const current = conflict;
    setConflict(null);
    if (mode === 'reload') { dirtyRef.current = false; await reload(); return; }
    const latest = current?.revision ?? (await edictumApi.get(name)).revision;
    await save(latest);
  }, [conflict, name, reload, save]);

  const ui = useMemo<FlowUi>(() => ({
    issues, running: new Set(), types, portTypes: meta.port_types, defaults, pluginDefaults, analysis, nodes, edges, connecting,
    kinds: KIND_VALUES[kinds],
  }), [analysis, connecting, defaults, edges, issues, kinds, meta.port_types, nodes, pluginDefaults, types]);

  const onMoveEnd = useCallback((_: unknown, viewport: Viewport) => setFlowViewport(name, viewport), [name, setFlowViewport]);

  return (
    <FlowUiContext.Provider value={ui}>
      <div className="flow-editor flex h-[calc(100vh-7.5rem)] min-h-[560px] flex-col gap-3">
        <header className="surface relative z-20 flex flex-none flex-wrap items-center gap-3 !py-2.5 px-4">
          <Link to={`/agents?edictum=${encodeURIComponent(name)}`} className="text-fg-3 hover:text-fg" aria-label="返回 Agent 配置"><ArrowLeft className="h-4 w-4" /></Link>
          <div className="min-w-0">
            <div className="flex items-center gap-2 text-[11px] text-fg-3">Agent 配置 <span>/</span> <span className="font-mono">{name}</span></div>
            <div className="flex items-center gap-2">
              <h1 className="text-[16px] font-semibold text-fg">流程编辑</h1>
              <Badge tone="purple">flow v3</Badge>
              {creating && <Badge tone="teal">新建</Badge>}
              {dirty && <Badge tone="orange" dot>未保存</Badge>}
            </div>
          </div>
          <div className="ml-auto flex flex-wrap items-center gap-2">
            <button type="button" onClick={() => { setSelected(null); setSelectedEdge(null); }} className="flex items-center gap-1.5" aria-label="查看流程检查">
              {validateState === 'unavailable' ? <Badge tone="orange" dot>检查不可用</Badge>
                : errorCount > 0 ? <Badge tone="red" dot>{errorCount} 个错误</Badge> : <Badge tone="green" dot>无错误</Badge>}
              {warnCount > 0 && <Badge tone="orange">{warnCount} 条提示</Badge>}
            </button>
            <div className="w-[110px]">
              <Select value={kinds} onChange={(value) => setKinds(value as Kinds)} title="会话会用在哪些对话类型上, 影响检查规则"
                options={[{ value: 'group', label: '群聊' }, { value: 'private', label: '私聊' }, { value: 'both', label: '群聊和私聊' }]} />
            </div>
            <div className="w-[150px]">
              <Select value={platformId} onChange={setPlatformId} title="选择平台后检查后台发送能力"
                options={[{ value: '', label: '不限平台' }, ...platforms.map((item) => ({ value: item.id, label: item.id, description: item.type }))]} />
            </div>
            <Button size="sm" variant="ghost" icon={<Undo2 className="h-4 w-4" />} onClick={undo} disabled={!history.current.canUndo} aria-label="撤销">撤销</Button>
            <Button size="sm" variant="ghost" icon={<Redo2 className="h-4 w-4" />} onClick={redo} disabled={!history.current.canRedo} aria-label="重做">重做</Button>
            <Button size="sm" variant="ghost" icon={<LayoutTemplate className="h-4 w-4" />} onClick={() => setShowTemplates(true)}>模板</Button>
            <Button size="sm" variant="ghost" icon={<Settings2 className="h-4 w-4" />} onClick={() => setShowSettings(true)}>流程设置</Button>
            <Button size="sm" variant="ghost" icon={<FileUp className="h-4 w-4" />} onClick={() => fileInput.current?.click()}>导入</Button>
            <input
              ref={fileInput} type="file" accept=".json,application/json" className="hidden" aria-label="选择流程 JSON 文件"
              onChange={(event) => { const file = event.target.files?.[0]; event.target.value = ''; if (file) void importFile(file); }}
            />
            <Button size="sm" icon={<FileJson className="h-4 w-4" />} onClick={() => { setJsonDraft(null); setShowJson(true); }}>查看配置</Button>
            <Button size="sm" variant="primary" icon={<Save className="h-4 w-4" />} loading={saving} onClick={() => void save()}>{creating ? '创建' : '保存'}</Button>
          </div>
        </header>

        {application.view && (
          <RuntimeApplicationProgress
            id={application.view.application.id}
            label={application.view.application.label}
            snapshot={application.view.snapshot}
            resultOk={application.view.result?.ok}
            busy={application.view.busy}
            error={application.view.error}
            onQuery={() => void application.query()}
          />
        )}

        <div className="flex min-h-0 flex-1 gap-3">
          <aside className="surface scroll w-[188px] flex-none !p-3"><Palette meta={meta} counts={counts} onAdd={(type) => addNode(type)} /></aside>
          <main
            className="surface relative min-w-0 flex-1 overflow-hidden !p-0" aria-label="流程画布"
            onDragOver={(event) => {
              event.preventDefault();
              const files = event.dataTransfer.types.includes('Files');
              event.dataTransfer.dropEffect = files ? 'copy' : 'move';
              if (files !== fileOver) setFileOver(files);
            }}
            onDragLeave={(event) => { if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setFileOver(false); }}
            onDrop={onDrop}
          >
            <ReactFlow<FlowNode, FlowEdge>
              nodes={nodes} edges={shownEdges} nodeTypes={nodeTypes}
              onNodesChange={onNodesChange} onEdgesChange={onEdgesChange}
              onBeforeDelete={async () => { record(); return true; }}
              onNodesDelete={(removed) => { if (removed.some((node) => node.id === selected)) setSelected(null); }}
              onEdgesDelete={() => setSelectedEdge(null)}
              onNodeDragStart={() => record()}
              onConnect={onConnect} onConnectStart={onConnectStart} onConnectEnd={() => setConnecting(null)} isValidConnection={isValidConnection}
              onNodeClick={(_, node) => { setSelected(node.id); setSelectedEdge(null); setFocusField(null); }}
              onPaneClick={() => { setSelected(null); setSelectedEdge(null); }}
              onEdgeClick={(_, edge) => { setSelectedEdge(edge.id); setSelected(null); }}
              onEdgeDoubleClick={(_, edge) => { setSelectedEdge(edge.id); setSelected(null); setEditLabel((value) => value + 1); }}
              onMoveEnd={onMoveEnd}
              deleteKeyCode={['Delete', 'Backspace']}
              {...(savedViewport ? { defaultViewport: savedViewport } : { fitView: true, fitViewOptions: { padding: 0.12 } })}
              minZoom={0.3} maxZoom={1.6}
              proOptions={{ hideAttribution: true }} colorMode={theme}
            >
              <Background variant={BackgroundVariant.Dots} gap={22} size={1.2} color={theme === 'dark' ? 'rgba(255,255,255,0.09)' : 'rgba(15,23,42,0.12)'} />
              <Controls showInteractive={false} position="bottom-left" />
              <MiniMap pannable zoomable position="top-right" style={{ width: 150, height: 100 }}
                nodeColor={(node) => { const def = types.get((node.data as NodeData).type); return def ? `rgb(var(--rgb-${nodeTint(def)}))` : 'rgb(var(--rgb-red))'; }}
                maskColor={theme === 'dark' ? 'rgba(10,10,14,0.6)' : 'rgba(240,242,247,0.6)'} />
            </ReactFlow>
            {nodes.length === 0 && !fileOver && (
              <div className="pointer-events-none absolute inset-0 grid place-items-center">
                <p className="text-[13px] text-fg-3">从左侧拖入节点, 点击"模板"选择一个起点, 或拖入流程 JSON 文件</p>
              </div>
            )}
            {fileOver && (
              <div className="pointer-events-none absolute inset-2 z-10 grid place-items-center rounded-xl border-2 border-dashed border-accent bg-[rgb(var(--rgb-accent)/0.08)]">
                <p className="rounded-full border border-line bg-[var(--glass-bg-strong)] px-4 py-1.5 text-[13px] font-medium text-fg shadow-[var(--shadow-1)]">松开以导入流程 JSON, 将替换当前画布 (可撤销)</p>
              </div>
            )}
            {connecting && (
              <div className="pointer-events-none absolute left-1/2 top-3 -translate-x-1/2 rounded-full border border-line bg-[var(--glass-bg-strong)] px-3 py-1 text-[12px] text-fg-2 shadow-[var(--shadow-1)]">
                正在连接 <span style={{ color: `rgb(${PORT_STYLE[connecting].rgb})` }}>{portLabel(connecting)}</span>, 只有接受该类型的端口可以放下
              </div>
            )}
          </main>
          <aside className="surface scroll w-[360px] flex-none !p-4" aria-label="检查器">
            {saveErrors.length > 0 && !selectedNode && !edgeSel && (
              <div role="alert" className="mb-4 rounded-lg border border-[rgb(var(--rgb-red)/0.35)] bg-[rgb(var(--rgb-red)/0.08)] px-3 py-2 text-[12px] text-red">
                保存被阻止: {saveErrors.length} 个错误需要修复
              </div>
            )}
            <Inspector
              edge={edgeSel} edgeType={edgeSel ? edgeTypes.get(edgeSel.id) ?? null : null} reading={edgeSel ? readings.get(edgeSel.id) ?? null : null} focusLabel={editLabel}
              node={selectedNode} focusField={focusField} nodes={nodes} types={types} portLabel={portLabel}
              issues={issues} validateState={validateState} defaults={defaults} pluginDefaults={pluginDefaults} analysis={analysis}
              plugins={plugins} models={meta.models} kinds={KIND_VALUES[kinds]}
              onEdgeLabel={setEdgeLabel} onEdgeDelete={deleteEdge} onPatch={patchNode} onConfig={setConfig} onReset={resetConfig}
              onDelete={deleteNode} onSelect={(id) => focusNode(id)} onFocusIssue={focusIssue} onPromote={promote}
            />
          </aside>
        </div>
      </div>

      <Modal
        open={showJson} onClose={() => setShowJson(false)} title="配置 JSON" icon={<FileJson className="h-4 w-4" />} size="lg"
        description={jsonDraft === null ? '命名配置 params.flow 的内容; 节点只保存显式设置的字段'
          : '粘贴 params.flow, 完整命名配置, 或含一个流程的配置文件; 应用后替换画布, 可撤销'}
        footer={jsonDraft === null
          ? <Button size="sm" icon={<FileUp className="h-4 w-4" />} onClick={() => setJsonDraft(JSON.stringify(definition, null, 2))}>编辑并替换</Button>
          : <>
            <Button size="sm" variant="ghost" onClick={() => setJsonDraft(null)}>取消</Button>
            <Button size="sm" variant="primary" disabled={!jsonDraft.trim()} onClick={() => { if (importFlow(jsonDraft, ' JSON')) setJsonDraft(null); }}>替换画布</Button>
          </>}
      >
        {jsonDraft === null
          ? <pre className="scroll max-h-[60vh] rounded-lg border border-line bg-[rgb(0_0_0/0.25)] p-3 font-mono text-[11px] leading-5 text-fg-2">{JSON.stringify(definition, null, 2)}</pre>
          : <Textarea
            data-autofocus aria-label="流程 JSON" spellCheck={false} value={jsonDraft} onChange={(event) => setJsonDraft(event.target.value)}
            rows={1} className="!h-[56vh] !resize-none font-mono !text-[11px] !leading-5"
          />}
      </Modal>

      <Modal open={showTemplates} onClose={() => setShowTemplates(false)} title="选择模板" description={nodes.length ? '应用模板会替换当前画布, 可撤销' : '从模板或空白画布开始'} icon={<LayoutTemplate className="h-4 w-4" />}>
        <div className="space-y-1.5">
          {TEMPLATES.map((template) => {
            const missing = template.nodePlugins.filter((plugin) => !meta.node_types.some((def) => def.plugin === plugin));
            return (
              <button key={template.id} type="button" data-template={template.id} onClick={() => applyTemplate(template.id)}
                className="w-full rounded-lg border border-line px-3 py-2.5 text-left hover:bg-glass-hover">
                <div className="text-[13px] font-medium text-fg">{template.label}</div>
                <div className="text-[12px] text-fg-3">{template.description}</div>
                {missing.length > 0 && <div className="mt-0.5 text-[11px] text-orange">未安装 {missing.join(', ')}, 对应节点会省略</div>}
              </button>
            );
          })}
        </div>
      </Modal>

      <FlowSettings
        open={showSettings} onClose={() => setShowSettings(false)} types={types} defaults={defaults} budget={budget}
        onDefaults={(next) => { record(); setDefaults(next); }} onBudget={(next) => { record('budget'); setBudget(next); }}
      />

      <Modal
        open={conflict !== null} onClose={() => setConflict(null)} title="配置已被别处修改" size="sm" className="tint-orange"
        footer={<>
          <Button variant="ghost" onClick={() => void resolveConflict('reload')}>重新加载</Button>
          <Button variant="danger" onClick={() => void resolveConflict('overwrite')}>覆盖</Button>
        </>}
      >
        <p className="text-[13px] leading-6 text-fg-2">
          {conflict?.message}。重新加载会丢弃本地修改; 覆盖会用当前画布替换别处保存的版本。
        </p>
      </Modal>

      <Modal
        open={confirmPrompt !== null} onClose={() => settleConfirm(false)} title="确认操作" size="sm" className="tint-orange"
        footer={<>
          <Button variant="ghost" onClick={() => settleConfirm(false)}>取消</Button>
          <Button variant="danger" onClick={() => settleConfirm(true)}>确认</Button>
        </>}
      >
        <p className="whitespace-pre-wrap break-words text-[13px] leading-6 text-fg-2">{confirmPrompt?.message}</p>
      </Modal>
    </FlowUiContext.Provider>
  );
}

export function FlowEditor() {
  const { name = '' } = useParams();
  const [search] = useSearchParams();
  const wantsCreate = search.get('create') === '1';
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const [error, setError] = useState('');
  const [generation, setGeneration] = useState(0);

  const load = useCallback(async () => {
    setError('');
    try {
      const [types, plugins, platforms, config] = await Promise.all([
        edictumApi.listTypes(),
        edictumApi.listPlugins(),
        controlApi.listPlatforms().then((result) => result.platforms || []).catch(() => [] as PlatformConfig[]),
        edictumApi.get(name).catch((reason: unknown) => {
          if (wantsCreate) return null;
          throw reason;
        }),
      ]);
      const meta = types.find((item) => item.name === 'flow')?.flow;
      if (!meta) throw new Error('后端没有提供 flow 类型的节点元数据');
      if (config && config.edictum_type !== 'flow') throw new Error(`配置 ${name} 的类型是 ${config.edictum_type}, 不是流程`);
      if (config && wantsCreate) throw new Error(`配置 ${name} 已存在`);
      const pluginDefaults = await loadPluginDefaults(meta);
      setLoaded({ meta, plugins, pluginDefaults, platforms, config });
      setGeneration((value) => value + 1);
    } catch (reason) {
      setError(errorText(reason, '读取流程失败'));
    }
  }, [name, wantsCreate]);

  useEffect(() => { void load(); }, [load]);

  if (error) {
    return (
      <EmptyState
        icon={<Workflow className="h-5 w-5" />} title="无法打开流程编辑器" description={error}
        actions={<Link to="/agents" className="text-accent hover:underline">返回 Agent 配置</Link>}
      />
    );
  }
  if (!loaded) return <Skeleton className="h-[calc(100vh-7.5rem)] w-full rounded-2xl" />;
  return (
    <ReactFlowProvider key={generation}>
      <Editor name={name} creating={!loaded.config} loaded={loaded} reload={load} />
    </ReactFlowProvider>
  );
}
