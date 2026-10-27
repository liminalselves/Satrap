# 新前端后端能力契约与文件规划

状态: A-F 后端代码已实现, 本机验收与跨平台未验收项以第 12 节记录为准

依据: `satrap-ui-preview/BACKEND_GAPS.md` 与当前 `ui-redesign` 分支的实际后端实现

按本文分批实施后端, 不覆盖工作区内正在进行的前端改版

实施范围仅包含后端代码、后端测试与契约文档; UI 由另一项工作单独处理, 本轮不新增或修改 satrap-ui/、satrap-ui-preview/ 下的代码、API 客户端、样式、前端测试或浏览器场景

下文涉及界面展示的描述属于接口使用说明, 不构成本轮 UI 开发任务; TypeScript DTO 仅作为前后端交接的协议表达

## 1. 范围与兼容边界

首版包含六批: 服务健康, 统一统计, 活动与审批, 知识库任务, 平台/插件/Agent 信息, 历史查询与设置

以下能力直接复用, 不重新设计协议:

- 原有会话历史与编辑接口, `variants`, `action: restore`, 检查点及回收区恢复
- 插件 ZIP 的预览/安装/冲突检查及现有配置应用进度
- 平台通知 `PlatformEventHub`, 现有群/好友/记忆业务账本
- 三服务认证, 对象身份, Debug 分组/增量正文/完整导出接口
- 配置修订冲突和现有逐项清理结果

后置项: Git/本地目录插件安装, 在线更新源, Agent 默认唤醒策略迁移, 历史日志全文检索, 跨设备未读位置, 逐消息精确 Token 分摊, 知识库任务自动断点续跑

旧接口保留路径, 请求体和成功状态码; 新能力通过新增入口或增加字段提供, 不把同步接口直接改为 HTTP 202

上线顺序为后端先支持新增契约, 再发布调用它的前端; 新接口不可用时明确显示版本不支持, 不增加静默 404 回退链路

## 2. 共用约定

### 2.1 服务归属与认证

| 服务 | 首版职责 |
| --- | --- |
| Control, 通常为 19871 | 聚合统计, 活动流, 统一审批目录与批量决策, 配置和冷数据管理 |
| Backend, 通常为 19870 | 平台实时状态, 平台消息采集, 原有平台动作执行, 所属模型调用统计 |
| Chat, 通常为 19872 | 独立聊天, 所属模型调用统计, 独立 RAG 操作与任务 |

端口仅为默认值, 前端使用当前服务配置, 不硬编码端口

- 各服务继续使用自己的认证, Cookie 和鉴权入口
- RAG/模型配置页面不要求 Backend 必须在线或已认证; Chat 主聊天不依赖 Control 在线
- Control 只能读取当前配置已绑定的服务和存储, 请求参数不能指定 URL 或数据库路径
- 独立 Chat 宿主通过受信任环境 SATRAP_CHAT_HOST/SATRAP_CHAT_PORT 指定, 默认 127.0.0.1:19872; 统计、插件应用和探测共用该地址, 独立容器不能把容器自身的回环地址当作 Chat
- 聚合的来源失败要分别报告, 不能把认证失败归为停止, 不能把离线来源归为零用量
- 调用各服务自己的只读统计接口完成跨进程聚合; 本地已保存数据可在服务离线时提供, 必须标记读取来源和有效时间
- 聚合入口不能通过转发把原本无权执行的业务动作变成可执行

### 2.2 时间, 身份与分页

- 时间统一为 UTC Unix 秒, 支持小数; 耗时统一为毫秒
- 分钟/小时/日统计接受 `tz`, 默认 `Asia/Shanghai`; 使用 IANA 时区, 正确处理日界线与夏令时
- 查询窗口为 `[from, to)`, `window` 返回解析后的 `from`, `to`, `tz`, `bucket`
- `bucket=minute|hour|day`; 分钟统计最多 24 小时, 小时统计最多 31 天, 日统计最多 366 天; 超限返回 400, 不静默裁剪
- 列表默认 50 条, 最大 100 条; 游标绑定筛选和稳定排序键, 筛选变化后不能继续使用旧游标
- 平台实例, 机器人账号, 群/私聊类型和目标 ID 组成对象身份; `session_id` 与 `context_id` 分开保存
- Chat 沿用现有会话对象身份, 不虚构跨会话的用户归属
- ID 和查询文本沿用现有长度约束, 新游标最大 4096 字符
- 新增 q 最多 512 字符, 原接口更小的限制保留; 计数和 Token 为非负整数, percent 为 0-100, 时间与耗时不能出现 NaN/Infinity

### 2.3 聚合响应

统计与活动响应统一增加如下公共部分, 资源正文仍使用各自字段名:

```json
{
  "generated_at": 1793059200,
  "window": { "from": 1793030400, "to": 1793059200, "tz": "Asia/Shanghai", "bucket": "hour" },
  "complete": false,
  "sources": [
    { "service": "backend", "state": "available", "as_of": 1793059199, "collection_started_at": 1793000000, "reason_code": null, "complete": true, "gaps": [], "gaps_truncated": false, "dropped_events": 0 },
    { "service": "chat", "state": "offline", "as_of": null, "collection_started_at": null, "reason_code": "service_unreachable", "complete": false, "gaps": [], "gaps_truncated": false, "dropped_events": 0 }
  ],
  "warnings": ["Chat 来源当前不可用"]
}
```

- `sources[].state`: `available`, `stale`, `offline`, `auth_required`, `error`
- `complete` 表示查询范围及来源覆盖完整, 不等于每次模型调用都有供应商 usage
- 已确认无事件使用 0; 没有观测依据使用 null; 部分覆盖可以返回已知值, 但必须标明不完整
- 同一列表只混合已完成授权检查的来源, 不因一个来源失败丢弃其他成功结果
- `generated_at` 是响应生成时间, `as_of` 是数据有效时间, 不能互相替代
- 本地快照必须核验当前配置和来源归属; 不能只因为存在同名 SQLite 文件就混入统计
- 来源同时返回自身 complete, 已知 gaps 和 dropped_events; gaps 最多 100 段, 超出时 gaps_truncated=true, 空 gaps 不代表没有无法定位的缺口

### 2.4 错误约定

新接口的错误返回保留顶层 `error` 文本, 并提供 `code` 和必要的 `details`:

```json
{ "error": "查询范围超过限制", "code": "invalid_window", "details": { "max_days": 31 } }
```

| HTTP | 使用场景 |
| --- | --- |
| 400 | 参数, 时间窗口或游标格式错误 |
| 401/403 | 当前服务的认证/授权未满足 |
| 404 | 对象或任务不存在 |
| 409 | 修订冲突, 操作 ID 被不同请求占用, 当前资源状态不允许操作 |
| 413 | 上传或请求体超过限制 |
| 503 | 当前服务队列满或存储不可用 |

聚合来源部分失败使用 HTTP 200 加 `complete: false`; 整个查询没有任何可用来源时返回 503

旧接口的现有错误码不批量重命名; 增补字段由前端适配层统一解释

### 2.5 首版核心 DTO

以下类型用于固定新增接口的核心结构; 已有业务项保留原字段, 通过后文定义的字段扩展, 不将整项退化为任意 JSON

```typescript
type Service = 'control' | 'backend' | 'chat';
type Purpose = 'conversation' | 'summary' | 'approval' | 'other';
type SourceState = 'available' | 'stale' | 'offline' | 'auth_required' | 'error';
interface TimeWindow { from: number; to: number; tz: string; bucket: 'minute' | 'hour' | 'day' | null }
interface SourceCoverage {
  service: Service; state: SourceState; as_of: number | null;
  collection_started_at: number | null; reason_code: string | null;
  complete: boolean; gaps: { from: number; to: number; reason_code: string }[];
  gaps_truncated: boolean; dropped_events: number;
}
interface AggregateMeta {
  generated_at: number; window: TimeWindow; complete: boolean;
  sources: SourceCoverage[]; warnings: string[];
}
interface Page<T> { items: T[]; has_more: boolean; next_cursor: string | null }
interface ServiceHealth {
  service: Service; running: boolean; healthy: boolean; runtime_id: string; pid: number;
  started_at: number; uptime_seconds: number; generated_at: number;
  python: string; platform: string; version: string | null; revision: string | null;
}
interface MessageCounts {
  inbound: number | null; outbound: number | null;
  model_requests: number | null; commands: number | null;
}
interface UsageTotals {
  calls: number; succeeded: number; failed: number; cancelled: number;
  usage_reported_calls: number; usage_missing_calls: number;
  input_tokens: number | null; output_tokens: number | null;
  cached_tokens: number | null; total_tokens: number | null;
  token_reported_calls: { input_tokens: number; output_tokens: number; cached_tokens: number; total_tokens: number };
  duration_p50_ms: number | null; duration_p95_ms: number | null;
  duration_method: 'exact' | 'histogram';
}
interface MetricBucket { from: number; to: number; messages: MessageCounts; usage: UsageTotals }
interface Comparison {
  current: number | null; previous: number | null;
  absolute_delta: number | null; percent_delta: number | null;
}
interface AdapterMetrics {
  platform_id: string; self_id: string; messages: MessageCounts; series: MetricBucket[];
}
interface Overview extends AggregateMeta {
  messages: MessageCounts; usage: UsageTotals; series: MetricBucket[]; by_adapter: AdapterMetrics[];
  comparison: { mode: 'yesterday_same_elapsed'; baseline_window: TimeWindow; messages: Comparison; tokens: Comparison };
}
interface ModelMetrics {
  model_config: string | null; model: string; provider: string | null;
  usage: UsageTotals; trend: MetricBucket[];
}
interface ContextOccupancy {
  used: number | null; window: number | null; source: 'reported' | 'estimated' | 'unknown';
  as_of: number | null; model_config: string | null;
}
interface SessionMetrics {
  platform_id: string; agent: string | null; session_id: string;
  usage: UsageTotals; trend: MetricBucket[]; context: ContextOccupancy;
}
type ModelsResponse = AggregateMeta & { models: ModelMetrics[]; has_more: boolean; next_cursor: string | null };
type SessionsResponse = AggregateMeta & { sessions: SessionMetrics[]; has_more: boolean; next_cursor: string | null };
interface ObjectIdentity {
  platform_id: string; self_id: string; conversation_kind: 'group' | 'private' | 'chat'; target_id: string;
}
interface ActionKey {
  platform_id: string; self_id: string; source: 'group' | 'invitation' | 'friend' | 'memory'; action_id: string;
}
type ActionState = 'pending' | 'ready' | 'executing' | 'succeeded' | 'failed' | 'unknown' | 'rejected' | 'expired';
interface PublicFailure { code: string; message: string }
interface DecisionInput { key: ActionKey; expected_revision: number; approve: boolean; note?: string }
interface DecisionOutput {
  key: ActionKey; result: 'applied' | 'conflict' | 'unavailable' | 'failed' | 'unknown';
  action: { revision: number; state: ActionState; source_state: string; decision: 'approved' | 'rejected' | null } | null;
  error: PublicFailure | null;
}
interface BatchDecisionResponse { items: DecisionOutput[] }
type ActivityRef =
  | { kind: 'approval'; key: ActionKey }
  | { kind: 'rag_job'; service: Service; platform_id: string; session_id: string; kb_id: string; job_id: string }
  | { kind: 'plugin_application'; service: Service; operation_id: string }
  | { kind: 'platform'; platform_id: string; self_id: string }
  | { kind: 'session'; platform_id: string; session_id: string };
interface ActivityEvent {
  event_id: string; occurred_at: number; service: Service; kind: string;
  tone: 'info' | 'warning' | 'error'; title: string;
  object: ObjectIdentity | null; request_id: string | null; ref: ActivityRef | null;
  count: number;
}
interface RagProgress { unit: 'documents' | 'chunks'; done: number; total: number | null; percent: number | null }
interface RagJob {
  job_id: string; operation_id: string; owner_service: 'control' | 'chat'; owner_runtime_id: string | null;
  platform_id: string; session_id: string; kb_id: string; action: 'ingest' | 'rebuild';
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'interrupted';
  phase: 'queued' | 'parsing' | 'splitting' | 'embedding' | 'publishing' | 'finished';
  progress: RagProgress; created_at: number; started_at: number | null; finished_at: number | null;
  error: PublicFailure | null;
  result: { document_ids: string[]; indexed_documents: number; indexed_chunks: number; generation: string } | null;
}
interface RagDocument {
  document_id: string; source_id: string | null; name: string; size_bytes: number | null;
  created_at: number | null; updated_at: number; last_indexed_at: number | null;
  status: 'queued' | 'indexing' | 'ready' | 'failed';
  error: PublicFailure | null; job_id: string | null; chunk_count: number;
}
interface YamlIssue { path: string | null; line: number | null; column: number | null; code: string; message: string }
interface YamlValidation { valid: boolean; errors: YamlIssue[] }
```

活动响应使用 `AggregateMeta & Page<ActivityEvent>`, 无时间桶的活动窗口返回 bucket=null; 统计接口的 bucket 必须是已声明的实际粒度

overview 的 comparison.messages 对应 inbound, comparison.tokens 对应已报告的 total_tokens; 任一比较范围不完整时差值与涨幅为 null, 不能比较不同覆盖范围

## 3. 第一批: 服务与适配器健康

### 3.1 服务接口

Backend 的 `/api/health` 和 Chat 的 `/api/chat/health` 增加公共字段; Control 新增 `GET /api/health`, 只检查 Control 自身

| 字段 | 类型与含义 |
| --- | --- |
| `service` | `control`, `backend`, `chat` |
| `running`, `healthy` | 布尔值, 进程运行与功能健康分开 |
| `runtime_id`, `pid` | 当前服务进程身份 |
| `started_at`, `uptime_seconds` | 启动墙钟时间和单调时钟计算的运行时长 |
| `python`, `platform` | 当前解释器和操作系统说明 |
| `version`, `revision` | 发布版本和构建提交, 无法确认时为 null |
| `generated_at` | 当前状态生成时间 |

启动时间表示服务本次启动, 不使用 PID 文件修改时间推算; 版本在启动时解析一次, 不在每次健康查询执行 Git 命令

Control `/status` 保留 `running`, `managed`, `health`, 增加 `probe`:

- `probe.target_service = backend`
- `probe.measured_at`: 此次探测时间
- `probe.latency_ms`: Control 到 Backend 的探测往返耗时, 无成功响应时为 null
- `probe.state`: 使用 2.3 的来源状态

前端顶栏分别探测各服务, 自行记录浏览器往返耗时; 不把 Control 自身健康绑定为 Backend 是否运行

### 3.2 适配器字段

扩展现有 `get_stats()` 和健康响应中的适配器对象:

| 字段 | 规则 |
| --- | --- |
| `started_at` | 保留现有字段, 表示适配器启动 |
| `connected_at`, `disconnected_at` | 当前连接建立和最近断线时间, 无法确认时 null |
| `reconnect_count` | 本次适配器运行内确认的重新连接次数, 初次连接不计 |
| `last_heartbeat_at`, `last_message_at` | 最近观测到的心跳和消息墙钟时间 |
| `latency_ms`, `latency_kind` | 只有可关联响应的往返探测才填数值; 单向心跳不能冒充 RTT |
| `protocol`, `description` | 适配器公开声明, 缺失时 null |
| `bot` | 可确认的 `self_id`, `nickname`, `avatar_url`, `as_of`; 不支持的资料为 null |

内部连接代次不直接充当重连统计; 昵称和头像按账号/连接代次缓存, 不在每次轮询请求平台

## 4. 第二批: 统一统计

### 4.1 接口目录

| 服务与接口 | 筛选 | 资源正文 |
| --- | --- | --- |
| Control `GET /api/stats/overview` | `from`, `to`, `tz`, `bucket`, 可选 `platform_id` | `messages`, `usage`, `comparison`, `series`, `by_adapter` |
| Control `GET /api/stats/models` | 公共时间窗口, 可选 `model_config`, 分页 | `models[]` |
| Control `GET /api/stats/sessions` | 公共时间窗口, 可选 `platform_id`, `agent`, `session_id`, 分页 | `sessions[]` |
| 各服务 `GET /api/stats/local` | 同一窗口, `view=overview|models|sessions` 及对应筛选 | 当前服务自己的贡献, 不递归聚合 |
| 各服务 `GET /api/logs/stats` | `window_seconds=120|600|1800`, `bucket_seconds=10|60` | `stream_id`, `by_level`, `series`, `error_count`, `lines_per_minute` |
| Control `GET/PUT /config/telemetry` | PUT 使用 `expected_revision` | `settings`, `revision` |

`stats/local` 同样需要服务鉴权, 不对外提供任意维度 SQL

overview 默认查询当前时区今日零点至现在, bucket=hour; models 默认最近 7 个自然日, sessions 默认最近 14 个自然日, bucket=day, 均包含当日已过去时段

显式指定时间时 from/to 必须同时提供; 可选 `purpose=conversation|summary|approval|other|all`, 默认 all; 实例趋势由 sessions 入口返回, 近 14 分钟适配器曲线使用 overview 的分钟桶

消息统计字段: `inbound`, `outbound`, `model_requests`, `commands`; 每个趋势桶带起止时间和同一组计数

用量统计字段: `calls`, `succeeded`, `failed`, `cancelled`, `usage_reported_calls`, `usage_missing_calls`, `input_tokens`, `output_tokens`, `cached_tokens`, `total_tokens`, `duration_p50_ms`, `duration_p95_ms`

模型行额外包含调用当时的模型配置名, 实际模型名, 提供方身份; 实例行额外包含平台, Agent 配置身份, `session_id`

耗时分位数不能由多个分组的 P95 再求平均; 聚合保存可合并的固定耗时直方图, 返回 `duration_method=exact|histogram`, 明细不足时用标明方法的近似分位数

无 usage 的调用仍计入 `calls`, 失败和取消也不能从分母消失; Token 数字为已知 usage 之和, 全部未知时为 null, 界面同时显示覆盖率

供应商可能只报告部分 usage 字段, token_reported_calls 分字段返回覆盖调用数, 不把缺少的输出/缓存 Token 当作 0; 单次响应最多输出 10000 个趋势桶, 超出时需缩小窗口或分页条数

日志统计使用进程内有限时间桶, 不依赖 5000 行正文是否已淘汰; 返回 `coverage_from` 和当前 `stream_id`, 重启前的时间段不补成 0

日志窗口起点按底层 10 秒计数桶向下对齐, 通过 window_from/window_to 与 boundary_resolution_seconds=10 明示实际范围; 速率按实际覆盖秒数计算, 不把部分桶标成精确秒级裁剪

### 4.2 采集与去重

- 平台入站在账号、回声与消息去重后计数, 使用真实消息身份; Bot 发送依据最终发送回执计数
- 同一消息方向被纠正时调整既有计数, 不再增加一条事件
- Chat 入站按持久展示轮次身份计一次, 重试/Fork/回复变体不重复增加原问题数
- API 调用次数以宿主一次 SDK create 调用为单位; SDK 内部透明重试不冒充可确认的独立 HTTP 次数
- 显式降级导致第二次 SDK create 时分开记录; 使用稳定 `call_id` 防止一个流式响应被重复结算
- 流式结束、非流式返回、异常和取消均结算; 已报告的部分 usage 可保存, 未报告部分保持未知
- 同一请求的子工作流、摘要和自动审批调用标明 `purpose=conversation|summary|approval|other`, 可分别过滤
- 统计独立于 Debug 开关; 复用模型请求边界, 不增加重复消费响应流的包装层

### 4.3 口径

- `inbound` 是实际收到的用户消息数, 不等于触发模型的请求数
- `outbound` 是确认发送的消息数; unknown 回执不能计入确认成功
- 缓存 Token 属于输入 Token 的子集, 不再次加进总量
- 上下文占用取最后一次实际模型请求的输入用量或明确标记的估计值, 不取累计 Token
- `context` 返回 `used`, `window`, `source=reported|estimated|unknown`, `as_of`, `model_config`; 无法确认时数值为 null
- 今日和昨日比较使用相同时区; 比较对象为昨日相同已过去时段, 昨日同期为 0 时涨幅返回 null, 同时返回绝对差值
- 撤回/清理原消息不等于从未发生, 已保存日汇总不随历史正文删除
- 旧 Chat `context_stats` 和 Debug 可用于明确标记的辅助历史展示, 不混入从采集启用起保证的全量用量序列
- 不承诺根据整次 usage 还原每条历史消息的真实计费 Token

### 4.4 存储与开销

每个服务独立写入 `.satrap/data/telemetry/{service}.sqlite`, 使用有界队列和单写入器; 聚合服务不写其他服务的库

首版内部表:

| 表 | 保存内容 |
| --- | --- |
| `model_calls` | call/runtime/request 身份, 平台/对象/Agent/会话, 配置名和实际模型, 时间/耗时/结果, usage, 计量来源 |
| `message_events` | 可去重的消息身份, 方向, 接收时间及统计维度, 不保存正文 |
| `metric_buckets` | 消息与调用的分钟/日汇总及覆盖信息 |
| `activity_events` | 活动摘要和业务引用 |
| `collection_runs` | 采集进程, 有效区间, 正常退出标志及缺口计数 |
| `context_occupancy` | 每实例最近一次实际模型输入用量与该次配置窗口, 摘要/审批不覆盖主对话占用 |
| `retention_bounds` | 按数据集记录已回收边界和原因, 后续写入不覆盖清理证据 |

默认采集开启; 调用明细保留 30 天, 活动 30 天, 日汇总 180 天, 每服务存储目标 128 MB, 均可由 `/config/telemetry` 修改

GET 返回 `{settings, revision}`, PUT 输入 `{settings, expected_revision}`; settings 固定包含 `usage_enabled`, `activity_enabled`, `usage_retention_days`, `activity_retention_days`, `aggregate_retention_days`, `max_service_mb`, 使用对象整体替换并校验未知字段

保留期允许 1-3660 天, max_service_mb 允许 16-4096; 关闭采集后记录覆盖缺口但不删除既有统计, 配置修改从下一次采集/清理周期生效

队列默认 4096 条, 每批最多 128 条或间隔最多 1 秒提交; 关闭时最多等待 5 秒排空, 热路径不等待磁盘写入

队列溢出或异常退出必须暴露缺口, 不能宣称统计完整; 写入故障不改变消息或模型业务返回, 不无限积压内存

容量清理优先清理过期明细, 保留有效汇总; 强制提前回收需更新覆盖范围, 不虚构完整历史

来源查询短缓存 5 秒, 聚合查询最多同时读取 3 个服务, 不在每次轮询扫描完整日志/历史正文

## 5. 第三批: 活动与审批

### 5.1 活动接口

Control `GET /api/activity`: `from`, `to`, `kind`, `tone`, `platform_id`, `limit`, `cursor`

默认最近 24 小时, 显式时间范围最多 31 天; cursor 继续使用首次确定的窗口, 不随翻页时钟漂移

返回公共覆盖信息及 `items`, `has_more`, `next_cursor`; 排序为 `occurred_at DESC, event_id`

每项包含:

- `event_id`, `occurred_at`, `service`, `kind`, `tone=info|warning|error`, `title`
- 可空的 `object`, `request_id`
- `ref`: 类型化业务引用, 如 `approval`, `rag_job`, `plugin_application`, `platform`; 前端据此生成站内路由

不由事件传入任意可执行跳转 URL; 不复制提示词、工具正文、平台处理 flag 或密钥

首版种类: `platform.connected/disconnected`, `config.applied/failed`, `approval.pending/decided`, `rag.completed/failed/interrupted`, `plugin.applied/failed`, `session.completed`

普通会话完成活动按对象至多每 60 秒一项, 包含合并数量; 错误、断线与审批不按此规则合并

完成活动使用固定 60 秒桶归并, count 为该桶内的处理次数, 引用保留最近一次实际实例; 其它活动 count=1

活动在业务状态提交后记录, 重复通知按稳定业务事件键去重; 业务账本仍是决策和执行状态的事实来源

首版轮询, 暂不扩展 `/ws/status` 活动推送

### 5.2 统一审批目录

Control 新增聚合 `GET /api/platform-actions` 与 `/api/platform-actions/summary`; Backend 同路径旧接口保留原语义

Control 在线读取 Backend 的内部 `/api/platform-actions/unified` 与 `/api/platform-actions/unified/summary`, 不改变原 Backend 目录 DTO; 批量决定转交同服务 `/api/platform-actions/decisions`

Backend 离线时, Control 可读取当前数据根的只读账本, 当前账号采用最后一次明确绑定记录并标记 stale; 无法唯一确认账号时禁止决定, 历史账号始终只读; 仅本地记忆事务可以离线处理, 平台网络动作保持不可操作, 认证失败不走该回退

原有 `group|invitation|friend` 来源增加 `memory`; 平台记录以当前账号为默认范围, 历史账号仍只读

新增筛选: `q`, `sort=created|expires`, `order=asc|desc`; 默认创建时间倒序, 到期为空的记录在 expires 排序中置后

统一项新增字段:

| 字段 | 约定 |
| --- | --- |
| `key` | `platform_id`, `self_id`, `source`, `action_id` 的完整组合 |
| `revision` | 此项状态修订, 不冒充群策略修订 |
| `decided_at`, `decided_by` | 决策时间与可信处理身份; 历史记录没有证据时为 null |
| `note` | 决策附言, 最大 1000 字符 |
| `risk`, `risk_source` | 登记时策略给出的风险或 unknown, 不根据操作文案猜测 |
| `group_name`, `requester`, `request` | 当前可确认的展示资料, 缺少来源时 null, 不暴露平台处理凭据 |
| `decision`, `state` | 决策结果与原业务执行状态分开 |
| `can_decide`, `unavailable_reason` | 保留并覆盖记忆来源 |

`decided_by.kind=panel|platform_user|system`; 共用面板密码不提供真实个人身份, 此时 kind=panel 且 id=null, 不把浏览器传入姓名当处理人

状态修订使用业务表内的 `action_revision`, API 暴露为 revision, 每次状态变化递增; 不能拿 group policy_revision 或请求事件 revision 代替

统一 state 使用 pending/ready/executing/succeeded/failed/unknown/rejected/expired, 同时保留 source_state; 记忆提案只有批准并实际生效后才归入 succeeded, 不能只因收到批准请求就标记成功

摘要增加 `by_state`, `by_source`, `today_decided`, `decision_latency_ms`; 接受 from/to/tz, 默认今日, 全部使用同一筛选和统计窗口; 审批列表默认不限制时间, 由分页和保留策略约束

`decision_latency_ms` 只统计有明确人工决策时间的记录, 自动通过和自然过期分别计数

该字段返回当前窗口的平均人工决策延迟; 按状态/来源的数量取窗口内创建的记录, today_decided 与延迟取相同窗口内实际决定的记录, 不要求提案也必须在当天创建

### 5.3 批量决策

`POST /api/platform-actions/decisions`, 请求:

```json
{
  "items": [
    {
      "key": { "platform_id": "qq", "self_id": "bot", "source": "friend", "action_id": "existing-action-id" },
      "expected_revision": 3,
      "approve": true,
      "note": "确认申请来源"
    }
  ]
}
```

- 每批 1-20 项, 重复 key 或非法顶层结构整批 400, 最大并发 2
- 复用原群/好友/记忆决策服务, 每项执行账号、状态、修订及实际操作者检查, 状态变更使用 CAS
- 不承诺跨平台事务; 合法批次返回 200 和逐项 `result=applied|conflict|unavailable|failed|unknown`, `action`, `error`
- `applied` 表示决策已写入, 不表示平台执行成功; 平台结果由 `action.state` 表示
- 未调用业务服务的队列/校验失败使用 failed/unavailable; 已提交而回执不确定使用 unknown
- 网络断开或 unknown 后先查询原记录, 不自动重发; 同一旧修订不能再次触发平台执行
- 单项决策接口可增补 note 和 revision, 旧调用保留原有互斥检查; 新前端使用带修订的入口
- 撤销提示只在前端尚未提交的 5 秒内成立, 发出请求后立即取消撤销入口

## 6. 第四批: 知识库后台任务

### 6.1 新接口

| Control 路径 | 用途 |
| --- | --- |
| `POST /config/rag/jobs` | 提交文本导入或重建, 返回 202 |
| `POST /config/rag/jobs/upload` | 二进制上传并登记任务, 返回 202 |
| `GET /config/rag/jobs` | 按平台/会话/知识库分页读取任务 |
| `GET /config/rag/jobs/{job_id}` | 任务详情 |
| `GET /config/rag/documents` | 分页文档目录, 包括失败和待处理文档 |
| `GET /config/rag/documents/{document_id}/chunks` | 分页片段, `include_text=true` 时才读取正文 |

Chat 增加对应 `/api/chat/rag/jobs`, `/api/chat/rag/jobs/upload`, `/api/chat/rag/jobs/{job_id}`, `/api/chat/rag/documents` 和片段入口, 复用同一服务, 无需 Control 在线

查询和提交必须包含已有的 platform/session/kb 作用域; document/job 的路径 ID 不能单独扩权

提交和单任务查询均返回 `{job: RagJob}`, 任务/文档列表返回 Page 结构; 任务列表支持 operation_id 精确查询, 供提交回执丢失后确认原任务

提交文本/重建使用 `operation_id`, `kb_id`, `expected_revision`, `action=ingest|rebuild` 及原有动作字段; upload 使用原始二进制请求体和等价查询参数, 保持现有 32 MiB 限额

唯一操作范围为知识库和 operation_id, 指纹包含平台/所属会话身份; 同 ID 同指纹返回原任务, 不同指纹返回 409, 不跨平台返回无法查询的原任务; 上传中断且未返回 202 时允许先查 operation_id, 不默认认为未登记

### 6.2 任务与文档协议

任务字段: `job_id`, `operation_id`, `owner_service`, `owner_runtime_id`, `platform_id`, `session_id`, `kb_id`, `action`, `status`, `phase`, `progress`, `created_at`, `started_at`, `finished_at`, `error`, `result`

- `status`: `queued|running|succeeded|failed|interrupted`
- `phase`: `queued|parsing|splitting|embedding|publishing|finished`
- `progress`: `unit=documents|chunks`, `done`, `total`, `percent`; 总工作量未知时 total/percent 为 null
- 同一知识库串行发布, 每进程最多 2 个任务同时处理, 排队最多 32 个; 超限 503 且不假报已接受
- 进度记录限频, 至多每秒或每阶段更新一次, 不按 Token/片段反复打开数据库
- 服务重启将本服务遗留的活动任务标记 interrupted, 不重复执行供应商请求; 其他仍运行服务的任务不被接管
- 保留既有配置修订、知识库锁、索引 generation 和原子发布; 新 generation 未发布前检索继续读取旧 generation
- 失败信息保持阶段与原因, 不暴露凭据; `last_indexed_at` 只在成功发布后更新

文档字段: `document_id`, `source_id`, `name`, `size_bytes`, `created_at`, `updated_at`, `last_indexed_at`, `status=queued|indexing|ready|failed`, `error`, `job_id`, `chunk_count`

文档在处理前登记, 失败项仍可查看和重试; 旧文档缺少原始大小/上传时间时为 null, 不用 updated_at 冒充

持久任务与文档元数据保存在知识库所属数据库中的 `rag_jobs`, `rag_documents`; 会话库随所属会话归档, 不放入 telemetry 库

现有同步 RAG API 不改变返回语义; 新界面主动采用 jobs 路径

检索接口增加 `elapsed_ms`, `candidates`, `returned_count`, `rerank_status`; candidates 表示阈值/重排前实际取回数量, 不冒充全库片段数

## 7. 第五批: 平台, 插件与 Agent

### 7.1 平台与群/好友列表

- 群目录保留现有 total/counts, 当前页批量补 `binding.agent`, `binding.source`, `pending_actions`, `last_activity_at`
- binding.source 为 `group|platform|default`, Agent 来自生效配置解析, 不能直接拿 session_type 当显示名
- `last_activity_at` 从已确认的平台消息归属读取, 同时标记记录覆盖范围
- 好友 total 仅在覆盖完整时为精确值, 否则返回 null 和 `total_loaded`; 不把部分目录的条数当平台总数
- 头像通过适配器声明提供, 不在共用服务里硬编码 QQ 地址规则; 平台不能提供的在线状态/加好友时间为 null
- `POST /api/platforms/{id}/enable` 和 `/disable` 复用配置保存及应用链路, 返回 `saved`, `desired_enabled`, `runtime.status`, `operation_id`
- `runtime.status=applied|pending|unconfirmed|failed`, 保存成功不等于运行时应用成功; 保留既有配置冲突及应用查询

### 7.2 插件

- `/config/plugins` 可通过 `include=health` 批量取得各服务/使用位置的 `health.state=healthy|attention|disabled|unknown`, `as_of`, `coverage`; 不为每个插件单独探测运行时
- 清单可选增加 `display_name`, `icon`, `license`, `homepage`, `repository`; 旧清单默认不变, 外部 URL 不当作可信指令或自动下载来源
- 安装冲突返回 409, `code=plugin_already_installed`, `installed_version`, 不增加覆盖安装能力
- 全局配置响应增加 `updated_at`, 缺少旧元数据时为 null
- 目录总开关 `execution_enabled` 属于宿主插件状态, 默认 true; 不塞入插件作者的 config_schema
- 总开关 false 时所有使用位置及插件间调用均不可执行; 保留原位置配置, 重新开启后恢复原解析结果
- 切换返回保存与逐服务应用状态, 不冒充所有运行实例已更新

### 7.3 Agent 与实例

命名 Agent 列表增加 `updated_at`, `active_instances`, `running_instances`; active 表示已加载实例, running 表示正在执行调用的实例

实例 runtime 增加 `started_at`, `config_applied_at`, `applied_revision`; 不使用会话历史创建时间冒充进程内实例启动时间

Token 与趋势由 stats/sessions 查询, 不强制在每次 `/api/sessions` 列表中计算 14 日趋势; 可按需 `include=usage,context` 取得批量摘要

不改变现有平台/群唤醒策略的优先级

## 8. 第六批: 历史与设置

### 8.1 历史只读浏览

Control 新增 `GET /config/conversations/messages`, Chat 对应 `GET /api/chat/conversations/{id}/messages`

参数: 原有平台/会话身份, `layer=context|history`, `q`, `role`, `limit`, `cursor`, `order=asc|desc`

- `role` 仅对 context 层生效, history 层传入时返回 400, 不伪造用户/助手分拆计数
- 响应含 `items`, `has_more`, `next_cursor`, `as_of`, `consistency=live`, `source=storage`; 不承诺跨页冻结的内存上下文
- 每项提供稳定 `record_id`; 展示轮次附带已存在的 variants, 工具记录和 context_stats
- `q` 在 context 层检索已保存 content 文本投影, 在 history 层检索轮次 user_input/answer/thinking; 不扩展为附件、工具参数或变体全文检索
- context 返回结构化 content 及已保存的 tool_calls/tool_call_id/reasoning_content; history 保留原表字段, 附带 variants/tool_call_records, JSON 文本字段沿用旧管理接口的编码
- variants 不包含 context_messages 中的模型上下文副本, 该字段通过原上下文管理接口读取
- 每页最多 100 条, 正文及当前页子记录按存储字节数合计最多 8 MiB; 提前结束时返回继续游标, 单条超过预算返回 413/message_too_large, 不截断后冒充完整记录
- 使用数据库范围分页, 当前页的变体与工具记录批量读取, 不先完整反序列化所有历史
- 新游标只用于只读浏览, 不充当编辑修订; 编辑前通过原管理接口取得当前 revision, 继续执行原 CAS 和活动实例互斥
- 活跃会话仍允许查看已保存快照, 前端注明存储来源; 旧编辑接口的 memory 语义不改变

目录 `/config/conversations` 按需 `include=aggregates` 返回同一筛选下的平台/上下文/展示轮次去重计数; 不在每次翻页都扫描所有正文

摘要结构为 `aggregates{platforms,conversations,contexts,display_turns}`, 计数覆盖全部匹配结果而非当前页; `aggregates_complete=false` 表示存在读取失败的平台, 计数仅覆盖已成功读取的来源

档案记录只有在存在真实关联依据时提供 conversation_id, 一条消息关联多个上下文时返回列表或空值, 不猜测唯一归属

现有档案没有保存消息到模型上下文的归属关系, 本轮管理读取返回 `conversation_id=null`; 不从当前群路由反推历史归属

### 8.2 YAML 与维护设置

- 新增 `POST /config/validate-yaml`, 输入 `{yaml: string}`, 只解析和校验, 不保存或应用
- 返回 `valid`, `errors[{path,line,column,message,code}]`; 行列从 1 开始, 无法定位的语义错误用 null, 不编造位置
- 复用生产 YAML 解析器和配置校验器, 不使用预览的轻量 YAML 子集解析器
- 现有 `/config/validate` 保持 JSON 配置输入
- 新增 `GET/PUT /config/storage-maintenance`, 使用 expected_revision, 返回 `trash_retention_days`, `auto_purge_enabled`, `revision`
- PUT 输入 `trash_retention_days`, `auto_purge_enabled`, `expected_revision`; 保留期允许 1-3660 天, 未知字段或类型错误返回 400
- 默认保留 30 天, 自动永久清理默认关闭; 开启后后台低频清理过期回收包, 沿用锁、活跃资源和路径检查
- 自动清理在持有任务的 Control 执行, 不允许 Backend/Chat 额外各启动一套清理器
- 原 `/storage/cleanup` 的逐项 results 保留, 增补稳定 `code`; skipped 展示由现有错误/不可处理状态归类, 不重做清理逻辑

## 9. 新增文件与目录

以下为六批完成后的计划清单, 不表示本次全部创建

```text
satrap/core/telemetry/                         # 新目录, 共用采集与聚合边界
  __init__.py                                 # 轻量公共入口, 导入时不启动线程
  contracts.py                                # 统计, 活动, 健康的类型与参数约束
  health.py                                   # 服务启动身份与版本快照
  store.py                                    # 独立库迁移, 批写, 汇总和保留期
  capture.py                                  # 有界队列与消息/活动采集入口
  model.py                                    # 共用 SDK 调用计量, 与 Debug 观察并行分发
  query.py                                    # 来源聚合, 时间桶, 缓存和分页
  api.py                                      # 已鉴权请求的统计/活动/设置分派

satrap/core/platform/action_decisions.py        # 新文件, 批量决策编排, 复用原业务服务
satrap/core/config/rag_jobs.py                  # 新文件, 管理任务队列与持久文档/任务状态
satrap/core/config/maintenance_settings.py      # 新文件, 维护设置与唯一清理任务生命周期

tests/unit/test_telemetry_flows.py              # 新文件, 共用采集/存储/聚合整合测试
docs/development/ui-backend-contract-2026-10-27.md  # 本文件
```

合计: 1 个新增代码目录, 11 个 Python 生产文件, 1 个整合测试文件, 1 份契约文档, 共 13 个文件; 不包含任何 UI 文件

目录树中的注释仅说明文件职责; 不新增通用任务系统、通用事件总线或独立统计服务进程

### 9.1 运行时生成, 不提交 Git

```text
.satrap/data/telemetry/                        # 随 SATRAP_DATA_ROOT 移动
  control.sqlite
  backend.sqlite
  chat.sqlite

.satrap/config/telemetry.json                  # 随现有配置根目录移动
.satrap/config/storage-maintenance.json
.satrap/config/plugins/.host-state.json         # 插件宿主总闸与实际更新时间, 不改写位置配置
.satrap/data/maintenance/control-purge.lock     # Control 唯一清理器的进程租约
.satrap/data/rag/jobs/control.lock              # RAG 宿主任务拥有权
.satrap/data/rag/jobs/chat.lock
.satrap/data/rag/imports/jobs/                  # 已接收任务的私有临时输入, 完成/失败/重启中断后回收
```

SQLite 的 WAL/SHM 由运行时生成; CLI/local 如启用统计使用独立 local.sqlite, 不混入 Backend

RAG 临时上传沿用已有 imports 位置和清理约束; rag_jobs/rag_documents 为所属现有数据库内的新表, 不新增另一套平台数据库

## 10. 修改现有文件的范围

| 文件或目录 | 修改内容 |
| --- | --- |
| `satrap/core/backend/BackendManager.py`, `http_api.py`, `control_server.py` | 健康与新路由接入, 来源聚合, 采集/清理任务生命周期; 不继续堆入统计 SQL |
| `satrap/display/server.py`, `service.py` | Chat 健康, 统计来源, 新 RAG 任务和历史浏览入口, 轮次去重采集 |
| `satrap/core/APICall/LLMCall/sync.py`, `async_.py` | 共用模型计量入口, 保持同步/异步与流式返回协议 |
| `satrap/core/log/debug/model.py` | 作为可选 Debug 观察者复用, 保留原始 Debug 数据和独立开关 |
| `satrap/core/log/stream.py`, `api.py` | 进程内日志时间桶与 stats 路由 |
| `satrap/core/platform/__init__.py`, `onebot/adapter.py`, `misskey/adapter.py` | 连接和身份健康字段, 活动采集 |
| `satrap/core/pipeline/scheduler.py`, `satrap/core/platform/__init__.py`, `satrap/display/service.py` | 已准入消息与确认的最终发送结果采集, 不额外修改档案正文或原平台事件结构 |
| `satrap/core/platform/action_catalog.py` | 原有目录补展示字段/计数, 新 Control 聚合复用 |
| `satrap/core/config/group_actions.py`, `satrap/core/friends/store.py`, `satrap/core/memory/` | 决策修订/处理身份/附言与风险快照, 业务状态仍由原服务维护 |
| `satrap/core/config/platform_schema.py`, `satrap/core/storage/database.py` | 集中表迁移, 新索引与存储归档范围; 不让新存储各自争夺 user_version |
| `satrap/core/rag.py`, `satrap/core/config/rag_service.py` | 持久文档状态, 阶段进度, 成功索引时间, 保留原有 generation 发布 |
| `satrap/edictum/plugin_catalog.py`, `plugin_config.py`, `satrap/core/config/plugin_service.py` | 展示元数据, 宿主总闸, 更新时间和健康摘要 |
| `satrap/edictum/plugin_archive.py` | 已有冲突的结构化响应 |
| `satrap/core/plugin_interop/capabilities.py` | 总闸参与已生效能力判定, 不允许插件互调绕过停用; 保留位置与作者能力开关 |
| `satrap/core/framework/SessionManager.py`, `satrap/core/config/edictum_service.py` | 实例时间与 Agent 批量摘要 |
| `satrap/core/framework/providers/edictum.py`, `SessionClassManager.py`, `satrap/edictum/config.py`, `satrap/core/config/session_class_service.py` | 区分真实配置更新时间、实例启动和运行时实际应用时间 |
| `satrap/core/config/group_directory.py` | 当前页批量群绑定/动作/活动摘要 |
| `satrap/core/friends/service.py`, `satrap/core/platform/onebot/friends.py` | 真实目录总量及适配器已提供的可空资料, 不猜测在线状态或头像地址 |
| `satrap/core/config/conversation_data.py`, `conversation_catalog.py`, `satrap/core/storage/maintenance.py` | 只读数据库分页, 同筛选聚合, 维护设置接入 |
| `satrap/core/config/platform_message_data.py`, `document.py`, `_yaml.py`, `satrap/core/storage/file_lock.py` | 档案归属未知值, 生产 YAML 定位校验, 真正跨进程共享的 RAG 读取租约 |
| `tests/unit/` 现有 logging/knowledge/group/friend/memory/plugin/storage/conversation 集成测试 | 按业务批次扩展, 不为每个字段新增测试模块 |
| `.github/workflows/` 现有测试工作流 | 新增后端场景已位于现有 tests/unit 门禁范围, 无需为本轮修改工作流或 UI 门禁 |

时间区间解析使用标准库 zoneinfo; 实施前核对 Windows/IANA 数据可用性, 如现有依赖不保证提供则显式更新 requirements/项目依赖声明, 不靠开发机隐式安装

实现时可收敛未需要修改的既有文件; 涉及新增文件超出第 9 节清单时先更新本契约说明职责

## 11. 分批交付与验收

| 批次 | 新增文件 | 主要交付 |
| --- | --- | --- |
| A | telemetry 的 init/contracts/health | 三服务独立健康, 现有接口兼容验证 |
| B | telemetry 的 store/capture/model/query/api, test_telemetry_flows.py | 消息/用量/日志统计, 覆盖信息与存储预算 |
| C | action_decisions.py | 活动流, 含记忆的统一审批, 批量决策 |
| D | rag_jobs.py | 持久后台任务, 文档状态, 片段预览 |
| E | 无额外新模块 | 平台/群/插件/Agent 现有接口扩展 |
| F | maintenance_settings.py | 历史只读分页, YAML 定位校验, 维护设置 |

本契约文档独立先行; A-F 分开提交, 不把所有业务变更压成一个无法回滚的大提交

提交先分离 A-F 的领域协议/实现, 三个宿主共有的路由和生命周期接线与跨域 HTTP 回归作为最后一个集成提交; 测试验收在完整工作树执行, 不将中间尚未接线的提交宣称为已上线功能

### 11.1 契约与正确性

- 通过后端 HTTP 契约测试验证旧请求继续可用, 新增字段的 null/覆盖状态符合约定; UI 展示由独立工作验收
- 三服务独立认证, Backend 停止时 Control 历史和 Chat 主功能仍可用
- 同群不同账号, 同号群/私聊, /new, Fork, 重试和回复变体不串统计或重复计数
- SDK 同步/异步, 流式/非流式, usage 缺失, 供应商错误, 取消和显式降级均有真实边界用例
- 批量审批包括跨来源, 记忆提案, 部分冲突, 账号切换, 重复旧修订, 离线及不确定回执
- RAG 包含重复 operation_id, 配置修订冲突, 中途失败, 重启中断, 旧 generation 可继续查询及临时文件回收
- 旧库迁移可读取, 决策处理人/旧文档上传时间缺失时不伪造; 只读查询不初始化不存在的业务库
- 新增后端公开结构使用 TypedDict/dataclass/枚举, 不以 Any 掩盖协议; 文档 DTO 供 UI 独立实现时参考

### 11.2 修改前后 benchmark

在相同机器和语料下保存 before/after 原始结果, 至少包含:

- 10 万消息, 1 万模型调用, 1000 个会话的总览/模型/实例分页: 冷/热 P50/P95, SQL 次数, 正文解码次数
- 10000 条单会话历史的首屏和后续页, 验证翻页不重复加载全部正文
- 日志统计与消息/模型采集的关闭/开启对照: 热路径耗时, 事件循环延迟, 队列峰值和进程 RSS
- 同一语料 1 万调用的明细库/WAL/汇总占用, 清理前后大小和覆盖信息
- 群目录/插件健康当前页查询次数, 确认没有逐行 N+1 请求
- 总览 HTTP 接口首次有效响应时间与服务间请求数, Backend 停止和单来源认证失败场景; 浏览器性能由 UI 工作单独验收

硬性门槛: 摘要不读取正文, 不在事件循环同步读写 SQLite, 请求数量不随卡片数量线性增长, 轮询不全表扫描完整历史, 队列和解码内存有界

延迟目标在 A 的基线结果后固定到 benchmark 配置, 不把不同机器的绝对数字当验收结论; 性能结果与正确性场景分开记录

每批执行相关后端整合测试、真实 HTTP 契约测试和类型检查; 最终合并前执行一次完整后端测试与 Windows/Linux/macOS 后端 CI, 不为每批重复全量; 本轮不修改或扩展前端测试与浏览器门禁

### 11.3 数据升级与回退

- 各库迁移由所属模块执行, 只读端点不能顺带创建表或提升版本
- 每批数据库变更先验证旧库升级, 备份与恢复; 新字段采用可空或有明确旧值语义的默认值
- 遥测库不参与业务恢复和审批授权, 可以独立停止采集, 保留业务服务继续运行
- RAG 与动作表的新增字段不得在回退时 DROP; 回退代码前先确认旧代码接受升级后的结构版本, 否则使用已备份库和对应版本一起回退
- 部署前保存后端代码版本, 配置和受影响业务库; 本轮不替换前端产物, 数据存在新写入时不自动覆盖为旧备份
- 回滚代码不会撤销已经提交的审批或索引发布; 以业务账本与实际 generation 核验结果

## 12. 实施进度与证据

2026-10-27 后端实施与本地验收, 未部署, 未修改 UI:

| 批次 | 当前状态 |
| --- | --- |
| A | 三服务健康, Control 独立鉴权和适配器连接资料已接入并通过本机真实 HTTP 与回归测试; 跨平台 CI 尚未验收 |
| B | 已接入三服务生命周期, 消息/命令/Chat 回复和 SDK 边界采集, 聚合/设置/日志统计路由已验证; 大样本查询与开启/关闭采集对照已记录, 全部验收仍待最终复核 |
| C | 已接入四来源统一目录, 查询/排序/摘要与带修订批量决定, Control/Backend 真实 HTTP 记忆审批和并发门禁通过; 已提交审批, 连接, 配置/插件应用, RAG/安装及按对象合并的会话完成活动均已接入 |
| D | 已实现持久任务、私有输入回收、两并发及同库串行、任务幂等和修订、文档/片段分页、旧 generation 并发查询、恢复任务中断; 已执行真实 Control/Chat TCP 与跨进程租约场景 |
| E | 已实现平台配置及应用回执、群当前页批量摘要、真实好友资料、插件宿主总闸/健康/安装冲突、Agent 与实例时间及可选批量计量; 411 项相关回归通过, 最终复核继续进行 |
| F | 已实现独立只读消息分页、同筛选摘要、YAML 真实定位与维护 CAS 设置; Control 唯一清理器默认关闭, 回收包恢复/永久删除互斥; 新增场景及相关回归 171 项通过 |

已执行证据:

- 新增健康/存储/写入器/SDK/时区/活动整合场景已扩展至 12 项; 包含三服务真实 TCP 统计聚合与独立鉴权, 损坏来源隔离, 仅活动数据的空间预算, 未开始消费的流关闭, 上下文占用和旧库升级
- 既有模型协议场景: 88 passed; 测试构造器补齐真实客户端已有的 base_url, 生产入口未为不完整测试对象添加兜底
- 健康/统计及相关宿主后端回归: 474 passed, 1 skipped; 跳过项为 Windows 无符号链接权限, 此记录在活动路由后续扩展之前执行, 不冒充 C-F 验收
- 新模块和变更宿主模块 Pyright: 0 errors, 0 warnings; 后续新增模块继续复查
- 活动路由扩展后的健康/目录/适配器定向回归: 133 passed; 审批事务与平台库迁移的群/好友/记忆/存储回归: 367 passed, 1 skipped, 跳过项仍为 Windows 符号链接权限
- 平台库已迁移至版本 12, 新旧审批仍由原业务事务处理; 新增动作项修订, 处理身份/附言和风险未知值, 旧记录不伪造人工身份; C 核心入口已接入, 整体验收仍需 D/E 的活动生产者及最终门禁
- C 的当前后端回归: 381 passed, 1 skipped, 跳过项为 Windows 无符号链接权限; 后续新增的遥测 v1/v2 迁移, 跨账号游标失效, 公开字段搜索与同项并发场景已定向通过
- 已用真实 Control/Backend TCP 验证四来源目录与记忆决定, 重复批次整批拒绝, 相同旧项修订仅一次生效, 跨批次总执行并发不超过 2; 决策记录保留 panel/id=null 的真实身份口径
- 真实 POST 验证修正了 Control HTTP 单请求连接缺少 Connection: close 的既有问题; 同一 aiohttp 客户端顺序 GET/POST 现可完整通过, 没有通过测试重试规避
- 遥测库迁移至版本 2 以保存完成活动的合并次数; v1 只读查询不升级库, 正常写宿主升级后保留原活动
- 当前 12 项遥测整合场景与 88 项模型协议场景合跑: 100 passed; 统一审批候选在每个平台返回后即裁剪到 limit+1, 单请求并行读取最多 4 个平台, 不累计全部平台的详情
- 健康 TCP benchmark 基线来自 Git `61b77753d30a1d289a10ec1dda4c65d2a7319ad1`, 使用相同 Python 3.13.5/Windows 机器, 每组预热 20 次后采样 200 次
- 基线 P50/P95 为 1.944/2.810 ms, 新健康字段组为 1.776/2.862 ms; 单次样本不宣称变快, 新增字段后 P95 增加约 0.052 ms
- 原始脚本, 基线源码归档和样本位于忽略目录 `tests/.pytest_cache/telemetry-benchmark/`, 不进入 .toolkit 或 UI 目录

### 12.1 B 的本机性能样本

同机 Python 3.13.5/Windows, 全部假供应商调用, 未访问真实模型或业务对话:

| 场景 | 测量结果 |
| --- | --- |
| 10 万消息, 1 万调用, 1000 实例, 总览 | 首次 649.7 ms, 热 P50/P95 643.7/677.8 ms; 6 条 SQL, Python 分配峰值约 0.52 MB |
| 同语料模型分页, 纯消息桶过滤前 | 热 P50/P95 582.7/656.1 ms; 3 条 SQL |
| 同语料模型分页, 过滤后 | 热 P50/P95 384.6/405.6 ms |
| 同语料实例分页, 纯消息桶过滤前 | 热 P50/P95 560.5/578.2 ms; 4 条 SQL, Python 分配峰值约 1.69 MB |
| 同语料实例分页, 过滤后 | 热 P50/P95 401.2/433.2 ms |
| 5000 次无网络同步调用, 采集关闭/开启 | P50 1.1/32.7 us, P95 1.6/58.1 us; 两组均开启 tracemalloc |
| 同步突发过载 | 队列峰值 4096, 明确丢弃并报告 776 条观测, 关闭时已排空; 不用于代表正常供应商流量 |
| 2000 次异步调用, 每批 20 次并让出调度 | 1 ms 心跳的额外延迟 P95 关闭/开启约 15.128/15.091 ms; Windows 定时器影响明显, 不据此宣称性能提升; 均无丢弃 |
| 同异步对照进程 RSS 增量 | 关闭约 0.03 MB, 开启约 0.24 MB; 单轮进程采样, 不作为稳定内存承诺 |
| 大语料文件与清理 | 初始库约 71.4 MB; 明细过期后删除 110000 行, 保留 30000 个汇总桶; 修正增量回收游标消费后, 一轮有界回收使库缩至约 37.8 MB, WAL 为 0 |

统计接口在基线提交中不存在, 因此不编造旧接口延迟; 上表过滤前/后是在相同汇总语料上的算法对照, 此外总览复测 P95 759.4 ms 显示单轮时间有波动

SQL 跟踪未访问 chat_history/display_turns/platform_messages 等正文表; 这些查询的正文解码为 0, Python 内存峰值不包含 SQLite 原生缓存; 原始 JSON 与脚本均留在上述忽略目录

B 本机回归门槛以该语料固定为: 总览热 P95 <= 1500 ms, 模型/实例热 P95 <= 1000 ms, 响应最多 10000 趋势桶, 当前页查询次数保持常数; 不据此替代最终 HTTP/跨平台验收

此节不作为整个契约已完成或全量 CI 通过的证明, 完成前须逐项对照第 11 节验收

### 12.2 D-F 与最终本机验证

- D 的真实 Control/Chat TCP 包含原同步上传保持 200、新任务接收 202、operation_id 查询、修订冲突、私有库隔离、文件大小上限和独立鉴权
- 两个真实进程同时持有共享 generation 读取租约时, 独占维护不能进入; 发布重建后旧 generation 在读者退出前不被删除
- 同库任务串行但不占满两个执行槽, 不同库最多两项并发、最多 32 项等待; 请求取消后已接收任务仍会完成登记/安排, 重启与归档恢复不自动重放
- 新维护策略默认为关闭且只读不创建配置文件, CAS 冲突保留新值, 同一数据根仅一个 Control 持有清理任务; 清理只访问合法回收包, 不调用全库孤儿扫描
- 真实 HTTP 验证新增历史分页、维护设置和 YAML 语法错误位置; 生产解析器能接受仅注释的 collection 节点, 语义错误无法定位时保留 null
- F 及相关回归 171 passed; 后续 RAG/配置/对话存储合跑 183 passed; 严格类型检查的新增 11 个独立模块为 0 errors, 0 warnings
- Windows Python 3.13.5 完整 tests/unit 首轮为 3978 passed, 9 failed, 8 skipped; 失败均定位为旧夹具缺失新增运行字段或降低版本号却保留新列, 修正后相关 300 passed, 1 skipped
- 随后的完整后端执行为 3987 passed, 8 skipped, 用时约 315 秒; 8 项跳过分别为 4 项 Windows 符号链接权限、3 项 POSIX 进程组专属场景、1 项需显式开启的真实视觉集成验收
- 旧宿主模块仍含 Unknown 类型警告, 不把独立新增模块的零警告误述为全仓零警告; 后续类型注释收敛和跨平台结果单独记录
- 未运行前端测试、浏览器测试或构建, 因本轮没有 UI 修改; 现有后端 CI 会自动收集新增 tests/unit 场景

### 12.3 历史分页与聚合 HTTP 对照

同一 Windows/Python 3.13.5, 合成数据不使用真实群消息, 原始脚本与 JSON 均在忽略目录 tests/.pytest_cache/telemetry-benchmark:

| 10000 条单会话记录, 每页 50 条 | 旧完整读取 P50/P95 ms | 新只读分页 P50/P95 ms | Python 分配峰值, 旧/新 |
| --- | --- | --- | --- |
| context 首屏 | 104.1 / 111.6 | 1.33 / 1.81 | 40.24 MB / 0.078 MB |
| context 第二页 | 106.4 / 213.7 | 1.28 / 1.59 | 40.24 MB / 0.079 MB |
| history 首屏 | 132.3 / 230.3 | 1.85 / 2.77 | 84.07 MB / 0.112 MB |
| history 第二页 | 131.5 / 246.4 | 1.67 / 2.07 | 84.07 MB / 0.112 MB |

旧路径为仍保持原语义的 read(), 其消息正文读取逻辑与基线提交相同; 新路径为 messages(), 两者共享同一数据库、范围和每页条数, 新路径不承担旧编辑 revision 的整份内容哈希

每组预热一次后 11 个热样本, P95 使用 nearest-rank; 同期有后端测试运行, 故保留全部原始样本, 不把这些绝对耗时当作跨机器承诺

context 每页解码从 10000 条降至 50 条, SQL 为 5/7 条; history 不调用上下文解码, 当前页正文读取从 10000 行降至 50 行, SQL 为 10/16 条, 多出的查询为固定表结构探测和当前页子记录读取, 不随全会话条数增长

三个真实 TCP 服务的总览接口, 每组 12 个样本:

| 场景 | 冷请求 P50/P95 ms | 缓存命中 P50/P95 ms | 每个冷请求/热请求的上游数量 |
| --- | --- | --- | --- |
| 两个来源在线 | 55.84 / 64.35 | 4.68 / 5.43 | 2 / 0 |
| Backend 认证拒绝 | 41.25 / 52.90 | 4.95 / 6.86 | 2 / 0 |
| Backend 停止, 存在历史快照 | 2029.34 / 2042.43 | 5.42 / 5.87 | 2 / 0 |

冷请求指主动清除五秒聚合缓存后的首个有效响应, 非 Python 进程冷启动; Backend 停止样本包含 Windows 网络连接等待, 最长单来源等待受两秒 timeout 限制, 认证拒绝不读该来源的磁盘快照来绕过认证

认证失败标记 auth_required, 停止后只在当前可信数据根已有快照时标记 stale, 其余来源仍返回; 不把离线来源伪造为零

群目录当前页含新增绑定/审批/活动资料时的集成断言为最多 7 条 SELECT; 100 个插件健康摘要仅两次服务请求, 不按插件逐个探测
