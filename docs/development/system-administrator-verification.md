# 系统管理员验收记录

日期: 2026-10-06

本记录对应 [实现契约](system-administrator-plan-contracts.md) 与 [插件权限契约](plugin-management-permission-contracts.md). 自动测试使用真实宿主, 会话安装和 HTTP 分派, 平台网络写操作通过测试适配器替代; 不将其标为真实 QQ 验收

## 自动覆盖

最终全量后端回归: 3429 passed, 21 skipped, 无失败. 跳过项为未显式开启的外部服务集成测试, reportlab 可选依赖与当前 Windows 会话的符号链接权限限制

全量后仅补充拒绝日志的真实平台 / 请求 ID, 并修正工具绑定读取的静态类型提示; 再回归 41 项授权与业务集成测试, 121 项实际会话测试, 均通过

前端: 234 项单测通过; 管理员与日志保留端到端验收通过; TypeScript, 变更文件 ESLint, Vite 生产构建及压缩通过. 本轮宿主与插件模块 Pyright 检查无错误

| 契约 | 验证入口与结果 |
| --- | --- |
| 空配置兼容, 管理组启停, 多组允许 / 排除, 同 ID 跨平台隔离, 平台重建失效 | test_administrator_groups.py |
| metadata 严格校验, 权限 AND, 旧名单独立授权, 未知入口拒绝, 普通入口兼容 | test_plugin_management_permissions.py |
| 原生命令可信身份, 拒绝模型工作流冒充, 拷贝作用域结束即撤销 | test_plugin_management_permissions.py; test_simple_session.py 的实际同步 / 异步安装与命令执行 |
| 映射不匹配实际注册入口时回滚工具, 命令和处理器 | test_simple_session.py 的同步 / 异步安装回滚 |
| 七项名单接入, 管理员免填, 开关保留, 私聊 / 群范围保留 | test_administrator_plugin_integration.py |
| 待审批动作仍需批准, 撤销 / 排除 / 关闭后执行拒绝, 无关授权更新不误杀 | test_administrator_plugin_integration.py 的真实持久申请与后端审批分派 |
| 好友删除保护包含有效系统管理员 | test_administrator_plugin_integration.py |
| 单区段原子保存, 冲突不覆盖, 平台身份规范由适配器提供 | test_administrator_settings_api.py |
| 后端读取已保存版本热应用, PID / runtime_id 不变, 拒绝任意正文与旧版本 | test_administrator_settings_api.py 的实际 HTTP apply 分派 |
| 保存与运行状态分开, 连接拒绝 / 超时 / 来源路径或版本不符不误报生效 | test_administrator_settings_api.py |
| 授权检查与接口故障记录日志并捕获, 后续请求仍可处理 | test_plugin_management_permissions.py; test_administrator_settings_api.py 的故障注入 |
| 无效单个平台配置不阻断其他平台初始化 | test_administrator_groups.py; test_platform_lifecycle.py |
| 动态平台和插件目录, 未接入 / 无效声明展示, 明确重绑, 草稿预览, 跨页签草稿保留 | satrap-ui/e2e/administrators.mjs |
| 冲突后保留草稿与最新版本对照, 保存不调用重启, 未确认时重试应用 | satrap-ui/e2e/administrators.mjs |

## 真实平台验收步骤

状态: 本轮核心验收完成, ADM1 / ADM2 / ADM2B / ADM3 / ADM4B / ADM5 / ADM6 已验证, 临时测试配置已恢复并核对. 由用户操作现有 QQ 测试环境, 只读核对配置, 上下文与持久化申请

本地环境复核: 控制服务 /status 返回 running=false, 已配置的后端 19870 端口未监听; 运行中的旧控制服务对 /config/administrator-groups 返回 404. 需先重新启动控制服务及后端加载新代码, 再继续真实平台验收. 这次复核只读状态, 未修改用户配置或发送平台消息

用户启动后的复核: 控制服务 /status 已返回 running=true, 管理员设置与后端状态接口均返回 200, 已保存区段与运行时修订一致. 记录热更新验收前的后端 PID 为 42904. 此时管理组为空, OneBot 的平台能力仍为 platform_disconnected; 私聊路由 onebot-private-test 尚无插件. 已请用户连接适配器, 在前端保存测试管理组并为私聊 Agent 启用好友列表读取, 等待实际操作结果

用户完成准备后的复核: OneBot 已连接, 群列表能力为 supported. QQ 管理组启用, 用户 2410323775 在 onebot-platform 上授权 group_admin, friend_manager, group_chat, 无失效成员或目录错误. 控制服务确认 applied, 与后端区段修订 551423ebbd588fa010fac4ed5563e852436473c0bde519078582213e0e46b2a7 一致; PID 仍为 42904 且 runtime_id 未变化, 验证真实前端保存管理员配置不重启后端. 私聊 onebot-private-test 已启用 friend_manager 与读取工具, managers / write_callers 均为空, 两个好友写开关均关闭. 下一步通过真实私聊消息验证管理员免填名单读取

ADM1 通过: 用户 2410323775 私聊实际路由 onebot-private-test. 只读查询平台数据库 chat_history, 在 ADM1 消息之后找到 friend_manager_list_friends 的真实调用, 参数为 {"limit":3}, 调用 ID call_00_QTtKbx4YU0q7AUsTFh5V0540; 对应 tool 记录返回 ok=true, data.items 长度为 3. 验证管理员可以在 managers 为空时执行读取, 未将模型文字回复当作工具执行证据, 未记录好友列表内容

ADM2 的撤销路径通过, 排除优先仍待复测: 实际保存的是从 included 移除 friend_manager, excluded 仍为空; 运行时修订 37429e8e94572769ad54dd4480ff313b8d02ab22f093aa9921b51c86b281c6a2 已应用, 后端 PID / runtime_id 未变, 私聊插件与读取工具仍启用且名单为空. ADM2 原始记录 id=1803 的 role 为 assistant, tool_calls / tool_call_id 均为 null, DSML 调用标记仅为普通文本, 没有 tool 结果或实际查询证据. 未将模型的文字调用标记视为执行, 未将没有调用结果误判为超时. 已请用户针对同一插件同时选择允许与排除后进行 ADM2B, 以验收真正的排除优先

ADM2B 排除优先通过: QQ 管理组的 included 与 excluded 同时包含 friend_manager, 已保存与实际运行修订均为 5329b5ab4e3b8c4cc35624d7bc52235722e8409597c4bd5962786f5784078818, PID / runtime_id 未变. 私聊插件与读取工具仍启用, managers / write_callers 仍为空, OneBot 好友读取能力为 supported 且无连接错误. 原始上下文在 ADM2B 后只有助手文字回复, tool_calls / tool_call_id 均为 null, 没有实际执行查询. 该拒绝符合排除优先, 不属于平台故障或插件未安装; 模型建议为机器人账号授权的说法没有宿主证据, 调用者授权应识别真实发言者 2410323775

ADM3 本地名单独立授权通过: QQ 管理组仍同时允许与排除 friend_manager, 运行时修订仍为 5329b5ab4e3b8c4cc35624d7bc52235722e8409597c4bd5962786f5784078818, PID 42904 / runtime_id 未变. 私聊 onebot-private-test 的 friend_manager.managers 已填写 2410323775. ADM3 原始消息 id=1872 后, id=1873 实际调用 friend_manager_list_friends, 参数 {"limit":3}, 调用 ID call_00_KuFVDyULkhIq4QN8owMl5023; id=1874 对应 tool 记录返回 ok=true, data.items 长度为 3, has_more=true. 证明中央排除只取消中央授权, 不阻断插件自己的有效名单, 未记录好友信息

ADM4 准备已核对: 群 1125293646 的 set_group_card 显式设为 approval_required, 后端返回 saved_revision=active_revision=3, apply_status=applied. 群路由使用 onebot-edictum, group_admin 已启用, write_tools_enabled=true, 昵称工具已启用, allowed_callers 已清空; 系统管理组仍授予调用者 2410323775 的 group_admin 权限, 运行时修订与 PID / runtime_id 未变. 已请用户提交修改机器人自身群昵称的申请, 保留 pending 待后续撤权审批验证

ADM4 首轮未完成: 原始消息 id=2012 的可信来源为群 1125293646, 发言者 2410323775, 实际路由 onebot-edictum. 模型只调用 group_chat_reply 说明工具不可用, 未调用 group_admin_set_group_nickname, 未产生待审批申请. 当时 saved / effective 配置均为写开关开启, 工具启用, allowed_callers 为空; 管理组授权与群审批已生效. 对该活跃会话执行只读插件预览, applied_fingerprint 与 desired_fingerprint 相同, 无待协调变更. 用相同已保存管理员配置 / 插件配置 / 来源身份构造隔离异步会话, 昵称工具授权来源为 administrator_group, available=true 且包含在工具定义中; 此复现未执行平台写操作, 不作为真实请求参数的证明. 发现 group_chat.environment 的 available_tools 只列 group_chat 工具, 可能被模型误读为完整工具列表; 当前普通执行未保存该轮完整 tools 参数, 暂不将失败定性为注入缺失或模型误判. 已请用户保持配置, 按实际工具接口复测 ADM4B

ADM4B 提交待审批通过: 原始消息 id=2030 后, id=2031 实际调用 group_admin_set_group_nickname, 参数 user_id=3588795965 / nickname=ADM4-待审批验收, 调用 ID call_00_P9YB3tcG4HnuqfUreR8D4972. 对应 id=2032 工具结果为 status=ok, action_id=2061456690ae5d97749ef5a62cec29bb, state=pending, decision_at / executed_at / result 均为空. 这次实际调用证明接口可用, 首轮“未开放”的说法与后续真实调用不符; 保留请求, 下一步撤销中央授权后验证审批执行重新核验来源

ADM5 撤权准备已确认: QQ 管理组 enabled=false, 已保存与运行时修订均为 88beeb9bc99a82018aaf7df8a103610894367972a4bff12bb47dc8c7fb1ed47e, applied, PID / runtime_id 未变. group_admin.allowed_callers 仍为空, 管理写开关仍开启; 原申请 2061456690ae5d97749ef5a62cec29bb 仍 pending 且未过期, decision_at / executed_at / result 均为空. 已请用户在此状态批准原申请, 等待实际拒绝结果

ADM5 审批前撤权通过: 用户批准原申请后, 数据库与前端均为 failed, result.reason=model_permission_revoked; 管理组仍关闭且运行时确认 applied. 后端日志记录同一 action_id / action=set_group_card / state=failed / reason=model_permission_revoked. 来源权限复核在 OneBot 写调用前执行并抛出, 本次未进入平台修改调用. 数据库 executed_at 已填写, 该字段记录流程结算时间, 不能当作平台成功执行证明; 平台结果以 state / result 为准

ADM6 管理员不能越过写功能开关通过: QQ 管理组已重新启用并 applied, group_admin.write_tools_enabled=false, 工具子能力仍开启, allowed_callers 仍为空. 后端群配置的 effective 结果一致, 活跃会话插件预览无待协调变更. 原始消息 id=2054 后助手 id=2055 无 tool_calls, 没有管理修改执行; 数据库中 ADM6-不应修改 对应申请数量为 0, 原 ADM4B 申请仍为 failed / model_permission_revoked. 本次后端 PID 已变为 37712, runtime_id=hD0v8oQBMw6ckC0lulqk411gOhBp-I1O, 不将此轮当作不重启的证明; 之前管理员区段保存与撤权已有 PID 不变的独立证据. 模型引用旧 pending 状态不代表原申请恢复待审批

收尾配置核对通过: QQ 管理组启用, 允许 group_admin / group_chat / friend_manager, excluded 为空, 无失效成员或插件目录错误. 已保存与运行时修订均为 e77f85e75706f1cd4dc9f86291ecbe54c2a8ece338c8bbb60a1bca5be172ab63, applied; 恢复时 PID 37712 / runtime_id 未变. group_admin.write_tools_enabled=true / allowed_callers 为空, 活跃群会话 applied_fingerprint 与 desired_fingerprint 一致; 修改群昵称的需审批设置保留. onebot-private-test 的 friend_manager.managers 已清空, 该私聊 Agent 当前没有活跃实例, 后续激活从已保存配置安装; 不声称已核对不存在实例的运行配置. 验收失败申请保持终态, 未发送额外平台消息或执行任何好友写操作

提示词范围歧义已修正: group_chat.environment 的 available_tools 改为 available_group_chat_tools, 注入说明明确只列本插件工具, 其他插件是否可调用以本轮实际工具接口为准. 24 项群聊插件与管理员组测试通过; 分别在同步 / 异步会话中安装 group_chat 和 group_admin 并经过实际 PipelineScheduler 调用, 捕获首轮模型请求, 确认环境清单只包含 group_chat 工具, 实际 tools 参数仍包含 group_admin_set_group_nickname, 且范围说明已注入. 验证使用离线模型与测试适配器, 未执行真实平台写操作. 当前运行后端需重新加载代码后才能使用新说明; 不宣称已经完成修改后的真实 QQ 复测

1. 在系统设置创建测试管理组, 添加当前 OneBot 平台与测试用户 QQ 号, 指定 group_admin 和 friend_manager, 保存后确认“已保存并生效”
2. 使用已安装插件的私聊 Agent, 暂时清空 friend_manager.managers 和 write_callers, 保留原写开关状态. 测试用户请求实际调用 friend_manager 的好友列表读取工具, 应成功; 不修改好友关系
3. 排除 friend_manager 并保存生效, 新对话请求同一读取工具, 应拒绝或不再提供该管理工具. 恢复插件自己的 managers 名单后, 原名单路径应独立恢复读取
4. 在测试群启用所需群管理写功能并保持该动作需审批. 管理员提交一次修改机器人自身群昵称的申请, 应进入 pending, 不得自行执行
5. 批准前移除该管理员并保存生效, 原插件写名单保持空. 审批应拒绝已失去权限的来源, 机器人群昵称不改变. 可另提交新申请验证重新授权后的正常审批
6. 关闭写开关后重试, 即使管理员身份有效也必须拒绝. 若有其他测试成员, 同时验证未授权成员不能通过空名单获得管理入口
7. 恢复原插件名单, 功能开关与管理组配置, 清理验收申请. 不批准好友删除或其他无关写操作

页面使用说明见 [系统管理员与插件管理权限](../plugins/system-administrators.md)
