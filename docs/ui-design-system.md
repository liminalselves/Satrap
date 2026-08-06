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

```css
/* 背景 */
--color-bg-base: #0f0f1a;              /* 深蓝黑基底 */
--color-bg-glass: rgba(255, 255, 255, 0.03);
--color-bg-glass-hover: rgba(255, 255, 255, 0.06);
--color-bg-glass-active: rgba(255, 255, 255, 0.08);

/* 边框 */
--color-border-glass: rgba(255, 255, 255, 0.08);
--color-border-glass-strong: rgba(255, 255, 255, 0.12);

/* 强调色 */
--color-accent: #6366f1;               /* 靛蓝 */
--color-accent-hover: #818cf8;
--color-success: #10b981;              /* 翠绿 */
--color-warning: #f59e0b;              /* 琥珀 */
--color-error: #ef4444;                /* 红色 */

/* 文字 */
--color-text-primary: #f8fafc;         /* 主要文字 */
--color-text-secondary: #94a3b8;       /* 次要文字 */
--color-text-tertiary: #64748b;        /* 辅助文字 */
```

### 语义化色彩

| 用途 | 变量 | 值 |
|-----|------|-----|
| 主要操作 | `--color-accent` | `#6366f1` |
| 成功状态 | `--color-success` | `#10b981` |
| 警告状态 | `--color-warning` | `#f59e0b` |
| 错误状态 | `--color-error` | `#ef4444` |

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

```css
.glass-overlay {
  @apply fixed inset-0 bg-black/60 backdrop-blur-sm;
}

.glass-modal {
  @apply glass-strong rounded-xl p-6;
  @apply animate-slide-up;
}
```

**特征:**
- 全屏毛玻璃遮罩
- 居中玻璃卡片
- 滑入动画

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

```css
/* 底部边框指示器 */
.tabs-trigger {
  @apply px-4 py-2 text-sm font-medium;
  @apply border-b-2 -mb-px;
}

.tabs-trigger.active {
  @apply text-accent border-accent;
}
```

**特征:**
- 底部边框指示器
- 无背景色块

## 动画

### 淡入 (Fade In)

```css
@keyframes fadeIn {
  from { opacity: 0; }
  to { opacity: 1; }
}

.animate-fade-in {
  animation: fadeIn 200ms ease-out;
}
```

### 滑入 (Slide Up)

```css
@keyframes slideUp {
  from {
    opacity: 0;
    transform: translateY(8px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

.animate-slide-up {
  animation: slideUp 200ms ease-out;
}
```

### 脉冲 (Pulse Subtle)

```css
@keyframes pulseSubtle {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.7; }
}

.animate-pulse-subtle {
  animation: pulseSubtle 2s ease-in-out infinite;
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
