import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math';
import rehypeKatex from 'rehype-katex';
import 'katex/dist/katex.min.css';
import { Card } from '@/components/ui/Card';
import { Button } from '@/components/ui/Button';
import { Modal } from '@/components/ui/Modal';
import { Select } from '@/components/ui/Select';
import { cn } from '@/utils/cn';
import { formatRelativeTime } from '@/utils/format';
import { useStandaloneGlassReflect } from '@/hooks/useGlassReflect';
import { useTheme } from '@/hooks/useTheme';
import {
  chatApi,
  subscribeChat,
  type Attachment,
  type CapabilityKind,
  type ChatEvent,
  type ChatPreloadSettings,
  type ChatPlugin,
  type DirEntry,
  type MemoryRecord,
  type ModelConfigItem,
  type PluginConfigResponse,
  type ProjectItem,
  type ToolCall,
} from '@/api/chat';
import {
  Plus,
  Send,
  Square,
  MessageSquare,
  Trash2,
  Sparkles,
  User,
  Bot,
  Search,
  Settings2,
  Paperclip,
  ArrowLeft,
  ArrowUp,
  Sun,
  Moon,
  ChevronDown,
  ChevronRight,
  Brain,
  Puzzle,
  Wrench,
  CheckCircle2,
  XCircle,
  Loader2,
  RotateCcw,
  GitFork,
  X,
  FileText,
  Pencil,
  Folder,
  FolderOpen,
  FolderPlus,
  FolderInput,
  HardDrive,
} from 'lucide-react';

// 聊天设置
interface ChatSettings {
  model: string;   // 模型配置名
  think: string;   // 思考强度 (off/low/medium/high)
  temperature: number;   // 采样温度
  systemPrompt: string;   // 系统提示词
}

const DEFAULT_SETTINGS: ChatSettings = {
  model: 'default',
  think: 'off',
  temperature: 0.7,
  systemPrompt: '',
};

// 能力类别中文名
const CAPABILITY_LABELS: Record<CapabilityKind, string> = {
  tools: '工具',
  skills: '技能',
  mcp: 'MCP',
  handlers: '处理器',
  commands: '命令',
};

// 会话列表多色循环(参考侧栏导航多色方案)
type GlassColor = 'accent' | 'purple' | 'teal' | 'pink' | 'orange' | 'green';
const CONVERSATION_COLORS: GlassColor[] = ['accent', 'purple', 'teal', 'pink', 'orange', 'green'];

// 颜色对应的玻璃卡片变体类
const COLOR_CARD_CLASS: Record<GlassColor, string> = {
  accent: 'glass-card-accent',
  purple: 'glass-card-purple',
  teal: 'glass-card-teal',
  pink: 'glass-card-pink',
  orange: 'glass-card-orange',
  green: 'glass-card-green',
};

// 颜色对应的文字色类
const COLOR_TEXT_CLASS: Record<GlassColor, string> = {
  accent: 'text-accent',
  purple: 'text-purple',
  teal: 'text-teal',
  pink: 'text-pink',
  orange: 'text-orange',
  green: 'text-green',
};

// 消息角色
type MessageRole = 'user' | 'assistant';

// 消息段类型 (按时间顺序排列, 前端本地格式)
type LocalMessageSegment =
  | { type: 'thinking'; content: string }
  | { type: 'tool'; tool: ToolCall }
  | { type: 'content'; content: string };

// 单条消息
interface ChatMessage {
  id: string;
  role: MessageRole;
  content: string;
  timestamp: number;
  // 思考流内容 (聚合, 兼容旧数据)
  thinking?: string;
  // 工具调用明细 (聚合, 兼容旧数据)
  toolCalls?: ToolCall[];
  // 附件列表
  attachments?: Attachment[];
  // 流式输出中标记
  streaming?: boolean;
  // 对应后端 turn_index (用于 fork)
  turnIndex?: number;
  // 分段内容 (按时间顺序, 用于流式渲染)
  segments?: LocalMessageSegment[];
}

interface PendingUserInput {
  requestId: string;
  question: string;
}

// 一个会话
interface Conversation {
  id: string;   // 后端 conversation_id
  title: string;
  messages: ChatMessage[];
  updatedAt: number;
  // 是否已从后端加载历史
  loaded?: boolean;
  // 所属项目 id (无项目会话为 null/缺省, 归入"最近")
  projectId?: string | null;
  // 是否为尚未发送首条消息的后端预加载会话
  preloaded?: boolean;
  // 前端触发本次预加载时的设置键
  preloadKey?: string;
}

// 生成唯一 id (本地消息用)
const genId = () => `${Date.now()}-${Math.random().toString(36).slice(2, 9)}`;

// 根据首条用户消息生成会话标题
const deriveTitle = (content: string) => {
  const text = content.trim().replace(/\s+/g, ' ');
  return text.length > 24 ? `${text.slice(0, 24)}…` : text || '新对话';
};

export function Chat() {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeId, setActiveId] = useState<string>('');
  const [input, setInput] = useState('');
  const [search, setSearch] = useState('');
  const [generating, setGenerating] = useState(false);
  const [pendingUserInputs, setPendingUserInputs] = useState<Record<string, PendingUserInput[]>>({});
  const [answeringRequestId, setAnsweringRequestId] = useState<string | null>(null);
  // 聊天设置与设置弹窗 / 折叠面板
  const [settings, setSettings] = useState<ChatSettings>(DEFAULT_SETTINGS);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [optionsOpen, setOptionsOpen] = useState(false);
  // 后端连接状态
  const [backendOk, setBackendOk] = useState<boolean | null>(null);
  // 可选模型列表(来自聊天服务)
  const [models, setModels] = useState<string[]>([]);
  // 插件列表(来自聊天服务)
  const [plugins, setPlugins] = useState<ChatPlugin[]>([]);
  // 插件配置保存后递增, 用于主动刷新空的预加载会话
  const [pluginConfigRevision, setPluginConfigRevision] = useState(0);
  // 能力弹窗当前查看的插件名 (null = 关闭)
  const [capabilityPlugin, setCapabilityPlugin] = useState<string | null>(null);
  // 配置弹窗当前查看的插件名 (null = 关闭)
  const [configPlugin, setConfigPlugin] = useState<string | null>(null);
  // 记忆面板开关
  const [memoryOpen, setMemoryOpen] = useState(false);
  // 项目列表 (绑定的工作区文件夹)
  const [projects, setProjects] = useState<ProjectItem[]>([]);
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
  // 模型详情 (设置弹窗管理用)
  const [modelsDetail, setModelsDetail] = useState<Record<string, ModelConfigItem>>({});
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
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  // WS 取消订阅函数
  const unsubscribeRef = useRef<(() => void) | null>(null);
  // 当前流式消息 id (assistant)
  const streamingMsgIdRef = useRef<string | null>(null);
  // 输入卡片独立反光
  const inputCardRef = useStandaloneGlassReflect<HTMLDivElement>({
    reflectRange: 120,
    reflectSize: 120,
  });

  const active = useMemo(
    () => conversations.find((c) => c.id === activeId),
    [conversations, activeId]
  );
  const activePendingUserInput = pendingUserInputs[activeId]?.[0];
  conversationsRef.current = conversations;

  // 更新指定会话
  const updateConversation = useCallback((id: string, updater: (c: Conversation) => Conversation) => {
    setConversations((prev) => prev.map((c) => (c.id === id ? updater(c) : c)));
  }, []);

  // 更新当前流式消息
  const updateStreamingMessage = useCallback(
    (updater: (m: ChatMessage) => ChatMessage) => {
      const msgId = streamingMsgIdRef.current;
      if (!msgId || !activeId) return;
      updateConversation(activeId, (c) => ({
        ...c,
        messages: c.messages.map((m) => (m.id === msgId ? updater(m) : m)),
        updatedAt: Math.round(Date.now() / 1000),
      }));
    },
    [activeId, updateConversation]
  );

  // 处理 WS 事件
  const handleEvent = useCallback(
    (event: ChatEvent) => {
      switch (event.type) {
        case 'thinking_delta':
          updateStreamingMessage((m) => {
            const segments = [...(m.segments ?? [])];
            const last = segments[segments.length - 1];
            // 追加到当前 thinking 段或新建段
            if (last?.type === 'thinking') {
              segments[segments.length - 1] = { ...last, content: last.content + event.delta };
            } else {
              segments.push({ type: 'thinking', content: event.delta });
            }
            return { ...m, thinking: (m.thinking ?? '') + event.delta, segments };
          });
          break;
        case 'content_delta':
          updateStreamingMessage((m) => {
            const segments = [...(m.segments ?? [])];
            const last = segments[segments.length - 1];
            // 追加到当前 content 段或新建段
            if (last?.type === 'content') {
              segments[segments.length - 1] = { ...last, content: last.content + event.delta };
            } else {
              segments.push({ type: 'content', content: event.delta });
            }
            return { ...m, content: m.content + event.delta, segments };
          });
          break;
        case 'tool_start':
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
          });
          break;
        case 'tool_end':
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
          });
          break;
        case 'ask_user':
          setGenerating(true);
          setPendingUserInputs((prev) => {
            const current = prev[event.conversation_id] ?? [];
            if (current.some((item) => item.requestId === event.request_id)) return prev;
            return {
              ...prev,
              [event.conversation_id]: [
                ...current,
                { requestId: event.request_id, question: event.question },
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
          break;
        case 'turn_done':
          updateStreamingMessage((m) => ({ ...m, content: event.answer || m.content, streaming: false }));
          setPendingUserInputs((prev) => ({ ...prev, [activeId]: [] }));
          streamingMsgIdRef.current = null;
          setGenerating(false);
          break;
        case 'error':
          updateStreamingMessage((m) => ({
            ...m,
            content: m.content + `\n\n[错误] ${event.error ?? event.message ?? '未知错误'}`,
            streaming: false,
          }));
          setPendingUserInputs((prev) => ({ ...prev, [activeId]: [] }));
          streamingMsgIdRef.current = null;
          setGenerating(false);
          break;
      }
    },
    [activeId, updateStreamingMessage]
  );

  // 加载会话历史消息
  const loadTurns = useCallback(
    async (conversationId: string) => {
      try {
        const { turns } = await chatApi.listTurns(conversationId);
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
          // 优先使用后端返回的 segments (含时间顺序), 否则按固定顺序构建
          let segments: LocalMessageSegment[] | undefined;
          if (turn.segments && turn.segments.length > 0) {
            // 转换后端 segments 格式为前端格式
            segments = turn.segments.map((seg) => {
              if (seg.type === 'thinking') {
                return { type: 'thinking' as const, content: seg.content ?? '' };
              }
              if (seg.type === 'tool' && seg.tool) {
                return { type: 'tool' as const, tool: seg.tool };
              }
              return { type: 'content' as const, content: seg.content ?? '' };
            });
          } else {
            // 兼容旧数据: 按固定顺序构建 (thinking -> tools -> content)
            const fallback: LocalMessageSegment[] = [];
            if (turn.thinking) {
              fallback.push({ type: 'thinking', content: turn.thinking });
            }
            for (const tool of turn.tool_calls ?? []) {
              fallback.push({ type: 'tool', tool });
            }
            if (turn.answer) {
              fallback.push({ type: 'content', content: turn.answer });
            }
            segments = fallback;
          }
          messages.push({
            id: `${turn.id}-a`,
            role: 'assistant',
            content: turn.answer,
            thinking: turn.thinking ?? undefined,
            toolCalls: turn.tool_calls,
            timestamp: turn.created_at * 1000,
            turnIndex: turn.turn_index,
            segments,
          });
        }
        updateConversation(conversationId, (c) => ({ ...c, messages, loaded: true }));
      } catch (err) {
        console.error('[Chat] 加载历史失败:', err);
        updateConversation(conversationId, (c) => ({ ...c, loaded: true }));
      }
    },
    [updateConversation]
  );

  // 订阅当前会话 WS (草稿态不订阅)
  useEffect(() => {
    if (!activeId || activeId === '__draft__') return;
    unsubscribeRef.current?.();
    unsubscribeRef.current = subscribeChat(activeId, handleEvent);
    return () => {
      unsubscribeRef.current?.();
      unsubscribeRef.current = null;
    };
  }, [activeId, handleEvent]);

  // 初始化: 健康检查 + 加载模型/插件/会话列表
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        await chatApi.health();
        if (cancelled) return;
        setBackendOk(true);
        const [{ models: modelList }, { plugins: pluginList }, { conversations: convList }, { models: detail }, { projects: projectList }] =
          await Promise.all([
            chatApi.listModels(),
            chatApi.listPlugins(),
            chatApi.listConversations(),
            chatApi.listModelsDetail(),
            chatApi.listProjects(),
          ]);
        if (cancelled) return;
        setModels(modelList);
        setPlugins(pluginList);
        setModelsDetail(detail);
        setProjects(projectList);
        if (modelList.length > 0) {
          setSettings((prev) => (modelList.includes(prev.model) ? prev : { ...prev, model: modelList[0] }));
        }
        // 恢复历史会话
        const restored: Conversation[] = convList.map((item) => ({
          id: item.conversation_id,
          title: item.title,
          messages: [],
          updatedAt: item.last_at,
          loaded: false,
          projectId: item.project_id ?? null,
        }));
        setConversations(restored);
        if (restored.length > 0) {
          // 有历史会话时展示入口选择, 不自动选中
          setShowEntryChoice(true);
        }
      } catch (err) {
        console.error('[Chat] 后端连接失败:', err);
        if (!cancelled) setBackendOk(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

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
    return all.map((name) => ({ value: name, label: name }));
  }, [models, settings.model]);

  // 更新单项设置
  const updateSettings = useCallback(<K extends keyof ChatSettings>(key: K, value: ChatSettings[K]) => {
    setSettings((prev) => ({ ...prev, [key]: value }));
  }, []);

  // 生成会影响 Session, 模型或插件安装结果的前端设置键
  const preloadBaseKey = useMemo(() => JSON.stringify({
    model: settings.model,
    temperature: settings.temperature,
    systemPrompt: settings.systemPrompt,
    modelConfig: modelsDetail[settings.model] ?? null,
    plugins,
    pluginConfigRevision,
  }), [settings.model, settings.temperature, settings.systemPrompt, modelsDetail, plugins, pluginConfigRevision]);

  const preloadKeyFor = useCallback(
    (projectId: string | null | undefined) => `${preloadBaseKey}:${projectId ?? ''}`,
    [preloadBaseKey]
  );

  const preloadSettingsFor = useCallback(
    (projectId: string | null | undefined): ChatPreloadSettings => ({
      model: settings.model,
      think: settings.think,
      temperature: settings.temperature,
      systemPrompt: settings.systemPrompt,
      projectId: projectId ?? null,
    }),
    [settings.model, settings.think, settings.temperature, settings.systemPrompt]
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
    const request = chatApi.preloadConversation(preloadSettingsFor(target.projectId))
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
    const sorted = [...conversations].sort((a, b) => b.updatedAt - a.updatedAt);
    const kw = search.trim().toLowerCase();
    const matched = kw ? sorted.filter((c) => c.title.toLowerCase().includes(kw)) : sorted;
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
      .filter((g) => g.conversations.length > 0 || !kw);
    return { groups, recent, colorIndex, total: matched.length };
  }, [conversations, search, projects]);

  // 滚动到底部
  const scrollToBottom = useCallback(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, []);

  // 仅在消息变化时滚动到底部 (面板开合不联动, 避免打断历史消息阅读)
  useEffect(() => {
    scrollToBottom();
  }, [active?.messages.length, active?.messages, scrollToBottom]);

  // 自动调整输入框高度
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }, [input]);

  // 创建本地草稿会话 (不切换选中, 供自动创建场景复用); projectId = 在项目下新建
  const createDraft = useCallback((projectId?: string): Conversation => {
    const draft: Conversation = {
      id: '__draft__',
      title: '新对话',
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
  }, []);

  // 新建会话并触发后端预加载; projectId = 在项目下新建
  const handleNew = useCallback((projectId?: string) => {
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
    const draft = createDraft(projectId);
    setActiveId(draft.id);
    setShowEntryChoice(false);
    setInput('');
    textareaRef.current?.focus();
  }, [conversations, createDraft]);

  // 草稿立即预加载; 未发送前变更构建设置时防抖刷新
  useEffect(() => {
    if (!active || active.messages.length > 0) return;
    if (active.id !== '__draft__' && !active.preloaded) return;
    const key = preloadKeyFor(active.projectId);
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
    (id: string) => {
      const conv = conversations.find((c) => c.id === id);
      const title = conv?.title || id;
      if (!window.confirm(`确定删除会话 "${title}" 吗？`)) return;
      // 本地先移除 (乐观更新)
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
      // 后端删除 (草稿会话无需调后端)
      if (id !== '__draft__') {
        chatApi.deleteConversation(id).catch((err) => {
          console.error('[Chat] 后端删除会话失败:', err);
        });
      }
    },
    [activeId, conversations]
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
  const handleDeleteProject = useCallback((projectId: string) => {
    const project = projects.find((p) => p.project_id === projectId);
    if (!window.confirm(`确定删除项目 "${project?.name ?? projectId}" 吗？\n其下会话将移入"最近", 磁盘文件不受影响。`)) return;
    chatApi.deleteProject(projectId).catch((err) => {
      console.error('[Chat] 删除项目失败:', err);
      alert(`删除项目失败: ${err instanceof Error ? err.message : String(err)}`);
    });
    setProjects((prev) => prev.filter((p) => p.project_id !== projectId));
    setConversations((prev) => prev.map((c) => (c.projectId === projectId ? { ...c, projectId: null } : c)));
  }, [projects]);

  // 会话改绑项目 (null = 移出项目归入"最近"; 仅影响之后的工具调用)
  const handleMoveConversation = useCallback(async (projectId: string | null) => {
    const cid = movingConvId;
    if (!cid) return;
    setMovingConvId(null);
    try {
      await chatApi.setConversationProject(cid, projectId);
      updateConversation(cid, (c) => ({ ...c, projectId }));
    } catch (err) {
      console.error('[Chat] 会话改绑项目失败:', err);
      alert(`移动会话失败: ${err instanceof Error ? err.message : String(err)}`);
    }
  }, [movingConvId, updateConversation]);

  // 发送消息
  const handleSend = useCallback(async () => {
    const content = input.trim();
    if ((!content && pendingAttachments.length === 0) || generating) return;

    // 无选中会话时自动创建草稿
    let targetConv = active;
    if (!targetConv) {
      targetConv = createDraft();
      setActiveId(targetConv.id);
      setShowEntryChoice(false);
    }

    const atts = pendingAttachments.length > 0 ? [...pendingAttachments] : undefined;
    const userMsg: ChatMessage = {
      id: genId(),
      role: 'user',
      content,
      timestamp: Date.now(),
      attachments: atts,
    };
    const assistantId = genId();
    const assistantMsg: ChatMessage = {
      id: assistantId,
      role: 'assistant',
      content: '',
      timestamp: Date.now(),
      streaming: true,
    };

    // 乐观更新: 先写入用户消息 + 占位 assistant 消息
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
    setGenerating(true);
    streamingMsgIdRef.current = assistantId;

    // 草稿或设置已变化的空会话: 等待对应预加载完成
    let realConvId = convId;
    const preloadKey = preloadKeyFor(targetConv.projectId);
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
        setGenerating(false);
        return;
      }
    }

    try {
      await chatApi.send(
        realConvId,
        content,
        settings.think,
        atts,
        preloadSettingsFor(targetConv.projectId),
      );
      updateConversation(realConvId, (c) => ({ ...c, preloaded: false, preloadKey: undefined }));
      // 流式内容经 WS 推送, 此处仅等待发送确认
    } catch (err) {
      console.error('[Chat] 发送失败:', err);
      updateConversation(realConvId, (c) => ({
        ...c,
        messages: c.messages.map((m) =>
          m.id === assistantId
            ? { ...m, content: `[发送失败] ${err instanceof Error ? err.message : String(err)}`, streaming: false }
            : m
        ),
      }));
      streamingMsgIdRef.current = null;
      setGenerating(false);
    }
  }, [input, active, generating, settings.think, pendingAttachments, updateConversation, createDraft, preloadKeyFor, preloadSettingsFor, startPreload]);

  // 选择文件
  const handleFileSelect = useCallback(async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files;
    if (!files) return;

    // 上传需要预分配的真实 ID, 但此时仍不持久化正式会话
    let targetConv = active;
    if (!targetConv) {
      targetConv = createDraft();
      setActiveId(targetConv.id);
      setShowEntryChoice(false);
    }
    const preloadKey = preloadKeyFor(targetConv.projectId);
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
      if (file.size > 10 * 1024 * 1024) {
        alert(`文件 ${file.name} 超过 10MB 限制`);
        continue;
      }
      const reader = new FileReader();
      const base64 = await new Promise<string>((resolve) => {
        reader.onload = () => resolve((reader.result as string).split(',')[1]);
        reader.readAsDataURL(file);
      });
      try {
        const result = await chatApi.uploadFile(targetConv.id, file.name, base64);
        if (result.ok) {
          setPendingAttachments((prev) => [...prev, {
            name: result.file_name,
            url: result.file_url,
            type: result.file_type,
          }]);
        }
      } catch (err) {
        console.error('[Chat] 上传失败:', err);
        alert(`上传 ${file.name} 失败`);
      }
    }
    // 清空 input 以便重复选择同一文件
    e.target.value = '';
  }, [active, pendingAttachments.length, createDraft, preloadKeyFor, startPreload]);

  // 移除待发送附件
  const removeAttachment = useCallback((index: number) => {
    setPendingAttachments((prev) => prev.filter((_, i) => i !== index));
  }, []);

  // Retry: 重试最后一轮
  const handleRetry = useCallback(async () => {
    if (!active || active.id === '__draft__' || active.preloaded || generating) return;
    try {
      const result = await chatApi.retry(active.id, settings.think);
      if (result.ok) {
        // 保留 user 消息, 替换最后的 assistant 消息为新的流式占位
        const assistantId = genId();
        const assistantMsg: ChatMessage = {
          id: assistantId,
          role: 'assistant',
          content: '',
          timestamp: Date.now(),
          streaming: true,
        };
        updateConversation(active.id, (c) => {
          const msgs = [...c.messages];
          // 删掉最后的 assistant 消息 (如果存在)
          if (msgs.length > 0 && msgs[msgs.length - 1].role === 'assistant') {
            msgs.pop();
          }
          msgs.push(assistantMsg);
          return { ...c, messages: msgs, updatedAt: Date.now() };
        });
        streamingMsgIdRef.current = assistantId;
        setGenerating(true);
      } else {
        alert(result.error || '重试失败');
      }
    } catch (err) {
      console.error('[Chat] 重试失败:', err);
      alert(`重试失败: ${err instanceof Error ? err.message : String(err)}`);
    }
  }, [active, generating, settings.think, updateConversation]);

  // Fork: 从指定轮次创建新会话
  const handleFork = useCallback(async (turnIndex: number) => {
    if (!active || active.id === '__draft__' || active.preloaded) return;
    try {
      const result = await chatApi.fork(active.id, turnIndex);
      if (result.ok && result.conversation_id) {
        // 刷新会话列表并切换到新会话
        const { conversations: convList } = await chatApi.listConversations();
        const restored: Conversation[] = convList.map((item) => ({
          id: item.conversation_id,
          title: item.title,
          messages: [],
          updatedAt: item.last_at,
          loaded: false,
        }));
        setConversations(restored);
        setActiveId(result.conversation_id);
      }
    } catch (err) {
      console.error('[Chat] Fork 失败:', err);
      alert(`Fork 失败: ${err instanceof Error ? err.message : String(err)}`);
    }
  }, [active]);

  // 删除模型配置
  const handleDeleteModel = useCallback(async (name: string) => {
    if (!confirm(`确定删除模型配置 "${name}" 吗？`)) return;
    try {
      const result = await chatApi.deleteModel(name);
      if (result.ok) {
        setModelsDetail((prev) => {
          const next = { ...prev };
          delete next[name];
          return next;
        });
        setModels((prev) => prev.filter((m) => m !== name));
        // 如果删除的是当前选中模型, 切换到第一个可用模型
        if (settings.model === name) {
          setSettings((prev) => ({ ...prev, model: models.find((m) => m !== name) ?? 'default' }));
        }
      } else {
        alert(result.error || '删除失败');
      }
    } catch (err) {
      console.error('[Chat] 删除模型失败:', err);
      alert(`删除模型失败: ${err instanceof Error ? err.message : String(err)}`);
    }
  }, [settings.model, models]);

  // 模型保存后刷新列表
  const handleModelSaved = useCallback(async () => {
    try {
      const [{ models: modelList }, { models: detail }] = await Promise.all([
        chatApi.listModels(),
        chatApi.listModelsDetail(),
      ]);
      setModels(modelList);
      setModelsDetail(detail);
    } catch (err) {
      console.error('[Chat] 刷新模型列表失败:', err);
    }
  }, []);

  // 停止生成 (调用后端取消 + 解除前端流式标记)
  const handleAnswerUserInput = useCallback(async () => {
    const answer = input.trim();
    if (!active || !activePendingUserInput || !answer || answeringRequestId) return;
    const requestId = activePendingUserInput.requestId;
    setAnsweringRequestId(requestId);
    try {
      await chatApi.answerAskUser(active.id, requestId, answer);
      setInput('');
      setPendingUserInputs((prev) => ({
        ...prev,
        [active.id]: (prev[active.id] ?? []).filter((item) => item.requestId !== requestId),
      }));
    } catch (err) {
      console.error('[Chat] 提交工具回答失败:', err);
      alert(`提交回答失败: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setAnsweringRequestId(null);
    }
  }, [active, activePendingUserInput, answeringRequestId, input]);

  const handleStop = useCallback(async () => {
    if (active) {
      try {
        await chatApi.cancel(active.id);
      } catch (err) {
        console.error('[Chat] 取消生成失败:', err);
      }
    }
    setGenerating(false);
    setPendingUserInputs((prev) => active ? { ...prev, [active.id]: [] } : prev);
    streamingMsgIdRef.current = null;
    if (active) {
      updateConversation(active.id, (c) => ({
        ...c,
        messages: c.messages.map((m) => (m.streaming ? { ...m, streaming: false } : m)),
      }));
    }
  }, [active, updateConversation]);

  // 键盘发送: Enter 发送, Shift+Enter 换行
  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        if (activePendingUserInput) {
          void handleAnswerUserInput();
        } else {
          void handleSend();
        }
      }
    },
    [activePendingUserInput, handleAnswerUserInput, handleSend]
  );

  // 切换插件聚合启停
  const togglePlugin = useCallback(async (name: string, enabled: boolean) => {
    try {
      await chatApi.setPluginEnabled(name, enabled);
      setPlugins((prev) => prev.map((p) => (p.name === name ? { ...p, enabled } : p)));
    } catch (err) {
      console.error('[Chat] 切换插件失败:', err);
      alert(`切换插件失败: ${err instanceof Error ? err.message : String(err)}`);
    }
  }, []);

  // 切换插件内单项能力独立启停 (双层模型: 能力生效 = 插件启用 ∧ 独立启用)
  const toggleCapability = useCallback(
    async (pluginName: string, kind: CapabilityKind, capName: string, enabled: boolean) => {
      try {
        await chatApi.setPluginCapability(pluginName, kind, capName, enabled);
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
      <div className="flex flex-col h-screen p-4 gap-4">
        <ChatHeader theme={theme} onToggleTheme={toggleTheme} onBack={() => navigate('/')} />
        <Card className="flex-1 flex flex-col items-center justify-center gap-3">
          <Bot className="h-10 w-10 text-text-tertiary" />
          <p className="text-text-primary font-medium">聊天服务未连接</p>
          <p className="text-sm text-text-tertiary">
            请先启动聊天服务: python -m satrap.display.server
          </p>
        </Card>
      </div>
    );
  }

  return (
    <div className="flex flex-col h-screen p-4 gap-4">
      {/* 聊天页独立顶栏 */}
      <ChatHeader theme={theme} onToggleTheme={toggleTheme} onBack={() => navigate('/')} />

      <div className="flex gap-4 flex-1 min-h-0">
      {/* 会话列表侧栏 */}
      <Card className="w-72 shrink-0 flex flex-col p-0 overflow-hidden">
        <div className="p-3 border-b border-glass-border space-y-2">
          <div className="flex gap-2">
            <Button variant="primary" className="flex-1" onClick={() => handleNew()}>
              <Plus className="h-4 w-4 mr-2" />
              新建对话
            </Button>
            <Button variant="ghost" title="新建项目 (绑定工作区文件夹)" onClick={() => setProjectDialogOpen(true)}>
              <FolderPlus className="h-4 w-4" />
            </Button>
          </div>
          <div className="relative">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-text-tertiary" />
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="搜索对话"
              className="glass-input w-full pl-9 pr-3 py-2 text-sm rounded-md"
            />
          </div>
        </div>

        <div className="flex-1 overflow-y-auto custom-scrollbar p-2 space-y-1">
          {grouped.total === 0 && grouped.groups.length === 0 ? (
            <div className="text-center text-text-tertiary text-sm py-8">
              {conversations.length === 0 && projects.length === 0 ? '暂无对话, 点击上方新建' : '无匹配对话'}
            </div>
          ) : (
            <>
              {/* 项目区: 可折叠分组, 组内渲染该项目会话 */}
              {grouped.groups.length > 0 && (
                <div className="space-y-1">
                  <div className="px-2 pt-1 pb-0.5 text-xs text-text-tertiary">项目</div>
                  {grouped.groups.map((g) => (
                    <ProjectGroup
                      key={g.project.project_id}
                      project={g.project}
                      conversations={g.conversations}
                      collapsed={!!collapsedProjects[g.project.project_id]}
                      activeId={active?.id ?? ''}
                      colorIndex={grouped.colorIndex}
                      onToggle={() =>
                        setCollapsedProjects((prev) => ({
                          ...prev,
                          [g.project.project_id]: !prev[g.project.project_id],
                        }))
                      }
                      onNewConversation={() => handleNew(g.project.project_id)}
                      onDeleteProject={() => handleDeleteProject(g.project.project_id)}
                      onSelect={(id) => {
                        setActiveId(id);
                        setShowEntryChoice(false);
                      }}
                      onDelete={handleDelete}
                      onMove={(id) => setMovingConvId(id)}
                    />
                  ))}
                </div>
              )}
              {/* 最近区: 无项目会话 (平铺, 保持原有形态) */}
              {grouped.recent.length > 0 && (
                <div className="space-y-1">
                  {grouped.groups.length > 0 && (
                    <div className="px-2 pt-2 pb-0.5 text-xs text-text-tertiary">最近</div>
                  )}
                  {grouped.recent.map((conv) => (
                    <ConversationItem
                      key={conv.id}
                      conversation={conv}
                      color={CONVERSATION_COLORS[(grouped.colorIndex.get(conv.id) ?? 0) % CONVERSATION_COLORS.length]}
                      active={conv.id === active?.id}
                      onSelect={() => {
                        setActiveId(conv.id);
                        setShowEntryChoice(false);
                      }}
                      onDelete={() => handleDelete(conv.id)}
                      onMove={conv.id === '__draft__' || conv.preloaded ? undefined : () => setMovingConvId(conv.id)}
                    />
                  ))}
                </div>
              )}
            </>
          )}
        </div>
      </Card>

      {/* 聊天主区 */}
      <Card className="flex-1 flex flex-col p-0 overflow-hidden min-w-0">
        {/* 头部 */}
        <div className="flex items-center justify-between px-5 py-3 border-b border-glass-border">
          <div className="flex items-center gap-2 min-w-0">
            <Sparkles className="h-4 w-4 text-accent shrink-0" />
            <h3 className="font-semibold text-text-primary truncate">
              {active?.title ?? '对话'}
            </h3>
          </div>
          <Button variant="ghost" size="sm" title="对话设置" onClick={() => setSettingsOpen(true)}>
            <Settings2 className="h-4 w-4" />
          </Button>
        </div>

        {/* 消息流 (输入区内嵌于底部, sticky 固定, 滚动不消失) */}
        <div className="relative z-10 flex-1 overflow-y-auto custom-scrollbar px-5 pt-4 flex flex-col">
          {/* 内容区 */}
          <div className="flex-1 flex flex-col pb-4">
            {showEntryChoice ? (
              <div className="flex-1 flex items-center justify-center">
                <div className="max-w-sm w-full space-y-3">
                  <button
                    onClick={() => {
                      setShowEntryChoice(false);
                      if (conversations.length > 0) setActiveId(conversations[0].id);
                    }}
                    className="glass-card glass-card-accent rounded-xl px-4 py-3.5 w-full text-left hover:bg-glass-active transition-colors"
                  >
                    <div className="flex items-center gap-2">
                      <MessageSquare className="h-4 w-4 text-accent shrink-0" />
                      <span className="text-sm font-medium text-text-primary">继续最近对话</span>
                    </div>
                    <p className="text-xs text-text-tertiary mt-1 truncate">
                      {conversations[0]?.title ?? ''}
                    </p>
                  </button>
                  <button
                    onClick={() => {
                      setShowEntryChoice(false);
                      handleNew();
                    }}
                    className="glass-card rounded-xl px-4 py-3.5 w-full text-left hover:bg-glass-active transition-colors"
                  >
                    <div className="flex items-center gap-2">
                      <Plus className="h-4 w-4 text-accent shrink-0" />
                      <span className="text-sm font-medium text-text-primary">开始新对话</span>
                    </div>
                  </button>
                </div>
              </div>
            ) : !active || active.messages.length === 0 ? (
              <EmptyWelcome onPromptClick={(text) => setInput(text)} />
            ) : (
              <div className="space-y-6">
                {active.messages.map((msg, idx) => (
                  <MessageBubble
                    key={msg.id}
                    message={msg}
                    isLast={idx === active.messages.length - 1}
                    onRetry={handleRetry}
                    onFork={handleFork}
                  />
                ))}
                <div ref={messagesEndRef} />
              </div>
            )}
          </div>

          {/* 输入区(sticky 固定在消息流底部, 不随滚动消失; 底部渐变让滚过的内容淡出) */}
          <div className="sticky bottom-0 z-10 shrink-0 -mx-5 px-5 pt-3 pb-4 bg-gradient-to-t from-[var(--glass-bg)] to-transparent">
            <div className="max-w-3xl mx-auto">
              {/* 折叠选项面板: think / model (紧凑 + 独立反光, 右对齐) */}
              {optionsOpen && (
                <div className="mb-2 w-fit ml-auto">
                  <OptionsPanel
                    think={settings.think}
                    model={settings.model}
                    modelOptions={modelOptions}
                    onToggleThink={(v) => updateSettings('think', v)}
                    onModelChange={(v) => updateSettings('model', v)}
                  />
                </div>
              )}

              {/* 附件预览条 */}
              {pendingAttachments.length > 0 && (
                <div className="flex flex-wrap gap-2 mb-2">
                  {pendingAttachments.map((att, i) => (
                    <div key={i} className="glass-card rounded-md px-2.5 py-1.5 flex items-center gap-2 text-xs">
                      <FileText className="h-3 w-3 text-text-tertiary shrink-0" />
                      <span className="text-text-primary truncate max-w-[120px]">{att.name}</span>
                      <button onClick={() => removeAttachment(i)} className="text-text-tertiary hover:text-error shrink-0">
                        <X className="h-3 w-3" />
                      </button>
                    </div>
                  ))}
                </div>
              )}

              {activePendingUserInput && (
                <div className="glass-card glass-card-accent rounded-xl px-4 py-3 mb-2">
                  <div className="flex items-center gap-2 text-sm font-medium text-accent mb-1.5">
                    <Wrench className="h-4 w-4" />
                    工具正在等待你的回答
                  </div>
                  <p className="text-sm text-text-primary whitespace-pre-wrap">
                    {activePendingUserInput.question}
                  </p>
                </div>
              )}

              <div ref={inputCardRef} className="glass-card glass-card-accent rounded-xl p-3 flex items-end gap-2">
                {/* 隐藏文件选择器 */}
                <input
                  ref={fileInputRef}
                  type="file"
                  multiple
                  accept="image/*,.txt,.md,.pdf"
                  onChange={handleFileSelect}
                  className="hidden"
                />
                <Button
                  variant="ghost"
                  size="sm"
                  title="附件"
                  className="shrink-0 mb-0.5"
                  disabled={Boolean(activePendingUserInput)}
                  onClick={() => fileInputRef.current?.click()}
                >
                  <Paperclip className="h-4 w-4" />
                </Button>
                <textarea
                  ref={textareaRef}
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  onKeyDown={handleKeyDown}
                  placeholder={activePendingUserInput
                    ? '输入对工具提问的回答, Enter 提交'
                    : '输入消息, Enter 发送, Shift+Enter 换行'}
                  rows={1}
                  className="flex-1 min-w-0 bg-transparent border-0 outline-none resize-none text-sm text-text-primary placeholder:text-text-tertiary py-2 max-h-[200px]"
                />
                {/* 折叠面板开关(替换原麦克风) */}
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => setOptionsOpen((v) => !v)}
                  className={cn('shrink-0 mb-0.5', optionsOpen && 'text-accent')}
                  title={optionsOpen ? '收起选项' : '展开选项'}
                >
                  <ChevronDown className={cn('h-4 w-4 transition-transform', optionsOpen && 'rotate-180')} />
                </Button>
                {activePendingUserInput ? (
                  <>
                    <Button
                      variant="primary"
                      size="sm"
                      onClick={() => void handleAnswerUserInput()}
                      disabled={!input.trim() || answeringRequestId === activePendingUserInput.requestId}
                      className="shrink-0 mb-0.5"
                      title="提交回答"
                    >
                      {answeringRequestId === activePendingUserInput.requestId
                        ? <Loader2 className="h-4 w-4 animate-spin" />
                        : <Send className="h-4 w-4" />}
                    </Button>
                    <Button variant="danger" size="sm" onClick={handleStop} className="shrink-0 mb-0.5">
                      <Square className="h-4 w-4" />
                    </Button>
                  </>
                ) : generating ? (
                  <Button variant="danger" size="sm" onClick={handleStop} className="shrink-0 mb-0.5">
                    <Square className="h-4 w-4" />
                  </Button>
                ) : (
                  <Button
                    variant="primary"
                    size="sm"
                    onClick={handleSend}
                    disabled={!input.trim() && pendingAttachments.length === 0}
                    className="shrink-0 mb-0.5"
                    title="发送"
                  >
                    <Send className="h-4 w-4" />
                  </Button>
                )}
              </div>
              <p className="text-center text-xs text-text-tertiary mt-2">
                内容由模型生成, 请注意甄别
              </p>
            </div>
          </div>
        </div>
      </Card>
      </div>

      {/* 聊天设置弹窗 */}
      <ChatSettingsModal
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        settings={settings}
        modelOptions={modelOptions}
        onChange={updateSettings}
        plugins={plugins}
        onTogglePlugin={togglePlugin}
        onViewCapabilities={(name) => setCapabilityPlugin(name)}
        onViewConfig={(name) => setConfigPlugin(name)}
        onOpenMemory={() => setMemoryOpen(true)}
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
        onClose={() => setConfigPlugin(null)}
        onSaved={handlePluginConfigSaved}
      />

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
          <p className="text-xs text-text-tertiary">仅影响之后的工具调用可见范围, 历史消息不变</p>
          <button
            onClick={() => void handleMoveConversation(null)}
            className="glass-card w-full rounded-lg px-3 py-2.5 text-left hover:bg-glass-active transition-colors"
          >
            <div className="flex items-center gap-2">
              <MessageSquare className="h-4 w-4 text-text-tertiary shrink-0" />
              <span className="text-sm text-text-primary">最近 (无项目)</span>
            </div>
          </button>
          {projects.map((p) => (
            <button
              key={p.project_id}
              onClick={() => void handleMoveConversation(p.project_id)}
              className="glass-card w-full rounded-lg px-3 py-2.5 text-left hover:bg-glass-active transition-colors"
            >
              <div className="flex items-center gap-2">
                <Folder className="h-4 w-4 text-accent shrink-0" />
                <span className="text-sm text-text-primary truncate">{p.name}</span>
              </div>
              <p className="text-xs text-text-tertiary mt-0.5 truncate">{p.root_path}</p>
            </button>
          ))}
          {projects.length === 0 && (
            <p className="text-xs text-text-tertiary">暂无项目, 可先在侧边栏新建</p>
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
        <p className="text-xs text-text-tertiary">
          项目绑定一个工作区文件夹, 项目下的对话可读写该文件夹, 并拥有独立的项目记忆层
        </p>
        {error && <p className="text-sm text-error">{error}</p>}
        <input
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="项目名称"
          className="glass-input w-full text-sm"
        />
        <div className="flex gap-2">
          <input
            type="text"
            value={rootPath}
            onChange={(e) => setRootPath(e.target.value)}
            placeholder="工作区文件夹绝对路径, 如 F:\work\my-project"
            className="glass-input flex-1 text-sm"
          />
          <Button variant="ghost" title="浏览服务器目录" onClick={() => setPickerOpen(true)}>
            <FolderOpen className="h-4 w-4" />
          </Button>
        </div>
        <div className="flex justify-end gap-2">
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
  const [parent, setParent] = useState<string | null>(null);
  const [dirs, setDirs] = useState<DirEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const load = useCallback(async (path: string) => {
    setLoading(true);
    setError('');
    try {
      const resp = await chatApi.browseDirs(path);
      setCurrent(resp.path);
      setParent(resp.parent);
      setDirs(resp.dirs);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (open) void load(initialPath.trim());
  }, [open, initialPath, load]);

  // 根视图 (盘符页) 无具体路径可选
  const canSelect = current !== '';

  return (
    <Modal open={open} onClose={onClose} title="选择工作区目录" size="md">
      <div className="space-y-3">
        <div className="flex items-center gap-2">
          <Button
            variant="ghost"
            size="sm"
            title="上一级"
            disabled={parent === null || loading}
            onClick={() => parent !== null && void load(parent)}
          >
            <ArrowUp className="h-4 w-4" />
          </Button>
          <div className="flex-1 min-w-0 text-sm text-text-secondary truncate" title={current}>
            {current || '选择磁盘'}
          </div>
        </div>
        {error && <p className="text-sm text-error">{error}</p>}
        <div className="max-h-72 overflow-y-auto custom-scrollbar space-y-1">
          {loading ? (
            <p className="text-sm text-text-tertiary px-1 py-2">加载中...</p>
          ) : dirs.length === 0 ? (
            <p className="text-sm text-text-tertiary px-1 py-2">无子目录</p>
          ) : (
            dirs.map((d) => (
              <button
                key={d.path}
                onClick={() => void load(d.path)}
                className="w-full flex items-center gap-2 px-2 py-1.5 rounded-md hover:bg-glass-active transition-colors text-left"
              >
                {current === '' ? (
                  <HardDrive className="h-4 w-4 text-accent shrink-0" />
                ) : (
                  <Folder className="h-4 w-4 text-accent shrink-0" />
                )}
                <span className="text-sm text-text-primary truncate">{d.name}</span>
              </button>
            ))
          )}
        </div>
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>取消</Button>
          <Button variant="primary" disabled={!canSelect} onClick={() => { onSelect(current); onClose(); }}>
            选择此目录
          </Button>
        </div>
      </div>
    </Modal>
  );
}

// 精致拨杆开关
function Toggle({ checked, onChange, title }: { checked: boolean; onChange: (v: boolean) => void; title?: string }) {
  return (
    <button
      onClick={() => onChange(!checked)}
      className={cn(
        'w-9 h-5 rounded-full relative transition-colors shrink-0 border',
        checked ? 'bg-accent border-accent' : 'bg-glass-active border-glass-border'
      )}
      title={title}
    >
      <span
        className={cn(
          'absolute top-1/2 -translate-y-1/2 left-0.5 w-3.5 h-3.5 rounded-full bg-white shadow transition-transform',
          checked && 'translate-x-4'
        )}
      />
    </button>
  );
}

// 折叠选项面板(紧凑自适应宽度 + 独立反光)
function OptionsPanel({
  think,
  model,
  modelOptions,
  onToggleThink,
  onModelChange,
}: {
  think: string;
  model: string;
  modelOptions: { value: string; label: string }[];
  onToggleThink: (v: string) => void;
  onModelChange: (v: string) => void;
}) {
  const reflectRef = useStandaloneGlassReflect<HTMLDivElement>({
    reflectRange: 100,
    reflectSize: 100,
  });

  return (
    <div
      ref={reflectRef}
      className="glass-card glass-card-accent rounded-xl px-3 py-2 flex items-center gap-4 animate-fade-in w-fit"
    >
      {/* think 选择 */}
      <div className="flex items-center gap-2">
        <Brain className={cn('h-4 w-4', think !== 'off' ? 'text-accent' : 'text-text-tertiary')} />
        <span className="text-sm text-text-secondary">思考</span>
        <Select
          value={think}
          onChange={(e) => onToggleThink(e.target.value)}
          options={[
            { value: 'off', label: '关闭' },
            { value: 'low', label: '低' },
            { value: 'medium', label: '中' },
            { value: 'high', label: '高' },
          ]}
        />
      </div>

      {/* 分隔线 */}
      <div className="w-px h-5 bg-glass-border" />

      {/* model 选择 */}
      <div className="flex items-center gap-2">
        <span className="text-sm text-text-secondary">模型</span>
        <div className="w-36">
          <Select
            value={model}
            onChange={(e) => onModelChange(e.target.value)}
            options={modelOptions}
          />
        </div>
      </div>
    </div>
  );
}

// 聊天设置弹窗
function ChatSettingsModal({
  open,
  onClose,
  settings,
  modelOptions,
  onChange,
  plugins,
  onTogglePlugin,
  onViewCapabilities,
  onViewConfig,
  onOpenMemory,
  modelsDetail,
  onAddModel,
  onEditModel,
  onDeleteModel,
}: {
  open: boolean;
  onClose: () => void;
  settings: ChatSettings;
  modelOptions: { value: string; label: string }[];
  onChange: <K extends keyof ChatSettings>(key: K, value: ChatSettings[K]) => void;
  plugins: ChatPlugin[];
  onTogglePlugin: (name: string, enabled: boolean) => void;
  onViewCapabilities: (name: string) => void;
  onViewConfig: (name: string) => void;
  onOpenMemory: () => void;
  modelsDetail: Record<string, ModelConfigItem>;
  onAddModel: () => void;
  onEditModel: (name: string) => void;
  onDeleteModel: (name: string) => void;
}) {
  return (
    <Modal open={open} onClose={onClose} title="对话设置" size="md">
      <div className="space-y-5">
        {/* 模型 */}
        <div>
          <div className="flex items-center justify-between mb-2">
            <label className="text-sm font-medium text-text-primary">模型</label>
            <button
              onClick={onAddModel}
              className="text-xs text-accent hover:underline inline-flex items-center gap-1"
            >
              <Plus className="h-3 w-3" /> 新增模型
            </button>
          </div>
          <Select
            value={settings.model}
            onChange={(e) => onChange('model', e.target.value)}
            options={modelOptions}
          />
          {/* 模型列表 */}
          {Object.keys(modelsDetail).length > 0 && (
            <div className="mt-2 space-y-1.5">
              {Object.entries(modelsDetail).map(([name, cfg]) => (
                <div
                  key={name}
                  className="glass-card rounded-md px-2.5 py-1.5 flex items-center justify-between gap-2 text-xs"
                >
                  <div className="min-w-0 flex-1">
                    <span className="text-text-primary font-medium">{name}</span>
                    {cfg.model && (
                      <span className="text-text-tertiary ml-2">{cfg.model}</span>
                    )}
                  </div>
                  <div className="flex items-center gap-1 shrink-0">
                    <button
                      onClick={() => onEditModel(name)}
                      className="p-0.5 rounded text-text-tertiary hover:text-accent transition-colors"
                      title="编辑"
                    >
                      <Pencil className="h-3 w-3" />
                    </button>
                    <button
                      onClick={() => onDeleteModel(name)}
                      className="p-0.5 rounded text-text-tertiary hover:text-error transition-colors"
                      title="删除"
                    >
                      <Trash2 className="h-3 w-3" />
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* 思考强度 */}
        <div className="flex items-center justify-between">
          <div>
            <div className="text-sm font-medium text-text-primary">思考强度</div>
            <div className="text-xs text-text-tertiary mt-0.5">控制模型推理深度(需模型支持)</div>
          </div>
          <Select
            value={settings.think}
            onChange={(e) => onChange('think', e.target.value)}
            options={[
              { value: 'off', label: '关闭' },
              { value: 'low', label: '低' },
              { value: 'medium', label: '中' },
              { value: 'high', label: '高' },
            ]}
          />
        </div>

        {/* 温度 */}
        <div>
          <div className="flex items-center justify-between mb-2">
            <label className="text-sm font-medium text-text-primary">温度</label>
            <span className="text-sm text-accent font-medium">{settings.temperature.toFixed(2)}</span>
          </div>
          <input
            type="range"
            min={0}
            max={2}
            step={0.05}
            value={settings.temperature}
            onChange={(e) => onChange('temperature', Number(e.target.value))}
            className="w-full accent-accent"
          />
          <div className="flex justify-between text-xs text-text-tertiary mt-1">
            <span>严谨 0</span>
            <span>平衡 1</span>
            <span>发散 2</span>
          </div>
        </div>

        {/* 系统提示词 */}
        <div>
          <label className="block text-sm font-medium text-text-primary mb-2">系统提示词</label>
          <textarea
            value={settings.systemPrompt}
            onChange={(e) => onChange('systemPrompt', e.target.value)}
            placeholder="为当前对话设置系统级指令, 留空使用默认"
            rows={4}
            className="glass-input w-full resize-none text-sm"
          />
        </div>

        {/* 插件 */}
        <div>
          <label className="block text-sm font-medium text-text-primary mb-2">插件</label>
          {plugins.length === 0 ? (
            <p className="text-xs text-text-tertiary">暂无可用插件</p>
          ) : (
            <div className="space-y-2">
              {plugins.map((p) => {
                const capCount = Object.values(p.capabilities).reduce((n, list) => n + list.length, 0);
                return (
                  <div
                    key={p.name}
                    className="glass-card rounded-lg px-3 py-2.5 flex items-center justify-between gap-3"
                  >
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <Puzzle className="h-4 w-4 text-accent shrink-0" />
                        <span className="text-sm font-medium text-text-primary truncate">{p.name}</span>
                        <span className="text-xs text-text-tertiary shrink-0">v{p.version}</span>
                      </div>
                      <p className="text-xs text-text-tertiary mt-1 line-clamp-2">{p.description}</p>
                      <div className="flex items-center gap-2 mt-1.5">
                        <button
                          onClick={() => onViewCapabilities(p.name)}
                          className="text-xs text-accent hover:underline inline-flex items-center gap-1"
                        >
                          查看全部能力 ({capCount})
                        </button>
                        <button
                          onClick={() => onViewConfig(p.name)}
                          className="text-xs text-accent hover:underline inline-flex items-center gap-1"
                        >
                          配置
                        </button>
                      </div>
                    </div>
                    <Toggle
                      checked={p.enabled}
                      onChange={(v) => onTogglePlugin(p.name, v)}
                      title={p.enabled ? '停用插件' : '启用插件'}
                    />
                  </div>
                );
              })}
            </div>
          )}
          <p className="text-xs text-text-tertiary mt-1.5">启用后对新会话生效, 能力来自插件 meta.yaml 声明</p>
        </div>

        {/* 记忆管理 */}
        <div>
          <label className="block text-sm font-medium text-text-primary mb-2">长期记忆</label>
          <button
            onClick={onOpenMemory}
            className="glass-card rounded-lg px-3 py-2.5 w-full text-left hover:bg-glass-active transition-colors"
          >
            <div className="flex items-center gap-2">
              <Brain className="h-4 w-4 text-accent shrink-0" />
              <span className="text-sm text-text-primary">管理记忆</span>
            </div>
            <p className="text-xs text-text-tertiary mt-1">查看 / 添加 / 删除长期记忆条目</p>
          </button>
        </div>

        <div className="flex justify-end pt-1">
          <Button variant="primary" onClick={onClose}>完成</Button>
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
  const kinds = Object.keys(CAPABILITY_LABELS) as CapabilityKind[];

  return (
    <Modal open={plugin !== null} onClose={onClose} title={`${plugin.name} 能力`} size="md">
      <div className="space-y-5">
        {/* 插件概要 */}
        <div className="flex items-center gap-2 text-xs text-text-tertiary">
          <span>v{plugin.version}</span>
          <span>·</span>
          <span className={plugin.enabled ? 'text-success' : 'text-text-tertiary'}>
            {plugin.enabled ? '插件已启用' : '插件已停用 (能力不生效)'}
          </span>
        </div>

        {kinds.map((kind) => {
          const list = plugin.capabilities[kind];
          if (list.length === 0) return null;
          return (
            <div key={kind}>
              <div className="text-sm font-medium text-text-primary mb-2">
                {CAPABILITY_LABELS[kind]}
                <span className="text-xs text-text-tertiary font-normal ml-2">{list.length}</span>
              </div>
              <div className="space-y-1.5">
                {list.map((cap) => {
                  // 独立启用位; 实效还需插件聚合启用
                  const effective = plugin.enabled && cap.enabled;
                  return (
                    <div
                      key={cap.name}
                      className="glass-card rounded-lg px-3 py-2 flex items-center justify-between gap-3"
                    >
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2">
                          <span className={cn('text-sm truncate', effective ? 'text-text-primary' : 'text-text-tertiary')}>
                            {cap.name}
                          </span>
                          {cap.enabled && !plugin.enabled && (
                            <span className="text-xs text-text-tertiary shrink-0">(插件停用中)</span>
                          )}
                        </div>
                        {cap.description && (
                          <p className="text-xs text-text-tertiary mt-0.5 line-clamp-2">{cap.description}</p>
                        )}
                      </div>
                      <Toggle
                        checked={cap.enabled}
                        onChange={(v) => onToggleCapability(plugin.name, kind, cap.name, v)}
                        title={cap.enabled ? `停用${CAPABILITY_LABELS[kind]}` : `启用${CAPABILITY_LABELS[kind]}`}
                      />
                    </div>
                  );
                })}
              </div>
            </div>
          );
        })}

        <div className="flex justify-end pt-1">
          <Button variant="primary" onClick={onClose}>完成</Button>
        </div>
      </div>
    </Modal>
  );
}

// 插件配置弹窗: 按 config_schema 渲染表单, 保存到后端
function PluginConfigModal({
  pluginName,
  onClose,
  onSaved,
}: {
  pluginName: string | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [data, setData] = useState<PluginConfigResponse | null>(null);
  const [form, setForm] = useState<Record<string, unknown>>({});
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  // 加载配置
  useEffect(() => {
    if (!pluginName) { setData(null); return; }
    let cancelled = false;
    (async () => {
      try {
        const resp = await chatApi.getPluginConfig(pluginName);
        if (!cancelled) {
          setData(resp);
          setForm({ ...resp.config });
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
      await chatApi.savePluginConfig(pluginName, form);
      onSaved();
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal open={pluginName !== null} onClose={onClose} title={`${pluginName} 配置`} size="md">
      <div className="space-y-4">
        {error && <p className="text-sm text-error">{error}</p>}
        {keys.length === 0 && !data && <p className="text-sm text-text-tertiary">加载中...</p>}
        {keys.length === 0 && data && <p className="text-sm text-text-tertiary">该插件无可配置项</p>}
        {keys.map((key) => {
          const field = schema[key];
          const value = form[key] ?? field.default ?? '';
          return (
            <div key={key}>
              <label className="block text-sm font-medium text-text-primary mb-1">
                {key}
                <span className="text-xs text-text-tertiary font-normal ml-2">{field.type}</span>
              </label>
              {field.description && (
                <p className="text-xs text-text-tertiary mb-1.5">{field.description}</p>
              )}
              {field.type === 'bool' ? (
                <Toggle
                  checked={Boolean(value)}
                  onChange={(v) => setForm((prev) => ({ ...prev, [key]: v }))}
                />
              ) : field.type === 'select' && field.options ? (
                <Select
                  value={String(value)}
                  onChange={(e) => setForm((prev) => ({ ...prev, [key]: e.target.value }))}
                  options={field.options.map((o) => ({ value: o, label: o }))}
                />
              ) : field.type === 'number' ? (
                <input
                  type="number"
                  value={Number(value) || 0}
                  onChange={(e) => setForm((prev) => ({ ...prev, [key]: Number(e.target.value) }))}
                  className="glass-input w-full text-sm"
                />
              ) : field.type === 'textarea' ? (
                <textarea
                  value={String(value)}
                  onChange={(e) => setForm((prev) => ({ ...prev, [key]: e.target.value }))}
                  className="glass-input w-full resize-y text-sm"
                  rows={5}
                />
              ) : (
                <input
                  type="text"
                  value={String(value)}
                  onChange={(e) => setForm((prev) => ({ ...prev, [key]: e.target.value }))}
                  className="glass-input w-full text-sm"
                  placeholder={String(field.default ?? '')}
                />
              )}
            </div>
          );
        })}
        <div className="flex justify-end gap-2 pt-1">
          <Button variant="ghost" onClick={onClose}>取消</Button>
          <Button variant="primary" onClick={handleSave} disabled={saving}>
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
    <Modal open={open} onClose={onClose} title="长期记忆管理" size="lg">
      <div className="space-y-4">
        {error && <p className="text-sm text-error">{error}</p>}

        {/* 新增记忆 */}
        <div className="glass-card rounded-lg p-3 space-y-2">
          <div className="text-sm font-medium text-text-primary">添加记忆</div>
          <input
            type="text"
            value={newTitle}
            onChange={(e) => setNewTitle(e.target.value)}
            placeholder="标题"
            className="glass-input w-full text-sm"
          />
          <textarea
            value={newContent}
            onChange={(e) => setNewContent(e.target.value)}
            placeholder="内容"
            rows={3}
            className="glass-input w-full resize-none text-sm"
          />
          <div className="flex gap-2">
            <input
              type="text"
              value={newTags}
              onChange={(e) => setNewTags(e.target.value)}
              placeholder="标签 (逗号分隔)"
              className="glass-input flex-1 text-sm"
            />
            <Select
              value={String(newImportance)}
              onChange={(e) => setNewImportance(Number(e.target.value))}
              options={[1, 2, 3, 4, 5].map((n) => ({ value: String(n), label: `权重 ${n}` }))}
            />
            <span className="glass-input text-sm text-text-secondary">当前会话</span>
          </div>
          <div className="flex justify-end">
            <Button variant="primary" onClick={handleAdd} disabled={adding || !memoryScope || !newTitle.trim() || !newContent.trim()}>
              {adding ? '添加中...' : '添加'}
            </Button>
          </div>
        </div>

        {/* 记忆列表 (按层分组: 全局 / 各项目) */}
        {loading ? (
          <p className="text-sm text-text-tertiary">加载中...</p>
        ) : memories.length === 0 ? (
          <p className="text-sm text-text-tertiary">暂无记忆</p>
        ) : (
          <div className="space-y-3 max-h-80 overflow-y-auto">
            {layeredGroups.map((g) => (
              <div key={g.scope} className="space-y-2">
                <div className="flex items-center gap-1.5 text-xs text-text-tertiary">
                  <Brain className="h-3.5 w-3.5" />
                  <span>当前会话 · {g.memories.length} 条</span>
                </div>
                {g.memories.map((m) => (
                  <div key={m.id} className="glass-card rounded-lg px-3 py-2.5">
                    <div className="flex items-start justify-between gap-2">
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2">
                          <span className="text-sm font-medium text-text-primary">{m.title}</span>
                          <span className="text-xs text-text-tertiary">权重 {m.importance}</span>
                        </div>
                        <p className="text-xs text-text-secondary mt-1 line-clamp-3">{m.content}</p>
                        {m.tags.length > 0 && (
                          <div className="flex gap-1 mt-1.5">
                            {m.tags.map((t) => (
                              <span key={t} className="text-xs px-1.5 py-0.5 rounded bg-glass-active text-text-tertiary">
                                {t}
                              </span>
                            ))}
                          </div>
                        )}
                      </div>
                      <button
                        onClick={() => handleDelete(m)}
                        className="text-text-tertiary hover:text-error shrink-0 mt-0.5"
                        title="删除记忆"
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

        <div className="flex justify-end pt-1">
          <Button variant="primary" onClick={onClose}>完成</Button>
        </div>
      </div>
    </Modal>
  );
}

// 聊天页独立顶栏
type ChatTheme = 'dark' | 'light';

function ChatHeader({
  theme,
  onToggleTheme,
  onBack,
}: {
  theme: ChatTheme;
  onToggleTheme: () => void;
  onBack: () => void;
}) {
  const headerRef = useStandaloneGlassReflect<HTMLElement>({
    reflectRange: 150,
    reflectSize: 150,
  });

  return (
    <header ref={headerRef} className="glass-header flex items-center justify-between px-4 py-2.5 shrink-0">
      <div className="flex items-center gap-3">
        <button onClick={onBack} className="theme-toggle" title="返回管理面板">
          <ArrowLeft className="h-4 w-4 text-text-secondary" />
        </button>
        <div className="flex items-center gap-2">
          <div className="w-7 h-7 rounded-md bg-accent flex items-center justify-center shadow-glow-accent">
            <Sparkles className="h-4 w-4 text-white" />
          </div>
          <h1 className="text-base font-semibold text-text-primary">Satrap 对话</h1>
        </div>
      </div>

      <button
        onClick={onToggleTheme}
        className="theme-toggle"
        title={theme === 'dark' ? '切换到亮色模式' : '切换到暗色模式'}
      >
        {theme === 'dark' ? (
          <Sun className="h-4 w-4 text-warning" />
        ) : (
          <Moon className="h-4 w-4 text-accent" />
        )}
      </button>
    </header>
  );
}

// 会话列表项
function ConversationItem({
  conversation,
  color,
  active,
  onSelect,
  onDelete,
  onMove,
}: {
  conversation: Conversation;
  color: GlassColor;
  active: boolean;
  onSelect: () => void;
  onDelete: () => void;
  // 移入/移出项目 (草稿会话无此入口)
  onMove?: () => void;
}) {
  const reflectRef = useStandaloneGlassReflect<HTMLDivElement>({
    reflectRange: 80,
    reflectSize: 60,
  });

  return (
    <div
      ref={reflectRef}
      onClick={onSelect}
      className={cn(
        'glass-card group flex items-center gap-2 px-3 py-2.5 rounded-md cursor-pointer transition-colors',
        COLOR_CARD_CLASS[color],
        active ? 'border-glass-border-hover' : 'opacity-80 hover:opacity-100'
      )}
    >
      <MessageSquare className={cn('h-4 w-4 shrink-0', active ? COLOR_TEXT_CLASS[color] : 'text-text-tertiary')} />
      <div className="flex-1 min-w-0">
        <div className={cn('text-sm truncate', active ? 'text-text-primary font-medium' : 'text-text-secondary')}>
          {conversation.title}
        </div>
        <div className="text-xs text-text-tertiary">{formatRelativeTime(conversation.updatedAt)}</div>
      </div>
      {onMove && (
        <button
          onClick={(e) => {
            e.stopPropagation();
            onMove();
          }}
          className="opacity-0 group-hover:opacity-100 transition-opacity text-text-tertiary hover:text-accent shrink-0"
          title="移入/移出项目"
        >
          <FolderInput className="h-3.5 w-3.5" />
        </button>
      )}
      <button
        onClick={(e) => {
          e.stopPropagation();
          onDelete();
        }}
        className="opacity-0 group-hover:opacity-100 transition-opacity text-text-tertiary hover:text-error shrink-0"
        title="删除对话"
      >
        <Trash2 className="h-3.5 w-3.5" />
      </button>
    </div>
  );
}

// 项目组 (侧边栏可折叠分组: 项目头 + 组内会话)
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
    <div>
      <div
        onClick={onToggle}
        className="group flex items-center gap-1.5 px-2 py-1.5 rounded-md cursor-pointer hover:bg-glass-active transition-colors"
      >
        {collapsed ? (
          <ChevronRight className="h-3.5 w-3.5 text-text-tertiary shrink-0" />
        ) : (
          <ChevronDown className="h-3.5 w-3.5 text-text-tertiary shrink-0" />
        )}
        <Folder className="h-4 w-4 text-accent shrink-0" />
        <span className="flex-1 min-w-0 text-sm font-medium text-text-primary truncate" title={project.root_path}>
          {project.name}
        </span>
        <button
          onClick={(e) => {
            e.stopPropagation();
            onNewConversation();
          }}
          className="opacity-0 group-hover:opacity-100 transition-opacity text-text-tertiary hover:text-accent shrink-0"
          title="在此项目下新建对话"
        >
          <Plus className="h-3.5 w-3.5" />
        </button>
        <button
          onClick={(e) => {
            e.stopPropagation();
            onDeleteProject();
          }}
          className="opacity-0 group-hover:opacity-100 transition-opacity text-text-tertiary hover:text-error shrink-0"
          title="删除项目 (仅解绑会话, 磁盘文件不动)"
        >
          <Trash2 className="h-3.5 w-3.5" />
        </button>
      </div>
      {!collapsed && (
        <div className="ml-4 mt-1 space-y-1">
          {conversations.length === 0 ? (
            <div className="px-2 py-1 text-xs text-text-tertiary">暂无对话</div>
          ) : (
            conversations.map((conv) => (
              <ConversationItem
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
      )}
    </div>
  );
}

// 空会话欢迎页
function EmptyWelcome({ onPromptClick }: { onPromptClick: (text: string) => void }) {
  const suggestions: { icon: typeof Sparkles; label: string; color: GlassColor }[] = [
    { icon: Sparkles, label: '帮我写一段 Python 快速排序', color: 'accent' },
    { icon: MessageSquare, label: '解释一下什么是上下文窗口', color: 'purple' },
    { icon: Bot, label: '如何设计一个 Agent 工作流', color: 'teal' },
  ];

  return (
    <div className="h-full flex flex-col items-center justify-center text-center px-6">
      <div className="w-14 h-14 rounded-xl bg-accent flex items-center justify-center shadow-glow-accent mb-4">
        <Sparkles className="h-7 w-7 text-white" />
      </div>
      <h2 className="text-xl font-semibold text-text-primary mb-1">开始新的对话</h2>
      <p className="text-sm text-text-tertiary mb-8">输入你的问题, 或从下方建议开始</p>
      <div className="grid grid-cols-1 gap-3 w-full max-w-md">
        {suggestions.map((s) => (
          <SuggestionCard key={s.label} suggestion={s} onClick={() => onPromptClick(s.label)} />
        ))}
      </div>
    </div>
  );
}

// 建议卡片(多色玻璃 + 独立反光)
function SuggestionCard({
  suggestion,
  onClick,
}: {
  suggestion: { icon: typeof Sparkles; label: string; color: GlassColor };
  onClick: () => void;
}) {
  const reflectRef = useStandaloneGlassReflect<HTMLButtonElement>({
    reflectRange: 100,
    reflectSize: 90,
  });
  const Icon = suggestion.icon;

  return (
    <button
      ref={reflectRef}
      onClick={onClick}
      className={cn(
        'glass-card rounded-lg p-4 flex items-center gap-3 text-left transition-transform hover:scale-[1.02]',
        COLOR_CARD_CLASS[suggestion.color]
      )}
    >
      <Icon className={cn('h-5 w-5 shrink-0', COLOR_TEXT_CLASS[suggestion.color])} />
      <div className="text-sm text-text-secondary">{suggestion.label}</div>
    </button>
  );
}

// 工具调用状态图标
function ToolStatusIcon({ success }: { success: boolean | null }) {
  if (success === null) return <Loader2 className="h-3 w-3 text-warning animate-spin" />;
  if (success) return <CheckCircle2 className="h-3 w-3 text-success" />;
  return <XCircle className="h-3 w-3 text-error" />;
}

// 消息气泡(用户紫色 / 助手蓝色, 独立反光)
function MessageBubble({
  message,
  isLast,
  onRetry,
  onFork,
}: {
  message: ChatMessage;
  isLast?: boolean;
  onRetry?: () => void;
  onFork?: (turnIndex: number) => void;
}) {
  const isUser = message.role === 'user';
  const reflectRef = useStandaloneGlassReflect<HTMLDivElement>({
    reflectRange: 90,
    reflectSize: 80,
  });

  return (
    <div className="flex gap-3 group">
      {/* 头像: 用户消息 order-2 在右, AI 消息 order-1 在左 */}
      <div
        className={cn(
          'w-8 h-8 rounded-md flex items-center justify-center shrink-0',
          isUser ? 'order-2 bg-purple shadow-glow-purple' : 'order-1 bg-accent shadow-glow-accent'
        )}
      >
        {isUser ? (
          <User className="h-4 w-4 text-white" />
        ) : (
          <Bot className="h-4 w-4 text-white" />
        )}
      </div>

      {/* 内容: 用户消息 order-1 靠右, AI 消息 order-2 靠左 */}
      <div className={cn('flex-1 min-w-0 flex', isUser ? 'order-1 justify-end' : 'order-2 justify-start')}>
        <div className={cn('space-y-2', isUser ? 'max-w-full' : 'w-full')}>
          {/* 附件 (用户消息或 AI 消息都可能携带) */}
          {message.attachments && message.attachments.length > 0 && (
            <div className="flex flex-wrap gap-2">
              {message.attachments.map((att, i) => (
                <div key={i} className="glass-card rounded-md px-2.5 py-1.5 flex items-center gap-2 text-xs">
                  <FileText className="h-3 w-3 text-text-tertiary shrink-0" />
                  <span className="text-text-primary">{att.name}</span>
                </div>
              ))}
            </div>
          )}

          {/* AI 消息: 按 segments 时间顺序渲染; 用户消息或旧数据: 聚合渲染 */}
          {!isUser && message.segments && message.segments.length > 0 ? (
            <>
              {message.segments.map((seg, idx) => {
                switch (seg.type) {
                  case 'thinking':
                    return <ThinkingBlock key={`thinking-${idx}`} thinking={seg.content} />;
                  case 'tool':
                    return (
                      <div
                        key={seg.tool.call_id || `tool-${idx}`}
                        className="glass-card rounded-md px-2.5 py-1.5 flex items-center gap-2 text-xs"
                      >
                        <Wrench className="h-3 w-3 text-text-tertiary shrink-0" />
                        <span className="text-text-primary font-medium">{seg.tool.name}</span>
                        <ToolStatusIcon success={seg.tool.success} />
                      </div>
                    );
                  case 'content':
                    return (
                      <div
                        key={`content-${idx}`}
                        ref={idx === message.segments!.length - 1 ? reflectRef : undefined}
                        className={cn(
                          'glass-card inline-block max-w-full rounded-lg px-4 py-2.5 text-sm leading-relaxed break-words',
                          'glass-card-accent'
                        )}
                      >
                        <div className="markdown-body">
                          <ReactMarkdown
                            remarkPlugins={[remarkGfm, remarkMath]}
                            rehypePlugins={[rehypeKatex]}
                          >
                            {seg.content}
                          </ReactMarkdown>
                        </div>
                        {message.streaming && idx === message.segments!.length - 1 && (
                          <span className="inline-block w-1.5 h-4 ml-0.5 align-middle bg-accent animate-pulse" />
                        )}
                      </div>
                    );
                  default:
                    return null;
                }
              })}
            </>
          ) : (
            <>
              {/* 兼容旧数据或用户消息: 聚合渲染 */}
              {message.thinking && (
                <ThinkingBlock thinking={message.thinking} />
              )}
              {message.toolCalls && message.toolCalls.length > 0 && (
                <div className="space-y-1">
                  {message.toolCalls.map((tool) => (
                    <div
                      key={tool.call_id || tool.seq}
                      className="glass-card rounded-md px-2.5 py-1.5 flex items-center gap-2 text-xs"
                    >
                      <Wrench className="h-3 w-3 text-text-tertiary shrink-0" />
                      <span className="text-text-primary font-medium">{tool.name}</span>
                      <ToolStatusIcon success={tool.success} />
                    </div>
                  ))}
                </div>
              )}
              {(message.content || message.streaming) && (
                <div
                  ref={reflectRef}
                  className={cn(
                    'glass-card inline-block max-w-full rounded-lg px-4 py-2.5 text-sm leading-relaxed break-words',
                    isUser ? 'glass-card-purple whitespace-pre-wrap' : 'glass-card-accent'
                  )}
                >
                  {isUser ? (
                    message.content
                  ) : (
                    <div className="markdown-body">
                      <ReactMarkdown
                        remarkPlugins={[remarkGfm, remarkMath]}
                        rehypePlugins={[rehypeKatex]}
                      >
                        {message.content}
                      </ReactMarkdown>
                    </div>
                  )}
                  {message.streaming && (
                    <span className="inline-block w-1.5 h-4 ml-0.5 align-middle bg-accent animate-pulse" />
                  )}
                </div>
              )}
            </>
          )}

          {/* 操作按钮 (hover 显示) */}
          {!message.streaming && (
            <div className={cn(
              'flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity',
              isUser && 'justify-end'
            )}>
              {!isUser && isLast && onRetry && (
                <button
                  onClick={onRetry}
                  className="p-1 rounded text-text-tertiary hover:text-accent transition-colors"
                  title="重试"
                >
                  <RotateCcw className="h-3.5 w-3.5" />
                </button>
              )}
              {onFork && message.turnIndex !== undefined && (
                <button
                  onClick={() => onFork(message.turnIndex!)}
                  className="p-1 rounded text-text-tertiary hover:text-accent transition-colors"
                  title="从这里分支"
                >
                  <GitFork className="h-3.5 w-3.5" />
                </button>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// 思考流折叠块 (独立反光, 避免与气泡容器光效不对齐)
function ThinkingBlock({ thinking }: { thinking: string }) {
  const reflectRef = useStandaloneGlassReflect<HTMLDetailsElement>({
    reflectRange: 60,
    reflectSize: 50,
  });
  return (
    <details ref={reflectRef} className="glass-card glass-card-teal rounded-lg px-3 py-2 text-xs">
      <summary className="cursor-pointer text-teal flex items-center gap-1.5 select-none">
        <Brain className="h-3.5 w-3.5" />
        思考过程
      </summary>
      <div className="mt-2 text-text-secondary whitespace-pre-wrap break-words leading-relaxed">
        {thinking}
      </div>
    </details>
  );
}

// 模型编辑弹窗
function ModelEditModal({
  modelName,
  modelsDetail,
  onClose,
  onSaved,
}: {
  modelName: string | null;  // null=关闭, ''=新增, 否则为编辑的模型名
  modelsDetail: Record<string, ModelConfigItem>;
  onClose: () => void;
  onSaved: () => void;
}) {
  const isNew = modelName === '';
  const existing = modelName ? modelsDetail[modelName] : undefined;

  const [name, setName] = useState('');
  const [model, setModel] = useState('');
  const [baseUrl, setBaseUrl] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [temperature, setTemperature] = useState('');
  const [topP, setTopP] = useState('');
  const [maxTokens, setMaxTokens] = useState('');
  const [contextWindow, setContextWindow] = useState('');
  const [historyRatio, setHistoryRatio] = useState('');
  const [reasoningBody, setReasoningBody] = useState('');
  const [thinkingFieldName, setThinkingFieldName] = useState('');
  const [thinkingFields, setThinkingFields] = useState('');
  const [saving, setSaving] = useState(false);

  // 打开时填充表单
  useEffect(() => {
    if (modelName === null) return;
    if (isNew) {
      setName('');
      setModel('');
      setBaseUrl('');
      setApiKey('');
      setTemperature('');
      setTopP('');
      setMaxTokens('');
      setContextWindow('');
      setHistoryRatio('');
      setReasoningBody('');
      setThinkingFieldName('');
      setThinkingFields('');
    } else if (existing) {
      setName(existing.name);
      setModel(existing.model ?? '');
      setBaseUrl(existing.base_url ?? '');
      // 脱敏的 api_key (含 *) 不回显, 留空表示不修改
      const rawKey = existing.api_key ?? '';
      setApiKey(rawKey.includes('*') ? '' : rawKey);
      setTemperature(existing.temperature !== undefined ? String(existing.temperature) : '');
      setTopP(existing.top_p !== undefined ? String(existing.top_p) : '');
      setMaxTokens(existing.max_tokens !== undefined ? String(existing.max_tokens) : '');
      setContextWindow(existing.context_window !== undefined ? String(existing.context_window) : '');
      setHistoryRatio(existing.history_ratio !== undefined ? String(existing.history_ratio) : '');
      setReasoningBody(existing.reasoning_body ? JSON.stringify(existing.reasoning_body, null, 2) : '');
      setThinkingFieldName(existing.thinking_field_name ?? '');
      setThinkingFields(existing.thinking_fields ? existing.thinking_fields.join(', ') : '');
    }
  }, [modelName, isNew, existing]);

  const handleSave = useCallback(async () => {
    if (!name.trim()) {
      alert('请输入配置名称');
      return;
    }
    setSaving(true);
    try {
      const config: ModelConfigItem = {
        name: name.trim(),
        ...(model.trim() ? { model: model.trim() } : {}),
        ...(baseUrl.trim() ? { base_url: baseUrl.trim() } : {}),
        ...(apiKey.trim() ? { api_key: apiKey.trim() } : {}),
        ...(temperature.trim() ? { temperature: Number(temperature) } : {}),
        ...(topP.trim() ? { top_p: Number(topP) } : {}),
        ...(maxTokens.trim() ? { max_tokens: Number(maxTokens) } : {}),
        ...(contextWindow.trim() ? { context_window: Number(contextWindow) } : {}),
        ...(historyRatio.trim() ? { history_ratio: Number(historyRatio) } : {}),
        ...(reasoningBody.trim() ? { reasoning_body: JSON.parse(reasoningBody) } : {}),
        ...(thinkingFieldName.trim() ? { thinking_field_name: thinkingFieldName.trim() } : {}),
        ...(thinkingFields.trim() ? { thinking_fields: thinkingFields.split(',').map(s => s.trim()).filter(Boolean) } : {}),
      };
      const result = isNew
        ? await chatApi.addModel(config)
        : await chatApi.updateModel(modelName!, config);
      if (result.ok) {
        onSaved();
        onClose();
      } else {
        alert(result.error || '保存失败');
      }
    } catch (err) {
      console.error('[Chat] 保存模型失败:', err);
      alert(`保存失败: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setSaving(false);
    }
  }, [name, model, baseUrl, apiKey, temperature, topP, maxTokens, contextWindow, historyRatio, reasoningBody, thinkingFieldName, thinkingFields, isNew, modelName, onSaved, onClose]);

  if (modelName === null) return null;

  return (
    <Modal open={modelName !== null} onClose={onClose} title={isNew ? '新增模型配置' : `编辑模型: ${modelName}`} size="md">
      <div className="space-y-4">
        {/* 配置名称 */}
        <div>
          <label className="block text-sm font-medium text-text-primary mb-1.5">配置名称</label>
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="如: gpt-4o, claude-sonnet"
            disabled={!isNew}
            className="glass-input w-full text-sm"
          />
          {!isNew && <p className="text-xs text-text-tertiary mt-1">配置名称不可修改</p>}
        </div>

        {/* 模型 ID */}
        <div>
          <label className="block text-sm font-medium text-text-primary mb-1.5">模型 ID</label>
          <input
            value={model}
            onChange={(e) => setModel(e.target.value)}
            placeholder="如: gpt-4o, claude-3-5-sonnet-20241022"
            className="glass-input w-full text-sm"
          />
        </div>

        {/* Base URL */}
        <div>
          <label className="block text-sm font-medium text-text-primary mb-1.5">Base URL</label>
          <input
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
            placeholder="如: https://api.openai.com/v1"
            className="glass-input w-full text-sm"
          />
        </div>

        {/* API Key */}
        <div>
          <label className="block text-sm font-medium text-text-primary mb-1.5">API Key</label>
          <input
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            placeholder={isNew ? 'sk-...' : '留空表示不修改'}
            type="password"
            className="glass-input w-full text-sm"
          />
        </div>

        {/* 高级参数 */}
        <div className="grid grid-cols-3 gap-3">
          <div>
            <label className="block text-xs font-medium text-text-secondary mb-1">温度</label>
            <input
              value={temperature}
              onChange={(e) => setTemperature(e.target.value)}
              placeholder="0.7"
              type="number"
              step="0.1"
              min="0"
              max="2"
              className="glass-input w-full text-sm"
            />
          </div>
          <div>
            <label className="block text-xs font-medium text-text-secondary mb-1">Top P</label>
            <input
              value={topP}
              onChange={(e) => setTopP(e.target.value)}
              placeholder="1.0"
              type="number"
              step="0.05"
              min="0"
              max="1"
              className="glass-input w-full text-sm"
            />
          </div>
          <div>
            <label className="block text-xs font-medium text-text-secondary mb-1">Max Tokens</label>
            <input
              value={maxTokens}
              onChange={(e) => setMaxTokens(e.target.value)}
              placeholder="4096"
              type="number"
              min="1"
              className="glass-input w-full text-sm"
            />
          </div>
        </div>

        {/* 上下文参数 */}
        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className="block text-xs font-medium text-text-secondary mb-1">上下文窗口</label>
            <input
              value={contextWindow}
              onChange={(e) => setContextWindow(e.target.value)}
              placeholder="128000"
              type="number"
              min="1"
              className="glass-input w-full text-sm"
            />
          </div>
          <div>
            <label className="block text-xs font-medium text-text-secondary mb-1">历史比例</label>
            <input
              value={historyRatio}
              onChange={(e) => setHistoryRatio(e.target.value)}
              placeholder="0.7"
              type="number"
              step="0.05"
              min="0"
              max="1"
              className="glass-input w-full text-sm"
            />
          </div>
        </div>

        {/* 思考参数 */}
        <div>
          <label className="block text-xs font-medium text-text-secondary mb-1">思考请求格式 (JSON)</label>
          <textarea
            value={reasoningBody}
            onChange={(e) => setReasoningBody(e.target.value)}
            placeholder='{"thinking": {"type": "enabled"}}'
            rows={3}
            className="glass-input w-full text-sm font-mono"
          />
        </div>
        <div>
          <label className="block text-xs font-medium text-text-secondary mb-1">思考字段名</label>
          <input
            value={thinkingFieldName}
            onChange={(e) => setThinkingFieldName(e.target.value)}
            placeholder="reasoning_content"
            className="glass-input w-full text-sm"
          />
        </div>
        <div>
          <label className="block text-xs font-medium text-text-secondary mb-1">思考字段列表</label>
          <input
            value={thinkingFields}
            onChange={(e) => setThinkingFields(e.target.value)}
            placeholder="reasoning_effort, thinking.type"
            className="glass-input w-full text-sm"
          />
          <p className="text-xs text-text-tertiary mt-1">逗号分隔, 如: reasoning_effort, thinking.type</p>
        </div>

        {/* 操作按钮 */}
        <div className="flex justify-end gap-2 pt-2">
          <Button variant="ghost" onClick={onClose}>取消</Button>
          <Button variant="primary" onClick={handleSave} disabled={saving}>
            {saving ? '保存中...' : '保存'}
          </Button>
        </div>
      </div>
    </Modal>
  );
}
