# Satrap UI - React 管理面板

基于 React 18 + TypeScript + Vite 构建的现代化管理面板，采用玻璃拟态扁平化设计风格。

## 技术栈

- **框架**: React 18 + TypeScript
- **构建工具**: Vite 5
- **样式**: Tailwind CSS 3
- **状态管理**: Zustand
- **路由**: React Router 6
- **HTTP 客户端**: Axios
- **图标**: Lucide React

## 开发

```bash
# 安装依赖
npm install

# 启动开发服务器 (端口 5173)
npm run dev

# 构建生产版本
npm run build

# 预览生产构建
npm run preview
```

## 项目结构

```
src/
├── api/              # API 客户端封装
│   ├── client.ts     # Axios 实例
│   ├── types.ts      # TypeScript 类型定义
│   └── ...           # 各模块 API
├── components/       # 组件
│   ├── ui/           # 基础 UI 组件
│   ├── layout/       # 布局组件
│   └── features/     # 功能组件
├── pages/            # 页面
│   ├── Dashboard/    # 仪表盘
│   ├── Models/       # 模型配置
│   ├── Sessions/     # 会话管理
│   ├── Platforms/    # 平台状态
│   ├── Logs/         # 日志监控
│   ├── Checkpoints/  # 检查点管理
│   ├── Users/        # 用户管理
│   └── Settings/     # 系统设置
├── stores/           # Zustand 状态管理
├── hooks/            # 自定义 Hooks
├── utils/            # 工具函数
└── styles/           # 全局样式
```

## 设计系统

### 玻璃拟态扁平化 (Glass-Flat)

- **扁平化基础**: 无拟物阴影、简洁几何形状
- **玻璃质感**: 半透明背景、backdrop-blur、细腻边框
- **层次通过透明度**: 而非阴影深度
- **微交互**: 150-300ms 过渡动画

### 色彩系统

```css
--color-bg-base: #0f0f1a;           /* 深蓝黑基底 */
--color-bg-glass: rgba(255, 255, 255, 0.03);
--color-accent: #6366f1;            /* 靛蓝 */
--color-success: #10b981;
--color-warning: #f59e0b;
--color-error: #ef4444;
```

## 后端集成

开发模式下，前端通过 Vite 代理访问后端 API:

```typescript
// vite.config.ts
server: {
  proxy: {
    '/api': 'http://127.0.0.1:19870',
    '/ws': 'ws://127.0.0.1:19870',
  },
}
```

生产模式下，后端直接托管前端构建产物:

```python
# satrap/core/backend/http_api.py
STATIC_DIR = Path(__file__).parent.parent.parent.parent / "satrap-ui" / "dist"
```

## 部署

1. 构建前端: `npm run build`
2. 启动后端: `python -m satrap.main run`
3. 访问: `http://127.0.0.1:19870`

## 浏览器支持

- Chrome 90+
- Firefox 88+
- Edge 90+
- Safari 14+
