"""控制服务与平台管理接口的 TypedDict 契约"""
from __future__ import annotations

from typing import Any, NotRequired, TypedDict


class AdministratorRuntimeStatus(TypedDict):
    """管理员区段运行时状态, 不含名单正文"""
    ok: bool
    """运行时状态读取成功的标志, 正常返回为 True"""
    section_revision: str
    """当前宿主实际使用的管理员组及例外配置修订"""
    runtime_id: str
    """当前后端运行实例的标识, 用于区分重启后的宿主"""
    pid: int
    """承载当前后端宿主的操作系统进程号"""
    config_path: str
    """当前宿主配置来源的绝对路径, 来源未知时为空串"""


class GroupActionRecord(TypedDict):
    """群管理动作对外记录, 不携带原始请求 flag"""
    action_id: str
    """提交方提供或宿主生成的动作幂等 ID, 相同 ID 必须对应相同指纹"""
    self_id: str
    """平台连接确认的机器人账号, 限定动作所属账号"""
    group_id: str
    """动作固定的目标群号, 来自请求或平台邀请"""
    action_type: str
    """群管理动作类别, 决定参数结构和执行方式"""
    params: dict[str, Any]
    """归一化后的动作参数, 不包含平台原始请求 flag"""
    fingerprint: str
    """账号, 群, 动作, 参数和来源计算的 SHA-256 幂等指纹"""
    actor_kind: str
    """动作发起来源, panel 表示管理界面, model 表示模型"""
    policy_revision: int
    """提交时固定的审批策略版本, 审批时用于检测策略变化"""
    state: str
    """动作状态: pending, executing, succeeded, failed, unknown, rejected 或 expired"""
    created_at: float
    """动作登记时间, Unix 时间戳, 单位秒"""
    expires_at: float | None
    """待审批截止的 Unix 时间戳, 单位秒; 无需审批时为 None"""
    decision_at: float | None
    """审批决定或失效时间, Unix 时间戳, 单位秒; 尚无决定时为 None"""
    executed_at: float | None
    """执行结果结算时间, Unix 时间戳, 单位秒; 尚未结算时为 None"""
    result: dict[str, Any] | None
    """有限的结果原因及诊断字段, 尚无结果时为 None, 不暴露平台原始响应"""


class GroupActionPage(TypedDict):
    """群管理动作分页结果"""
    items: list[GroupActionRecord]
    """当前页动作, 按创建时间倒序排列"""
    total: int
    """同一事务中满足账号, 群和状态等筛选条件的动作总数"""
    page: int
    """从 1 开始的请求页码"""
    page_size: int
    """每页条数, 支持 25 或 50"""


class GroupSyncState(TypedDict):
    """群目录同步任务的持久化状态"""
    status: str
    """最近同步状态: never, running, complete, partial 或 failed"""
    sync_id: str | None
    """服务端生成的最近同步令牌, 尚未同步时为 None"""
    complete: bool
    """最近结果是否为可信完整快照, 只有完整快照可将缺席群标记为离开"""
    truncated: bool
    """最近响应是否因数量限制或无效条目而截断"""
    reason: str | None
    """不完整或失败的原因码, 无原因时为 None"""
    started_at: float | None
    """最近一次同步开始的 Unix 时间戳, 单位秒; 未开始时为 None"""
    completed_at: float | None
    """最近记录的同步结束 Unix 时间戳, 单位秒; 尚无结束记录时为 None"""
    last_complete_at: float | None
    """上次可信完整快照的 Unix 时间戳, 单位秒; 从未完整同步时为 None"""
    connection_generation: int
    """发起同步时的适配器连接代次, 防止旧连接结果覆盖; 未同步时为 0"""


class GroupSyncTicket(TypedDict):
    """群目录同步任务回执"""
    sync_id: str
    """服务端生成或复用的同步令牌, 用于固定账号查询任务"""
    status: str
    """提交回执中的任务状态, 当前为 running"""
    reused: bool
    """是否复用了同一平台和账号仍在运行的同步任务"""


class GroupAccountRecord(TypedDict):
    """已采用账号的群接入设置"""
    self_id: str
    """可信平台连接确认并采用的机器人账号"""
    mode: str
    """群接入模式, selected 按群选择, all 使用全部群的默认接入"""
    approval_defaults: dict[str, Any]
    """按动作设置的账号级默认审批模式, 可由单群覆盖"""
    revision: int
    """账号接入和审批设置的持久修订号, 更新时用于乐观并发校验"""
    migrated_at: float
    """账号首次采用或登记时间, Unix 时间戳, 单位秒"""
    last_bound_at: float
    """最近一次确认绑定该账号的 Unix 时间戳, 单位秒"""
    legacy_adopted: bool
    """是否保存了旧群设置来源指纹, 用于识别已采用的旧配置"""


class GroupAccountSummary(TypedDict):
    """已确认账号的历史摘要"""
    self_id: str
    """本平台历史确认的机器人账号"""
    mode: str
    """该账号保存的群接入模式, 为 selected 或 all"""
    revision: int
    """账号设置当前持久修订号"""
    last_bound_at: float
    """最近绑定该账号的 Unix 时间戳, 单位秒; 列表按此倒序"""


class GroupConfigRecord(TypedDict):
    """单群显式配置与修订"""
    explicit: dict[str, Any]
    """本群显式覆盖的配置分区, 未创建记录时为空对象"""
    schema_version: int
    """持久群配置的结构版本, 未创建时为 1"""
    revision: int
    """单群配置修订号, 未创建时为 0, 更新时用于并发校验"""
    route_generation: int
    """群会话路由代次, 用于区分旧路由; 未创建时为 0"""
    updated_at: float | None
    """配置最近写入的 Unix 时间戳, 单位秒; 未创建时为 None"""


class GroupDirectoryRecord(TypedDict):
    """单群目录记录, 仅有配置未确认时 membership 为 config_only"""
    group_id: str
    """平台群号, 与固定机器人账号共同确定目录记录"""
    group_name: str | None
    """平台目录或可信成员事件提供的群名称, 未知时为 None"""
    member_count: int | None
    """平台目录报告的成员数, 未提供或仅有配置时为 None"""
    max_member_count: int | None
    """平台目录报告的成员上限, 未提供或仅有配置时为 None"""
    membership: str
    """机器人与群的关系: joined, left, unknown 或仅有配置的 config_only"""
    confirmed_at: float | None
    """最近可信确认成员关系的 Unix 时间戳, 单位秒; 未确认时为 None"""


class ScopedSessionSummary(TypedDict):
    """归属本群的持久会话实例汇总"""
    known_scoped_count: int
    """可由持久范围路由键归属本群的去重会话实例数, 包含旧路由"""
    current_route_count: int
    """其中路由代次和绑定修订均匹配当前配置的去重实例数"""
    session_ids: list[str]
    """所有可归属本群的持久会话实例 ID, 去重后排序"""
    current_session_ids: list[str]
    """当前路由代次及绑定修订仍有效的实例 ID, 去重后排序"""
    override_counts: dict[str, int]
    """全部已知实例中显式配置 model, prompt 和 plugins 的实例数"""


class GroupAccountsResult(TypedDict):
    """平台已确认账号清单"""
    items: list[GroupAccountSummary]
    """本平台持久化的当前及历史确认账号摘要"""
    current_account: str
    """适配器当前确认的机器人账号, 尚未确认时为空串"""
    waiting_for_account: bool
    """当前适配器尚无可信账号时为 True"""


class GroupMembersPage(TypedDict):
    """固定群成员分页结果"""
    items: list[dict[str, Any]]
    """从 OneBot 已加载成员中筛选后切出的当前页"""
    total_loaded: int
    """已加载集合内符合查询的成员数, 截断时不是群成员总数"""
    page: int
    """从 1 开始的请求页码"""
    page_size: int
    """每页条数, 支持 25, 50 或 100"""
    truncated: bool
    """已加载成员达到 2048 条上限时为 True, 表示可能还有未加载成员"""
    complete: bool
    """已加载集合未触及截断上限时为 True"""
    account: str
    """读取成员时固定的当前机器人账号"""
    group_id: str
    """读取成员的固定目标群号"""


class GroupActionTypesResult(TypedDict):
    """群管理动作的参数结构, 风险和有效审批模式"""
    items: list[dict[str, Any]]
    """各动作的参数和风险元数据, 有效审批来源及当前群能力"""
    account: str
    """计算动作权限所用的机器人账号"""
    group_id: str
    """计算审批模式和成员关系所用的目标群号"""


class GroupBindingOptionsResult(TypedDict):
    """群绑定的可选命名会话定义, 模型和插件清单"""
    items: list[dict[str, Any]]
    """命名会话定义及其可用性, 已删除的绑定仍显示为不可用"""
    account: str
    """请求固定的已采用机器人账号"""
    models: list[str]
    """模型配置目录中可供引用的 LLM 配置名, 按名称排序"""
    plugins: list[dict[str, Any]]
    """当前 Edictum 提供者插件目录的公开条目, 目录不可用时为空"""


class PlatformStoreRecovery(TypedDict):
    """平台状态存储恢复结果"""
    ok: bool
    """恢复校验成功且存储已解除降级时为 True"""
    store: str
    """被检查的持久存储名称, 为 manual_wake 或 request_ledger"""
    reason: str
    """稳定结果原因码, 成功时为空串"""
    degraded: NotRequired[bool]
    """存储当前是否仍处于降级状态, 存储不可用时可省略"""
    detail: NotRequired[str]
    """存储提供的降级原因详情, 不可取得时省略"""


class ManualWakeStatusResult(TypedDict):
    """手动唤醒请求状态查询结果"""
    status: str
    """持久状态为 accepted, executing, sent, partial, failed 或 unknown; 不可查询也返回 unknown"""
    request_id: str
    """客户端提交的手动唤醒幂等请求 ID"""
    reason: NotRequired[str]
    """状态未知时的原因码, 如 not_found 或 store_degraded"""
    detail: NotRequired[str]
    """已存请求的状态详情或存储降级原因, 不可取得时省略"""
    adapter_id: NotRequired[str]
    """已存请求所属的平台适配器实例 ID, 未找到时省略"""
    target: NotRequired[str]
    """已存请求固定的目标, 手动群唤醒使用 group:群号"""
    operator: NotRequired[str]
    """提交时由可信管理边界确认的操作者身份"""
    created_at: NotRequired[float]
    """请求持久占位的 Unix 时间戳, 单位秒; 无记录时省略"""
    updated_at: NotRequired[float]
    """请求状态最近更新的 Unix 时间戳, 单位秒; 无记录时省略"""


class WakeSubmitResult(TypedDict):
    """平台手动唤醒提交回执"""
    status: str
    """提交结果: accepted, already_pending, no_pending 或 rejected"""
    request_id: NotRequired[str]
    """客户端幂等请求 ID, 输入尚未通过校验时可省略"""
    reason: NotRequired[str]
    """拒绝, 重复或无待处理内容的原因码, 无原因时省略"""
    state: NotRequired[str]
    """重复请求对应的已有处理状态, 非重复回执通常省略"""


class RequestDiagnosticsStats(TypedDict):
    """请求诊断容量统计"""
    capacity: int
    """每个适配器可保留的最近请求数上限, 默认 256"""
    records_per_request: int
    """每个请求可保留的阶段记录数上限, 当前为 16"""
    requests_total: int
    """所选适配器范围内当前保留的请求数, 跨适配器时求和"""
    records_total: int
    """所选适配器范围内当前保留的阶段记录条数"""


class RequestDiagnosticsPage(TypedDict):
    """请求诊断汇总页, 统计键仅在可用时存在"""
    records: list[dict[str, object]]
    """按最新记录倒序的请求摘要, 不可用时为空列表"""
    available: bool
    """调度器诊断存储是否可查询, False 不代表没有请求"""
    reason: NotRequired[str]
    """不可查询的原因码, 如 scheduler_unavailable"""
    capacity: NotRequired[int]
    """每适配器请求容量上限, 诊断可用时返回"""
    records_per_request: NotRequired[int]
    """单请求阶段记录容量上限, 诊断可用时返回"""
    requests_total: NotRequired[int]
    """所选适配器当前保留的请求数, 不受摘要条数限制"""
    records_total: NotRequired[int]
    """所选适配器当前保留的阶段记录总数, 不受摘要筛选限制"""


class RequestDiagnosticDetail(TypedDict):
    """单个请求的诊断明细"""
    request_id: str
    """查询的逻辑请求 ID, 缺失原始 ID 时可使用消息或匿名回退键"""
    adapter_id: str
    """保存该请求诊断的平台适配器实例 ID"""
    records: list[dict[str, object]]
    """该请求保留的脱敏阶段记录, 按采集时间及追加序号升序"""
    stages: list[str]
    """与 records 顺序一致的阶段名, 同一阶段可能出现多次"""
    available: bool
    """找到该请求的诊断明细时为 True"""
    truncated: bool
    """保留记录数达到单请求上限时为 True, 提示可能发生截断"""
    reason: NotRequired[str]
    """可选的明细不可用原因码"""


class DiagnosticStatusBody(TypedDict):
    """诊断明细不可用时的状态体"""
    status: str
    """无法取得诊断时为 unknown, 不表示请求未执行"""
    reason: str
    """不可用原因码, 如 scheduler_unavailable 或 not_found"""
    request_id: NotRequired[str]
    """查询的逻辑请求 ID, 群限定状态响应可能省略"""


class ReloadConfigResult(TypedDict):
    """配置热加载结果"""
    ok: bool
    """全部平台策略已应用且全部 Edictum 会话同步成功时为 True"""
    platforms: list[dict[str, Any]]
    """逐平台的在线策略应用状态及失败详情"""
    edictum_sessions: list[dict[str, Any]]
    """活跃 Edictum 会话的配置协调结果"""
