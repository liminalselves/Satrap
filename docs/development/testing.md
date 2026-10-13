# 测试说明

测试按外部依赖和运行成本分为三层:

- `tests/unit/`: 以离线单元测试为主, 其中少量标记为 `integration` 的场景需要外部服务
- `tests/integration/`: 需要模型, 向量服务或其他外部配置的集成测试
- `tests/manual/`: 手动 Demo, 基准和调试脚本, 不由 pytest 自动收集

测试依赖安装:

```powershell
python -m pip install -r requirements-test.txt
```

## 常用命令

运行默认测试集:

```powershell
python -m pytest -q
```

默认集包含离线单元测试, 带有 `integration` 标记的测试会被标记为 skipped, 不会访问外部服务。只运行不带集成标记的测试可以使用:

```powershell
python -m pytest -q tests/unit
python -m pytest -q -m "not integration"
```

配置好所需环境变量后, 显式运行集成测试:

```powershell
python -m pytest -q --run-integration
python -m pytest -q tests/integration --run-integration
```

`--run-integration` 会允许测试访问真实模型或外部服务, 请确认 API key, base URL 和相关服务已经准备好。

## 手动 Demo

手动脚本不会被 pytest 自动收集。Agent Demo 的帮助信息可以直接查看:

```powershell
python tests/agent_demo.py --help
```

实际脚本位于 `tests/manual/agent_demo.py`。Demo 使用固定的 session ID 时, 启动阶段会清理旧上下文, 避免历史消息影响结果。

## 前端测试

前端 (`satrap-ui/`) 有单元测试与浏览器端到端测试两套, 均不访问用户服务:

- **单元测试 (Vitest)**: 与源码同目录的 `*.test.ts(x)`, 按域组织为 api 客户端, stores 与 `src/scenarios/` 下的集中场景模块 (服务契约 / 配置契约 / 浏览器运行环境 / 展示与性能), 运行 `npm test` (即 `vitest run`);
- **浏览器 e2e**: `satrap-ui/e2e/` 下的 14 个场景模块经 Playwright (Chromium headless) 驱动真实 React 页面, HTTP / WebSocket 使用受控数据或临时后端。`access-routing` (管理员 / 智能体路由 / 后端平台状态), `model-configuration` (ASR 模型 / 模型维度), `platform-configuration` (平台策略 / 手动唤醒 / 群轮询), `plugin-configuration` (插件 / 插件加载 / Edictum 设置 / 好友工具设置 / 主分支合并), `runtime-applications` (控制台插件应用 / 应用进度 / 会话级智能体与插件应用), `account-relations` (好友 / 账号请求), `group-configuration` (群 / 群聊插件), `group-data` (群聊媒体 / 摘要 / 记忆与提醒), `conversation-management` (对话 / 对话用户 / 平台归档), `chat-media-flows` (聊天历史媒体 / 聊天媒体 / 聊天分叉项目), `chat-recovery-flows` (WS 重连 / 检查点分支 / 恢复分页), `logging-flows` (日志保留 / 日志流), `startup-forms` (鉴权启动 / 表单必填约束), `knowledge-flows` (RAG 设置 / RAG 上传, 真实 HTTP 上传 + 文档解析 + SQLite / FAISS 检索全链路, 自带临时后端 `rag-upload-backend.py`)。

全部场景由总入口 `node e2e/run.mjs` (即 `npm run test:e2e:all`) 一次跑完, 复用同一个 Chromium 与 Vite 服务, 每个场景创建独立页面与请求夹具。单个场景可用 `node e2e/<模块名>.mjs --case <场景名>` 运行; `package.json` 中另有 `test:e2e:*` 别名对应常用场景, `npm run test:e2e` 只跑聊天重连场景。

两套测试都会自行拉起临时 Vite 服务, 无需预先启动前端或后端。

对话维护的浏览器回归使用 `npm run test:e2e:conversations`, 覆盖上下文与展示历史分别编辑、深链与旧路由、保存失败保留草稿、离开确认、删除与备份恢复、清空保留提示词及手机布局。后端对应的隔离测试位于 `tests/unit/test_conversation_storage_flows.py`, HTTP 和活动 Chat 路径分别由路由与展示服务测试覆盖, 不修改用户的真实对话数据。
