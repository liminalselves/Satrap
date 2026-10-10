# 开发过程记录摘要

本文汇总 2026-09 至 2026-10 各轮计划, 审查与验收记录的结论. 原文已移入本地 `docs/archive/development/` (不入库), 只反映当时状态; 现行行为以各篇用户文档为准

## issue #12 安全审查 (PR #15)

- 基线 `1ecb13c`, 增量复审 `31604d6`, 修复提交至 `a153d5a`; 共 19 项, 无 P0
- 已修复: 本地媒体来源统一白名单校验; OneBot 空 token 回环监听告警, 非回环拒绝启动; sandbox / shell 子进程剥离密钥类环境变量; 会话扫描目录追加到 `sys.path` 末尾并校验模块来源; 文件 token 增加 24 小时 TTL 与 1024 条上限; group_admin 空 `allowed_callers` 拒绝模型写操作, 新增 `allowed_read_callers` 与 `high_risk_approval`
- 保留的已知限制:
  - 代码沙箱与 shell 依赖人工审批, 没有 OS 隔离; 环境变量过滤不阻止已批准代码读取配置文件
  - Windows 下鉴权文件不设 ACL, 非回环 HTTP 无 TLS; 主 token 无轮换接口
  - `/api/file/` 回调路由未实现, TTL 限制不代表文件服务可用
  - 文件工具的用户正则没有超时; 媒体授权与实际读取之间不是原子的文件系统隔离
  - 群窗口合成的 `[用户 ..., 消息 ...]` 标记可被正文伪造 (不改变冻结的调用来源); 合并转发展示名可由参数指定
- 现行说明: [媒体来源白名单](../getting-started/configuration.md#媒体来源白名单), [Coding 插件配置](../plugins/satrap-coding-plugin.md#插件配置)

## main 合并 (2026-10-05)

- 将远端 main `fb0d200` 合入 `fix/issue1`, 合并提交 `f5f0cdf`; 解析 9 个冲突并语义复核 17 个自动合并文件, 后端全量 3356 passed
- 升级行为变化 (已写入 [配置说明](../getting-started/configuration.md)):
  - group_admin 开启写操作但 `allowed_callers` 为空时拒绝, 不从群主, 管理员或其他名单自动补充
  - `high_risk_approval` 只能收紧审批, 不能取消宿主策略要求的审批; 高危动作统一进入持久审批队列
  - 非空 `media_allowed_roots` 完全替换默认授权, 缓存目录与表情库需显式列入

## group_chat 首批与按对话类型选择 Agent (2026-10-03)

- 平台新增 `session_bindings`, 私聊 / 群聊可分别绑定 Agent; 优先级为单群绑定 > 对话类型绑定 > 平台默认, 启用后使用 v2 隔离键, 私聊与群聊即使选同一 Agent 也不共享上下文 (见 [平台接入](../platform/platforms.md))
- 平台数据库新增消息档案, 在唤醒判断前采集已准入消息, 默认保留 30 天, 删除只影响检索不改模型上下文
- 结构化回复采用轮次草稿: `prepared` 只表示组件已核验, 轮次成功结束后单次提交, 未知回执不自动重发
- QQ 现场验收 GA1-GA4 通过 (六个工具, 未唤醒消息检索, 写开关关闭拒绝, 非空名片人工审批执行)
- 已知问题: SnowLuma v1.14.17 无法清空群名片 (空字符串字段被编码器省略, 返回 retcode 100 / QQ 1007), 未修复, 需在客户端手动清空; 现场观察到平台时间与本机时间偏差会影响入站 / 出站混合排序, 时间归一策略未改

## group_chat 第二阶段: 摘要, 图片与表情, 群记忆, 提醒 (2026-10-05)

- A1/A2 群摘要, B1/B2 图片与表情, C1/C2 记忆插件从 base_take 独立并新增群记忆 / 成员偏好, D1/D2 一次性提醒全部实现; 后端全量 3169 passed, 前端 234 passed
- 原计划中的每群功能覆盖接口 `/preferences`, 以及重复日程, 定时摘要, 图片提醒未实现
- QQ 现场验收通过: 成员偏好保存, 下一轮注入, 重启后读取, 更正与忘记; 群记忆待审批不生效, 人工批准后生效; 提醒短时发送, 到期前取消, 重启等待, 离线补发, 停用暂停后人工恢复
- 现场发现并修复:
  - 重启误暂停: 群接入快照未加载时路由代次为 -1, 提醒被永久暂停; 改为 `waiting / group_state_pending`
  - 离线误暂停: `ApiNotAvailable` 被当成平台明确拒绝; 改为可重试的 `member_unavailable`, 只有 `ActionFailed` 视为拒绝
- 遗留: 连接恢复不重置退避计时, 离线恢复后可能约 2 分钟才补发; `unknown` / `partial` 与崩溃提交窗口只用替身验证, 未在真实群制造重复消息
- 现行说明: [group_chat 插件](../plugins/group-chat-plugin.md), [memory 插件](../plugins/memory-plugin.md)

## 文案统一 (2026-10-06)

- 基于 `aca8329` 采集了群聊新增功能的全部模型 / 前端 / 错误文案 (约 700 条) 供逐条审阅
- 用词统一为: 本群共享信息称“群记忆”, 成员群内显示名称称“本群昵称”; 接口字段 `group_rule`, `card` 保持兼容

## 系统管理员与插件管理权限 (2026-10-06)

- 先对 group_chat, memory, group_admin, friend_manager 的 47 个工具做了静态权限核查, 记录各名单空值语义, 默认开关与审批要求
- 随后实现管理组 (`administrator_groups`), meta.yaml 管理权限声明与宿主统一授权, 接入七处调用者名单; 系统管理员只免填名单, 不跳过功能开关, 范围, 平台能力与审批
- 自动回归 3429 passed; QQ 现场 ADM1-ADM6 通过 (免填名单读取, 排除优先, 本地名单独立授权, 待审批申请在撤权后拒绝执行, 写开关关闭时管理员仍被拒绝), 保存管理员配置不重启后端
- 现场修正: `group_chat.environment` 的 `available_tools` 改名为 `available_group_chat_tools`, 避免模型误以为是完整工具清单
- 现行说明: [系统管理员](../plugins/system-administrators.md), [插件管理权限声明](../edictum/plugin-system.md#管理权限声明-可选)

## 尚未实施: 插件聊天命令

计划为业务插件补充聊天命令, 宿主侧的命令权限声明 (`command_permissions`) 与原生命令绑定接口已实现, 命令本身未实现:

- memory: `/memory list|get|edit|del`, `add self|group`, `pending|proposal|approve|reject`
- group_admin: `/group pending|action|approve|reject`
- friend_manager: `/friend list|find|requests|accept|reject|actions|approve|deny` (仅管理者私聊)
- group_chat: `/remind add|list|show|cancel|resume`, `/summary list|show|delete`, `/sticker list|collections|enable|disable`

约定: 身份只取自本轮原始消息, 命令参数不能自报审批者; 复用现有提案 / 动作状态机, 只批准明确的完整 ID, 不批准“最近一条”或“全部”; 批准不等于平台执行成功, 分别返回实际状态; 人工审批命令不绕过宿主禁止策略, 账号保护与原申请来源校验
