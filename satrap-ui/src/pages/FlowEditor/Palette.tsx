import type { DragEvent, ReactNode } from 'react';
import { Puzzle } from 'lucide-react';
import type { FlowNodeTypePayload, FlowPortType, FlowTypeMetadata } from '@/api/types';
import { cn } from '@/utils/cn';
import { nodeIcon, nodeTint, PORT_STYLE } from './portStyle';

/** 拖放到画布时 dataTransfer 使用的类型 */
export const DRAG_MIME = 'application/satrap-flow-node';

/** 内置分类的显示顺序; 未列出的分类按出现顺序排在后面 */
const CATEGORY_ORDER = ['Workflow', '输出', '触发', '条件', '逻辑', '数据', '其他'];

function Item({ def, disabled, reason, onAdd }: { def: FlowNodeTypePayload; disabled: boolean; reason: string; onAdd: (type: string) => void }) {
  const Icon = nodeIcon(def);
  return (
    <button
      type="button" draggable={!disabled} disabled={disabled} data-palette={def.type}
      onDragStart={(event: DragEvent) => { event.dataTransfer.setData(DRAG_MIME, def.type); event.dataTransfer.effectAllowed = 'move'; }}
      onClick={() => onAdd(def.type)}
      title={disabled ? reason : `${def.description}\n拖到画布, 或点击添加`}
      className={cn(`tint-${nodeTint(def)}`, 'flex w-full cursor-grab items-center gap-2 rounded-lg border border-transparent px-2 py-1.5 text-left text-[13px] text-fg-2 transition-colors hover:border-[rgb(var(--tint)/0.3)] hover:bg-[rgb(var(--tint)/0.1)] hover:text-fg active:cursor-grabbing disabled:cursor-not-allowed disabled:opacity-40')}
    >
      <span className="grid h-6 w-6 flex-none place-items-center rounded-md bg-[rgb(var(--tint)/0.16)] text-tint"><Icon className="h-3.5 w-3.5" /></span>
      <span className="min-w-0 truncate">{def.label}</span>
    </button>
  );
}

function Group({ title, children }: { title: ReactNode; children: ReactNode }) {
  return (
    <div>
      <div className="mb-1.5 flex items-center gap-1 px-1 text-[11px] font-semibold uppercase tracking-wide text-fg-3">{title}</div>
      <div className="space-y-1">{children}</div>
    </div>
  );
}

export function Palette({ meta, counts, onAdd }: {
  meta: FlowTypeMetadata; counts: Map<string, number>; onAdd: (type: string) => void;
}) {
  const builtin = meta.node_types.filter((def) => !def.plugin);
  const pluginNodes = meta.node_types.filter((def) => def.plugin);
  const categories = [...new Set(builtin.map((def) => def.category))]
    .sort((a, b) => {
      const ia = CATEGORY_ORDER.indexOf(a), ib = CATEGORY_ORDER.indexOf(b);
      return (ia < 0 ? CATEGORY_ORDER.length : ia) - (ib < 0 ? CATEGORY_ORDER.length : ib);
    });
  const plugins = [...new Set(pluginNodes.map((def) => def.plugin!))];
  const item = (def: FlowNodeTypePayload) => {
    const full = def.maxPerFlow !== undefined && (counts.get(def.type) ?? 0) >= def.maxPerFlow;
    return <Item key={def.type} def={def} disabled={full} reason={`每个流程最多 ${def.maxPerFlow} 个`} onAdd={onAdd} />;
  };
  return (
    <div className="space-y-4" aria-label="节点面板">
      {categories.map((category) => (
        <Group key={category} title={category === '触发' ? '触发磁贴' : category === '逻辑' ? '逻辑门' : category}>
          {builtin.filter((def) => def.category === category).map(item)}
        </Group>
      ))}
      {plugins.length > 0 && (
        <Group title={<><Puzzle className="h-3 w-3" />插件节点</>}>
          <div className="space-y-2">
            {plugins.map((plugin) => (
              <div key={plugin}>
                <div className="px-1 font-mono text-[10px] text-fg-4">{plugin}</div>
                {pluginNodes.filter((def) => def.plugin === plugin).map(item)}
              </div>
            ))}
          </div>
        </Group>
      )}
      <div className="space-y-1 rounded-lg border border-line p-2.5 text-[11px] leading-4 text-fg-3">
        <div className="mb-1 text-fg-4">连线类型</div>
        {(Object.keys(PORT_STYLE) as FlowPortType[]).map((type) => (
          <div key={type} className="flex items-center gap-2" title={PORT_STYLE[type].desc}>
            <span className="h-0 w-5 border-t-2" style={{ borderColor: `rgb(${PORT_STYLE[type].rgb})`, borderTopStyle: PORT_STYLE[type].dash ? 'dashed' : 'solid' }} />
            {meta.port_types[type]?.label ?? type}
          </div>
        ))}
      </div>
    </div>
  );
}
