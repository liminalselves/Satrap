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

- **单元测试 (Vitest)**: 与源码同目录的 `*.test.ts(x)` (api 客户端 / hooks / 工具函数 / Chat 组件), 运行 `npm test` (即 `vitest run`);
- **浏览器 e2e**: `satrap-ui/e2e/` 下的脚本经 Playwright (Chromium headless) 驱动真实 React 页面, HTTP / WebSocket 使用受控数据或临时后端: `chat-reconnect.mjs` (WS 重连 / 快照恢复 / 序号去重), `model-dimensions.mjs`, `rag-settings.mjs`, `rag-upload.mjs` (真实 HTTP 上传 + 文档解析 + SQLite / FAISS 检索全链路, 自带临时后端 `rag-upload-backend.py`)。`npm run test:e2e` 目前只跑 `chat-reconnect.mjs`, 其余用 `node e2e/<脚本名>.mjs` 单独运行。

两套测试都会自行拉起临时 Vite 服务, 无需预先启动前端或后端。
