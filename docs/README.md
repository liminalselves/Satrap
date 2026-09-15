# Satrap 文档

这组文档按照“先跑起来, 再扩展”的顺序组织, 按子目录分类。第一次使用建议从快速开始看起; 如果已经在集成后端或平台适配器, 可以直接跳到对应分类。

## 目录索引

### getting-started/ 入门

- [快速开始](getting-started/quick-start.md): 安装, 文本与图片调用, 流式输出, 一个最小工具
- [配置说明](getting-started/configuration.md): `config.yaml`, 模型配置, Session 类配置和环境变量
- [常见问题](getting-started/faq.md): 常见报错, 配置排查, 图片与上下文问题

### core/ 核心框架

- [核心 API](core/core-api.md): LLM, 流式事件, ContextManager, Embedding, ReRank 和消息组件
- [工具与 Agent](core/tools-and-agent.md): Tool / AsyncTool, ToolsManager, workflow 和 sub-agent
- [检查点](core/checkpoint.md): 快照, 回滚, 重试, 分支与 HTTP / 面板管理
- [多模态输入](core/multimodal-input.md): 图像, 视频与 PDF 页面输入的模型能力配置与用法
- [Session, 后端与 CLI](core/sessions-backend-cli.md): Session 写法, 后端生命周期, CLI 常用命令
- [数据目录布局](core/data-layout.md): 数据目录结构, 数据库与文件存储位置

### edictum/ 简易 Agent 体系

- [edictum 体系总览](edictum/README.md): 模块地图, 处理器链, 插件生命周期, 与 SessionManager 的衔接
- [SimpleSession 用法](edictum/simple-session.md): SimpleSession / AsyncSimpleSession, 处理器, MCP 接入与流式
- [插件系统](edictum/plugin-system.md): 目录插件结构, meta.yaml 能力声明, 能力收集约定, 双层启停与错误处理
- [会话 Provider](edictum/session-providers.md): SessionProvider 契约, 命名会话定义与 Edictum 冷配置

### plugins/ 内置插件与扩展

- [satrap_coding 插件](plugins/satrap-coding-plugin.md): 简易 Coding Agent, 文件工具 / 沙箱 / 记忆 / 子代理 / 目标与计划模式
- [base_take 插件](plugins/base-take-plugin.md): 长期记忆插件, 全局 / 项目分层与注入机制
- [RAG 与会话覆盖](plugins/rag-and-session-overrides.md): RAG 插件使用流程, 知识库参数与文档管理, Embedding 绑定与索引重建, 检索参数, 会话级插件配置覆盖
- [扩展模块](plugins/extensions.md): `satrap.expend` 工具集: 搜索, 网页抓取, 代码沙箱, RAG, 长期记忆和 sub-agent

### execution/ 执行引擎与恢复

- [执行引擎架构](execution/engine.md): 状态转换 / 驱动器 / 持久化三层结构与 recoverable 契约
- [任务执行记录与恢复](execution/task-recovery.md): 可恢复 Agent 循环的用法, 未知副作用处理, Chat 续跑与 retry / fork 的区别

### platform/ 平台接入

- [平台接入](platform/platforms.md): Misskey, OneBot / aiocqhttp, 多平台路由和适配器扩展

### ui/ 展示层与前端

- [聊天展示层](ui/chat-display.md): 面向前端聊天页的独立实时服务 (录制 / 会话编排 / WebSocket / HTTP API)
- [UI 设计系统](ui/ui-design-system.md): 前端设计规范与组件约定

### development/ 开发规范

- [开发规范](development/development-guidelines.md): 注释规范, 静态类型检查规范与门禁
- [测试说明](development/testing.md): 测试目录, 离线测试, 集成测试和手动 Demo

### archive/ 历史记录

实施 / 迁移 / 清理 / 优化的过程记录, 只反映当时状态, 不作为现行参考: RAG 实施记录, 恢复机制验收与存储优化记录, 向量存储迁移, 前端迁移, 死代码清理。

## 阅读路径

1. 跑起来: [快速开始](getting-started/quick-start.md) → [配置说明](getting-started/configuration.md)
2. 用核心 API 开发: [核心 API](core/core-api.md) → [工具与 Agent](core/tools-and-agent.md), 需要现成能力时看 [扩展模块](plugins/extensions.md)
3. 接入后端: [Session, 后端与 CLI](core/sessions-backend-cli.md), 平台适配看 [平台接入](platform/platforms.md)
4. 用 edictum 体系: [edictum 体系总览](edictum/README.md) → [SimpleSession 用法](edictum/simple-session.md) → [插件系统](edictum/plugin-system.md)
5. 深入可靠性: [检查点](core/checkpoint.md) → [任务执行记录与恢复](execution/task-recovery.md)
6. 前端与展示: [聊天展示层](ui/chat-display.md) → [UI 设计系统](ui/ui-design-system.md)

## 项目结构速览

```text
satrap/
  core/
    APICall/          # LLM, Embedding, ReRank 调用封装
    backend/          # BackendManager, HTTP API, WebSocket 与 React 静态资源托管
    components/       # 跨平台消息组件
    config/           # 配置加载与服务定位
    framework/        # Workflow, Session, SessionManager, 配置管理; Base/execution/ 为执行引擎
    platform/         # 平台适配器基类和内置适配器
    pipeline/         # 调度与限流
    state/            # 状态检查点: 快照 / 变更 / 注册 / 存储
    storage/          # SQLite 数据库与数据目录维护
    utils/            # 上下文, 工具, 多模态, sandbox, db 路径等工具模块
  display/            # 聊天展示层: DisplayRecorder / ChatService / 独立 HTTP+WS 服务 / 插件注册
  edictum/            # 简易 Agent 体系: SimpleSession / 处理器链 / 插件运行时
  expend/             # 可选扩展: tools/ 工具类, command/, skills/, plugins/ 内置插件
  cli/                # satrap 命令行实现
  api/                # 面向 HTTP 的 API 层
tests/                # unit, integration 和 manual 测试
satrap-ui/            # React 管理面板 + 聊天页 (前端)
docs/                 # 项目文档
```

## 推荐工作流

先用 [快速开始](getting-started/quick-start.md) 验证模型调用, 再用 [工具与 Agent](core/tools-and-agent.md) 增加工具调用能力。需要开箱即用的搜索, RAG 或代码执行能力时看 [扩展模块](plugins/extensions.md) 和 [内置插件](plugins/satrap-coding-plugin.md)。如果要接入真实聊天平台, 先写一个 `Session` 子类, 通过 [Session, 后端与 CLI](core/sessions-backend-cli.md) 注册到后端, 最后按 [平台接入](platform/platforms.md) 配置 Misskey 或 OneBot。构建自己的 Agent 应用时, 优先看 [edictum 体系总览](edictum/README.md)。
