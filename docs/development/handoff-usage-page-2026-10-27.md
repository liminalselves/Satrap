# 交接: 用量统计页移植 (2026-10-27)

分支 `ui-redesign`, 所有改动未提交。

## 状态

- 已完成 (工作区, 未浏览器复核): 对话侧栏行样式 (`satrap-ui/src/pages/Chat/ChatSidebar.tsx`), 原始 YAML 编辑器高度 (`satrap-ui/src/pages/Settings/RawConfigSection.tsx`), 浅色玻璃变量 .58/.78 (`satrap-ui/src/styles/globals.css`)
- 未开始写代码: 用量统计页生产移植。预览已获用户批准

## 任务: 移植 `/usage`

来源: `satrap-ui-preview/src/pages/usage/Usage.tsx` (布局/交互) 与 `data.ts` (仅模拟数据, 不移植)。

需要改动:

1. 新建 `satrap-ui/src/pages/Usage/index.tsx`, 导出 `Usage`
2. `satrap-ui/src/router.tsx`: 增加 lazy 导入与 `{ path: 'usage', element: lazyRoute(<Usage />) }`
3. `satrap-ui/src/components/layout/nav.ts`: 在 `data` 组加 `{ path: '/usage', label: '用量统计', icon: BarChart3, keywords: 'usage tokens yltj' }`。命令面板读取 `navGroups`, 无需单独改
4. `satrap-ui/src/pages/Dashboard/index.tsx:485`: 今日 Token 卡片 `to="/logs"` 改为 `to="/usage"`
5. 运行 `npm run build` 与相关 e2e (`satrap-ui/e2e/run.mjs`), 必要时补 e2e 场景

## 导入路径对照 (预览 → 生产)

- `@/lib/cn` → `@/utils/cn`
- `@/ui/*` → `@/components/kit` (Badge, StatusDot, Button, Card, CountUp, PageHeader, Sparkline, Tabs, Tooltip 均已导出)
- `Tooltip` 的 `content` 仅接受 string; `Tabs` 需要 `label` 属性, 支持 `size`, `icon`, `count`
- CSS 类 `animate-rise`, `stagger`, `display`, `num`, `tint-*`, `text-tint` 生产中均存在

## 数据接入

客户端 `statsApi` 位于 `satrap-ui/src/api/contracts.ts:255`, 类型在同文件 44-87 行:

- `statsApi.overview(query, signal)` → `Overview`: `usage`, `series: MetricBucket[]` (每桶 `usage.input_tokens/output_tokens/total_tokens/calls`), `comparison.tokens.percent_delta/previous`, `sources`, `complete`, `warnings`
- `statsApi.models(query, signal)` → `models: ModelMetrics[]`, 每项 `trend: MetricBucket[]` (预览里是 number[], 需映射为 `usage.total_tokens ?? 0`)
- `statsApi.sessions(query, signal)` → `sessions: SessionMetrics[]`, 含 `context`

查询参数 (`StatsWindowQuery`): `from`, `to`, `tz`, `bucket`, `purpose`, `limit` (1-100)。后端规则 (`satrap/core/telemetry/contracts.py:186`):

- from/to 必须同时提供, 否则用默认窗口: overview 今日零点起, models 7 天, sessions 14 天
- 粒度上限: minute 24h, hour 31 天, day 366 天, 超限返回 400
- 默认 tz `Asia/Shanghai`; `purpose=all` 等于不过滤

范围映射建议:

- 今日: 不传 from/to, `bucket: 'hour'`
- 近 7/30 日: `from` 取 (今日零点 - (N-1) 天), `to` 取现在, `bucket: 'day'`
- 用途 Tabs 传 `purpose`, 三个接口同时刷新; models/sessions `limit: 100`

同比: 后端只为默认今日窗口提供 `yesterday_same_elapsed`; 跨天范围显示 "跨天范围不提供同比"。

## 沿用的生产惯例

- 轮询: `startVisiblePoll(task, { interval: 30000 })` (`src/utils/visiblePoll.ts`), 参考 Dashboard 398-441 行, 每个请求 `.catch(() => null)`, 检查 `signal.aborted`
- 数值防御: Dashboard 里的 `finite()` 辅助函数
- 覆盖提示: Dashboard 的 `CoverageChips` 和 `sourceTone` (131 行、95 行), 可抽出复用或复制; 服务名 `SERVICE_LABELS`
- 未知值显示 "—", 未上报用量的 Token 不按 0 计入 (预览底部说明保留)
- 空数据: `EmptyState` (kit), 加载: `Skeleton`
- 注释风格: 中文, 只写约束

## 注意

- Node PATH 有怪癖, 见记忆 `frontend-redesign-preview`
- 预览的柱状图为 `<button>` 列, 悬停/聚焦显示明细, 保留 aria-label
