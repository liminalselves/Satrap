import type { FlowDefinitionJSON } from '@/api/types';
import { ARCHIVE_VERSION, splitEndpoint, type TypeIndex } from './flowModel';

/** 导入文件的大小上限; 正常流程存档只有几十 KB */
export const IMPORT_MAX_BYTES = 2 * 1024 * 1024;

/** 解析出的导入内容 */
export interface FlowImport {
  /** 要放到画布上的 params.flow */
  definition: FlowDefinitionJSON;
  /** 内容来自哪一层, 用于提示, 如 "命名配置 rp" */
  origin: string;
}

const isObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

/** 从命名配置里取 params.flow; 不是流程类型时报错 */
function fromNamedConfig(config: Record<string, unknown>, name?: string): FlowImport {
  const label = name ? `命名配置 ${name}` : '命名配置';
  if (config.edictum_type !== undefined && config.edictum_type !== 'flow') {
    throw new Error(`${label} 的类型是 ${String(config.edictum_type)}, 不是流程`);
  }
  const flow = isObject(config.params) ? config.params.flow : undefined;
  if (!isObject(flow)) throw new Error(`${label} 里没有 params.flow`);
  return { definition: checkDefinition(flow), origin: label };
}

/**
 * 只检查结构: 画布转换需要的字段齐全且类型正确
 *
 * 节点类型是否存在, 端口能否相连等语义问题交给后端检查, 导入后照常显示在检查面板
 */
function checkDefinition(flow: Record<string, unknown>): FlowDefinitionJSON {
  const version = flow.version;
  if (version !== undefined && (typeof version !== 'number' || !Number.isInteger(version))) throw new Error('version 必须是整数');
  if (typeof version === 'number' && version > ARCHIVE_VERSION) throw new Error(`流程格式版本 ${version} 高于编辑器支持的 ${ARCHIVE_VERSION}`);
  if (!Array.isArray(flow.nodes)) throw new Error('缺少 nodes 数组');
  const ids = new Set<string>();
  flow.nodes.forEach((node, index) => {
    if (!isObject(node)) throw new Error(`nodes[${index}] 不是对象`);
    if (typeof node.id !== 'string' || !node.id) throw new Error(`nodes[${index}] 缺少 id`);
    if (typeof node.type !== 'string' || !node.type) throw new Error(`节点 ${node.id} 缺少 type`);
    if (node.config !== undefined && !isObject(node.config)) throw new Error(`节点 ${node.id} 的 config 不是对象`);
    if (node.title !== undefined && typeof node.title !== 'string') throw new Error(`节点 ${node.id} 的 title 不是字符串`);
    if (node.version !== undefined && typeof node.version !== 'number') throw new Error(`节点 ${node.id} 的 version 不是数字`);
    if (ids.has(node.id)) throw new Error(`节点 id 重复: ${node.id}`);
    ids.add(node.id);
  });
  if (flow.edges !== undefined && !Array.isArray(flow.edges)) throw new Error('edges 不是数组');
  (flow.edges ?? []).forEach((edge, index) => {
    if (!isObject(edge) || typeof edge.from !== 'string' || typeof edge.to !== 'string') throw new Error(`edges[${index}] 需要字符串 from 和 to`);
    for (const endpoint of [edge.from, edge.to]) {
      const [node] = splitEndpoint(endpoint);
      if (!ids.has(node)) throw new Error(`连线 ${edge.from} -> ${edge.to} 引用了不存在的节点 ${node}`);
    }
  });
  for (const key of ['defaults', 'layout'] as const) {
    if (flow[key] !== undefined && !isObject(flow[key])) throw new Error(`${key} 不是对象`);
  }
  if (flow.budget !== undefined && flow.budget !== null && !isObject(flow.budget)) throw new Error('budget 不是对象');
  return flow as unknown as FlowDefinitionJSON;
}

/**
 * 解析用户导入的 JSON 文本
 *
 * 接受三种形状:
 * - params.flow 本身 (有 nodes 数组)
 * - 一个命名配置 (有 params.flow, 即 "查看配置" 之外的完整配置对象)
 * - 命名配置文件 (配置名 -> 命名配置), 其中恰好一个流程配置
 *
 * 返回: 流程定义和来源说明; 无法识别或结构错误时抛出带中文说明的 Error
 */
export function parseFlowImport(text: string): FlowImport {
  let data: unknown;
  try {
    data = JSON.parse(text);
  } catch (error) {
    throw new Error(`不是有效的 JSON: ${error instanceof Error ? error.message : String(error)}`);
  }
  if (!isObject(data)) throw new Error('顶层必须是 JSON 对象');
  if ('nodes' in data) return { definition: checkDefinition(data), origin: 'params.flow' };
  if ('params' in data || 'edictum_type' in data) return fromNamedConfig(data);
  const flows = Object.entries(data).filter(([, value]) => isObject(value) && value.edictum_type === 'flow');
  if (flows.length === 1) {
    const [name, config] = flows[0];
    return fromNamedConfig(config as Record<string, unknown>, name);
  }
  if (flows.length > 1) throw new Error(`文件里有 ${flows.length} 个流程配置 (${flows.map(([name]) => name).join(', ')}), 请只导入其中一个`);
  throw new Error('无法识别: 需要 params.flow, 命名配置, 或含一个流程的配置文件');
}

/** 导入内容里当前宿主不认识的节点类型 (多为未安装的插件节点) */
export function unknownNodeTypes(definition: FlowDefinitionJSON, types: TypeIndex): string[] {
  return [...new Set(definition.nodes.map((node) => node.type).filter((type) => !types.has(type)))];
}
