# Issue #27 实施与验收记录

日期: 2026-10-25

方案: issue-27-logging-and-chat-model-plan-2026-10-25.md

状态: 实施中, 未完成整个方案, 未推送或部署

## A. 错误分类与失败反馈

- 模型 SDK 的同步/异步, 流式/非流式入口共用脱敏错误分类, 保留 HTTP 状态, 合法供应商错误码和可用请求标识
- 请求失败通过 error_info 传递, 工作流拒绝把失败当作正常回答提交; 流式 error 会立即结束本轮, 已展示片段仍可保存
- Chat 将失败摘要保存为回复版本元数据, 历史读取与 Fork 均保留; 平台反馈使用相同展示文本
- 会话限时使用 WARNING 和 session_timeout, 模型网络超时使用 network_timeout; 无法确定来自网络的内部 TimeoutError 使用 operation_timeout, 不冒充会话限时
- SDK 自动重试策略和现有 return_false 显式配置保持不变, 没有新增实例级 last_error

已执行:

- Windows Python 3.13.5: LLM protocol, workflow execution contract, display service, pipeline scheduler 四模块 265 passed
- 前端 TypeScript 检查通过, Vitest 322 passed
- A 批次 11 个变更生产模块 Pyright: 0 errors, 0 warnings; 差异检查通过
- 补查 tools_agent 后, 同步/异步临时 Agent 的非流式入口不再把模型失败转成固定成功文本; 与流式入口一致抛出带资料的 ModelCallError
- 补查后的 workflow execution contract: 87 passed, 包括四种同步/异步和流式/非流式临时 Agent 失败组合

## B. 日志改动前基线

Windows Python 3.13.5 单次抽样, 使用临时目录, 未访问用户日志:

- 内存已有 5000 条记录时获取最近 100 条, 200 次: 平均 0.01466 ms, P95 0.01570 ms
- 单进程旧 ManagedDailyHandler 写入 1000 条中文记录: 0.00960 秒, 约 104163 条/秒

该结果不包含网络和浏览器渲染, 不用于承诺真实部署吞吐; 新增追加协调后需对照复测

## 未完成范围

- B: 日志共享文件, 活跃保护, 独立增量接口, 前端来源与认证
- C: 整体默认模型, 会话切换与重试一致性, 实际模型版本资料
- 全范围验证, 浏览器验收和跨平台结果仍需后续记录
