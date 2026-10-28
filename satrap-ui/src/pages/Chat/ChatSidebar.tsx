import { ChevronDown, Folder, FolderInput, FolderPlus, MessageSquarePlus, Plus, Search, Trash2 } from 'lucide-react';
import type { ProjectItem } from '@/api/chat';
import { cn } from '@/utils/cn';
import type { Conversation } from './types';
import { Select as KitSelect } from '@/components/kit/Select';

export type GlassColor = 'accent' | 'purple' | 'teal' | 'pink' | 'orange' | 'green';

export const CONVERSATION_COLORS: GlassColor[] = ['accent', 'purple', 'teal', 'pink', 'orange', 'green'];

const WEEKDAYS = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];

/** 侧栏短时间: 今天显示时刻, 昨天/一周内显示相对日, 更早显示月日 */
function formatShortTime(timestamp: number): string {
  const date = new Date(timestamp * 1000);
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const days = Math.floor((today.getTime() - new Date(date).setHours(0, 0, 0, 0)) / 86_400_000);
  if (days <= 0) return `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`;
  if (days === 1) return '昨天';
  if (days < 7) return WEEKDAYS[date.getDay()];
  return `${date.getMonth() + 1}/${date.getDate()}`;
}

export interface GroupedConversations {
  groups: { project: ProjectItem; conversations: Conversation[] }[];
  recent: Conversation[];
  colorIndex: Map<string, number>;
  total: number;
}

/** 会话列表项: 标题保持 div.text-sm.truncate 两层于行容器之下 (e2e 依赖) */
function ConversationRow({
  conversation,
  active,
  onSelect,
  onDelete,
  onMove,
}: {
  conversation: Conversation;
  /** 保留给调用方; 选中指示条统一使用主题色 */
  color?: GlassColor;
  active: boolean;
  onSelect: () => void;
  onDelete: () => void;
  onMove?: () => void;
}) {
  return (
    <div
      onClick={onSelect}
      aria-current={active ? 'true' : undefined}
      className={cn(
        'group relative flex h-9 w-full cursor-pointer items-center rounded-[10px] px-2.5 text-left transition-colors',
        active ? 'bg-[rgb(var(--rgb-accent)/0.14)] text-fg' : 'text-fg-2 hover:bg-glass-hover hover:text-fg',
      )}
    >
      {active && <span className="absolute left-0 top-1/2 h-4 w-[3px] -translate-y-1/2 rounded-r-full bg-accent" />}
      <div className={cn('flex min-w-0 flex-1 items-center gap-2', onMove ? 'group-focus-within:pr-12 group-hover:pr-12' : 'group-focus-within:pr-6 group-hover:pr-6')}>
        <div className="min-w-0 flex-1 truncate text-sm !text-[13px]">{conversation.title}</div>
        <span className="num flex-none text-[11px] text-fg-4 group-hover:hidden group-focus-within:hidden">{formatShortTime(conversation.updatedAt)}</span>
      </div>
      <span className="absolute inset-y-0 right-1.5 flex items-center gap-0.5 opacity-0 transition-opacity group-focus-within:opacity-100 group-hover:opacity-100">
        {onMove && (
          <button
            type="button"
            onClick={(event) => { event.stopPropagation(); onMove(); }}
            title="移入/移出项目"
            aria-label="移入/移出项目"
            className="rounded-md p-1 text-fg-4 transition-colors hover:bg-glass-hover hover:text-accent"
          >
            <FolderInput className="h-3.5 w-3.5" />
          </button>
        )}
        <button
          type="button"
          onClick={(event) => { event.stopPropagation(); onDelete(); }}
          title="删除对话"
          aria-label="删除对话"
          className="rounded-md p-1 text-fg-4 transition-colors hover:bg-glass-hover hover:text-red"
        >
          <Trash2 className="h-3.5 w-3.5" />
        </button>
      </span>
    </div>
  );
}

/** 项目分组: 项目名两层之下为分组容器 (e2e 依赖), 根路径放在 title 上 */
function ProjectGroup({
  project,
  conversations,
  collapsed,
  activeId,
  colorIndex,
  onToggle,
  onNewConversation,
  onDeleteProject,
  onSelect,
  onDelete,
  onMove,
}: {
  project: ProjectItem;
  conversations: Conversation[];
  collapsed: boolean;
  activeId: string;
  colorIndex: Map<string, number>;
  onToggle: () => void;
  onNewConversation: () => void;
  onDeleteProject: () => void;
  onSelect: (id: string) => void;
  onDelete: (id: string) => void;
  onMove: (id: string) => void;
}) {
  return (
    <div className="mb-1">
      <div
        onClick={onToggle}
        className="group flex w-full cursor-pointer items-center gap-2 rounded-[10px] px-2.5 py-1.5 text-left transition hover:bg-glass-hover"
      >
        <ChevronDown className={cn('h-3.5 w-3.5 flex-none text-fg-4 transition-transform duration-300', collapsed && '-rotate-90')} />
        <Folder className="h-3.5 w-3.5 flex-none text-orange" />
        <span className="flex-1 truncate text-left text-[13px] font-medium text-fg-2" title={project.root_path}>
          {project.name}
        </span>
        <span className="num flex-none text-[11px] text-fg-4 group-hover:hidden">{conversations.length}</span>
        <button
          type="button"
          onClick={(event) => { event.stopPropagation(); onNewConversation(); }}
          title="在此项目下新建对话"
          aria-label="在此项目下新建对话"
          className="rounded-md p-1 text-fg-4 opacity-0 transition-all hover:bg-glass-hover hover:text-accent group-hover:opacity-100 focus-visible:opacity-100"
        >
          <Plus className="h-3.5 w-3.5" />
        </button>
        <button
          type="button"
          onClick={(event) => { event.stopPropagation(); onDeleteProject(); }}
          title="删除项目 (仅解绑会话, 磁盘文件不动)"
          aria-label="删除项目 (仅解绑会话, 磁盘文件不动)"
          className="rounded-md p-1 text-fg-4 opacity-0 transition-all hover:bg-glass-hover hover:text-red group-hover:opacity-100 focus-visible:opacity-100"
        >
          <Trash2 className="h-3.5 w-3.5" />
        </button>
      </div>
      <div
        className={cn(
          'grid transition-all duration-400 [transition-timing-function:var(--ease-out)]',
          collapsed ? 'grid-rows-[0fr] opacity-0' : 'grid-rows-[1fr] opacity-100',
        )}
      >
        <div className="overflow-hidden">
          <div className="ml-[14px] space-y-0.5 border-l border-line pl-1.5 pt-0.5">
            {conversations.length === 0 ? (
              <div className="px-2 py-1 text-xs text-fg-4">当前列表暂无对话</div>
            ) : (
              conversations.map((conv) => (
                <ConversationRow
                  key={conv.id}
                  conversation={conv}
                  color={CONVERSATION_COLORS[(colorIndex.get(conv.id) ?? 0) % CONVERSATION_COLORS.length]}
                  active={conv.id === activeId}
                  onSelect={() => onSelect(conv.id)}
                  onDelete={() => onDelete(conv.id)}
                  onMove={conv.id === '__draft__' || conv.preloaded ? undefined : () => onMove(conv.id)}
                />
              ))
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

export function ChatSidebar({
  search,
  onSearch,
  projectFilter,
  onProjectFilter,
  projects,
  grouped,
  activeId,
  directoryLoading,
  pagingError,
  conversationsCount,
  nextConversations,
  onLoadMore,
  collapsedProjects,
  onToggleProject,
  onCreateProject,
  onNew,
  onNewInProject,
  onDeleteProject,
  onSelect,
  onDelete,
  onMove,
}: {
  search: string;
  onSearch: (value: string) => void;
  projectFilter: string;
  onProjectFilter: (value: string) => void;
  projects: ProjectItem[];
  grouped: GroupedConversations;
  activeId: string;
  directoryLoading: boolean;
  pagingError: string;
  conversationsCount: number;
  nextConversations: string | null;
  onLoadMore: () => void;
  collapsedProjects: Record<string, boolean>;
  onToggleProject: (projectId: string) => void;
  onCreateProject: () => void;
  onNew: () => void;
  onNewInProject: (projectId: string) => void;
  onDeleteProject: (projectId: string) => void;
  onSelect: (id: string) => void;
  onDelete: (id: string) => void;
  onMove: (id: string) => void;
}) {
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="space-y-2 p-3">
        <button type="button" onClick={onNew} className="ctl ctl-primary w-full">
          <MessageSquarePlus className="h-4 w-4" />
          新建对话
        </button>
        <div className="flex gap-1.5">
          <div className="relative min-w-0 flex-1">
            <Search className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-fg-4" />
            <input
              value={search}
              onChange={(event) => onSearch(event.target.value)}
              placeholder="搜索对话"
              aria-label="搜索对话"
              className="field !h-8 !pl-8 text-[13px]"
            />
          </div>
          <button
            type="button"
            title="新建项目 (绑定工作区文件夹)"
            aria-label="新建项目 (绑定工作区文件夹)"
            onClick={onCreateProject}
            className="ctl ctl-ghost ctl-icon ctl-sm !h-8 !w-8"
          >
            <FolderPlus className="h-4 w-4" />
          </button>
        </div>
        <KitSelect
          aria-label="筛选对话项目"
          className="field !h-8 text-[13px]"
          value={projectFilter}
          onChange={(value) => onProjectFilter(value)}
        >
          <option value="">全部项目</option>
          {projects.map((project) => (
            <option key={project.project_id} value={project.project_id}>{project.name}</option>
          ))}
        </KitSelect>
      </div>

      <div className="scroll fade-edges min-h-0 flex-1 px-2 pb-3 pt-3.5">
        {directoryLoading && <p className="px-2 py-1 text-xs text-fg-3">正在查询对话…</p>}
        {!directoryLoading && grouped.total === 0 && grouped.groups.length === 0 ? (
          <div className="py-10 text-center text-sm text-fg-3">
            {pagingError ? '对话查询未完成, 请重试' : conversationsCount === 0 && projects.length === 0 ? '暂无对话, 点击上方新建' : '无匹配对话'}
          </div>
        ) : (
          <>
            {grouped.groups.length > 0 && (
              <div>
                <div className="flex items-center justify-between px-2.5 pb-1 pt-2 text-[11px] font-semibold uppercase tracking-[0.08em] text-fg-4">
                  项目
                </div>
                {grouped.groups.map((group) => (
                  <ProjectGroup
                    key={group.project.project_id}
                    project={group.project}
                    conversations={group.conversations}
                    collapsed={!!collapsedProjects[group.project.project_id]}
                    activeId={activeId}
                    colorIndex={grouped.colorIndex}
                    onToggle={() => onToggleProject(group.project.project_id)}
                    onNewConversation={() => onNewInProject(group.project.project_id)}
                    onDeleteProject={() => onDeleteProject(group.project.project_id)}
                    onSelect={onSelect}
                    onDelete={onDelete}
                    onMove={onMove}
                  />
                ))}
              </div>
            )}
            {grouped.recent.length > 0 && (
              <div>
                {grouped.groups.length > 0 && (
                  <div className="px-2.5 pb-1 pt-4 text-[11px] font-semibold uppercase tracking-[0.08em] text-fg-4">最近</div>
                )}
                {grouped.recent.map((conv) => (
                  <ConversationRow
                    key={conv.id}
                    conversation={conv}
                    color={CONVERSATION_COLORS[(grouped.colorIndex.get(conv.id) ?? 0) % CONVERSATION_COLORS.length]}
                    active={conv.id === activeId}
                    onSelect={() => onSelect(conv.id)}
                    onDelete={() => onDelete(conv.id)}
                    onMove={conv.id === '__draft__' || conv.preloaded ? undefined : () => onMove(conv.id)}
                  />
                ))}
              </div>
            )}
            {nextConversations && !directoryLoading && (
              <button type="button" onClick={onLoadMore} className="ctl ctl-ghost mt-2 w-full">
                加载更多对话
              </button>
            )}
          </>
        )}
      </div>
    </div>
  );
}
