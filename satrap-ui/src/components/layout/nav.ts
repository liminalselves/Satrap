import {
  LayoutDashboard, Cpu, Database, Bot, Puzzle, Globe, ShieldCheck, History, Boxes, ScrollText, Bug, Settings,
  type LucideIcon,
} from 'lucide-react';

export type NavTint = 'accent' | 'purple' | 'teal' | 'pink' | 'green' | 'orange';

export interface NavItem {
  path: string;
  label: string;
  icon: LucideIcon;
  /** 命令面板匹配用: 英文名与拼音首字母 */
  keywords: string;
}

export interface NavGroup {
  id: string;
  label: string;
  tint: NavTint;
  items: NavItem[];
}

/** 侧栏按领域分组, 每组固定一种染色 */
export const navGroups: NavGroup[] = [
  { id: 'overview', label: '概览', tint: 'accent', items: [
    { path: '/', label: '仪表盘', icon: LayoutDashboard, keywords: 'dashboard ybp' },
  ] },
  { id: 'intel', label: '智能', tint: 'purple', items: [
    { path: '/models', label: '模型配置', icon: Cpu, keywords: 'models llm mxpz' },
    { path: '/rag', label: '知识库', icon: Database, keywords: 'rag knowledge zsk' },
  ] },
  { id: 'orch', label: '编排', tint: 'teal', items: [
    { path: '/agents', label: 'Agent 配置', icon: Bot, keywords: 'agent session pz' },
    { path: '/plugins', label: '插件管理', icon: Puzzle, keywords: 'plugins cjgl' },
  ] },
  { id: 'platform', label: '平台', tint: 'pink', items: [
    { path: '/platforms', label: '平台状态', icon: Globe, keywords: 'platforms adapters ptzt' },
    { path: '/platforms/approvals', label: '审批', icon: ShieldCheck, keywords: 'approvals sp' },
  ] },
  { id: 'data', label: '数据', tint: 'green', items: [
    { path: '/conversations', label: '对话记录', icon: History, keywords: 'conversations history dhjl' },
    { path: '/conversations/instances', label: '会话实例', icon: Boxes, keywords: 'instances hhsl' },
  ] },
  { id: 'ops', label: '运维', tint: 'orange', items: [
    { path: '/logs', label: '日志监控', icon: ScrollText, keywords: 'logs rzjk' },
    { path: '/logs/debug', label: '调试日志', icon: Bug, keywords: 'debug trace tsrz' },
    { path: '/settings', label: '系统设置', icon: Settings, keywords: 'settings xtsz' },
  ] },
];

/** 当前路径命中的最长导航项 (子路由归属到父项) */
export function findNav(pathname: string): { group?: NavGroup; item?: NavItem } {
  let best: { group?: NavGroup; item?: NavItem; length: number } = { length: -1 };
  for (const group of navGroups) {
    for (const item of group.items) {
      const match = item.path === '/' ? pathname === '/' : pathname === item.path || pathname.startsWith(`${item.path}/`);
      if (match && item.path.length > best.length) best = { group, item, length: item.path.length };
    }
  }
  return { group: best.group, item: best.item };
}
