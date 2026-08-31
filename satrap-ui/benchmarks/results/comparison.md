# Satrap UI Benchmark 对比

- 当前结果: 2026-08-31T13:38:27.180Z
- 基线结果: 2026-08-31T13:32:02.460Z
- 浏览器: 151.0.7922.34
- 迭代: 3 次正式运行, 1 次预热

| 场景 | 指标 | 基线中位数 | 当前中位数 | 变化 | 判定 |
|---|---:|---:|---:|---:|---:|
| 仪表盘浅色静置 | average_fps | 157.843 | 209.902 | 33.0% | 通过 |
| 仪表盘浅色静置 | p95_frame_ms | 12.3 | 8.4 | -31.7% | 通过 |
| 仪表盘浅色静置 | longest_frame_ms | 24.9 | 16.8 | -32.5% | 通过 |
| 仪表盘浅色静置 | dropped_frames | 1 | 0 | -100.0% | 通过 |
| 仪表盘浅色静置 | long_tasks | 0 | 0 | n/a | 通过 |
| 仪表盘浅色静置 | long_task_total_ms | 0 | 0 | n/a | 通过 |
| 仪表盘浅色静置 | task_ms | 1,086.848 | 488.077 | -55.1% | 通过 |
| 仪表盘浅色静置 | script_ms | 21.521 | 30.705 | 42.7% | 回退 |
| 仪表盘浅色静置 | layout_ms | 0 | 0 | n/a | 通过 |
| 仪表盘浅色静置 | recalc_style_ms | 113.114 | 106.071 | -6.2% | 通过 |
| 仪表盘浅色静置 | js_heap_peak_mb | 5.244 | 5.255 | 0.2% | 通过 |
| 仪表盘浅色静置 | dom_nodes | 186 | 186 | 0.0% | 通过 |
| 仪表盘浅色静置 | renderer_mutations | 0 | 0 | n/a | 通过 |
| 仪表盘深色静置 | average_fps | 145.357 | 196.924 | 35.5% | 通过 |
| 仪表盘深色静置 | p95_frame_ms | 12.5 | 8.4 | -32.8% | 通过 |
| 仪表盘深色静置 | longest_frame_ms | 25.1 | 21.1 | -15.9% | 通过 |
| 仪表盘深色静置 | dropped_frames | 2 | 1 | -50.0% | 通过 |
| 仪表盘深色静置 | long_tasks | 0 | 0 | n/a | 通过 |
| 仪表盘深色静置 | long_task_total_ms | 0 | 0 | n/a | 通过 |
| 仪表盘深色静置 | task_ms | 656.644 | 469.824 | -28.5% | 通过 |
| 仪表盘深色静置 | script_ms | 22.39 | 30.604 | 36.7% | 回退 |
| 仪表盘深色静置 | layout_ms | 0 | 0 | n/a | 通过 |
| 仪表盘深色静置 | recalc_style_ms | 102.871 | 99.75 | -3.0% | 通过 |
| 仪表盘深色静置 | js_heap_peak_mb | 5.254 | 5.28 | 0.5% | 通过 |
| 仪表盘深色静置 | dom_nodes | 194 | 194 | 0.0% | 通过 |
| 仪表盘深色静置 | renderer_mutations | 0 | 0 | n/a | 通过 |
| 玻璃卡片指针移动 | average_fps | 114.323 | 181.895 | 59.1% | 通过 |
| 玻璃卡片指针移动 | p95_frame_ms | 12.6 | 8.5 | -32.5% | 通过 |
| 玻璃卡片指针移动 | longest_frame_ms | 33.2 | 25 | -24.7% | 通过 |
| 玻璃卡片指针移动 | dropped_frames | 1 | 1 | 0.0% | 通过 |
| 玻璃卡片指针移动 | long_tasks | 0 | 0 | n/a | 通过 |
| 玻璃卡片指针移动 | long_task_total_ms | 0 | 0 | n/a | 通过 |
| 玻璃卡片指针移动 | task_ms | 304.949 | 240.575 | -21.1% | 通过 |
| 玻璃卡片指针移动 | script_ms | 17.042 | 16.471 | -3.4% | 通过 |
| 玻璃卡片指针移动 | layout_ms | 0 | 0 | n/a | 通过 |
| 玻璃卡片指针移动 | recalc_style_ms | 89.697 | 80.379 | -10.4% | 通过 |
| 玻璃卡片指针移动 | js_heap_peak_mb | 5.387 | 5.325 | -1.2% | 通过 |
| 玻璃卡片指针移动 | dom_nodes | 186 | 186 | 0.0% | 通过 |
| 玻璃卡片指针移动 | renderer_mutations | 667 | 531 | -20.4% | 通过 |
| Chat 100 条消息滚动 | average_fps | 91.284 | 107.233 | 17.5% | 通过 |
| Chat 100 条消息滚动 | p95_frame_ms | 16.6 | 12.7 | -23.5% | 通过 |
| Chat 100 条消息滚动 | longest_frame_ms | 54.1 | 37.5 | -30.7% | 通过 |
| Chat 100 条消息滚动 | dropped_frames | 4 | 2 | -50.0% | 通过 |
| Chat 100 条消息滚动 | long_tasks | 0 | 0 | n/a | 通过 |
| Chat 100 条消息滚动 | long_task_total_ms | 0 | 0 | n/a | 通过 |
| Chat 100 条消息滚动 | task_ms | 1,041.637 | 1,047.663 | 0.6% | 通过 |
| Chat 100 条消息滚动 | script_ms | 59.21 | 64.161 | 8.4% | 通过 |
| Chat 100 条消息滚动 | layout_ms | 25.291 | 25.411 | 0.5% | 通过 |
| Chat 100 条消息滚动 | recalc_style_ms | 94.279 | 96.712 | 2.6% | 通过 |
| Chat 100 条消息滚动 | js_heap_peak_mb | 11.715 | 11.713 | -0.0% | 通过 |
| Chat 100 条消息滚动 | dom_nodes | 3,098 | 3,098 | 0.0% | 通过 |
| Chat 100 条消息滚动 | renderer_mutations | 801 | 780 | -2.6% | 通过 |
| Chat 500 条消息滚动 | average_fps | 88.515 | 98.198 | 10.9% | 通过 |
| Chat 500 条消息滚动 | p95_frame_ms | 20.7 | 16.8 | -18.8% | 通过 |
| Chat 500 条消息滚动 | longest_frame_ms | 33.5 | 37.6 | 12.2% | 通过 |
| Chat 500 条消息滚动 | dropped_frames | 12 | 6 | -50.0% | 通过 |
| Chat 500 条消息滚动 | long_tasks | 0 | 0 | n/a | 通过 |
| Chat 500 条消息滚动 | long_task_total_ms | 0 | 0 | n/a | 通过 |
| Chat 500 条消息滚动 | task_ms | 1,485.903 | 1,438.23 | -3.2% | 通过 |
| Chat 500 条消息滚动 | script_ms | 61.794 | 63.407 | 2.6% | 通过 |
| Chat 500 条消息滚动 | layout_ms | 79.999 | 80.701 | 0.9% | 通过 |
| Chat 500 条消息滚动 | recalc_style_ms | 122.673 | 124.812 | 1.7% | 通过 |
| Chat 500 条消息滚动 | js_heap_peak_mb | 17.341 | 17.38 | 0.2% | 通过 |
| Chat 500 条消息滚动 | dom_nodes | 10,738 | 10,738 | 0.0% | 通过 |
| Chat 500 条消息滚动 | renderer_mutations | 1,237 | 1,293 | 4.5% | 通过 |
| Chat 1000 条消息滚动 | average_fps | 80.735 | 70.45 | -12.7% | 回退 |
| Chat 1000 条消息滚动 | p95_frame_ms | 20.8 | 20.9 | 0.5% | 通过 |
| Chat 1000 条消息滚动 | longest_frame_ms | 24.9 | 25.2 | 1.2% | 通过 |
| Chat 1000 条消息滚动 | dropped_frames | 12 | 30 | 150.0% | 回退 |
| Chat 1000 条消息滚动 | long_tasks | 0 | 0 | n/a | 通过 |
| Chat 1000 条消息滚动 | long_task_total_ms | 0 | 0 | n/a | 通过 |
| Chat 1000 条消息滚动 | task_ms | 2,173.338 | 1,596.261 | -26.6% | 通过 |
| Chat 1000 条消息滚动 | script_ms | 64.819 | 51.875 | -20.0% | 通过 |
| Chat 1000 条消息滚动 | layout_ms | 137.802 | 110.452 | -19.8% | 通过 |
| Chat 1000 条消息滚动 | recalc_style_ms | 140.843 | 123.519 | -12.3% | 通过 |
| Chat 1000 条消息滚动 | js_heap_peak_mb | 29.344 | 22.182 | -24.4% | 通过 |
| Chat 1000 条消息滚动 | dom_nodes | 20,288 | 20,288 | 0.0% | 通过 |
| Chat 1000 条消息滚动 | renderer_mutations | 1,304 | 1,005 | -22.9% | 通过 |
| Chat 1000 个流式增量 | average_fps | 93.722 | 123.19 | 31.4% | 通过 |
| Chat 1000 个流式增量 | p95_frame_ms | 16.6 | 12.6 | -24.1% | 通过 |
| Chat 1000 个流式增量 | longest_frame_ms | 33.5 | 33.5 | 0.0% | 通过 |
| Chat 1000 个流式增量 | dropped_frames | 2 | 2 | 0.0% | 通过 |
| Chat 1000 个流式增量 | long_tasks | 0 | 0 | n/a | 通过 |
| Chat 1000 个流式增量 | long_task_total_ms | 0 | 0 | n/a | 通过 |
| Chat 1000 个流式增量 | task_ms | 313.616 | 302.731 | -3.5% | 通过 |
| Chat 1000 个流式增量 | script_ms | 30.887 | 34.335 | 11.2% | 回退 |
| Chat 1000 个流式增量 | layout_ms | 2.599 | 3.106 | 19.5% | 回退 |
| Chat 1000 个流式增量 | recalc_style_ms | 32.119 | 31.843 | -0.9% | 通过 |
| Chat 1000 个流式增量 | js_heap_peak_mb | 11.922 | 11.934 | 0.1% | 通过 |
| Chat 1000 个流式增量 | dom_nodes | 1,608 | 1,608 | 0.0% | 通过 |
| Chat 1000 个流式增量 | renderer_mutations | 212 | 213 | 0.5% | 通过 |
| Chat 1000 个流式增量 | stream_integrity | 1 | 1 | 0.0% | 通过 |
| Chat 5000 个流式增量 | average_fps | 85.103 | 107.99 | 26.9% | 通过 |
| Chat 5000 个流式增量 | p95_frame_ms | 24.9 | 12.7 | -49.0% | 通过 |
| Chat 5000 个流式增量 | longest_frame_ms | 91.8 | 66.7 | -27.3% | 通过 |
| Chat 5000 个流式增量 | dropped_frames | 7 | 5 | -28.6% | 通过 |
| Chat 5000 个流式增量 | long_tasks | 0 | 0 | n/a | 通过 |
| Chat 5000 个流式增量 | long_task_total_ms | 0 | 0 | n/a | 通过 |
| Chat 5000 个流式增量 | task_ms | 613.198 | 569.867 | -7.1% | 通过 |
| Chat 5000 个流式增量 | script_ms | 52.484 | 49.927 | -4.9% | 通过 |
| Chat 5000 个流式增量 | layout_ms | 6.328 | 6.591 | 4.2% | 通过 |
| Chat 5000 个流式增量 | recalc_style_ms | 47.206 | 47.962 | 1.6% | 通过 |
| Chat 5000 个流式增量 | js_heap_peak_mb | 14.665 | 14.49 | -1.2% | 通过 |
| Chat 5000 个流式增量 | dom_nodes | 1,608 | 1,608 | 0.0% | 通过 |
| Chat 5000 个流式增量 | renderer_mutations | 358 | 354 | -1.1% | 通过 |
| Chat 5000 个流式增量 | stream_integrity | 1 | 1 | 0.0% | 通过 |
| Chat 常用交互 | average_fps | 65.391 | 73.323 | 12.1% | 通过 |
| Chat 常用交互 | p95_frame_ms | 37.4 | 37.4 | 0.0% | 通过 |
| Chat 常用交互 | longest_frame_ms | 54.1 | 41.6 | -23.1% | 通过 |
| Chat 常用交互 | dropped_frames | 12 | 12 | 0.0% | 通过 |
| Chat 常用交互 | long_tasks | 0 | 0 | n/a | 通过 |
| Chat 常用交互 | long_task_total_ms | 0 | 0 | n/a | 通过 |
| Chat 常用交互 | task_ms | 630.559 | 583.924 | -7.4% | 通过 |
| Chat 常用交互 | script_ms | 31.941 | 32.686 | 2.3% | 通过 |
| Chat 常用交互 | layout_ms | 10.237 | 10.272 | 0.3% | 通过 |
| Chat 常用交互 | recalc_style_ms | 171.667 | 220.704 | 28.6% | 回退 |
| Chat 常用交互 | js_heap_peak_mb | 11.48 | 11.277 | -1.8% | 通过 |
| Chat 常用交互 | dom_nodes | 3,098 | 3,098 | 0.0% | 通过 |
| Chat 常用交互 | renderer_mutations | 187 | 175 | -6.4% | 通过 |

回退项: 7

