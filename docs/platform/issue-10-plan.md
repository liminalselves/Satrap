# Issue #10 平台适配性核查与实施方案

核查日期: 2026-09-20  
对照审查修订: 2026-09-21  
本地基线: `1ecb13c` (`Merge pull request #11 from liminalselves/fix/issue-7`)  
议题: [平台适配性 #10](https://github.com/liminalselves/Satrap/issues/10)

## 结论与范围

议题描述的主要缺口成立, 本次应以 OneBot v11 适配器为交付范围, 同时保证通用事件和发送接口不破坏 Misskey 等现有平台。当前并非缺少所有消息组件, 而是组件解析、内容获取、模型输入和实际发送之间尚未连通。

新增确认一个优先级更高的缺陷: 默认 OneBot 入站路径没有设置群聊唤醒标志, 群内即使 @机器人也会被 scheduler 直接过滤。应先修复此问题, 再验收其他群聊功能。

本次只修订方案文档, 未修改业务实现, 未向 GitHub 发布评论。结论来自本地代码、离线测试、协议核对及 AstrBot / MaiBot 源码对照, 不代表真实 QQ 客户端联调结果。

## 已裁定的交付边界

以下为用户已确认的要求, 优先于本文早期建议:

| 项目 | 确定要求 |
| --- | --- |
| 部署与分发 | SnowLuma 与 Satrap 同机部署, SnowLuma 由用户自行安装, 不打包进 Satrap release |
| 验收对象 | 使用用户已初步配置的实际 SnowLuma, 打通 SnowLuma 与 Satrap 的双向通信; 可模拟 QQ 侧传输, 不要求 SnowLuma 与 QQ 连通 |
| 群聊默认行为 | 默认仅 @机器人或唤醒词触发; 引用机器人可开启; @全体和单独附件不自动触发 |
| 扩展唤醒 | 参考 MaiBot 增加可配置规则, 并提供独立的手动唤醒接口 |
| ASR | 本地与云端统一兼容 OpenAI API, 不专门适配本地推理框架; API 封装位于 APICall 下, 与其他 API Call 同层 |
| 群管理 | 提供完整的相关接口和工具, 由用户配置是否启用, 不将交付范围缩减为只读 API 或管理员命令 |
| 真实 API 测试 | 用户已授权使用本地 .toolkit 内的临时 SnowLuma 密码、LLM 和额外准备的 ASR 接口配置开展联调及真实 API 测试; 运行时按需读取, 不写入文档、测试夹具或版本库 |
| 前端交付 | 平台策略配置、ASR 模型管理与转录测试、管理工具开关、手动唤醒入口和运行状态反馈均纳入本次交付, 与后端分批同步完成 |

已确认本地存在 .toolkit/snowluma.txt 与 .toolkit/apikey.txt, 用户另已说明准备了 ASR 测试接口; 本轮方案修订只检查文件存在性, 未读取或输出凭据内容, 尚未验证接口可用性。后续测试沿用已有授权, 不重复索取确认, LLM 与 ASR 各自按对应配置调用。

## 仓库规范与设计约束

已阅读 [CONTRIBUTING.md](../../CONTRIBUTING.md) 与 [开发规范](../development/development-guidelines.md), 本方案据此采用以下约束:

- 优先复用 Platform、MessageEvent、Session、Plugin 和 Edictum 抽象, 不建立新的平行会话系统或独立消息总线。现有结构无法表达的需求可以调整设计, 但须说明边界与迁移影响
- #10 作为本次改造的讨论与关联议题; 按功能批次拆分 PR, 不混入无关重构。贡献指南建议提前讨论较大架构调整, 并未要求本次文档修订另行审批
- Python 兼容项目声明的 3.10+, 不直接复制参考项目可能依赖更高 Python 版本的语法或运行时能力
- 新增非测试 Python 模块必须有模块 Docstring, 核心模块说明职责及协作关系。公开接口及非显然私有方法按规范描述参数、返回值和失败分支
- 注释采用中文、英文标点、行末无句号; 行内注释前固定三个空格, 过长移到代码下一行; 多阶段流程使用连续的 Step.N 注释
- 导入按外部库、内部库、logger 三组组织, 组内按路径长度降序。外部 JSON 在适配器边界验证和收窄, 核心模型优先采用 dataclass、TypedDict、Protocol, 不将 Any 扩散到调度器或会话
- 静态类型检查按规范属于推荐项: 以仓库 Pyright 配置和 basic 模式为依据记录结果, 区分存量问题和本次新增问题。提交前先运行相关行为测试, PR 前建议运行完整离线测试, 外部联调单独记录

## 参考实现核查与取舍

以下均为 2026-09-20 实际读取的源码快照, 链接固定到提交, 避免后续默认分支变化。借鉴的是职责划分和已验证的处理路径, 不把参考项目的行为当成 OneBot 标准或 Satrap 已实现能力。

| 来源与固定版本 | 已确认行为 | Satrap 采用方式 |
| --- | --- | --- |
| AstrBot `6914bc3` [唤醒阶段](https://github.com/AstrBotDevs/AstrBot/blob/6914bc3aa61e14ca9a9c2cb37a0f9ec1ff5d6334/astrbot/core/pipeline/waking_check/stage.py#L114) | 管线按前缀、At、AtAll、引用发送者判断唤醒 | 在 scheduler 的现有唤醒阶段补齐统一策略, OneBot 负责可靠解析身份和组件 |
| AstrBot [引用解析](https://github.com/AstrBotDevs/AstrBot/blob/6914bc3aa61e14ca9a9c2cb37a0f9ec1ff5d6334/astrbot/core/platform/sources/aiocqhttp/aiocqhttp_platform_adapter.py#L303) | get_msg 后复用消息转换, 通过 get_reply=False 防止多层引用继续回源, 失败保留引用段 | 复用 Reply 字段和转换器, 增加请求预算与循环检测, 保留失败信息 |
| AstrBot [发送分流](https://github.com/AstrBotDevs/AstrBot/blob/6914bc3aa61e14ca9a9c2cb37a0f9ec1ff5d6334/astrbot/core/platform/sources/aiocqhttp/aiocqhttp_message_event.py#L127) 与 [引用/提及装饰](https://github.com/AstrBotDevs/AstrBot/blob/6914bc3aa61e14ca9a9c2cb37a0f9ec1ff5d6334/astrbot/core/pipeline/result_decorate/stage.py#L431) 及 [文本分段](https://github.com/AstrBotDevs/AstrBot/blob/6914bc3aa61e14ca9a9c2cb37a0f9ec1ff5d6334/astrbot/core/pipeline/result_decorate/stage.py#L209) | Node/Nodes 走专用转发 API, File 独立发送; 结果装饰阶段实际添加引用/提及并处理文本分段 | 分离回复策略和协议发送, 明确混合链的发送顺序; 不照搬随机等待或将表现分段当作长度保障 |
| MaiBot-Napcat-Adapter `5f2e9f2` [入站 codec](https://github.com/Mai-with-u/MaiBot-Napcat-Adapter/blob/5f2e9f266f3c6812054d0c9c716bc35c184004dd/codecs/inbound/message_codec.py#L338) | 通过查询服务补引用预览; 转发优先使用内联节点, 否则回源; 语音获取字节; file 主要变成名称、大小、链接摘要 | 分离纯转换、平台查询和模型输入投影; 采用内联节点优先; 文件正文仍需 Satrap 自行接入 documents |
| MaiBot-Napcat-Adapter [出站 codec](https://github.com/Mai-with-u/MaiBot-Napcat-Adapter/blob/5f2e9f266f3c6812054d0c9c716bc35c184004dd/codecs/outbound/message_codec.py#L47) | 转发选择 send_group_forward_msg / send_private_forward_msg, 并可将普通段包装为节点 | 借鉴 API 分流; Satrap 默认保持普通消息与转发的原有边界, 仅显式选择整条转发模式时包装普通段 |
| MaiBot-Napcat-Adapter [通知路由](https://github.com/Mai-with-u/MaiBot-Napcat-Adapter/blob/5f2e9f266f3c6812054d0c9c716bc35c184004dd/runtime/router.py#L141) 与 [通知 codec](https://github.com/Mai-with-u/MaiBot-Napcat-Adapter/blob/5f2e9f266f3c6812054d0c9c716bc35c184004dd/codecs/notice/message_codec.py#L92) | 按事件配置过滤, 构建通知摘要, 携带原始字段及去重键路由 | 复用 Satrap PlatformEvent / EventHandler, 加有界去重与显式订阅, 默认不转成 LLM 请求 |
| MaiBot `fb4a8bd` [消息处理](https://github.com/Mai-with-u/MaiBot/blob/fb4a8bd917d6fe373df9e3c22289d229e7f1b344/src/chat/message_receive/message.py#L395) 与 [语音服务](https://github.com/Mai-with-u/MaiBot/blob/fb4a8bd917d6fe373df9e3c22289d229e7f1b344/src/common/utils/utils_voice.py#L18) | 语音内容优先复用, 未处理时调用 ASR, 禁用/失败保留占位; 消息模型区分平台最终 ID 与内部 ID | 内容层编排 ASRCall, 协议封装置于 APICall; 出站回执只记录平台确认 ID, 不用内部 ID 伪造成功 |
| MaiBot [触发门控](https://github.com/Mai-with-u/MaiBot/blob/fb4a8bd917d6fe373df9e3c22289d229e7f1b344/src/maisaka/turn_gates.py) 与 [回复时机配置](https://github.com/Mai-with-u/MaiBot/blob/fb4a8bd917d6fe373df9e3c22289d229e7f1b344/src/config/official_configs.py#L531) | 支持消息数量阈值、回复必要性评分、名字提及和按会话/时段调整频率; 数量不足时可使用空窗补偿, 但至少有一条新消息 | 增加默认关闭的可选唤醒策略, 保持现有 Session 执行模式, 不搬入 MaiBot 的完整 Planner/注意力系统 |
| MaiBot [触发调度](https://github.com/Mai-with-u/MaiBot/blob/fb4a8bd917d6fe373df9e3c22289d229e7f1b344/src/maisaka/turn_scheduler.py#L63) 与 [空闲退避](https://github.com/Mai-with-u/MaiBot/blob/fb4a8bd917d6fe373df9e3c22289d229e7f1b344/src/maisaka/idle_backoff.py) | 调度区分强制触发与普通门控, 对连续无行动轮次退避 | 将显式手动唤醒与自动规则分开; 采用冷却和有界待处理窗口, 不照搬依赖 Planner wait 动作的退避计数 |

参考实现也存在与本项目需求不同的边界: MaiBot 当前文件入站摘要不能替代正文提取; AstrBot 的通知转换不能直接套用到 Satrap 的唤醒过滤; 两个项目的转发请求分别可见 messages 与 message 字段用法, 必须按目标 OneBot 实现验证。上述源码读取未运行外部项目, 不据此宣称其完整兼容性。

## 核查结果

| 项目 | 当前实际状态 | 依据与处理意见 |
| --- | --- | --- |
| 群聊唤醒 | 默认链路缺失 | `event.py:356` 两个标志初始化为 False; `onebot/adapter.py:208` 提交时未设置; `scheduler.py:120` 因此过滤群消息 |
| 引用入站 | 仅保留 ID 和占位文本 | `onebot_utils.py:124` 的转换函数未回源; `Reply` 已有 chain、sender、time、message_str 字段, 可复用 |
| 引用与 @ 出站 | 有序列化, 缺少自动回复策略 | `onebot_utils.py:193` 可序列化 At、Reply; scheduler 兜底回复只构造纯文本 |
| @ 入站语义 | 保存目标 ID, 模型只收到文本表示 | 保留 ID, 补充发送者和提及对象结构, 不将普通文本中的 @数字当作可信提及 |
| 语音入站 | Record + [语音], 未发现 ASR 接入 | scheduler 仅提取 image/video; `ProviderRequest.audio_urls` 存在不等于执行链路支持音频 |
| 文件入站 | 可解析 file 段, 未接入内容提取 | 群文件上传还可能通过 notice 上报, 不能只补 file 分支; 仓库已有受限文档提取器可复用 |
| 合并转发入站 | forward 落入 Unknown | 已有 Forward、Node、Nodes 类型, 但 OneBot 转换器未映射, 也没有 get_forward_msg 回源 |
| 合并转发出站 | 不能仅靠构造 Nodes 完成 | Nodes.to_dict 返回 messages 容器; 当前 send_message 将所有组件作为普通消息段发出, 需要专门的发送分流 |
| notice/request | 仅记录日志 | `onebot/adapter.py:178`、`:187` 未提交事件; 当前 dispatcher 仅按 MessageEvent 处理 |
| 群管理 API | OneBot 缺少封装 | 通用 get_group 是默认空实现; 未发现禁言、踢人、审批等 OneBot 封装 |
| 长消息 | 无长度策略 | send_stream 仅合并生成结果或按生成器块发送, 不保证每条消息符合长度限制 |
| 文件出站 | 只能确认生成 file 段 | 不能据此认定所有 OneBot 实现都支持文件发送; 应按实际实现的扩展 API 验证 |
| 图片/视频 | 已有输入提取与输出序列化 | 模型是否理解取决于视觉配置、资源可访问性和提供方能力, 不应标记为无条件端到端支持 |
| Face/JSON | 保留组件, 模型看到占位符 | 可后续增加受限文本摘要, 不建议为此扩大首期改造范围 |

代码路径均相对仓库根目录, 上表未写完整路径的源码分别位于 `satrap/core/platform/`、`satrap/core/platform/onebot/`、`satrap/core/pipeline/` 和 `satrap/core/components/message.py`。

## 实施设计

完整对照证据与优先级见方案审查 `docs/archive/issue-10/issue-10-review.md` (本地留档, 不入库)。以下已吸收该审查的 R1-R10 修正, 均为待实施要求。

### 0. 执行基础与兼容边界

适配器接收与慢执行解耦, 按最终 Session 串行、不同 Session 有界并发, 复用 SessionManager 的执行约束并避免重复加锁。入站、等待执行和出站队列均定义容量、TTL 和满载策略; 心跳、状态更新和动作回包不等待 LLM。取消、停用、重连与关闭统一由后端管理任务生命周期, 不逐消息创建无界后台任务。

区分平台聊天标识、最终 Session、触发窗口和本轮操作者。新增 context_scope: legacy_user 保持旧用户映射, group_member 隔离同用户在不同群的上下文, group 显式共享群上下文。既有配置保持原映射, 新建 OneBot 配置推荐 group_member; 切换范围创建新映射并保留旧会话, 不静默合并历史。UI 显示范围和切换影响。群级活跃度可参与决策, 但用户隔离模式不得把其他成员的待处理正文注入该用户会话。

普通入站消息也按实例、账号、聊天和平台消息 ID 做有界 TTL 去重, 排除自身消息回声; 缺失 ID 时不能单凭相同文本永久去重。观察窗口允许重启后清空; 已接受的手动请求、已提交发送尝试使用现有状态存储留下最小记录, 重启后无法确定的结果标为 unknown, 不自动重放。

### 1. 优先修复群聊唤醒

OneBot 创建事件时负责保留正确 self_id、发送者和 At 组件; 在 PipelineScheduler 现有 Stage 1b 中补齐统一唤醒判断, 根据真实 At 目标设置已有唤醒字段。此处修订原方案中直接在适配器塞入全部唤醒策略的安排, 让规则能复用于其他平台。已有上游明确设置的唤醒标志应保留, 不覆盖为 False。

唤醒词使用明确配置; @其他人及 @全体不自动触发。私聊保留当前行为。引用机器人消息作为可选唤醒能力: 先经过静默接收防洪与权限检查, 仅对 Reply 使用独立的小预算查询发送者, 再决定是否继续完整内容补全。普通未唤醒消息不扣模型调用额度, 不发送限流反馈。默认关闭引用唤醒, 避免 P0 依赖网络回源, 也避免出现必须先唤醒才能回源、必须先回源才能唤醒的循环依赖。

验收必须覆盖从原始 OneBot payload 到 SessionManager 调用的完整链路, 不能在测试中手工设置 event.is_wake 后宣称已覆盖适配器。

P0 同步新增适配器实例级 `group_whitelist: list[str] = []`, 空列表表示不限制群范围, 不覆盖群聊总开关、工具权限和其他拒绝策略; 非空时仅允许列出的群。群 ID 在边界归一化为字符串并校验, 非法值拒绝保存。此入口范围检查早于唤醒观察、回源和模型限流, 非白名单群静默拒绝, 不进入待处理窗口; 群级唤醒覆盖和手动唤醒不能绕过它。私聊不受该列表影响。群业务通知、面向群的工具动作和主动发送沿用同一目标范围检查; 心跳/动作回包及维护连接、身份、缓存所需的技术状态更新仍正常处理。平台编辑页在群聊开关旁提供群 ID 列表, 明示“留空允许所有群”, 后端再次校验。配置收紧后清理被排除群的待处理任务, 执行与发送前检查当前范围, 已发出的动作无法撤销; 保存/生效反馈接入 P0a-2。

### 1.1 扩展群聊唤醒规则

在既有 scheduler 旁增加小型 WakePolicy 模块, 返回类型化 WakeDecision, 包含是否触发、原因、命中规则和可选下次检查时间。配置以平台实例为基础, 支持群级覆盖; 规则之间的优先顺序明确且可测试。以下均为 Satrap 的拟定规则, 不代表逐项照搬 MaiBot:

| 规则 | 拟定行为 | 默认 |
| --- | --- | --- |
| 直接触发 | 真实 At 指向 self_id 或命中配置的唤醒词 | 开启 |
| 引用机器人 | 受限回源确认被引用者身份后触发 | 关闭, 可开启 |
| 名字/别名提及 | 对当前消息正文匹配显式配置的机器人名字/别名, 不扫描引用、转发或附件正文 | 关闭 |
| 消息数量/频率 | 按群的待处理新消息数量达到阈值触发, talk_value 映射为阈值而非逐条随机抽签 | 关闭 |
| 回复必要性 | 基于提及、问题特征、积压量及近期机器人发言占比做可解释的本地评分; 不为每条消息额外调用 LLM | 关闭, 与频率模式二选一 |
| 空窗补偿 | 频率模式下有待处理消息但暂未达到数量阈值时, 在配置的最长等待窗口内重新评估; 没有新消息绝不因沉默反复触发 | 关闭 |
| 时段/群覆盖与冷却 | 按群和时段调整自动参与频率, 限制连续自动回复; 显式 @与手动请求不受自动参与的评分/冷却阻挡 | 随对应自动规则启用 |

@全体和单独附件仍不自动唤醒, 也不单独贡献频率计数或必要性触发; 包含有效正文的混合消息可以按正文评估。其他通知默认不计入自动唤醒。直接触发优先于自动参与模式, 但所有路径均遵守停用状态、权限和系统限流。

自动参与模式需要有界的待处理消息窗口, 按 adapter_id、self_id、群及 context_scope 对应路由键隔离, 保留发送者和消息 ID。只收集策略允许的消息, 限制 TTL、条数及字符数; 普通未唤醒消息仅保留轻量文本和元信息, 不提前下载附件或调用 ASR。触发后冻结一次批次快照, 按到达顺序组装输入, 通过既有 UserManager / SessionManager 路由; 仅显式 group 范围允许共享成员正文, 其他范围只投影目标用户允许的内容。调用已提交与未提交分别记录, 防止重复消费或错误重试。

空窗计时任务受 BackendManager 生命周期管理, 每路由最多一个有界待执行任务; 配置停用、平台关闭和会话回收时取消。没有新消息的定时主动聊天不在本次默认行为内。规则参数、评分权重和冷却阈值由实现测试确定初值并支持配置, 不要求用户逐项裁定。

### 1.2 手动唤醒接口

提供独立的应用层 wake 接口, 在现有后端 HTTP/control 路由及 Python API 中暴露, 不另启服务。拟定输入包括 adapter_id、平台会话目标、用于既有会话路由的用户身份、可选 message_id 或 prompt、reason、request_id; 操作者身份来自已认证的调用上下文, 不能由正文伪造。群目标不能唯一确定既有会话时返回明确的路由歧义错误。

- 指定 message_id 时唤醒并处理指定消息; 指定 prompt 时启动一次带该输入的会话处理; 二者均未指定时处理该路由当前待处理快照, 没有可处理内容则返回 no_pending
- 手动请求绕过自动唤醒的关键词、频率与评分判定, 但不绕过访问权限、平台停用、event.call_llm=False、停止状态或系统限流
- 返回 accepted、already_pending、no_pending、rejected 等明确结果及请求标识; request_id 用于幂等, 忙碌时合并同一请求或排入现有调度, 不并发启动重复 Session 调用
- 不伪造用户 @消息, 不修改永久群配置, 不通过强行重置事件标志撤销插件的显式拒绝; 回复沿原适配器和平台会话发送
- 同步在现有运行时会话页面提供手动唤醒按钮及轻量弹窗, 复用 Python 与现有 HTTP/control 的同一契约, 不另建完整唤醒管理页面

### 2. 引用回源与模型输入补全

保留同步的消息段转换函数用于纯解析, 在现有管线新增明确的异步内容补全调用点。推荐顺序为: 纯解析 → 静默接收防洪、身份校验与去重 → 轻量 preprocessor 与权限检查 → 观察/唤醒判断 (按需用独立预算查询引用发送者) → 模型执行额度 → 内容补全 → 构建 UserCall → 会话执行。preprocessors 不放入无预算的下载、回源或 ASR。接收防洪、外部回源和模型限流分别计量; 自动参与被限流不向群刷反馈, 显式请求的反馈按策略节流。阶段调整同步更新注释和测试。

补全能力按小模块实现, 不引入另一套通用管线框架。OneBot 查询封装只负责协议 I/O 和边界归一化, 内容补全负责预算与缓存, 输入投影负责文本及媒体排列。转发优先消费已提供的内联节点, 再使用 get_forward_msg; 在边界统一标准和已验证扩展的节点容器、sender、content/message 字段, 核心不保留多套分支。

引用通过 get_msg 获取原文, 填充已有 Reply 字段, 并将发送者、时间和原文作为明确标记的引用上下文传给模型。仅填 Reply.chain 不够: 当前 scheduler 读取 event.message_str, 媒体提取也只扫描顶层组件。应新增统一输入投影函数, 显式处理引用和转发内的文本、图片、视频, 并避免重复附加。

引用内容不参与当前消息的命令解析或唤醒判定。引用与文件内容作为用户提供的资料, 不提升为系统指令。回源失败时保留原始消息和可识别的失败状态, 继续处理当前问题。

建议起始预算: 引用递归深度 1、转发深度 2、最多 20 个节点、单次回源超时 5 秒; 总文本和媒体数量另设上限。这些是应用初始配置建议, 不是协议限制。缓存按适配器实例、机器人账号、会话和消息 ID 隔离, 使用 TTL 和有界容量; 无法确认来源的内容不得混用其他会话缓存。

文本、引用、转发、文件和 ASR 共享一次请求的字符/token/媒体总预算, 超限优先保留当前问题和来源并标记删减。观察窗口不持有临时媒体; 真正触发后获取资源, 延迟执行任务接管的资产由任务在完成、超时、取消或异常时清理, 不由原事件提前删除。

### 3. 出站回复与长消息

引入携带来源消息 ID、发送者 ID 的回复上下文, 由统一回复入口应用策略。建议配置 `reply_with_quote`、`reply_with_mention`, 默认关闭以保持现有行为, 群聊按需开启。不要把最近一次入站消息存成适配器上的共享可变状态。

引用段置于合法位置, @发送者仅用于群聊, 已有 Reply/At 时去重。覆盖 scheduler 兜底发送、显式事件发送及流式降级路径; 主动消息缺少来源上下文时不自动添加引用。错误反馈是否引用单独控制。

长消息在适配器实际发送前统一处理: 优先按段落和换行拆分, 超长单段按配置预算切分; 保留组件顺序, 非文本组件不可切开, 处理代码围栏可读性。只在首块添加回复引用和提及。长度阈值按实际实现配置, 不声称 OneBot 有统一固定上限。

使用每会话有界发送队列控制顺序和节奏。记录各块返回的 message_id 与失败位置; 部分成功后不重发全部内容, 对发送结果不明的超时不盲目自动重试。合并转发模式须在实现能力已确认后启用, 不支持时降级为分段发送。

先形成可测试的发送计划, 再逐项执行: 连续普通段可合并, Node/Nodes 与 File 按目标实现能力分流, 保持前后顺序; @ 后按需补空格防止与正文粘连。例如 Plain(A)、Nodes(B)、Plain(C) 默认按普通 A、转发 B、普通 C 发送, 不隐式把整条链包装成转发。

新增结构化发送回执, 区分成功、部分成功、明确失败和结果未知, 记录平台消息 ID 列表。先在 OneBot 落地并修正 MessageEvent.send 的无条件已发送标志; 对其他适配器旧式 None 返回保留兼容层, 不一律判为失败, 也不伪造平台确认 ID。部分成功或结果未知不能触发 scheduler 重发全文。分块任务须由一次逻辑回复持有上下文, 保证流式多次调用也只在首个已提交块添加引用/@。队列复用适配器生命周期, 关闭时取消或有界排空, 不新增独立后台服务。

### 4. 平台事件与管理 API

优先复用现有 PlatformEvent、EventHandler 和 PlatformAdapter.emit_event, 不预先新增平行的 NoticeEvent / RequestEvent 体系。保留适配器 ID、事件类型、群/用户/操作者 ID、时间和原始载荷, request 保存 flag 和 sub_type; 在边界用明确的类型化载荷表达这些字段, 避免业务依赖任意 extras 键。

现有 emit_event 在未注册 handler 时会丢弃事件, 因此实施必须把 BackendManager 的 handler 装配纳入范围。建议复用现有适配器队列及 EventDispatcher, 队列类型显式扩展为 MessageEvent | PlatformEvent; 非消息分支调用已有 emit_event / EventHandler, 消息分支继续走 scheduler。每个事件只提交一次, 避免同时调用 callback 和队列导致重复派发。只有现有抽象确实无法表达时再增加子类型, 不改变现有消息消费者的类型契约。

通知按类型启用、按有界 TTL 去重并分发给明确注册的处理器。借鉴 MaiBot 的技术性去重键, 用平台实例、账号、事件类型及稳定载荷字段构造键, 不用随机生成的内部消息 ID 去重。插件侧需要一个在现有 Plugin 生命周期中注册和注销事件处理器的入口; 不假设当前 Session 插件已经能消费 PlatformEvent。默认不让所有入退群、撤回、请求直接调用 LLM。

如果显式规则决定转为会话消息, 再构造 MessageEvent 并应用权限和触发策略。另需补齐 scheduler 对 event.call_llm 与停止状态的实际检查: 当前字段和 setter 已存在, execute 尚未消费, 仅调用 should_call_llm(False) 不能保证跳过模型。

群文件上传 notice 可归一化成附件事件, 但下载与模型处理仍须遵守该会话的触发策略。撤回事件使相关引用缓存失效; 已持久化模型上下文的撤回同步作为明确的后续范围, 不承诺删除远端模型已接收内容。

提供本议题范围内完整的类型化接口和对应工具, 通过现有 Plugin / 工具注册机制接入。至少覆盖: 消息读取与撤回、转发读写、群列表/信息/成员信息/成员列表/荣誉信息、踢人、单人/全员/匿名禁言、设置管理员、匿名开关、群名片、群名、专属头衔、退群/解散参数, 以及好友请求和加群请求/邀请的批准与拒绝。首个实现阶段对照 OneBot v11 与 SnowLuma 的能力表逐项登记, 不以若干示例 API 代替完整交付。

所有相关工具均实现, 是否注册给模型、允许的调用者与作用群由用户配置决定, 不固定限制为人工命令。沿用现有工具 enable/disable 和插件生命周期, 支持按工具启停及配置更新。建议初始关闭管理写操作工具, 用户开启后按其配置执行, 不额外引入一律逐次人工审批的工作流。平台账号本身的权限和协议限制仍需核验; 不支持的动作返回明确 unsupported, 不伪装成功。统一超时、参数验证、错误类型和执行记录, 不因收到 request 事件就自动审批, 自动化由用户显式配置。

通用接口提供 capability 查询和明确的 unsupported 结果, 不要求其他平台伪造群管理能力。实现相关的文件与合并转发发送接口放在 OneBot 实现配置中, 按目标实现版本验证载荷与返回结构。

工具权限依赖类型化的逐次调用上下文, 从入站边界经 UserCall、Session 到工具贯穿 adapter_id、self_id、origin_chat、actor、source_message_id 和 request_id, 模型参数不能覆盖这些字段。分别实现 SessionClassProvider 和 EdictumProvider 的注入/注册路径, 不支持的会话类型明确返回不可用。共享群会话仍保留本轮真实主体, 不继承上次管理员权限; 手动唤醒使用已认证主体。

能力矩阵逐项登记标准/扩展归属、参数、响应、工具名、权限范围、读写属性及安全重试条件。状态区分未知、支持、不支持、暂不可用, 不依赖 SDK 动态属性判断能力, 不执行踢人等写动作探测。用户开关和实例能力共同决定模型可见工具, 配置关闭后清理旧注册。状态事件沿轻量分发路径及时更新, 不排在耗时会话后面。

### 5. 文件与语音

文件先解析远程文件 ID/URL, 经受限下载落入临时目录, 复用 `satrap/core/utils/documents.py:285` 的 extract_text。限制大小、超时、解压规模和最终上下文长度, 对不支持类型返回文件元信息与原因。复用现有 outbound URL 防护, 不直接把平台上报路径视为 Satrap 主机上的可信本地路径。提取结果冻结进本次输入后再清理临时文件, 避免恢复执行依赖已删除文件。

ASR 的 API 封装新增于 `satrap/core/APICall/ASRCall/`, 与 LLMCall、EmbedCall、ReRankCall 同层。参照现有 API Call 的 base.py、sync.py、async_.py、utils.py、__init__.py 划分, 提供 ASR / AsyncASR 对称入口, 共用配置合并与响应解析, 不在适配器内散落 SDK 调用。

协议基线为 OpenAI 兼容的 `audio.transcriptions.create`, 向 `/audio/transcriptions` 提交音频文件和模型名, 默认请求 JSON 并提取文本; 不使用聊天补全端点假装 ASR。依据 [OpenAI 官方转录 API 文档](https://developers.openai.com/api/reference/python/resources/audio/subresources/transcriptions/methods/create)。本地和云端统一通过 base_url、api_key、model 配置, 不打包本地推理引擎, 不做某个本地框架的私有适配, 也不限定模型名为 OpenAI 官方模型。

新增 ASRConfig 与类型化响应, 接入既有配置加载/模型配置管理入口; 保持构造默认值与调用覆盖、URL 归一化、超时、凭据保护和 suppress_error 的总体设计一致。拟定 transcribe 接收受控本地文件或带文件名/MIME 的字节输入, 返回 ASRResponse(text, model, 可选 language/duration), 抑制异常时返回 None, 不抑制时传播明确异常; 空转录文本与失败区分。同步/异步的返回和错误语义必须一致。可选 language、prompt 等参数按已配置能力传递, 不假定所有兼容服务支持全部 OpenAI 扩展参数。

适配器负责获取 Record 对应资源, 内容补全层负责调用 AsyncASR、缓存结果并投影进模型输入, APICall 层只负责转录协议。已转写内容优先复用; 禁用或失败时明确标记未转写, 限制大小、时长、并发和整体耗时。音频格式不兼容时优先使用 SnowLuma 可用的转换能力或已有媒体设施, 无可用转换时明确降级, 不把未知二进制直接送入文本模型。音频解码与文档提取不阻塞事件循环。

### 6. SnowLuma 同机通信验收

Satrap release 只包含自身适配器、配置示例和联调说明, 不附带 SnowLuma 程序、账号配置或临时凭据。SnowLuma 的公开项目说明确认支持 OneBot WebSocket 服务端/客户端等模式, 参考 [SnowLuma 官方仓库](https://github.com/SnowLuma/SnowLuma); 实际端口、版本和已开启连接在联调时读取用户现有安装配置, 不按文档默认值覆盖。

优先复用 Satrap 现有 aiocqhttp 反向 WebSocket: Satrap 监听回环地址, SnowLuma 作为 WebSocket 客户端连接, 使用数组消息和可承载事件/API 的连接角色。WebUI 登录密码与 OneBot access token 分别处理, 不默认两者相同。配置采用测试实例/账号标识, 修改前保留原配置并在验收后恢复临时改动。

验收分为三层, 每层结果单独记录:

1. 离线协议测试: 用固定消息、notice/request、动作响应夹具验证所有转换、规则、工具路由和异常分支
2. 实际 SnowLuma 通信: 必须使用实际 SnowLuma 的 OneBot 网络层, 检查鉴权、连接/重连、事件进入 Satrap、Satrap 动作到达 SnowLuma 以及 echo 回包关联。QQ 侧采用模拟事件源/动作执行替身, 不登录 QQ、不发送真实 QQ 消息; 若安装版本未提供模拟注入能力, 使用基于其真实网络模块的测试驱动并记录挂接点, 不能仅用一个冒充 SnowLuma 的 WebSocket 服务宣称本层通过
3. 真实模型 API: 使用已授权的 LLM 配置, 对模拟入站完成模型调用与回复回传; 使用用户额外提供的 ASR 接口对短音频执行真实转录, 验证内容进入后续模型输入, 不再将 ASR 真实测试列为有接口才开展的可选项。同步/异步契约另由协议测试覆盖。若接口临时不可达或模型不支持所需格式, 报告具体失败与未完成项, 不以模拟结果代替真实 ASR 验收

媒体、回源、管理操作和审批可以使用 QQ 侧的可控模拟结果验证请求格式与 Satrap 行为, 不要求 QQ 本身执行这些操作。报告明确区分实际通信、模拟动作结果和真实模型结果, 不将本次范围升级为 QQ 全链路验收。

### 7. 前端配置与操作闭环

前端是本议题的正式交付范围, 用户应能在现有界面完成配置、启停和手动唤醒, 不需要依靠修改 YAML 才能使用新增能力。已核查 `satrap-ui/DEVELOPMENT.md`、`docs/ui/ui-design-system.md` 及下列现有源码; 实现复用现有组件、主题变量和交互模式。

#### 7.1 当前界面缺口

- `pages/Platforms/index.tsx` 的 OneBot 表单仅有连接、身份、私聊和群聊开关; handleFieldChange 目前把 settings 后缀当作单层键, 不能直接用点号字段表达新的多层配置
- `pages/Models/index.tsx`、`api/model.ts`、`api/control.ts` 和 `stores/useConfigStore.ts` 均将模型类型限定为 llm/embedding/rerank; 后端 ModelConfigService 同样需要增加 asr 分支, 不能只新增一个前端标签页
- `components/common/PluginConfigFields.tsx` 仅将 llm/embed/rerank 识别为模型选项; 需补 asr 类型以及后端 plugin-model-options 数据源
- `pages/Sessions/EdictumPluginManager.tsx` 已有 tools/handlers/commands 等能力开关, 可以承载群管理工具启停; 会话覆盖复用 SessionPluginSettingsModal, 不创建重复的工具管理系统
- 平台卡片当前显示通用运行状态并直接渲染 settings 的 JSON 摘要; 应改为脱敏的连接与策略摘要, 避免把配置细节和凭据当作页面摘要

上述前端源码路径相对 `satrap-ui/src/`。既有 Chat 页面有自身模型编辑与会话插件入口, 需要检查共享类型变化对这些路径的影响, ASR 不应混入聊天主模型下拉选项。

#### 7.2 页面与交互改动

| 页面/入口 | 本次交付 | 对应后端契约 |
| --- | --- | --- |
| 平台管理 / OneBot 编辑 | 按连接、唤醒、回复、媒体、事件分组; 配置唤醒词/引用/名字、自动参与模式、群级与时段覆盖、引用/@回复、长消息阈值、文件限制、ASR 配置引用及通知类型 | 类型化 OneBot settings 与校验, 保存后返回实际生效状态 |
| 平台管理 / 运行状态 | 区分适配器启动、SnowLuma 已连接、最近通信错误和能力可用性; 展示同机安装说明和连接地址, 不将 QQ 在线作为本次连接成功条件 | 脱敏连接状态、最近事件/动作时间、能力矩阵; 未检测显示未知, 不用“已启动”推断已连通 |
| 模型管理 / ASR 标签 | 新增/编辑/删除命名 ASR 配置, 填写 Base URL、模型、API Key、超时及可选语言等; 本地/云端用同一表单 | ASRConfig 的 CRUD、脱敏读取、引用检查及运行时更新 |
| 模型管理 / ASR 测试 | 选择已保存的 ASR 配置和一段短音频, 点击测试后显示转录文本、耗时或明确错误; 不自动发起付费调用 | 后端受限音频上传与转录测试端点, 后端读取密钥并调用 ASRCall, 请求结束清理临时资产 |
| 会话 / 插件配置 | 复用现有能力列表展示完整群管理工具及说明, 可逐项启停; 配置适配器绑定、允许群与调用者范围, 显示继承和覆盖来源 | 插件 schema、capabilities 与会话覆盖配置, 工具实际注册状态及目标平台能力 |
| 运行时会话 / 手动唤醒 | 按钮预填平台和会话目标; 弹窗选择待处理消息、指定消息 ID 或输入提示; 无法确定用户路由时要求明确选择 | wake 接口与同一 request_id 幂等契约, 返回 accepted/already_pending/no_pending/rejected |
| 会话/平台详情与日志 | 展示最近唤醒原因、处理状态、附件提取/语音转录失败和发送部分成功; 接受唤醒与回复已送达分别呈现 | 后端简明状态与结构化回执, 不仅依靠前端 toast 或日志字符串推断 |

唤醒表单采用基础选项和展开的高级规则, 模式不匹配的字段隐藏或禁用但保存时不意外丢值。群级覆盖必须区分继承、显式关闭与显式值, 数值阈值、时段及列表由后端再次校验。新增配置以平台实例为默认层, 群覆盖按已定义规则生效, 不重复提供多个互相冲突的主配置入口。

手动唤醒请求提交后禁用重复提交, 失败重试复用逻辑请求标识; accepted 仅表示已接受, 后续执行/发送状态由后端状态接口反馈。后端离线时配置编辑仍沿现有 control 通路工作, 运行操作显示不可用原因。

平台 settings 应使用明确的嵌套更新/表单转换函数, 不把 `wake.rules` 写成含点号的字面键; 保留未被当前表单识别的扩展字段。onebot 与 aiocqhttp 别名使用同一表单和归一化规则。保存配置和运行时热加载分别反馈, 后端只在实际成功应用后标记“已生效”, 无法热加载的字段说明需要重启。

当前 BackendManager.reload_config 未协调平台配置, 因此必须增加真实应用过程: 返回 saved_revision、active_revision 及 applied/pending_restart/failed。策略配置用不可变快照绑定请求, 连接字段由适配器定向重连/重启; 先验证候选值, 应用失败保留旧生效配置并显示原因。删除/停用实例时取消相关任务, 不能以通用 reload 成功冒充平台更新完成。

新增字段描述和默认值从后端校验定义导出, 复用现有表单组件; 群/时段列表使用专用编辑器。保留编辑草稿, 增加脏状态、离开提醒、恢复默认/继承、字段级错误和有效配置预览。保存使用修订号检测 UI/CLI 并发修改, schema 后到或页面刷新不得覆盖未保存编辑。提供唤醒规则试算, 复用真实策略函数但不调用模型、不写窗口, 展示命中/拒绝原因、配置来源和阈值。

运行状态区分启动、连接、身份确认和可发送状态, 记录最后事件/心跳/动作回包与状态更新时间; 心跳缺失但 I/O 正常时不能直接判离线。每实例绑定一个 self_id, 拒绝第二账号覆盖共享身份; 被禁言、移出群或身份改变时失效相关缓存。按 request_id 关联唤醒、补全、模型和发送, 提供有界脱敏记录与刷新恢复接口, 解释未唤醒、限流、转录失败和发送未知的区别。

API Key 与 access token 沿用脱敏读写契约, 空输入保持、替换和清除语义明确, 掩码不能回写成真实密钥。ASR 测试由后端调用外部服务, 浏览器不直接访问供应商; 模型删除时检查平台/插件引用, 不留下静默失效的绑定。

本次不把 OneBot 群消息自动导入 WebChat 会话, 也不新增完整 QQ 客户端、浏览器录音或独立群管理工作台。平台运行反馈在其已有页面和详情中呈现; 如果共有消息展示 DTO 增加了附件/转录元信息, Chat 侧仅同步做必要兼容, 不借本议题扩大聊天系统重构。

#### 7.3 前后端联动与验收

接口、TypeScript 类型、store 和组件与各后端批次同时交付。模型类型统一定义后复用, 避免在多个文件分别维护不一致的联合类型; 同步检查后端配置加载、序列化、脱敏、control/HTTP 路由与插件模型选项。

行为测试覆盖 ASR CRUD/脱敏/引用、平台嵌套配置往返、群覆盖继承、工具启停实际生效、手动唤醒重复请求及 busy/no_pending、保存成功但热加载失败。ASR 测试的上传/失败/清理使用可控夹具, 另使用用户提供的真实 API 完成一次可核验短音频测试。

前端验收运行 `npm run test`、`npm run lint`、`npm run build`, 按存量问题与新增问题分别报告。为新增流程补充 Playwright 场景并显式执行, 不能以现有 `npm run test:e2e` 的 chat-reconnect 单一脚本替代本次覆盖。浅色/深色、窄屏、键盘焦点、加载/空/失败状态做浏览器视觉检查, UI PR 附截图或录屏; 本轮方案修订尚未执行这些实现验收。

release 验收使用新构建的前端资源并检查打包/静态服务实际加载版本, 防止 Python 后端已支持 ASR 而发行包仍带旧页面。SnowLuma 仍由用户单独安装。

## 建议交付批次

| 批次 | 交付内容 | 主要改动范围 | 完成标准 |
| --- | --- | --- | --- |
| P0 | 群白名单、群聊唤醒、限流顺序与已有停止/禁用模型标志生效 | scheduler.py, OneBot 配置/入口与平台表单, 入站链路测试 | 白名单范围生效, @机器人确实调用 Session, 普通群消息不耗模型额度、不产生反馈, 私聊不回归, 显式禁用模型生效 |
| P0a-1 | 执行基础与上下文隔离 | dispatcher, SessionManager/UserManager, context_scope 配置与必要表单 | 同 Session 串行、跨 Session 有界并发, 满载与取消可观察, 跨群/成员隔离及 scope 迁移通过; 可用启动时配置独立验收 |
| P0a-2 | 配置实际应用闭环 | BackendManager, 配置服务, Platforms 页面 | saved/active 修订号一致性, 策略更新和定向重连, 失败保留旧配置, UI 准确呈现生效状态; 用现有连接字段独立验收 |
| P0a-3 | 可信调用身份 | UserCall, SessionClassProvider/EdictumProvider, 工具注册与调用边界 | 两种 Provider 身份传递可验证, 并发及共享会话权限不串用, 模型参数不可覆盖来源; 使用测试工具独立验收 |
| P0b | 可选群聊规则与手动唤醒入口 | WakePolicy, scheduler, BackendManager, 现有 API/control, Platforms/Sessions 页面 | 默认行为不变, 规则可通过 UI 配置, 手动唤醒幂等且路由正确, 无新消息不反复触发 |
| P1 | 类型化 API 边界、引用入站、回复引用/@、发送回执与文本长消息 | onebot, event.py, scheduler.py | 引用原文进入 UserCall, 所有回复入口策略一致, 分块有序且部分失败不重发全文 |
| P2 | 复用 PlatformEvent 的 notice/request 分发、完整管理接口及工具、合并转发 | platform/__init__.py, BackendManager.py, 现有插件/工具接入点, onebot | handler 实际装配且事件只派发一次, 工具可逐项启停, 通过 SnowLuma 通信及模拟动作验收 |
| P3 | 文件获取与内容提取、OpenAI 兼容 ASR | APICall/ASRCall, ASRConfig 与模型配置入口, 内容补全层, documents, Models 页面及 ASR 测试表单 | ASR 同步/异步契约一致, UI 能配置/绑定/测试, 转写/提取进入上下文, 失败可降级 |
| P4 | SnowLuma 同机通信、真实模型与 UI 验收 | 集成测试驱动, 用户现有 SnowLuma, LLM/ASR 接口, Playwright 与构建产物 | 实际 OneBot 网络通信往返成功, QQ 侧可模拟, 真实 LLM 和 ASR 验证完成, UI 流程可用且 release 不携带 SnowLuma |
| 配套 | 各阶段前端、配置、文档、回归与能力表 | satrap-ui 对应页面/API/store, config.example.yaml, docs/platform, tests | P1 回复策略、P2 工具开关和状态反馈等随后端交付, 保持 Misskey 和多实例隔离 |

各批次独立评审和验收。P0a-1 与 P0a-2 不互为交付前置, 各自提供测试夹具和必要配置入口; P0a-3 独立验证调用身份契约, 在 P0a-1 合入后补共享范围集成验证。P0b 的执行与隔离依赖 P0a-1, 手动唤醒身份依赖 P0a-3, UI 宣称热生效前须完成 P0a-2; P2 开放管理工具前须完成 P0a-3。避免重新把三个子批次捆成一次验收。SnowLuma 基础通信探针在 P0/P1 阶段就验证连接可行性, P4 汇总完整验收, 不等到最后才发现传输不兼容。P0/P1 可先解决最常见交互问题, #10 完整关闭仍需其余缺口按用户裁定的范围完成。

## 验证记录与验收清单

首轮核查已执行以下现有测试, 结果为 **54 passed in 8.82s**。本轮只修订文档, 未重复运行这些测试, 也未运行外部参考项目:

```text
python -m pytest tests/unit/test_onebot_adapter.py tests/unit/test_pipeline_scheduler.py tests/unit/test_message_components.py tests/unit/test_multi_adapter_routing.py -q
```

使用真实 OneBotAdapter 入站转换和 PipelineScheduler, 以 AsyncMock 替代 SessionManager 做额外离线探针:

```text
group   is_wake=False is_at_or_wake_command=False session_calls=0
private is_wake=False is_at_or_wake_command=False session_calls=1
```

两条 payload 均包含指向 self_id 的 At 组件和 hello 文本。这个结果确认默认群聊链路缺陷, 同时表明现有测试通过并不能证明议题中的能力已完整接通。

对照审查另使用真实 RateLimiter (burst=1, rate=0.01) 提交两条普通群消息, 得到 `ordinary_group_messages=2, session_calls=0, outbound_feedback=1`。这确认未唤醒消息也会消耗额度并导致限流反馈, 详细边界见审查 R2; 本轮尚未修复业务代码。

实施时补充以下行为验收:

- 引用成功、过期、超时、自引用、引用内图片以及缓存跨实例隔离
- 回复引用与 @ 的组合、已有组件去重、主动消息、流式降级、长消息部分发送失败
- forward 的标准与实现特定返回结构、节点数量上限、嵌套转发与不支持发送时降级
- 群上传 notice、普通 file 段、失效 URL、超大/不支持文件, ASR 禁用与失败
- 入群/退群/撤回/请求事件不误触发模型, 重复事件和审批幂等处理
- 数组消息正常解析; 字符串 CQ 消息应选择配置强制数组或使用兼容解析器, 当前 normalize_segments 会把字符串直接当纯文本
- 使用同机实际 SnowLuma 验证 OneBot 通信, 群聊/私聊/附件/转发/管理操作使用 QQ 侧模拟夹具, 记录版本、真实通信和模拟边界, 不要求 QQ 连通
- 引用机器人唤醒开关、event.call_llm=False、停止标志, 以及先拒绝再补全时不会发生媒体下载
- Plain/Nodes/File 混合发送顺序、@后空格、平台 ID 与内部 ID 区分, OneBot 使用明确回执且旧适配器 None 返回契约不回归
- emit_event handler 的后端装配、无订阅者行为、重复通知去重、订阅注销和关闭时发送队列清理
- 默认规则、名字提及、数量阈值、必要性评分、时段覆盖与空窗补偿; 无新消息和纯附件不触发; 批次冻结、跨用户路由与计时任务取消
- 手动唤醒的 prompt/message_id/待处理快照三种路径, 重复 request_id、忙碌、无待处理消息、路由歧义及权限失败
- 管理 API 与工具能力表逐项核对, 开关关闭时模型看不到对应工具, 开启后按用户配置执行, 不自动附加固定审批步骤
- ASR 同步/异步参数合并与错误一致性、JSON 解析、空结果、超时、文件格式和本地/云端 base_url; 真实 LLM 与真实 ASR 测试分别报告
- 前端 ASR 管理/测试、OneBot 策略表单、工具开关、手动唤醒及状态反馈完成端到端验收; 前端构建资源与后端契约一致
- 同平台慢请求不阻塞其他会话/心跳, 队列满载可观察, 同 Session 无并发执行; 普通消息不扣模型额度或产生反馈
- context_scope 跨群/成员隔离与迁移, 两种 Provider 的可信身份传递, 共享会话不继承前一操作者权限
- 普通消息去重、自身回声、重启后手动请求与未知发送状态; 全输入预算与排队资产的取消清理
- 配置冲突、草稿保护、规则试算无副作用, saved/active 版本差异、连接更新失败和旧配置保留, 状态过期与账号绑定

- group_whitelist 的空列表/命中/未命中、群聊总开关优先、私聊不受影响、非法 ID、实例隔离、手动/工具/主动发送不能绕过, 技术状态仍可更新, 配置收紧后的待处理清理与发送前校验

实现批次提交前的验证记录应包含: 相关测试结果、完整离线 pytest 结果或未执行原因、仓库 Pyright 配置检查结果、中文注释与导入规范检查、配置/文档同步情况。新增模块用 ast.get_docstring 检查模块头文档。PR 描述列出行为变化、验证、已知限制并关联 #10; 不将本方案记录的首轮 54 项结果当作未来实现的验收结果。

## 协议依据与实施时核实项

[OneBot v11 公开 API](https://github.com/botuniverse/onebot-11/blob/master/api/public.md) 定义了消息回源、转发读取、群管理和请求审批接口; [通知事件](https://github.com/botuniverse/onebot-11/blob/master/event/notice.md) 包含群文件上传等事件; [消息段规范](https://github.com/botuniverse/onebot-11/blob/master/message/segment.md) 是消息段映射的基准。

架构与交付边界已经由用户裁定, 当前无须再次确认。实施时自动核实 SnowLuma 安装版本、端口、鉴权、模拟挂接点、文件/转发动作载荷及长度限制; ASR 统一使用 OpenAI 兼容协议, 使用用户已提供的专门测试接口核实模型与格式支持。以上属于环境检测和能力验证, 不作为重复询问用户的前置条件。若实际接口不可用或存在协议差异, 报告具体失败证据与未完成项。
