# 信息加载 benchmark 前后对比

修改前提交: b295cc24e2f393f897880399e99a53818cc2ff91
修改后基于提交: b295cc24e2f393f897880399e99a53818cc2ff91

| 页面 / 额外等待 / 轮次 | 修改前 P50 ms | 修改后 P50 ms | 变化 | 修改前请求 | 修改后请求 |
| --- | ---: | ---: | ---: | ---: | ---: |
| / / 0ms / 50 | 241.5 | 243.1 | 0.7% | 20 | 6 |
| /chat / 0ms / 100 历史 | 630.3 | 722.5 | 14.6% | 19 | 16 |
| /chat / 0ms / 1000 历史 | 7526.2 | 696.6 | -90.7% | 19 | 16 |
| /chat / 0ms / 10 历史 | 660.1 | 636.0 | -3.7% | 21 | 18 |

| 页面 / 等待 / 轮次 | 修改前 P95 ms | 修改后 P95 ms | 修改前字节 | 修改后字节 | 修改前 DOM | 修改后 DOM |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| / / 0ms / 50 | 254.7 | 287.0 | 1081 | 255 | 202 | 207 |
| /chat / 0ms / 100 | 670.8 | 814.8 | 53657 | 30866 | 5243 | 3050 |
| /chat / 0ms / 1000 | 14260.8 | 789.5 | 433103 | 31139 | 41603 | 3050 |
| /chat / 0ms / 10 | 837.8 | 693.2 | 16299 | 14268 | 1647 | 1473 |

```json
{
  "scenario": {
    "route": "/",
    "delay": 0,
    "turns": 50
  },
  "before": {
    "duration_ms": {
      "median": 1265.723,
      "p95": 1275.711,
      "worst": 1275.711
    },
    "average_fps": {
      "median": 142.959,
      "p95": 172.331,
      "worst": 172.331
    },
    "p95_frame_ms": {
      "median": 10.9,
      "p95": 12,
      "worst": 12
    },
    "longest_frame_ms": {
      "median": 16.7,
      "p95": 23.5,
      "worst": 23.5
    },
    "dropped_frames": {
      "median": 0,
      "p95": 1,
      "worst": 1
    },
    "frame_samples": {
      "median": 180,
      "p95": 215,
      "worst": 215
    },
    "long_tasks": {
      "median": 0,
      "p95": 0,
      "worst": 0
    },
    "long_task_total_ms": {
      "median": 0,
      "p95": 0,
      "worst": 0
    },
    "renderer_mutations": {
      "median": 0,
      "p95": 0,
      "worst": 0
    },
    "dom_nodes": {
      "median": 202,
      "p95": 202,
      "worst": 202
    },
    "js_heap_start_mb": {
      "median": 5.494,
      "p95": 5.547,
      "worst": 5.547
    },
    "js_heap_peak_mb": {
      "median": 5.507,
      "p95": 5.561,
      "worst": 5.561
    },
    "js_heap_end_mb": {
      "median": 5.507,
      "p95": 5.561,
      "worst": 5.561
    },
    "task_ms": {
      "median": 104.964,
      "p95": 131.816,
      "worst": 131.816
    },
    "script_ms": {
      "median": 7.328,
      "p95": 8.948,
      "worst": 8.948
    },
    "layout_ms": {
      "median": 0,
      "p95": 0,
      "worst": 0
    },
    "recalc_style_ms": {
      "median": 21.179,
      "p95": 27.676,
      "worst": 27.676
    }
  },
  "after": {
    "duration_ms": {
      "median": 1268.211,
      "p95": 1274.164,
      "worst": 1274.164
    },
    "average_fps": {
      "median": 162.602,
      "p95": 182.562,
      "worst": 182.562
    },
    "p95_frame_ms": {
      "median": 10.1,
      "p95": 11.4,
      "worst": 11.4
    },
    "longest_frame_ms": {
      "median": 16.7,
      "p95": 21.5,
      "worst": 21.5
    },
    "dropped_frames": {
      "median": 0,
      "p95": 1,
      "worst": 1
    },
    "frame_samples": {
      "median": 206,
      "p95": 232,
      "worst": 232
    },
    "long_tasks": {
      "median": 0,
      "p95": 0,
      "worst": 0
    },
    "long_task_total_ms": {
      "median": 0,
      "p95": 0,
      "worst": 0
    },
    "renderer_mutations": {
      "median": 0,
      "p95": 0,
      "worst": 0
    },
    "dom_nodes": {
      "median": 207,
      "p95": 207,
      "worst": 207
    },
    "js_heap_start_mb": {
      "median": 4.759,
      "p95": 5.141,
      "worst": 5.141
    },
    "js_heap_peak_mb": {
      "median": 4.773,
      "p95": 5.141,
      "worst": 5.141
    },
    "js_heap_end_mb": {
      "median": 4.772,
      "p95": 4.785,
      "worst": 4.785
    },
    "task_ms": {
      "median": 131.435,
      "p95": 148.432,
      "worst": 148.432
    },
    "script_ms": {
      "median": 7.866,
      "p95": 9.248,
      "worst": 9.248
    },
    "layout_ms": {
      "median": 0,
      "p95": 0,
      "worst": 0
    },
    "recalc_style_ms": {
      "median": 27.91,
      "p95": 33.02,
      "worst": 33.02
    }
  }
}
```


```json
{
  "scenario": {
    "route": "/chat",
    "delay": 0,
    "turns": 100,
    "history": true
  },
  "before": {
    "duration_ms": {
      "median": 1870.076,
      "p95": 1952.811,
      "worst": 1952.811
    },
    "average_fps": {
      "median": 77.848,
      "p95": 80.335,
      "worst": 80.335
    },
    "p95_frame_ms": {
      "median": 21.8,
      "p95": 23.4,
      "worst": 23.4
    },
    "longest_frame_ms": {
      "median": 41.4,
      "p95": 54.7,
      "worst": 54.7
    },
    "dropped_frames": {
      "median": 14,
      "p95": 16,
      "worst": 16
    },
    "frame_samples": {
      "median": 144,
      "p95": 146,
      "worst": 146
    },
    "long_tasks": {
      "median": 0,
      "p95": 0,
      "worst": 0
    },
    "long_task_total_ms": {
      "median": 0,
      "p95": 0,
      "worst": 0
    },
    "renderer_mutations": {
      "median": 117,
      "p95": 129,
      "worst": 129
    },
    "dom_nodes": {
      "median": 5243,
      "p95": 5243,
      "worst": 5243
    },
    "js_heap_start_mb": {
      "median": 9.674,
      "p95": 9.984,
      "worst": 9.984
    },
    "js_heap_peak_mb": {
      "median": 13.87,
      "p95": 14.2,
      "worst": 14.2
    },
    "js_heap_end_mb": {
      "median": 13.87,
      "p95": 14.2,
      "worst": 14.2
    },
    "task_ms": {
      "median": 1101.552,
      "p95": 1256.547,
      "worst": 1256.547
    },
    "script_ms": {
      "median": 91.933,
      "p95": 101.44,
      "worst": 101.44
    },
    "layout_ms": {
      "median": 42.056,
      "p95": 50.898,
      "worst": 50.898
    },
    "recalc_style_ms": {
      "median": 65.736,
      "p95": 72.988,
      "worst": 72.988
    }
  },
  "after": {
    "duration_ms": {
      "median": 2049.71,
      "p95": 2339.363,
      "worst": 2339.363
    },
    "average_fps": {
      "median": 69.139,
      "p95": 82.137,
      "worst": 82.137
    },
    "p95_frame_ms": {
      "median": 21.8,
      "p95": 25,
      "worst": 25
    },
    "longest_frame_ms": {
      "median": 26.2,
      "p95": 31.2,
      "worst": 31.2
    },
    "dropped_frames": {
      "median": 15,
      "p95": 24,
      "worst": 24
    },
    "frame_samples": {
      "median": 146,
      "p95": 151,
      "worst": 151
    },
    "long_tasks": {
      "median": 0,
      "p95": 0,
      "worst": 0
    },
    "long_task_total_ms": {
      "median": 0,
      "p95": 0,
      "worst": 0
    },
    "renderer_mutations": {
      "median": 260,
      "p95": 276,
      "worst": 276
    },
    "dom_nodes": {
      "median": 3050,
      "p95": 3050,
      "worst": 3050
    },
    "js_heap_start_mb": {
      "median": 12.776,
      "p95": 12.882,
      "worst": 12.882
    },
    "js_heap_peak_mb": {
      "median": 12.889,
      "p95": 13.004,
      "worst": 13.004
    },
    "js_heap_end_mb": {
      "median": 9.66,
      "p95": 12.33,
      "worst": 12.33
    },
    "task_ms": {
      "median": 1357.397,
      "p95": 1524.527,
      "worst": 1524.527
    },
    "script_ms": {
      "median": 102.885,
      "p95": 121.544,
      "worst": 121.544
    },
    "layout_ms": {
      "median": 35.493,
      "p95": 42.952,
      "worst": 42.952
    },
    "recalc_style_ms": {
      "median": 115.167,
      "p95": 130.646,
      "worst": 130.646
    }
  }
}
```


```json
{
  "scenario": {
    "route": "/chat",
    "delay": 0,
    "turns": 1000,
    "history": true
  },
  "before": {
    "duration_ms": {
      "median": 6463.765,
      "p95": 7321.08,
      "worst": 7321.08
    },
    "average_fps": {
      "median": 19.096,
      "p95": 24.647,
      "worst": 24.647
    },
    "p95_frame_ms": {
      "median": 67.2,
      "p95": 79.7,
      "worst": 79.7
    },
    "longest_frame_ms": {
      "median": 86.6,
      "p95": 120.9,
      "worst": 120.9
    },
    "dropped_frames": {
      "median": 1,
      "p95": 2,
      "worst": 2
    },
    "frame_samples": {
      "median": 129,
      "p95": 138,
      "worst": 138
    },
    "long_tasks": {
      "median": 65,
      "p95": 94,
      "worst": 94
    },
    "long_task_total_ms": {
      "median": 3619,
      "p95": 5582,
      "worst": 5582
    },
    "renderer_mutations": {
      "median": 2012,
      "p95": 2020,
      "worst": 2020
    },
    "dom_nodes": {
      "median": 41603,
      "p95": 41603,
      "worst": 41603
    },
    "js_heap_start_mb": {
      "median": 50.324,
      "p95": 61.062,
      "worst": 61.062
    },
    "js_heap_peak_mb": {
      "median": 61.38,
      "p95": 62.623,
      "worst": 62.623
    },
    "js_heap_end_mb": {
      "median": 28.046,
      "p95": 54.035,
      "worst": 54.035
    },
    "task_ms": {
      "median": 6314.607,
      "p95": 7180.206,
      "worst": 7180.206
    },
    "script_ms": {
      "median": 843.143,
      "p95": 1004.074,
      "worst": 1004.074
    },
    "layout_ms": {
      "median": 682.551,
      "p95": 860.16,
      "worst": 860.16
    },
    "recalc_style_ms": {
      "median": 238.373,
      "p95": 256.737,
      "worst": 256.737
    }
  },
  "after": {
    "duration_ms": {
      "median": 1947.818,
      "p95": 2153.8,
      "worst": 2153.8
    },
    "average_fps": {
      "median": 73.965,
      "p95": 84.132,
      "worst": 84.132
    },
    "p95_frame_ms": {
      "median": 21,
      "p95": 25.5,
      "worst": 25.5
    },
    "longest_frame_ms": {
      "median": 25.5,
      "p95": 35.4,
      "worst": 35.4
    },
    "dropped_frames": {
      "median": 14,
      "p95": 20,
      "worst": 20
    },
    "frame_samples": {
      "median": 145,
      "p95": 151,
      "worst": 151
    },
    "long_tasks": {
      "median": 0,
      "p95": 0,
      "worst": 0
    },
    "long_task_total_ms": {
      "median": 0,
      "p95": 0,
      "worst": 0
    },
    "renderer_mutations": {
      "median": 248,
      "p95": 277,
      "worst": 277
    },
    "dom_nodes": {
      "median": 3050,
      "p95": 3050,
      "worst": 3050
    },
    "js_heap_start_mb": {
      "median": 12.811,
      "p95": 13.003,
      "worst": 13.003
    },
    "js_heap_peak_mb": {
      "median": 12.935,
      "p95": 13.003,
      "worst": 13.003
    },
    "js_heap_end_mb": {
      "median": 9.358,
      "p95": 12.298,
      "worst": 12.298
    },
    "task_ms": {
      "median": 1263.998,
      "p95": 1455.574,
      "worst": 1455.574
    },
    "script_ms": {
      "median": 92.767,
      "p95": 109.244,
      "worst": 109.244
    },
    "layout_ms": {
      "median": 30.93,
      "p95": 36.896,
      "worst": 36.896
    },
    "recalc_style_ms": {
      "median": 104.54,
      "p95": 128.946,
      "worst": 128.946
    }
  }
}
```


```json
{
  "scenario": {
    "route": "/chat",
    "delay": 0,
    "turns": 10,
    "history": true,
    "streaming": true
  },
  "before": {
    "duration_ms": {
      "median": 785.583,
      "p95": 1001.991,
      "worst": 1001.991
    },
    "average_fps": {
      "median": 84.034,
      "p95": 90.434,
      "worst": 90.434
    },
    "p95_frame_ms": {
      "median": 22.3,
      "p95": 28.8,
      "worst": 28.8
    },
    "longest_frame_ms": {
      "median": 87.2,
      "p95": 105.4,
      "worst": 105.4
    },
    "dropped_frames": {
      "median": 5,
      "p95": 7,
      "worst": 7
    },
    "frame_samples": {
      "median": 65,
      "p95": 84,
      "worst": 84
    },
    "long_tasks": {
      "median": 0,
      "p95": 0,
      "worst": 0
    },
    "long_task_total_ms": {
      "median": 0,
      "p95": 0,
      "worst": 0
    },
    "renderer_mutations": {
      "median": 48,
      "p95": 50,
      "worst": 50
    },
    "dom_nodes": {
      "median": 1647,
      "p95": 1647,
      "worst": 1647
    },
    "js_heap_start_mb": {
      "median": 9.677,
      "p95": 9.793,
      "worst": 9.793
    },
    "js_heap_peak_mb": {
      "median": 12.134,
      "p95": 13.209,
      "worst": 13.209
    },
    "js_heap_end_mb": {
      "median": 11.816,
      "p95": 11.856,
      "worst": 11.856
    },
    "task_ms": {
      "median": 484.598,
      "p95": 642.632,
      "worst": 642.632
    },
    "script_ms": {
      "median": 52.933,
      "p95": 74.158,
      "worst": 74.158
    },
    "layout_ms": {
      "median": 5.775,
      "p95": 8.008,
      "worst": 8.008
    },
    "recalc_style_ms": {
      "median": 42.467,
      "p95": 63.064,
      "worst": 63.064
    },
    "first_content_ms": {
      "median": 53.805,
      "p95": 70.236,
      "worst": 70.236
    },
    "stream_integrity": {
      "median": 1,
      "p95": 1,
      "worst": 1
    }
  },
  "after": {
    "duration_ms": {
      "median": 736.789,
      "p95": 805.657,
      "worst": 805.657
    },
    "average_fps": {
      "median": 85.679,
      "p95": 94.098,
      "worst": 94.098
    },
    "p95_frame_ms": {
      "median": 21.4,
      "p95": 31.2,
      "worst": 31.2
    },
    "longest_frame_ms": {
      "median": 70.1,
      "p95": 99.8,
      "worst": 99.8
    },
    "dropped_frames": {
      "median": 5,
      "p95": 7,
      "worst": 7
    },
    "frame_samples": {
      "median": 63,
      "p95": 72,
      "worst": 72
    },
    "long_tasks": {
      "median": 0,
      "p95": 0,
      "worst": 0
    },
    "long_task_total_ms": {
      "median": 0,
      "p95": 0,
      "worst": 0
    },
    "renderer_mutations": {
      "median": 48,
      "p95": 50,
      "worst": 50
    },
    "dom_nodes": {
      "median": 1473,
      "p95": 1473,
      "worst": 1473
    },
    "js_heap_start_mb": {
      "median": 10.981,
      "p95": 11.436,
      "worst": 11.436
    },
    "js_heap_peak_mb": {
      "median": 12.27,
      "p95": 13.153,
      "worst": 13.153
    },
    "js_heap_end_mb": {
      "median": 8.719,
      "p95": 8.777,
      "worst": 8.777
    },
    "task_ms": {
      "median": 412.267,
      "p95": 465.539,
      "worst": 465.539
    },
    "script_ms": {
      "median": 46.154,
      "p95": 56.377,
      "worst": 56.377
    },
    "layout_ms": {
      "median": 4.721,
      "p95": 6.099,
      "worst": 6.099
    },
    "recalc_style_ms": {
      "median": 38.035,
      "p95": 41.085,
      "worst": 41.085
    },
    "first_content_ms": {
      "median": 53.386,
      "p95": 61.452,
      "worst": 61.452
    },
    "stream_integrity": {
      "median": 1,
      "p95": 1,
      "worst": 1
    }
  }
}
```
