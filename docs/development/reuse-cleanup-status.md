# 公共实现与接口精简进度

范围: C1-C23 与 D1-D27. 功能缺陷 B 与风格项 E 不在本轮范围内. 原始 branch-audit-2026-10-12.md 保持不变

状态: C1-C23 与 D1-D27 共 50 项全部完成, 当前生产代码及相应验证已核对. 后端提交 c13e7ec, 前端提交 ab5c88e, 文档随本记录一同提交

## 删除与缩小接口

| 项目 | 内容 | 当前处置 |
|---|---|---|
| C1 | ManualWakeStore.pending_counts | 删除测试专用计数入口, 测试核验持久记录 |
| C2 | ReminderStore.due | 删除无调度消费者的筛选接口, 保留 active |
| C3 | ReplyTurn._output | 删除只写缓存, 保留发送大小计数 |
| C4 | RequestDiagnosticLog.capacity | 删除无消费者属性 |
| C5 | RequestInbox.discard | 删除无消费者入口 |
| C6 | 账本 pending_counts 与注册器 cached_state | 删除测试入口及无生产读取的近期缓存, 账本仍是权威来源 |
| C7 | group_chat_native_stickers | 删除复数包装, 保留单组件能力 |
| C8 | group_active_revision | 删除无消费者入口 |
| C9 | forward_ids_in_message | 删除无消费者工具函数 |
| C10 | OneBotAdmin.get_group_list 与 get_message | 删除仅测试接口, 生产目录和消息 reader 保留 |
| C11 | MessageEvent.agent_binding_source | 删除只写字段和测试专用断言 |
| C12 | MemoryStore.count | 删除无消费者且不在服务白名单内的入口 |
| C13 | ASR getters 与 set_parameters | 删除无业务消费者接口, 构造及逐次调用参数保持 |
| C14 | Plugin.permissions 与 effective_config | 删除字段及同步/异步安装路径的四处只写赋值, 真正的入口权限绑定保留 |
| C15 | 管理员例外 strict_scope | 只删除 overrides 的未使用参数, 群名单实际 strict_scope 保留 |
| C16 | group_admin needs_group | 删除定义表恒真字段及不可达分支 |
| C17 | retryFullRuntimeChanges | 删除无页面消费者 API, 保留真实运行应用接口 |
| C18 | 唤醒策略契约内部导出 | 缩小无跨模块消费者的类型和常量导出, 生产使用的 POLICY_FIELD_BY_KEY 保留 |
| C19 | 配置版本内部导出 | 内部类型和比较函数改私有, 公共读取选项保留 |
| C20 | 唤醒覆盖内部导出 | RowsConversion 与 parseOverrideJson 改私有, 测试通过公共行转换验证 |
| C21 | STAGE_LABELS | 改模块内部常量 |
| C22 | reminderStateLabels | 改模块内部常量 |
| C23 | FormModal.cancelText | 删除无消费者 prop, ConfirmModal 的实际文案用途保留 |

## 公共实现与复用

| 项目 | 内容 | 实现位置 | 保留的契约 |
|---|---|---|---|
| D1 | Agent 配置引用保护 | `core/config/reference_guard.py` | 两类配置服务共用拒绝与扫描错误映射, 写锁仍由原入口持有 |
| D2 | 插件引用字段枚举 | `core/config/plugin_references.py` | ASR 与群引用扫描共用枚举, 名称 trim 和空项规则仍分别保留 |
| D3 | 平台无关身份基础规则 | `core/utils/validation.py` | 六个领域共用有界文字检查, 保留原值/trim/计数方式差异 |
| D4 | 覆盖表名 | `core/schema_names.py` | 七个存储/配置消费者使用 SESSION_OVERRIDE_TABLE |
| D5 | 文件指纹与来源变化重试 | `core/utils/file_snapshot.py` | 三个来源共用文件身份, 两个无缓存捕获共用三次重试; SnapshotCache 的并发发布协议保留 |
| D6 | 好友迁移标记 | `edictum/migration_keys.py` | 读写共用 GLOBAL_RECEIPT, 不引入循环依赖 |
| D7 | 原子 JSON 写入 | `core/storage/persist.py` | 迁移/插件启用/配置写入共用原语, 文档保留 LF/fsync/Windows 重试与 CAS 文件锁 |
| D8 | 迁移访问配置路径 | `edictum/plugin_config.py` | 公开 global_path, 所有跨模块私有调用移除 |
| D9 | 签名与 offset 分页 | `core/utils/pagination.py` | 提醒和群记忆共用游标, 签名仍在各自数据库读事务生成 |
| D10 | 管理入口平台存在性 | `core/config/conversation_catalog.py` | 五个管理调用共用 require_catalog_platform, 配置/归档/冷平台语义保留 |
| D11 | LLM 与 ASR 引用删除 | `core/framework/BackGroundManager.py` | 共用删除与检查流程, 外层 REFERENCE_SCAN_LOCK 和内层管理锁保留 |
| D12 | 两个 reconcile_one | `core/framework/SessionManager.py` | 共用锁顺序/身份/绑定/来源 await 前后检查/结果观测, 两类领域动作及原计时字段保留 |
| D13 | 群会话路由解码 | `core/config/group_session.py` | 两入口共用解码, 事件三项快照兼容与适配器严格代次检查保留 |
| D14 | 发送与流式发送守卫 | `core/platform/event.py` | 共用入口守卫, 适配器跨 await 再核验保留 |
| D15 | 图片与语音标识 | `core/platform/onebot/validation.py` | 两个 SDK 入口共用媒体标识规则, 原错误文案保留 |
| D16 | 语音转写消息 ID | `core/platform/onebot/admin.py` | fetch_ptt_text 使用既有消息 ID 归一化 |
| D17 | OneBot 数字身份 | `core/platform/onebot/validation.py` | 共用十进制基础规则, Unicode/ASCII/负号/类型转换差异保留 |
| D18 | 申请 preflight 组合 | `core/config/group_action_origin.py` | 共用外层优先的同步/异步复核组合, 本层 inbox 与 ledger 校验仍保留, 发送标记在全部复核通过之后 |
| D19 | get_message 响应常量 | `C10 删除` | 无消费者的 get_message 已移除, 不新增无用常量 |
| D20 | 群管理动作参数 | `core/platform/onebot/validation.py` | 共享严格 bool 和文字长度, 宿主与 SDK 两道入口校验保留 |
| D21 | 环境变量名称解析 | `core/utils/config_text.py` | 两个插件使用同一逗号解析器, 不用于权限名单 |
| D22 | 好友异常重复解析 | `expend/plugins/friend_manager/tools.py` | 每个 catch 只解析一次, 日志与返回共享解析结果 |
| D23 | CLI 检查器装配 | `core/config/reference_checkers.py` | 三个 CLI 共用装配, ASR 独立扫描和配置 data_root 保留 |
| D24 | 前端鉴权拦截器 | `satrap-ui/src/api/serviceAuth.ts` | 两客户端共享会话与单次 401 重放, 地址绑定/禁止写重放/响应解包/领域错误保持 |
| D25 | 群操作状态标签 | `satrap-ui/src/pages/Groups/groupActionLabels.ts` | 三个相同映射共享, 操作页 all 筛选保留 |
| D26 | 日志条目类型 | `satrap-ui/src/pages/Logs/VirtualLogList.tsx` | 页面直接使用 LogEntry, 删除重复类型及数组强转 |
| D27 | 平台错误码规则 | `core/platform/onebot/validation.py` | 错误对象与动作拒绝映射共用 normalized_retcode, 缺失动作判定的不同语义保留 |

## 最终验证与修正记录

- 剩余六项边界重构首批: 232 条单元测试通过
- 逐实例生命周期/来源/并发/模型/回滚回归: 98 条通过
- 新增原子替换失败与恢复回归: 记忆迁移 9 条通过
- 前端类型检查通过, 38 个测试文件共 305 条通过, 生产构建通过
- 新增鉴权回归证明: 绑定写请求 401 不重放, 读取仅重放一次, 等待期间地址变化不发送, service_changed 原因码不丢失
- 生产 Python 目录全部 389 个文件类型检查通过, 0 errors / 0 warnings; 改动的发行冒烟脚本也纳入最终类型检查
- 首轮后端全量: 4007 passed / 9 skipped / 7 failed. 五条仍调用被删除的 count/due, 已改用真实列表/调度入口; 两条发现 C15 误删管理员组保存的 strict_scope=True, 已恢复且相关 80 条通过. 第二轮完整回归 4018 passed / 9 skipped / 0 failed, 已从 JUnit 逐条确认首轮失败项通过
- 申请复核新增异步外层拒绝和作用域恢复覆盖, 加上插件权限回归共 71 条通过
- 浏览器 8 个脚本通过: auth-startup, control-plugin-application, runtime-application-progress, scoped-agent-apply, log-stream, confirmation, account-requests, group-chat-memory-reminders
- log-stream 首次并行运行在虚拟列表末行可见性等待超时; 同一脚本单独运行通过, 当前生产构建下再次通过, 保留完整导出/5000 条保序/暂停/阅读锚点/窄屏断言
- 默认全仓 Pyright 会扫描归档与测试替身并报告类型错误; 未改动的 scripts/release/launcher.py 另有 TextIO.reconfigure 类型诊断. 本轮生产 satrap 目录与改动脚本零错误, 不把默认全仓扫描声称为通过
- 最终后端报告: `C:/Users/midwa/AppData/Local/Temp/satrap-reuse-verified-a24eeb63422b447a8dc00527084378bd.xml`, 共 4027 个 testcase, 4018 通过 / 9 跳过 / 0 失败
- 9 个跳过原因: 4 个显式集成测试, 4 个 Windows 符号链接权限限制, 1 个未安装 reportlab 的 PDF 项; 与既有环境限制一致
- 最终生产类型检查: `python -m pyright satrap scripts/release/smoke_release.py`, 0 errors / 0 warnings
- pyflakes 检查 251 个相关生产文件, 唯一诊断是已注明注册副作用的 OneBot 适配器导入, 其余无未使用导入或局部量
- 最新生产构建通过, `git diff --check` 通过; 50 项条目都有当前实现位置和契约说明, 原始审计文件未编辑
- C15 最终核验: 管理员组保存仍传 strict_scope=True, 只删除例外归一化的无用 strict_scope 参数
