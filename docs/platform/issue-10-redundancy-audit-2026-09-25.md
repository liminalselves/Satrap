# Issue #10 冗余清理实施审计

日期: 2026-09-25

审计范围: `b30c7c2..f3875a2`, 对照 [冗余清理方案](../archive/issue-10/issue-10-redundancy-plan.md) 与实施前四项裁定。审计开始时工作区干净; 本次不修改业务代码

结论: 四批次主体已实施, 但暂不判定全部验收通过。发现三项 P2 问题, 其中两项为行为兼容回归, 一项为共享清单写回契约未兑现。现有回归测试通过不能替代以下反例

## 1. P2: 共用表单关闭原生校验, 导致其他页面必填约束失效

位置: `satrap-ui/src/components/common/FormModal.tsx:176`; 消费方示例 `satrap-ui/src/pages/Models/index.tsx:168`

批次 3 为策略表单统一错误提示, 在共用 FormModal 的 form 上无条件增加 noValidate。handleSubmit 只执行 preventDefault 与 onSubmit, 没有补充必填检查。模型配置页的配置名称依赖 required, 提交函数也没有检查空名称, 因此这项修改影响了策略表单以外的页面

浏览器复现 (真实 Models 页面, API 全部使用受控响应):

1. 打开 `/models`, 点击新增配置
2. 清空配置名称, 此时输入元素的 `validity.valueMissing` 为 true
3. 点击创建, 实际发出 `POST /config/models/llm/`, 名称为空

修复建议: 恢复共用表单的默认原生校验; 若策略表单确需关闭, 通过显式选项限定到该表单, 并补齐该表单自己的必填校验。也可统一实现完整的必填与控件有效性校验后再替换原生校验, 但不要扩大本次重构范围

验收: 模型配置名称为空时不发请求; 策略边界值仍由预期校验器提示, 非法草稿保留

## 2. P2: 账本清单的额外键与未解释字段未按旧语义保留

位置: `satrap/core/storage/durability.py:72`, `:171`; `tests/unit/test_durability.py:325`

账本声明 keep_extra_keys=True 与 normalize_degraded_at=False, 但 payload() 总是重建 expected_files/degraded, 仅保留顶层未知键。读取时 degraded.at 被替换为 0.0, 写回还会丢弃 degraded 内其他字段。旧账本清单读取返回原字典, 不进行这些改写

最小复现:

- 输入 expected_files 为 `{"entries":"present","future":"keep"}`, 校验后 payload() 只剩 entries
- 输入 degraded 为 `{"reason":"","at":"raw","note":"keep"}`, 校验后 payload() 变成 `{"reason":"","at":0.0}`
- 用带 future 的合法清单启动真实 RequestApprovalLedger, 再调用其降级标记写入入口, 落盘后的 expected_files 确认丢失 future; 旧实现的降级标记写入不替换 expected_files

当前 test_ledger_samples 甚至把 at 变成 0.0 写成预期结果, 因而无法作为“旧格式写回等价”的证据。该问题不等于审批记录丢失或重复审批, 但违反了本轮明确要求的历史清单信息保留契约

修复建议: 对账本保留原始嵌套载荷, 只覆盖本次确实更新的声明字段; 不解释 at 时保持其原值。主动写入新降级标记时可以按旧行为替换标记。MWS 继续使用自己的归一化丢弃规则, 不放宽其校验

验收: 增加旧清单完整写回样例, 同时覆盖顶层、expected_files 与 degraded 的额外键; 验证账本真实降级写入保留 expected_files 扩展键, 并纠正现有测试中的错误期望

## 3. P2: 前后端文本长度单位不同, 新前端校验误拒合法 ASR 名称

位置: `satrap-ui/src/utils/wakePolicyContract.ts:243` (列表元素长度检查也采用同样方式)

后端 len(str) 按 Unicode 码点计算, 前端 string.length 按 UTF-16 码元计算。共享 max_length=128 不足以保证同口径, 包含辅助平面汉字或 emoji 的名称可能在后端合法、前端非法

最小复现: `asr_model = "😀" × 65`

- 后端真实 validate_wake_policy 接受, 长度为 65
- 前端真实 validatePolicySettings 拒绝, 因 string.length 为 130, 报超过 128 字符

影响: 已有合法配置名可能导致平台表单保存或试算被新增校验拦截。此例用于证明长度口径, 不依赖调用 ASR 服务

修复建议: 前端长度判断使用 Unicode 码点数, 如 Array.from(text).length, 保持现有后端语义; 将同样口径用于受 max_length 约束的列表项

验收: 共享样例增加 BMP 与非 BMP 字符混合、128 码点合法及 129 码点非法的边界; 两侧真实入口结论一致

## 4. 已核实落地项

- 独立 docs 提交及四批次提交均存在, 方案已归档, 旧拒绝路由和兼容方法保留
- 多值 stage 按请求筛选且保留完整阶段, 手动请求聚焦时不叠加拒绝预设, 两条相关页面回归通过
- 冷却保持有限非负, 最长等待使用排他上界 120; 事件容量四字段未进入策略表
- 缺失覆盖字段不注入默认值, 阈值优先级逻辑未被静态默认值迁移替换
- 契约和共享样例均为 `i/lf w/lf attr/text eol=lf`, 同步脚本使用原始字节比较并通过
- 持久化组件没有统一两侧读取模型或接管启动/恢复决策树; 既有降级、隔离、恢复用例通过
- 持久化三文件合计净增 359 行 (含注释), 属实现集中而非减行。方案已取消减行数验收要求, 本项仅作维护成本提示, 不作为额外阻断项

## 5. 独立验证

- 针对性后端测试: 234 passed
- 前端 Vitest: 20 files / 190 tests passed
- Playwright: manual-wake、platform-policy 均 PASS, 使用受控 API 响应
- TypeScript: `tsc --noEmit` 通过; ESLint 通过
- 契约同步 `--check`、两 JSON 文件 LF 属性及提交差异 whitespace 检查通过
- 三项问题均有最小复现, 模型空名称问题通过真实浏览器页面确认, 未调用外部模型或真实平台
- 全量后端 `python -m pytest tests/unit -q --tb=short`: 2207 passed / 7 skipped, 188.68 秒。跳过项为默认不运行的集成测试、缺少 reportlab 和 Windows 符号链接权限, 未排除上传测试
- Pyright `python -m pyright -p .pyrightcfg`: 0 errors / 1532 warnings, 数量与记录的门禁基线一致。首次未指定项目配置的运行误扫 build/lib, 不作为本轮门禁结论

未重新执行历史真实 ASR/LLM/SnowLuma 服务验收和发行静态资源部署验收; 本报告只审计本次冗余清理差异, 不将受控 API 页面回归当作上述验收证据
