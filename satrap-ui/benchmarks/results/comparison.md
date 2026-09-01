# Satrap UI Benchmark 对比

- 当前结果: 2026-08-31T15:22:17.577Z
- 基线结果: 2026-08-31T14:34:33.705Z
- 浏览器: 151.0.7922.34
- 迭代: 3 次正式运行, 1 次预热

| 场景 | 指标 | 基线中位数 | 当前中位数 | 变化 | 判定 |
|---|---:|---:|---:|---:|---:|
| 仪表盘浅色静置 | average_fps | 221.899 | 200.284 | -9.7% | 通过 |
| 仪表盘浅色静置 | p95_frame_ms | 8.3 | 8.4 | 1.2% | 通过 |
| 仪表盘浅色静置 | longest_frame_ms | 16.4 | 20.7 | 26.2% | 回退 |
| 仪表盘浅色静置 | dropped_frames | 0 | 1 | n/a | 通过 |
| 仪表盘浅色静置 | long_tasks | 0 | 0 | n/a | 通过 |
| 仪表盘浅色静置 | long_task_total_ms | 0 | 0 | n/a | 通过 |
| 仪表盘浅色静置 | task_ms | 510.799 | 482.167 | -5.6% | 通过 |
| 仪表盘浅色静置 | script_ms | 31.711 | 30.222 | -4.7% | 通过 |
| 仪表盘浅色静置 | layout_ms | 0 | 0 | n/a | 通过 |
| 仪表盘浅色静置 | recalc_style_ms | 110.117 | 104.856 | -4.8% | 通过 |
| 仪表盘浅色静置 | js_heap_peak_mb | 5.258 | 5.236 | -0.4% | 通过 |
| 仪表盘浅色静置 | dom_nodes | 186 | 186 | 0.0% | 通过 |
| 仪表盘浅色静置 | renderer_mutations | 0 | 0 | n/a | 通过 |
| 仪表盘深色静置 | average_fps | 195.358 | 196.955 | 0.8% | 通过 |
| 仪表盘深色静置 | p95_frame_ms | 8.4 | 8.4 | 0.0% | 通过 |
| 仪表盘深色静置 | longest_frame_ms | 16.6 | 16.8 | 1.2% | 通过 |
| 仪表盘深色静置 | dropped_frames | 0 | 0 | n/a | 通过 |
| 仪表盘深色静置 | long_tasks | 0 | 0 | n/a | 通过 |
| 仪表盘深色静置 | long_task_total_ms | 0 | 0 | n/a | 通过 |
| 仪表盘深色静置 | task_ms | 476.173 | 455.29 | -4.4% | 通过 |
| 仪表盘深色静置 | script_ms | 31.71 | 29.438 | -7.2% | 通过 |
| 仪表盘深色静置 | layout_ms | 0 | 0 | n/a | 通过 |
| 仪表盘深色静置 | recalc_style_ms | 104.831 | 96.792 | -7.7% | 通过 |
| 仪表盘深色静置 | js_heap_peak_mb | 5.248 | 5.237 | -0.2% | 通过 |
| 仪表盘深色静置 | dom_nodes | 194 | 194 | 0.0% | 通过 |
| 仪表盘深色静置 | renderer_mutations | 0 | 0 | n/a | 通过 |
| 玻璃卡片指针移动 | average_fps | 106.197 | 179.744 | 69.3% | 通过 |
| 玻璃卡片指针移动 | p95_frame_ms | 12.6 | 8.5 | -32.5% | 通过 |
| 玻璃卡片指针移动 | longest_frame_ms | 45.8 | 29.2 | -36.2% | 通过 |
| 玻璃卡片指针移动 | dropped_frames | 1 | 1 | 0.0% | 通过 |
| 玻璃卡片指针移动 | long_tasks | 0 | 0 | n/a | 通过 |
| 玻璃卡片指针移动 | long_task_total_ms | 0 | 0 | n/a | 通过 |
| 玻璃卡片指针移动 | task_ms | 288.237 | 223.395 | -22.5% | 通过 |
| 玻璃卡片指针移动 | script_ms | 20.38 | 15.006 | -26.4% | 通过 |
| 玻璃卡片指针移动 | layout_ms | 0 | 0 | n/a | 通过 |
| 玻璃卡片指针移动 | recalc_style_ms | 94.688 | 66.186 | -30.1% | 通过 |
| 玻璃卡片指针移动 | js_heap_peak_mb | 5.385 | 5.329 | -1.0% | 通过 |
| 玻璃卡片指针移动 | dom_nodes | 186 | 186 | 0.0% | 通过 |
| 玻璃卡片指针移动 | renderer_mutations | 751 | 241 | -67.9% | 通过 |
| Chat 100 条消息滚动 | average_fps | 70.603 | 93.397 | 32.3% | 通过 |
| Chat 100 条消息滚动 | p95_frame_ms | 20.8 | 20.6 | -1.0% | 通过 |
| Chat 100 条消息滚动 | longest_frame_ms | 54.2 | 41.5 | -23.4% | 通过 |
| Chat 100 条消息滚动 | dropped_frames | 11 | 11 | 0.0% | 通过 |
| Chat 100 条消息滚动 | long_tasks | 0 | 0 | n/a | 通过 |
| Chat 100 条消息滚动 | long_task_total_ms | 0 | 0 | n/a | 通过 |
| Chat 100 条消息滚动 | task_ms | 868.287 | 988.386 | 13.8% | 回退 |
| Chat 100 条消息滚动 | script_ms | 59.06 | 53.789 | -8.9% | 通过 |
| Chat 100 条消息滚动 | layout_ms | 24.578 | 26.102 | 6.2% | 通过 |
| Chat 100 条消息滚动 | recalc_style_ms | 77.752 | 46.838 | -39.8% | 通过 |
| Chat 100 条消息滚动 | js_heap_peak_mb | 11.729 | 11.694 | -0.3% | 通过 |
| Chat 100 条消息滚动 | dom_nodes | 3,098 | 3,208 | 3.6% | 通过 |
| Chat 100 条消息滚动 | renderer_mutations | 801 | 0 | -100.0% | 通过 |
| Chat 500 条消息滚动 | average_fps | 52.47 | 83.137 | 58.4% | 通过 |
| Chat 500 条消息滚动 | p95_frame_ms | 25.1 | 20.9 | -16.7% | 通过 |
| Chat 500 条消息滚动 | longest_frame_ms | 75.1 | 37.6 | -49.9% | 通过 |
| Chat 500 条消息滚动 | dropped_frames | 6 | 20 | 233.3% | 回退 |
| Chat 500 条消息滚动 | long_tasks | 0 | 0 | n/a | 通过 |
| Chat 500 条消息滚动 | long_task_total_ms | 0 | 0 | n/a | 通过 |
| Chat 500 条消息滚动 | task_ms | 897.213 | 1,302.269 | 45.1% | 回退 |
| Chat 500 条消息滚动 | script_ms | 43.93 | 52.652 | 19.9% | 回退 |
| Chat 500 条消息滚动 | layout_ms | 56.647 | 86.867 | 53.3% | 回退 |
| Chat 500 条消息滚动 | recalc_style_ms | 58.36 | 66.903 | 14.6% | 回退 |
| Chat 500 条消息滚动 | js_heap_peak_mb | 16.717 | 16.831 | 0.7% | 通过 |
| Chat 500 条消息滚动 | dom_nodes | 10,738 | 11,288 | 5.1% | 通过 |
| Chat 500 条消息滚动 | renderer_mutations | 955 | 0 | -100.0% | 通过 |
| Chat 1000 条消息滚动 | average_fps | 45.564 | 71.45 | 56.8% | 通过 |
| Chat 1000 条消息滚动 | p95_frame_ms | 29.3 | 25 | -14.7% | 通过 |
| Chat 1000 条消息滚动 | longest_frame_ms | 70.8 | 29.2 | -58.8% | 通过 |
| Chat 1000 条消息滚动 | dropped_frames | 6 | 29 | 383.3% | 回退 |
| Chat 1000 条消息滚动 | long_tasks | 0 | 0 | n/a | 通过 |
| Chat 1000 条消息滚动 | long_task_total_ms | 0 | 0 | n/a | 通过 |
| Chat 1000 条消息滚动 | task_ms | 983.002 | 1,572.739 | 60.0% | 回退 |
| Chat 1000 条消息滚动 | script_ms | 37.925 | 48.326 | 27.4% | 回退 |
| Chat 1000 条消息滚动 | layout_ms | 92.942 | 117.303 | 26.2% | 回退 |
| Chat 1000 条消息滚动 | recalc_style_ms | 53.026 | 71.294 | 34.5% | 回退 |
| Chat 1000 条消息滚动 | js_heap_peak_mb | 21.015 | 22.727 | 8.1% | 通过 |
| Chat 1000 条消息滚动 | dom_nodes | 20,288 | 21,388 | 5.4% | 通过 |
| Chat 1000 条消息滚动 | renderer_mutations | 545 | 0 | -100.0% | 通过 |
| Chat 1000 个流式增量 | average_fps | 54.201 | 115.914 | 113.9% | 通过 |
| Chat 1000 个流式增量 | p95_frame_ms | 25 | 12.6 | -49.6% | 通过 |
| Chat 1000 个流式增量 | longest_frame_ms | 70.7 | 29.1 | -58.8% | 通过 |
| Chat 1000 个流式增量 | dropped_frames | 2 | 2 | 0.0% | 通过 |
| Chat 1000 个流式增量 | long_tasks | 0 | 0 | n/a | 通过 |
| Chat 1000 个流式增量 | long_task_total_ms | 0 | 0 | n/a | 通过 |
| Chat 1000 个流式增量 | task_ms | 256.466 | 261.008 | 1.8% | 通过 |
| Chat 1000 个流式增量 | script_ms | 31.869 | 29.532 | -7.3% | 通过 |
| Chat 1000 个流式增量 | layout_ms | 2.556 | 2.62 | 2.5% | 通过 |
| Chat 1000 个流式增量 | recalc_style_ms | 20.916 | 21.464 | 2.6% | 通过 |
| Chat 1000 个流式增量 | js_heap_peak_mb | 12.475 | 13.349 | 7.0% | 通过 |
| Chat 1000 个流式增量 | dom_nodes | 1,608 | 1,632 | 1.5% | 通过 |
| Chat 1000 个流式增量 | renderer_mutations | 177 | 29 | -83.6% | 通过 |
| Chat 1000 个流式增量 | stream_integrity | 1 | 1 | 0.0% | 通过 |
| Chat 5000 个流式增量 | average_fps | 91.416 | 102.754 | 12.4% | 通过 |
| Chat 5000 个流式增量 | p95_frame_ms | 16.8 | 16.7 | -0.6% | 通过 |
| Chat 5000 个流式增量 | longest_frame_ms | 104.1 | 58.5 | -43.8% | 通过 |
| Chat 5000 个流式增量 | dropped_frames | 6 | 5 | -16.7% | 通过 |
| Chat 5000 个流式增量 | long_tasks | 0 | 0 | n/a | 通过 |
| Chat 5000 个流式增量 | long_task_total_ms | 0 | 0 | n/a | 通过 |
| Chat 5000 个流式增量 | task_ms | 557.152 | 499.965 | -10.3% | 通过 |
| Chat 5000 个流式增量 | script_ms | 57.364 | 47.756 | -16.7% | 通过 |
| Chat 5000 个流式增量 | layout_ms | 5.86 | 6.1 | 4.1% | 通过 |
| Chat 5000 个流式增量 | recalc_style_ms | 43.985 | 34.252 | -22.1% | 通过 |
| Chat 5000 个流式增量 | js_heap_peak_mb | 14.629 | 14.074 | -3.8% | 通过 |
| Chat 5000 个流式增量 | dom_nodes | 1,608 | 1,632 | 1.5% | 通过 |
| Chat 5000 个流式增量 | renderer_mutations | 375 | 62 | -83.5% | 通过 |
| Chat 5000 个流式增量 | stream_integrity | 1 | 1 | 0.0% | 通过 |
| Chat 常用交互 | average_fps | 92.661 | 84.897 | -8.4% | 通过 |
| Chat 常用交互 | p95_frame_ms | 29.2 | 29 | -0.7% | 通过 |
| Chat 常用交互 | longest_frame_ms | 41.7 | 45.8 | 9.8% | 通过 |
| Chat 常用交互 | dropped_frames | 11 | 8 | -27.3% | 通过 |
| Chat 常用交互 | long_tasks | 0 | 0 | n/a | 通过 |
| Chat 常用交互 | long_task_total_ms | 0 | 0 | n/a | 通过 |
| Chat 常用交互 | task_ms | 547.243 | 483.375 | -11.7% | 通过 |
| Chat 常用交互 | script_ms | 30.372 | 28.776 | -5.3% | 通过 |
| Chat 常用交互 | layout_ms | 11.379 | 9.183 | -19.3% | 通过 |
| Chat 常用交互 | recalc_style_ms | 170.112 | 124.195 | -27.0% | 通过 |
| Chat 常用交互 | js_heap_peak_mb | 11.023 | 11.285 | 2.4% | 通过 |
| Chat 常用交互 | dom_nodes | 3,098 | 3,208 | 3.6% | 通过 |
| Chat 常用交互 | renderer_mutations | 176 | 72 | -59.1% | 通过 |

回退项: 12

