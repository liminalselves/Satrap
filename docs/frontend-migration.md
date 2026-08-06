# 前端迁移指南

从 Streamlit 迁移到 React 的完整指南。

## 概述

Satrap 前端已从 Streamlit 迁移到 React + TypeScript + Vite，带来以下改进:

- **性能**: 虚拟 DOM、代码分割、懒加载
- **用户体验**: 实时更新、流畅动画、响应式设计
- **可维护性**: TypeScript 类型安全、组件化架构
- **可扩展性**: 现代前端生态、丰富的组件库

## 快速开始

### 开发模式

```bash
# 终端 1: 启动后端
python -m satrap.main run

# 终端 2: 启动前端开发服务器
cd satrap-ui
npm install
npm run dev
```

访问 `http://localhost:5173` (前端开发服务器)

### 生产模式

```bash
# 构建前端
cd satrap-ui
npm run build

# 启动后端 (自动托管前端)
python -m satrap.main run
```

访问 `http://127.0.0.1:19870`

## 功能对照

| Streamlit 页面 | React 页面 | 状态 |
|---------------|-----------|------|
| `admin.py` (欢迎页) | `Dashboard` | 已合并 |
| `01_dashboard.py` | `Dashboard` | 完成 |
| `02_model_config.py` | `Models` | 完成 |
| `03_session_management.py` | `Sessions` | 完成 |
| `04_platform_status.py` | `Platforms` | 完成 |
| `05_log_monitor.py` | `Logs` | 完成 |
| `06_settings.py` | `Settings` | 完成 |
| `07_checkpoint_management.py` | `Checkpoints` | 完成 |
| `08_user_management.py` | `Users` | 完成 |

## 架构变化

### 原 Streamlit 架构

```
Streamlit Server (Python)
    ↓
Session State (内存)
    ↓
本地文件系统 (配置/数据库)
```

### 新 React 架构

```
React SPA (浏览器)
    ↓ HTTP/WebSocket
Backend API Server (Python asyncio)
    ↓
本地文件系统 (配置/数据库)
```

## API 集成

前端通过 RESTful API 与后端通信:

```typescript
// src/api/client.ts
const apiClient = axios.create({
  baseURL: 'http://127.0.0.1:19870',
  timeout: 10000,
});
```

### 可用端点

| 端点 | 方法 | 说明 |
|-----|------|------|
| `/api/health` | GET | 后端健康状态 |
| `/api/config/reload` | POST | 重载配置 |
| `/api/shutdown` | POST | 关闭后端 |
| `/api/config/models` | GET/POST/PATCH/DELETE | 模型配置 |
| `/api/config/session-classes` | GET/POST/PUT/DELETE | 会话类 |
| `/api/users` | GET | 用户列表 |
| `/api/user/*` | POST | 用户操作 |
| `/api/checkpoints` | GET | 检查点列表 |
| `/api/checkpoint/*` | POST | 检查点操作 |

## 状态管理

使用 Zustand 进行状态管理:

```typescript
// src/stores/useBackendStore.ts
export const useBackendStore = create<BackendState>((set) => ({
  health: null,
  loading: false,
  refreshHealth: async () => {
    // ...
  },
}));
```

## 样式系统

采用玻璃拟态扁平化设计:

```css
/* 玻璃效果 */
.glass {
  background: rgba(255, 255, 255, 0.03);
  backdrop-filter: blur(12px);
  border: 1px solid rgba(255, 255, 255, 0.08);
}

/* 扁平化卡片 */
.glass-card {
  @apply glass rounded-xl p-6;
  transition: all 200ms ease;
}
```

## 部署

### 构建

```bash
cd satrap-ui
npm run build
```

产物输出到 `satrap-ui/dist/`

### 后端托管

后端自动检测并托管 `satrap-ui/dist/` 目录:

```python
# satrap/core/backend/http_api.py
STATIC_DIR = Path(__file__).parent.parent.parent.parent / "satrap-ui" / "dist"
```

### 静态文件服务

- 所有非 `/api/` 路径返回 `index.html` (SPA 路由)
- 静态资源 (JS/CSS/图片) 直接服务
- 支持缓存头优化

## 故障排除

### 前端无法连接后端

1. 确认后端已启动: `python -m satrap.main run`
2. 检查后端地址: 默认 `http://127.0.0.1:19870`
3. 查看浏览器控制台网络请求

### 构建失败

```bash
# 清除缓存重新安装
rm -rf node_modules package-lock.json
npm install
npm run build
```

### 样式不生效

确认 Tailwind CSS 已正确配置:

```bash
# 检查 tailwind.config.js
# 检查 postcss.config.js
# 重新构建
npm run build
```

## 回滚到 Streamlit

如需回滚到 Streamlit 前端:

```bash
# 直接运行 Streamlit
streamlit run satrap/admin.py
```

Streamlit 代码仍保留在 `satrap/pages/` 目录。

## 后续计划

- [ ] WebSocket 实时日志推送
- [ ] 用户认证与权限
- [ ] 多主题支持
- [ ] 移动端适配
- [ ] PWA 支持
