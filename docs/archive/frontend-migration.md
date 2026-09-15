# React 前端迁移说明

Satrap 管理前端已经从 Streamlit 完整迁移到 React + TypeScript + Vite, 旧 Streamlit 入口与依赖已移除

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

# 终端 2: 启动独立控制服务, 用于配置与进程控制
python -m satrap.core.backend.control_server

# 终端 3: 启动前端开发服务器
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

# 终端 1: 启动控制服务
python -m satrap.core.backend.control_server

# 终端 2: 启动后端, 自动托管前端
python -m satrap.main run
```

访问 `http://127.0.0.1:19870`

## 功能对照

| Streamlit 页面 | React 页面 | 状态 |
|---------------|-----------|------|
| `admin.py` (欢迎页) | `Dashboard` | 已替换, 旧入口已移除 |
| `01_dashboard.py` | `Dashboard` | 已替换, 旧页面已移除 |
| `02_model_config.py` | `Models` | 已替换, 旧页面已移除 |
| `03_session_management.py` | `Sessions` | 已替换, 支持会话类发现、Edictum 命名冷配置和实例查看 |
| `04_platform_status.py` | `Platforms` | 已替换, 支持离线配置 CRUD 和运行时热加载 |
| `05_log_monitor.py` | `Logs` | 已替换, 支持历史行数、多级筛选和无丢失暂停 |
| `06_settings.py` | `Settings` | 已替换, 支持默认配置创建、校验和按需重启 |
| `07_checkpoint_management.py` | `Checkpoints` | 已替换, 支持回滚、重试、描述和血缘查看 |
| `08_user_management.py` | `Users` | 已替换, 旧页面已移除 |
| — (新增) | `Chat` | React 新增聊天页, 独立整页, 由聊天展示层服务 (19872) 提供, 见 [聊天展示层](../ui/chat-display.md) |
| — (新增) | `Rag` (知识库) | React 新增 RAG 知识库管理页, 列表 / 文档导入 / 配置 / 重建 / 检索测试, 与 Chat 页共用 `RagManager` 组件, 见 [RAG 与会话覆盖](../plugins/rag-and-session-overrides.md) |

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
| `/api/config/edictum/types` | GET | Edictum 可用类型与能力 |
| `/api/config/edictum/sessions` | GET/POST/PATCH/DELETE | Edictum 命名冷配置 |
| `/api/session/discovery` | GET | 扫描配置声明的 Session 目录 |
| `/api/session/discovery/directories` | POST | 创建配置声明的 Session 目录 |
| `/api/sessions` | GET/POST | 查看和创建会话实例 |
| `/api/users` | GET | 用户列表 |
| `/api/user/*` | POST | 用户操作 |
| `/api/checkpoints` | GET | 检查点列表 |
| `/api/checkpoint/*` | POST | 检查点操作 |
| `/ws/logs?lines=100` | WebSocket | 日志历史与实时推送, 行数范围 50–500 |

独立控制服务默认监听 `127.0.0.1:19871`, 提供 `/config`、`/config/default`、`/config/validate`、`/config/platforms`、`/config/models`、`/config/session-classes`、`/config/edictum`、`/config/session/discovery`、`/config/session/discovery/directories` 和后端启动、停止、重启接口。控制服务还提供 RAG 与会话插件配置的冷接口 (`/config/rag`、`/config/session-plugin-config`) 以及 Chat 历史冷管理接口 (`/chat/history/*`); 对应的在线接口由聊天服务 (19872) 的 `/api/chat/rag`、`/api/chat/session-plugin-config`、`/api/chat/history/*` 提供, 见 [聊天展示层](../ui/chat-display.md)。

模型配置、会话类配置和 Edictum 命名配置的管理请求由控制服务处理, 因此平台后端停止时仍可完成增删改查。会话扫描目录的创建和 Session 类扫描同样由控制服务处理; 冷扫描会导入扫描目录中的模块以识别 Session/AsyncSession 子类, 但不会创建运行时会话实例。运行时会话的列出与创建仍由平台后端负责。

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

## 后续计划

- [ ] 用户认证与权限
- [ ] 多主题支持
- [ ] 移动端适配
- [ ] PWA 支持
