# Satrap UI Benchmark

该基准使用生产构建和固定 HTTP/WebSocket 夹具测量前端本身, 不包含模型、数据库和真实网络延迟

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

RAF 数据来自固定版本的 Chromium 新无头模式, 适合与同一设备上的基线比较, 不应当作显示器物理刷新率

对比报告默认将 FPS、帧耗时、脚本、布局、样式重算和内存变化超过 10%～15% 标记为回退, 流式内容完整性不允许回退
