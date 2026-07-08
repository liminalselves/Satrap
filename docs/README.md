# Satrap 文档

这组文档按照“先跑起来, 再扩展”的顺序组织。第一次使用建议从快速开始看起; 如果已经在集成后端或平台适配器, 可以直接跳到对应章节。

## 阅读路径

1. [快速开始](quick-start.md): 安装, 一次文本调用, 一次图片调用, 一个最小工具
2. [配置说明](configuration.md): `config.yaml`, 模型配置, Session 类配置和环境变量
3. [核心 API](core-api.md): LLM, ContextManager, Embedding, ReRank 和消息组件
4. [工具与 Agent](tools-and-agent.md): Tool / AsyncTool, ToolsManager, workflow 和 sub-agent
5. [扩展模块](extensions.md): 搜索, 网页抓取, 代码沙箱, RAG, 长期记忆和 sub-agent
6. [Session, 后端与 CLI](sessions-backend-cli.md): Session 写法, 后端生命周期, CLI 常用命令
7. [平台接入](platforms.md): Misskey, OneBot / aiocqhttp, 多平台路由和适配器扩展
8. [常见问题](faq.md): 常见报错, 配置排查, 图片与上下文问题

## 项目结构速览

```text
satrap/
  core/
    APICall/          # LLM, Embedding, ReRank 调用封装
    backend/          # BackendManager 和 HTTP API
    components/       # 跨平台消息组件
    framework/        # Workflow, Session, SessionManager, 配置管理
    platform/         # 平台适配器基类和内置适配器
    pipeline/         # 调度与限流
    utils/            # 上下文, 工具, 多模态, sandbox 等工具模块
  cli/                # satrap 命令行实现
  pages/              # Streamlit 管理面板页面
  expend/             # 可选扩展工具, 如搜索, RAG, mem0, sandbox
tests/                # 单元测试与示例脚本
docs/                 # 项目文档
```

## 推荐工作流

先用 [快速开始](quick-start.md) 验证模型调用, 再用 [工具与 Agent](tools-and-agent.md) 增加工具调用能力。需要开箱即用的搜索, RAG 或代码执行能力时看 [扩展模块](extensions.md)。如果要接入真实聊天平台, 先写一个 `Session` 子类, 通过 [Session, 后端与 CLI](sessions-backend-cli.md) 注册到后端, 最后按 [平台接入](platforms.md) 配置 Misskey 或 OneBot。
