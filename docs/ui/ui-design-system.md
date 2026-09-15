# Satrap UI 设计系统

玻璃拟态扁平化 (Glass-Flat) 设计系统规范。

## 设计原则

### 1. 扁平化基础

- 无拟物阴影 (no skeuomorphic shadows)
- 无渐变文字 (no gradient text)
- 简洁几何形状 (clean geometric shapes)
- 明确的边界 (clear boundaries)

### 2. 玻璃质感

- 半透明背景 (semi-transparent backgrounds)
- 背景模糊 (backdrop blur)
- 细腻边框 (subtle borders)
- 层次感通过透明度 (hierarchy through opacity)

### 3. 微交互

- 150-300ms 过渡动画
- 悬停状态反馈
- 点击缩放效果
- 加载状态指示

## 色彩系统

### 基础色板

主题为 Fluent 2 风格, 暗色 / 浅色两套 (`:root` 暗色为默认, 浅色覆盖同组变量)。暗色主要变量:

```css
/* 背景层次 */
--color-bg-base: #0d0d0f;
--color-bg-layer1: rgba(30, 30, 35, 0.6);
--color-bg-layer2: rgba(40, 40, 48, 0.5);
--color-bg-layer3: rgba(50, 50, 60, 0.4);

/* 玻璃材质 */
--glass-bg: rgba(255, 255, 255, 0.03);
--glass-bg-hover: rgba(255, 255, 255, 0.05);
--glass-bg-active: rgba(255, 255, 255, 0.08);
--glass-border: rgba(255, 255, 255, 0.06);
--glass-border-hover: rgba(255, 255, 255, 0.08);

/* 强调色 */
--color-accent: #0078d4;
--color-accent-light: #2b88d8;
--color-accent-dark: #106ebe;
--color-accent-subtle: rgba(0, 120, 212, 0.15);
--color-success: #13a10e;
--color-warning: #ff8c00;
--color-error: #e81123;

/* 文字 */
--color-text-primary: #ffffff;
--color-text-secondary: rgba(255, 255, 255, 0.75);
--color-text-tertiary: rgba(255, 255, 255, 0.5);
```

### 语义化色彩

| 用途 | 变量 | 值 |
|-----|------|-----|
| 主要操作 | `--color-accent` | `#0078d4` |
| 成功状态 | `--color-success` | `#13a10e` |
| 警告状态 | `--color-warning` | `#ff8c00` |
| 错误状态 | `--color-error` | `#e81123` |

## 组件规范

### 按钮 (Button)

```css
/* 基础按钮 */
.glass-button {
  @apply glass rounded-lg px-4 py-2 text-sm font-medium;
  @apply transition-all duration-200;
  @apply hover:bg-bg-glass-hover;
  @apply active:scale-[0.98];
}

/* 主要按钮 */
.glass-button-primary {
  @apply bg-accent border-accent text-white;
  @apply hover:bg-accent-hover;
}
```

**特征:**
- 圆角: 8px (`rounded-lg`)
- 无阴影
- 悬停时背景加深
- 点击时轻微缩放 (0.98)

### 输入框 (Input)

```css
.glass-input {
  @apply glass rounded-lg px-4 py-2 text-sm;
  @apply placeholder:text-text-tertiary;
  @apply focus:outline-none focus:border-accent focus:ring-1 focus:ring-accent;
}
```

**特征:**
- 玻璃背景
- 聚焦时边框高亮
- 无内阴影

### 卡片 (Card)

```css
.glass-card {
  @apply glass rounded-xl p-6;
  transition: background-color 200ms ease, border-color 200ms ease;
}

.glass-card:hover {
  background: var(--color-bg-glass-hover);
  border-color: var(--color-border-glass-strong);
}
```

**特征:**
- 圆角: 12px (`rounded-xl`)
- 玻璃背景
- 无边距阴影
- 悬停时背景加深

### 模态框 (Modal)

`components/ui/Modal.tsx` 渲染 `.glass-overlay` + `.glass-modal`, 支持嵌套弹窗栈 (只有栈顶响应 Escape 与遮罩点击, z-index 递增):

```css
.glass-overlay {
  position: fixed; inset: 0;
  background: rgba(0, 0, 0, 0.5);
  backdrop-filter: blur(8px);
  animation: fadeIn 200ms ease;
}

.glass-modal {
  background: var(--glass-bg);
  backdrop-filter: blur(40px) saturate(180%);
  border: 1px solid var(--glass-border);
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-modal);
  animation: slideIn 250ms cubic-bezier(0.1, 0.9, 0.2, 1);
  padding: 24px;
}
```

尺寸档位 (`size` prop, 默认 `md`):

| size | 最大宽度 | 用途 |
| --- | --- | --- |
| `sm` | `max-w-sm` (384px) | 小型确认对话框 |
| `md` | `max-w-md` (448px) | 常规表单 |
| `lg` | `max-w-lg` (512px) | 较大内容 (如 RAG 管理) |
| `xl` | `max-w-xl` (576px) | 宽表单 |
| `2xl` | `max-w-5xl` (1024px) | 超宽内容 |
| `3xl` | `max-w-3xl` (768px) | 左右分栏布局 (如对话设置) |

**特征:**
- 全屏毛玻璃遮罩
- 居中玻璃卡片, `max-h-[85vh]`, 内容区独立滚动
- 滑入动画 (slideIn)

### 表格 (Table)

```css
.glass-table-row {
  @apply transition-colors duration-150;
}

.glass-table-row:hover {
  @apply bg-bg-glass-hover;
}
```

**特征:**
- 透明行背景
- 悬停时玻璃高亮
- 细边框分隔

### 标签页 (Tabs)

`components/ui/Tabs.tsx` 提供 `Tabs` / `TabsList` / `TabsTrigger` / `TabsContent` 组合式组件 (非 CSS 类), 以内联 Tailwind 类实现:

```tsx
<Tabs defaultValue="general">
  <TabsList>           {/* flex gap-1 border-b border-border-glass */}
    <TabsTrigger value="general">通用</TabsTrigger>
  </TabsList>
  <TabsContent value="general">...</TabsContent>   {/* animate-fade-in */}
</Tabs>
```

**特征:**
- 底部边框指示器
- 无背景色块
- 内容切换淡入

### 导航项 (glass-nav-item)

侧边栏与管理弹窗分类导航共用 `.glass-nav-item` (globals.css), 提供图标 + 文字的行式导航, 悬停有径向反光效果; 颜色变体 `nav-accent` / `nav-purple` / `nav-teal` / `nav-pink` / `nav-orange` / `nav-green` 决定悬停与 `.active` 选中态的配色:

```tsx
<button className={cn('glass-nav-item nav-accent', isActive && 'active')}>
  <Icon className="h-4 w-4" /> 对话
</button>
```

**变体:**
- `.nav-fill`: 撑满整列宽度 (`width: calc(100% - 20px)`) — `button` 元素默认收缩到内容宽度, 在设置弹窗侧栏等需要等宽对齐的场景必须加此变体

### 其他 UI 原语

`components/ui/` 还包含:

| 组件 | 说明 |
| --- | --- |
| `Select` | 自定义下拉选择 (`options: {value, label}[]` 驱动, portal 渲染浮层, 非原生 select) |
| `Badge` | 状态徽标 |
| `Toast` | 全局轻提示 (`toast(type, message, duration?)`, type: `success` / `error` / `warning` / `info`) |
| `CopyButton` | 复制到剪贴板按钮 (带已复制状态反馈) |

## 动画

### 淡入 (Fade In)

```css
@keyframes fadeIn {
  from { opacity: 0; }
  to { opacity: 1; }
}

.animate-fade-in {
  animation: fadeIn 200ms ease;
}
```

### 滑入 (Slide In)

```css
@keyframes slideIn {
  from { opacity: 0; transform: translateY(-12px) scale(0.96); }
  to { opacity: 1; transform: translateY(0) scale(1); }
}

.animate-slide-in {
  animation: slideIn 250ms cubic-bezier(0.1, 0.9, 0.2, 1);
}
```

### 脉冲 (Pulse)

```css
@keyframes pulse {
  0%, 100% { transform: scale(1); opacity: 0.3; }
  50% { transform: scale(1.5); opacity: 0; }
}

.animate-pulse {
  animation: pulse 2s ease-in-out infinite;
}
```

## 图标

使用 [Lucide React](https://lucide.dev/) 图标库:

```tsx
import { Plus, Edit2, Trash2 } from 'lucide-react';

<Plus className="h-4 w-4" />
```

**尺寸规范:**
- 小: `h-3 w-3` (12px)
- 中: `h-4 w-4` (16px)
- 大: `h-5 w-5` (20px)
- 特大: `h-6 w-6` (24px)

## 间距

使用 Tailwind CSS 间距系统:

| 类名 | 值 | 用途 |
|-----|-----|------|
| `p-2` | 8px | 紧凑内边距 |
| `p-4` | 16px | 标准内边距 |
| `p-6` | 24px | 卡片内边距 |
| `gap-2` | 8px | 紧凑间距 |
| `gap-4` | 16px | 标准间距 |
| `space-y-4` | 16px | 垂直间距 |

## 响应式

### 断点

| 断点 | 宽度 | 设备 |
|-----|------|------|
| `sm` | 640px | 手机横屏 |
| `md` | 768px | 平板 |
| `lg` | 1024px | 笔记本 |
| `xl` | 1280px | 桌面 |

### 示例

```tsx
<div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
  {/* 响应式网格 */}
</div>
```

## 无障碍

### 焦点状态

所有交互元素必须有焦点状态:

```css
.focus-visible {
  @apply outline-none ring-2 ring-accent/50;
}
```

### 颜色对比度

- 主要文字: 对比度 ≥ 4.5:1
- 次要文字: 对比度 ≥ 3:1
- 交互元素: 对比度 ≥ 3:1

### 语义化 HTML

```tsx
<button aria-label="删除配置">
  <Trash2 className="h-4 w-4" />
</button>
```

## 使用示例

### 页面布局

```tsx
<div className="space-y-6">
  <div className="flex items-center justify-between">
    <h1 className="text-2xl font-bold text-text-primary">页面标题</h1>
    <Button variant="primary">操作</Button>
  </div>
  
  <Card>
    <p>内容</p>
  </Card>
</div>
```

### 表单

```tsx
<form className="space-y-4">
  <div>
    <label className="block text-sm font-medium text-text-secondary mb-1">
      标签
    </label>
    <Input placeholder="输入..." />
  </div>
  <Button type="submit" variant="primary">提交</Button>
</form>
```

### 数据展示

```tsx
<Card>
  <h3 className="text-lg font-semibold text-text-primary mb-4">标题</h3>
  <Table>
    <TableHeader>
      <TableRow>
        <TableHead>列1</TableHead>
        <TableHead>列2</TableHead>
      </TableRow>
    </TableHeader>
    <TableBody>
      <TableRow>
        <TableCell>数据1</TableCell>
        <TableCell>数据2</TableCell>
      </TableRow>
    </TableBody>
  </Table>
</Card>
```
