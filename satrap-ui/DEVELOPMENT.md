# Satrap UI 前端开发文档

## 目录

- [技术栈](#技术栈)
- [项目结构](#项目结构)
- [设计语言](#设计语言)
  - [Fluent 2 设计系统](#fluent-2-设计系统)
  - [玻璃拟态 (Glassmorphism)](#玻璃拟态-glassmorphism)
  - [色彩系统](#色彩系统)
  - [圆角规范](#圆角规范)
  - [阴影系统](#阴影系统)
- [组件库](#组件库)
  - [基础 UI 组件](#基础-ui-组件)
  - [通用业务组件](#通用业务组件)
- [开发注意事项](#开发注意事项)
- [性能优化规范](#性能优化规范)

---

## 技术栈

| 技术 | 版本 | 用途 |
|------|------|------|
| React | 18.x | UI 框架 |
| TypeScript | 5.x | 类型系统 |
| Vite | 5.x | 构建工具 |
| Tailwind CSS | 3.x | 样式框架 |
| Zustand | 4.x | 状态管理 |
| React Router | 6.x | 路由管理 |
| Lucide React | - | 图标库 |

---

## 项目结构

```
satrap-ui/
├── src/
│   ├── components/
│   │   ├── ui/           # 基础 UI 组件（Button, Card, Input 等）
│   │   ├── common/       # 通用业务组件（PageHeader, DataTable 等）
│   │   └── layout/       # 布局组件（Sidebar, Header 等）
│   ├── pages/            # 页面组件
│   │   ├── Dashboard/
│   │   ├── Sessions/
│   │   ├── Models/
│   │   └── ...
│   ├── hooks/            # 自定义 Hooks
│   ├── stores/           # Zustand 状态管理
│   ├── services/         # API 服务层
│   ├── styles/           # 全局样式
│   ├── types/            # TypeScript 类型定义
│   └── utils/            # 工具函数
├── scripts/              # 启动/停止脚本
└── public/               # 静态资源
```

---

## 设计语言

### Fluent 2 设计系统

本项目采用微软 **Fluent 2** 设计语言，核心理念包括：

1. **层次感 (Depth)** - 通过阴影和透明度营造视觉层次
2. **材质感 (Material)** - 玻璃拟态效果模拟真实材质
3. **动效 (Motion)** - 流畅的过渡动画
4. **适应性 (Adaptive)** - 支持暗色/亮色双主题

### 玻璃拟态 (Glassmorphism)

玻璃拟态是本项目的核心视觉效果，通过 CSS 变量实现：

```css
/* 基础玻璃材质 */
--glass-bg: rgba(255, 255, 255, 0.03);
--glass-bg-hover: rgba(255, 255, 255, 0.05);
--glass-border: rgba(255, 255, 255, 0.06);

/* 使用方式 */
.glass-card {
  background: var(--glass-bg);
  border: 1px solid var(--glass-border);
  backdrop-filter: blur(20px);
}
```

**关键特性：**
- `backdrop-filter: blur(20px)` - 背景模糊效果
- 半透明背景色
- 微妙的边框高光
- 鼠标跟随反光效果（通过 `useGlassReflect` Hook）

### 色彩系统

#### 主色调

| 变量名 | 暗色模式 | 亮色模式 | 用途 |
|--------|----------|----------|------|
| `--color-accent` | `#0078d4` | `#0078d4` | 主品牌色 |
| `--color-accent-light` | `#2b88d8` | `#106ebe` | 悬停状态 |
| `--color-accent-dark` | `#106ebe` | `#005a9e` | 按下状态 |

#### 辅助色

| 色彩 | 变量前缀 | 用途 |
|------|----------|------|
| 紫色 | `--color-purple` | 次要操作 |
| 青色 | `--color-teal` | 信息提示 |
| 粉色 | `--color-pink` | 特殊标记 |
| 橙色 | `--color-orange` | 警告提示 |
| 绿色 | `--color-green` | 成功状态 |

#### 状态色

| 状态 | 暗色模式 | 亮色模式 |
|------|----------|----------|
| Success | `#13a10e` | `#107c10` |
| Warning | `#ff8c00` | `#f7630c` |
| Error | `#e81123` | `#d13438` |
| Info | `#2b88d8` | `#0078d4` |

#### 文字颜色

```css
--color-text-primary: #ffffff;           /* 主要文字 */
--color-text-secondary: rgba(255, 255, 255, 0.75);  /* 次要文字 */
--color-text-tertiary: rgba(255, 255, 255, 0.5);    /* 辅助文字 */
--color-text-disabled: rgba(255, 255, 255, 0.3);    /* 禁用文字 */
```

#### 染色玻璃变体

每种颜色都有对应的玻璃变体，用于卡片和组件：

```css
/* 蓝色染色玻璃 */
--glass-accent-bg: rgba(0, 120, 212, 0.08);
--glass-accent-border: rgba(0, 120, 212, 0.15);
--glass-accent-glow: rgba(0, 120, 212, 0.12);
```

### 圆角规范

| 变量名 | 值 | 用途 |
|--------|-----|------|
| `--radius-sm` | 6px | 小元素（按钮、输入框） |
| `--radius-md` | 10px | 中等元素（卡片、模态框） |
| `--radius-lg` | 14px | 大元素（面板、容器） |
| `--radius-xl` | 20px | 特殊元素（主要卡片） |

**使用规范：**
- 按钮、输入框：使用 `rounded-md` (10px)
- 卡片、模态框：使用 `rounded-lg` (14px)
- 主要容器：使用 `rounded-xl` (20px)

### 阴影系统

```css
/* 玻璃阴影 */
--shadow-glass: 0 8px 32px rgba(0, 0, 0, 0.3);
--shadow-glass-hover: 0 12px 40px rgba(0, 0, 0, 0.4);

/* 发光效果 */
--shadow-glow-accent: 0 0 30px var(--glass-accent-glow);
```

---

## 组件库

### 基础 UI 组件

位于 `src/components/ui/`，提供最基础的 UI 元素：

| 组件 | 用途 | 主要 Props |
|------|------|------------|
| `Button` | 按钮 | `variant`, `size`, `loading`, `disabled` |
| `Card` | 玻璃卡片 | `variant`, `interactive` |
| `Input` | 输入框 | `type`, `placeholder`, `error` |
| `Select` | 下拉选择 | `options`, `value`, `onChange` |
| `Modal` | 模态框 | `open`, `onClose`, `title` |
| `Table` | 表格 | 配合 `TableHeader`, `TableBody` 等 |
| `Tabs` | 标签页 | `value`, `onValueChange` |
| `Badge` | 徽章 | `variant`, `size` |
| `Toast` | 消息提示 | 通过 `useToast` Hook 使用 |

#### Card 组件示例

```tsx
import { Card } from '@/components/ui/Card';

// 默认卡片
<Card>内容</Card>

// 染色卡片
<Card variant="accent">蓝色染色卡片</Card>
<Card variant="purple">紫色染色卡片</Card>

// 可交互卡片（有悬停效果）
<Card interactive>可点击卡片</Card>
```

### 通用业务组件

位于 `src/components/common/`，提供可复用的业务组件：

| 组件 | 用途 |
|------|------|
| `PageHeader` | 页面标题 + 描述 + 操作按钮 |
| `StatCard` / `StatCardGrid` | 统计卡片和网格布局 |
| `DataTable` | 通用数据表格（泛型） |
| `FormModal` | 通用表单模态框 |
| `EmptyState` | 空状态提示 |
| `ActionButtons` | 操作按钮组 |
| `AlertCard` | 警告/提示卡片 |

#### PageHeader 示例

```tsx
import { PageHeader } from '@/components/common';

<PageHeader
  title="会话管理"
  description="管理系统中的所有会话"
  actions={
    <Button onClick={handleCreate}>
      <Plus className="h-4 w-4" /> 新建会话
    </Button>
  }
/>
```

#### StatCard 示例

```tsx
import { StatCard, StatCardGrid } from '@/components/common';

<StatCardGrid>
  <StatCard
    variant="accent"
    icon={<Server className="h-6 w-6" />}
    label="后端状态"
    value="运行中"
  />
  <StatCard
    variant="purple"
    icon={<Database className="h-6 w-6" />}
    label="会话数"
    value={42}
  />
</StatCardGrid>
```

#### DataTable 示例

```tsx
import { DataTable, Column } from '@/components/common';

interface User {
  id: string;
  name: string;
  email: string;
}

const columns: Column<User>[] = [
  { key: 'name', title: '姓名' },
  { key: 'email', title: '邮箱' },
  {
    key: 'actions',
    title: '操作',
    render: (user) => (
      <Button size="sm" onClick={() => handleEdit(user)}>编辑</Button>
    ),
  },
];

<DataTable
  columns={columns}
  data={users}
  keyExtractor={(user) => user.id}
  emptyMessage="暂无用户数据"
/>
```

#### FormModal 示例

```tsx
import { FormModal, FormField } from '@/components/common';

const fields: FormField[] = [
  { key: 'name', label: '名称', type: 'text', required: true },
  { key: 'email', label: '邮箱', type: 'text' },
  { key: 'role', label: '角色', type: 'select', options: [
    { value: 'admin', label: '管理员' },
    { value: 'user', label: '普通用户' },
  ]},
  { key: 'bio', label: '简介', type: 'textarea', rows: 3 },
];

<FormModal
  open={isOpen}
  onClose={() => setIsOpen(false)}
  title="编辑用户"
  fields={fields}
  values={formValues}
  onChange={(key, value) => setFormValues(prev => ({ ...prev, [key]: value }))}
  onSubmit={handleSubmit}
  loading={isSubmitting}
/>
```

---

## 开发注意事项

### 1. 样式规范

**使用 CSS 变量而非硬编码颜色：**

```tsx
// ❌ 错误
<div style={{ color: '#0078d4' }}>

// ✅ 正确
<div className="text-accent">
```

**使用 Tailwind 工具类：**

```tsx
// ❌ 避免内联样式
<div style={{ padding: '24px', marginTop: '16px' }}>

// ✅ 使用 Tailwind
<div className="p-6 mt-4">
```

### 2. 组件使用规范

**优先使用通用组件：**

```tsx
// ❌ 重复造轮子
<div className="flex items-center justify-between mb-6">
  <h1 className="text-2xl font-bold">标题</h1>
  <Button>操作</Button>
</div>

// ✅ 使用 PageHeader
<PageHeader title="标题" actions={<Button>操作</Button>} />
```

**Card 组件自带玻璃反光效果，无需额外处理：**

```tsx
// Card 组件内部已集成 useGlassReflect，直接使用即可
<Card variant="accent">内容</Card>
```

### 3. 类型定义规范

**禁用 `any`，优先具体类型：**

```tsx
// ❌ 错误
const handleData = (data: any) => { ... }

// ✅ 正确
interface UserData {
  id: string;
  name: string;
}
const handleData = (data: UserData) => { ... }
```

**泛型组件需要明确类型参数：**

```tsx
// ✅ DataTable 使用泛型
<DataTable<User>
  columns={columns}
  data={users}
  keyExtractor={(user) => user.id}
/>
```

### 4. 状态管理规范

**使用 Zustand 进行全局状态管理：**

```tsx
// stores/useAppStore.ts
import { create } from 'zustand';

interface AppState {
  theme: 'dark' | 'light';
  setTheme: (theme: 'dark' | 'light') => void;
}

export const useAppStore = create<AppState>((set) => ({
  theme: 'dark',
  setTheme: (theme) => set({ theme }),
}));
```

### 5. API 调用规范

**使用统一的 API 服务层：**

```tsx
// services/api.ts
import { api } from '@/services/api';

// 获取数据
const fetchUsers = async () => {
  const response = await api.get('/users');
  return response.data;
};

// 提交数据
const createUser = async (data: CreateUserRequest) => {
  const response = await api.post('/users', data);
  return response.data;
};
```

### 6. 主题切换

**通过 `data-theme` 属性切换主题：**

```tsx
// 切换到亮色模式
document.documentElement.setAttribute('data-theme', 'light');

// 切换到暗色模式
document.documentElement.removeAttribute('data-theme');
```

---

## 性能优化规范

### 1. 使用 React.memo 包装纯展示组件

```tsx
import { memo } from 'react';

const UserCard = memo(function UserCard({ user }: { user: User }) {
  return <Card>{user.name}</Card>;
});
```

### 2. 使用 useMemo 缓存计算结果

```tsx
import { useMemo } from 'react';

const columns = useMemo<Column<User>[]>(() => [
  { key: 'name', title: '姓名' },
  { key: 'email', title: '邮箱' },
], []);
```

### 3. 使用 useCallback 缓存事件处理函数

```tsx
import { useCallback } from 'react';

const handleClick = useCallback((id: string) => {
  // 处理点击
}, []);
```

### 4. 避免不必要的重渲染

```tsx
// ❌ 每次渲染都创建新对象
<ChildComponent style={{ margin: 10 }} />

// ✅ 使用 useMemo 或提取常量
const style = { margin: 10 };
<ChildComponent style={style} />
```

### 5. 列表渲染使用 key

```tsx
// ✅ 使用唯一标识作为 key
{users.map((user) => (
  <UserCard key={user.id} user={user} />
))}
```

---

## 启动脚本

```bash
# 启动开发环境（控制服务 + 前端）
.\scripts\start-dev.bat

# 仅启动前端 UI
.\scripts\start-ui.bat

# 停止所有服务
.\scripts\stop-dev.bat
```

---

## 相关文档

- [Fluent 2 设计系统](https://fluent2.microsoft.design/)
- [Tailwind CSS 文档](https://tailwindcss.com/docs)
- [React 文档](https://react.dev/)
- [Zustand 文档](https://docs.pmnd.rs/zustand)
