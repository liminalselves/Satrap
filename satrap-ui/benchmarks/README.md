# Satrap UI Benchmark

该基准使用生产构建和固定 HTTP/WebSocket 夹具测量前端本身, 不包含模型、数据库和真实网络延迟

## 信息加载与登录基准

`npm run benchmark:loading` 在内存中构建生产资源并启动独立临时 HTTP 服务, 不覆盖 `dist`, 不启动用户后端, 不访问用户数据库

```powershell
npm run benchmark:loading -- --baseline
npm run benchmark:loading
npm run benchmark:loading -- --baseline --catalog
npm run benchmark:loading -- --catalog
npm run benchmark:loading -- --baseline --interaction
npm run benchmark:loading -- --interaction
```

- 默认 2 次预热与 10 次正式运行, `BENCHMARK_ITERATIONS` / `BENCHMARK_WARMUPS` 可调
- `--baseline` 从 `SATRAP_BENCHMARK_BASE` 指定提交读取原始前端源码, 默认当前 HEAD; 不切换工作区, 不覆盖已存在基线
- 修改后使用当前工作区源码, 记录构建资源摘要, 基于提交和生产差异摘要
- 普通场景覆盖 0 / 150 / 300ms 模拟 API 等待及长历史, `--catalog` 覆盖模型与插件页面的冷 / 热读取
- 热缓存场景区分 SPA 数据显示与版本探测, 不通过清空 store 模拟热加载
- `--interaction` 使用与原交互基准一致的完整 Chromium 和启动参数, 采集滚动及 1,000 增量内容完整性
- 实际浏览器, Node, OS 或采样数不同会拒绝生成改善比例; 超时样本保留且不计算虚假加速比例
- 默认写入 `results/frontend-loading-2026-10-23`, 后续运行可用 `SATRAP_BENCHMARK_OUTPUT` 指定新的结果目录
- `query_loading.py` 在独立内存 SQLite 执行生产查询, 输出 30 次采样; `catalog_loading.py` 比较原提交与当前目录的时间和 Python 峰值内存
- `auth_loading.py` 使用临时配置与凭据库, 验证旧 Cookie 引导失败与当前持久恢复, 单独统计认证函数开销; 真实浏览器重开和真实 HTTP 由 `test_minihttp.py` 验收
- `concurrent_loading.py` 在临时数据库执行原提交与当前真实路由, 测量 5,000 会话查询期间健康检查的调度延迟; 不包含 HTTP 传输时间
- `archived-headless-shell-baseline.json` 是统一交互浏览器参数前保留的排查记录, 不参与正式交互前后比较

这些结果不包含真实公网延迟, 真实模型推理或远端生产数据库耗时

## 命令

```powershell
npm run benchmark:smoke
npm run benchmark:baseline
npm run benchmark
```

- `benchmark:smoke`: 单次短场景, 用于检查基准框架是否可运行
- `benchmark:baseline`: 1 次预热和 5 次正式运行, 覆盖全部场景并更新基线
- `benchmark`: 使用同样的完整场景生成最新结果并与基线比较

首次运行前需要安装 Chromium:

```powershell
npx playwright install chromium
```

可用环境变量:

- `BENCHMARK_ITERATIONS`: 正式运行次数, 默认 `5`
- `BENCHMARK_WARMUPS`: 预热次数, 默认 `1`
- `BENCHMARK_IDLE_MS`: 单个背景静置场景时长, 默认 `10000`

结果保存在 `benchmarks/results/`, `baseline.json` 是基线, `latest.json` 是最近一次结果, `comparison.md` 是对比报告

## 场景

- 仪表盘浅色和深色动态背景静置
- 玻璃卡片连续指针移动
- Chat 100、500、1000 条消息滚动
- Chat 1000、5000 个 WebSocket 流式增量
- Chat 选项、设置弹窗和主题切换

## 指标

- RAF 平均 FPS、P95 帧耗时、最长帧和掉帧数
- Long Task 数量和总时长
- Chromium 任务、脚本、布局和样式重算耗时
- JS Heap 起始、峰值和结束值
- DOM 节点数和渲染器 Mutation 记录数
- 流式首字符延迟、最终字符数和内容完整性

生产构建未注入 React Profiler, 因此使用 MutationObserver 记录真实 DOM 更新量, 避免性能探针改变待测对象本身

夹具按页面来源允许携带凭据, 状态 WebSocket 使用与真实客户端一致的 data 信封
未匹配接口, 页面异常及非主动取消的请求失败会使场景失败; 仪表盘适配器与 Chat 历史加载完成后才开始测量
固定使用 4173 严格端口, 端口占用时直接失败, 避免误测其它服务

RAF 数据来自固定版本的 Chromium 新无头模式, 适合与同一设备上的基线比较, 不应当作显示器物理刷新率

对比报告默认将 FPS、帧耗时、脚本、布局、样式重算和内存变化超过 10%～15% 标记为回退, 流式内容完整性不允许回退

## 对话记录与统一审批中心

从仓库根目录执行查询规模基准, 使用已安装项目依赖的 Python:

```powershell
python satrap-ui/benchmarks/platform_actions.py
```

从 satrap-ui 执行生产浏览器验收:

```powershell
npm run build
node benchmarks/verify-platform-management.mjs
```

查询基准在系统临时目录创建合成数据库, 覆盖 1 / 5 个平台和每平台 1,000 / 10,000 条动作, 每组 2 次预热与 30 次正式采样; 记录列表和摘要耗时, SQL 次数及查询期间健康检查的额外调度延迟

浏览器验收使用生产 dist 和独立临时 HTTP 服务, API 与 WebSocket 使用夹具, 覆盖标签, 编辑保护, 统一审批, 原动作语义, 写响应丢失, 账号和离线状态; 不访问生产后端或用户数据库

结果写入 results/platform-approvals-2026-10-23, 临时数据库和服务在结束时释放; 基准不包含真实 HTTP 查询传输和公网平台耗时
