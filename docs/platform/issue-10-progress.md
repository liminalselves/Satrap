# Issue #10 实施记录

更新日期: 2026-09-23 (异常/日志整改收官)

目标仍为完整实施 [主方案](issue-10-plan.md), 本记录不将首批改动视为议题整体完成。

## P0 当前改动

- scheduler 根据真实 At 和顶层 Plain 中的 wake_words 判断唤醒, 保留上游显式标志, 不扫描引用与附件
- 未唤醒、停止或禁用模型的事件不消耗模型额度, 不产生限流反馈; 空输入同样不消耗额度
- OneBot 实例级 group_whitelist 在入站、管线和主动发送前检查, 空列表不限制群范围; 配置保存与启动共用校验
- 平台 UI 支持群白名单和唤醒词逐行编辑, onebot/aiocqhttp 共用表单与规范化; 暂时准确提示平台改动需重启后端
- 更新配置示例、平台接入说明及行为测试, 未提交或发布 PR

## 已执行检查

- 完整单元测试: `python -m pytest tests/unit -q`, 1562 passed / 7 skipped; 跳过原因为显式集成开关、缺少 reportlab 和 Windows 符号链接权限
- 最后补充排队后收紧白名单测试后的相关回归: 67 passed
- 前端既有完整测试: 66 passed; 新增表单转换案例后的目标文件测试: 6 passed
- 前端 lint、TypeScript/Vite 构建成功
- 四个修改的后端模块按 `.pyrightcfg/pyrightconfig.json` 检查: 0 errors / 0 warnings
- `git diff --check` 通过

以上不等同于完整离线 pytest、浏览器交互验收或外部系统联调。尚未读取测试凭据、连接 SnowLuma 或调用真实模型 API。

## 接续工作

1. P0 前端浏览器回归与深浅主题/窄屏截图检查已完成; 后续新界面继续扩展验收
2. P0a-1 执行基础已加入有界入站/等待区、跨来源会话并发和最终 Session 回复串行; context_scope 持久化路由与保留旧映射已接入, 仍待浏览器与两种 Provider 的完整集成验收
3. P0a-2 配置 saved/active 应用闭环, P0a-3 两种 Provider 的可信调用身份
4. 按主方案继续 P0b、P1-P4: 高级/手动唤醒、回源与出站、事件和完整工具、ASR/文件、真实 SnowLuma 与模型验收

当前不将 P0 标记为完整验收通过。引用机器人可选唤醒、独立接收防洪、去重/账号绑定和其余计划能力尚待对应批次完成。

## P0a-1 执行基础进展

- 默认入站容量 256, 待执行容量 256, 并发来源会话数 8, 等待 TTL 120 秒; 配置使用 event_queue_capacity/event_pending_capacity/event_concurrency/event_queue_ttl, 保存及启动时拒绝非法容量与非有限 TTL
- 同来源会话保持顺序, 不同来源有界并发; scheduler 在最终 Session 上覆盖模型与发送的整轮串行, SessionManager 原有执行锁继续生效, 空闲锁及时回收
- 满载丢弃最新入站, 过期丢弃等待请求; 关闭取消任务并清理排队资产, 配对 queue.task_done; 健康状态返回 queued/capacity/active/pending/dropped/expired
- 相关平台/管线/健康/配置回归 78 passed; 最后增强回复顺序行为测试后调度/管线 39 passed; 修改模块 Pyright 0 errors / 0 warnings
- 本批次之后尚未重复完整单元测试, 未完成浏览器验收; notice/心跳独立处理仍待事件接入批次, 不宣称已有真实心跳联调

后续优先检查 UserManager 的 context_sessions 键、SessionManager 的切换会话路由及存储归属, 再实现 context_scope, 避免只改变表面 Session ID 而留下共享群权限或历史归属错误。

## P0a-1 会话范围进展

- 增加不可变 ConversationRoute, 使用独立范围键持久化群成员/群共享路由; legacy_user 与私聊保留旧键, 变更 scope 不合并历史
- UserCall 携带本轮路由; SessionManager 切换会话时更新来源范围键, 避免从目标会话 ID 反推并改错群映射
- 共享群的存储归属留空, 不绑定首位成员; Provider 上下文值与内部会话标识分离, 不把范围散列当用户 ID 注入
- 平台表单新增范围选择和变更提示, 新建 OneBot 默认为 group_member, 旧配置缺省仍为 legacy_user
- 范围/管线相关测试 43 passed, 覆盖跨群/账号/成员隔离、共享、恢复、旧映射、原始入站及会话切换; 本批次修改模块 Pyright 0 errors / 0 warnings, 前端 lint/build 通过

完整目标仍在进行。下一步补浏览器验收, 然后推进 P0a-2 配置实际应用与 P0a-3 可信工具调用身份; 本次路由字段不等同于已完成工具权限接入。

用户管理、Provider、会话配置/并发及存储回归: 85 passed / 1 skipped (Windows 符号链接权限), 未使用真实模型 API。

## 平台页浏览器验收

- 新增 `npm run test:e2e:platform`, 用 Vite 真页面与 Playwright 受控 API 验证, 不访问真实配置或模型
- 覆盖旧配置保持 legacy_user、aiocqhttp 共用表单、新建默认 group_member、列表保存回显、空白名单、错误保留输入、扩展字段保留、标签关联、Tab 操作与 390px 窄屏
- 通用 FormModal 增加稳定的标签/输入关联; 平台卡片替换原始 settings JSON 为策略摘要, 浏览器验证不展示夹具中的 token
- 已查看深色桌面、浅色桌面与浅色窄屏截图, 截图位于 `satrap-ui/test-results/platform-policy/`, 临时验收产物不入库
- 浏览器脚本通过, 前端完整单元测试 68 passed, lint/build 通过

P0a-2 接续核查: BackendConfig 当前未记录来源配置路径, reload_config 仅重载模型/会话定义。须先保留实际启动文件路径, 再读取目标配置并实现 saved/active 修订状态、策略快照与定向适配器应用; 不能猜测工作目录中的默认文件或把模型 reload 成功当平台生效。

## P0a-2 保存/生效状态与策略应用

- YAML/JSON 加载记录实际 source_path, 不从配置正文接受该字段, 不写回公开配置; 平台重载从实际启动文件重新读取, 嵌入式字典配置沿用内存来源
- reload 与 health 提供逐实例 saved_revision/active_revision/status; 已启动 OneBot 的策略以配置替换应用, 原事件保留接收时策略副本
- 连接、实例新增/删除、执行容量与会话绑定当前标记 pending_restart, 尚未完成定向重建; 不混合应用同一实例中与连接变更一起保存的策略
- 读取/校验失败保留旧状态, 错误不回显配置正文; 前端展示状态与版本, 保存成功但运行时失败不误报保存失败
- 后端相关回归先运行 84 passed, 补默认值等价与非法端口后配置/健康回归 19 passed; 修改模块 Pyright 0 errors / 0 warnings; 前端 lint/build 通过

后续必须完成定向生命周期协调、失败回滚、配置冲突检测、停用/删除清理, 才能将 P0a-2 标记为完整交付。P0a-3 及 P0b/P1-P4 仍待继续。

平台浏览器回归已扩展并通过: 受控后端依次返回 applied/pending_restart/failed, 页面准确显示三种状态与版本; 页面进入时主动刷新后端健康。后续需将控制端保存修订与后端源文件修订相互校验, 防止不同配置源被误认为同一次保存已生效。

## P0a-2 定向生命周期协调

- 分发器支持动态添加/移除独立平台工作器, 定向替换不取消其他平台任务
- 连接/容量/会话绑定变更先构建并验证候选实例, 停止旧实例后启动新实例并检查就绪; 失败恢复旧配置与运行实例, 回滚未恢复时不保留虚假的生效版本
- 支持新增、停用、重新启用及删除实例; 停用/删除清理等待消息和临时资产, 不删除会话历史; 后端关闭与配置应用互斥
- OneBot 通过实例专属本地 HTTP 探针验证服务就绪, 测试实际启动本地 aiocqhttp/Quart 监听, 覆盖端口被其他服务占用的失败情况; 这不等同于 SnowLuma 联调
- 前端补平台启用开关, 配置保存校验布尔值; 浏览器回归、lint/build 通过
- 生命周期/调度/健康目标回归 36 passed, 修改模块 Pyright 0 errors / 0 warnings

保存冲突检测和控制端/后端配置源修订校验仍待完成, P0a-2 不标为完整交付。其后继续 P0a-3 与 P0b/P1-P4。

## 完整回归与配置写入冲突基础

- 上批完整 pytest: 1580 passed / 19 skipped / 2 failed; 两个失败均为文档上传真实 HTTP 测试读取超时
- 在临时目录导出的未修改 HEAD 上复跑相同两个测试, 同样 2 failed, 错误位置与 TimeoutError 一致; 不将完整回归描述为全绿, 本批不混入文档上传修复
- 配置保存复用跨进程 FileLock, 将修订比较与原子替换置于同一临界区; 过期修订抛出独立 ConfigRevisionConflict, 保留现有文档且不回显凭据
- CLI 平台新增/更新/删除携带读取时修订; 原有调用兼容无条件保存, 因而尚不能宣称所有写入入口都已防止覆盖
- 新增独立进程竞争测试和过期写入测试; 配置文档及管理端回归 22 passed

接续仍需控制端 GET 返回修订、前端提交读取时修订与 409 冲突保留草稿、其他配置写入入口接入, 以及控制端保存结果与后端实际来源修订一致性校验。

## 平台页修订闭环

- 控制端平台读取/保存返回文档修订, 新增/更新/删除接受 expected_revision; 冲突返回 HTTP 409, 不覆盖后续配置
- 页面固定使用打开表单时的修订, 冲突保留表单内容并提示用户合并; 删除使用列表读取时修订, 缺少修订时不执行写入
- 保存返回的修订传给后端重载; 后端读取实际启动源并核对内容, 不匹配或无文件来源时拒绝平台应用, 保留原运行配置并报告 source_revision_mismatch
- 后端目标回归 37 passed, 修改的四个核心模块 Pyright 0 errors / 0 warnings; 浏览器覆盖保存修订传递、409 不写入和草稿保留, lint/build 通过

旧平台 API 调用不提供修订时仍兼容, 只防止其服务端读取到写入之间的竞争; 其他完整配置编辑入口仍需接入修订。当前冲突恢复采用保留输入并提示复制/刷新合并, 尚无界面内三方合并功能。P0a-3 与 P0b/P1-P4 仍待实施。

## P0a-3 逐次调用来源基础

- 新增不可变 CallOrigin, 在 MessageEvent 创建时冻结 adapter_id/self_id/chat_type/chat_id/actor_id/source_message_id/request_id, 经 UserCall 进入执行边界
- SessionManager 同步/异步入口使用独立 ContextVar 作用域, 不写入共享 Session 或模型参数; 无来源调用显式屏蔽外层身份
- 工具通过 require_call_origin 读取当前来源, 缺失时报不可用; 作用域结束/取消后撤销派生上下文的访问, 避免后台任务沿用上一轮身份
- 已验证并发成员隔离、模型伪造参数不覆盖身份、线程池传递/清理、取消及后台任务撤销、消息预处理不能改写冻结来源; 首批来源/管线/OneBot 回归 57 passed, 修改核心模块类型检查 0 errors / 0 warnings

这只是可信来源传递基础, 不代表已完成平台工具或权限检查。仍需两种 Provider 实际工具安装/执行集成验收、平台能力及权限查询, 并将未来手动唤醒的已认证主体接入同一契约。平台上报主体的真实性仍依赖传输鉴权与后续账号绑定校验, 不把冻结结构本身当作认证。

补充来源线程池及入站冻结测试后, 来源/OneBot/既有 Provider/SessionManager 并发回归 74 passed; 其中既有 Provider 回归用于检查兼容性, 不替代上述待补的身份工具集成验收。

## Provider 实际工具循环与独立唤醒策略

- 使用真实 SessionClassProvider/EdictumProvider 构造同步和异步 SimpleSession, 经过工具注册及模型工具循环验证同一会话内 admin/member 连续调用; 模型为可控替身, 工具读取真实作用域, 四种组合全部通过
- 独立 WakeDecision/evaluate_wake 提供触发规则及原因, scheduler 保留决策; 默认仍为真实提及或唤醒词, 可选 wake_aliases 仅扫描顶层正文
- 配置保存/启动校验、在线策略应用和前端别名表单已接入; @全体、文本伪造 @、单独附件不触发
- 来源/Provider/OneBot/管线/配置目标回归 80 passed, 唤醒模块及 scheduler 类型检查通过

Provider 测试验证工具注册表与工作流身份传递, 不宣称已实现群管理插件及权限查询。高级频率/必要性/空窗/群覆盖、手动唤醒与引用回源仍待后续批次完成。

## 自动参与频率模式与正文窗口

- 新增有界 WakeWindow: 最多 512 个路由, 每路由 32 条/8192 字符, TTL 120 秒; 仅保留顶层文本及消息/成员标识, 不提前处理附件
- 路由键包含平台实例、账号、群、Provider/会话类型和范围; 仅显式 group 共享成员正文, 其他范围按成员隔离
- wake_mode 默认 explicit, 可选 frequency; wake_message_threshold 默认 3, wake_cooldown 默认 30 秒, 明确唤醒绕过自动冷却
- scheduler 在权限检查后观察, 在最终 Session 锁内提交前重新核对并消费快照; 限流前不消费, 已提交消息不自动重试; 重载策略或替换实例清理对应窗口
- 平台表单加入模式/阈值/冷却配置; 保存和 OneBot 启动共用校验
- 窗口/管线/配置运行时回归 40 passed, 修改窗口及策略模块类型检查通过

仍需补窗口生命周期与配置并发的完整验收、可配置窗口限制、必要性评分、群及时段覆盖、空窗补偿和手动唤醒。当前频率规则为显式消息数量阈值, 尚未提供 talk_value 映射。

## 本地必要性评分与提交复查

- 新增 necessity 模式, 与 frequency 二选一; 根据问题特征、指向性表达、积压量和近期调用提交占比计算有界分值, 不调用额外 LLM
- 决策包含分值与各因素解释, 阈值及四项权重支持配置校验和在线更新; 页面提供模式及阈值, 默认仍为 explicit
- 历史活动按路由保留至多 64 项, 全局路由数量有界; 提交占比用于保守抑制连续调用, 不将其误称为实际成功发送占比
- 最终 Session 锁内在模型提交前重新检查停止状态、来源范围与权限; 旧自动策略事件在停用/改变规则后不能重新填充窗口或提交自动调用
- 窗口/管线/配置目标回归 42 passed, 修改核心模块类型检查通过; 剩余空窗、群/时段覆盖、talk_value、手动唤醒等继续实施

## 群及时段覆盖

- wake_group_overrides 支持按规范群 ID 覆盖唤醒词/别名和自动参与参数, 至多 512 群; 禁止覆盖权限、账号或 context_scope
- wake_time_rules 使用本机时区 HH:MM, 支持跨午夜, 起始包含/结束不包含, 最多 32 段; 重叠时段后项覆盖前项
- 优先级为平台默认 → 当前时段 → 当前群; 时段仅调整自动参与, 不关闭显式提及或修改唤醒词
- 入站冻结有效策略, 自动提交前按当前时段/群重新比较; 配置变更继续清理平台窗口
- 平台表单提供两项 JSON 配置入口; 前端转换测试 7 passed, lint/build 与既有浏览器回归通过; 后端策略/窗口/管线/运行时回归 51 passed, 类型检查已修正至 0 errors / 0 warnings

覆盖配置界面目前为 JSON 编辑, 尚未提供逐条时段编辑器或规则预览。空窗补偿、talk_value 映射、手动唤醒和完整后续计划仍待继续。

## 实际 SnowLuma 网络探针

- 已找到用户安装的 1.14.17 发行包, 新增 `scripts/probe_snowluma.py`; 使用安装包实际 WsClientAdapter 与原生 WebSocket 实现, 仅 QQ 侧事件源/动作执行器模拟
- 错误 token 返回 403, 正确 token 建立 Universal 连接; 两次入站、四次乱序动作回包对应、断线后自动重连全部通过
- 不启动 QQ、不改现有账号配置、不打包 SnowLuma; 临时驱动与监听在结束时清理, 运行时随机 token 不写入配置
- 版本、包摘要、命令和挂接边界详见 `issue-10-snowluma-probe.md`; 本次未调用真实 LLM/ASR, 不宣称后续完整验收完成

## 频率模式空窗补偿

- 新增 wake_max_wait, 默认 0 关闭, 可设置大于 0 且小于窗口 TTL 120 秒; 只为 frequency 模式已有正文安排补偿
- 每路由最多一个计时任务, 总数受窗口 512 路由容量约束; 保留独立正文事件, 不持有原事件附件或原始载荷
- 到期提交现有平台队列, 再走预处理、权限、来源、冷却、限流和最终 Session 锁; 不额外累计一条消息, 未消费正文才可能提交
- 成功提交取消同路由计时; 配置变化撤销计时与已排队票据; 后端关闭等待计时取消完成; 无新消息时不反复重试
- 自动冷却或限流可阻止到期调用, 不承诺绕过这些约束的绝对回复时限
- 窗口/覆盖/管线/配置/健康回归 60 passed, 修改核心模块类型检查通过; 前端补最长等待配置

仍待 talk_value 映射、手动唤醒、完整窗口可观测性及后续 P1-P4。空窗补偿完成基础实现, 尚需纳入最终全链路联调。

## 手动唤醒后端基础

- 新增 BackendManager.wake_platform 与已鉴权管理路由 POST /api/platforms/wake; 明确指定 adapter_id/group_id/user_id/request_id, prompt 可选, 无 prompt 时消费对应路由待处理窗口
- 管理服务的现有认证是共享管理身份, HTTP 边界固定使用 management, 不从正文接受操作者; CallOrigin 分开记录 management 主体与用于会话路由的群成员 ID
- 有界进程内幂等记录, 相同请求不会重复入队, 同 ID 不同内容拒绝; 返回 accepted/already_pending/no_pending/rejected, 重复结果附处理状态
- 手动标志由内部事件登记识别, 不伪造 @消息; 继续经过预处理、停止状态、权限、群范围、系统限流与 Session 串行; 队列满返回明确拒绝
- 手动/窗口/管线/运行时目标回归 47 passed, 修改核心模块类型检查通过

当前仅支持 OneBot 群目标和 prompt/待处理窗口, 不将路由 user_id 当作认证身份。指定 message_id 回源、路由歧义体验、前端会话按钮、control 代理和最终 HTTP 鉴权集成测试仍待接续; 不宣称手动接口完整验收完成。幂等记录目前仅进程内, 重启后不保留。

## 手动消息回源与页面入口

- 支持 message_id 与 prompt 二选一; get_msg 回源并发上限 4, 含槽位等待的总超时 5 秒, 响应长度限制 64 KiB
- 回源核对消息 ID、群、发送者和可用账号字段, 获取后复查群范围; 不接受跨群/成员回源, 不因回源动作下载附件
- 回源等待后再次检查幂等和实例身份, 并发重复请求只入队一次
- 真实本地 HTTP 测试验证未认证请求 401 且不入队, 合法管理 token 入队并使用服务端 management 身份; 手动唤醒测试 5 passed, 修改后端类型检查 0 errors / 0 warnings
- 会话实例页面增加手动唤醒弹窗, 支持正文/消息 ID/待处理窗口, 失败保留输入, 相同表单重试保留幂等 ID; 前端 lint/build 通过

尚待新增弹窗的浏览器交互验收、control 代理与更友好的路由/拒绝原因呈现。完整计划及 P1-P4 仍未完成。

## 分享对话接续: 入站隔离与发送回执

- 已读取分享对话并核对本地改动, 保留此前实现和用户裁定; 不将已有手动唤醒浏览器脚本视为本轮已执行验收
- OneBot 配置 self_id 或首次消息绑定账号后拒绝其他账号, 自身回声不进入队列; get_stats 的 ingress 字段提供账号/回声/重复拒绝计数
- 普通消息按实例内账号/类型/目标/消息 ID 去重, TTL 120 秒, 容量 4096; 并发转换前预占标识, 转换失败/取消/队列满释放, 无 ID 不共用去重键; 当前不跨重启持久化
- 新增 SendReceipt, OneBot 普通发送和流式降级区分 success/partial/failed/unknown, 保留平台 ID 和失败块位置; 平台明确拒绝与网络结果不明分开, 不回显动作响应细节
- MessageEvent 保存最近回执, 明确失败不无条件标记已发送, 部分成功与结果未知禁止兜底重发全文; 旧适配器 None 返回保留兼容, 后续失败不清除之前成功的去重标记
- 流式发送首次失败停止后续块, 生成器中断仍保留已确认 ID; 此处尚未实现按长度切分、每会话发送队列或回复引用/@策略
- 实际 SnowLuma 1.14.17 网络探针已按新回执更新并再次通过: 两次消息往返, 四次乱序动作回包, 错误 token 拒绝及重连通过; QQ 侧仍为模拟, 本轮未调用 LLM/ASR
- 最后补充无消息 ID 去重边界后的相关回归 96 passed, 修改的三个核心模块和探针 Pyright 0 errors / 0 warnings; UTF-8 读取与模块头 Docstring 检查通过
- 完整单元测试 1642 passed / 7 skipped / 2 failed (169.75 秒); 两个失败仍为 test_document_upload.py 中 control/chat 大文件上传读取超时, 与前序基线记录一致, 不宣称完整回归全绿

接续仍需完成手动唤醒 UI 验收及 control 路径, P1 引用补全/回复策略/长消息/状态展示, P2 事件和管理工具, P3 ASR 与文件, P4 真实模型和发行验证。高级规则编辑器、试算、持久化状态等主方案缺口同样保留, 整体目标未完成。

## P1 长消息拆分与每会话发送串行

- 新增 `onebot/outbound.py`: `split_components` 按段落 → 换行 → 硬切拆分文本, 非文本组件不切开且保序, 不修改原消息链; `OutboundTurns` 每实例至多 64 个逻辑回复, 同目标串行, 等待执行权 30 秒, 关闭时取消未完成任务
- `message_text_limit` (默认 2000, 64-32000) 由平台策略校验与前端表单接入; 多块发送首个非 success 后停止, 聚合为 partial/unknown 并保留 failed_index, 不重发已确认块; 队列不可用返回 failed/send_queue_unavailable 且不提交动作
- 此批次代码在上一轮分享对话中断处已写入但缺少测试与文档, 本轮补齐: 新增 `tests/unit/test_onebot_outbound.py` 17 passed, 覆盖拆分边界、配置校验、多块顺序/部分失败/未知结果、同目标串行与跨目标不阻塞、满载与关闭拒绝
- 配置示例与 `platforms.md` 同步; 修改模块 Pyright 0 errors / 0 warnings

回复引用/@ 策略、引用回源、合并转发发送分流仍未实现; 发送队列与分块目前只在 OneBot 落地, 其他适配器保持旧契约。

## 手动唤醒浏览器验收与 CLI 入口

- 实际执行 `npm run test:e2e:wake` 通过: 弹窗失败保留输入、相同表单重试复用幂等 ID、请求不携带 operator、no_pending 提示保持弹窗; 已查看 390px 窄屏截图, 布局与提示可读, 截图不入库
- 新增 `satrap platform wake <id> --group --user [--prompt|--message-id] [--reason] [--request-id]`, 经 DaemonClient.wake_platform 调用同一 HTTP 契约, 拒绝转为非零退出; CLI 解析与命令行为测试 14 passed, 修改模块 Pyright 新增 0 errors / 0 warnings (client.py/main.py 存量警告未变)
- 控制端 (control) 服务本身只负责配置与后端生命周期, 手动唤醒属于运行时操作, 因此不在 control 端增加代理路由, 页面与 CLI 均直连后端 API

幂等记录仍为进程内; 路由歧义体验与更友好的拒绝原因文案未进一步扩展。

## P1 回复引用与 @发送者策略

- 新增 `reply_with_quote` / `reply_with_mention` (默认关闭, 布尔校验), 允许群级覆盖, 时段规则禁止修改; MessageEvent.decorate_reply 在调用适配器前为群聊回复前置 Reply/At 并在 @ 后补空格
- 已有 Reply 或同发送者 At 不重复, 对他人的 @ 不受影响; 私聊、无来源消息 ID (prompt 手动唤醒) 不加引用; 流式只装饰首个非空块并跳过空块, 长消息拆分后只有首块携带引用/@; 原消息链不被修改
- 装饰位于事件层, 覆盖 scheduler 兜底、错误反馈、插件显式 send 与流式降级; 适配器主动发送不经过事件, 保持无装饰
- 前端补两个开关与布尔归一化; 配置示例与 platforms.md 同步
- 新增 `tests/unit/test_reply_decoration.py` 14 passed; 事件/OneBot/调度/手动唤醒/覆盖/Misskey 相关回归 124 passed; 前端 vitest 7 passed, lint/tsc 通过, platform-policy 浏览器回归通过; 修改模块 Pyright 0 errors / 0 warnings

引用回源 (get_msg 填充 Reply) 与输入投影仍待下一批; 错误反馈是否引用目前与普通回复共用同一开关, 尚未单独配置。

## P1 引用回源与模型输入投影

- PlatformAdapter 新增 `fetch_quoted_message` 默认 None; OneBot 实现复用 get_msg 并发槽位与 5 秒/64 KiB 预算, 核验账号、群或私聊双方归属, 白名单收紧后拒绝, 不递归不下载附件
- 新增 `pipeline/input_projection.py`: `resolve_quotes` 在唤醒与限流之后填充顶层首个 Reply 字段, 每事件最多一次回源; `project_input` 组装 `[引用 … 的消息: …]` 标记、合并引用内媒体 (上限 4)、截断引用文本 (2000 字符), 失败时标记不可获取并保留当前问题
- scheduler 在 Step.4 接入, 仅含引用的已唤醒消息也进入模型; `quote_lookup` 布尔配置 (默认开启) 支持群覆盖, 前端开关与归一化, 配置示例/文档同步
- 新增 `tests/unit/test_quote_projection.py` 13 passed (端到端 UserCall 携带引用上下文, 未唤醒不回源, 跨群/外账号/私聊他人拒绝, 超时/超长/关闭/白名单, 媒体与文本预算, 不递归, 引用机器人标注且不当作唤醒); 相关管线/OneBot/路由/运行时回归 156 passed; 修改模块 Pyright 0 errors / 0 warnings

- 补充 `wake_on_quote_self` (默认关闭, 可群覆盖): 唤醒阶段用同一回源预算确认被引用者为机器人后视为明确唤醒, 引用他人/回源失败不唤醒不耗额度, 回源结果复用于投影; 前端开关/配置/文档同步, 测试增至 16 passed

转发 (forward) 入站回源与合并转发发送分流留在 P2。

## P2 通知/请求事件分发基础

- 新增 `platform/notices.py`: NoticePayload 类型化载荷与稳定去重键, PlatformEventHub 订阅/注销/通配、有界 TTL 去重 (4096/120s)、独立小任务分发 (至多 64 并行)、统计与关闭; `current_hub()` 供插件在工厂中订阅
- OneBot `_handle_notice/_handle_request` 归一为 PlatformEvent 经 emit_event 派发, 不入消息队列; `notice_types` 列表过滤 (校验最多 64 项), 群通知受白名单限制, 外账号事件计入 ingress.account
- BackendManager 创建与替换适配器时注入同一 hub, 关闭时 close 并清除进程级引用; 健康响应新增 `platform_events` 统计
- 前端订阅类型多行输入与归一化, 配置示例/文档同步
- 新增 `tests/unit/test_platform_notices.py` 13 passed (载荷收窄/外账号拒绝, 去重键, 单次派发与去重, 无订阅计数与异常隔离, 并行上限与关闭, OneBot 过滤与白名单, 不进入消息管线, 后端装配); 相关运行时/手动唤醒/管理回归 50 passed; 修改模块 Pyright 0 errors / 0 warnings
- 修复顺序依赖缺陷: `test_multi_adapter_routing.py` 两个用例调用 `_init_platforms` 后未 `stop`, 进程级 hub 泄漏导致本文件装配用例在全量运行时失败; 已补 try/finally `stop` 清理, 本文件另加 autouse 复位 fixture 防御; 修复后完整单元测试 1695 passed / 19 skipped / 0 failed (164 秒)

撤回事件使引用缓存失效目前无需处理 (引用回源不缓存); 群文件上传 notice 到附件事件的转换与群管理工具留待后续批次。

## P2 合并转发入站与出站分流

- 入站 `forward` 段映射为 `Forward(id, nodes=None)`, 实现内联 `content` 时直接解析节点; `parse_forward_nodes` 归一 `type/data` 包装与直接字段两种形态, 兼容 `content`/`message` 与字符串正文, 每条至多 20 节点, 嵌套转发保留占位不再展开
- 适配器新增 `fetch_forward_message` (基类默认 None): `get_forward_msg` 与引用回源共用 4 并发槽位, 5 秒/256 KiB 预算, 核验机器人账号与群范围, 兼容 `messages`/`message` 响应字段
- `input_projection.resolve_forwards` 每事件最多回源 2 条顶层转发, `forward_lookup` 布尔配置默认开启可群覆盖; 投影渲染 `[转发消息 N 条: - 昵称: 摘要]` 块, 单条截断 2000 字符, 转发媒体与引用共享 4 个预算, 未解析保留 `[转发]` 占位; scheduler Step.4 接入
- 出站 `split_forward_turns` 在 Node/Nodes 边界拆分, 普通段与转发段按原顺序分送 (Plain A/转发 B/Plain C), 不隐式整链包装; `send_private/group_forward_msg` 返回 message_id 计入回执, 动作未找到 (retcode 10002/1404) 降级为 `flatten_forward_nodes` 分段发送, 其他动作拒绝不重试; 转发段复用同一逻辑回复执行权与失败即停止语义
- 前端补 `forward_lookup` 开关与归一化, 配置示例与 platforms.md 同步
- 新增 `tests/unit/test_onebot_forward.py` 26 passed (解析形态/节点上限/嵌套占位, 回源账号/白名单/预算, 投影渲染/共享预算/截断/不回源未唤醒消息, 混合链顺序/私群 API 选择/降级/业务拒绝/无 ID 未知/白名单); 相关管线/出站/引用/事件回归 157 passed; 修改模块 Pyright 0 errors / 0 warnings (顺带修复 input_projection 存量 reportOptionalMemberAccess 1 处); 前端 vitest 69 passed, tsc/eslint 通过; 完整单元测试 1721 passed / 19 skipped / 0 failed (180 秒)

群文件上传 notice 转附件事件仍待后续批次。

## P2 群管理动作与工具插件

- `satrap/core/platform/onebot/admin.py` 新增 `OneBotAdmin` 动作集, 登记 18 个 OneBot v11 标准管理动作 (5 读 + 13 写); 统一 10 秒超时, 群号/QQ 号纯数字校验, 群动作先过实例群白名单, 列表响应收窄白名单字段 (群列表 ≤512, 成员 ≤2048); 错误归一 `UnsupportedAdminAction` (retcode 10002/1404 或方法缺失) / `AdminActionRejected` (业务拒绝) / `AdminActionUnconfirmed` (超时与传输异常, 不假定成功)
- 适配器持有 `admin` 与 `_loop` 字段, `admin_capabilities()` 客户端在连时报全支持否则全不可用, 随 `get_stats` 的 `capabilities` 键暴露; 进程级 `set_current_adapter_manager`/`current_adapter_manager` 访问器在 `_init_platforms`/`stop` 中绑定与清理, 与 `set_current_hub` 同生命周期
- 内置 `group_admin` 插件 (仅 `session_type: platform`) 暴露 18 个同名模型工具; 执行时按 `CallOrigin` 解析来源实例, 不缓存适配器引用; 写操作要求 `write_tools_enabled: true` (默认关) 且可选 `allowed_callers`/`allowed_groups` 逐行收窄, 私聊必须显式 `group_id`; 布尔参数严格校验拒绝真值语义; 读工具 `recovery_policy=retry`, 写工具 `manual`; 同步工具经 `run_coroutine_threadsafe` 桥接到适配器事件循环 (15 秒上限), 循环未就绪直接报错不创建协程
- `platforms.md` 新增"群管理动作与能力矩阵"章节, 逐项登记动作/工具名/读写/参数/响应收窄与重试策略
- 新增 `tests/unit/test_onebot_admin.py` + `tests/unit/test_group_admin_plugin.py` 共 24 passed (参数边界/白名单/字段收窄/动作参数归一/错误三类归一/能力上报, 插件 meta 与定义一致性/权限门槛/私聊显式群/同步桥接/未知平台); 相关回归 (plugin_spec/session_providers/plugin_compatibility/module_loading/notices/multi_adapter/forward) 128 passed; 修改模块 Pyright 0 errors / 0 warnings; 完整单元测试 1745 passed / 19 skipped / 0 failed (184 秒)

## P2 群文件上传通知转附件事件

- `notices.notice_attachment` 将 `notice.group_upload` 载荷归一为 `File` 组件 (name 文件名, file 远端文件 ID, url 实现下载地址可为空), 缺 ID 与 URL 时不生成; 大小与 busid 保留在 `payload.file`; 只携带远端元信息不触发下载, 下载与模型处理留给 P3 的会话触发策略
- 适配器 `_emit_notice` 把附件放入事件 `extras["attachment"]`, 事件仍走 `notice.group_upload` 轻量分发路径, 不进入消息管线; platforms.md 通知章节同步, 并修正"管理工具待后续批次"的过期表述
- `tests/unit/test_platform_notices.py` 新增 3 用例共 16 passed (仅 group_upload 归一/缺字段与无远端句柄容忍/适配器事件携带附件且不进队列); 修改模块 Pyright 0 errors / 0 warnings

P2 全部完成, P3 文件提取与 ASR 待后续批次。

## P3 ASR 基础: APICall/ASRCall, 配置层, 控制端点与前端

- 新增 `satrap/core/APICall/ASRCall/` (base/sync/async_/utils/__init__), 与 LLMCall/EmbedCall/ReRankCall 同层: OpenAI 兼容 `audio.transcriptions.create`, ASR/AsyncASR 对称入口, `prepare_audio_input` 校验后缀与大小, `parse_transcription` 归一为 ASRResponse(text/model/language/duration), suppress_error 语义与其他 API Call 一致; `build_asr_from_config` 由 ASRConfig 构造
- ASRConfig 加入 core/type; ModelConfigManager 新增 asr 增删改查与脱敏列表; ModelConfigService 支持 asr 目标并提供 `test_asr_config` (读取已存密钥, 8 MiB 上限, 不落临时文件, 请求后关闭客户端); control 新增 `POST /config/models/asr/<name>/test` (base64 音频, 请求体上限双倍); 插件 config_schema 与 plugin-model-options 识别 asr 类型
- CLI `satrap model` 四个子命令 choices 加入 asr, cmd_model 映射完成; 文档 `getting-started/configuration.md` 补 ASR 字段与测试说明
- 前端: types/control/model API 与 store 增加 asr, Models 页新增 ASR 标签、表单字段、卡片「测试转录」按钮与 AsrTestModal (文件选择 → base64 → 后端测试, 4xx 错误保留服务端原因, 成功显示文本/模型/语言/时长/耗时); `testAsrConfig` 把结构化 HTTP 错误转为 ok=false
- 新增 `satrap-ui/e2e/asr-models.mjs` (`npm run test:e2e:asr`) 并实际执行通过: 新增配置写入、密钥不回显、测试失败与成功两种呈现, 请求不携带密钥; 截图已查看
- 测试: `test_asr_call.py` 13 passed (接手模型编写), 新增 `test_model_config_service.py` 2 用例 (asr CRUD/脱敏/客户端构造, 测试端点使用已存密钥并拒绝空/超限/缺配置、关闭客户端), CLI 解析新增 1 用例; 相关 61 passed; 前端 vitest 69 passed, tsc/eslint 通过; 修改模块 Pyright 0 errors / 0 warnings (cmd_model 2 警告为存量)

本批未调用真实 ASR 服务。语音 Record 组件进入内容补全层并投影到模型输入, 以及文件附件提取, 留在下一批。

## P3 语音转写与文件正文补全

- 新增 `pipeline/attachments.py`: `resolve_attachments` 在引用/转发之后处理顶层 Record/File (每事件 ≤4), 经 `safe_async_get` 受限下载 (http/https, 私网需 `media_trusted_hosts` 登记), 语音按 `asr_model` 解析 ASRConfig 后调用 AsyncASR (16 MiB / 60s), 文件经临时文件 + 线程池 `extract_text` (32 MiB / 20000 字符), 临时文件登记到事件由其清理; 结果类型化 AttachmentResult, `render_attachments` 生成资料块, 所有失败降级为标记不阻塞
- 转写结果冻结到 Record.text 避免重复调用; scheduler 由 BackendManager 注入 `asr_resolver` (来自 ModelConfigManager), 仅含附件的已唤醒消息也进入模型; `project_input` 新增 attachments 参数与 attachment_status
- 策略校验新增 `asr_model` (≤128 字符)、`attachment_extract` (布尔)、`media_trusted_hosts` (≤32 主机名); 前端平台表单提供 ASR 配置下拉 (读取已保存 ASR 列表)、附件提取开关、媒体主机多行输入及归一化; 配置示例与 platforms.md 同步
- 新增 `tests/unit/test_attachments.py` 14 passed (转写与投影/冻结复用, 未绑定 ASR 与配置缺失 disabled 且不下载, 404/私网/file 协议/格式不支持降级, 文件提取与临时文件清理, 不支持类型/关闭/数量上限 partial, 端到端 UserCall 携带转写, 未唤醒不下载, 渲染与解析器, 策略校验); 相关管线/引用/转发/调度/运行时回归 146 passed; 修改模块 Pyright 0 errors / 0 warnings; 前端 vitest 69 passed, tsc/eslint 与 platform-policy 浏览器回归通过

本批以受控替身验证下载与转写协议路径, 真实 ASR/LLM 调用留在 P4 验收记录。音频格式转换当时未实现, 已在 2026-09-22 语音转写批次补齐 (见文末)。

## P4 真实模型与 SnowLuma 汇总验收

按 [主方案](issue-10-plan.md) 第 6 节三层验收记录, 凭据仅在运行时从 `.toolkit` 读取, 未写入版本库、文档、测试夹具或命令行。

- 真实 ASR + LLM 全链路 (2026-09-21): 用系统 TTS 生成 4 秒中文 wav (211 KB), 经本地回环 HTTP 作为 OneBot `record` 附件 URL (`media_trusted_hosts` 登记 127.0.0.1), 走原始群消息 payload → OneBotAdapter → PipelineScheduler → `resolve_attachments` → 真实 SiliconFlow XingChenASR 转写 → 投影进 UserCall → 真实 DeepSeek 兼容接口 `AsyncLLM.chat` → 回复回传 `send_group_msg`。转写结果 "今天天气不错 / 我们一起去公园散步" (分段说话人格式, 个别字有识别误差), `attachment_status=resolved`, 模型回复为一句相关中文并成功回传。结论: 语音内容进入后续模型输入, 真实 LLM 回复经出站路径送达
- SnowLuma 实际网络通信: 复跑 `python -m scripts.probe_snowluma --installation F:\other\SnowLuma-v1.14.17-win-x64`, 错误 token 403、Universal 握手、2 次入站往返、4 次乱序回包关联、断线重连全部通过, 包摘要 `79732efb…0981` 与首次记录一致; QQ 侧仍为模拟, 不启动 QQ
- 协议与模拟层: 引用/转发/通知/管理动作/上传附件/文件提取由单元与协议测试覆盖 (受控替身), 见前述各批次
- 前端: `npm run build` 成功, 构建产物含 ASR 模型管理与平台策略表单; vitest 69 passed, tsc/eslint 通过, Playwright `platform-policy`/`manual-wake`/`asr` 脚本各批次显式执行通过; 后端以 `satrap-ui/dist` 托管构建产物, 仓库与发行内容不含 SnowLuma
- 完整回归: `python -m pytest tests/unit -q` 1778 passed / 19 skipped (跳过为显式集成开关、Windows 符号链接权限等既有原因)

未完成或明确不在本轮范围: 真实 QQ 全链路 (方案裁定为模拟); SnowLuma 发行版更新后探针挂接点需复核。音频格式转换已于 2026-09-22 补齐 (见文末)。以上不影响议题内已裁定范围的关闭。

## 2026-09-22 代码审计与整改

对 fix/issue10 全部改动做三视角只读审计 (管线/唤醒, 平台/OneBot/出站, 后端/配置/前端), 方案与逐项验证见 [issue-10-audit-2026-09-22.md](issue-10-audit-2026-09-22.md)。6 批提交 (`d1d4116`…`265db30`) 修复 3 项严重 (群管理写开关不可开启, 平台 reload 异常路径自毁, 空消息 IndexError)、20 项中等与十余项低优先级缺陷, 新增平台入站/配置面 benchmark 并完成热路径优化 (每消息 −58%, 满窗 observe −96%, 50 平台 reload −85%), 清理死代码并合并 5 份模型类型映射。对外行为变化: 附件下载默认校验 TLS (新增 `media_insecure_tls`), `enable_private/enable_group` 保存时校验布尔, 手动唤醒参数错误返回 400。收官回归 1787 passed, 前端全绿, SnowLuma 探针复跑通过。

## 2026-09-22 语音转写来源与格式兜底

方案见 `issue-10-audio-convert-plan.md`。事实基础: SnowLuma 1.14.17 上报的 record 段文件名为 `<md5>.amr` 但内容是 SILK v3, PyAV 不含 SILK 解码器; SnowLuma/NapCat 提供 `get_record out_format` 服务端转码与 `fetch_ptt_text` 原生转写。

- `pipeline/audio_convert.py`: `probe_audio` 按魔数 (优先 SILK 签名, 再 amr/wav/ogg/flac/mp3/webm/ftyp) 与扩展名判定编码, 返回 accepted/convertible/reason; `convert_to_wav` 用 PyAV (延迟导入, 缺包缓存为 None) 解码重采样为 16 kHz 单声道 s16 wav, 超过 `max_seconds` 抛 `AudioTooLong`
- `OneBotAdmin` 新增只读动作 `get_record(file, out_format, max_bytes)` (校验 out_format 枚举, base64 解码, 大小双重上限) 与 `fetch_ptt_text(message_id)` (25 秒超时); 登记 `ADMIN_CAPABILITIES` 但不暴露为群管理工具
- `attachments.py`: 新增 `voice_transcribe` (`off`/`asr` 默认/`platform`/`asr_then_platform`); `asr` 路径 `_fetch_voice` 三级: `get_record` → 下载+探测 → 线程池本地转码 (`AUDIO_MAX_SECONDS=300`); 移除下载前的扩展名白名单, 按真实内容判定; `render_attachments` 对 `silk_needs_platform_transcode`/`av_missing`/`audio_too_long`/`platform_transcribe_unavailable` 给出具体文案
- 策略校验新增 `voice_transcribe` 枚举; 热更新键补入 `asr_model`/`voice_transcribe`/`attachment_extract`/`media_trusted_hosts`/`media_insecure_tls` (均为逐事件读取, 无需重建实例)
- `ModelConfigService.test_asr_config` 与入站共用探测: SILK/未知格式返回明确 400 文案, amr 本地转码后附 `converted_from`; 前端 ASR 测试弹窗显示转码提示, 平台表单新增"语音转写来源"下拉, `adminMigration` 默认值 `asr` 不写入配置
- `setup.py` extras `audio = ["av>=15,<19"]`; README/config.example/platforms.md 同步

## 2026-09-23 异常传播与日志覆盖整改 (五批收官)

依据 [异常/日志审计](issue-10-exception-logging-audit-2026-09-22.md) 与 [整改方案](issue-10-exception-logging-fix-plan.md), 按批次 1-5 全部落地, 逐项状态映射见审计报告文末"整改状态"。

- 批次 1 `fd9a4af`: 平台初始化/启动/停止逐实例隔离, 坏配置不再拖垮整个后端; `_run_task` done callback 把主循环异常退出与静默返回置 ERROR; health 暴露 `adapters_errored` 与逐平台 failed 状态; 替换实例回滚独立兜底并全程留日志
- 批次 2 `282426c`: WakeTimers 到期复查提交兜底, 复查事件按剩余冷却重排不再零延迟忙循环; 四个 aiocqhttp handler 顶层兜底 (单条坏事件只计数不置 ERROR); OutboundTurns 区分外部取消与 close 取消; 顺带修复既有 benchmark frequency 场景设计缺陷
- 批次 3 `0600cd6`: admin/outbound/group_admin 零日志补齐, 发送失败回执/连接绑定/回源降级/reload 失败/手动拒绝/两处兜底 500 全部落日志, 高频路径限频 (`_warn_once`); 顺带清零批次 2 引入的 `type: ignore` 与测试辅助函数类型
- 批次 4 `e7e11ea`: 无效平台配置告警只打 id/type, ASR 错误日志只记类型/状态码/request_id 不落响应体, 探针脱敏补 URL 编码形式; 敏感字段回归测试 (caplog 不含 token/响应体)
- 批次 5 `b4b3f64`: 配置持锁读写移出事件循环 (`to_thread`), `_replace_with_retry` 扩 errno 白名单; 发送路径 PermissionError 归一为回执 (原"首块上抛"契约废止, 两处测试改断言); json 段非对象降级不丢消息; 回源 time 非数字不丢消息; admin 时长类型校验前移; ASR 客户端关闭异常不覆盖转写结果; 通知分发任务异常不再逃向 GC; 等 14 项

验收: 改动文件 pyright 与 HEAD 基线逐项 diff 无新增 error/warning; benchmark 无回退; SnowLuma 1.14.17 探针复跑通过且实际观察到连接日志; 坏端口平台真实冒烟 (后端保持启动, /api/health 报告 failed, 正常关闭); 全量单测 1849 passed / 19 skipped。对外行为变化: 群范围外主动发送由抛 PermissionError 改为返回 `failed/target_unavailable` 回执。自动重连与 FileLock 异步化按方案裁定不做。

## 2026-09-23 残留复核整改 (A/B 两批收官)

依据独立复核意见 (写开关 fail-open / pyright 全库口径 / TLS 全局生效 / 附件总预算 / 事件循环阻塞 / 3.10 超时兼容等) 出具 [修复方案](issue-10-residual-review-fix-plan.md), 批次 A `eb3b712` 与批次 B `8f8f8ae` 全部落地, 逐项状态见方案文末"实施状态"。

- N1 写开关 fail-open 修复: `ConfigField.validate` bool 宽松分支改显式映射 ("false"/"0"/"no"/"off" → False), 无法识别回退默认值并告警; 经 `load_global` 加载的字符串 "false" 不再被 `bool()` 判真
- `media_insecure_tls` 收敛: 仅对 `media_trusted_hosts` 登记主机关闭校验, 公网下载始终校验 (原全局生效)
- 附件事件级总预算 `ATTACHMENT_TOTAL_TIMEOUT=90s`: 耗尽后其余附件标记 `attachment_budget_exceeded` 不再获取, 最坏路径 ~320s 收敛到 90s+单项收尾; 临时文件写入与 `get_record` base64 解码移出事件循环
- pyright 全库 48 error 清零 (含 P2 批次遗留的 14 个测试文件错误), 当前 0 errors / 1586 warnings; `get_tools` 增 overload 区分同步/异步返回
- N2 Python 3.10 兼容: `except asyncio.TimeoutError` (http_api 日志流 / control_server 兜底)
- 公网明文 http: 新增 `media_plaintext_http` 开关 (默认关闭, 平台设置界面勾选后写入配置文件, 已入热更新键); 用户裁定由"默认放行+文档"改为"把选择权给用户"; `media_trusted_hosts` 登记主机不受限
- `get_record` source 形态校验 (≤512 字符, 无控制字符); `probe_duration` 时长探测 (wav 标准库 / 其他 PyAV 容器时长), 免转码路径超 300s 拒收; `comp.text` 冻结截断至 TRANSCRIPT_LIMIT
- 引用/转发/附件解析移入会话锁 claim 成功之后: 并发未认领批次不再浪费下载与转写, 锁持有上限即 90s 附件预算; 平台上报文件名消毒 `_safe_display`; FORWARD_NODE_LIMIT 说明字符串归位 (N3)

验收: 全量单测 1862 passed / 19 skipped; pyright 全库 0 errors / 1586 warnings (不增); 前端 tsc 0 error + vitest 69 绿; 附件 benchmark 复跑 (get_record 路径 +0.5ms 为 to_thread 预期开销); 入站 benchmark 在 B6 重排后无回退 (结果存 `tests/benchmark/results/platform/after-residual.json` 与 `audio-after-residual.json`)。至此独立复核意见全部闭环, fix/issue10 待合回 main。

## 目标审计与复审 (2026-09-23)

[issue-10-goal-audit-2026-09-23.md](issue-10-goal-audit-2026-09-23.md) 以"完整目标验收"为口径审计 (基线 `e683fce`), 结论不通过: 可复现缺陷 A1-A4 (跨群归属核验缺失/自动参与覆盖补全内容/取消清理竞态/无统一输入预算) 与交付缺口 A5-A8 (手动请求持久化/ASR 引用检查/能力状态语义/前端范围)。[issue-10-goal-audit-review-2026-09-23.md](issue-10-goal-audit-review-2026-09-23.md) 逐项复审全部属实, 并将 File 出站分流与 talk_value 映射两条证据不足项升级为"确认缺失"。用户裁定: 该两项补实现 (File 不断言现状必失败; talk_value 不做群活跃度采集), 历史验收记录与发行包核验搁置; 其余 10 项修复方案见 [issue-10-goal-audit-fix-plan.md](issue-10-goal-audit-fix-plan.md) (四批次, 待实施)。方案经 [issue-10-goal-audit-fix-plan-review-2026-09-23.md](issue-10-goal-audit-fix-plan-review-2026-09-23.md) 复审, R1-R8 八条意见全部采纳并修订 (A3 登记改由子任务终态驱动; A2 按事件类别区分合成/真实消息; A1 flag 原子占用与四态; A5 接受事务/降级/发送尝试记录; talk_value 改为配置来源接入 WakeWindow.decide; File/新工具走公共发送路径与连接代次能力缓存; A4 顶层媒体实际裁剪; A8 试算共用决策逻辑与决策点拒绝记录)。

### 目标审计批次 1 (A1/A2/A3)

- A1: 新增 `onebot/request_registry.py` — 群/好友请求分表的有界 flag 登记 (512 条/10 分钟), 状态 available→executing→completed/unknown 单向迁移, 重复入站不重置已占用状态; `adapter._handle_request` 在订阅过滤前登记 (先验 self_id 归属); `recall_message` 执行前 `get_msg` 回源验群并在回源后复查群范围; `handle_group_request`/`handle_friend_request` 先原子占用再动作, 超时/传输异常记 unknown 不可重放
- A2: scheduler 合并替代覆盖——真实当前消息保留完整投影并按 request_id 去重后以"[先前窗口消息 N 条]"追加批次; 窗口类合成事件 (无 prompt 手动唤醒/定时补偿) 以实际 claim 结果为唯一输入, 不带入陈旧正文
- A3: OutboundTurns 登记清理改由子任务 done 回调驱动 (`_settle` 幂等), 调用方取消时有界等待 (CANCEL_SETTLE_TIMEOUT=5s) 但不摘除未终态登记, 未终态任务继续占用容量且同目标锁保留排队; close 有界等待并明确报告未终态数量
- 测试: 撤回归属/回源失败/复查收紧; flag 未登记/异群/异类/重放/超时 unknown/并发唯一胜出/群友分表/重复入站不重置; 登记 TTL 与容量; 真实消息合并/手动窗口/定时补偿三类输入组装; 取消清理顺序/超时保留/重复取消/close 覆盖清理中任务

验收: 全量单测 1886 passed / 19 skipped; pyright 全库 0 errors / 1532 warnings (开工基线 0/1532, 不增); 入站 benchmark 与即时干净树对照差 ≤±3.3% (旧基线整体漂移为机器环境差异, 干净树复跑同幅, 结果存 `goal-batch1.json`/`goal-batch1-clean-tree.json`)。

### 目标审计批次 2 (A7 能力语义/File 出站分流/A4 输入预算/talk_value 映射)

- A7 能力四态: `adapter.admin_capabilities()` 返回 unavailable/unknown/supported/unsupported。连接状态只取 meta 事件 (lifecycle connect/disable 与 heartbeat), 不再以 `_bot is not None` 充当; 反向 WS 未挂 meta handler 时全部回 unknown (已知局限, 不主动探测)。`note_action_outcome(action, supported)` 被动学习: `_call` 缺动作、`_send_forward`/`_send_file`/`fetch_forward_message` 缺失回退前记 unsupported, 成功记 supported; 按连接代次 (`_connection_generation`, lifecycle connect 时 +1) 失效并清空。心跳停滞 (>90s 且已见 ≥2 次心跳) 判 unavailable。friendly→协议动作映射 `_CAPABILITY_ACTIONS`, 多动作条目 (send_forward/upload_file) 取"任一 unsupported 即 unsupported, 全部 learned supported 即 supported, 否则 unknown"
- 补三工具 (均走公共发送/回源路径): `group_admin_get_message` (get_msg 验群+回源后复查群范围+64KiB 截断+字段收窄), `group_admin_get_forward` (forward_id 校验后委托 `fetch_forward_message`, 失败返回动作结果未知), `group_admin_send_forward` (nodes 1..30 项/单项 1..2000 字符/昵称 ≤30, uin 取 bot_self_id 非十进制即拒绝, 经 `_adapter.send_message` 走 OutboundTurns)
- File 出站分流: `split_forward_turns` 遇 File 组件冲刷文本/转发累积器并按原序发出 ("file", [File]) 轮次; `_send_file` 用 upload_group_file/upload_private_file (source 取本地路径剥离 file:// 并 abspath, 否则 url/base64 原样; name 缺省取 basename); 缺动作 (10002/1404) 记 unsupported 并 `_send_file_fallback` 经 `_send_chunk` 发文件段, 回执 reason 前缀 `fallback_unverified` (聚合回执亦重标注, 明确不构成送达证据); 同连接代次已失败动作不再重试直达回退
- A4 统一输入预算: 平台级 `input_text_limit` (默认 20000, 1..200000) / `input_media_limit` (默认 8, 1..32), 不进 GROUP_KEYS (群/时段不可覆盖)。`ProjectionBudget.assemble` 固定优先级 正文(保前缀) > 来源标记头 > 资料块内容; 每块固定成本=头+尾+1(分隔)+1(“…”预留, 仅有内容时), 完整放下可借预留位 (`allowance = limit - used + 1`); 笔记 body_budget_truncated / `{kind}_budget_truncated` / `{kind}_budget_dropped` / `top_media_truncated` (顶层媒体先实际裁剪, 余量供引用/转发共享)。转发块改为按顶层原始顺序渲染 (旧实现前插导致逆序)。scheduler 窗口块消耗剩余预算, 合成路径同额截断, 笔记 window_budget_truncated / window_budget_dropped (dataclasses.replace 冻结投影)
- talk_value 映射: 平台/群/时段级 `wake_talk_value` (0..1 有限浮点, 入 AUTOMATIC_KEYS); `TALK_VALUE_LADDER = ((1.0,1),(0.75,2),(0.5,3),(0.35,5),(0.2,8),(0.1,13))`, 低于 0.1 取 21, 0 取 `NEVER_TRIGGER_THRESHOLD = sys.maxsize`; `map_talk_value_threshold(talk_value, base=3)` 仅 0.5 档用 base 替换。仅接入 `WakeWindow.decide` 频率分支 `_message_threshold` (显式 wake_message_threshold > wake_talk_value 映射 > 默认 3), reason 记录来源; 0 不阻断必要性模式、@ 提及与手动唤醒
- SnowLuma 1.14.17 载荷核对 (发行包 bundle `config-GJCFWjtq.js`, sha256 79732efb…): `upload_group_file` 参数 `{group_id, file, name="", folder="", folder_id="", upload_file=true}`, 返回 `{file_id}` (无 message_id); `upload_private_file` 参数 `{user_id, file, name, upload_file}`; `f.file()` 接受本地路径/URL/base64。与 `_send_file` 实现一致, 文件上传成功回执以 file_id 为据 (`file_uploaded`)
- 探针复跑通过 (`scripts/probe_snowluma.py` 驱动端增补 upload_group_file 桩与 ACTIONS 序列断言): Plain/File/Plain 混合链动作序列与次序精确断言 (文本段经 send_group_msg, 文件段经 upload_group_file); 单段拒绝 (retcode 1200) 得 partial 回执; 反向 WS 动作中途断连得 unknown 回执——实测 aiocqhttp `ResultStore.fetch` 默认 `api_timeout_sec=60` 才把悬空动作结清为 NetworkError, 即掉线动作的"未知"结论最坏滞后约 60 秒 (已注于探针注释); 同代次第二次缺动作上传不重试直达回退 (两次均成功且带 fallback_unverified)
- 测试: 能力四态/连接代次重置/异账号拒绝/心跳停滞/disable; get_message 收窄与跨群拒绝/get_forward 委托与失败/send_group_forward 公共路径 spy 与 nodes 校验; File 分流次序/私聊/缺动作回退与代次/拒绝部分成功/未知/空文件; ProjectionBudget 记账 (含恰好放下边界 10→全量与 9→截断) 与平台级限定; 顶层媒体裁剪与共享余量; 窗口块预算集成; talk_value 阶梯/单调/base/显式优先/零语义/必要性不受影响/群覆盖 resolve/校验拒绝

验收: 全量单测 1943 passed / 19 skipped; pyright 全库 0 errors / 1532 warnings (开工基线 0/1532, 不增); SnowLuma 1.14.17 探针复跑通过; 入站 benchmark 双跑对照 (批次树 180.8/182.3µs vs 干净树 172.4/178.2µs, 干净树自身漂移 +3.4%, 各场景区间重叠, 判机器环境漂移非批次回退, 结果存 `goal-batch2.json`/`goal-batch2-clean-tree.json`/`goal-batch2-rerun.json`)。对外行为变化: 管理端能力状态语义改为四态 (原 supported/unknown 二态); 合并转发块在投影中按原始顺序渲染; 新增三个群管理工具 (读二写一)。

### 目标审计批次 3 (A5 手动请求持久化与发送尝试记录/A6 ASR 引用检查)

- A5 持久化存储 `satrap/core/pipeline/manual_wake_store.py`: JSON 单文件 (FileLock + NamedTemporaryFile/os.replace 原子写), 键 `adapter_id\nrequest_id` (request_id 拒绝含换行)。请求状态 accepted/executing/sent/partial/failed/unknown, 尝试状态 submitted/sent/partial/failed/unknown; SETTLED={sent,partial,failed}, unknown 属未决永不容量淘汰 (仅 7 天保留期清理)。容量 请求 1024/尝试 2048, 满时先扫超期 settled, 再按 updated_at 最旧轮转 settled 至单代 `.1` 归档 (2048/4096), 归档满或未决占满则 ManualWakeStoreError(capacity) 拒绝且不动未决记录。文件损坏改名 `.corrupt-<ts>` 隔离并进显式降级: 拒绝依赖去重的新请求, 发送/查询等其余功能照常, 同路径可重建。启动清扫: accepted/executing→unknown/restart_unconfirmed, submitted 尝试 (含逐段)→unknown, 不自动重发; update_request 终态守卫 (settled/unknown 不可改写)
- A5 接受事务 (BackendManager.wake_platform): `_wake_accept_lock` 内完成 内存查重→降级拒绝 (store_unavailable)→存储查重 (同指纹 already_pending+存储状态/异指纹 request_id_conflict)→适配器再校验→队列预检→`store.accept_request` (to_thread, capacity→request_capacity/其余→store_unavailable)→内存登记→入队; 入队失败回滚内存登记并落 failed/queue_full 后按 queue_full 拒绝——accepted 仅在持久化成功后返回, 不留假占位
- A5 调度器单写者终态裁决: Step.6 会话调用前落 executing; `manual_detail` 跟踪错误出口 (rate_limited/empty_message/llm_timeout/pipeline_input:T/pipeline_error:T); finally 按 取消→failed/cancelled > manual_detail→failed/detail > 无回执→failed/no_response > `_RECEIPT_TO_REQUEST_STATUS` 映射回执状态 落终态, 反馈消息发送不再污染请求结论。`clear_manual_wakes(adapter_id)` 同步 `store.adapter_stopped`: accepted→failed/platform_stopped, executing→unknown/stopped_unconfirmed
- A5 发送尝试记录: 平台侧结构协议 `SendAttemptRecorder` (receipt.py); ABC `send_message(..., *, request_id="")`, misskey 接受并忽略, event.send 传 `call_origin.request_id`。OneBot `_send_message` 统一走分块循环 (单块快路径移除), `_plan_send_segments` 按 转发=1/文件=1(空文件跳过)/普通=逐分块 展开段计划, `_turn_signature` 只留类型摘要散列+字符数 (不落正文); I/O 之前 to_thread 落 submitted (失败告警继续发送不记录), 完成后按回执逐段标 sent/failed/unknown, 未到达段标 skipped, 混合链聚合 partial
- A5 状态查询: `BackendManager.manual_wake_status(request_id, adapter_id=None)` (store_unavailable/store_degraded/not_found/记录字段); HTTP `GET /api/platforms/wake/{request_id}[?adapter_id=]` → 400 invalid_request_id / 404 not_found / 503 store_unavailable|store_degraded / 200 记录
- A6 引用检查 `satrap/core/config/asr_references.py`: 三来源汇总 平台 settings.asr_model (配置文档或传入列表) / 插件全局配置 (scan_plugin_dirs→parse_config_schema 取 type==asr 字段→`.satrap/plugin_config/<name>.json` 值匹配) / 会话覆盖 (各平台库只读 sqlite `mode=ro`, namespace 前缀 `plugins.`, config_json 字段匹配; 平台 id 列表含 chat/local 内置); 每条引用带 summary 与 kind/定位字段。`ConfigInUseError(ValueError)` 携带 references, 在 remove_asr_config 与 update_named_config (仅 asr 且改名) 强制; 检查器以 Callable 注入 ModelConfigManager (框架层不依赖平台/插件知识), 扫描异常 fail-closed 按被引用拒绝 (scan_error)。控制端 DELETE → 409 `{code: config_in_use, references}` (先于 ValueError→400 捕获); BackendManager._init_model_config (传 platforms) 与 CLI cmd_model._init_mgr (传 platforms+默认布局) 完成接线; display/server.py 删除仅 LLM, 不动
- 既有测试逮住两处回归并已修复: ①测试 fake `_RecorderAdapter` 等 4 处 send_message 旧签名 (ABC 新增 request_id 关键字参数) — fake 补签名; ②单块快路径移除后 combine_receipts 给单段失败回执补了 failed_index=0 (旧形状为 None) — combine_receipts 增单段直通保持既有可观测形状 (多段聚合语义不变)
- 测试: 存储单元 (接受/查重/终态守卫; 重启降级 unknown 不重发; 损坏隔离+降级+重建; 容量轮转/归档满拒绝/未决不淘汰; 保留期只清 settled; 尝试段 partial/skipped/终态不重写; adapter_stopped 语义); 接受事务 (跨重启去重 already_pending/冲突 request_id_conflict; 入队失败落 failed/queue_full 且重试返回原状态; 落盘失败拒绝且占位回滚; 并发重复唯一胜出); 管线 (accepted→executing→sent 全链路, 发送中可见 executing; 管线异常 failed/pipeline_error 不误标 sent; 尝试在 I/O 前落盘由假 bot 发送中读文件证实, ActionFailed 第二块得 partial 且段状态 sent/failed/skipped; 降级拒绝新请求但发送照常); 状态路由 200/404/503/400; A6 (已绑定删除 409 且配置仍在; 无引用删除正常; 最后一项重置仅无引用发生; 重命名冲突 ValueError/绑定拒绝/解绑放行; 插件全局+会话覆盖引用清单; 扫描失败 fail-closed; 控制路由 409→解绑→200)

验收: 全量单测 1966 passed / 19 skipped; pyright 全库 0 errors / 1532 warnings (开工基线 0/1532, 不增)。对外行为变化: 手动唤醒 accepted 语义收紧为"已持久化" (落盘失败按 store_unavailable 拒绝); 新增手动唤醒状态查询端点; ASR 命名配置删除/重命名新增引用保护 (控制端 409)。

### 目标审计批次 4 (A8 试算/拒绝记录/前端交付)

- A8 试算 `satrap/core/pipeline/wake_dry_run.py`: `POST /config/wake-dry-run` (控制端)。草稿 settings 经 `validate_wake_policy` 校验, group_id 经 `normalize_group_whitelist` 规范核验 (非法报"group_id 必须是规范的正整数群 ID"), local_time HH:MM 注入当日时刻保证时段规则确定性。事件经一次性 OneBotAdapter + `convert_message` + `MessageEvent` 真实转换 (路由键/组件解析与线上一致), `resolve_wake_settings(草稿, 群, 注入时刻)` 覆写 event.policy_settings。场景循环用隔离 `WakeWindow()` 生产默认值, 基准 now=1_000_000.0: 消息步 `observe`, `{submit: true}` 步 `decide`+`claim(automatic=True)` (真实冷却标记); 显式路径 `evaluate_wake` 探测事件 (at_self 注入 At 组件)。无法判断分支显式返回 `{triggered: None, rule: "undetermined", reason}`: 无探测消息/探测带 quote_self (回源不可得)/场景无消息。上限 MAX_STEPS=64/MAX_STEP_TEXT=1000/MAX_ADVANCE_SECONDS=3600; 不写状态不调模型
- A8 拒绝记录 `satrap/core/pipeline/wake_rejections.py`: `WakeRejection` 冻结数据类 (adapter/session/actor/stage/decision/reason/recorded_at/message_id/request_id/send_status); `WakeRejectionLog` 每适配器 `deque(maxlen=256)` + threading.Lock, list 最新在前 (本机 ISO 时间), 查询上限 256。调度器两个采集点: ①唤醒决策不通过的提前返回 (reason 记 `规则: 原因`, 先于投影, "未执行投影也能查到拒绝原因"验收案例由测试证实: handle_call_async 未被调用且 input_projection 为空而记录已存在); ②限流拒绝 (发送反馈后补 send_status)。`clear_manual_wakes` 同步清空该适配器记录。端点 `GET /api/platforms/wake/rejections[?adapter_id=&limit=]` 注册在通用 `/wake/{request_id}` 分支之前 (避免 "rejections" 被解析为 request_id), 400 invalid_limit
- 前端群/时段编辑器 `utils/wakeOverrides.ts` + `Platforms/WakeOverrideEditor.tsx`: 继承/显式关闭/显式值三态行编辑替换 JSON textarea; 17 字段与后端 GROUP_KEYS/AUTOMATIC_KEYS 对齐 (wake_mode 关闭='explicit', wake_talk_value 关闭=0, 词表/别名关闭=[], 5 布尔仅群级); `fromGroupRows` 丢弃空号/非正整数号/重复号/全继承行, `fromTimeRows` 丢弃非法 HH:MM/起止相同/空设置行; 编辑器持本地行状态 + valueKey 重同步 (不完备行不在 emit 中丢弃导致新行消失)。`fieldState` 数组 offValue 按内容比较 (修复: 解析后的 [] 与定义字面量引用不等, 保存再打开会误显"显式值"——新 vitest 逮住)
- 前端试算面板 `Platforms/WakeDryRunPanel.tsx`: 平台编辑弹窗内嵌, 草稿经 `normalizePlatformSettings` 归一化后作为 settings 发送 (解析失败禁用按钮); 样例消息每行一条按序注入, 可选末尾模拟一次模型提交观察冷却; 渲染有效策略徽章 (wake_* 六项) + 显式唤醒/自动参与/到期复查三行裁定 (触发/不触发/无法判断三态徽章) + 窗口有效正文数与冷却剩余
- 前端脏保护 `hooks/useDirtyGuard.ts`: beforeunload + 平台弹窗 openSnapshot 比对 (`JSON.stringify(formData) !== openSnapshot`), Modal 唯一出口 onClose 经 `guardedClose` 确认 (Escape/遮罩/关闭按钮/取消同路)。核查结论: react-router-dom 6.30 非数据路由的 navigator 上 **block 方法已被 @remix-run/router 移除** (useBlocker 不可用); 应用内路由切换由弹窗遮罩独占保证——弹窗打开时侧栏链接不可点, 点击落在遮罩上即走放弃确认, 遮罩吞掉点击不产生导航; 浏览器前进/后退跳过确认属已知边界 (与改版前一致, 需迁移数据路由才能补齐), 已写入 hook 文档注释
- 前端状态查询 `Sessions/ManualWakeModal.tsx` 重写: accepted/already_pending 不再直接关窗, 内嵌 `WakeStatusPanel` 每 2s 轮询 `GET /api/platforms/wake/{request_id}` 至终态 (sent/partial/failed) 或 60s 超时, 404 not_found/503 store_degraded/store_unavailable 分别提示; `WakeRejectionsPanel` 展示近期拒绝记录 (stage 徽章 限流/决策), 提交与终态时刷新
- FormModal 新增 `type: 'custom'` 字段 (render 注入编辑器/面板); control.ts 增 `dryRunWake`, backend.ts 增 `getWakeStatus`/`listWakeRejections`
- 测试: 后端 21 项 (试算频率+talk_value 映射/群>时段>平台优先级/必要性评分/submit 冷却 49-60s/到期 max_wait/TTL 过期/explicit_only/legacy 成员隔离; 显式提及/唤醒词/未命中; 无法判断三分支; 8 类非法输入; 控制路由 200/400/None; 拒绝记录容量/次序/过滤/清空/限流 send_status/路由); 前端 vitest +21 (wakeOverrides 三态/互转/丢弃规则/JSON 兼容); Playwright 扩展 platform-policy (行编辑器保存载荷精确断言/试算无法判断与不触发两分支/脏保护 Escape 与遮罩点击两处确认流) 与 manual-wake (状态跟踪执行中→已送达/拒绝记录渲染/受理后弹窗保留) 均 PASS

验收: 全量单测 1987 passed / 19 skipped; pyright 全库 0 errors / 1532 warnings (开工基线 0/1532, 不增; 批次内 13 条新增告警已清零——dict 字面量注解/cast 辅助/deque 显式参数化); 前端 tsc 0 error + vitest 90 绿; Playwright test:e2e:platform 与 test:e2e:wake 通过。对外行为变化: 手动唤醒受理后弹窗保留并内嵌状态跟踪; 平台弹窗群/时段覆盖由 JSON 文本改为行编辑器; 新增试算端点与拒绝记录查询端点。至此修复方案四批次 10 项全部交付, fix/issue10 待用户合回 main/推送。

### 目标审计批次 5 (B1 转发来源证明/B2 降级持久化/B4 审批账本)

- B1 转发来源证明 `satrap/core/platform/onebot/admin.py` + `onebot_utils.forward_ids_in_message`: `get_forward_message(group_id, forward_id, source_message_id)` 先 `get_msg` 回源核验来源消息 group_id 属于目标群、`message_type` 为 group、`self_id` 与当前绑定账号一致、回源 message_id 与请求一致 (任一不符 → AdminActionRejected), 再要求 `forward_id ∈ forward_ids_in_message(来源消息顶层组件)`; 嵌套转发不构成授权 (只取顶层, 不递归展开)。核验前记录 `connection_generation()` 与 `bot_self_id`, 回源与真正读取之间各复查一次群范围与连接代次, 变更即 `AdminActionUnconfirmed("连接或账号已变更, 来源证明失效")`。`adapter.fetch_forward_message` 对响应中与本群矛盾的 group_id 直接拒绝 (之前只做"存在即接受")。工具 `group_admin_get_forward` 新增必填 `source_message_id` (缺失即参数错误, 不提供不安全兼容放行), 模型无法填的字段不进参数表
- B2 降级持久化与清单事务 `satrap/core/storage/persist.py` (新) + `satrap/core/pipeline/manual_wake_store.py`: 抽出共用原语 `atomic_write_json` (同目录临时文件 + os.replace) 与 `quarantine_file` (`<名>.corrupt-<YYYYmmdd-HHMMSS>[-序号]`, 保留原始字节)。存储引入 `MANIFEST_VERSION=1` 清单: `expected_files` 三态 (main present/missing, archive absent/present/missing) + `degraded{reason,at}`, 清单与主/归档共用同一事务协调。启动路径矩阵按"清单是否可读 × 主文件/归档/隔离文件是否存在"分流: 清单缺失或损坏但已有数据文件 → 校验后迁移并补写清单 (不当作首次初始化), 无任何数据文件才初始化。损坏处理顺序固定为"先持久记录降级, 再隔离坏文件", 降级标记落盘失败则保留原文件不隔离 (避免下次启动误判为空目录)。记录校验加严: 键身份一致、拒绝空 ID、段状态枚举、`sent` 尝试不得含未确认段、请求/尝试重叠检测 (`strict_duplicates`)。`recover()` 要求主文件存在且校验通过才清除降级, 保留记录不清空; 构造不再抛异常 (标记失败只记日志)
- B4 审批账本防重放 `satrap/core/platform/onebot/request_registry.py`: 新增 `RequestApprovalLedger` (JSON + 清单 + FileLock; 无 path 时为纯进程内账本)。键 `adapter_id\nself_id\nkind\nflag 摘要` (只存 sha256[:32], 不落原始 flag); 每次变更在文件锁内 `_reload_locked()` 重读后判定并原子写回, 锁覆盖完整的"读取-判定-占用-落盘"——两个 store 实例并发占用同一 flag 至多一个成功 (旧实现只锁最终写入, 实例各自的内存表会双双放行)。登记重复入站不改写身份、不改写状态、不刷新可审批时限 (默认 600 秒); 过期转 `expired` 墓碑而不删身份; 归属冲突 (同 key 不同群/子类型/用户) 拒绝登记; 容量达限 (每"实例+账号"4096 / 全表 16384) 拒绝新登记而不是淘汰旧身份; 重启把无法确认的 `executing` 记为 `unknown`。占用落盘成功才返回, `occupy` 的 `LookupError` 文案区分未登记/归属不符/已处理或结果未知/超时限, 审批层转 `AdminActionRejected` 并在任何网络动作之前拒绝。取消 `await asyncio.to_thread(...)` 不撤销已落盘占用 (占用过程无回滚分支), 没有明确成功结果不发网络动作。`RequestFlagRegistry` 降级为门面: 512 条近期缓存只用于跳过重复入站的磁盘访问, 淘汰不影响账本身份; 内存账本无需锁文件 (修正 `_transaction` 对内存账本抛 RuntimeError 的缺陷); `settle` 只在账本落盘成功时更新缓存状态。`BackendManager._attach_request_ledger` 在适配器创建/替换时注入 `.satrap/data/request_ledger.json`, 降级时日志告警
- 文档同步: [运行数据布局](../core/data-layout.md) 新增"后端状态文件" (两个 JSON 的清单/归档/隔离/降级/恢复规则), [平台接入](../platform/platforms.md) 补请求审批的账本语义与反重放边界
- 测试: B1 7 项 (来源消息跨群/账号与消息 ID 不符/转发 ID 不在来源消息中/嵌套转发不授权/回源失败与代际变更/响应 group_id 矛盾/缺 source_message_id 参数错误); B2 `TestPersistentDegradation` 10 项 + `TestRecoveryEntry` 4 项; B4 `TestApprovalLedger` 11 项 + 门面/适配器层 5 项 (含重启后不可重放、降级拒绝且不发动作)。关键反例逐项做了变异验证: 去掉转发 ID 成员检查、去掉占用前的锁内重读、把"缺清单按首次初始化"改回旧行为, 均能让对应测试变红

验收: 全量单测 2023 passed / 19 skipped; pyright 全库 0 errors / 1532 warnings (与开工基线一致, 未新增)。对外行为变化: `group_admin_get_forward` 新增必填参数 `source_message_id` (缺参数即拒绝, 不再只凭 forward_id 读取); 未支持上传动作时的文件兼容回落由 success/`fallback_unverified` 改判 unknown 在批次 6 完成 (同一提交内不计)。

### 目标审计批次 6 (B3 发送状态裁决/B7 file 未确认/B5 扫描完整性/B10 后端诊断)

- B3 发送状态裁决 `satrap/core/platform/onebot/adapter.py` + `satrap/core/pipeline/manual_wake_store.py` + `satrap/core/pipeline/scheduler.py`: 段计划与已提交动作分开表示。发送计划在 I/O 之前落盘为 `planned`; 每段 I/O 前 `mark_segment_submitted` 推进 `submitted`; 回执到达后 `record_segment_result` 立即落该段结果并同时推进下一段 (单次写入); 未尝试段由收尾标 `skipped`。收尾 `complete_send_attempt` 把 `submitted` 转 `unknown`, 由 `derive_attempt_status(segments)` 按段证据归并终态 (全部确认→sent; 确认前缀+明确失败或未尝试→partial; 有未确认段→unknown; 无确认段→failed), 调用方的"未知"只能让结果更保守, 不能把未确认段提升为已送达。适配器侧记录失败不再让段停留在 `planned`: 段标记与段结果双双失败时记入 `gaps`, 收尾按 `untracked` 把这些段转 `unknown` 并给 detail `tracking_incomplete`。收尾归发送子任务所有 (`asyncio.wait_for(asyncio.shield(worker), 2.0)`), 超时或被取消保留已落定段证据、记录保持未终结, 由后续可信确认精化且不重发。`record_send_attempt(..., purpose)` 区分 `business` 与 `error_feedback`; 旧记录缺 `purpose` 按 `unknown` 处理, 只提高保守程度 (`request_send_outcome` 单列 `legacy_confirmed`)。`_adjudicate_manual_request` 用 `request_send_outcome` 归并: 未确认段或历史未知→unknown, 确认前缀+失败/未尝试/管线出口→partial (detail 保留 `confirmed_prefix`), 全确认→sent, 取消且未提交→failed/cancelled_before_send; 进程内 `last_business_receipt` 仅在平台不记录尝试时兜底。`event.send` 新增 `purpose` 与 `require_send_tracking`: 已受理请求要求发送证据, 发送前记录不可用时拒绝业务发送并返回 `unknown`/`tracking_unavailable`
- B7 file 未确认语义 `_send_file_fallback`: 缺上传动作时的兼容回落即使普通消息动作返回 `message_id` 也只记为 `unknown` 并带 `file_delivery_unconfirmed` (首段确认保留, 后续段不再发送); 只有平台明确拒绝该消息动作时才判 `failed`。探针与文档同步更新
- B5 扫描完整性 `satrap/core/config/asr_references.py` + `session_overrides.py` + `edictum/plugin_config.py` + `BackGroundManager` + control/CLI 错误映射: 新增 `AsrReferenceScanError(reason, origin)`, 已存在来源读不出/解析失败/数据库错误/覆盖 JSON 非法/结构版本声明与库不符时不再返回"无引用"; 旧库 (user_version 早于覆盖表版本) 缺表仍属契约允许的空集合。扫描与配置写入共用 `REFERENCE_SCAN_LOCK` (`session_overrides.replace`、`plugin_config.save_global` 同锁), 覆盖"扫描→删除"之间的新增引用。`ensure_override_tables` 在同一事务内只向上写 `PRAGMA user_version`。模型配置删除/重命名时扫描不完整改为独立错误: 控制端 503 `asr_reference_scan_failed` (带 reason), CLI 非零退出, 不再伪装成 `config_in_use`
- B10 后端诊断 `satrap/core/pipeline/wake_rejections.py` + `scheduler.py` + `BackendManager` + `http_api.py`: `WakeRejectionLog` 由"拒绝记录环形"扩展为按请求关联的有界诊断 (每实例 256 个请求 × 每请求 16 条, 同阶段同原因码去重留最新), `WakeRejection` 增补 self_id/status/reason_code/turn_id/attachments/notes。调度器在决策、限流、补全结束、模型结束与发送收尾五个位置就地采集 (纯内存, 失败只记日志); 未被唤醒的事件不产生发送阶段记录。发送阶段只读发送证据: 确认前缀+未尝试→partial, 有未确认段→unknown, 全确认→sent, 无业务尝试也无业务回执→skipped (不把管线出口原因冒充成发送失败)。新增 `GET /api/platforms/wake/diagnostics` (适配器/阶段/请求过滤 + 上限) 与 `/diagnostics/{request_id}` 详情, 兼容保留 `/wake/rejections` (只返回 `wake_decision`/`rate_limit` 两个拒绝阶段, 语义不变)
- 过程中发现并修复两处真实缺陷: ①`wake_rejections.stats()` 的计数键 `records` 与列表端点的 `records` 列表同名, 导致列表接口返回整数 (已改名 `requests_total`/`records_total`); ②`asr_references` 旧库分支漏 import `logger`, 有插件声明 asr 字段时该分支必然 NameError (已修复并补反例测试)
- 文档同步: [平台接入](platforms.md) 新增"发送证据与请求结论""请求诊断"两节, 回执契约与 ASR 引用保护各补一段
- 测试: B3 段进度与裁决 9 项 (I/O 前取消→failed/cancelled_before_send 且无网络动作、首段确认后取消→unknown 且保留确认前缀、模型超时但工具已写出→partial、错误反馈回执不覆盖业务失败、跟踪不可用拒绝业务发送、旧记录缺 purpose 不算业务送达、逐段落盘无重叠 submitted、无证据段不记 sent、收尾超时不显示已送达且不重发 [变异验证: 去掉有界等待即变红]); B7 回落 2 项 (Plain/File/Plain 的确认前缀/未确认文件段/未尝试后缀在回执与持久化两侧都不显示已送达, 同代次能力缓存不重复试错); B5 10 项 (插件元数据/全局 JSON/数据库锁定/覆盖 JSON/声明版本缺表/旧库空集合 + 503 路由 + CLI 退出码 [变异验证: 去掉 logger import 即变红]); B10 15 项 (未投影拒绝/限流/附件失败与发送分开/partial 与 unknown 分离/模型超时/容量与去重/适配器过滤/无正文/列表与详情路由/不可用标记/采集异常不影响管线/普通与手动请求共用阶段)

验收: 全量单测 2056 passed / 19 skipped (另有既有的上传+索引集成用例偶发超时, 单跑通过); pyright 全库 0 errors / 1532 warnings (与开工基线一致, 未新增)。对外行为变化: 文件兼容回落由 `unknown` 统一承载 (`fallback_unverified` 不再出现); ASR 配置删除/重命名在扫描不完整时由 409 改成 503 `asr_reference_scan_failed`; 新增诊断列表/详情端点, 旧拒绝记录接口只返回拒绝阶段。
