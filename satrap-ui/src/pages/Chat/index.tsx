import { ApprovalSettings } from './ApprovalSettings';
import { useApprovalMode, type ApprovalControl } from './useApprovalMode';
import { useConfirm } from '@/hooks/useConfirm';
import { useConfigWatch } from '@/hooks/useConfigWatch';
import { useConfigStore } from '@/stores/useConfigStore';
import { conversationBoundaryReached, restoreConversation, sidebarConversationVisible } from './conversations';
import { RunRecovery } from "./RunRecovery";
import { LLMConfigModal } from '@/components/common/LLMConfigModal';
import { RagManager } from '@/components/common/RagManager';
import { MediaAttachment } from './MediaAttachment';
import { useChatScroll } from './useChatScroll';
import { ragApi } from '@/api/rag';
import type { ConfigOption } from '@/components/common/PluginConfigFields';
import { PluginConfigFields, type ModelOptions } from '@/components/common/PluginConfigFields';
import { SessionPluginSettingsModal } from '@/components/common/SessionPluginSettingsModal';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { PluginCapabilities } from '@/components/common/PluginCapabilities';
import { controlApi } from '@/api/control';
import type { GlobalPluginConfig, PluginRuntimeResult } from '@/api/types';
import { PluginRuntimeStatus } from '@/pages/Plugins/GlobalPluginSettings';
import { pluginError } from '@/pages/Plugins/InstallPluginModal';
import { Button } from '@/components/ui/Button';
import { Modal } from '@/components/ui/Modal';
import { Select } from './Select';
import { Toggle } from '@/components/ui/Toggle';
import { toast } from '@/components/ui/Toast';
import { cn } from '@/utils/cn';
import {
  getThinkingOptions,
} from '@/utils/constants';
import { createAnimationFrameLimiter, MAX_UI_FRAME_RATE } from '@/utils/frameLimiter';
import { ThinkDial } from './ThinkDial';
import { useTheme } from '@/hooks/useTheme';
import { useBackendStore } from '@/stores/useBackendStore';
import { useShallow } from 'zustand/react/shallow';
import {
  chatApi,
  subscribeChat,
  type Attachment,
  type ApprovalMode,
  type CapabilityKind,
  type ChatEvent,
  type ChatDefaults,
  type ChatPreloadSettings,
  type ChatPlugin,
  type ChatTurnVariant,
  type ChatTurn,
  type ConversationItem as ChatConversationItem,
  type DirEntry,
  type MemoryRecord,
  type ModelConfigItem,
  type ProjectItem,
  type ToolCall,
} from '@/api/chat';
import { ChatHistoryManager } from './ChatHistoryManager';
import { AskUserPanel, type PendingUserInput } from './AskUserPanel';
import {
  applyStreamingDeltas,
  enqueueStreamingDelta,
  type LocalMessageSegment,
  type StreamingDelta,
} from './streaming';
import { ChatHeader } from './ChatHeader';
import { ChatSidebar } from './ChatSidebar';
import { EmptyWelcome, MessageBubble } from './MessageView';
import type { ChatMessage, Conversation, LocalResponseVariant } from './types';
import {
  Plus,
  Square,
  MessageSquare,
  Trash2,
  Bot,
  Settings2,
  Paperclip,
  ArrowUp,
  ChevronDown,
  Brain,
  Puzzle,
  Pencil,
  Folder,
  FolderOpen,
  HardDrive,
  BookOpen,
  Cpu,
  Database,
  SlidersHorizontal,
  type LucideIcon,
} from 'lucide-react';


// 聊天设置
interface ChatSettings {
  model: string;   // 模型配置名
  think: string;   // 思考强度 (off/low/medium/high)
  temperature: number;   // 采样温度
  commandHistoryEnabled: boolean;   // 是否将已执行命令及回复写入模型上下文
  defaultAgentPromptEnabled: boolean;   // 是否在本会话补充内置 Agent 提示
  maxIterations?: number;   // 最大工具迭代次数, undefined 沿用会话定义默认
  systemPrompt: string;   // 系统提示词
}

const DEFAULT_SETTINGS: ChatSettings = {
  model: 'default',
  think: 'off',
  temperature: 0.7,
  commandHistoryEnabled: false,
  defaultAgentPromptEnabled: false,
  maxIterations: undefined,
  systemPrompt: '',
};

const CHAT_THINK_STORAGE_KEY = 'satrap.chat.think';

function loadPersistedThink(): string {
  try {
    return window.localStorage.getItem(CHAT_THINK_STORAGE_KEY)?.trim() || DEFAULT_SETTINGS.think;
  } catch {
    return DEFAULT_SETTINGS.think;
  }
}

// 生成唯一 id (本地消息用)
const genId = () => `${Date.now()}-${Math.random().toString(36).slice(2, 9)}`;

// 根据首条用户消息生成会话标题
const deriveTitle = (content: string) => {
  const text = content.trim().replace(/\s+/g, ' ');
  return text.length > 24 ? `${text.slice(0, 24)}…` : text || '新对话';
};

function convertSegments(variant: ChatTurnVariant): LocalMessageSegment[] | undefined {
  const errorText = variant.response_metadata?.error_text;
  const errorSegments: LocalMessageSegment[] = errorText ? [{ type: 'content', content: `\n\n[错误] ${errorText}` }] : [];
  if (variant.segments && variant.segments.length > 0) {
    return [...variant.segments.map((segment) => {
      if (segment.type === 'thinking') {
        return { type: 'thinking' as const, content: segment.content ?? '' };
      }
      if (segment.type === 'tool' && segment.tool) {
        return { type: 'tool' as const, tool: segment.tool };
      }
      return { type: 'content' as const, content: segment.content ?? '' };
    }), ...errorSegments];
  }
  const fallback: LocalMessageSegment[] = [];
  if (variant.thinking) fallback.push({ type: 'thinking', content: variant.thinking });
  for (const tool of variant.tool_calls ?? []) fallback.push({ type: 'tool', tool });
  if (variant.answer) fallback.push({ type: 'content', content: variant.answer });
  return [...fallback, ...errorSegments];
}

function variantAnswer(variant: ChatTurnVariant): string {
  const error = variant.response_metadata?.error_text;
  return variant.answer + (error ? `\n\n[错误] ${error}` : '');
}

function convertVariant(variant: ChatTurnVariant): LocalResponseVariant {
  return {
    variantIndex: variant.variant_index,
    content: variantAnswer(variant),
    thinking: variant.thinking ?? undefined,
    toolCalls: variant.tool_calls ?? [],
    segments: convertSegments(variant),
    contextStats: variant.context_stats,
    outputFiles: variant.output_files,
    responseMetadata: variant.response_metadata,
  };
}

function turnsToMessages(turns: ChatTurn[]): ChatMessage[] {
  const messages: ChatMessage[] = [];
  for (const turn of turns) {
    messages.push({
      id: `${turn.id}-u`,
      role: 'user',
      content: turn.user_input,
      timestamp: turn.created_at * 1000,
      attachments: turn.attachments ?? undefined,
      turnIndex: turn.turn_index,
    });
    const currentVariant: ChatTurnVariant = {
      response_metadata: turn.response_metadata,
      variant_index: turn.active_variant ?? 0,
      thinking: turn.thinking,
      answer: turn.answer,
      segments: turn.segments,
      created_at: turn.created_at,
      tool_calls: turn.tool_calls,
      context_stats: turn.context_stats,
      output_files: turn.output_files,
    };
    const segments = convertSegments(currentVariant);
    messages.push({
      id: `${turn.id}-a`,
      responseMetadata: turn.response_metadata,
      role: 'assistant',
      streaming: turn.generating ?? false,
      content: turn.interrupted ? (turn.answer || '[生成已中断]') : variantAnswer(currentVariant),
      thinking: turn.thinking ?? undefined,
      toolCalls: turn.tool_calls,
      timestamp: turn.created_at * 1000,
      turnIndex: turn.turn_index,
      segments,
      activeVariant: turn.active_variant ?? 0,
      variantCount: turn.variant_count ?? 1,
      variantIndices: turn.variant_indices,
      variants: (turn.variants?.length ? turn.variants : [currentVariant]).map(convertVariant),
      outputFiles: turn.output_files,
      contextStats: turn.context_stats,
    });
  }
  return messages;
}

export function Chat() {
  const confirm = useConfirm();
  const [listOpen, setListOpen] = useState(true);
  const [mobileList, setMobileList] = useState(false);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeId, setActiveId] = useState<string>('');
  const [input, setInput] = useState('');
  const [search, setSearch] = useState('');
  const [generating, setGenerating] = useState(false);
  const [pendingUserInputs, setPendingUserInputs] = useState<Record<string, PendingUserInput[]>>({});
  const [pendingUserAnswers, setPendingUserAnswers] = useState<Record<string, string>>({});
  const [answeringRequestId, setAnsweringRequestId] = useState<string | null>(null);
  // 聊天设置与设置弹窗 / 折叠面板
  const [settings, setSettings] = useState<ChatSettings>(() => ({
    ...DEFAULT_SETTINGS,
    think: loadPersistedThink(),
  }));
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [defaults, setDefaults] = useState<ChatDefaults | null>(null);
  const [defaultSaving, setDefaultSaving] = useState(false);
  const [bindingPending, setBindingPending] = useState<Set<string>>(() => new Set());
  const [bindingProblem, setBindingProblem] = useState<string | null>(null);
  const [optionsOpen, setOptionsOpen] = useState(false);
  const [draftApprovalMode, setDraftApprovalMode] = useState<ApprovalMode>('user');
  // 后端连接状态
  const [backendOk, setBackendOk] = useState<boolean | null>(null);
  // 可选模型列表(来自聊天服务)
  const [models, setModels] = useState<string[]>([]);
  const [modelsLoaded, setModelsLoaded] = useState(false);
  // 插件列表(来自聊天服务)
  const [plugins, setPlugins] = useState<ChatPlugin[]>([]);
  // 插件配置保存后递增, 用于主动刷新空的预加载会话
  const [pluginConfigRevision, setPluginConfigRevision] = useState(0);
  // 能力弹窗当前查看的插件名 (null = 关闭)
  const [capabilityPlugin, setCapabilityPlugin] = useState<string | null>(null);
  // 配置弹窗当前查看的插件名 (null = 关闭)
  const [configPlugin, setConfigPlugin] = useState<string | null>(null);
  const [sessionConfigPlugin, setSessionConfigPlugin] = useState<string | null>(null);
  // 记忆面板开关
  const [memoryOpen, setMemoryOpen] = useState(false);
  const [ragOpen, setRagOpen] = useState(false);
  // 历史管理面板开关
  const [historyOpen, setHistoryOpen] = useState(false);
  // 项目列表 (绑定的工作区文件夹)
  const [projects, setProjects] = useState<ProjectItem[]>([]);
  const [nextConversations, setNextConversations] = useState<string | null>(null);
  const [sidebarProject, setSidebarProject] = useState('');
  const [listedConversations, setListedConversations] = useState<string[]>([]);
  const [directoryLoading, setDirectoryLoading] = useState(false);
  const directoryEpoch = useRef(0);
  const directoryPages = useRef(1);   // 同一筛选下刷新已展开的分页范围
  const directoryBoundary = useRef<ChatConversationItem | null>(null);
  const directoryFilters = useRef({ search: search.trim(), project_id: sidebarProject });
  directoryFilters.current = { search: search.trim(), project_id: sidebarProject };
  const [pagingError, setPagingError] = useState('');
  const pageBusy = useRef(false);
  const olderBusy = useRef(new Set<string>());
  // 项目组折叠状态 (project_id -> 是否折叠)
  const [collapsedProjects, setCollapsedProjects] = useState<Record<string, boolean>>({});
  // 新建项目对话框
  const [projectDialogOpen, setProjectDialogOpen] = useState(false);
  // 正在改绑项目的会话 id (null = 关闭对话框)
  const [movingConvId, setMovingConvId] = useState<string | null>(null);
  // 入口选择: 有历史会话时展示 "继续最近/开始新对话"
  const [showEntryChoice, setShowEntryChoice] = useState(false);
  // 待发送附件
  const [pendingAttachments, setPendingAttachments] = useState<Attachment[]>([]);
  useConfigWatch(['llm']);
  const publishedModels = useConfigStore((state) => state.llmConfigs);
  const modelReadVersion = useRef(0);
  // 模型详情 (设置弹窗管理用)
  const [modelsDetail, setModelsDetail] = useState<Record<string, ModelConfigItem>>({});
  const modelDetailsSnapshot = useRef<Record<string, ModelConfigItem>>({});
  const initialModelRead = useRef<Promise<void> | null>(null);
  const initialPublishedModels = useRef(publishedModels);
  // 模型编辑弹窗: null=关闭, ''=新增, 否则为编辑的模型名
  const [editingModel, setEditingModel] = useState<string | null>(null);
  // 文件选择器 ref
  const fileInputRef = useRef<HTMLInputElement>(null);
  // 最新会话快照, 供异步预加载结果判断目标是否仍存在
  const conversationsRef = useRef<Conversation[]>([]);
  // 预加载请求序号与当前请求, 防止较慢的旧请求覆盖新设置
  const preloadVersionRef = useRef(0);
  const preloadRequestRef = useRef<{
    version: number;
    key: string;
    sourceId: string;
    promise: Promise<string | null>;
  } | null>(null);

  const navigate = useNavigate();
  const { theme, toggleTheme } = useTheme();
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  // WS 取消订阅函数
  const unsubscribeRef = useRef<(() => void) | null>(null);
  // 当前流式消息 id (assistant)
  const streamingMsgIdRef = useRef<string | null>(null);
  // 当前会话和生成设置快照, 供稳定的消息操作回调读取
  const activeIdRef = useRef('');
  const activeRef = useRef<Conversation | undefined>(undefined);
  const generatingRef = useRef(false);
  const mutationPendingRef = useRef(false);
  const snapshotEpochRef = useRef<Record<string, number>>({});
  const settingsThinkRef = useRef(DEFAULT_SETTINGS.think);
  const bindingEpoch = useRef<Record<string, number>>({});
  const bindingMutations = useRef(new Set<string>());
  const preloadKeyRef = useRef<(projectId: string | null | undefined, model?: string) => string>(() => '');
  // 单帧内积累的流式增量和对应消息目标
  const streamingDeltasRef = useRef<StreamingDelta[]>([]);
  const streamingDeltaTargetRef = useRef<{ conversationId: string; messageId: string } | null>(null);
  const streamingFrameLimiter = useMemo(
    () => createAnimationFrameLimiter({ maxFps: MAX_UI_FRAME_RATE }),
    [],
  );

  const active = useMemo(
    () => conversations.find((c) => c.id === activeId),
    [conversations, activeId]
  );
  const modelBusy = bindingPending.has(activeId);
  const modelReady = Boolean(defaults) && !modelBusy && !bindingProblem && models.includes(settings.model);
  const approval = useApprovalMode(
    backendOk && active && active.id !== '__draft__' && !active.preloaded ? active.id : '',
    !active || active.id === '__draft__' || active.preloaded ? {
      mode: draftApprovalMode,
      available: plugins.some((plugin) => plugin.name === 'permissions' && plugin.enabled),
      pending: generating,
      onChange: setDraftApprovalMode,
    } : undefined,
  );
  const refreshApproval = approval.refresh;
  useEffect(() => {
    if ((optionsOpen || settingsOpen) && !generating) void refreshApproval();
  }, [optionsOpen, settingsOpen, generating, active?.messages.length, plugins, pluginConfigRevision, refreshApproval]);
  // 打开面板, 完成命令或更新插件后重新读实际模式, 两个入口共用同一状态
  const messageScroll = useChatScroll(`${backendOk}:${activeId}:${showEntryChoice}`, active?.messages, Boolean(active?.loaded));
  const activePendingUserInput = pendingUserInputs[activeId]?.[0];
  const activePendingUserAnswer = activePendingUserInput
    ? pendingUserAnswers[activePendingUserInput.requestId] ?? ''
    : '';
  conversationsRef.current = conversations;
  activeIdRef.current = activeId;
  activeRef.current = active;
  generatingRef.current = generating;
  settingsThinkRef.current = settings.think;

  const mergeHistoryItems = useCallback((items: ChatConversationItem[]) => {
    setConversations((current) => {
      const existing = new Map(current.map((conversation) => [conversation.id, conversation]));
      const restored = items.map((item) => (
        restoreConversation(item, existing.get(item.conversation_id))
      ));
      const drafts = current.filter((conversation) => (
        conversation.id === '__draft__' || conversation.preloaded
      ));
      const next = [...drafts, ...restored, ...current.filter((conversation) => !drafts.includes(conversation) && !items.some((item) => item.conversation_id === conversation.id))];
      return next;
    });
  }, []);

  const refreshHistorySidebar = useCallback(async (removedIds: string[] = []) => {
    if (removedIds.length) {
      setConversations((current) => current.filter((item) => !removedIds.includes(item.id)));
      setActiveId((current) => removedIds.includes(current) ? '' : current);
    }
    if (backendOk === false) return;
    const filters = directoryFilters.current;
    const epoch = ++directoryEpoch.current;
    const pageCount = directoryPages.current;
    const boundary = directoryBoundary.current;
    const isCurrent = () => epoch === directoryEpoch.current && JSON.stringify(filters) === JSON.stringify(directoryFilters.current);
    setDirectoryLoading(true);
    try {
      const items: ChatConversationItem[] = [];
      let cursor: string | null | undefined;
      let loadedPages = 0;
      do {
        const page = await chatApi.listConversationPage(cursor ?? undefined, filters);
        if (!isCurrent()) return;
        items.push(...page.conversations);
        cursor = page.next_cursor;
        loadedPages += 1;
      } while (cursor && (loadedPages < pageCount || !conversationBoundaryReached(items[items.length - 1], boundary)));
      const refreshed = [...new Map(items.map((item) => [item.conversation_id, item])).values()];
      mergeHistoryItems(refreshed);
      setListedConversations(refreshed.map((item) => item.conversation_id));
      setNextConversations(cursor ?? null);
      directoryPages.current = loadedPages;
      directoryBoundary.current = refreshed[refreshed.length - 1] ?? null;
      const selected = activeIdRef.current;
      if (selected && selected !== '__draft__' && !conversationsRef.current.find((item) => item.id === selected)?.preloaded
        && !refreshed.some((item) => item.conversation_id === selected)) {
        const exact = await chatApi.getConversation(selected);
        if (isCurrent() && !exact.conversations.length) {
          setConversations((current) => current.filter((item) => item.id !== selected));
          setActiveId((current) => current === selected ? refreshed[0]?.conversation_id ?? '' : current);
        }
      }
      if (isCurrent()) setPagingError('');
    } catch (error) {
      if (isCurrent()) throw error;
    } finally {
      if (isCurrent()) setDirectoryLoading(false);
    }
  }, [backendOk, mergeHistoryItems]);

  useEffect(() => {
    if (backendOk !== true) return;
    let cancelled = false;
    const controller = new AbortController();
    const filters = directoryFilters.current;
    const epoch = ++directoryEpoch.current;
    const isCurrent = () => !cancelled && epoch === directoryEpoch.current && JSON.stringify(filters) === JSON.stringify(directoryFilters.current);
    setDirectoryLoading(true);
    directoryPages.current = 1;
    directoryBoundary.current = null;
    setListedConversations([]);
    setNextConversations(null);
    const timer = setTimeout(() => {
      void chatApi.listConversationPage(undefined, filters, controller.signal).then((page) => {
        if (!isCurrent()) return;
        mergeHistoryItems(page.conversations);
        setListedConversations(page.conversations.map((item) => item.conversation_id));
        directoryBoundary.current = page.conversations[page.conversations.length - 1] ?? null;
        setNextConversations(page.next_cursor);
        setPagingError('');
        if (!activeIdRef.current && !search.trim() && !sidebarProject && page.conversations.length) setShowEntryChoice(true);
      }).catch((error: unknown) => {
        if (isCurrent()) setPagingError(error instanceof Error ? error.message : '对话加载失败');
      }).finally(() => {
        if (isCurrent()) setDirectoryLoading(false);
      });
    }, search.trim() ? 250 : 0);
    return () => { cancelled = true; controller.abort(); clearTimeout(timer); };
  }, [backendOk, search, sidebarProject, mergeHistoryItems]);

  // 更新指定会话
  const updateConversation = useCallback((id: string, updater: (c: Conversation) => Conversation) => {
    setConversations((prev) => prev.map((c) => (c.id === id ? updater(c) : c)));
  }, []);

  const applyModelBinding = useCallback((id: string, model: string) => {
    updateConversation(id, (conversation) => ({ ...conversation, model }));
    if (activeIdRef.current === id) setSettings((previous) => ({ ...previous, model }));
  }, [updateConversation]);

  // 更新当前流式消息
  const updateStreamingMessage = useCallback(
    (updater: (m: ChatMessage) => ChatMessage, conversationId = activeIdRef.current) => {
      const msgId = streamingMsgIdRef.current;
      if (!msgId || !conversationId) return;
      updateConversation(conversationId, (c) => ({
        ...c,
        messages: c.messages.map((m) => (m.id === msgId ? updater(m) : m)),
        updatedAt: Math.round(Date.now() / 1000),
      }));
    },
    [updateConversation]
  );

  const commitStreamingDeltas = useCallback(() => {
    const deltas = streamingDeltasRef.current;
    const target = streamingDeltaTargetRef.current;
    streamingDeltasRef.current = [];
    streamingDeltaTargetRef.current = null;
    if (!target || deltas.length === 0) return;
    updateConversation(target.conversationId, (conversation) => ({
      ...conversation,
      messages: conversation.messages.map((message) => (
        message.id === target.messageId ? applyStreamingDeltas(message, deltas) : message
      )),
      updatedAt: Math.round(Date.now() / 1000),
    }));
  }, [updateConversation]);

  const flushStreamingDeltas = useCallback(() => {
    streamingFrameLimiter.flush();
  }, [streamingFrameLimiter]);

  const queueStreamingDelta = useCallback((delta: StreamingDelta, conversationId = activeIdRef.current) => {
    const target = {
      conversationId,
      messageId: streamingMsgIdRef.current ?? '',
    };
    if (!target.conversationId || !target.messageId) return;
    const currentTarget = streamingDeltaTargetRef.current;
    if (
      currentTarget
      && (
        currentTarget.conversationId !== target.conversationId
        || currentTarget.messageId !== target.messageId
      )
    ) {
      flushStreamingDeltas();
    }
    streamingDeltaTargetRef.current = target;
    enqueueStreamingDelta(streamingDeltasRef.current, delta);
    streamingFrameLimiter.schedule(commitStreamingDeltas);
  }, [commitStreamingDeltas, flushStreamingDeltas, streamingFrameLimiter]);

  useEffect(() => () => {
    streamingFrameLimiter.cancel();
    streamingDeltasRef.current = [];
    streamingDeltaTargetRef.current = null;
  }, [streamingFrameLimiter]);

  // 处理 WS 事件
  const handleEvent = useCallback(
    (conversationId: string, event: ChatEvent) => {
      switch (event.type) {
        case 'model_changed':
          bindingEpoch.current[conversationId] = (bindingEpoch.current[conversationId] ?? 0) + 1;
          applyModelBinding(conversationId, event.model);
          if (!bindingMutations.current.has(conversationId)) setBindingPending((current) => { const next = new Set(current); next.delete(conversationId); return next; });
          break;
        case 'snapshot': {
          if (event.model) applyModelBinding(conversationId, event.model);
          flushStreamingDeltas();
          snapshotEpochRef.current[conversationId] = (snapshotEpochRef.current[conversationId] ?? 0) + 1;
          const preservePending = mutationPendingRef.current && event.state === 'idle';
          updateConversation(conversationId, (conversation) => {
            let messages = turnsToMessages(event.turns);
            if (preservePending) {
              const pending = conversation.messages.find((message) => message.streaming);
              if (pending) {
                const localTurn = conversation.messages.filter((message) => message.turnIndex === pending.turnIndex);
                messages = [...messages.filter((message) => message.turnIndex !== pending.turnIndex), ...localTurn];
              }
            }
            if (event.has_more !== undefined) {
              const lower = event.turns[0]?.turn_index;
              const overlapping = lower !== undefined && conversation.messages.some((message) => message.turnIndex !== undefined && message.turnIndex >= lower);
              const earlier = event.has_more && overlapping ? conversation.messages.filter((message) => message.turnIndex !== undefined && message.turnIndex < lower!) : [];
              return { ...conversation, messages: [...earlier, ...messages], loaded: true,
                hasOlder: earlier.length ? conversation.hasOlder : event.has_more,
                olderBefore: earlier.length ? conversation.olderBefore : event.next_before };
            }
            return { ...conversation, messages, loaded: true };
          });
          if (!preservePending) {
            streamingMsgIdRef.current = event.active_turn_id === null ? null : `${event.active_turn_id}-a`;
            generatingRef.current = event.state !== 'idle';
            setGenerating(event.state !== 'idle');
          }
          setPendingUserInputs((previous) => ({
            ...previous,
            [conversationId]: event.pending_user_inputs.map((item) => ({
              requestId: item.request_id, question: item.question, options: item.options, approval: item.approval,
            })),
          }));
          break;
        }
        case 'turn_start':
          flushStreamingDeltas();
          if (!streamingMsgIdRef.current) {
            streamingMsgIdRef.current = `${event.turn_id}-a`;
            updateConversation(conversationId, (conversation) => {
              const previous = conversation.messages.find((message) => message.role === 'assistant' && message.turnIndex === event.turn_index);
              const messages = conversation.messages.filter((message) => message.turnIndex !== event.turn_index);
              return { ...conversation, messages: [...messages, {
                id: `${event.turn_id}-u`, role: 'user', content: event.user_input, timestamp: Date.now(), turnIndex: event.turn_index,
              }, {
                id: `${event.turn_id}-a`, role: 'assistant', content: '', timestamp: Date.now(), turnIndex: event.turn_index,
                activeVariant: event.variant_index, streaming: true, variants: previous?.variants,
              }] };
            });
          }
          generatingRef.current = true;
          setGenerating(true);
          updateStreamingMessage((message) => ({
            ...message,
            turnIndex: event.turn_index,
            activeVariant: event.variant_index,
          }), conversationId);
          updateConversation(conversationId, (conversation) => {
            const messages = [...conversation.messages];
            const assistantIndex = messages.findIndex(
              (message) => message.id === streamingMsgIdRef.current,
            );
            if (assistantIndex > 0 && messages[assistantIndex - 1].role === 'user') {
              messages[assistantIndex - 1] = {
                ...messages[assistantIndex - 1],
                turnIndex: event.turn_index,
              };
            }
            return { ...conversation, messages };
          });
          break;
        case 'thinking_delta':
          queueStreamingDelta({ type: 'thinking', delta: event.delta }, conversationId);
          break;
        case 'content_delta':
          queueStreamingDelta({ type: 'content', delta: event.delta }, conversationId);
          break;
        case 'tool_start':
          flushStreamingDeltas();
          updateStreamingMessage((m) => {
            const newTool: ToolCall = {
              seq: (m.toolCalls ?? []).length,
              name: event.name,
              arguments: typeof event.arguments === 'string' ? event.arguments : JSON.stringify(event.arguments),
              success: null,
              call_id: event.call_id,
              created_at: Date.now() / 1000,
            };
            const segments = [...(m.segments ?? []), { type: 'tool' as const, tool: newTool }];
            return {
              ...m,
              toolCalls: [...(m.toolCalls ?? []), newTool],
              segments,
            };
          }, conversationId);
          break;
        case 'tool_end':
          flushStreamingDeltas();
          updateStreamingMessage((m) => {
            const updatedTools = (m.toolCalls ?? []).map((t) =>
              t.call_id === event.call_id ? { ...t, success: event.success } : t
            );
            // 同步更新 segments 中的 tool 状态
            const segments = (m.segments ?? []).map((seg) =>
              seg.type === 'tool' && seg.tool.call_id === event.call_id
                ? { ...seg, tool: { ...seg.tool, success: event.success } }
                : seg
            );
            return { ...m, toolCalls: updatedTools, segments };
          }, conversationId);
          break;
        case 'ask_user':
          generatingRef.current = true;
          setGenerating(true);
          setPendingUserInputs((prev) => {
            const current = prev[event.conversation_id] ?? [];
            if (current.some((item) => item.requestId === event.request_id)) return prev;
            return {
              ...prev,
              [event.conversation_id]: [
                ...current,
                {
                  requestId: event.request_id,
                  question: event.question,
                  options: event.options ?? [],
                  approval: event.approval,
                },
              ],
            };
          });
          break;
        case 'ask_user_end':
          setPendingUserInputs((prev) => ({
            ...prev,
            [event.conversation_id]: (prev[event.conversation_id] ?? []).filter(
              (item) => item.requestId !== event.request_id
            ),
          }));
          setPendingUserAnswers((prev) => {
            const next = { ...prev };
            delete next[event.request_id];
            return next;
          });
          break;
        case 'turn_done': {
          flushStreamingDeltas();
          updateStreamingMessage((message) => {
            const content = event.answer || message.content;
            const completedVariant: LocalResponseVariant = {
              variantIndex: event.variant_index,
              content,
              thinking: message.thinking,
              toolCalls: message.toolCalls ?? [],
              segments: message.segments,
              contextStats: event.context_stats,
              outputFiles: event.output_files,
              responseMetadata: event.response_metadata,
            };
            const variants = [
              ...(message.variants ?? []).filter(
                (variant) => variant.variantIndex !== event.variant_index,
              ),
              completedVariant,
            ].sort((left, right) => left.variantIndex - right.variantIndex);
            return {
              ...message,
              content,
              streaming: false,
              turnIndex: event.turn_index,
              activeVariant: event.variant_index,
              variantCount: event.variant_count,
              variants,
              outputFiles: event.output_files,
              responseMetadata: event.response_metadata,
              contextStats: event.context_stats,
            };
          }, conversationId);
          setPendingUserInputs((prev) => ({ ...prev, [conversationId]: [] }));
          streamingMsgIdRef.current = null;
          generatingRef.current = false;
          setGenerating(false);
          break;
        }
        case 'error': {
          flushStreamingDeltas();
          const errorText = `\n\n[错误] ${event.error ?? event.message ?? '未知错误'}`;
          updateStreamingMessage((m) => ({
            ...m,
            content: m.content + errorText,
            segments: m.segments?.length ? [...m.segments, { type: 'content', content: errorText }] : undefined,
            responseMetadata: event.response_metadata,
            streaming: false,
            turnIndex: event.turn_index ?? m.turnIndex,
            activeVariant: event.variant_index ?? m.activeVariant,
            variantCount: event.variant_count ?? m.variantCount,
            contextStats: event.context_stats ?? m.contextStats,
            outputFiles: event.output_files ?? m.outputFiles,
          }), conversationId);
          setPendingUserInputs((prev) => ({ ...prev, [conversationId]: [] }));
          streamingMsgIdRef.current = null;
          generatingRef.current = false;
          setGenerating(false);
          break;
        }
        case 'variant_selected':
          break;
      }
    },
    [flushStreamingDeltas, queueStreamingDelta, updateConversation, updateStreamingMessage, applyModelBinding]
  );

  // 加载会话历史消息
  const loadTurns = useCallback(
    async (conversationId: string) => {
      const epoch = snapshotEpochRef.current[conversationId] ?? 0;
      try {
        const { turns, has_more, next_before } = await chatApi.listTurnPage(conversationId);
        if ((snapshotEpochRef.current[conversationId] ?? 0) !== epoch) return;
        const messages = turnsToMessages(turns);
        updateConversation(conversationId, (c) => ({ ...c, messages, loaded: true, hasOlder: has_more, olderBefore: next_before }));
      } catch (err) {
        if ((snapshotEpochRef.current[conversationId] ?? 0) !== epoch) return;
        console.error('[Chat] 加载历史失败:', err);
        setPagingError(err instanceof Error ? err.message : '历史加载失败');
        updateConversation(conversationId, (c) => ({ ...c, loaded: true }));
      }
    },
    [updateConversation]
  );

  const loadOlder = useCallback(async (conversation: Conversation) => {
    if (!conversation.hasOlder || conversation.olderBefore == null || olderBusy.current.has(conversation.id)) return;
    const before = conversation.olderBefore;
    const epoch = snapshotEpochRef.current[conversation.id] ?? 0;
    olderBusy.current.add(conversation.id);
    try {
      const page = await chatApi.listTurnPage(conversation.id, before);
      if ((snapshotEpochRef.current[conversation.id] ?? 0) !== epoch) return;
      const messages = turnsToMessages(page.turns);
      updateConversation(conversation.id, (current) => current.olderBefore !== before ? current : {
        ...current, messages: [...messages.filter((message) => !current.messages.some((item) => item.id === message.id)), ...current.messages],
        hasOlder: page.has_more, olderBefore: page.next_before,
      });
      setPagingError('');
    } catch (error) {
      if ((snapshotEpochRef.current[conversation.id] ?? 0) !== epoch) return;
      setPagingError(error instanceof Error ? error.message : '历史加载失败');
    }
    finally { olderBusy.current.delete(conversation.id); }
  }, [updateConversation]);

  const loadConversationPage = async () => {
    if (!nextConversations || pageBusy.current) return;
    pageBusy.current = true;
    const epoch = directoryEpoch.current;
    const filters = directoryFilters.current;
    const isCurrent = () => epoch === directoryEpoch.current && JSON.stringify(filters) === JSON.stringify(directoryFilters.current);
    try {
      const page = await chatApi.listConversationPage(nextConversations, filters);
      if (!isCurrent()) return;
      directoryPages.current += 1;
      directoryBoundary.current = page.conversations[page.conversations.length - 1] ?? directoryBoundary.current;
      setListedConversations((current) => [...new Set([...current, ...page.conversations.map((item) => item.conversation_id)])]);
      setConversations((current) => [...current, ...page.conversations.filter((item) => !current.some((entry) => entry.id === item.conversation_id)).map((item) => restoreConversation<ChatMessage>(item))]);
      setNextConversations(page.next_cursor);
      setPagingError('');
    } catch (error) { if (isCurrent()) setPagingError(error instanceof Error ? error.message : '对话加载失败'); }
    finally { pageBusy.current = false; }
  };

  // 订阅当前会话 WS (草稿态不订阅)
  useEffect(() => {
    if (!activeId || activeId === '__draft__') return;
    unsubscribeRef.current?.();
    unsubscribeRef.current = subscribeChat(activeId, (event) => handleEvent(activeId, event));
    return () => {
      unsubscribeRef.current?.();
      unsubscribeRef.current = null;
    };
  }, [activeId, handleEvent]);

  const handleModelSaved = useCallback(async (signal?: AbortSignal) => {
    const version = ++modelReadVersion.current;
    try {
      const { models: detail } = await chatApi.listModelsDetail(signal);
      if (signal?.aborted || version !== modelReadVersion.current) return;
      const names = Object.keys(detail);
      setModels(names);
      setModelsLoaded(true);
      setModelsDetail(detail);
      modelDetailsSnapshot.current = detail;
    } catch (error) {
      if (!signal?.aborted) console.error('[Chat] 刷新模型列表失败:', error);
    }
  }, []);

  // 初始化: 健康检查 + 加载模型/插件/会话列表
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        void chatApi.health().then(() => { if (!cancelled) setBackendOk(true); }).catch((error) => {
          console.error('[Chat] 健康检查失败:', error);
          if (!cancelled) setBackendOk(false);
        });
        void chatApi.listPlugins().then(({ plugins }) => { if (!cancelled) setPlugins(plugins); }).catch((error) => console.error('[Chat] 插件读取失败:', error));
        void chatApi.listProjects().then(({ projects }) => { if (!cancelled) setProjects(projects); }).catch((error) => console.error('[Chat] 项目读取失败:', error));
        initialModelRead.current = handleModelSaved();
        void chatApi.getSettings().then((value) => {
          if (!value || typeof value.default_model !== 'string' || typeof value.revision !== 'string') throw new Error('Chat 默认设置响应无效');
          if (!cancelled) {
            setDefaults(value);
            if (!activeIdRef.current) setSettings((previous) => ({ ...previous, model: value.default_model }));
          }
        }).catch((error: unknown) => { if (!cancelled) toast('error', `读取 Chat 默认模型失败: ${String(error)}`); });

      } catch (err) {
        console.error('[Chat] 后端连接失败:', err);
        if (!cancelled) setBackendOk(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [handleModelSaved]);

  useEffect(() => {
    if (!settingsOpen && !Object.keys(publishedModels).length && publishedModels === initialPublishedModels.current) return;
    const controller = new AbortController();
    void (async () => {
      await initialModelRead.current;
      if (controller.signal.aborted) return;
      const details = modelDetailsSnapshot.current;
      const matching = Object.keys(details).length === Object.keys(publishedModels).length && Object.entries(publishedModels).every(([name, config]) => {
        const actual = details[name] as unknown as Record<string, unknown> | undefined;
        return actual && Object.entries(config).every(([key, value]) => JSON.stringify(actual[key]) === JSON.stringify(value));
      });
      if (settingsOpen || !matching) await handleModelSaved(controller.signal);
    })();
    return () => controller.abort();
  }, [publishedModels, settingsOpen, handleModelSaved]);
  // 等待首次详情后比较实际配置, 初始化期间来源变化仍会刷新, 不盲目忽略首次发布

  // 切换会话时按需加载历史
  useEffect(() => {
    if (active && !active.loaded) {
      void loadTurns(active.id);
    }
  }, [active, loadTurns]);

  // 模型下拉选项(保证当前选中项总在选项中)
  const modelOptions = useMemo(() => {
    const list = models.length > 0 ? models : [DEFAULT_SETTINGS.model];
    const all = list.includes(settings.model) ? list : [settings.model, ...list];
    return all.map((name) => ({ value: name, label: models.includes(name) ? name : `${name} (不可用, 请重选)` }));
  }, [models, settings.model]);

  const thinkingOptions = useMemo(
    () => getThinkingOptions(modelsDetail[settings.model]),
    [modelsDetail, settings.model]
  );

  useEffect(() => {
    if (!Object.prototype.hasOwnProperty.call(modelsDetail, settings.model)) return;
    if (thinkingOptions.some((option) => option.value === settings.think)) return;
    setSettings((current) => ({ ...current, think: 'off' }));
  }, [modelsDetail, settings.model, settings.think, thinkingOptions]);

  useEffect(() => {
    try {
      window.localStorage.setItem(CHAT_THINK_STORAGE_KEY, settings.think);
    } catch {
      // 浏览器禁用本地存储时继续使用当前页面状态
    }
  }, [settings.think]);

  useEffect(() => {
    const handleStorage = (event: StorageEvent) => {
      if (event.key !== CHAT_THINK_STORAGE_KEY) return;
      setSettings((current) => ({
        ...current,
        think: event.newValue?.trim() || DEFAULT_SETTINGS.think,
      }));
    };
    window.addEventListener('storage', handleStorage);
    return () => window.removeEventListener('storage', handleStorage);
  }, []);

  // 更新单项设置
  const updateSettings = useCallback(<K extends keyof ChatSettings>(key: K, value: ChatSettings[K]) => {
    if (key === 'model') {
      const model = String(value);
      const id = activeId;
      if (generatingRef.current || bindingMutations.current.has(id)) return;
      if (!id || id === '__draft__') {
        preloadVersionRef.current += 1;
        if (id) applyModelBinding(id, model);
        else {
          setConversations((previous) => [{ id: '__draft__', title: '新对话', model, messages: [], updatedAt: Math.round(Date.now() / 1000), loaded: true }, ...previous]);
          setActiveId('__draft__'); setShowEntryChoice(false);
          setSettings((previous) => ({ ...previous, model }));
        }
        return;
      }
      bindingEpoch.current[id] = (bindingEpoch.current[id] ?? 0) + 1;
      bindingMutations.current.add(id);
      setBindingPending((current) => new Set(current).add(id));
      const expected = conversationsRef.current.find((conversation) => conversation.id === id)?.model;
      void chatApi.setModel(id, model, expected).then((result) => {
        if (!result.ok) throw new Error(result.error || '模型切换失败');
        applyModelBinding(id, result.model);
        updateConversation(id, (conversation) => ({ ...conversation,
          preloadKey: conversation.preloaded ? preloadKeyRef.current(conversation.projectId, result.model) : conversation.preloadKey }));
        if (activeIdRef.current === id) setBindingProblem(null);
      }).catch(async (error: unknown) => {
        await chatApi.getModel(id).then((result) => applyModelBinding(id, result.model)).catch(() => undefined);
        toast('error', `模型切换失败: ${String(error)}`);
      }).finally(() => {
        bindingMutations.current.delete(id);
        setBindingPending((current) => { const next = new Set(current); next.delete(id); return next; });
      });
      return;
    }
    if (key === 'defaultAgentPromptEnabled' && activeId && activeId !== '__draft__') {
      void chatApi.setDefaultAgentPrompt(activeId, value === true).then((result) => {
        if (!result.ok) throw new Error(result.error || '保存失败');
        if (activeIdRef.current === activeId) setSettings((prev) => ({ ...prev, defaultAgentPromptEnabled: result.default_agent_prompt_enabled }));
      }).catch((error: unknown) => toast('error', String(error)));
      return;
    }
    if (key === 'commandHistoryEnabled' && activeId && activeId !== '__draft__') {
      void chatApi.setCommandHistory(activeId, value === true).then((result) => {
        if (!result.ok) throw new Error(result.error || '保存失败');
        if (activeIdRef.current === activeId) setSettings((prev) => ({ ...prev, commandHistoryEnabled: result.command_history_enabled }));
      }).catch((error: unknown) => toast('error', String(error)));
      return;
    }
    if (key === 'maxIterations' && activeId && activeId !== '__draft__') {
      void chatApi.setMaxIterations(activeId, typeof value === 'number' ? value : null).then((result) => {
        if (!result.ok) throw new Error(result.error || '保存失败');
        if (activeIdRef.current === activeId) setSettings((prev) => ({ ...prev, maxIterations: result.max_iterations }));
      }).catch((error: unknown) => toast('error', String(error)));
      return;
    }
    setSettings((prev) => ({ ...prev, [key]: value }));
  }, [activeId, applyModelBinding, updateConversation]);

  useEffect(() => {
    setBindingProblem(null);
    const cached = conversationsRef.current.find((conversation) => conversation.id === activeId);
    if (cached?.model) setSettings((previous) => ({ ...previous, model: cached.model! }));
    if (!activeId || activeId === '__draft__') return;
    const id = activeId;
    const epoch = (bindingEpoch.current[id] ?? 0) + 1;
    bindingEpoch.current[id] = epoch;
    setBindingPending((current) => new Set(current).add(id));
    let cancelled = false;
    void chatApi.getModel(id).then((result) => {
      if (!result.ok || typeof result.model !== 'string') throw new Error('会话模型响应无效');
      if (!cancelled && bindingEpoch.current[id] === epoch) applyModelBinding(id, result.model);
    }).catch((error: unknown) => {
      if (!cancelled && bindingEpoch.current[id] === epoch) setBindingProblem(`读取会话模型失败: ${String(error)}`);
    }).finally(() => {
      if (bindingEpoch.current[id] === epoch) setBindingPending((current) => { const next = new Set(current); next.delete(id); return next; });
    });
    return () => { cancelled = true; };
  }, [activeId, applyModelBinding]);

  const saveDefaultModel = useCallback(async (model: string) => {
    if (!defaults || defaultSaving) return;
    setDefaultSaving(true);
    try {
      const result = await chatApi.saveSettings(model, defaults.revision);
      setDefaults(result);
      if (!activeIdRef.current) setSettings((previous) => ({ ...previous, model: result.default_model }));
      toast('success', '整体默认模型已保存, 仅影响之后创建的会话');
    } catch (error) { toast('error', `保存整体默认模型失败: ${String(error)}`); }
    finally { setDefaultSaving(false); }
  }, [defaults, defaultSaving]);

  useEffect(() => {
    if (!settingsOpen) return;
    let cancelled = false;
    void chatApi.getSettings().then((value) => {
      if (typeof value.default_model !== 'string' || typeof value.revision !== 'string') throw new Error('Chat 默认设置响应无效');
      if (!cancelled) setDefaults(value);
    }).catch((error: unknown) => { if (!cancelled) toast('error', `读取整体默认失败: ${String(error)}`); });
    return () => { cancelled = true; };
  }, [settingsOpen]);

  useEffect(() => {
    if (!activeId || activeId === '__draft__') return;
    let cancelled = false;
    void chatApi.getDefaultAgentPrompt(activeId).then((result) => {
      if (!cancelled) setSettings((prev) => ({ ...prev, defaultAgentPromptEnabled: result.default_agent_prompt_enabled === true }));
    }).catch((error: unknown) => { if (!cancelled) toast('error', String(error)); });
    void chatApi.getCommandHistory(activeId).then((result) => {
      if (!cancelled) setSettings((prev) => ({ ...prev, commandHistoryEnabled: result.command_history_enabled }));
    }).catch((error: unknown) => { if (!cancelled) toast('error', String(error)); });
    void chatApi.getMaxIterations(activeId).then((result) => {
      if (!cancelled) setSettings((prev) => ({ ...prev, maxIterations: result.max_iterations }));
    }).catch((error: unknown) => { if (!cancelled) toast('error', String(error)); });
    return () => { cancelled = true; };
  }, [activeId]);

  // 生成会影响 Session, 模型或插件安装结果的前端设置键
  const preloadBaseKey = useMemo(() => JSON.stringify({
    temperature: settings.temperature,
    commandHistoryEnabled: settings.commandHistoryEnabled,
    defaultAgentPromptEnabled: settings.defaultAgentPromptEnabled,
    maxIterations: settings.maxIterations,
    systemPrompt: settings.systemPrompt,
    plugins,
    pluginConfigRevision,
  }), [settings.temperature, settings.systemPrompt, settings.commandHistoryEnabled, settings.defaultAgentPromptEnabled, settings.maxIterations, plugins, pluginConfigRevision]);

  const preloadKeyFor = useCallback(
    (projectId: string | null | undefined, model = settings.model) => `${preloadBaseKey}:${JSON.stringify({ model, config: modelsDetail[model] ?? null })}:${projectId ?? ''}`,
    [preloadBaseKey, settings.model, modelsDetail]
  );
  preloadKeyRef.current = preloadKeyFor;

  const preloadSettingsFor = useCallback(
    (projectId: string | null | undefined, model = settings.model): ChatPreloadSettings => ({
      model,
      think: settings.think,
      temperature: settings.temperature,
      commandHistoryEnabled: settings.commandHistoryEnabled,
      defaultAgentPromptEnabled: settings.defaultAgentPromptEnabled,
      maxIterations: settings.maxIterations,
      systemPrompt: settings.systemPrompt,
      projectId: projectId ?? null,
    }),
    [settings.model, settings.think, settings.temperature, settings.systemPrompt, settings.commandHistoryEnabled, settings.defaultAgentPromptEnabled, settings.maxIterations]
  );

  // 启动或复用当前设置对应的预加载请求
  const startPreload = useCallback((target: Conversation, key: string): Promise<string | null> => {
    const running = preloadRequestRef.current;
    if (
      running
      && running.version === preloadVersionRef.current
      && running.sourceId === target.id
      && running.key === key
    ) {
      return running.promise;
    }

    const version = preloadVersionRef.current + 1;
    preloadVersionRef.current = version;
    const sourceId = target.id;
    const oldPreloadedId = target.preloaded ? target.id : null;
    const request = chatApi.preloadConversation(preloadSettingsFor(target.projectId, target.model))
      .then(async ({ conversation_id: conversationId }) => {
        if (preloadVersionRef.current !== version) {
          await chatApi.deleteConversation(conversationId).catch(() => undefined);
          return null;
        }
        const latest = conversationsRef.current.find((conv) => conv.id === sourceId);
        if (!latest) {
          await chatApi.deleteConversation(conversationId).catch(() => undefined);
          return null;
        }
        setConversations((prev) => prev.map((conv) => (
          conv.id === sourceId
            ? { ...conv, id: conversationId, preloaded: true, preloadKey: key, loaded: true }
            : conv
        )));
        setActiveId((current) => (current === sourceId ? conversationId : current));
        if (oldPreloadedId && oldPreloadedId !== conversationId) {
          await chatApi.deleteConversation(oldPreloadedId).catch((err) => {
            console.error('[Chat] 释放旧预加载会话失败:', err);
          });
        }
        return conversationId;
      })
      .catch((err) => {
        if (preloadVersionRef.current === version) {
          console.error('[Chat] 预加载会话失败:', err);
        }
        return null;
      });
    preloadRequestRef.current = { version, key, sourceId, promise: request };
    void request.finally(() => {
      if (preloadRequestRef.current?.version === version) {
        preloadRequestRef.current = null;
      }
    });
    return request;
  }, [preloadSettingsFor]);

  // 过滤 + 分组会话列表 (项目分组 + 最近区; 颜色按全局排序位置稳定分配)
  const grouped = useMemo(() => {
    const visible = new Set(listedConversations);
    const sorted = conversations.filter((c) => sidebarConversationVisible(c, visible, activeId, search, sidebarProject)).sort((a, b) => b.updatedAt - a.updatedAt);
    const kw = search.trim().toLowerCase();
    const matched = sorted;
    const colorIndex = new Map<string, number>();
    matched.forEach((c, i) => colorIndex.set(c.id, i));
    const knownProject = new Set(projects.map((p) => p.project_id));
    const byProject = new Map<string, Conversation[]>();
    const recent: Conversation[] = [];
    for (const c of matched) {
      // 项目已删除但会话未解绑的兜底: 归入最近
      if (c.projectId && knownProject.has(c.projectId)) {
        const list = byProject.get(c.projectId) ?? [];
        list.push(c);
        byProject.set(c.projectId, list);
      } else {
        recent.push(c);
      }
    }
    // 搜索时只展示有匹配的分组; 非搜索时展示全部项目 (含空项目, 便于新建)
    const groups = projects
      .map((p) => ({ project: p, conversations: byProject.get(p.project_id) ?? [] }))
      .filter((g) => (!sidebarProject || g.project.project_id === sidebarProject) && (g.conversations.length > 0 || !kw));
    return { groups, recent, colorIndex, total: matched.length };
  }, [conversations, search, projects, listedConversations, sidebarProject, activeId]);

  // 自动调整输入框高度
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }, [input]);

  // 创建本地草稿会话 (不切换选中, 供自动创建场景复用); projectId = 在项目下新建
  const createDraft = useCallback((projectId?: string): Conversation => {
    const existing = conversationsRef.current.find((conversation) => conversation.id === '__draft__' && (conversation.projectId ?? null) === (projectId ?? null));
    if (existing) return existing;
    const draft: Conversation = {
      id: '__draft__',
      title: '新对话',
      model: defaults?.default_model ?? DEFAULT_SETTINGS.model,
      messages: [],
      updatedAt: Math.round(Date.now() / 1000),
      loaded: true,
      projectId: projectId ?? null,
    };
    setConversations((prev) => {
      const existing = prev.find((c) => c.id === '__draft__');
      // 已有同项目空草稿则复用, 否则替换 (避免旧草稿的项目归属串味)
      if (existing && (existing.projectId ?? null) === (projectId ?? null)) return prev;
      return [draft, ...prev.filter((c) => c.id !== '__draft__')];
    });
    return draft;
  }, [defaults]);

  // 新建会话并触发后端预加载; projectId = 在项目下新建
  const handleNew = useCallback((projectId?: string) => {
    if (!defaults) { toast('error', '整体默认模型尚未读取完成'); return; }
    const existing = conversations.find((conv) => (
      conv.preloaded
      && conv.messages.length === 0
      && (conv.projectId ?? null) === (projectId ?? null)
    ));
    if (existing) {
      setActiveId(existing.id);
      setShowEntryChoice(false);
      setInput('');
      textareaRef.current?.focus();
      return;
    }

    const abandoned = conversations.filter((conv) => conv.preloaded && conv.messages.length === 0);
    preloadVersionRef.current += 1;
    for (const conv of abandoned) {
      void chatApi.deleteConversation(conv.id).catch((err) => {
        console.error('[Chat] 释放未使用预加载会话失败:', err);
      });
    }
    if (abandoned.length > 0) {
      const abandonedIds = new Set(abandoned.map((conv) => conv.id));
      setConversations((prev) => prev.filter((conv) => !abandonedIds.has(conv.id)));
    }
    setDraftApprovalMode('user');
    const draft = createDraft(projectId);
    setActiveId(draft.id);
    setShowEntryChoice(false);
    setInput('');
    textareaRef.current?.focus();
  }, [conversations, createDraft, defaults]);

  // 草稿立即预加载; 未发送前变更构建设置时防抖刷新
  useEffect(() => {
    if (!active || active.messages.length > 0) return;
    if (active.id !== '__draft__' && !active.preloaded) return;
    const key = preloadKeyFor(active.projectId, active.model);
    if (active.preloaded && active.preloadKey === key) return;
    // 已上传附件时保留同一 ID 和文件, 首次发送由后端按指纹原地重建
    if (active.preloaded && pendingAttachments.length > 0) return;

    const delay = active.id === '__draft__' ? 0 : 250;
    const timer = window.setTimeout(() => {
      void startPreload(active, key);
    }, delay);
    return () => window.clearTimeout(timer);
  }, [active, pendingAttachments.length, preloadKeyFor, startPreload]);

  // 删除会话 (调后端删除 + 本地移除)
  const handleDelete = useCallback(
    async (id: string) => {
      const conv = conversations.find((c) => c.id === id);
      const title = conv?.title || id;
      if (!(await confirm(`确定删除会话 "${title}" 吗？`))) return;
      if (id !== '__draft__') {
        try {
          await chatApi.deleteConversation(id);
        } catch (err) {
          alert(`删除会话失败: ${err instanceof Error ? err.message : String(err)}`);
          return;
        }
      }
      setConversations((prev) => {
        const next = prev.filter((c) => c.id !== id);
        if (id === activeId) {
          setActiveId(next[0]?.id ?? '');
        }
        return next;
      });
      setPendingUserInputs((prev) => {
        const next = { ...prev };
        delete next[id];
        return next;
      });
    },
    [activeId, conversations, confirm]
  );

  // 新建项目 (绑定工作区文件夹, 后端校验路径存在且是目录)
  const handleCreateProject = useCallback(async (name: string, rootPath: string) => {
    const result = await chatApi.createProject(name, rootPath);
    if (!result.ok || !result.project) {
      throw new Error(result.error || '创建项目失败');
    }
    setProjects((prev) => [...prev, result.project as ProjectItem]);
  }, []);

  // 删除项目 (仅解绑会话归入"最近", 磁盘文件不动)
  const handleDeleteProject = useCallback(async (projectId: string) => {
    const project = projects.find((p) => p.project_id === projectId);
    if (!(await confirm(`确定删除项目 "${project?.name ?? projectId}" 吗？\n其下会话将移入"最近", 磁盘文件不受影响。`))) return;
    try {
      const result = await chatApi.deleteProject(projectId);
      if (!result.ok) throw new Error(result.error || '服务拒绝删除项目');
    }
    catch (err) {
      alert(`删除项目失败: ${err instanceof Error ? err.message : String(err)}`);
      return;
    }
    setProjects((prev) => prev.filter((p) => p.project_id !== projectId));
    setConversations((prev) => prev.map((c) => (c.projectId === projectId ? { ...c, projectId: null } : c)));
    if (directoryFilters.current.project_id === projectId) {
      setSidebarProject('');
    } else {
      try { await refreshHistorySidebar(); }
      catch (error) { setPagingError(`项目已删除, 侧栏刷新失败: ${error instanceof Error ? error.message : String(error)}`); }
    }
  }, [projects, confirm, refreshHistorySidebar]);

  // 会话改绑项目 (null = 移出项目归入"最近"; 仅影响之后的工具调用)
  const handleMoveConversation = useCallback(async (projectId: string | null) => {
    const cid = movingConvId;
    if (!cid) return;
    setMovingConvId(null);
    try {
      const result = await chatApi.setConversationProject(cid, projectId);
      if (!result.ok) throw new Error(result.error || '服务拒绝移动会话');
      updateConversation(cid, (c) => ({ ...c, projectId }));
    } catch (err) {
      console.error('[Chat] 会话改绑项目失败:', err);
      alert(`移动会话失败: ${err instanceof Error ? err.message : String(err)}`);
      return;
    }
    try { await refreshHistorySidebar(); }
    catch (error) { setPagingError(`会话已移动, 侧栏刷新失败: ${error instanceof Error ? error.message : String(error)}`); }
  }, [movingConvId, updateConversation, refreshHistorySidebar]);

  // 发送消息
  const handleSend = useCallback(async () => {
    const content = input.trim();
    if ((!content && pendingAttachments.length === 0) || generating || !modelReady || bindingMutations.current.has(activeIdRef.current)) return;
    if (pendingAttachments.some((attachment) => ((attachment.type ?? '').startsWith('image/') && !modelsDetail[settings.model]?.supports_visual_input) || ((attachment.type ?? '').startsWith('video/') && !modelsDetail[settings.model]?.supports_visual_input && !modelsDetail[settings.model]?.supports_native_video_input))) {
      alert('当前模型未启用图像与视频输入, 请切换模型或移除媒体附件');
      return;
    }

    // 无选中会话时自动创建草稿
    let targetConv = active;
    if (!targetConv) {
      targetConv = createDraft();
      setActiveId(targetConv.id);
      setShowEntryChoice(false);
    }

    const atts = pendingAttachments.length > 0 ? [...pendingAttachments] : undefined;
    const nextTurnIndex = targetConv.messages.reduce(
      (maximum, message) => Math.max(maximum, message.turnIndex ?? -1),
      -1,
    ) + 1;
    const userMsg: ChatMessage = {
      id: genId(),
      role: 'user',
      content,
      timestamp: Date.now(),
      attachments: atts,
      turnIndex: nextTurnIndex,
    };
    const assistantId = genId();
    const assistantMsg: ChatMessage = {
      id: assistantId,
      role: 'assistant',
      content: '',
      timestamp: Date.now(),
      streaming: true,
      turnIndex: nextTurnIndex,
      activeVariant: 0,
      variantCount: 0,
    };

    // 乐观更新: 先写入用户消息 + 占位 assistant 消息
    mutationPendingRef.current = true;
    const isDraft = targetConv.id === '__draft__';
    const convId = isDraft ? '__draft__' : targetConv.id;
    updateConversation(convId, (c) => ({
      ...c,
      title: c.messages.length === 0 ? deriveTitle(content) : c.title,
      messages: [...c.messages, userMsg, assistantMsg],
      updatedAt: Math.round(Date.now() / 1000),
    }));
    setInput('');
    setPendingAttachments([]);
    generatingRef.current = true;
    setGenerating(true);
    streamingMsgIdRef.current = assistantId;

    // 草稿或设置已变化的空会话: 等待对应预加载完成
    let realConvId = convId;
    const preloadKey = preloadKeyFor(targetConv.projectId, targetConv.model);
    const shouldRefreshPreload = isDraft || (
      targetConv.preloaded
      && targetConv.preloadKey !== preloadKey
      && !atts
    );
    if (shouldRefreshPreload) {
      try {
        const preloadedId = await startPreload(targetConv, preloadKey);
        if (!preloadedId) throw new Error('后端未能完成会话预加载');
        realConvId = preloadedId;
      } catch (err) {
        console.error('[Chat] 预加载会话失败:', err);
        updateConversation(convId, (c) => ({
          ...c,
          messages: c.messages.map((m) =>
            m.id === assistantId
              ? { ...m, content: `[预加载会话失败] ${err instanceof Error ? err.message : String(err)}`, streaming: false }
              : m
          ),
        }));
        streamingMsgIdRef.current = null;
        generatingRef.current = false;
        setGenerating(false);
        mutationPendingRef.current = false;
        return;
      }
    }

    try {
      const sendResult = await chatApi.send(
        realConvId,
        content,
        settings.think,
        atts,
        {
          ...preloadSettingsFor(targetConv.projectId, targetConv.model),
          ...((isDraft || targetConv.preloaded)
            && (draftApprovalMode !== 'user' || plugins.some((plugin) => plugin.name === 'permissions' && plugin.enabled))
            ? { approvalMode: draftApprovalMode } : {}),
        },
        targetConv.model ?? settings.model,
      );
      updateConversation(realConvId, (conversation) => ({
        ...conversation,
        preloaded: false,
        locallyOpened: true,
        preloadKey: undefined,
        messages: conversation.messages.map((message) => (
          message.id === assistantId || message.id === userMsg.id
            ? { ...message, turnIndex: sendResult.turn_index }
            : message
        )),
      }));
      // 流式内容经 WS 推送, 此处仅等待发送确认
    } catch (err) {
      console.error('[Chat] 发送失败:', err);
      void chatApi.getModel(realConvId).then((binding) => applyModelBinding(realConvId, binding.model)).catch(() => undefined);
      updateConversation(realConvId, (c) => ({
        ...c,
        messages: c.messages.map((m) =>
          m.id === assistantId
            ? { ...m, content: `[发送失败] ${err instanceof Error ? err.message : String(err)}`, streaming: false }
            : m
        ),
      }));
      streamingMsgIdRef.current = null;
      generatingRef.current = false;
      setGenerating(false);
    } finally {
      mutationPendingRef.current = false;
    }
  }, [input, active, generating, modelReady, settings.think, settings.model, modelsDetail, pendingAttachments, updateConversation, createDraft, preloadKeyFor, preloadSettingsFor, startPreload, draftApprovalMode, plugins, applyModelBinding]);

  // 选择文件
  const handleFileSelect = useCallback(async (e: React.ChangeEvent<HTMLInputElement>) => {
    if (!modelReady || bindingMutations.current.has(activeIdRef.current)) return;
    const files = e.target.files;
    if (!files) return;

    // 上传需要预分配的真实 ID, 但此时仍不持久化正式会话
    let targetConv = active;
    if (!targetConv) {
      targetConv = createDraft();
      setActiveId(targetConv.id);
      setShowEntryChoice(false);
    }
    const preloadKey = preloadKeyFor(targetConv.projectId, targetConv.model);
    const needsPreload = targetConv.id === '__draft__' || (
      targetConv.preloaded
      && targetConv.preloadKey !== preloadKey
      && pendingAttachments.length === 0
    );
    if (needsPreload) {
      try {
        const conversationId = await startPreload(targetConv, preloadKey);
        if (!conversationId) throw new Error('后端未能完成会话预加载');
        targetConv = {
          ...targetConv,
          id: conversationId,
          preloaded: true,
          preloadKey,
        };
      } catch (err) {
        console.error('[Chat] 预加载会话失败:', err);
        alert('预加载会话失败, 无法上传文件');
        e.target.value = '';
        return;
      }
    }

    for (const file of Array.from(files)) {
      if ((file.type.startsWith('image/') && !modelsDetail[settings.model]?.supports_visual_input) || (file.type.startsWith('video/') && !modelsDetail[settings.model]?.supports_native_video_input && !modelsDetail[settings.model]?.supports_visual_input)) {
        alert('当前模型未启用图像与视频输入');
        continue;
      }
      const limitMiB = /^(image|video)\//.test(file.type) ? 32 : 10;
      if (file.size > limitMiB * 1024 * 1024) {
        alert(`文件 ${file.name} 超过 ${limitMiB} MiB 限制`);
        continue;
      }
      try {
        const reader = new FileReader();
        const base64 = await new Promise<string>((resolve, reject) => {
          reader.onload = () => resolve((reader.result as string).split(',')[1]);
          reader.onerror = () => reject(new Error(`无法读取 ${file.name}`));
          reader.onabort = () => reject(new Error(`已取消读取 ${file.name}`));
          reader.readAsDataURL(file);
        });
        const result = await chatApi.uploadFile(targetConv.id, file.name, base64);
        if (!result.ok) throw new Error(result.error || '上传失败');
        if (result.ok) {
          setPendingAttachments((prev) => [...prev, {
            name: result.file_name,
            url: result.file_url,
            type: result.file_type,
          }]);
        }
      } catch (err) {
        console.error('[Chat] 上传失败:', err);
        alert(`上传 ${file.name} 失败: ${err instanceof Error ? err.message : String(err)}`);
      }
    }
    // 清空 input 以便重复选择同一文件
    e.target.value = '';
  }, [active, pendingAttachments.length, createDraft, preloadKeyFor, startPreload, modelsDetail, settings.model, modelReady]);

  // 移除待发送附件
  const removeAttachment = useCallback((index: number) => {
    setPendingAttachments((prev) => prev.filter((_, i) => i !== index));
  }, []);

  // Retry: 重试最后一轮
  const handleRetry = useCallback(async () => {
    const currentActive = activeRef.current;
    if (!currentActive || currentActive.id === '__draft__' || currentActive.preloaded || generatingRef.current || bindingMutations.current.has(currentActive.id) || bindingPending.has(currentActive.id) || bindingProblem) return;
    const previousAssistant = currentActive.messages[currentActive.messages.length - 1];
    if (!previousAssistant || previousAssistant.role !== 'assistant') return;
    flushStreamingDeltas();
    const preservedVariants = previousAssistant.variants?.length
      ? previousAssistant.variants
      : [{
        variantIndex: previousAssistant.activeVariant ?? 0,
        content: previousAssistant.content,
        thinking: previousAssistant.thinking,
        toolCalls: previousAssistant.toolCalls ?? [],
        segments: previousAssistant.segments,
        contextStats: previousAssistant.contextStats,
        outputFiles: previousAssistant.outputFiles,
        responseMetadata: previousAssistant.responseMetadata,
      }];
    const assistantId = genId();
    const assistantMsg: ChatMessage = {
      id: assistantId,
      role: 'assistant',
      content: '',
      timestamp: Date.now(),
      streaming: true,
      turnIndex: previousAssistant.turnIndex,
      variants: preservedVariants,
      activeVariant: previousAssistant.activeVariant,
      variantCount: previousAssistant.variantCount ?? preservedVariants.length,
    };
    updateConversation(currentActive.id, (conversation) => ({
      ...conversation,
      messages: [...conversation.messages.slice(0, -1), assistantMsg],
      updatedAt: Math.round(Date.now() / 1000),
    }));
    streamingMsgIdRef.current = assistantId;
    generatingRef.current = true;
    setGenerating(true);
    mutationPendingRef.current = true;
    try {
      const result = await chatApi.retry(currentActive.id, settingsThinkRef.current, currentActive.model);
      if (result.ok) {
        updateStreamingMessage((message) => ({
          ...message,
          turnIndex: result.turn_index,
          activeVariant: result.variant_index,
        }), currentActive.id);
      } else {
        throw new Error(result.error || '重试失败');
      }
    } catch (err) {
      console.error('[Chat] 重试失败:', err);
      void chatApi.getModel(currentActive.id).then((binding) => applyModelBinding(currentActive.id, binding.model)).catch(() => undefined);
      updateConversation(currentActive.id, (conversation) => ({
        ...conversation,
        messages: [...conversation.messages.slice(0, -1), previousAssistant],
      }));
      streamingMsgIdRef.current = null;
      generatingRef.current = false;
      setGenerating(false);
      alert(`重试失败: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      mutationPendingRef.current = false;
    }
  }, [flushStreamingDeltas, updateConversation, updateStreamingMessage, applyModelBinding, bindingPending, bindingProblem]);

  const handleSelectVariant = useCallback(async (turnIndex: number, variantIndex: number) => {
    const currentActive = activeRef.current;
    if (!currentActive || currentActive.id === '__draft__' || currentActive.preloaded || generatingRef.current) return;
    try {
      const result = await chatApi.selectVariant(currentActive.id, turnIndex, variantIndex);
      const turn = result.turn;
      const selected = turn.variants.find(
        (variant) => variant.variant_index === turn.active_variant,
      );
      if (!selected) throw new Error('后端未返回选中的回复版本');
      updateConversation(currentActive.id, (conversation) => ({
        ...conversation,
        messages: conversation.messages.map((message) => (
          message.role === 'assistant' && message.turnIndex === turnIndex
            ? {
              ...message,
              content: variantAnswer(selected),
              thinking: selected.thinking ?? undefined,
              toolCalls: selected.tool_calls,
              segments: convertSegments(selected),
              activeVariant: turn.active_variant,
              variantCount: turn.variant_count,
              variantIndices: turn.variants.map((variant) => variant.variant_index),
              variants: turn.variants.map(convertVariant),
              contextStats: selected.context_stats,
              outputFiles: selected.output_files,
              responseMetadata: selected.response_metadata,
            }
            : message
        )),
      }));
    } catch (err) {
      console.error('[Chat] 切换回复版本失败:', err);
      alert(`切换回复版本失败: ${err instanceof Error ? err.message : String(err)}`);
    }
  }, [updateConversation]);

  // Fork: 从指定轮次创建新会话
  const handleFork = useCallback(async (turnIndex: number) => {
    const currentActive = activeRef.current;
    if (!currentActive || currentActive.id === '__draft__' || currentActive.preloaded) return;
    let result: Awaited<ReturnType<typeof chatApi.fork>>;
    try {
      result = await chatApi.fork(currentActive.id, turnIndex);
      if (!result.ok || !result.conversation_id) throw new Error('服务未返回新分支');
    } catch (err) {
      alert(`Fork 失败: ${err instanceof Error ? err.message : String(err)}`);
      return;
    }
    const id = result.conversation_id;
    setConversations((current) => [{ ...currentActive, id, title: `${currentActive.title} (分支)`,
      messages: [], loaded: false, locallyOpened: true, updatedAt: Date.now() / 1000 }, ...current.filter((item) => item.id !== id)]);
    setActiveId(id);
    try {
      const { conversations: items } = await chatApi.getConversation(id);
      if (!items.some((item) => item.conversation_id === id)) throw new Error('新分支暂时不可读取');
      mergeHistoryItems(items);
    } catch (error) { setPagingError(`分支已创建 (${id}), 读取失败: ${error instanceof Error ? error.message : String(error)}`); }
    try { await refreshHistorySidebar(); }
    catch (error) { setPagingError(`分支已创建 (${id}), 侧栏刷新失败: ${error instanceof Error ? error.message : String(error)}`); }
  }, [refreshHistorySidebar, mergeHistoryItems]);

  // 删除模型配置
  const handleDeleteModel = useCallback(async (name: string) => {
    if (!(await confirm(`确定删除模型配置 "${name}" 吗？`))) return;
    try {
      const result = await chatApi.deleteModel(name);
      if (result.ok) {
        await handleModelSaved();
      } else {
        alert(result.error || '删除失败');
      }
    } catch (err) {
      console.error('[Chat] 删除模型失败:', err);
      alert(`删除模型失败: ${err instanceof Error ? err.message : String(err)}`);
    }
  }, [handleModelSaved, confirm]);

  // 停止生成 (调用后端取消 + 解除前端流式标记)
  const handleAnswerUserInput = useCallback(async (answerValue: string) => {
    const answer = answerValue.trim();
    if (!active || !activePendingUserInput || !answer || answeringRequestId) return;
    const requestId = activePendingUserInput.requestId;
    setAnsweringRequestId(requestId);
    try {
      await chatApi.answerAskUser(active.id, requestId, answer);
      setPendingUserInputs((prev) => ({
        ...prev,
        [active.id]: (prev[active.id] ?? []).filter((item) => item.requestId !== requestId),
      }));
      setPendingUserAnswers((prev) => {
        const next = { ...prev };
        delete next[requestId];
        return next;
      });
    } catch (err) {
      console.error('[Chat] 提交工具回答失败:', err);
      alert(`提交回答失败: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setAnsweringRequestId(null);
    }
  }, [active, activePendingUserInput, answeringRequestId]);

  const handleStop = useCallback(async () => {
    const currentActive = activeRef.current;
    flushStreamingDeltas();
    if (currentActive) {
      try {
        await chatApi.cancel(currentActive.id);
      } catch (err) {
        console.error('[Chat] 取消生成失败:', err);
      }
    }
    generatingRef.current = false;
    setGenerating(false);
    if (currentActive) {
      const pendingIds = new Set(
        (pendingUserInputs[currentActive.id] ?? []).map((item) => item.requestId),
      );
      setPendingUserAnswers((answers) => Object.fromEntries(
        Object.entries(answers).filter(([requestId]) => !pendingIds.has(requestId)),
      ));
      setPendingUserInputs((prev) => ({ ...prev, [currentActive.id]: [] }));
    }
    streamingMsgIdRef.current = null;
    if (currentActive) {
      updateConversation(currentActive.id, (c) => ({
        ...c,
        messages: c.messages.map((m) => (m.streaming ? { ...m, streaming: false } : m)),
      }));
    }
  }, [flushStreamingDeltas, pendingUserInputs, updateConversation]);

  // 键盘发送: Enter 发送, Shift+Enter 换行
  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        if (!activePendingUserInput) void handleSend();
      }
    },
    [activePendingUserInput, handleSend]
  );

  // 切换插件聚合启停
  const togglePlugin = useCallback(async (name: string, enabled: boolean) => {
    try {
      const result = await chatApi.setPluginEnabled(name, enabled);
      setPlugins((prev) => prev.map((p) => (p.name === name ? { ...p, enabled } : p)));
      if (result.applied === false) {
        alert(`插件配置已保存, 但有 ${result.failed || 0} 个活跃会话应用失败`);
      }
    } catch (err) {
      console.error('[Chat] 切换插件失败:', err);
      alert(`切换插件失败: ${err instanceof Error ? err.message : String(err)}`);
    }
  }, []);

  // 切换插件内单项能力独立启停 (双层模型: 能力生效 = 插件启用 ∧ 独立启用)
  const toggleCapability = useCallback(
    async (pluginName: string, kind: CapabilityKind, capName: string, enabled: boolean) => {
      try {
        const result = await chatApi.setPluginCapability(pluginName, kind, capName, enabled);
        setPlugins((prev) =>
          prev.map((p) =>
            p.name === pluginName
              ? {
                  ...p,
                  capabilities: {
                    ...p.capabilities,
                    [kind]: p.capabilities[kind].map((c) => (c.name === capName ? { ...c, enabled } : c)),
                  },
                }
              : p
          )
        );
        if (result.applied === false) {
          alert(`能力配置已保存, 但有 ${result.failed || 0} 个活跃会话应用失败`);
        }
      } catch (err) {
        console.error('[Chat] 切换能力失败:', err);
        alert(`切换能力失败: ${err instanceof Error ? err.message : String(err)}`);
      }
    },
    []
  );

  const handlePluginConfigSaved = useCallback(() => {
    setPluginConfigRevision((revision) => revision + 1);
  }, []);

  // 能力弹窗当前插件对象
  const activeCapabilityPlugin = plugins.find((p) => p.name === capabilityPlugin) ?? null;

  // 后端未连接提示
  if (backendOk === false) {
    return (
      <div className="flex h-screen flex-col gap-2 overflow-hidden p-2">
        <ChatHeader
          theme={theme}
          title="Satrap 对话"
          subtitle="聊天服务未连接"
          listOpen={false}
          onToggleList={() => undefined}
          onToggleTheme={toggleTheme}
          onBack={() => navigate('/')}
          onSettings={() => setSettingsOpen(true)}
        />
        <div className="surface flex min-h-0 flex-1 flex-col items-center justify-center gap-3 !rounded-[20px] p-6 text-center">
          <div className="tint-orange grid h-14 w-14 place-items-center rounded-2xl bg-[rgb(var(--rgb-orange)/0.12)] text-orange">
            <Bot className="h-7 w-7" />
          </div>
          <p className="text-base font-semibold text-fg">聊天服务未连接</p>
          <p className="text-sm text-fg-3">请先启动聊天服务: python -m satrap.display.server</p>
          <Button variant="default" title="对话设置" onClick={() => setSettingsOpen(true)}>
            <Settings2 className="mr-2 h-4 w-4" />对话设置
          </Button>
        </div>
        <Modal open={settingsOpen} onClose={() => setSettingsOpen(false)} title="对话设置" size="md">
          <button
            onClick={() => setHistoryOpen(true)}
            className="tile tint-accent w-full rounded-xl px-4 py-3 text-left"
          >
            <div className="flex items-center gap-2">
              <HardDrive className="h-4 w-4 shrink-0 text-accent" />
              <span className="text-sm font-medium text-fg">管理会话历史</span>
            </div>
            <p className="mt-1 text-xs text-fg-3">Chat 服务停止时使用冷管理读取历史和回收站</p>
          </button>
        </Modal>
        <SessionPluginSettingsModal
          context={sessionConfigPlugin && activeId ? { platformId: 'chat', sessionId: activeId } : null}
          plugins={sessionConfigPlugin ? [sessionConfigPlugin] : []}
          onClose={() => setSessionConfigPlugin(null)}
        />
        <ChatHistoryManager
          open={historyOpen}
          onClose={() => setHistoryOpen(false)}
          projects={projects}
          models={models}
          activeConversationId={activeId}
          preferCold
          onOpenConversation={() => undefined}
          onChanged={() => undefined}
        />
      </div>
    );
  }

  const showWelcome = !active || active.messages.length === 0;
  const rounds = active ? Math.ceil(active.messages.length / 2) : 0;
  const activeProject = projects.find((project) => project.project_id === active?.projectId);
  const headerTitle = !active
    ? 'Satrap 对话'
    : active.messages.length === 0
      ? (active.id === '__draft__' ? '新对话' : active.title)
      : active.title;
  const headerSubtitle = active
    ? `${activeProject?.name ?? '最近'} · ${settings.model}`
    : 'Satrap';
  const toggleSidebar = () => {
    const mobile = typeof window !== 'undefined' && window.matchMedia('(max-width: 767px)').matches;
    if (mobile) setMobileList((value) => !value);
    else setListOpen((value) => !value);
  };
  const selectConversation = (id: string) => {
    setActiveId(id);
    setShowEntryChoice(false);
    setMobileList(false);
  };
  const startNewConversation = (projectId?: string) => {
    handleNew(projectId);
    setMobileList(false);
  };

  return (
    <div className="flex h-screen flex-col gap-2 overflow-hidden p-2">
      <ChatHeader
        theme={theme}
        title={headerTitle}
        subtitle={headerSubtitle}
        rounds={rounds}
        listOpen={typeof window !== 'undefined' && window.matchMedia('(max-width: 767px)').matches ? mobileList : listOpen}
        onToggleList={toggleSidebar}
        onToggleTheme={toggleTheme}
        onBack={() => navigate('/')}
        onSettings={() => setSettingsOpen(true)}
      />

      <div className="flex min-h-0 flex-1 gap-2">
        {mobileList && (
          <div
            className="fixed inset-0 z-30 bg-black/35 backdrop-blur-sm md:hidden"
            aria-hidden
            onClick={() => setMobileList(false)}
          />
        )}
        {/* 会话列表侧栏 */}
        <aside
          aria-label="会话列表"
          className={cn(
            'surface flex min-h-0 flex-none flex-col overflow-hidden !rounded-[20px] transition-all duration-500 [transition-timing-function:var(--ease-out)]',
            'fixed inset-y-2 left-2 z-40 w-[min(268px,calc(100vw-24px))] md:static md:inset-auto md:z-auto md:w-[268px]',
            mobileList ? 'flex opacity-100' : 'hidden md:flex',
            listOpen ? 'md:opacity-100' : 'md:w-0 md:border-0 md:opacity-0',
          )}
        >
          <div className="h-full min-h-0 w-[268px] max-w-full">
            <ChatSidebar
              search={search}
              onSearch={setSearch}
              projectFilter={sidebarProject}
              onProjectFilter={setSidebarProject}
              projects={projects}
              grouped={grouped}
              activeId={active?.id ?? ''}
              directoryLoading={directoryLoading}
              pagingError={pagingError}
              conversationsCount={conversations.length}
              nextConversations={nextConversations}
              onLoadMore={() => void loadConversationPage()}
              collapsedProjects={collapsedProjects}
              onToggleProject={(projectId) => setCollapsedProjects((prev) => ({ ...prev, [projectId]: !prev[projectId] }))}
              onCreateProject={() => setProjectDialogOpen(true)}
              onNew={() => startNewConversation()}
              onNewInProject={(projectId) => startNewConversation(projectId)}
              onDeleteProject={(projectId) => void handleDeleteProject(projectId)}
              onSelect={selectConversation}
              onDelete={(id) => void handleDelete(id)}
              onMove={(id) => setMovingConvId(id)}
            />
          </div>
        </aside>

        {/* 聊天主区 */}
        <main className="surface relative flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden !rounded-[20px]">
          {pagingError && (
            <div role="alert" className="mx-3 mt-3 rounded-xl border border-[rgb(var(--rgb-red)/0.3)] bg-[rgb(var(--rgb-red)/0.1)] px-3 py-2 text-sm text-red">
              {pagingError}
            </div>
          )}

          {active && active.id !== "__draft__" && !active.preloaded && (
            <RunRecovery conversation={active.id} generating={generating} />
          )}

          {/* 消息流 */}
          <div
            ref={messageScroll.containerRef}
            onScroll={() => {
              messageScroll.onScroll();
              if ((messageScroll.containerRef.current?.scrollTop ?? 100) < 80 && active) void loadOlder(active);
            }}
            style={{ overflowAnchor: 'none' }}
            className="scroll relative z-10 min-h-0 flex-1 overflow-y-auto custom-scrollbar"
          >
            <div ref={messageScroll.contentRef} className="mx-auto flex min-h-full w-full max-w-3xl flex-col px-3 py-5 md:px-6 md:py-7">
              {showEntryChoice ? (
                <div className="flex flex-1 items-center justify-center py-8">
                  <div className="w-full max-w-sm space-y-3">
                    <button
                      onClick={() => {
                        setShowEntryChoice(false);
                        if (conversations.length > 0) setActiveId(conversations[0].id);
                      }}
                      className="tile tint-accent w-full rounded-xl px-4 py-3.5 text-left"
                    >
                      <div className="flex items-center gap-2">
                        <MessageSquare className="h-4 w-4 shrink-0 text-accent" />
                        <span className="text-sm font-medium text-fg">继续最近对话</span>
                      </div>
                      <p className="mt-1 truncate text-xs text-fg-3">
                        {conversations[0]?.title ?? ''}
                      </p>
                    </button>
                    <button
                      onClick={() => {
                        setShowEntryChoice(false);
                        startNewConversation();
                      }}
                      className="tile w-full rounded-xl px-4 py-3.5 text-left"
                    >
                      <div className="flex items-center gap-2">
                        <Plus className="h-4 w-4 shrink-0 text-accent" />
                        <span className="text-sm font-medium text-fg">开始新对话</span>
                      </div>
                    </button>
                  </div>
                </div>
              ) : showWelcome ? (
                <EmptyWelcome onPromptClick={(text) => setInput(text)} />
              ) : (
                <div className="flex flex-col gap-6 md:gap-7">
                  {active.hasOlder && (
                    <div className="flex justify-center">
                      <Button variant="ghost" size="sm" onClick={() => void loadOlder(active)}>加载更早消息</Button>
                    </div>
                  )}
                  {active.messages.map((msg, idx) => (
                    <MessageBubble
                      key={msg.id}
                      message={msg}
                      conversationId={active.id}
                      isLast={idx === active.messages.length - 1}
                      onRetry={handleRetry}
                      onFork={handleFork}
                      onSelectVariant={handleSelectVariant}
                    />
                  ))}
                </div>
              )}
            </div>
          </div>

          {/* 输入区 */}
          <div className="relative z-20 flex-none bg-gradient-to-t from-[var(--glass-bg-strong)] via-[var(--glass-bg-strong)] to-transparent px-3 pb-3 pt-2 md:px-6">
            <div className="mx-auto w-full max-w-3xl">
              {modelBusy && <p className="mb-2 px-1 text-xs text-fg-2">正在确认会话模型…</p>}
              {bindingProblem && <p role="alert" className="mb-2 px-1 text-xs text-red">{bindingProblem}</p>}
              {modelsLoaded && !modelBusy && !models.includes(settings.model) && (
                <p role="alert" className="mb-2 px-1 text-xs text-red">
                  模型 {settings.model} 不可用, 请展开选项手动选择
                </p>
              )}
              {optionsOpen && (
                <div className="mb-2 flex justify-end">
                  <OptionsPanel
                    modelDisabled={generating || modelBusy}
                    approval={approval}
                    think={settings.think}
                    model={settings.model}
                    modelOptions={modelOptions}
                    thinkingOptions={thinkingOptions}
                    onToggleThink={(v) => updateSettings('think', v)}
                    onModelChange={(v) => updateSettings('model', v)}
                  />
                </div>
              )}

              {pendingAttachments.length > 0 && (
                <div className="mb-2 flex flex-wrap gap-2">
                  {pendingAttachments.map((att, i) => (
                    <MediaAttachment key={att.url} attachment={att} conversationId={active?.id ?? ''}
                      onRemove={() => removeAttachment(i)} />
                  ))}
                </div>
              )}

              {activePendingUserInput && (
                <AskUserPanel
                  request={activePendingUserInput}
                  answer={activePendingUserAnswer}
                  queueLength={pendingUserInputs[activeId]?.length ?? 1}
                  submitting={answeringRequestId === activePendingUserInput.requestId}
                  onAnswerChange={(answer) => setPendingUserAnswers((prev) => ({
                    ...prev,
                    [activePendingUserInput.requestId]: answer,
                  }))}
                  onSubmit={(answer) => void handleAnswerUserInput(answer)}
                />
              )}

              <div className="surface surface-strong !rounded-[22px] p-2 transition-shadow duration-300 focus-within:!shadow-[var(--shadow-2),0_0_0_4px_rgb(var(--rgb-accent)/0.12)]">
                <input
                  ref={fileInputRef}
                  type="file"
                  multiple
                  accept={(modelsDetail[settings.model]?.supports_visual_input ? 'image/*,video/*,' : modelsDetail[settings.model]?.supports_native_video_input ? 'video/*,' : '') + '.txt,.md,.pdf'}
                  onChange={handleFileSelect}
                  className="hidden"
                />
                <textarea
                  ref={textareaRef}
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  onKeyDown={handleKeyDown}
                  placeholder={activePendingUserInput
                    ? '请先回答上方的工具问题'
                    : '输入消息, Enter 发送, Shift+Enter 换行'}
                  disabled={Boolean(activePendingUserInput)}
                  rows={1}
                  className="scroll block max-h-[200px] w-full resize-none bg-transparent px-3 pb-1 pt-2.5 text-[14.5px] leading-7 text-fg outline-none placeholder:text-fg-4 disabled:opacity-50"
                />
                <div className="flex flex-wrap items-center gap-1.5 px-1 pt-1">
                  <button
                    type="button"
                    title="附件"
                    aria-label="附件"
                    disabled={Boolean(activePendingUserInput)}
                    onClick={() => fileInputRef.current?.click()}
                    className="ctl ctl-ghost ctl-icon ctl-sm"
                  >
                    <Paperclip className="h-4 w-4" />
                  </button>
                  <ThinkDial
                    value={settings.think}
                    options={thinkingOptions}
                    disabled={Boolean(activePendingUserInput)}
                    onChange={(value) => updateSettings('think', value)}
                  />
                  <span className="chip tint-accent hidden max-w-[160px] items-center gap-1.5 sm:inline-flex">
                    <Cpu className="h-3 w-3 shrink-0" />
                    <span className="truncate">{settings.model}</span>
                  </span>
                  <div className="ml-auto flex items-center gap-1.5">
                    <button
                      type="button"
                      onClick={() => setOptionsOpen((v) => !v)}
                      className={cn('ctl ctl-ghost ctl-icon ctl-sm', optionsOpen && 'text-accent')}
                      title={optionsOpen ? '收起选项' : '展开选项'}
                      aria-label={optionsOpen ? '收起选项' : '展开选项'}
                      disabled={Boolean(activePendingUserInput)}
                    >
                      <ChevronDown className={cn('h-4 w-4 transition-transform duration-300', optionsOpen && 'rotate-180')} />
                    </button>
                    {generating ? (
                      <button
                        type="button"
                        title="停止生成"
                        aria-label="停止生成"
                        onClick={() => void handleStop()}
                        className="grid h-9 w-9 place-items-center rounded-full border border-[var(--glass-border-strong)] bg-glass-hover text-fg transition hover:scale-105 active:scale-95"
                      >
                        <Square className="h-3.5 w-3.5 fill-current" />
                      </button>
                    ) : (
                      <button
                        type="button"
                        title="发送"
                        aria-label="发送"
                        onClick={() => void handleSend()}
                        disabled={!modelReady || (!input.trim() && pendingAttachments.length === 0)}
                        className={cn(
                          'grid h-9 w-9 place-items-center rounded-full transition-all duration-300 [transition-timing-function:var(--ease-spring)]',
                          input.trim() || pendingAttachments.length > 0
                            ? 'bg-[rgb(var(--rgb-accent))] text-white shadow-[0_6px_20px_-4px_rgb(var(--rgb-accent)/0.8)] hover:scale-110 active:scale-95'
                            : 'bg-glass-hover text-fg-4',
                          (!modelReady || (!input.trim() && pendingAttachments.length === 0)) && 'opacity-50',
                        )}
                      >
                        <ArrowUp className="h-4 w-4" strokeWidth={2.5} />
                      </button>
                    )}
                  </div>
                </div>
              </div>
              <p className="mt-2 text-center text-[11px] text-fg-4">
                内容由模型生成, 请注意甄别
              </p>
            </div>
          </div>
        </main>
      </div>

      {/* 聊天设置弹窗 */}
      <ChatSettingsModal
        defaults={defaults}
        defaultSaving={defaultSaving}
        onDefaultModel={(model) => void saveDefaultModel(model)}
        modelDisabled={generating || modelBusy}
        approval={approval}
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        settings={settings}
        modelOptions={modelOptions}
        thinkingOptions={thinkingOptions}
        onChange={updateSettings}
        plugins={plugins}
        onTogglePlugin={togglePlugin}
        onViewCapabilities={(name) => setCapabilityPlugin(name)}
        onViewConfig={(name) => setConfigPlugin(name)}
        onOpenMemory={() => setMemoryOpen(true)}
        onOpenRag={() => setRagOpen(true)}
        onOpenHistory={() => setHistoryOpen(true)}
        modelsDetail={modelsDetail}
        onAddModel={() => setEditingModel('')}
        onEditModel={(name) => setEditingModel(name)}
        onDeleteModel={handleDeleteModel}
      />

      {/* 模型编辑弹窗 */}
      <ModelEditModal
        modelName={editingModel}
        modelsDetail={modelsDetail}
        onClose={() => setEditingModel(null)}
        onSaved={handleModelSaved}
      />

      {/* 插件能力弹窗 */}
      <PluginCapabilitiesModal
        plugin={activeCapabilityPlugin}
        onClose={() => setCapabilityPlugin(null)}
        onToggleCapability={toggleCapability}
      />

      {/* 插件配置弹窗 */}
      <PluginConfigModal
        pluginName={configPlugin}
        onSessionConfig={activeId && activeId !== '__draft__' ? () => { setSessionConfigPlugin(configPlugin); setConfigPlugin(null); } : undefined}
        onClose={() => setConfigPlugin(null)}
        onSaved={handlePluginConfigSaved}
      />

      <ChatHistoryManager
        open={historyOpen}
        onClose={() => setHistoryOpen(false)}
        projects={projects}
        models={models}
        activeConversationId={activeId}
        onOpenConversation={async (conversationId) => {
          if (!conversationsRef.current.some((item) => item.id === conversationId)) {
            try {
              const result = await chatApi.getConversation(conversationId);
              const item = result.conversations.find((entry) => entry.conversation_id === conversationId);
              if (!item) throw new Error('对话不存在或已移除');
              setConversations((current) => current.some((entry) => entry.id === conversationId) ? current : [...current, { ...restoreConversation<ChatMessage>(item), locallyOpened: true }]);
            } catch (error) {
              setPagingError(error instanceof Error ? error.message : '对话读取失败');
              return;
            }
          }
          updateConversation(conversationId, (item) => ({ ...item, locallyOpened: true }));
          setActiveId(conversationId);
          setShowEntryChoice(false);
          setHistoryOpen(false);
          setSettingsOpen(false);
        }}
        onChanged={refreshHistorySidebar}
      />

      <Modal open={ragOpen} onClose={() => setRagOpen(false)} title="RAG 知识库" size="lg">
        {ragOpen && <RagManager key={activeId} context={{ platformId: 'chat', sessionId: activeId && activeId !== '__draft__' ? activeId : undefined }} />}
      </Modal>

      {/* 记忆管理面板 */}
      <MemoryPanel
        open={memoryOpen}
        onClose={() => setMemoryOpen(false)}
        conversationId={activeId === '__draft__' || active?.preloaded ? '' : activeId}
      />

      {/* 新建项目对话框 */}
      <ProjectDialog
        open={projectDialogOpen}
        onClose={() => setProjectDialogOpen(false)}
        onCreate={handleCreateProject}
      />

      {/* 会话改绑项目对话框 */}
      <Modal
        open={movingConvId !== null}
        onClose={() => setMovingConvId(null)}
        title="移动会话到项目"
        size="sm"
      >
        <div className="space-y-2">
          <p className="text-xs text-fg-3">仅影响之后的工具调用可见范围, 历史消息不变</p>
          <button
            onClick={() => void handleMoveConversation(null)}
            className="tile w-full rounded-xl px-3 py-2.5 text-left"
          >
            <div className="flex items-center gap-2">
              <MessageSquare className="h-4 w-4 shrink-0 text-fg-3" />
              <span className="text-sm text-fg">最近 (无项目)</span>
            </div>
          </button>
          {projects.map((p) => (
            <button
              key={p.project_id}
              onClick={() => void handleMoveConversation(p.project_id)}
              className="tile tint-accent w-full rounded-xl px-3 py-2.5 text-left"
            >
              <div className="flex items-center gap-2">
                <Folder className="h-4 w-4 shrink-0 text-accent" />
                <span className="truncate text-[13px] text-fg">{p.name}</span>
              </div>
              <p className="mt-0.5 truncate text-xs text-fg-3">{p.root_path}</p>
            </button>
          ))}
          {projects.length === 0 && (
            <p className="text-xs text-fg-3">暂无项目, 可先在侧边栏新建</p>
          )}
        </div>
      </Modal>
    </div>
  );
}

// 新建项目对话框 (名称 + 工作区路径, 后端校验路径存在且是目录; 支持浏览选择目录)
function ProjectDialog({
  open,
  onClose,
  onCreate,
}: {
  open: boolean;
  onClose: () => void;
  onCreate: (name: string, rootPath: string) => Promise<void>;
}) {
  const [name, setName] = useState('');
  const [rootPath, setRootPath] = useState('');
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);
  // 目录选择器开关
  const [pickerOpen, setPickerOpen] = useState(false);

  useEffect(() => {
    if (open) {
      setName('');
      setRootPath('');
      setError('');
    }
  }, [open]);

  // 浏览选择回填: 名称为空时顺手用目录名填入
  const handlePick = (p: string) => {
    setRootPath(p);
    if (!name.trim()) {
      const base = p.replace(/[\\/]+$/, '').split(/[\\/]/).pop() ?? '';
      if (base) setName(base);
    }
  };

  const handleSubmit = async () => {
    if (!name.trim() || !rootPath.trim()) return;
    setSaving(true);
    setError('');
    try {
      await onCreate(name.trim(), rootPath.trim());
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal open={open} onClose={onClose} title="新建项目" size="sm">
      <div className="space-y-3">
        <p className="text-[13px] leading-5 text-fg-3">
          项目绑定一个工作区文件夹, 项目下的对话可读写该文件夹, 并拥有独立的项目记忆层
        </p>
        {error && <p role="alert" className="rounded-lg border border-[rgb(var(--rgb-red)/0.3)] bg-[rgb(var(--rgb-red)/0.1)] px-3 py-2 text-sm text-red">{error}</p>}
        <input
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="项目名称"
          aria-label="项目名称"
          className="field"
        />
        <div className="flex gap-2">
          <input
            type="text"
            value={rootPath}
            onChange={(e) => setRootPath(e.target.value)}
            placeholder="工作区文件夹绝对路径, 如 F:\work\my-project"
            aria-label="工作区文件夹路径"
            className="field min-w-0 flex-1"
          />
          <button
            type="button"
            title="浏览服务器目录"
            aria-label="浏览服务器目录"
            onClick={() => setPickerOpen(true)}
            className="ctl ctl-ghost ctl-icon flex-none"
          >
            <FolderOpen className="h-4 w-4" />
          </button>
        </div>
        <div className="flex justify-end gap-2 pt-1">
          <Button variant="ghost" onClick={onClose}>取消</Button>
          <Button
            variant="primary"
            onClick={() => void handleSubmit()}
            disabled={saving || !name.trim() || !rootPath.trim()}
          >
            {saving ? '创建中...' : '创建'}
          </Button>
        </div>
      </div>
      <DirectoryPicker
        open={pickerOpen}
        initialPath={rootPath}
        onClose={() => setPickerOpen(false)}
        onSelect={handlePick}
      />
    </Modal>
  );
}

// 目录选择对话框 (浏览服务器文件系统, 只列目录; Windows 空路径为盘符视图)
function DirectoryPicker({
  open,
  initialPath,
  onClose,
  onSelect,
}: {
  open: boolean;
  initialPath: string;    // 打开时的初始定位 ('' = 根视图)
  onClose: () => void;
  onSelect: (path: string) => void;
}) {
  const [current, setCurrent] = useState('');
  const [location, setLocation] = useState('');
  const generation = useRef(0);
  const [parent, setParent] = useState<string | null>(null);
  const [dirs, setDirs] = useState<DirEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const load = useCallback(async (path: string) => {
    const version = ++generation.current;
    setLoading(true);
    setError('');
    try {
      const resp = await chatApi.browseDirs(path);
      if (version !== generation.current) return;
      setCurrent(resp.path);
      setLocation(resp.path);
      setParent(resp.parent);
      setDirs(resp.dirs);
    } catch (err) {
      if (version === generation.current) setError(err instanceof Error ? err.message : String(err));
    } finally {
      if (version === generation.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (open) void load(initialPath.trim());
    return () => { generation.current++; };
  }, [open, initialPath, load]);

  // 根视图 (盘符页) 无具体路径可选
  const canSelect = current !== '' && !loading && !error && location.trim() === current;

  return (
    <Modal open={open} onClose={onClose} title="选择工作区目录" size="md">
      <div className="space-y-3">
        <div className="flex items-center gap-2">
          <button
            type="button"
            title="上一级"
            aria-label="上一级"
            disabled={parent === null || loading}
            onClick={() => parent !== null && void load(parent)}
            className="ctl ctl-ghost ctl-icon ctl-sm flex-none"
          >
            <ArrowUp className="h-4 w-4" />
          </button>
          <input
            aria-label="目录路径"
            className="field min-w-0 flex-1"
            value={location}
            onChange={(event) => setLocation(event.target.value)}
            onKeyDown={(event) => { if (event.key === 'Enter') { event.preventDefault(); void load(location.trim()); } }}
            placeholder="选择磁盘或输入服务器目录路径"
          />
          <Button variant="ghost" size="sm" disabled={loading} onClick={() => void load(location.trim())}>前往</Button>
        </div>
        {error && <p role="alert" className="rounded-lg border border-[rgb(var(--rgb-red)/0.3)] bg-[rgb(var(--rgb-red)/0.1)] px-3 py-2 text-sm text-red">{error}</p>}
        <div className="scroll max-h-72 space-y-0.5 rounded-xl border border-line p-1">
          {loading ? (
            <p className="px-2 py-2 text-sm text-fg-3">加载中...</p>
          ) : dirs.length === 0 ? (
            <p className="px-2 py-2 text-sm text-fg-3">无子目录</p>
          ) : (
            dirs.map((d) => (
              <button
                key={d.path}
                onClick={() => void load(d.path)}
                className="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left transition-colors hover:bg-glass-hover"
              >
                {current === '' ? (
                  <HardDrive className="h-4 w-4 flex-none text-accent" />
                ) : (
                  <Folder className="h-4 w-4 flex-none text-accent" />
                )}
                <span className="truncate text-[13px] text-fg">{d.name}</span>
              </button>
            ))
          )}
        </div>
        <div className="flex justify-end gap-2 pt-1">
          <Button variant="ghost" onClick={onClose}>取消</Button>
          <Button variant="primary" disabled={!canSelect} onClick={() => { onSelect(current); onClose(); }}>
            选择此目录
          </Button>
        </div>
      </div>
    </Modal>
  );
}

// 折叠选项面板 (思考 / 模型 / 执行审批)
function OptionsPanel({
  modelDisabled,
  approval,
  think,
  model,
  modelOptions,
  thinkingOptions,
  onToggleThink,
  onModelChange,
}: {
  modelDisabled: boolean;
  approval: ApprovalControl;
  think: string;
  model: string;
  modelOptions: { value: string; label: string }[];
  thinkingOptions: { value: string; label: string }[];
  onToggleThink: (v: string) => void;
  onModelChange: (v: string) => void;
}) {
  return (
    <div className="surface surface-strong tint-accent flex w-fit max-w-full flex-wrap items-center gap-3 !rounded-2xl px-3 py-2 animate-pop">
      {/* think 选择 */}
      <div className="flex items-center gap-2">
        <Brain className={cn('h-4 w-4', think !== 'off' ? 'text-accent' : 'text-fg-4')} />
        <span className="text-sm text-fg-2">思考</span>
        <div className="w-28">
          <Select
            value={think}
            onChange={(e) => onToggleThink(e.target.value)}
            options={thinkingOptions}
          />
        </div>
      </div>

      <div className="hidden h-5 w-px bg-[var(--glass-border-strong)] sm:block" />

      {/* model 选择 */}
      <div className="flex items-center gap-2">
        <span className="text-sm text-fg-2">模型</span>
        <div className="w-40">
          <Select
            value={model}
            disabled={modelDisabled}
            onChange={(e) => onModelChange(e.target.value)}
            options={modelOptions}
          />
        </div>
      </div>
      <div className="hidden h-5 w-px bg-[var(--glass-border-strong)] sm:block" />
      <ApprovalSettings approval={approval} compact />
    </div>
  );
}

// 聊天设置弹窗分类
type ChatSettingsCategory = 'chat' | 'models' | 'plugins' | 'data';

const CHAT_SETTINGS_CATEGORIES: { key: ChatSettingsCategory; label: string; icon: LucideIcon }[] = [
  { key: 'chat', label: '对话', icon: SlidersHorizontal },
  { key: 'models', label: '模型管理', icon: Cpu },
  { key: 'plugins', label: '插件', icon: Puzzle },
  { key: 'data', label: '数据管理', icon: Database },
];

// 聊天设置弹窗: 左侧分类导航 + 右侧分类内容
function ChatSettingsModal({
  defaults,
  defaultSaving,
  onDefaultModel,
  modelDisabled,
  approval,
  open,
  onClose,
  settings,
  modelOptions,
  thinkingOptions,
  onChange,
  plugins,
  onTogglePlugin,
  onViewCapabilities,
  onViewConfig,
  onOpenMemory,
  onOpenRag,
  onOpenHistory,
  modelsDetail,
  onAddModel,
  onEditModel,
  onDeleteModel,
}: {
  defaults: ChatDefaults | null;
  defaultSaving: boolean;
  onDefaultModel: (model: string) => void;
  modelDisabled: boolean;
  approval: ApprovalControl;
  open: boolean;
  onClose: () => void;
  settings: ChatSettings;
  modelOptions: { value: string; label: string }[];
  thinkingOptions: { value: string; label: string }[];
  onChange: <K extends keyof ChatSettings>(key: K, value: ChatSettings[K]) => void;
  plugins: ChatPlugin[];
  onTogglePlugin: (name: string, enabled: boolean) => void;
  onViewCapabilities: (name: string) => void;
  onViewConfig: (name: string) => void;
  onOpenMemory: () => void;
  onOpenRag: () => void;
  onOpenHistory: () => void;
  modelsDetail: Record<string, ModelConfigItem>;
  onAddModel: () => void;
  onEditModel: (name: string) => void;
  onDeleteModel: (name: string) => void;
}) {
  const [category, setCategory] = useState<ChatSettingsCategory>('chat');

  // 每次打开重置到第一个分类
  useEffect(() => {
    if (open) setCategory('chat');
  }, [open]);

  return (
    <Modal open={open} onClose={onClose} title="对话设置" size="3xl"
      footer={<Button variant="primary" onClick={onClose}>完成</Button>}>
      <div className="flex h-[62vh] min-h-[420px] flex-col gap-3 sm:flex-row sm:gap-4">
        {/* 左侧分类导航 */}
        <nav className="grid flex-none grid-cols-2 gap-1 border-b border-line pb-2 sm:flex sm:w-44 sm:flex-col sm:border-b-0 sm:border-r sm:pb-0 sm:pr-3">
          {CHAT_SETTINGS_CATEGORIES.map((item) => (
            <button
              key={item.key}
              type="button"
              onClick={() => setCategory(item.key)}
              className={cn(
                'flex min-w-0 items-center gap-2 rounded-[10px] px-3 py-2 text-left text-[13px] transition-colors sm:w-full sm:text-sm',
                category === item.key
                  ? 'bg-[rgb(var(--rgb-accent)/0.14)] font-medium text-accent'
                  : 'text-fg-2 hover:bg-glass-hover hover:text-fg',
              )}
            >
              <item.icon className="h-4 w-4 flex-none" />
              <span className="truncate">{item.label}</span>
            </button>
          ))}
        </nav>

        {/* 右侧分类内容: 负边距与等量内边距留出卡片悬停阴影, 布局位置不变 */}
        <div className="scroll -my-3 -mr-5 min-h-0 min-w-0 flex-1 py-3 pr-5 sm:-ml-3 sm:pl-4">
          <div className="space-y-5">
            {category === 'chat' && (
              <>
                <ApprovalSettings approval={approval} />
                {/* 模型 */}
                <div className="space-y-1.5">
                  <label className="block text-sm font-medium text-fg">当前会话模型</label>
                  <Select
                    disabled={modelDisabled}
                    value={settings.model}
                    onChange={(e) => onChange('model', e.target.value)}
                    options={modelOptions}
                  />
                  <p className="text-xs text-fg-3">模型配置的新增与编辑见「模型管理」页</p>
                </div>

                <div className="space-y-1.5">
                  <label className="block text-sm font-medium text-fg">整体默认模型</label>
                  <Select value={defaults?.default_model ?? 'default'} disabled={!defaults || defaultSaving}
                    onChange={(event) => onDefaultModel(event.target.value)}
                    options={defaults && !modelOptions.some((option) => option.value === defaults.default_model)
                      ? [{ value: defaults.default_model, label: `${defaults.default_model} (不可用, 请重选)` }, ...modelOptions] : modelOptions} />
                  <p className="text-xs text-fg-3">{defaultSaving ? '正在保存…' : '仅影响之后创建的会话, 已有会话和草稿保持自己的选择'}</p>
                </div>

                {/* 思考强度 */}
                <div className="flex items-center justify-between gap-4">
                  <div>
                    <div className="text-sm font-medium text-fg">思考强度</div>
                    <div className="mt-0.5 text-xs text-fg-3">控制模型推理深度(需模型支持)</div>
                  </div>
                  <div className="w-28 flex-none">
                    <Select
                      value={settings.think}
                      onChange={(e) => onChange('think', e.target.value)}
                      options={thinkingOptions}
                    />
                  </div>
                </div>

                <label className="flex items-center justify-between gap-4">
                  <div>
                    <div className="text-sm font-medium text-fg">使用内置 Agent 提示</div>
                    <div className="text-xs text-fg-3">补充工具调用与结果核实的通用规则, 可与自定义提示同时使用</div>
                  </div>
                  <span className="relative inline-flex h-6 w-11 flex-none items-center">
                    <input
                      type="checkbox"
                      className="peer absolute inset-0 z-10 m-0 cursor-pointer opacity-0"
                      checked={settings.defaultAgentPromptEnabled}
                      onChange={(e) => onChange('defaultAgentPromptEnabled', e.target.checked)}
                    />
                    <span className="absolute inset-0 rounded-full border border-[var(--glass-border-strong)] bg-glass-hover transition-colors duration-300 peer-checked:border-[rgb(var(--rgb-accent))] peer-checked:bg-[rgb(var(--rgb-accent))]" />
                    <span className="pointer-events-none absolute left-1 h-4 w-4 rounded-full bg-fg-3 shadow-sm transition-all duration-300 [transition-timing-function:var(--ease-spring)] peer-checked:translate-x-5 peer-checked:bg-white" />
                  </span>
                </label>

                <label className="flex items-center justify-between gap-4">
                  <div>
                    <div className="text-sm font-medium text-fg">命令写入上下文</div>
                    <div className="text-xs text-fg-3">让 AI 在后续对话中看到已执行的命令及回复, 不会额外调用模型</div>
                  </div>
                  <span className="relative inline-flex h-6 w-11 flex-none items-center">
                    <input
                      type="checkbox"
                      className="peer absolute inset-0 z-10 m-0 cursor-pointer opacity-0"
                      checked={settings.commandHistoryEnabled}
                      onChange={(e) => onChange('commandHistoryEnabled', e.target.checked)}
                    />
                    <span className="absolute inset-0 rounded-full border border-[var(--glass-border-strong)] bg-glass-hover transition-colors duration-300 peer-checked:border-[rgb(var(--rgb-accent))] peer-checked:bg-[rgb(var(--rgb-accent))]" />
                    <span className="pointer-events-none absolute left-1 h-4 w-4 rounded-full bg-fg-3 shadow-sm transition-all duration-300 [transition-timing-function:var(--ease-spring)] peer-checked:translate-x-5 peer-checked:bg-white" />
                  </span>
                </label>

                <div className="flex items-center justify-between gap-4">
                  <div>
                    <div className="text-sm font-medium text-fg">最大工具迭代</div>
                    <div className="text-xs text-fg-3">单轮对话中模型最多调用工具的次数, 留空沿用会话配置默认</div>
                  </div>
                  <input type="number" min={1} step={1} placeholder="默认"
                    aria-label="最大工具迭代"
                    value={settings.maxIterations ?? ''}
                    onChange={(e) => {
                      const raw = e.target.value.trim();
                      const parsed = raw === '' ? undefined : Number(raw);
                      if (parsed !== undefined && (!Number.isInteger(parsed) || parsed < 1)) return;
                      onChange('maxIterations', parsed);
                    }}
                    className="field !h-8 w-20 flex-none text-right" />
                </div>

                {/* 温度 */}
                <div>
                  <div className="mb-2 flex items-center justify-between">
                    <label className="text-sm font-medium text-fg">温度</label>
                    <span className="num text-sm font-medium text-accent">{settings.temperature.toFixed(2)}</span>
                  </div>
                  <input
                    type="range"
                    min={0}
                    max={2}
                    step={0.05}
                    value={settings.temperature}
                    onChange={(e) => onChange('temperature', Number(e.target.value))}
                    className="h-1.5 w-full cursor-pointer appearance-none rounded-full bg-[var(--glass-border-strong)] accent-[rgb(var(--rgb-accent))]"
                  />
                  <div className="mt-1 flex justify-between text-xs text-fg-3">
                    <span>严谨 0</span>
                    <span>平衡 1</span>
                    <span>发散 2</span>
                  </div>
                </div>

                {/* 系统提示词 */}
                <div className="space-y-1.5">
                  <label className="block text-sm font-medium text-fg">系统提示词</label>
                  <textarea
                    value={settings.systemPrompt}
                    onChange={(e) => onChange('systemPrompt', e.target.value)}
                    placeholder="为当前对话设置系统级指令, 留空使用默认"
                    rows={4}
                    className="field w-full resize-y"
                  />
                </div>
              </>
            )}

            {category === 'models' && (
              <div>
                <div className="mb-2 flex items-center justify-between">
                  <label className="text-sm font-medium text-fg">模型配置</label>
                  <button
                    type="button"
                    onClick={onAddModel}
                    className="inline-flex items-center gap-1 text-xs text-accent hover:underline"
                  >
                    <Plus className="h-3 w-3" /> 新增模型
                  </button>
                </div>
                {Object.keys(modelsDetail).length === 0 ? (
                  <p className="text-xs text-fg-3">暂无模型配置</p>
                ) : (
                  <div className="mt-2 space-y-1.5">
                    {Object.entries(modelsDetail).map(([name, cfg]) => (
                      <div key={name} className="glass-card tile flex items-center justify-between gap-2 rounded-xl px-3 py-2 text-xs">
                        <div className="min-w-0 flex-1">
                          <span className="font-medium text-fg">{name}</span>
                          {cfg.model && <span className="ml-2 truncate text-fg-3">{cfg.model}</span>}
                        </div>
                        <div className="flex flex-none items-center gap-1">
                          <button
                            type="button"
                            onClick={() => onEditModel(name)}
                            className="rounded-md p-1 text-fg-3 transition-colors hover:bg-glass-hover hover:text-accent"
                            title="编辑"
                            aria-label={`编辑模型 ${name}`}
                          >
                            <Pencil className="h-3.5 w-3.5" />
                          </button>
                          <button
                            type="button"
                            onClick={() => onDeleteModel(name)}
                            className="rounded-md p-1 text-fg-3 transition-colors hover:bg-glass-hover hover:text-red"
                            title="删除"
                            aria-label={`删除模型 ${name}`}
                          >
                            <Trash2 className="h-3.5 w-3.5" />
                          </button>
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}

            {category === 'plugins' && (
              <div>
                <label className="mb-2 block text-sm font-medium text-fg">插件</label>
                {plugins.length === 0 ? (
                  <p className="text-xs text-fg-3">暂无可用插件</p>
                ) : (
                  <div className="space-y-2">
                    {plugins.map((p) => {
                      const capCount = Object.values(p.capabilities).reduce((n, list) => n + list.length, 0);
                      return (
                        <div key={p.name} className="tile flex items-center justify-between gap-3 rounded-xl px-3 py-2.5">
                          <div className="min-w-0 flex-1">
                            <div className="flex items-center gap-2">
                              <Puzzle className="h-4 w-4 flex-none text-accent" />
                              <span className="truncate text-[13px] font-medium text-fg">{p.name}</span>
                              <span className="flex-none text-xs text-fg-4">v{p.version}</span>
                            </div>
                            <p className="mt-1 line-clamp-2 text-xs text-fg-3">{p.description}</p>
                            {p.availability?.allowed === false && (
                              <p className="mt-1 text-xs text-orange">{p.availability.message}</p>
                            )}
                            <div className="mt-1.5 flex items-center gap-2">
                              <button
                                type="button"
                                onClick={() => onViewCapabilities(p.name)}
                                className="inline-flex items-center gap-1 text-xs text-accent hover:underline"
                              >
                                查看全部能力 ({capCount})
                              </button>
                              <button
                                type="button"
                                onClick={() => onViewConfig(p.name)}
                                className="inline-flex items-center gap-1 text-xs text-accent hover:underline"
                              >
                                配置
                              </button>
                            </div>
                          </div>
                          <Toggle
                            checked={p.enabled}
                            disabled={p.availability?.allowed === false && !p.enabled}
                            onChange={(v) => onTogglePlugin(p.name, v)}
                            title={p.enabled ? '停用插件' : '启用插件'}
                          />
                        </div>
                      );
                    })}
                  </div>
                )}
                <p className="mt-1.5 text-xs text-fg-3">启用后对新会话生效, 能力来自插件 meta.yaml 声明</p>
              </div>
            )}

            {category === 'data' && (
              <>
                <div>
                  <label className="mb-2 block text-sm font-medium text-fg">RAG 知识库</label>
                  <button type="button" onClick={onOpenRag} className="tile tint-teal w-full rounded-xl px-3 py-2.5 text-left">
                    <div className="flex items-center gap-2">
                      <BookOpen className="h-4 w-4 flex-none text-teal" />
                      <span className="text-sm font-medium text-fg">管理知识库</span>
                    </div>
                    <p className="mt-1 text-xs text-fg-3">知识库创建、文档导入与检索测试</p>
                  </button>
                </div>

                <div>
                  <label className="mb-2 block text-sm font-medium text-fg">长期记忆</label>
                  <button type="button" onClick={onOpenMemory} className="tile tint-purple w-full rounded-xl px-3 py-2.5 text-left">
                    <div className="flex items-center gap-2">
                      <Brain className="h-4 w-4 flex-none text-purple" />
                      <span className="text-sm font-medium text-fg">管理记忆</span>
                    </div>
                    <p className="mt-1 text-xs text-fg-3">查看 / 添加 / 删除长期记忆条目</p>
                  </button>
                </div>

                <div>
                  <label className="mb-2 block text-sm font-medium text-fg">会话历史</label>
                  <button type="button" onClick={onOpenHistory} className="tile tint-accent w-full rounded-xl px-3 py-2.5 text-left">
                    <div className="flex items-center gap-2">
                      <HardDrive className="h-4 w-4 flex-none text-accent" />
                      <span className="text-sm font-medium text-fg">管理历史与回收站</span>
                    </div>
                    <p className="mt-1 text-xs text-fg-3">搜索、批量清理、恢复或永久删除 Chat 会话</p>
                  </button>
                </div>
              </>
            )}
          </div>
        </div>
      </div>
    </Modal>
  );
}

// 插件能力弹窗: 分类列出全部能力, 每项独立启停 (双层模型: 能力生效 = 插件启用 ∧ 独立启用)
function PluginCapabilitiesModal({
  plugin,
  onClose,
  onToggleCapability,
}: {
  plugin: ChatPlugin | null;
  onClose: () => void;
  onToggleCapability: (pluginName: string, kind: CapabilityKind, capName: string, enabled: boolean) => void;
}) {
  if (!plugin) return null;
  const descriptions = Object.fromEntries(Object.entries(plugin.capabilities).map(([kind, items]) => [kind, Object.fromEntries(items.map((item) => [item.name, item.description]))]));
  const values = Object.fromEntries(Object.entries(plugin.capabilities).map(([kind, items]) => [kind, Object.fromEntries(items.map((item) => [item.name, item.requested_enabled ?? item.enabled]))]));

  return (
    <Modal open={plugin !== null} onClose={onClose} title={`${plugin.name} 能力`} size="md"
      footer={<Button variant="primary" onClick={onClose}>完成</Button>}>
      <div className="space-y-5">
        {/* 插件概要 */}
        <div className="flex items-center gap-2 text-xs text-fg-3">
          <span className="num">v{plugin.version}</span>
          <span>·</span>
          <span className={plugin.enabled ? 'text-green' : 'text-fg-3'}>
            {plugin.enabled ? '插件已启用' : '插件已停用 (能力不生效)'}
          </span>
        </div>

        <Link className="text-sm text-accent hover:underline" target="_blank" rel="noopener noreferrer" to={`/plugins/${encodeURIComponent(plugin.name)}`}>打开插件详情</Link>
        <PluginCapabilities capabilities={descriptions} values={values} active={plugin.enabled} effective={plugin.capabilities}
          onChange={(kind, name, enabled) => onToggleCapability(plugin.name, kind as CapabilityKind, name, enabled)} />
      </div>
    </Modal>
  );
}

// 插件配置弹窗: 按 config_schema 渲染表单, 保存到后端
function PluginConfigModal({
  pluginName,
  onSessionConfig,
  onClose,
  onSaved,
}: {
  pluginName: string | null;
  onSessionConfig?: () => void;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [data, setData] = useState<GlobalPluginConfig | null>(null);
  const [runtime, setRuntime] = useState<PluginRuntimeResult[]>([]);
  const [form, setForm] = useState<Record<string, unknown>>({});
  const [knowledgeBases, setKnowledgeBases] = useState<ConfigOption[]>([]);
  const [modelOptions, setModelOptions] = useState<ModelOptions>({});
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  // 加载配置
  useEffect(() => {
    if (!pluginName) { setData(null); return; }
    let cancelled = false;
    setData(null); setForm({}); setRuntime([]); setError('');
    (async () => {
      try {
        const [resp, options] = await Promise.all([controlApi.getGlobalPluginConfig(pluginName), controlApi.pluginModelOptions()]);
        if (!cancelled) setModelOptions(options.options);
        if (Object.values(resp.schema).some((field) => field.type.startsWith('knowledge_base'))) {
          const libraries = await ragApi.list({ platformId: 'local', via: 'control' });
          if (!cancelled) setKnowledgeBases(libraries.knowledge_bases.filter((item) => item.scope === 'global').map((item) => ({ value: item.id, label: item.name, scope: item.scope })));
        }
        if (!cancelled) {
          setData(resp);
          setForm({ ...resp.overrides });
          setRuntime([]);
          setError('');
        }
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      }
    })();
    return () => { cancelled = true; };
  }, [pluginName]);

  if (!pluginName) return null;

  const schema = data?.schema ?? {};
  const keys = Object.keys(schema);

  const handleSave = async () => {
    setSaving(true);
    setError('');
    try {
      if (!data) return;
      const result = await controlApi.saveGlobalPluginConfig(pluginName, form, data.revision);
      setData(result);
      setRuntime(result.runtime || []);
      onSaved();
      if (result.runtime?.some((item) => item.status === 'error')) setError('全局参数已保存，部分运行实例应用失败');
      else onClose();
    } catch (err) {
      setError(pluginError(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal open={pluginName !== null} onClose={onClose} title={`${pluginName} 配置`} size="md">
      <div className="space-y-4">
        {error && <p role="alert" className="rounded-lg border border-[rgb(var(--rgb-red)/0.3)] bg-[rgb(var(--rgb-red)/0.1)] px-3 py-2 text-sm text-red">{error}</p>}
        {keys.length === 0 && !data && <p className="text-sm text-fg-3">加载中...</p>}
        {keys.length === 0 && data && <p className="text-sm text-fg-3">该插件无可配置项</p>}
        {onSessionConfig && <Button variant="subtle" onClick={onSessionConfig}>配置当前会话参数</Button>}
        <Link className="block text-sm text-accent hover:underline" target="_blank" rel="noopener noreferrer" to={`/plugins/${encodeURIComponent(pluginName)}?tab=config`}>打开全局参数管理</Link>
        <PluginConfigFields schema={schema} values={form} modelOptions={modelOptions} knowledgeBases={knowledgeBases} disabled={saving}
          onChange={(key, value) => setForm((prev) => ({ ...prev, [key]: value }))} />
        {runtime.length > 0 && <PluginRuntimeStatus results={runtime} busy={saving} onRetry={() => {
          setSaving(true);
          controlApi.reconcilePlugins({ plugin_name: pluginName }).then((result) => { setRuntime(result.runtime); setError(result.runtime.some((item) => item.status === 'error') ? '部分运行实例应用失败' : ''); })
            .catch((err) => setError(pluginError(err))).finally(() => setSaving(false));
        }} />}
        <div className="flex justify-end gap-2 border-t border-line pt-3">
          <Button variant="ghost" onClick={onClose}>取消</Button>
          <Button variant="primary" onClick={handleSave} disabled={saving || !data}>
            {saving ? '保存中...' : '保存'}
          </Button>
        </div>
      </div>
    </Modal>
  );
}

// 记忆管理面板: 列出 / 添加 / 删除记忆
function MemoryPanel({
  open,
  onClose,
  conversationId,
}: {
  open: boolean;
  onClose: () => void;
  conversationId: string;
}) {
  const [memories, setMemories] = useState<MemoryRecord[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  // 新增表单
  const [newTitle, setNewTitle] = useState('');
  const [newContent, setNewContent] = useState('');
  const [newTags, setNewTags] = useState('');
  const [newImportance, setNewImportance] = useState(1);
  const [adding, setAdding] = useState(false);
  const memoryScope = conversationId ? `session:${conversationId}` : '';

  const loadMemories = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      if (!memoryScope) {
        setMemories([]);
        return;
      }
      const result = await chatApi.listMemories(memoryScope);
      setMemories(result.memories);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [memoryScope]);

  useEffect(() => {
    if (open) loadMemories();
  }, [open, loadMemories]);

  const handleAdd = async () => {
    if (!newTitle.trim() || !newContent.trim()) return;
    setAdding(true);
    setError('');
    try {
      await chatApi.addMemory(newTitle.trim(), newContent.trim(), newTags.trim(), newImportance, memoryScope);
      setNewTitle('');
      setNewContent('');
      setNewTags('');
      setNewImportance(1);
      await loadMemories();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setAdding(false);
    }
  };

  const handleDelete = async (m: MemoryRecord) => {
    try {
      await chatApi.deleteMemory(m.id, m.scope);
      setMemories((prev) => prev.filter((x) => x.id !== m.id));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const layeredGroups = useMemo(() => {
    const byScope = new Map<string, MemoryRecord[]>();
    for (const m of memories) {
      const list = byScope.get(m.scope) ?? [];
      list.push(m);
      byScope.set(m.scope, list);
    }
    return [...byScope.keys()].map((scope) => ({ scope, memories: byScope.get(scope) ?? [] }));
  }, [memories]);

  return (
    <Modal open={open} onClose={onClose} title="长期记忆管理" size="lg"
      footer={<Button variant="primary" onClick={onClose}>完成</Button>}>
      <div className="space-y-4">
        {error && <p role="alert" className="rounded-lg border border-[rgb(var(--rgb-red)/0.3)] bg-[rgb(var(--rgb-red)/0.1)] px-3 py-2 text-sm text-red">{error}</p>}

        {/* 新增记忆 */}
        <div className="surface space-y-2 p-3">
          <div className="text-sm font-medium text-fg">添加记忆</div>
          <input
            type="text"
            value={newTitle}
            onChange={(e) => setNewTitle(e.target.value)}
            placeholder="标题"
            aria-label="记忆标题"
            className="field"
          />
          <textarea
            value={newContent}
            onChange={(e) => setNewContent(e.target.value)}
            placeholder="内容"
            aria-label="记忆内容"
            rows={3}
            className="field w-full resize-y"
          />
          <div className="flex flex-wrap gap-2">
            <input
              type="text"
              value={newTags}
              onChange={(e) => setNewTags(e.target.value)}
              placeholder="标签 (逗号分隔)"
              aria-label="记忆标签"
              className="field min-w-0 flex-1"
            />
            <div className="w-28 flex-none">
              <Select
                value={String(newImportance)}
                onChange={(e) => setNewImportance(Number(e.target.value))}
                options={[1, 2, 3, 4, 5].map((n) => ({ value: String(n), label: `权重 ${n}` }))}
              />
            </div>
            <span className="chip self-center">当前会话</span>
          </div>
          <div className="flex justify-end">
            <Button variant="primary" onClick={handleAdd} disabled={adding || !memoryScope || !newTitle.trim() || !newContent.trim()}>
              {adding ? '添加中...' : '添加'}
            </Button>
          </div>
        </div>

        {/* 记忆列表 (按层分组: 全局 / 各项目) */}
        {loading ? (
          <p className="text-sm text-fg-3">加载中...</p>
        ) : memories.length === 0 ? (
          <p className="text-sm text-fg-3">暂无记忆</p>
        ) : (
          <div className="scroll max-h-80 space-y-3 pr-1">
            {layeredGroups.map((g) => (
              <div key={g.scope} className="space-y-2">
                <div className="flex items-center gap-1.5 text-xs text-fg-3">
                  <Brain className="h-3.5 w-3.5" />
                  <span>当前会话 · {g.memories.length} 条</span>
                </div>
                {g.memories.map((m) => (
                  <div key={m.id} className="tile tint-purple rounded-xl px-3 py-2.5">
                    <div className="flex items-start justify-between gap-2">
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2">
                          <span className="text-sm font-medium text-fg">{m.title}</span>
                          <span className="text-xs text-fg-4">权重 {m.importance}</span>
                        </div>
                        <p className="mt-1 line-clamp-3 text-xs text-fg-2">{m.content}</p>
                        {m.tags.length > 0 && (
                          <div className="mt-1.5 flex flex-wrap gap-1">
                            {m.tags.map((t) => (
                              <span key={t} className="chip !h-5 !px-1.5 !text-[11px]">
                                {t}
                              </span>
                            ))}
                          </div>
                        )}
                      </div>
                      <button
                        type="button"
                        onClick={() => handleDelete(m)}
                        className="mt-0.5 flex-none rounded-md p-1 text-fg-3 transition-colors hover:bg-glass-hover hover:text-red"
                        title="删除记忆"
                        aria-label="删除记忆"
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            ))}
          </div>
        )}
      </div>
    </Modal>
  );
}

function ModelEditModal({ modelName, modelsDetail, onClose, onSaved }: {
  modelName: string | null;
  modelsDetail: Record<string, ModelConfigItem>;
  onClose: () => void;
  onSaved: () => void;
}) {
  const { isRunning, reloadConfig } = useBackendStore(useShallow((state) => ({ isRunning: state.isRunning, reloadConfig: state.reloadConfig })));
  const handleSave = async (config: ModelConfigItem) => {
    if (modelName === null) return false;
    const result = modelName === '' ? await chatApi.addModel(config) : await chatApi.updateModel(modelName, config);
    if (!result.ok) {
      throw new Error(result.error || '保存失败');
    }
    const platformReloaded = !isRunning || await reloadConfig();
    const refreshed = result.refreshed_conversations ?? 0;
    const deferred = result.deferred_conversations ?? 0;
    const chatStatus = deferred > 0
      ? `Chat 已更新 ${refreshed} 个会话, ${deferred} 个将在本轮结束后更新`
      : `Chat 已更新 ${refreshed} 个会话`;
    toast(platformReloaded ? 'success' : 'warning', platformReloaded ? `${chatStatus}, 平台配置已热加载` : `${chatStatus}, 平台热加载失败`);
    onSaved();
    return true;
  };
  if (modelName === null) return null;
  return <LLMConfigModal key={modelName} modelName={modelName} initialConfig={modelsDetail[modelName]} onClose={onClose} onSave={handleSave} />;
}
