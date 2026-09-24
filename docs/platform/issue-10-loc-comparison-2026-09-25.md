# Issue #10 与 AstrBot / MaiBot 的实现规模横向评估

日期: 2026-09-25

结论: 不能据这轮统计认定 Satrap 的消息处理核心普遍过长。按已选定的职责模块, 唤醒核心小于 MaiBot, 附件/引用处理小于 AstrBot, 群管理去除注释后与 MaiBot 适配器 API 层接近。主要差距在跨重启状态保证、控制面和配置治理; 它们大部分不是参考项目相同功能的重复实现, 但其中的状态表达、包装层和专用界面值得优先做下一步成本评估

本报告只测量和比较, 不据行数删功能, 不修改业务代码。此前“持久化抽取偏重”的判断保留为待深入评估项, 不扩大成对 Issue #10 全部实现的判断

## 1. 固定版本与计数口径

| 项目 | 本次源码版本 |
| --- | --- |
| Satrap | `b9d9192d091e3f14a4d887e320b55d3da0605abc` |
| Satrap 的 Issue #10 基线 | main = `1ecb13c18b226de55cbb6c7ad298552bbdb4ac3c`, 同议题最初实施基线 |
| AstrBot | [6914bc3aa61e14ca9a9c2cb37a0f9ec1ff5d6334](https://github.com/AstrBotDevs/AstrBot/tree/6914bc3aa61e14ca9a9c2cb37a0f9ec1ff5d6334) |
| MaiBot | [fb4a8bd917d6fe373df9e3c22289d229e7f1b344](https://github.com/Mai-with-u/MaiBot/tree/fb4a8bd917d6fe373df9e3c22289d229e7f1b344) |
| MaiBot-Napcat-Adapter | [5f2e9f266f3c6812054d0c9c716bc35c184004dd](https://github.com/Mai-with-u/MaiBot-Napcat-Adapter/tree/5f2e9f266f3c6812054d0c9c716bc35c184004dd) |

沿用 Issue #10 方案实际引用的快照, 不是三个项目最新默认分支。参考项目从 GitHub 固定提交的源码归档重新取得, 未运行其服务。MaiBot 的 OneBot 功能分布在主仓与适配器仓, 两者都统计, 不能只算主仓入口

计数规则:

- 表内 `物理 / 代码` 两个数字: 物理行为源文件行数; Python 代码行为包含有效 token 的物理行, 排除空行、注释、docstring 及独立说明字符串, 保留声明、导入、类型、装饰器和配置字面量。不是逻辑语句数
- Python 通过 AST 标出说明字符串, 再通过 tokenize 计行; 函数/类切片含装饰器, 行号边界见清单
- 前端只列物理行, 不拿它与 Python 代码行直接比
- 统计的是功能落点的模块包或明确函数切片, 不是整个项目, 也不是完整依赖闭包。SDK、插件框架、通用数据库和模型运行器不按每个功能重复收费; 已发现的大型共用依赖另外列出
- 有些参考模块的功能更宽或更窄, 表中明确标注。没有找到同等契约时写“未发现同等实现”, 不记成 0 行, 也不推断整个插件生态都没有该能力
- **表内各行不能简单相加成项目排名**: notice 切片与适配器整文件重叠, 还有共享模块和范围不等的功能

可核对的 [逐文件/函数计数 CSV](issue-10-loc-comparison-2026-09-25.csv) 包含项目、功能组、源码路径、精确行范围、两种行数和读取文件的 SHA-256。CSV 中 Satrap 哈希为本机工作区原始字节哈希, 换行格式会影响哈希, 不影响行数

## 2. 对应功能用了多少行

| 功能落点 | Satrap 物理 / 代码 | AstrBot 物理 / 代码 | MaiBot + 适配器 物理 / 代码 | 范围差异 |
| --- | ---: | ---: | ---: | --- |
| 唤醒、窗口、到期检查核心 | **526 / 295** | **259 / 207** | **728 / 568**, 另运行时相关方法 **133 / 103** | AstrBot 此处主要是前缀、提及、引用等明确唤醒; MaiBot 还有必要性评分、空闲退避、focus 相关门控 |
| OneBot 收发与协议落点 | **1,979 / 1,183** | 适配器和消息事件 **841 / 705**, 另结果装饰/响应阶段 **776 / 667** | **3,433 / 1,898** | Satrap 包含回执追踪挂接; MaiBot 包含自有传输、查询服务与更多卡片转换, AstrBot/Satrap 借用 aiocqhttp |
| 附件、引用、转发及模型输入补全 | **1,085 / 650** | **1,828 / 1,555** | 主仓消息处理 **547 / 392** | MaiBot 的引用/语音字节获取还在上一行的入站 codec 中; 文件为摘要, 不等价于 Satrap 正文提取 |
| OpenAI 兼容 ASR 的专用实现 | **386 / 225** | Whisper provider **49 / 40** | 语音入口及专用调用切片 **190 / 143** | Satrap 同时提供同步/异步客户端、配置构造、输入/输出归一化; 另两者复用既有 provider/LLM 基础设施 |
| notice/request 转换落点 | **301 / 158** | 两个转换函数 **53 / 49** | notice codec 包 **392 / 225** | AstrBot 数字仅为转换, 非全套分发系统; Satrap 包含事件去重/调度相关内容, MaiBot 路由在上一行适配器包 |
| 群管理 API 与工具落点 | **1,211 / 710** | 所查核心源码无对应成套封装, 可通过底层客户端调用; 外部插件未计 | API mixin 包 **2,200 / 722** | MaiBot 包还包括账号、文件、消息等额外 API; Satrap 数字包含群管理工具、权限/来源检查, 查询/传输依赖在前面另列 |
| 策略配置与引用治理 | **983 / 632** | 复用通用配置体系, 未单独分摊为 #10 对应行数 | **695 / 566** | Satrap 包含 ASR 引用扫描; MaiBot 包括回复时机/语音声明和整个 ChatConfigUtils, 不是完全同一字段集 |
| 持久状态及清单原语 | **2,350 / 1,538** | 所查路径未发现与 Satrap 请求/发送账本同等契约 | 所查路径未发现同等契约; 另有禁言状态存储 **168 / 86**, 不属于同一功能 | 不能用禁言状态文件代替审批占用、发送分段、unknown、恢复和归档账本 |
| 手动唤醒控制、试算、请求诊断 | **865 / 575** | 所查路径未发现这一整套同等控制面 | 有 @/提及的强制轮次状态, 已计入运行时方法; 未发现同等幂等手动请求+试算+阶段查询组合 | 强制下一轮不是持久 accepted/already_pending 与发送状态查询 |

### 2.1 哪些文件组成上述数字

- Satrap 唤醒: `wake_policy.py`、`wake_window.py`、`wake_timers.py`; 不把通用 scheduler 全文件算成唤醒成本。scheduler 整文件另为 **763 / 518**, 其中含既有管线, 不全归于 #10
- Satrap 收发: OneBot `adapter.py`、`onebot_utils.py`、`outbound.py` 与 `receipt.py`
- Satrap 补全: `attachments.py`、`input_projection.py`、`audio_convert.py`
- Satrap 群管理: `onebot/admin.py` 与 `group_admin` 插件 Python 文件
- Satrap 配置: `platform_policy.py`、`wake_overrides.py`、`asr_references.py`
- AstrBot 收发: `aiocqhttp_platform_adapter.py`、`aiocqhttp_message_event.py`; 共用出站为 `result_decorate/stage.py`、`respond/stage.py`
- AstrBot 补全: `preprocess_stage/stage.py`、`file_extract.py`、`quoted_message/`、`quoted_message_parser.py`, 加 `astr_main_agent.py` 中 `_apply_file_extract`、`_process_quote_message`、`collect_initial_request` 三个函数。最后一个还含普通模型请求构造, 数字是模块落点规模而非纯附件增量
- MaiBot 唤醒: `turn_gates.py`、`turn_scheduler.py`、`idle_backoff.py`、`reply_necessity.py`、`mode_policy.py`, 外加 runtime 的 13 个频率、待处理与强制轮次方法
- MaiBot 适配器收发: `transport.py`、`runtime/router.py`、入站/出站 codec 包、`query_service.py`、`action_service.py`; 群管理行是 `apis/group.py`、`account.py`、`file.py`、`message.py`、`support.py`

### 2.2 共享成本不能漏算, 也不能全部压到一个功能上

AstrBot 的 [Whisper provider](https://github.com/AstrBotDevs/AstrBot/blob/6914bc3aa61e14ca9a9c2cb37a0f9ec1ff5d6334/astrbot/core/provider/sources/whisper_api_source.py) 会调用 MediaResolver, 对应 `media_utils.py` 为 **1,987 / 1,344**。这份工具还承担图片、视频与下载等功能, 既不能说“ASR 总共 49 行”, 也不能把 1,987 行全部归给 ASR

Satrap 补全使用已有受控下载和文档提取模块: `utils/documents.py` 与 `utils/outbound/` 合计 **1,210 / 721**。这同样是共用成本, 未计入上表专用补全行。MaiBot 的 190 行 ASR 切片还依赖共用模型选择、请求构建、重试、服务和 SDK; 190 行不是完整 ASR 子系统总成本

因此, 表中可比较的是实现形态与量级, 不能用某一入口文件直接算“效率倍数”

## 3. 差距最大的地方是什么

### 3.1 最大差距: Satrap 交付了参考路径没有对等保证的持久状态层

| 新增文件 | 物理行 | Python 代码行 | 主要职责 |
| --- | ---: | ---: | --- |
| manual_wake_store.py | 1,144 | 803 | 请求和发送尝试, 分段状态, 重启清扫, 容量、归档与恢复 |
| request_registry.py | 788 | 516 | 审批身份账本及近期缓存, 原子占用, 终态/过期墓碑 |
| durability.py | 365 | 192 | 清单模型与校验, 降级标记和隔离顺序 |
| persist.py | 53 | 27 | 原子 JSON 写入与隔离原语 |
| 合计 | **2,350** | **1,538** | 不含上层发送状态记录的调用点 |

MaiBot 的 [加群审批入口](https://github.com/Mai-with-u/MaiBot-Napcat-Adapter/blob/5f2e9f266f3c6812054d0c9c716bc35c184004dd/apis/group.py#L449) 是规范化后的 action 转发, `action_service.py` 负责检查返回状态; 所查路径没有 Satrap 的占用先落盘、重启保留不可重复审批身份等同等账本。MaiBot runtime 的 `_arm_forced_turn_state` 是内存里的强制触发标记, 也不等价于手动请求持久受理

这解释了为什么这块不能用参考项目的十几行入口替换。它并不证明 Satrap 的 2,350 行全部必要: 下一步应在**保留已确认保证**的条件下, 检查清单模型、数据载荷、业务内存状态之间是否重复表达, 以及两类账本的验证/迁移编排是否可简化。不要先以删掉恢复、unknown 或幂等为减行办法

### 3.2 第二处差距: 控制面和治理比消息运行时本身更重

完全新建的以下模块合计 **4,005 物理行 / 2,610 Python 代码行**:

- 上述持久状态层: 2,350 / 1,538
- `manual_wake.py` + `wake_dry_run.py` + `request_diagnostics.py`: 672 / 440
- `platform_policy.py` + `wake_overrides.py` + `asr_references.py`: 983 / 632

Issue #10 相对 main 新建的 Python 文件合计 7,965 物理行, 上述模块占 **50.3%**。它们还没有包含 BackendManager 新增入口、策略重载和发送路径里的接线成本

这里有需求差异: 手动受理与查询、五阶段诊断、真实策略试算、来源展示、ASR 删除/重命名引用保护, 不是“把 @ 和语音跑通”本身。参考项目使用通用配置、日志、插件框架或另一套聊天 runtime, 不能据未发现同一专用模块就记零成本

优先看这组模块之间的数据与职责是否重复, 比先压缩唤醒算法或 ASR 调用收益更大

### 3.3 前端增长: 需要把专用界面与共用编辑器分开看

Satrap 六个新建专用文件共 **1,655 物理行**: 请求诊断面板、手动唤醒弹窗、策略试算面板、覆盖编辑器、覆盖转换工具和前端契约校验器。另有生成的 JSON **303 行**, 它增加仓库行数, 但不属于另一份手工维护契约

参考项目并非没有 UI 成本:

- AstrBot `AstrBotConfigV4.vue` + `ConfigItemRenderer.vue`: **1,149 行**, 是跨功能共用配置界面
- MaiBot `plugin-config.tsx` + `config-schema.ts`: **2,349 行**, 也是通用配置页与 schema 类型

这些数字不能与 Satrap 1,655 行做等功能大小排名。可借鉴的是把通用字段渲染复用起来; 不能假设共用表单能免费覆盖请求阶段诊断、试算和未知发送状态。应先评估 Satrap 专用编辑器与现有 FormModal 重复了哪些职责, 暂不另建通用前端框架

### 3.4 ASR 封装确实更长, 但不是主要增量

Satrap 386 / 225, 对比 AstrBot provider 49 / 40、MaiBot 专用调用切片 190 / 143。Satrap 多出的职责包括同步和异步两份入口、文件/字节输入检查、响应类型与配置构造。与 AstrBot 的入口差额为 337 物理行, 远小于持久状态与控制面

这里可以检查公共 base/utils 与两套入口的划分, 但即便明显简化, 也不足以解释或消除 Issue #10 的主要增长

## 4. 哪些地方暂时没有“明显写得比别人多”的证据

- 唤醒核心: Satrap 295 代码行, MaiBot 门控/评分核心 568, 另相关运行时 103。两者复杂度不同, 但不支持“Satrap 的简单门控写成了远大于 MaiBot 的系统”这个说法
- 引用/转发和附件补全: AstrBot 不只有适配器中的一段 get_msg, 还有完整 quoted_message 工具包和 agent 投影逻辑。Satrap 650 代码行不构成明显异常量级
- 群管理: Satrap 1,211 物理行对 MaiBot API 包 2,200 行看似差很多, 去掉说明后为 710 对 722。文档格式和封装覆盖面的影响很大, 不能按物理行断言冗余
- 通知: Satrap 与 MaiBot 在同一百行级代码量, AstrBot 的 53 行切片只是转换层, 不能当成完整通知系统

这些不是“无需优化”的结论, 只是本轮横向数据不支持把它们列为第一批压缩对象

## 5. Satrap 整个 Issue #10 的增量账

以 `git diff --numstat main HEAD` 计算, 基线和终点均固定在第 1 节; 下表为净物理行数, 与前面“当前选定模块的代码量”不同:

| 类别 | 新增 | 删除 | 净增 |
| --- | ---: | ---: | ---: |
| 后端 Python | 11,448 | 343 | **11,105** |
| 前端源文件, 排除测试/生成物 | 2,468 | 116 | **2,352** |
| 生成契约 JSON | 303 | 0 | 303 |
| 测试、E2E、样例 | 13,357 | 9 | 13,348 |
| docs | 3,468 | 2 | 3,466 |
| 其余配置、脚本等 | 462 | 0 | 462 |

手工维护的后端 Python 与前端源文件净增 **13,457 行**, 测试与文档增长单列。代码长并非完全由测试掩盖; 但也不能把测试、归档文档和生成 JSON 全部算成运行时复杂度

下一步的评估顺序应为: **持久状态层 → 手动请求/诊断/试算与配置治理 → 专用前端的共用程度 → ASR 封装**。先核实每处差额对应的保证和维护成本, 再决定简化方案与预算; 本轮不承诺未经证明的减行目标
