# 插件管理条目声明与统一授权契约

日期: 2026-10-06

状态: A / B / C 已实现; D 的自动验收与文档已完成, 真实平台验收待执行. 结果见 [验收记录](system-administrator-verification.md)

关联设计: [系统管理员与插件授权计划](system-administrator-plan-contracts.md), [插件聊天命令计划](plugin-command-plan-contracts.md), [现有权限核查](plugin-permission-audit.md)

## 1. 本轮边界

实现系统管理员配置, 管理组的插件范围, 插件管理权限声明及宿主统一校验. 首次接入 group_chat, group_admin 和 friend_manager 的七处名单检查

本轮不增加业务审批命令, 不调整 memory 模型工具的所有权与提案规则, 不改 satrap_coding 的执行权限. 为后续命令定义同一声明与校验接口, 不为工具和命令各建一套授权引擎

系统管理员与插件原有名单均可授予调用者身份; 功能开关, 插件 / 工具启停, 主工作流限制, 操作范围, 来源有效性, 平台能力, 账号保护及逐次审批仍由原业务规则约束

## 2. 管理条目如何识别

插件通过 meta.yaml 明确声明管理权限, 并将权限绑定到工具或命令. 不通过工具名, config_schema 字段名, 中文描述或模型判断猜测其是否属于管理功能

| 元数据字段 | 契约 |
| --- | --- |
| permission_schema_version | 使用本契约时填写 1; 不支持的版本拒绝加载该权限声明 |
| management_permissions | 权限 ID 到规则的映射; ID 只在当前插件内有效 |
| tool_permissions | 已声明工具名称到所需权限 ID 列表的映射 |
| command_permissions | 已声明命令名称到默认权限 / 子命令权限的映射 |

每个管理权限规则:

| 字段 | 类型与要求 | 含义 |
| --- | --- | --- |
| description | 必填非空字符串 | 前端显示的权限用途 |
| requirements | 可选列表, 每项为非空字符串, 最多 32 项 | 展示功能开关, 范围和审批等剩余条件; 仅为说明, 不参与授权判断 |
| caller_list | 可选 config_schema 字段名 | 使用此插件实际安装配置中的 ID 名单作为本地授权来源 |
| empty_policy | caller_list 存在时必填, allow 或 deny | 名单空时的本地判断, 保留既有语义 |
| system_admin | 必填布尔值 | true 时允许管理组授予该权限; false 时仅使用本地名单 |

没有 caller_list 时, 本地名单路径恒为拒绝, empty_policy 不得出现. 它适用于未来只允许明确授权管理员执行的审批子命令

最小有效示例, 展示入群申请查询与处理的实际组合:

```yaml
permission_schema_version: 1
tools:
  group_admin_list_group_requests: 查询申请
  group_admin_handle_group_request: 处理申请
config_schema:
  allowed_read_callers:
    type: textarea
  allowed_callers:
    type: textarea
  request_managers:
    type: textarea
management_permissions:
  read:
    description: 使用群管理查询工具
    caller_list: allowed_read_callers
    empty_policy: allow
    system_admin: true
  write:
    description: 请求群管理修改和发送操作
    caller_list: allowed_callers
    empty_policy: deny
    system_admin: true
  requests:
    description: 查询和处理入群申请与邀请
    caller_list: request_managers
    empty_policy: deny
    system_admin: true
tool_permissions:
  group_admin_list_group_requests: [read, requests]
  group_admin_handle_group_request: [write, requests]
```

一个入口列出多个权限时全部满足, 即 AND. 每个权限内部采用“本地名单允许 OR 获得系统管理员授权”. 未绑定管理权限的普通入口继续原业务行为, 不因管理员身份自动提升权限

## 3. 命令与子命令声明

后续命令采用同一规则, 格式为:

```yaml
permission_schema_version: 1
commands:
  memory: 群记忆管理
management_permissions:
  approve_memory:
    description: 查看并批准或拒绝本群的群记忆提案
    system_admin: true
command_permissions:
  memory:
    subcommands:
      pending: [approve_memory]
      approve: [approve_memory]
      reject: [approve_memory]
```

此例是未来 memory 命令的契约, 不代表今天已有这些子命令. 可选 default 填权限列表, 对命令整体设定门槛. 子命令执行时使用 default 与精确匹配的 subcommands 规则的并集, 不能用子命令配置解除命令默认权限. 没有 default 或未匹配的子命令不追加权限, 继续其原有普通业务校验

子命令名精确匹配已解析的原生命令 token, 不按文本前缀匹配. 参数内容, 引用, 转发和模型输出不能选择人工审批身份或成为人工批准. 插件必须在注册信息中声明子命令, metadata 中的不存在子命令不能静默忽略

## 4. 加载与兼容契约

1. 没有本契约字段的旧插件保持原行为, 不自动接入系统管理员授权
2. 有权限映射却没有合法版本或权限定义, 视为无效 metadata
3. 权限 ID 必须是唯一非空字符串; 映射必须引用当前插件的已声明入口和已定义权限, 权限列表不得为空或含重复项
4. caller_list 必须引用已声明且支持 ID 列表的配置字段; 解析使用共享的字符串逐行 / 字符串列表规则, 不把数字, 布尔值或对象强制转成身份
5. 未知规则字段, 无效 empty_policy, 非布尔 system_admin, 不合法子命令结构均拒绝加载相关插件, 前端显示可读原因, 后端记录日志
6. 插件目录扫描与实际安装使用同一解析器; 不能出现前端认为接入, 运行时忽略声明的情况
7. 单个插件声明错误不得中断目录扫描或平台进程, 不退化成无条件允许

接入版本 1 时, 通过权限服务使用的入口须列入 tools 或 commands. 已声明且未映射管理权限的入口是普通入口, 不新增管理员身份要求; 未知名称不能伪装成普通入口

为 PluginCatalogEntry 与安装后的插件对象增加不可变权限声明. 目录接口返回 permission_schema_version, management_permissions 和入口映射, 供配置页面展示; 不返回其他用户名单或本轮身份

“已接入插件”指至少有一个合法管理权限声明 system_admin=true. 仅安装插件或列出工具不构成接入

## 5. 宿主统一函数

新增宿主权限模块, 由插件目录与运行时加载器提供已校验规则和入口归属. 对外接口为:

```python
authorize_plugin_entry(binding: PluginEntryBinding, *, subcommand: str | None = None) -> AuthorizationDecision
require_plugin_entry_permission(binding: PluginEntryBinding, *, subcommand: str | None = None) -> AuthorizationDecision
```

第一项返回结果, 用于声明过滤和授权预览; 第二项只在结果为 denied 时抛出稳定权限异常, 用于执行边界. 两项共用同一判断实现

PluginEntryBinding 由宿主创建, 固定实际安装插件, 入口种类, 工具 / 命令名称与实例引用. config 从当前实际安装状态解析, CallOrigin 从可信作用域读取. 模型参数不得提供 binding, 管理员身份, 管理组, 名单或配置版本

AuthorizationDecision 的字段契约:

| 字段 | 含义 |
| --- | --- |
| status | allowed, denied 或 not_applicable; 后者表示已知普通入口未绑定管理权限 |
| plugin_name / entry_kind / entry_name | 固定当前安装入口身份 |
| required_permissions | 实际需要的权限 ID |
| grants | 每项权限的允许结果, 来源为 local_list, local_empty_allow 或 administrator_group, 以及命中的管理组 ID |
| reason_code | 稳定失败原因, 如 permission_denied, identity_missing, entry_disabled, stale_authorization, invalid_permission_config, authorization_error |
| policy_revision | 本轮读取的授权配置版本, 用于等待后的复核 |
| permission_fingerprint | 当前身份与相关权限规则 / 有效授权的指纹, 用于持久申请复核 |

未知入口, 插件或入口已停用, 管理入口缺少可信平台身份, 运行时名单结构错误返回 denied; 不能按 not_applicable 处理. 未映射管理权限的普通入口不新增平台身份要求. 真实平台调用者通过平台实例及 instance_id + actor_id 匹配管理组. 控制面人工 API 保持其现有鉴权入口, 不伪造平台管理员来源

授权检查本身抛出异常时仍按拒绝处理, 记录完整堆栈并返回 authorization_error; 该原因码与名单结构错误的 invalid_permission_config, 以及调用者确实未获授权的 permission_denied 相互区分. 转换为 PluginPermissionDenied 时只有 authorization_error 使用 “管理入口权限检查暂时失败, 请查看后端日志” 提示, 其余原因码保持 “当前调用者未获得 <插件> 管理入口权限” 文案

commands 入口另需宿主在已识别原生命令后创建的可信执行作用域; 仅有某人的 CallOrigin 不足以调用审批命令. 模型工具作用域不能创建 command 授权, 插件注册表中的命令名称也不能直接当成已获批准的调用凭证

为管理组身份计算和入口授权分别提供内部函数, 但插件仅使用上述宿主 API. 统一服务不依赖 group_admin, friend_manager 等业务插件, 业务插件也不相互读取配置或调用工具

## 6. 管理组的确定规则

管理组 schema 和前端见关联的系统管理员计划; 核心规则固定如下:

1. 未配置管理组时, 原名单检查结果保持一致
2. 仅启用且匹配真实平台实例和用户 ID 的组参与计算
3. selected 模式仅授予 included 插件; all 模式授予所有合法接入的插件, 含后续接入者
4. 多组允许范围取并集, 任一命中组的 excluded 优先于管理组允许
5. 排除取消系统管理员授权路径; 原名单仍可独立授权, 包括原有空名单允许语义
6. 有效系统管理员可以通过该插件 system_admin=true 的所有权限, 无须分别填本地名单; system_admin=false 不接受该路径
7. 管理组只选择插件范围, 本批不增加逐工具的管理组授权开关. 插件通过 system_admin 标明可接受的自动授权条目, 前端逐项展示
8. 插件未安装或暂不可用不删除已保存选择; 尚未接入不授予权限. 平台同名删除重建后旧成员授权失效, 必须明确重新绑定

例: 某人获 group_admin 授权但本地 allowed_callers 与 request_managers 都空, 可以通过 write 与 requests 身份检查. 若 write_tools_enabled=false, 处理申请仍拒绝; 若动作需审批, 仍产生 pending 记录

## 7. 各插件迁移表

| 插件 / 权限 ID | 原名单字段 | 空名单规则 | 绑定入口 |
| --- | --- | --- | --- |
| group_chat / group_list | cross_group_query_callers | deny | group_chat_list_groups |
| group_chat / self_nickname | nickname_allowed_callers | allow | group_chat_set_group_nickname |
| group_admin / read | allowed_read_callers | allow | get_honors, get_forward, list_group_requests |
| group_admin / write | allowed_callers | deny | send_forward, recall_message, kick, ban, whole_ban, ban_anonymous, set_admin, set_anonymous, set_group_nickname, set_name, set_title, leave, handle_group_request |
| group_admin / requests | request_managers | deny | list_group_requests, handle_group_request |
| friend_manager / access | managers | deny | 本插件全部五个工具 |
| friend_manager / write | write_callers | deny | handle_request, delete_friend, 同时要求 access |

本表省略的工具名前缀为其插件名称, 实际 metadata 使用完整名字. 上述七个权限均声明 system_admin=true

每个插件迁移后删除已替代的直接成员名单判断; 后端来源权限复核也调用统一服务. 继续保留功能开关和业务校验, 不保留新的授权判断与旧名单拒绝逻辑并行执行. 未迁移插件保持原逻辑

memory 的自动注入, 普通查询和本人偏好写入不标记为新增管理授权入口. 未来人工审批 / 管理命令再显式声明权限, 模型共享群记忆写入继续 pending. 既有 satrap_coding 受保护命令先保留原授权路径, 不随本次插件范围配置自动放宽

## 8. 展示, 执行与撤销

工具声明过滤和执行均读取相同规则与当前安装配置; 过滤结果不能作为之后免检执行的凭证. 同一处理步骤内的入口判定复用同一份结果, 步骤结束后立即失效, 不跨步骤也不跨异步等待. 异步等待后和平台写入前重验必要权限, 不将管理员布尔值缓存到 Agent 对话中

group_admin / friend_manager 原来源授权回调及后台指纹检查接入同一入口规则, 保留原会话, 机器人账号, 路由, 连接与业务审批策略的核验. 新管理组配置不能改变这些固定目标

命中组, 成员绑定, 相关插件授权或管理规则变化时重算有效授权. 无关用户或插件配置变化不因全局 revision 改变就使全部申请失效. 原本已有的本地授权未变化时, 移除额外系统授权不影响该本地路径

当前有效授权撤销则拒绝未执行申请; 相关来源规则改变造成原业务权限指纹冲突时要求重新提交, 不静默替换旧申请的授权依据. 本轮不回滚已经执行的平台操作

授权检查与写事务使用一致快照, 发现版本变化时重算或拒绝 stale_authorization; 不能在检查后使用已撤销的管理员身份发起平台操作. 平台请求已提交后权限变化仍须如实记录实际 / 未知结果, 不声称可撤回已提交动作

系统管理员账号的好友删除保护接入宿主保护集合, 不通过伪造 friend_manager managers 内容实现. 保护只在对应平台且有效启用的管理组身份上生效, 与插件管理员放行范围分别计算

日志记录请求 / 操作 ID, 真实身份, 插件入口, 授权来源及命中的管理组, 拒绝原因; 不记录服务鉴权密钥. 每项权限的正常预览 / 工具过滤拒绝避免重复刷日志, 实际执行拒绝记录日志并转换为可读结果. 所有异常在入口捕获, 不向平台进程传播

## 9. 前端与保存接口

系统设置增加管理员页签, 提供管理组卡片和成员编辑, 平台动态选择器, 用户 ID 输入, 插件多选 / 所有模式 / 排除多选及授权预览. 不要求用户编辑 YAML 或填内部权限 ID

选中插件显示其可接受的管理条目, 并注明仍需开启的业务功能及审批. 原插件配置中的名单保留, 说明补充“授权此插件的系统管理员无需重复填写此名单”; 安装子能力开关与管理员放行分别展示

新增配置区段接口:

| 接口 | 契约 |
| --- | --- |
| GET /config/administrator-groups | 返回管理组, 整体配置 revision, 区段 revision 及运行时应用状态 |
| PUT /config/administrator-groups | 提交完整组列表与 expected_revision, 只更新该配置区段; 新成员由服务绑定平台实例代次 |
| POST /config/administrator-groups/apply | 应用已保存区段指定 revision; 不接受一份未保存的授权正文 |
| POST /config/administrator-groups/preview | 校验编辑草稿并返回所选真实平台注册成员的有效插件范围, 仅预览, 不保存或授予权限 |

接口沿用控制服务鉴权. 版本冲突返回 409 并保留前端草稿, 不覆盖其他设置. 身份与规则结构错误返回 400 并记录失败原因; 平台被移除的已有成员显示失效, 新提交成员不允许绑定不存在的平台

保存后通过现有后端管理通信路径同步不可变授权快照, 使用已保存 revision 与宿主应用 revision 比对状态, 不重启整个后端. 后端未运行时保存用于下次启动; 应用超时显示“已保存, 无法确认运行时生效”并提供重试, 不自动声称管理员已获授权或已撤销

## 10. 实施批次与交付契约

| 批次 | 范围 | 交付与验证 |
| --- | --- | --- |
| A | 管理组 schema, metadata 解析, 目录返回及统一授权服务 | 声明严格校验, 空配置兼容, 平台身份隔离, 多组排除优先, 七项名单的 allow / deny 空值语义 |
| B | 三个插件执行 / 过滤迁移, 后端审批复核, 好友保护 | 管理员免填名单可用, 普通成员仍按原名单, 开关关闭仍拒绝, 审批仍生效, 无重复旧名单校验 |
| C | 配置区段 API, 应用状态与前端管理员设置 | 草稿保留, 409 冲突, 同名平台重建失效, 动态插件目录, 保存不重启, 后端应用状态准确 |
| D | 文档, 真实平台验收与后续命令接缝 | 身份不能从工具参数冒充, 移除管理员使未执行请求拒绝, 群组 / 私聊限制保持, 删除好友仍待审批 |

自动验证至少覆盖: 元数据引用错误, 未接入插件, 管理组 all / selected / excluded, 多组 AND 权限组合, 同用户 ID 跨实例隔离, 本地名单与系统授权两条路径, 子工作流既有写限制, 热更新竞态和待审批来源复核

人工验收使用授权管理员与普通成员分别请求同一管理操作, 验证管理员免填名单且普通成员拒绝; 再关闭功能开关, 排除插件, 移除成员并检查拒绝. 需要审批的动作确认仍进入 pending, 原有普通查询与个人操作回归验证

每批形成独立可审阅更改并分批提交. 本文件定义完整交付契约, 核心模块的实现不代表业务迁移与前端已完成
