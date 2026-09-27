# OneBot 群管理

管理面板的“群管理”入口按平台实例、已确认的机器人账号和群号定位数据。账号来自 OneBot 连接确认; 下拉框只查看当前账号和历史账号。要切换机器人账号, 需修改平台绑定配置并重建适配器。历史账号可读, 写配置和发动作时必须提交当前 `expected_self_id`。

## 首次接入与迁移

1. 配置 OneBot 平台及命名会话, 建立连接并确认 `self_id`
2. 从平台卡片进入群列表, 手动同步群目录; 选择目标群并启用响应
3. 在群详情设置响应策略、会话绑定与范围, 通过试算检查唤醒规则
4. 用群消息或手动唤醒验证回复; 在事件、诊断和审批页查看结果

平台数据库在首次打开时事务性迁移到 `PRAGMA user_version=2`, 新增账号、群配置、群目录、同步和管理动作表。已有会话覆盖表保留。迁移不删除旧配置或历史会话; 数据库声称是 v2 却缺表时拒绝使用。现有 OneBot 配置中的 `group_whitelist` 和 `wake_group_overrides` 在首次确认账号时原子采用一次, 并记录旧字段指纹。非空白名单转为 `selected`, 空白名单保持旧版的 `all` 语义。以后群表成为权威来源; 若旧字段再次变化, 返回冲突并要求显式处理, 不静默覆盖。首次接入的新格式使用 `group_management_version: 1`, 默认 `selected`, 因而同步出的新群默认不响应。旧字段只属于首次确认的账号, 后续绑定的其他账号不会继承。

`selected` 只允许显式启用的群响应; `all` 默认启用已确认加入的群, 显式关闭的群除外。平台总开关和群消息总开关仍在最外层生效。群同步只有完整合法的响应才把缺席群标为已离开; 超时、部分或截断结果保留旧成员关系并显示不完整状态。账号变化、退群和连接代次变化会拒绝旧任务或管理动作。群请求待审批项在重启时因协议 `flag` 不可恢复而过期; 执行中动作在重启时标为结果未知。

## 配置与运行时

群配置分 `policy`、`session`、`approval`、`events` 四个区域, 各区域独立保存。`expected_revision` 防止覆盖其他页面的新修改, `base_revision` 包含平台绑定及命名资源依赖; 任一变化均需重读。API 字段以 `{ "mode": "inherit" }` 或 `{ "mode": "value", "value": ... }` 表达, 显式空提示词和继承不同。保存后检查 `apply_status`、`saved_revision`、`active_revision`; 应用失败可用 `/config/apply` 重试同一保存版本。群列表的“管理默认设置”编辑账号级审批默认值, 并显示各动作当前已加入的继承群数量; 群详情“本群审批设置”只编辑逐群覆盖。切换自动执行仅影响新请求, 不执行已有待审批动作。

会话绑定可继承平台, 或指定 SessionClass/Edictum 命名配置; 范围可继承旧 `legacy_user`, 或指定 `group_member`、`group_shared`。绑定或范围切换递增群路由代次, 新消息进入新会话, 旧历史保留。群模型、提示词和插件覆盖目前由具备相应能力的 Edictum 类型支持; 不支持的 SessionClass 或 Edictum 类型在 UI 标明且服务端拒绝。实例显式覆盖优先于群覆盖。插件参数只接受其 schema 允许的会话覆盖字段; 模型引用只存名称, 不复制凭据。删除或重命名被群引用的命名会话、模型和插件资源时进行严格扫描。

群会话页显示可由范围路由键归属到本群的持久实例数量、ID 与模型/提示词/插件显式覆盖数量。“查看本群可归属实例”跳转到会话管理并按平台和这些实例 ID 筛选。旧版 `legacy_user` 范围按成员共享会话, 其历史无法可靠归属单群, 因而不计入上述数字; 切换绑定或范围前页面会明确提示这一限制。

策略试算使用与实际接入相同的合并规则, 不写配置、不调用模型。事件开关只控制业务通知的投递与展示; 机器人自身进退群维护和请求 `flag` 账本登记持续执行。近期事件仅保存固定的脱敏字段, 进程内总容量 4096, 重启后清空。

## HTTP API

所有路径在 `/api/platforms/{adapter}/groups` 下; 路径中的 `{group}` 是群号。列表、查询和历史读取须带 `?account={self_id}`, 写请求体须带 `expected_self_id`。面板沿用现有管理 API 认证。

| 方法和路径 | 用途 |
| --- | --- |
| `GET /accounts` | 当前账号和历史账号 |
| `GET /?account=...&q=...&membership=...&response=...&page=...&page_size=...` | 群目录分页、总数、同步状态 |
| `POST /sync`, `GET /sync/{sync_id}?account=...` | 显式同步和状态查询; POST 返回 202 |
| `GET/PATCH /settings` | 账号接入模式及默认审批策略; GET 附动作风险元数据与已加入继承群数量, PATCH 带 `mode`、`approval_defaults`、`expected_revision` |
| `GET /bindings?account=...` | 命名会话、模型和插件可用项 |
| `GET/PATCH /{group}/config` | 显式值、有效值、来源、能力与修订; PATCH 带 `section`、`values`、`expected_revision`、`base_revision` |
| `POST /{group}/config/apply` | 带 `saved_revision` 重试应用 |
| `POST /{group}/dry-run` | 带策略草稿和 `scenario` 做无副作用试算 |
| `GET /{group}/members`, `GET /{group}/info` | 按需从平台读取成员与群信息 |
| `GET /{group}/action-types` | 服务端动作 schema、风险和审批模式 |
| `GET/POST /{group}/actions`, `GET /{group}/actions/{action_id}` | 列表、提交与按原 ID 查询动作 |
| `POST /{group}/actions/{action_id}/decision` | 带布尔 `approve` 决定待审批动作 |
| `GET /{group}/events`, `GET /{group}/diagnostics` | 脱敏事件与本群请求诊断 |
| `POST /{group}/send`, `POST /{group}/wake`, `GET /{group}/wake/{request_id}` | 人工纯文本发送、唤醒与状态查询 |

群动作仅接受服务端登记的目标动作和参数。自动执行仍检查调用来源、模型插件开关、调用人、群范围、连接和成员关系。群请求审批按账号、群、请求子类型和账本 `flag` 校验; 好友请求继续使用原有授权与账本。`pending` 表示尚未调用平台, `executing` 表示可能已提交, `unknown` 必须人工核查平台状态。相同 `action_id` 和相同参数返回既有记录; 换参数复用 ID 返回冲突。待审批 10 分钟到期, 成功/失败/拒绝/过期记录保留 30 天; 结果未知记录不会被自动删除或重发。手动发送复用同一幂等机制, 响应丢失时先按原 ID 查询。

常见错误码通过响应 `reason` 返回: `missing_account`、`invalid_group_query` (400), `group_action_forbidden` (403), `group_record_not_found` (404), `account_changed`、`group_config_conflict` (409), `group_service_unavailable` (503)。平台明确拒绝、超时或动作结果未知还会出现在动作记录的 `state` 和 `result` 中。群成员列表上游没有分页时由服务端对一次最多 2048 条的读取结果分页, `complete`/`truncated` 表示边界; 不要把截断结果解释为完整群成员集。

## 验证

离线回归运行 `python -m pytest -q`、`python -m pyright -p .pyrightcfg/pyrightconfig.json`, 前端运行 `npm test`、`npm run lint`、`npm run build`。`node e2e/groups.mjs` 使用真实页面和 API 客户端覆盖列表、会话覆盖、审批、冲突草稿及窄屏布局, 截图存于本地 `satrap-ui/test-results/groups/`。`python scripts/probe_snowluma.py --help` 提供 SnowLuma 联调入口; 实际连接验证需本地 SnowLuma 环境。自动测试使用 QQ 协议模拟和模型替身, 不能代替真实 QQ 网络或真实模型供应商验收。
