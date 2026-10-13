# 平台状态存储恢复

手动唤醒存储和请求审批账本降级后, 先恢复日志中指出的原始主文件或归档文件, 再通过运行中的后端执行校验:

```text
satrap platform recover-store manual-wake
satrap platform recover-store request-ledger
```

命令沿用 CLI 的后端地址和管理令牌配置. 两个存储由后端共享, 不按单个平台实例恢复.

管理 API 为 `POST /api/platforms/recover-store`, 正文分别是 `{"store":"manual_wake"}` 或 `{"store":"request_ledger"}`. 沿用后端管理 API 鉴权, 不接受文件路径或清空选项.

- 200: 文件校验通过并解除降级, 或存储本来正常
- 400: 存储名称或请求字段无效
- 409: 原文件未恢复, 记录冲突或恢复写入失败, 保持不可用
- 503: 后端尚未装配该存储

恢复保留申请身份和防重复记录. 请求账本中中断的执行标记为结果未知, 不重新处理平台申请. 正常存储上的重复恢复请求不改写文件或当前执行状态.
