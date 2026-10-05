# main 合并验收记录

日期: 2026-10-05
状态: 合并实现与最终回归完成
行为约定: [合并计划与契约](main-merge-plan-contracts.md)

## 合并基线与交付

- 本地分支: `fix/issue1`, 原 HEAD `32440003787be5fba02ff0bc014e6be53fa1fbbe`
- 远端 main: `fb0d2001452808c766a295597337937dc34650f3`, 执行中再次查询 origin 未变化
- 共同祖先: `0638b90db2526700113e83f64faf731f94b9ef71`
- 恢复引用: `codex/pre-main-merge-20261005`, 固定在原本地 HEAD
- 在隔离 worktree 中以 `--no-commit --no-ff` 解析 9 个冲突, 复核其余自动合并文件; 合并提交保留双方完整历史
- 提交边界: 合并与必要的集成适配一批, 本计划和验收记录一批; 仅本地交付, 不推送
- 合并提交: `f5f0cdfa0c4c2efc2a5ee7e054758cabe8af6c3c`, 两个父提交分别为原本地 HEAD 和固定的远端 main

## 集成适配

| 范围 | 合并结果 |
| --- | --- |
| 媒体 | 保留 main 的真实路径校验和精确布局授权; 额外允许实际数据根下 `group-chat/stickers/<64 位小写十六进制摘要>` 文件, 相邻数据库和其他文件仍拒绝; 显式根目录覆盖默认规则 |
| OneBot | 保留空 token 的回环告警与非回环拒绝规则; 保留本地 Agent 路由, 群配置, 消息档案, 发送回执和提醒状态契约 |
| Coding | 保留 main 的实例配置, 路径保护, 审批等待后复核和环境剥敏; `base_take` 接入环境放行列表, 不恢复旧记忆实现 |
| 安装与扫描 | 保留 main 的模块来源加载和失败恢复; 同步/异步安装失败调用 cleanup, 恢复本地已有工具状态和首次技能激活回滚; cleanup 出错记录堆栈, 不覆盖安装错误 |
| 群授权 | 空 `allowed_callers` 拒绝模型写操作; `allowed_read_callers` 只影响 group_admin 的当前只读工具, 不扩散至 group_chat 或 friend_manager |
| 高危审批 | 插件要求与宿主策略取 OR, 统一进入既有持久动作队列; 不调用即时 user_input_provider, 不产生第二套申请; 权限摘要包含审批要求, 提交后撤权或改配拒绝执行 |
| 插件边界 | 保留当前群查询/成员/消息读取工具归属与自身昵称降级; 不恢复 group_admin 的旧工具, 不恢复 base_take 记忆, 三个业务插件不相互导入 |

新增 `test_main_merge_contracts.py` 验证默认表情发送和数据库拒绝, 显式根覆盖, 符号链接越界, 读写名单独立, 无审批服务时拒绝直接调用 SDK, 插件/宿主审批的四种组合及批准前撤权

## 自动化证据

| 检查 | 结果 |
| --- | --- |
| 后端最终默认全量 `python -m pytest -q --tb=short` | 3356 passed, 22 skipped, 0 failed; 557.55 秒 |
| 媒体与新组合契约定向复测 | 58 passed, 1 skipped |
| OneBot / 出站档案 / 请求收件箱定向复测 | 99 passed |
| 前端单元测试 | 27 文件, 234 项通过 |
| 前端 lint / TypeScript / 生产构建 | 全部通过 |
| 修改的生产 Python 模块 pyright | 0 errors, 0 warnings |
| Git 差异空白检查 | `git diff --check HEAD` 通过 |
| 浏览器: main-merge | 新授权字段保存与回填, 单条群审批, 仅执行一次批准, 390px 窄屏通过 |
| 浏览器: edictum-settings / agent-routing | Agent 编辑与平台私聊/群聊路由通过 |
| 浏览器: group-chat-media | 上传, 重试, 预览, 版本冲突, 集合归属, 删除和窄屏通过 |
| 浏览器: group-chat-memory-reminders | 记忆来源与审批, 草稿冲突, 提醒重试与时间转换, 取消/恢复, 账号隔离和窄屏通过 |

首轮后端全量为 3345 passed, 7 failed, 22 skipped. 失败均来自旧测试夹具: 3 项媒体用例使用平铺临时数据库而非真实平台缓存布局, 2 项文件发送用例使用未授权文件, 2 项加群处理用例遗漏显式写调用者名单. 已分别使用真实存储布局, 显式授权测试目录和明确调用者修正; 没有放宽生产白名单, 删除断言或新增跳过

最终 22 项跳过分别为 16 项需显式启用的集成测试, 4 项缺 Windows 符号链接权限, 1 项缺 reportlab, 1 项缺 `.toolkit` 测试图片. 最终全量日志位于运行机器的 `%TEMP%/satrap-main-merge-backend-final.log`; 最终回归启动后生产代码和测试文件未再修改

浏览器检查拦截测试 API, 后端平台调用使用替身; 这些结果不代表真实 QQ / Misskey 已加载合并版验收. 既有 C/D 现场结果仅是历史证据. 如需现场复核, 由用户重启后最小验证合法图片/表情发送和现有配置兼容

## 升级要求

- 旧 group_admin 配置即使开启写操作, `allowed_callers` 留空也会拒绝. 需要管理写操作的 Agent 必须由用户明确填写调用者; 不从群主, 管理员, command_operators 或 request_managers 自动授权
- 开启 `high_risk_approval` 后仍只使用群管理页面的现有持久审批; false 不能取消宿主要求的审批
- 使用非空 `media_allowed_roots` 时, 缓存和表情库也须包含在显式授权目录中; 不因目录存在而绕过授权
- 合并不修改当前 `.satrap` 配置或运行数据, 不重启服务; 工作区代码更新后由用户决定何时重启

main 带入的 [issue-12 安全审查](security-review-issue-12.md) 是历史审查记录, 其中的历史测试结果不替代本次合并结果
