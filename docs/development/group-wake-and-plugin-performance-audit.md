# 群聊全触发与插件性能验收审计

状态: 计划要求已核验, 保留边界见第 3 节

范围: `group-wake-and-plugin-performance-plan.md` 第 1-7 节, 本轮读取性能与界面行为优化, 定向应用及全触发契约

业务实现核验代码为 `4f8a31f`, 运行日期为 2026-10-11; 配置, 令牌, 端口和平台实例均隔离, 未操作用户 QQ 运行服务

## 1. 已核验的要求

下表中的通过指当前代码与实际测试覆盖对应要求, 不表示真实 QQ 或外部 MCP 时延已经测量

| 要求 | 当前实现与证据 | 结果 |
| --- | --- | --- |
| 第 1 节范围 1/8: 唯一策略枚举与派生契约 | `platform_policy.py`, 生成 JSON, `sync_wake_policy_contract.py --check`, `test_policy_field_contract.py` | 通过 |
| 范围 2/4: 正常消息, 附件, 去重及准入 | `wake_policy.py`, `scheduler.py`; `test_wake_all.py` 的支持输入, 拒绝输入, 实际 OneBot 入站用例 | 通过 |
| 范围 3/5/7: 平台, 群, 时段及中文覆盖选项 | `wakeOverrides.ts`, `platform-policy.mjs`; 实际表单保存往返, 继承及切换 | 通过 |
| 范围 6/9: all 不使用阈值, 冷却, 自动计时 | `wake_dry_run.py`, `wake_window.py`, `wake_timers.py`; 高阈值/旧冷却/旧 timer 和 ticket 的反例 | 通过 |
| 范围 10: 原子消费, 来源与真实调用者 | `test_all_shared_batch_keeps_identity_and_cannot_gain_other_member_permission`, 竞争认领及富媒体原事件测试 | 通过 |
| 全触发默认兼容与命令路径 | 默认 explicit, 私聊沿原判断; 唤醒, 会话命令, 调度及平台策略回归 | 通过 |
| P1: 每个候选元数据只解析一次, get 用索引 | `plugin_catalog.py`, `test_plugin_catalog_snapshot.py`; 跨进程控制服务 10 个候选各解析一次, 后续新增 0 次 | 通过 |
| 快照不共享可变内容, 保留目录优先级及错误 | 目录/命名/应用快照独立副本与损坏恢复测试, 外部新增/删除/替换测试 | 通过 |
| singleflight 取消, 失败, 队列满与迟到结果 | `snapshot_cache.py`, `test_plugin_catalog_snapshot.py` 的线程竞争, 取消隔离, 清理后通知和池满反例 | 通过 |
| P2: 读取不阻塞控制事件循环, 明确队列满 | 目录异步预加载, 有界工作线程; 并发 6 个 200 ms 慢读取期间 30 次实际状态查询 P95 小于 100 ms; 管理员队列满返回 503 | 通过 |
| 冷配置, 迁移及保存 revision 保持原规则 | `test_edictum_config_snapshot.py`, `test_plugin_config.py`, 管理员/插件/群工具/记忆迁移回归 | 通过 |
| 配置版本与正文对应, 外部改动可检测 | `config_versions.py`, `configVersions.ts`; 原子替换, 同 stat 内容变化, 强制核验, 损坏/删除/恢复和前后版本测试 | 通过 |
| P3: 仅选中插件读取参数, 选项独立加载 | `plugin-loading.mjs` 实际 12 插件表单; 无需求不读取模型/RAG, 选项失败不隐藏普通参数, 新增草稿保留 | 通过 |
| 管理员配置先显示, 运行结果独立确认 | `test_administrator_read_split.py`, `administrators.test.ts`, `administrators.mjs`; 状态挂起时可编辑, 原 GET 兼容, 迟到结果不覆盖保存 | 通过 |
| P4: 按插件/Agent/使用位置应用, 不重建无关模型 | `test_plugin_apply_scope.py`, Provider 完整应用与平台快照测试; `scoped-agent-apply.mjs`, 三进程实际 2 个目标/3 个不相关实例 | 通过 |
| 单次应用固定来源, 会话覆盖实时核验 | `test_plugin_apply_snapshot.py`, `test_full_agent_apply_snapshot.py`, 平台排队变化/管理器替换/实例代次/回滚反例 | 通过 |
| Chat 有界并发, 原会话锁, 模型请求不取消 | Chat 服务回归与三进程模型等待测试; 5 个实例实际安装, 同配置重试额外安装/卸载为 0 | 通过 |
| 可查询任务, 真阶段, 同 ID 只执行一次 | `test_application_progress.py`, API 与控制桥接测试, 三进程 HTTP; 同 ID 冲突, GET 不启动任务, 旧同步接口兼容 | 通过 |
| P5: 配置与健康读取分离 | `useBackendStatusSync.ts`, 配置版本/订阅单测, `backend-platform-status.mjs`; WS 在线 30 秒兜底, 断线 5 秒, 配置按消费需求 | 通过 |
| P6: 认证独立, 明确截止与请求隔离 | `auth.test.ts`, `auth-startup.mjs`; 挂起服务无业务/WS, 可用控制页先显示, 单服务重试, URL 令牌移除 | 通过 |
| P7: 可见性, 防重入, 取消, 截止及草稿 | `visiblePoll.test.ts`, `group-polling.mjs`; 隐藏暂停/恢复单次补查, 慢读取不叠加, 60 秒动作截止不恢复重启 | 通过 |
| P8: 无关状态订阅与未变化引用 | 原 17 处无 selector 调用已移除; store 与配置读取单测核验稳定引用, 保存后局部合入, 旧全量读取不回退 | 通过 |
| P9: 长操作总截止, 原服务查询与迟到保护 | `runtimeApplication.test.ts`, 控制/Agent 进度浏览器; 120 秒覆盖认证和在途 GET, 超时保留保存事实, 查询不重新提交 | 通过 |
| 撤权, 待审批复核及真实身份不缓存 | `test_administrator_plugin_integration.py`, `test_plugin_management_permissions.py`, `test_provider_call_origin.py`; 实际待审批执行前撤权及原调用作用域失效反例 | 通过 |
| P10: 日志保序, 筛选, 导出, 暂停及阅读位置 | `logBuffer.test.ts`, 生产 `log-stream.mjs`; 5000 条导出逐条比对, 可见 DOM <100, 多行/窄屏/阅读锚点 | 通过 |
| Chat 长历史与媒体按规模验收 | 生产 `chat-history-media.mjs`; 100/500/1000 消息, 10/50/100 图片及 WebM, 100 个流式增量, 切换与分页编辑定位, 取消迟到媒体 | 通过 |
| 性能夹具有效且错误明确失败 | `fixtures.node-test.mjs`, smoke 6 场景; 凭据 CORS, WS data 信封, 未匹配接口和页面异常的失败路径 | 通过 |

## 2. 本轮实际验证

- 后端组合回归分别 549 项与 285 项通过, 包含元数据/版本/热应用/权限/迁移/会话/唤醒; 管理员补修的控制与跨进程组合 86 项通过, 后续补充队列满 4 项通过; 各组有交叉, 不累加成总测试数
- 前端全量 298 项单测, TypeScript 检查, Vite 生产构建及压缩脚本通过; 修改的后端与新增验收文件 Pyright 错误级检查无错误
- 串行浏览器运行 `plugin-loading`, `auth-startup`, `backend-platform-status`, `group-polling`, `log-stream`, `platform-policy`, `control-plugin-application`, `runtime-application-progress`, `scoped-agent-apply`, `chat-history-media` 通过; 管理员补修浏览器另行通过
- 使用 `LOG_STREAM_PRODUCTION=1` 单独验证生产日志: 本轮突发到末条约 716.2 ms, 筛选约 14.6 ms, 没有观测到长任务; 单轮固定夹具不是持续负载 P95, 不换算速度倍率
- 最新串行生产 Chat 夹具: 100/500/1000 消息首开约 497/517/839 ms, 阅读锚点偏移 -0.203/-0.203/-0.25 px, 底部间隙均为 0; 采样环境不同, 不以与旧记录的差值宣称优化倍率
- 跨进程验证运行命令为 `python -m pytest tests/integration/test_plugin_application_processes.py --run-integration`; 不加开关会跳过, 跳过不计作通过
- 原用户配置规模的最终只读复测通过: `python tests/manual/plugin_read_scale_probe.py --report docs/development/group-wake-and-plugin-read-measurement.json`; 当前配置副本为 9 插件, 3 命名配置, 11 处引用, 与第 2 节初始规模相同, 元数据各解析一次共 9 次, 命名归一化共 3 次, 并发阶段均新增 0 次
- 最终实际 HTTP 首读/暖读中位数: 插件总览 201.910/15.605 ms, 好友参数 5.479/5.888 ms, 命名配置 19.234/7.176 ms, 原管理员接口 47.387/46.683 ms, 独立配置快照 29.659/18.635 ms; 每接口 1 次首读和 5 次暖读, 不把单项首读称为独立进程冷启动
- 6 个原参数读取各增加 200 ms 慢存储时, 30 次状态查询 P95 33.084 ms/最大 33.765 ms; 源配置内容摘要与修改时间逐一核验不变, 保存入口另加拒绝守卫; 只保留统计的 JSON 报告已入库
- 该同规模复测执行当前原 HTTP 和领域读取实现, 将原配置路径映射到临时副本, 健康/管理员状态由隔离后端返回, 没有适配器或模型连接; 数据可与初始规模定位结果对应, 不把连接环境差异换算为速度倍率

## 3. 保留边界

- 原规模读取已另行核验, 与 10 插件/2 命名配置的三进程生命周期验收分开记录, 不混用配置规模和时延
- 第 6.4 节为规模风险与审查范围, 没有承诺实现无限历史虚拟列表, 大视频零拷贝或 RAG 导入链路重构; RAG 新建/删除存在显式 list 与 selectedId effect 再读取的路径, 已确认, 未测量其实际时间占比
- content-visibility 当前确实存在, 不等于 DOM 数量减少; 本轮保留全部历史和流式内容, 不宣称解决无限规模或所有显卡上的滚动性能
- 已测真实隔离插件的原安装/卸载与会话锁, 首次应用的人为暂停只用于阶段验收; 外部 MCP 初始化和真实平台网络时延不包含在采样中
- 全触发不保证超载时每条消息单独请求模型, 不承诺必然回复; 队列/限流/容量诊断, 命令消费, 身份和授权规则仍生效
- 未重启原运行服务或进行新的真人 QQ 验收; 最终上线后可观察实际负载, 不能把当前隔离采样当成线上 SLA
- 第一轮离线复测把连接等待计入状态耗时, 因此未计作通过; 其失败临时目录 `C:/Users/midwa/AppData/Local/Temp/satrap-read-scale-3y5sdeyo` 清理被自动审批拦截, 保留原位置且没有再尝试删除; 成功复测关闭日志与服务后正常清理自己的新临时目录

## 4. 页面读取版本误报回归修正 (2026-10-11)

- 用户进入仪表盘, Agent 配置和模型配置时遇到持续变化提示; 三个入口共用模型配置读取, `ModelConfigManager.reload()` 原先在每次成功加载后无条件保存, 内容未变但文件指纹改变, 前端三次重试均失败
- 修正前新增的四种模型实际控制路由测试全部复现版本改变; 修正为仅在磁盘内容与归一化结果不同时写回, 保留首次创建, 旧格式迁移, 显式保存和真实外部修改的版本检测
- 修正后后端相关 104 项, 前端版本与配置 store 12 项通过, 修改 Python 文件 Pyright 无错误; 测试覆盖连续读取, 四类型并发读取, 外部编辑可见与旧格式仅迁移一次
- 另将当前实际模型配置复制到临时目录, 按四种类型连续读取三轮, 12 次读取均保持正文, 修改时间与版本不变; 原文件正文及修改时间未改变, 未重启原运行服务
- 原规模性能复测未包含模型页面的读取前后版本闭环, 且前端夹具提供固定版本, 因此没有覆盖此次回归; 新增控制路由测试直接串联实际版本服务与模型读取, 不以固定版本替代
