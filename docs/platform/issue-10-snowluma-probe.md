# SnowLuma 实际网络模块通信验收

2026-09-21 在本机用户安装的 SnowLuma 1.14.17 上执行:

```powershell
python -m scripts.probe_snowluma --installation F:\other\SnowLuma-v1.14.17-win-x64
```

结果:

| 检查 | 结果 |
| --- | --- |
| 错误 access token | Satrap 返回 HTTP 403, 连接未建立 |
| 正确 token / Universal 反向 WebSocket | 完成 HTTP 101 握手 |
| 群消息入站 | 2 次实际网络上报, 正文和来源消息 ID 匹配 |
| Satrap 动作及 echo 回包关联 | 4 次 send_group_msg, 故意乱序的回包与原调用正确对应 |
| 断线重连 | SnowLuma 自身重连逻辑恢复连接, 恢复后再次完成双向通信 |
| 清理 | 子进程和 Satrap 临时监听退出, 临时模块自动删除 |

安装包 `index.mjs` SHA256:

```text
79732efb62c1e32aba90c51f76eebdf95cb563cdb7d8e2f686fdcdadd50f0981
```

探针从本机发行包的源模块标记中提取 event-filter、网络适配器基类、WebSocket 实现、网络 utils 和 WsClientAdapter, 在临时目录加载; WebSocket 原生扩展从安装包原目录加载。网络实现未替换为自制 WebSocket 客户端。日志接口使用静默替身, 断连错误类型提供本地等价错误类, QQ 侧 ctx 事件源与动作执行器为模拟实现。没有启动 QQ、登录账号或发送真实消息, 没有修改安装目录和用户配置, 没有将第三方发行包内容纳入 Satrap 仓库。

本结果验证实际 SnowLuma 网络层兼容性, 不等同于完整 SnowLuma 应用启动、真实 QQ、真实模型、ASR 或所有 OneBot 动作验收。脚本按该版本发行包标记提取, 后续发行版改变打包结构时需复核挂接点。WebUI 密码不用于 OneBot 鉴权; 本次使用随机临时 access token, 通过进程标准输入传递, 不保存到文档或命令行。

2026-09-21 接续实现 SendReceipt 后再次执行同一命令, 全部探针检查通过。发送断言现检查 success 状态与平台 message_ids, 验证新回执与实际网络回包关联兼容; 安装包摘要和 QQ 模拟边界不变。
