# Issue #10 目标审计复审 (2026-09-23)

复审对象: [issue-10-goal-audit-2026-09-23.md](issue-10-goal-audit-2026-09-23.md) (基线 `fix/issue10` @ `e683fce`, 工作树干净)。本次复审为只读核对, 不修改业务代码, 不执行真实 QQ 管理动作, 不调用付费模型。复审方式: 对审计每项主张在当前源码上逐条定位取证, 结论分为"属实""确认缺失""证据纪律声明""前提待裁定"。

总体结论: **审计报告 A1-A8 全部属实, "不通过完整目标验收"的判定准确**。证据不足项中 File 出站与 talk_value 两条建议升级为"确认缺失" (代码证据为确定性零命中); 历史验收记录全部真实存在; 发行包核验的事实层成立但验收前提需按项目实际分发形态裁定。

## 一、可复现缺陷复审 (A1-A4)

### A1 撤回/审批不核验对象群归属 — 属实 (最严重)

- `satrap/core/platform/onebot/admin.py:391` `recall_message`: 仅 `_check_group(normalize_group_id(group_id))` 校验传入群号, 随后直接 `delete_msg(message_id=int(text))`, 全程无 `get_msg` 回源确认消息实际所属群。
- `satrap/core/platform/onebot/admin.py:568` `handle_group_request`: 同样只验传入群号, flag/sub_type 直通审批动作。
- 工具层 `satrap/expend/plugins/group_admin/tools.py` 的 `_group_id` 只解析来源并查白名单, `_build_call` 将模型提供的 message_id/flag 原样透传。
- 结论: "限定群 A"不能阻止作用于群 B 的消息 ID / 审批 flag, 是真实的跨群越权面。

### A2 自动参与覆盖已补全内容 — 属实, 且比审计所述更宽

- `satrap/core/pipeline/scheduler.py:255` 在 `_session_turn` 锁内将 `user_call.message` 整体覆盖为 `PendingText` 纯文本拼接 (残留复核 B6 仅将该语句移入锁内, 未改覆盖语义)。
- 引用/转发上下文与付费获取的 ASR/文件正文全部丢失; 图片/视频因走独立字段幸免。
- 审计未明说的加重细节: `observe(event)` 发生在 `is_wake_up()` 判断之前, frequency/necessity 模式下**即使消息明确 @机器人**也会产生 pending, 显式唤醒消息同样被覆盖 — 不限于"自动参与"场景。

### A3 发送取消未等子任务退出就释放锁登记 — 属实

- `satrap/core/platform/onebot/outbound.py:155-163`: `task.cancel()` 后立即 `raise`, `finally` 同步将任务移出 `self.tasks` 并递减/移除目标锁。
- `cancel()` 只是取消请求, 子任务可能仍在异步 `finally` 中清理; 此时同目标新发送获得新锁并发执行, 而 `close()` (:175 起) 只 await `self.tasks` 现存任务, 永远等不到已被摘除的旧任务。

### A4 缺少统一输入总预算 — 属实

- `satrap/core/pipeline/input_projection.py:238-243`: 顶层正文与顶层图片/视频无上限直入输出。
- `_MediaBudget.__init__` 将 `remaining` 设为 4 但**不扣除顶层已有媒体**, 只约束引用/转发补入部分 (:220 附近)。
- `message_text_limit` 是出站分块参数 (`adapter.py:559` 发送路径), 与输入预算无关; `ATTACHMENT_TOTAL_TIMEOUT=90s` 是时间限制不是体量限制。
- 方案"全输入预算"无对应实现。

## 二、交付缺口复审 (A5-A8)

### A5 手动请求无持久化 — 属实

- `satrap/core/pipeline/manual_wake.py:34-40`: 纯内存 `OrderedDict` + `WeakKeyDictionary`, docstring 自述"有限的进程内幂等记录"。
- 方案 `issue-10-plan.md:94` 明确要求"使用现有状态存储留下最小记录, 重启后无法确定的结果标为 unknown, 不自动重放"。文档声明"进程内"不等于完成原要求。

### A6 ASR 配置删除无引用检查 — 属实

- `satrap/core/config/model_service.py` `delete` → `satrap/core/framework/BackGroundManager.py:589` `remove_asr_config` 直接 pop/重置, 不检查任何平台 `asr_model` 绑定。
- `control_server.py:1475` DELETE 分支直通 `service.delete`。
- 方案第 7 节 (:230-250 区域) 要求的引用检查不存在。

### A7 管理能力状态语义 — 属实

- `satrap/core/platform/onebot/adapter.py:509-517`: `status = "supported" if self._bot is not None else "unavailable"`, 对全部 `ADMIN_CAPABILITIES` (含 `get_record`/`fetch_ptt_text` 等 SnowLuma 扩展动作) 一视同仁。
- `MISSING_ACTION_RETCODES = frozenset({10002, 1404})` 已定义但未用于缓存 unsupported。
- 违反方案 :184 的 unknown/supported/unsupported/unavailable 四态区分要求。

### A8 前端交付缺口 — 属实

- `satrap-ui/src/pages/Platforms/index.tsx:267-268`: 群/时段覆盖仍为 JSON textarea (方案 7.2 要专用编辑器); :490 关闭弹窗无脏状态保护。
- `satrap-ui/src/pages/Sessions/ManualWakeModal.tsx:37-41`: accepted 后 toast + 关窗, 无 request_id 状态查询界面。
- 全库无任何 dry-run/规则试算端点。

## 三、证据不足项详录

### 1. File 出站分流 — 建议升级为"确认缺失"

审计原文: "File 出站仍由 onebot_utils.py:271 调用 File.to_dict 后进入普通 send_msg。未见按实际 SnowLuma 文件动作能力分流及 Plain/File/Plain 真通信验收; 不能据此断言目标实现一定失败, 但文件出站验收尚不能判通过。"

方案依据:
- `issue-10-plan.md:78` (现状表): "文件出站 | 只能确认生成 file 段 | 不能据此认定所有 OneBot 实现都支持文件发送; **应按实际实现的扩展 API 验证**"
- `issue-10-plan.md:160`: "Node/Nodes 与 File **按目标实现能力分流**, 保持前后顺序"
- `issue-10-plan.md:180`: "实现相关的文件与合并转发发送接口放在 OneBot 实现配置中, 按目标实现版本验证载荷与返回结构"
- `issue-10-plan.md:310` (验收清单): "Plain/Nodes/File 混合发送顺序……"

代码现状:
- `satrap/core/platform/onebot/onebot_utils.py:271-272`: `File` 组件只执行 `await component.to_dict()`, 产出 `{"type": "file", "data": {"name", "file"}}` (组件定义 `satrap/core/components/message.py:849`, `toDict` 约 :869), 混入普通消息段列表。
- `satrap/core/platform/onebot/adapter.py:702`: 整串 segments 经 `_dispatch_action(..., "send_private_msg", "send_group_msg", message=segments)` 发送, 即 file 段被当作普通消息内容。
- 对比证据 (分流骨架存在而 File 未接): `adapter.py:568-570` 将 `Node` 组件拆出走 `_send_forward` (:606) → `send_private_forward_msg`/`send_group_forward_msg` (:628) 专用动作。
- `upload_group_file`/`upload_private_file` (NapCat/SnowLuma 文件上传扩展动作) 在 satrap/、satrap-ui/src/、scripts/、docs/、tests/ 全部 **0 命中**。
- OneBot v11 标准消息段类型本无 `file`; 文件发送在这些实现里是独立动作 (先 upload 取 file_id), file 段经 send_msg 对 SnowLuma 大概率不产生任何文件。
- 探针 `scripts/probe_snowluma.py:32` 只断言 `send_group_msg` 动作, 不含任何文件动作。

复审结论: 代码侧证据是确定性零命中, 应定为"确认缺失"; 真正不足的只有 plan:310 的 Plain/File/Plain 混合顺序真通信验收证据。

复核路径:

```bash
grep -rn "upload_group_file\|upload_private_file" satrap/ scripts/ tests/   # 应为空
```

再对照读 `onebot_utils.py:249-292` (段转换) 与 `adapter.py:560-630` (Node 分流 vs File 直通)。

### 2. talk_value 到参与阈值的映射 — 建议升级为"确认缺失"

审计原文: "talk_value 到参与阈值的映射未找到实现; 现有显式消息阈值不是该映射。"

方案依据: `issue-10-plan.md:115` (唤醒策略表"消息数量/频率"行): "按群的待处理新消息数量达到阈值触发, **talk_value 映射为阈值**而非逐条随机抽签 | 默认关闭"。

代码现状:
- 全库 `talk_value` 共 5 处命中, **全部在文档**: 审计报告 1 处 + `issue-10-progress.md:138/146/156/174` 四处历史进度自认"talk_value 映射待做/未提供"; 代码 0 命中。
- 现有阈值实现 (`satrap/core/pipeline/wake_window.py`): :165 frequency 模式 `wake_message_threshold` (默认 3, 固定条数); :178 necessity 模式 `wake_score_threshold` (默认 0.65, 评分=问题/指向性/积压/近期提交占比)。两者均为显式配置值, 无群活跃度输入。
- talk_value 语义: NapCat 系群活跃"话痨值", 方案意图是频率阈值随群活跃度映射而非固定条数。

复审结论: 确认缺失, 进度文档自身已四次承认。缓解因素: 方案给该策略标默认关闭, 不影响默认行为; 但方案列明即应有实现或明确的缩减裁定。

复核路径:

```bash
grep -rn "talk_value" satrap/ satrap-ui/src/ scripts/ tests/   # 应为空, 仅 docs 命中
```

读 `issue-10-plan.md:115` 原文与 `wake_window.py:150-185`, 确认阈值/评分输入中无 talk_value。

### 3. 历史验收记录的有效性 — 证据纪律声明, 记录全部属实

审计原文: "ASR 同层封装、模型配置、媒体提取和真实 ASR/LLM 历史验收记录已存在; 本轮没有重新调用外部 API, 不将历史记录说成本轮实测。"

该条不是缺陷主张, 是审计的证据纪律声明。所引历史记录经核对全部真实存在:

| 记录 | 位置 |
| --- | --- |
| ASR 同层封装/配置/控制端点/前端 (P3) | `issue-10-progress.md` 约 87-96 行 (APICall/ASRCall 与 LLMCall 同层、asr CRUD、`test_asr_config`、前端 ASR 标签页) |
| 媒体提取管线 | `issue-10-progress.md` 约 100 行 (`resolve_attachments`、受限下载、ASR 16MiB/60s、文件 32MiB/20000 字符) |
| 真实 ASR+LLM 全链路 | `issue-10-progress.md:310` (2026-09-21: TTS 合成 4 秒 wav → 回环 HTTP → 真实 SiliconFlow XingChenASR → 真实 DeepSeek `AsyncLLM.chat` → `send_group_msg` 回传成功, 转写"今天天气不错/我们一起去公园散步") |
| 语音三级路径真实 ASR | `issue-10-audio-convert-plan.md:90-95` (2026-09-22: 硅基流动 transcriptions, get_record 替身与 PyAV 转码两条路径 resolved, 样本为合成正弦) |
| SnowLuma 网络层探针 | `issue-10-snowluma-probe.md` (安装包 SHA256、挂接边界、2026-09-21 复跑通过) |

需用户裁定: 这些条目是否接受历史记录作为验收证据。注意两处边界声明:
- `issue-10-snowluma-probe.md:28` 探针自述"不等同于完整 SnowLuma 应用启动、真实 QQ、真实模型、ASR 或所有 OneBot 动作验收" — 探针不覆盖范围恰好包含文件上传动作 (与第 1 条咬合)。
- `issue-10-progress.md:316` "真实 QQ 全链路 (方案裁定为模拟)" — 真实 QQ 侧从未验过, 属方案级裁定而非遗漏。

复核路径: 逐份打开上表行号核对; 如需刷新证据可重跑 `scripts/probe_snowluma.py` (只读、QQ 侧模拟、凭据仅运行时从 `.toolkit/` 读取), 但其不覆盖文件动作与真实 QQ。

### 4. 发行包构建与安装后核验 — 事实成立, 验收前提待裁定

审计原文: "发行检查历史记录证明前端 build, 但未见本轮构建发行包并从安装后的静态服务核验新增页面版本的证据。"

方案依据: `issue-10-plan.md:262`: "release 验收使用新构建的前端资源并检查**打包/静态服务实际加载版本**, 防止 Python 后端已支持 ASR 而**发行包仍带旧页面**。SnowLuma 仍由用户单独安装。"

现状:
- 历史证据两条, 均停在开发树内: `issue-10-audit-2026-09-22.md:126` "前端 `npm run build` 成功"; `issue-10-progress.md:313` "后端以 `satrap-ui/dist` 托管构建产物"。两者证明 build 发生过, 均非"安装后的静态服务核验"。
- 仓库**无发行构建流程**: 根目录无 `.spec`/`.iss`/installer 配置; `scripts/` 仅 start/stop-dev、kill-chat-server、probe_snowluma; 仅 `setup.py` + `pyproject.toml` (Python 包元数据)。当前形态为源码/pip 运行, 后端托管仓库内 `satrap-ui/dist`。

复审结论: 事实层属实 (无安装后核验记录), 但存在前提问题需裁定: plan:262 预设"发行包 + 安装后静态服务"形态, 而 Satrap 当前没有安装包构建流程。在源码运行形态下, "发行包带旧页面"风险等价于"干净检出/pip 安装后, 后端托管的 dist 是否为新版构建"; 若确认该形态, 验收方式应改写为"从干净环境安装后核验静态资源版本"。归类: 证据不足成立, 且验收前提需按项目实际分发形态改写。

复核路径:

```bash
ls F:/work/Satrap F:/work/Satrap/scripts/        # 确认无打包配置
find . -maxdepth 2 -iname "*.spec" -o -iname "*.iss" -o -iname "*installer*"   # 应为空
```

读 `issue-10-plan.md:262` 与 `issue-10-progress.md:313` 对比语义差, 并确认 README/setup.py 描述的实际分发方式。

## 四、测试口径说明

审计本轮全量单测 1875 passed / 7 skipped, 与残留复核收官记录的 1862 passed / 19 skipped 口径不同; 差异来自环境 (reportlab 缺失、Windows 符号链接权限等跳过项变化), 非代码回退, 两个数字均真实。

## 五、建议与待裁定项

修复优先级同意审计排序: **A1 (安全面) > A2 (丢数据, 须覆盖显式 @ 场景) > A3 (并发正确性) > A4**。

- A2 修法应将 batch 与当前事件已补全的 projected 内容**合并去重**而非二选一, 与 B6 已落地的锁内覆盖语义直接冲突, 需一并调整。
- **A5 (持久化) 与 A8 (前端编辑器/试算/状态页)** 工作量较大, 审计自注"或由用户明确裁定缩减此要求" — 是否全量实施待用户裁定。
- 证据不足项第 1、2 条建议按"确认缺失"纳入修复范围; 第 3 条待裁定是否接受历史证据; 第 4 条待裁定验收口径。

## 六、用户裁定与修复方案 (2026-09-23)

用户裁定: File 出站分流与 talk_value 映射**确认缺失, 应补实现** (File 措辞上不断言 SnowLuma 经 send_msg 必然失败; talk_value 不扩展为群活跃度采集); 历史验收记录有效性与发行包核验两项**搁置**。其余 10 项 (A1-A8 + File 出站 + talk_value) 的修复方案见 [issue-10-goal-audit-fix-plan.md](issue-10-goal-audit-fix-plan.md), 分四批次: ① A1/A2/A3 ② A4/A7/File/talk_value ③ A5/A6 ④ A8 前端。
