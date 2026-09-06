# RAG 与通用会话覆盖实施记录

本功能依据会话确认的方案实施, RAG 只提供工具调用, 不内部调用 LLM 生成答案

## 完成条件

- [x] YAML 支持 llm/embed/rerank 模型引用, 运行时注入同步或异步客户端, 严格校验引用并处理更新与释放
- [x] 通用会话参数覆盖存 platform.db, 按会话与配置域隔离, 提供字段来源、恢复继承与并发控制
- [x] Chat 与 Edictum 均接通覆盖的读写、配置表单、运行时合并与安全更新
- [x] 覆盖记录与 RAG 会话数据接入删除、归档、恢复和分支生命周期
- [x] 目录按需创建, 列表查询不递归扫描会话目录, 大小统计采用缓存及独立刷新
- [x] 全局与会话知识库统一管理, 支持 session/global/session_global 检索范围和显式写入目标
- [x] 知识库绑定 embed, 支持文档导入、去重/替换、删除、来源、原子重建与模型身份校验
- [x] rag_search/rag_list/rag_ingest 同步和异步工具, 支持可选 rerank、排名融合、去重、预算及错误区分
- [x] 后端管理页和会话页共用 RAG 管理组件, 支持列表、文档、导入、重建及检索测试
- [x] 有意义的离线测试、前端测试与构建、相关回归检查和使用文档

## 配置约定

知识库: name, scope, embed, chunk_size=800, chunk_overlap=120, batch_size=32, duplicate_policy=skip

插件: db_scope=session, global_db_ids=[], session_db_ids=[], write_db_id="", rerank="", candidate_k=20,
top_k=5, similarity_threshold=null, rerank_min_score=null, rerank_failure_policy=fallback, max_result_chars=12000

覆盖优先级: schema 默认 < 全局插件配置 < Edictum 命名配置 < 当前会话覆盖

空值按 schema 校验, 删除键表示恢复继承, 不保存合并结果, 对象和数组按字段整体替换

## 实施说明

覆盖清空后保留空记录及修订号, 防止旧请求在删除再创建后通过版本校验; 未使用覆盖的会话不建记录

## 完成证据

| 要求 | 实现和验证 |
| --- | --- |
| 模型引用和客户端 | plugin_config.py / plugin_resources.py, test_plugin_resources.py 验证三种模型的同步与异步客户端构造及释放 |
| 复用覆盖模板 | session_overrides.py / plugin_settings.py, test_session_overrides.py 验证重启持久化、显式空值、继承、CAS、归档互斥 |
| Chat 与 Edictum 接入 | test_display_service.py / test_control_dispatch.py 覆盖 HTTP 读写及隔离, test_session_providers.py 验证下一轮更新和外部模型配置变更 |
| 会话生命周期 | storage/database.py / session_fork.py, RAG 测试验证私有库复制、引用替换、归档恢复和写入互斥 |
| 按需目录与列表 | test_storage_layout.py 验证覆盖不创建会话目录、仅显式刷新统计才扫描磁盘, 列表调用 session_size_snapshot 的缓存读取 |
| RAG 检索与导入 | test_rag_service.py 验证全局/会话/联合、去重、默认写入、预算、模型身份、重建失败、来源替换及文件清理 |
| 模型工具调用 | test_rag_service.py 在真实 SimpleSession / AsyncSimpleSession 安装并调用三个工具, 检查不调用会话 LLM 生成答案 |
| 共用管理界面 | RagManager / PluginConfigFields / SessionPluginSettingsModal, e2e/rag-settings.mjs 运行真实页面、验证请求与字段行为并截图检查 |
| 文档 | docs/rag-and-session-overrides.md 包含所有配置项、YAML 约定、覆写复用示例、管理入口和生命周期 |

验证使用隔离的临时数据及离线模型替身, 不向真实模型服务发送测试资料

## 最终验证结果

- 后端: `python -m pytest tests/unit -q --disable-warnings --tb=short`, 1204 passed, 7 skipped
- 跳过项: 4 项需要显式启用的模型集成测试, 1 项缺少 reportlab 的 PDF 样本生成测试, 2 项当前 Windows 缺少符号链接权限的测试
- 前端: `npm test`, 14 个测试文件共 52 项通过
- 浏览器: `node e2e/rag-settings.mjs` 通过, 已检查管理页面和覆盖表单截图
- 构建: `npm run build` 通过
- 补丁: 使用仓库当前行尾设置执行 `git diff --check` 通过

真实模型服务未发送测试请求, 模型网络调用的业务结果使用离线替身验证
