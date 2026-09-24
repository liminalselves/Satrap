# Issue #10 整改后最终复核

日期: 2026-09-24

范围: `a4195ad` 相对 [修复后独立复审](issue-10-goal-audit-recheck-2026-09-24.md) 所列 B1-B10 的实现与回归。本文不替代真实 ASR、LLM、SnowLuma 或发行环境验收。

## 结论

**B1-B10 的代码验收通过, 但仓库全量单测门禁目前不通过。**

十项原始反例均已有对应的定向单测或浏览器端到端测试, 本轮独立重跑全部通过。实现也与约定设计一致: 手动唤醒与审批账本使用清单和持久降级, 审批占用在文件锁内完成落盘, 旧段记录兼容默认值, ASR 扫描只把 `user_version=0` 的缺表当作旧库, 前端已迁为数据路由并拦截站内离开。

不过完整 `tests/unit` 的当前实测为 `2081 passed, 7 skipped, 2 failed`。失败项是 `tests/unit/test_document_upload.py::test_real_http_upload_limits_scope_and_search` 的 `control` 和 `chat` 参数组, 均在 RAG 文档上传 HTTP 请求读取响应时超过 15 秒。单独复现与 cProfile 显示, 40 万字符的无分隔中文文本在 `TextSplitter._merge_splits` 里反复执行列表头部 `pop(0)`, 约 23.0 秒总耗时中 22.8 秒花在文本切分; 该循环形成二次复杂度。它是已有 `text_utils.py` 的性能缺陷, 不在 B1-B10 差异中; 差异里的 `control_server.py` 仅增加 ASR 引用扫描错误的 503 映射, 不处于 RAG 路由。故将它登记为**独立阻断项**, 不能据此称本次提交全绿。

## 本轮实测

| 检查 | 结果 |
| --- | --- |
| B1/B2/B3/B4/B5/B6/B10 定向后端 | `160 passed` |
| 前端单测 | `18 files, 94 passed` |
| 前端类型检查 | 通过 |
| 前端 lint | 通过 |
| 前端生产构建 | 通过 |
| 平台配置 E2E | PASS |
| 手动唤醒 E2E | PASS |
| 聊天重连 E2E | PASS |
| Pyright | `0 errors, 1532 warnings` |
| 后端全量单测 | `2081 passed, 7 skipped, 2 failed` |
| `git diff --check` | 通过 |

跳过项为已有的显式集成开关、缺少 `reportlab` 和 Windows 符号链接权限条件。

## 十项复核

| 项目 | 复核结论 | 依据 |
| --- | --- | --- |
| B1 转发来源证明 | 通过 | 来源消息、账号、群和顶层 `forward_id` 均需核验; 非法来源不调用 `get_forward_msg` |
| B2 手动唤醒损坏恢复 | 通过 | 清单持久化 `initialized`、`expected_files`、`degraded`; 损坏后跨重启保持降级 |
| B3 副作用裁决 | 通过 | 段状态及 `purpose` 落盘; 已提交但未确认的取消归为 `unknown` |
| B4 审批防重放 | 通过 | 占用落盘后才允许网络动作; 账本不因缓存淘汰、重连或清空唤醒而消失 |
| B5 ASR 引用扫描 | 通过 | 插件元数据和数据库异常 fail-closed; 合法旧库缺表例外受 `user_version=0` 限定 |
| B6 `talk_value=0` | 通过 | 在最长等待判断前关闭自动参与; @、手动和 necessity 保持既定语义 |
| B7 文件兼容回落 | 通过 | 未验证的文件回落为 `unknown/file_delivery_unconfirmed`, 不显示已送达 |
| B8 编辑草稿 | 通过 | 不完整和重复行保留在草稿中并阻止保存, 不再输入时删行 |
| B9 脏数据离开保护 | 通过 | 数据路由 `useBlocker` 覆盖浏览器后退和站内导航 |
| B10 配置与诊断 | 通过 | 平台级预算与 `wake_talk_value` 可配置且热更新; 普通、手动请求均可查询有界脱敏诊断 |

## 仍需处理

1. 将 `TextSplitter._merge_splits` 的队首移除改为 `collections.deque.popleft()` 或索引游标, 补连续无分隔长文本性能回归用例, 再重跑后端全量单测
2. 若要关闭原始完整目标, 仍需按原验收范围补做真实 ASR/LLM/SnowLuma 及发行环境验收; 本轮没有把历史记录冒充为重新实测
3. 已知聊天页在后端返回缺少 `runs` 的非契约响应时会出现路由错误页, 不属于 B1-B10, 但应在后续提高接口容错性
