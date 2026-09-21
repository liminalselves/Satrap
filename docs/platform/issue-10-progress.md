# Issue #10 实施记录

更新日期: 2026-09-21 (P1 长消息批次补记)

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
