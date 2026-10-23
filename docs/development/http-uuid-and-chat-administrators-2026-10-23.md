# HTTP UUID 与授权群内全员管理验收

日期: 2026-10-23

## UUID 修复

公网 HTTP 页面不是安全上下文, 浏览器未提供 crypto.randomUUID, 原前端在创建运行时应用任务时同步抛错, 配置虽已保存但热应用请求未发送

新增统一 createUuid, 优先使用原生 randomUUID, 缺失时使用 getRandomValues 生成标准 UUID v4, 不使用 Math.random 或时间戳替代; 缺少安全随机源时明确失败

已替换全部前端直接调用, 包含运行时任务, 管理组与成员例外, 好友动作, 群动作, 手动唤醒, 贴纸, 记忆和提醒等入口

验证:

- 运行时任务模块 15 项通过, 包括 HTTP 回退, ID 唯一性与格式, 丢响应后沿同一 ID 只读核查, 不重放 POST, 无安全随机源不发请求
- Chromium 实际非安全 HTTP 域名中 isSecureContext=false 且 randomUUID=undefined, 从生产工具源码转译的函数连续生成 1,000 个合法且不重复的 UUID
- 浏览器 scoped-agent-apply 与 runtime-application-progress 均在禁用 randomUUID 后通过, 覆盖保存, 完整热应用, 阶段轮询, 120 秒截止, 后续查询, 部分失败和精准重试
- 旧 E2E 夹具未实现新目录快照接口, 已显式返回 404 来覆盖受支持的旧目录回退, 不用伪造的 200 成功响应掩盖接口缺失

## 群聊授权

入口为“系统设置 → 管理员 → 管理组详情 → 授权群聊”, 添加平台实例与真实群号, 选择适用插件后保存并应用. 管理组可以同时包含指定成员和授权群聊, 不需要登记或查询全部群成员

配置新增可选 chats: [{platform_id, platform_instance_id, chat_id}]. 空 chats 不写入规范配置, 升级不改变旧管理组的修订. 平台代次由服务端绑定, QQ 群号按十进制规范化, 不支持群聊的平台拒绝保存

- 仅可信来源为该群的真实平台成员获得授权, 同一成员私聊或在别群发言时不会继承
- 以实际调用者叠加成员例外, 保留指定插件与所有插件的范围规则, 原插件名单仍可独立授权
- 群授权没有赋予全员账号保护, 也没有修改真实 QQ 管理员角色
- group_admin 仅靠群授权时只能操作来源群, 明确的个人授权仍按原范围处理
- 功能开关, 宿主审批, 计划模式, 账号与平台能力检查沿用原链路; 撤销授权后待审批动作在执行前重新核验
- 权限来源标记为 administrator_chat, 指纹包含来源群, 防止不同群的授权结果混用
- 平台删除重建后授权失效, 客户端伪造代次不能恢复; 可预览并通过 rebind_chats 显式重绑
- 含群聊的旧指定插件排除不能展开为固定个人例外, 保留原排除并提示待迁移, 不丢失否决

已在同步与异步实际工具及原生命令链路验证, 包括群内不同普通成员, 私聊和别群拒绝, 模型转发命令拒绝, 待审批动作撤权, 原名单独立授权和账号保护保持

用户操作与 YAML 示例见 [系统管理员说明](../plugins/system-administrators.md)

## 验收结果

| 检查 | 结果 |
| --- | --- |
| 前端 Vitest | 18 文件, 308 passed |
| TypeScript 与生产构建 | 通过 |
| Pyright, 7 个变更生产 Python 文件 | 0 errors, 0 warnings |
| Windows Python 3.13, 7 个相关大流程模块 | 348 passed, 1 skipped |
| 原生 Linux Python 3.14.4, 同 7 个模块 | 349 passed |
| 管理员浏览器流程 | 新增群聊, 保存, 重绑, 删除, 草稿, 冲突与运行确认通过 |
| Edictum 浏览器流程 | 无 randomUUID 的保存与运行时应用两场景通过 |

后端模块: test_administrator_policy_flows, test_administrator_management_flows, test_group_administration_flows, test_request_approval_flows, test_account_request_flows, test_control_ui_flows, test_permissions_flows

Windows 跳过项为当前会话无符号链接权限, 相同用例已在 Linux 通过; Linux 的 14 条警告来自既有 asyncio.iscoroutinefunction 弃用提示

最终群管理复核调整后, Windows 与原生 Linux 的管理员策略和群管理流程各 133 项通过, 保持每次目标解析重新核验授权, 类型检查通过

本轮仅修改代码与测试, 未连接或部署服务器, 未配置任何实际群的全员授权
