# main 合并计划与行为契约

状态: A-E 已实施并通过合并结果回归, 证据见 [合并验收记录](main-merge-verification.md)
比较日期: 2026-10-05

## 1. 固定基线与范围

| 项目 | 基线 |
| --- | --- |
| 本地目标分支 | fix/issue1, 3244000 |
| 待合入远端 main | fb0d2001452808c766a295597337937dc34650f3 |
| 共同祖先 | 0638b90db2526700113e83f64faf731f94b9ef71 |
| main 独有 | 9 个提交, 51 个文件 |
| 本地独有 | 125 个提交, 434 个文件 |
| 双方共同修改 | 26 个文件 |
| Git 试算冲突 | 9 个文件 |

origin 和 liminalselves/Satrap 的远端 main 在比较时一致. 本地 main 停留在 1ecb13c, 本方案使用远端 main. PR #14 的实现已经属于共同祖先; 本轮新增内容主要来自 PR #15 / review/issue-12

执行前重新获取远端引用并检查工作区. 若远端或本地基线变化, 先补充比较和影响清单, 不套用旧冲突数量. 合并保留双方提交历史, 不 rebase 已共享分支, 不强推. 不自动修改运行配置, 停止服务或清理数据库

本轮只处理合并及集成必需的适配, 不同时实现连接恢复后提前补发, 新平台或新插件功能

## 2. 实施批次

实施批次用于逐项实现和验证; 不在 Git 尚有冲突或代码无法运行时提交半成品

| 批次 | 实施内容 | 完成门槛 |
| --- | --- | --- |
| A 基线与冲突清单 | 固定双方 SHA, 保存本地目标引用, 更新差异和试算结果; 确定每个冲突和自动合并重叠文件的处理方式 | 工作区和基线明确, 9 个已知冲突均有处理契约, 历史可恢复 |
| B 媒体与适配器 | 合入路径校验, file URI, 媒体组件, OneBot token 规则及发送失败原因; 精确适配表情库目录 | 合法资产/表情可发, 非媒体数据拒绝, 中文 URI 往返, 暂时离线和未知回执契约不变 |
| C Coding 与加载生命周期 | 合入子进程剥敏, 实例配置, 实际沙箱保护, 会话模块加载与插件失败清理; 合并 base_take 和两版安装回滚 | 多会话配置无串用, 失败安装无残留, 原有技能和工具状态正确, memory 保持独立 |
| D 群管理授权 | 合入显式写调用者名单和只读限制, 将高危审批开关接入宿主持久审批; 适配来源复核与权限摘要 | 一次申请和一次执行, 撤权后拒绝, 当前工具边界和好友隔离不变 |
| E 回归与文档 | 检查自动合并文件, 更新配置与升级说明, 运行后端/前端全量及定向浏览器检查 | 无未处理冲突, 合并结果测试通过, 差异和历史复核完成 |

实际合并使用 no-commit / no-ff 流程, 先在隔离检出中完成全部解析和检查, 再交付到目标分支. 合并不触碰当前运行数据. 提交按以下边界组织:

1. 合并提交: 双方完整历史, 全部冲突解析, 必需的集成适配及对应测试, 包含 main 原有文档
2. 文档提交: 本地新增合并验收结果和升级说明

无关功能改进另开后续提交. 默认仅本地提交, 不推送或部署. 如果验证失败, 保留可定位的失败结果, 修复后再提交, 不以选择整文件一侧或放宽测试消除失败

## 3. 媒体与平台契约

### 3.1 来源与授权

- 统一入口解析 HTTP(S), Base64, Data URL, 裸路径和 file URI; 先校验本地路径, 不以文件存在与否跳过检查
- 使用真实路径解析处理符号链接和 ..; 中文, #, % 等文件名通过标准 URI 往返
- 默认媒体目录保留 main 的精确布局规则: .satrap/sandbox, 实际 data_root 下的平台 cache 和会话 uploads/artifacts/sandbox/cache
- 补充默认授权仅限实际 data_root/group-chat/stickers 内的受管理图片文件. 不放行整个 group-chat 或 data_root; catalog.db, platform.db, 凭据和其他运行数据仍拒绝
- 已核查 main 的实际函数: 平台 cache/group-chat/assets/probe.png 允许, group-chat/stickers/probe.png 拒绝, group-chat/catalog.db 拒绝. 新表情目录规则必须增加拒绝相邻数据库及符号链接越界的回归
- 非空 media_allowed_roots 替换默认授权, SATRAP_EXTRA_MEDIA_ROOTS 追加授权; 显式覆盖时表情库和下载缓存不会自行获得权限
- 白名单只控制本地媒体来源. 远程下载继续经过既有出站 URL/DNS/重定向检查; 不能将 HTTP 分类当作下载授权
- 白名单授权与群资产授权同时成立才可发送. 保留资产来源复核, 当前群范围, 生命周期租约, 撤回/删除失效和并发清理保护

### 3.2 OneBot 与发送结果

- 非回环监听且 token 为空时记录错误并拒绝启动; 回环监听空 token 仅告警. 不自动生成或修改用户 token
- 保留本地账户隔离, 接入快照, 群会话覆盖, 动态 Agent 路由, 消息档案和后台提醒队列
- 媒体越界在平台调用前拒绝, 返回 media_source_denied, 不回退另一媒体属性绕过拒绝
- ActionFailed 属于明确平台拒绝; ApiNotAvailable 等通信异常的读取可重试, 已提交写操作或发送保持未确认, 不自动重复执行
- 只有真实确认消息 ID 才登记成功出站. partial / unknown 不伪装为 sent; 失败不能中断其他任务和消息循环
- 保留 D3 启动未就绪等待, D4 离线退避与宽限补发, D5 停用暂停及显式恢复. 不复活已暂停/取消/过期的历史任务

## 4. Coding 与插件生命周期契约

- workspace_root, sandbox_root, protected_dirs, allowed_env_vars, shell_timeout 按工具/会话实例保存; 其他会话空配置不得继承先前实例权限
- 宿主工作区/沙箱/缓存根优先于独立插件兜底配置. 同一会话工具, 命令和处理器共用同一状态; 数据根更换需卸载后重装, 不搬迁或删除旧文件
- main 的 CodeSandbox 环境剥敏同时接入 base_take; 只合入 allowed_env_vars 解析和传递, 不恢复 MemoryStore, 旧记忆状态或旧记忆工具
- 文件读取, 写入, 编辑, 批量替换, glob 和 grep 共用当前实例的保护规则. 实际沙箱与工作区交集可以作为既有例外, 敏感文件与其他保护目录仍受保护
- 执行等待审批后重新核验计划模式, 工作区, 开关和权限, 不依据审批前快照继续写入
- 子进程剥离密钥类环境变量, 显式放行按平台大小写规则处理; 不宣称环境过滤是 OS 隔离. 保留审批和超时
- 会话扫描目录追加到 sys.path 末尾, 避免模块名冲突和重复执行; 加载失败移除半初始化模块, 更新失败恢复旧模块
- 同步和异步安装失败清理本次工具, handlers, commands, MCP, 技能, 资源和插件状态; 调用 cleanup, 同时恢复原有工具启用状态
- 重复安装已存在插件的拒绝路径不清理原实例. 卸载和失败回滚各自最多清理本实例一次; 清理失败有日志, 不覆盖原始安装错误
- 保留首次安装技能激活和配置状态, 不退回需要模型再激活的旧行为

## 5. 群管理授权与审批契约

### 5.1 调用者与插件边界

| 配置 | 合并后的行为 |
| --- | --- |
| group_admin.write_tools_enabled | false 时拒绝全部模型管理写操作 |
| group_admin.allowed_callers | 仅控制管理写操作; 非空且包含真实 actor_id 才允许, 空名单拒绝写操作 |
| group_admin.allowed_read_callers | 新增, 空名单不额外限制 group_admin 当前只读工具; 非空时仅列表成员可读 |
| group_admin.allowed_groups | 额外限制目标群, 空值不额外限制; 不能覆盖宿主实际群范围 |
| group_admin.request_managers | 保留, 空值禁用加群申请查询/处理; 写处理还必须满足写开关和 allowed_callers |
| group_admin.high_risk_approval | 新增, 默认 false; true 时要求高危模型动作通过宿主持久审批 |

这是明确的升级行为变化: 旧配置开启写操作但 allowed_callers 为空, 合并后会拒绝. 不从群主、管理员、command_operators 或 request_managers 自动填充写名单, 不自动改变用户的授权配置. 文档和配置界面明确空值语义

group_admin 的只读限制仅作用于该插件现存工具, 不自动传播到 group_chat. friend_manager 继续使用自己的 managers/write_callers/保护名单, 不接受 group_admin 配置

已迁移的群/成员查询和 get_message 保持在 group_chat, 好友工具保持在 friend_manager. 不恢复 group_admin_set_card 等旧名称或重复入口. group_chat 的自身群昵称降级能力和独立授权保持现状. 插件之间不 import 对方, 不读取对方配置或调用对方工具; 通用复核通过宿主接缝实现

### 5.2 一个持久审批路径

- main 的逐次输入审批意图通过现有群管理动作队列实现, 不另外询问 user_input_provider, 不生成第二个审批请求
- 高危范围按 main 意图映射到当前动作: 踢人, 成员禁言, 全员禁言, 匿名禁言, 设置管理员, 修改群名, 退群/解散和处理加群请求. 已迁出的好友请求不属于此插件
- 有效要求为: 宿主账号/群策略要求审批 OR 当前插件要求该高危动作审批. true 只收紧, false 不能取消宿主审批
- 插件强制审批要求通过可信来源接缝交给宿主, 不能作为模型可填写的工具参数. 来源权限摘要纳入写名单, 高危审批要求和 request_managers 等当前有效权限; 提交/批准/执行前均复核
- 宿主拒绝群范围, 账号失配, 来源失效或写权限时先拒绝, 不创建可批准的动作. 审批服务不可用时明确拒绝, 不回落即时输入或直接执行
- 返回 pending 只表示申请已保存; succeeded 才表示已执行. failed/unconfirmed 和过期必须分别呈现, 相同申请不得执行两次
- 保存账号, 平台, 目标群, 来源工具和会话身份; 审批展示实际目标和参数. 来源工具移除, 会话切换, 插件停用或权限摘要变化后旧申请不得继续执行
- 后台人工操作保留自己的宿主权限与逐群策略, 不套用模型插件调用者名单. 群审批和好友审批仍为各自范围

## 6. 冲突文件处理清单

| 文件 | 决策 |
| --- | --- |
| docs/getting-started/configuration.md | 保留本地平台绑定/操作员语义, 新增媒体白名单说明与表情目录授权 |
| docs/plugins/satrap-coding-plugin.md | 保留 .satrap/config/plugins 路径, 合入配置生命周期与隔离说明 |
| satrap/core/platform/onebot/adapter.py | 双方 import 与实现合并; 本地群覆盖状态和发送契约完整保留 |
| satrap/edictum/simple_session/async_plugins.py | 安装失败 cleanup 与本地技能/工具回滚同时保留 |
| satrap/edictum/simple_session/sync_plugins.py | 与异步路径一致, 不吞掉清理或安装失败 |
| satrap/expend/plugins/base_take/tools/__init__.py | 接入环境剥敏, 不合回记忆旧实现 |
| satrap/expend/plugins/group_admin/meta.yaml | 新字段和空名单语义与本地现有工具名单一起更新 |
| satrap/expend/plugins/group_admin/tools.py | 保留本地来源和持久审批, 适配 main 授权要求, 删除即时输入审批冗余实现 |
| tests/unit/test_group_admin_plugin.py | 同时验证显式写名单和 request_managers, 不用一项替换另一项 |

另外 17 个自动合并的重叠文件也需语义检查, 特别是 BackendManager 的媒体根初始化和来源权限摘要, message.py 的组件转换, satrap_coding 的实例配置, 命令/处理器状态与既有测试. 没有文本冲突不等于兼容

## 7. 验证与交付契约

### 7.1 定向检查

| 范围 | 必需证据 |
| --- | --- |
| 媒体 | main 的 test_media_source_security, 本地组件/平台发送与 group_chat assets/stickers/media 测试; 合法默认表情通过, 数据库/显式未授权/符号链接越界拒绝 |
| OneBot | 空 token 回环与非回环, 入站/出站/路由/身份/提醒; ApiNotAvailable 读取等待, 已提交发送 unknown 且不重复 |
| Coding | main 的 test_proc_env/test_coding_configuration 与现有 coding/security/contracts/base_take 测试; 同步和异步多实例交错, 实际沙箱, 审批等待后的撤权 |
| 安装与加载 | session_discovery, plugin_module_loading, plugin_resources, plugin_skill_activation; cleanup 和技能/工具状态回滚组合, 重复安装不能清理原实例 |
| 群授权 | group_admin_plugin, group_actions, group_tool_migration; 高危开关/宿主策略组合, 空写名单拒绝, pending 后撤权, 单次执行与请求归属 |
| 独立功能 | friend_manager, memory 的独立性/迁移/注入, reminder 的存储/调度/管理/原生发送; 不恢复已删除的旧入口或插件依赖 |

必须以合并结果运行测试. main 报告中的历史失败和本地历史成功只用于定位, 都不能充当合并结果的验收证据. 新配置语义导致夹具变化时补明确名单与对应拒绝反例, 不通过减少断言或跳过测试掩盖失败

### 7.2 最终门槛

- 后端默认全量测试通过; 可选集成和环境跳过项分别说明, 不计为外部服务验收
- 前端单元测试, lint, tsc 和生产构建通过; 定向浏览器验证插件新字段, 群动作单一持久审批和 Agent 配置保存
- 修改生产 Python 模块的 pyright 无错误, git diff --check 通过, 无冲突标记或未合并索引
- 非预期异常记录堆栈并在业务边界隔离; 所有新增失败分支有可定位日志, 不泄露凭据
- 校验合并提交同时包含双方祖先, 本地功能和 main 安全改动均保留; 文档使用当前配置目录, 不残留旧工具和冗余审批实现
- 如加载合并版做现场复核, 由用户重启服务; 最小检查合法图片/表情真实发送, 同步/异步插件安装, 当前配置兼容. 原 C/D 测试保留为历史证据, 不以旧版本现场结果替代新媒体路径验收

## 8. 失败与恢复

隔离检出中的失败不影响现有运行服务和 .satrap 数据. 实际合并中止使用 Git 的合并中止机制, 不硬重置用户改动. 已提交后需要撤销时保留双方历史, 使用可审查的回退提交, 不强推或改写共享历史. 新授权语义的兼容调整明确写入文档, 不通过扩大白名单或自动授权掩盖问题
