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

状态: 尚未执行. 需要由用户操作现有 QQ 测试环境, 不发送未经用户确认的实际平台消息

本地环境复核: 控制服务 /status 返回 running=false, 已配置的后端 19870 端口未监听; 运行中的旧控制服务对 /config/administrator-groups 返回 404. 需先重新启动控制服务及后端加载新代码, 再继续真实平台验收. 这次复核只读状态, 未修改用户配置或发送平台消息

1. 在系统设置创建测试管理组, 添加当前 OneBot 平台与测试用户 QQ 号, 指定 group_admin 和 friend_manager, 保存后确认“已保存并生效”
2. 使用已安装插件的私聊 Agent, 暂时清空 friend_manager.managers 和 write_callers, 保留原写开关状态. 测试用户请求实际调用 friend_manager 的好友列表读取工具, 应成功; 不修改好友关系
3. 排除 friend_manager 并保存生效, 新对话请求同一读取工具, 应拒绝或不再提供该管理工具. 恢复插件自己的 managers 名单后, 原名单路径应独立恢复读取
4. 在测试群启用所需群管理写功能并保持该动作需审批. 管理员提交一次修改机器人自身群昵称的申请, 应进入 pending, 不得自行执行
5. 批准前移除该管理员并保存生效, 原插件写名单保持空. 审批应拒绝已失去权限的来源, 机器人群昵称不改变. 可另提交新申请验证重新授权后的正常审批
6. 关闭写开关后重试, 即使管理员身份有效也必须拒绝. 若有其他测试成员, 同时验证未授权成员不能通过空名单获得管理入口
7. 恢复原插件名单, 功能开关与管理组配置, 清理验收申请. 不批准好友删除或其他无关写操作

页面使用说明见 [系统管理员与插件管理权限](../plugins/system-administrators.md)
