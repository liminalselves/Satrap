import { createContext, useContext } from 'react';
import type { FlowIssue, FlowPortType, FlowTypeMetadata } from '@/api/types';
import type { FlowAnalysis } from './analysis';
import type { ConversationKinds, FlowDefaults, FlowEdge, FlowNode, PluginDefaults, TypeIndex } from './flowModel';

/** 画布节点渲染需要的共享状态; 由编辑器提供, 节点组件只读 */
export interface FlowUi {
  /** 后端检查给出的问题 */
  issues: FlowIssue[];
  /** 运行状态面板叠加时正在运行的 WF */
  running: Set<string>;
  /** 节点类型表 */
  types: TypeIndex;
  /** 端口类型显示名和是否信号端口 */
  portTypes: FlowTypeMetadata['port_types'] | Record<string, { label: string; signal: boolean }>;
  /** 流程默认 */
  defaults: FlowDefaults;
  /** 插件全局默认, 键为完整节点类型名 */
  pluginDefaults: PluginDefaults;
  /** 结构分析, 只用于展示 */
  analysis: FlowAnalysis;
  /** 当前全部节点 */
  nodes: FlowNode[];
  /** 当前全部边 */
  edges: FlowEdge[];
  /** 正在拖线时源端口的类型, 用于高亮可连的端口 */
  connecting: FlowPortType | null;
  /** 顶部选择的对话类型; 唤醒和消息入口的说明随之变化 */
  kinds: ConversationKinds;
  /** 激活链路追踪中要高亮的节点和追踪目标; null 表示没有在追踪 */
  traced: { target: string; nodes: Set<string> } | null;
}

export const FlowUiContext = createContext<FlowUi>({
  issues: [], running: new Set(), types: new Map(), portTypes: {}, defaults: {}, pluginDefaults: {},
  analysis: { workflows: new Map(), drives: new Map() }, nodes: [], edges: [], connecting: null, kinds: ['group'], traced: null,
});

export const useFlowUi = () => useContext(FlowUiContext);
