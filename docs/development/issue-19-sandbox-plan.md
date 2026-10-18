# Issue #19: Shell 执行隔离与审批分离方案

> 状态说明: 本文中的 satrap_coding 三平台 OS 隔离方案仍暂停, 不作为当前实现验收标准; 原 user/auto-agent/full 审批描述已由 [三档审批实现说明](../plugins/satrap-coding-plugin.md#三档审批) 取代. code_sandbox 的现有 worker 限制保持独立

状态: 待审设计, 尚未实施

补充状态: 本大方案暂停推进. code_sandbox 的后续范围以 [最小变更方案](code-sandbox-minimal-plan.md) 为准, 不再按本文的共享会话执行权限/审批方案实施; satrap_coding 尚未实施. code_sandbox 已按最小方案实施, Windows/Linux 回归通过, macOS 未验收, 具体记录见最小方案第 12.1 节

最新裁定: code_sandbox 已选择独立 Python worker + 自写网络/进程猴子补丁 + 库白名单/受限执行, 不引入 hermetic, 不实施三平台原生隔离后端. 网络拦截不新增外部依赖, RestrictedPython 与业务库仍保留. 本文保留的系统隔离设计不适用于该入口, 也不代表当前具有系统级断网或文件隔离能力

日期: 2026-10-15

适用分支: `fix/issue19`

本文件只提出暂停方案的设计与验收要求, 不代表下列配置, API 或原生后端已经存在. code_sandbox 的独立实现状态以最小方案为准

## 1. 要解决的行为与边界

目标是让模型能在已验证的操作系统沙箱内连续执行普通 Shell 和 Python 任务, 同时将人工审批保留给明确要求审批的操作和显式权限扩大请求

本次工作包含两个入口:

| 入口 | 当前行为 | 计划行为 |
| --- | --- | --- |
| `satrap_coding` 的同步与异步 Shell | 宿主机子进程, 全部逐次审批 | 共用执行服务, 按实际后端与审批策略决定 |
| `base_take` 的 `code_sandbox.run/run_file` | 宿主机 Python 子进程, 独立审批回调 | 接入同一执行服务与同一会话权限配置 |

首版不包含 Docker/WSL 自动安装, Windows 管理员账户/防火墙配置, 全功能终端, 后台常驻服务, 域名级网络代理, 第三方插件进程隔离, MCP 服务进程隔离. 现有可信 Python 插件仍运行在宿主进程中; 这里约束的是模型经上述入口启动的工作负载

完成标准不能只看审批次数减少. 必须在真实后端上证明允许范围内可执行, 范围外被系统拒绝, 子进程受到同样限制, 后端失效不会转成宿主机执行

## 2. 已核实的现状

| 位置 | 已有行为 | 本次处理 |
| --- | --- | --- |
| `satrap/expend/plugins/satrap_coding/tools/shell.py` | `_prepare_shell` 将风险至少提升为 HIGH; `_run_shell` 直接 `subprocess.run` | 拆分请求准备与实际执行, 移除入口内私有启动路径 |
| `satrap/expend/plugins/satrap_coding/core/permission.py` | Shell 在持久规则, 会话规则与 full 模式之前无条件 ASK | Shell 决策移交执行策略, 不再以风险等级伪装实际隔离能力 |
| `satrap/expend/plugins/satrap_coding/tools/approval.py` | Shell 只支持本次批准, 其他操作的 all 会改变整个会话模式 | Shell 使用结构化执行审批; 不复用 all 的整类授权 |
| `satrap/core/utils/sandbox/__init__.py` | `_safe_join` 约束包装器路径, Python 代码本身仍能访问宿主资源 | 保留现有文件管理接口, `run/run_file` 改用执行服务 |
| `satrap/expend/plugins/base_take/tools/utils.py` | 通过 `user_input_provider` 单独确认 Python 执行 | 有受信执行服务时改用共享决策, 旧回调保留为兼容桥接 |
| `satrap/display/service.py` | 会话绑定工作区与 sandbox 目录, coding 启用时协调停用 code_sandbox | 复用宿主绑定位置, 两个插件不各自推导不同权限 |

现有 `sandbox_root` 是文件工具的免审批目录/代码文件目录, 不能直接改名解释为操作系统沙箱边界. `cwd` 与环境变量清理也不是隔离证明

## 3. 建议采纳的主要设计决定

1. 新建 `satrap/core/execution/` 作为共享执行域, 不在 `core/` 根目录增加零散执行文件, 不让 `base_take` 反向依赖 `satrap_coding`
2. Python 负责会话权限, 审批, 规则与结果适配; 原生执行器负责 sandboxed 工作负载的系统隔离, 启动和整个进程树的回收
3. 首个沙箱后端采用 Windows 原生 MXC ProcessContainer, 用薄 Rust 执行器封装微软的版本化 MXC SDK. 不要求用户安装整个 Codex CLI
4. 首版仅在通过实际能力验证的平台开放自动执行. Linux/macOS 预留后端接口, 本轮不声称完成它们的隔离实现
5. 旧配置默认仍为宿主机逐次审批. 新沙箱自动执行必须由宿主管理员显式配置, 升级不自动扩大权限
6. 审批只授予同一执行边界内的指定请求; 规则 allow 也不授权脱离沙箱
7. 沙箱命令失败后不自动放宽权限重跑, 模型必须明确提交新的权限扩大请求
8. 首版持久命令规则采用完整调用匹配; 不实现任意 Shell 脚本的前缀放行

这些是本方案建议的裁定值, 供评审调整, 尚未据此开始开发

## 4. 后端选择与前置验证

### 4.1 为什么选薄原生执行器

Codex 的隔离实现具有 Rust crate 与进程启动协议依赖, 不是一个可以直接调用的 Python 函数. 直接依赖整个 `codex exec-server` 会引入额外服务协议与版本耦合

建议新增 `native/execution-helper/`, 构建产物暂定为 `satrap-execution-helper.exe`. Python 仅使用固定协议调用它. 执行器基于微软 MXC SDK 的公开版本化接口, 上游依赖锁定版本/提交, 保留对应许可与来源记录

微软 MXC 提供 Rust SDK, Codex 当前 Windows MXC 实现也依赖微软仓库. 这只证明路线存在; 在本项目能否满足全部边界, 必须通过下述 P0 验证, 不能把上游提供接口等同于本项目已经可用

### 4.2 平台支持范围

| 环境 | 首版处理 | 不满足要求时 |
| --- | --- | --- |
| 支持原生 ProcessContainer 的 Windows | 原生执行器 + MXC | 返回具体不可用原因 |
| 不支持该能力的 Windows / 受企业策略限制 | 保留原有宿主机逐次审批模式 | sandboxed 请求拒绝, 不自动选 host |
| Linux / macOS | 保留 host 兼容入口, 状态明确显示未实现沙箱后端 | sandboxed 请求拒绝 |

不实现 Codex 的旧 elevated/unelevated Windows 后端作为自动回退, 也不启用 MXC 的其他降级后端. 选择原生 ProcessContainer 后必须实际使用该后端

### 4.3 P0 必须交付的证据

P0 是后续开发授权后的第一阶段, 本轮没有运行这些实验:

- 在目标机器读取 OS/架构与实际能力探测结果, 不只按系统版本号猜测可用
- 固定 SDK 版本后成功构建, 独立运行发布产物, 确认用户无需 Rust 工具链
- PowerShell, cmd, 当前 Python 环境在最小运行时读取授权下正常执行, 中文输入输出为 UTF-8
- 工作区允许写入, 外部哨兵文件读写拒绝, 显式拒绝子路径拒绝, 新子进程继承限制
- 禁网时验证 TCP, UDP/DNS, 本机回环连接均不可绕过; 不用环境变量代替网络隔离
- 验证命令结束, 超时, 用户取消, Python 调用方退出时, 后代进程全部终止
- 验证失败, 不支持的路径或网络策略, 执行器缺失与协议不匹配不会触发 host 回退
- 确认错误结果是否足以区分后端启动失败与普通命令失败; 不能可靠分类时输出 unknown, 不据此自动提权
- 确认系统 IPC, 命名管道, 继承句柄与进程控制不会允许工作负载借助宿主控制端间接访问受保护资源

P0 失败时只能修订后端方案并更新本文档, 不能用缩减隔离能力的实现继续开放自动执行. 首版不支持的运行时/路径形态应明确列为不兼容, 不自动授权整个磁盘来修复启动失败

## 5. 统一配置模型

### 5.1 执行配置与审批配置分开

以下是拟议的逻辑结构, 最终序列化接入已有宿主配置设施, 不在两个插件里各建一份可独立修改的配置:

```json
{
  "version": 1,
  "execution_mode": "sandboxed",
  "backend": "windows_mxc",
  "filesystem_profile": "workspace_write",
  "additional_read_roots": [],
  "additional_write_roots": [],
  "denied_roots": [],
  "network": "off",
  "approval_policy": "on_escalation",
  "allow_permission_requests": false,
  "max_timeout_seconds": 120,
  "max_parallel_processes": 2
}
```

示例是新模式的推荐设置, 不是升级后默认值. 旧会话未配置时解析为 `host + always`, 保持现有 Shell/Python 逐次批准

| 配置 | 语义 |
| --- | --- |
| `execution_mode=host` | 当前用户权限下的宿主机执行, 每次须人工确认 |
| `execution_mode=sandboxed` | 必须有能执行完整策略的真实沙箱后端 |
| `filesystem_profile=read_only` | 已授权目录可读, 工作区不可写, 只开放本次私有临时目录的写入 |
| `filesystem_profile=workspace_write` | 在上一档基础上允许指定工作区写入, 保留保护子路径 |
| `network=off` | 工作负载无网络, 包括回环与 DNS; 后端若不能兑现就拒绝该配置 |
| `network=on` | 开放后端所支持的网络访问, 不是域名白名单; 只能由管理员预先配置或审批明确扩大 |
| `allow_permission_requests` | 是否允许模型提出扩大请求, 不直接授权任何扩大 |

首版不提供自动选择 host 的 `auto` 值, 不提供 `fallback_to_host`, 不提供“无沙箱且全自动”的组合. `host + never` 表示无法取得所需人工授权, 因而拒绝执行

host 的有效能力明确为当前宿主用户的文件/网络权限, 不显示受限文件系统或禁网已生效. 上表的文件系统与网络限制只适用于 sandboxed; host 配置不接受这些字段来伪装隔离. 已批准的 host 脚本可能访问宿主敏感数据, 命令规则与批准卡不能代替系统边界

### 5.2 文件系统策略如何产生

宿主基于当前会话的受信工作区绑定形成策略, 模型传入的 workdir 不能改变工作区根:

- 读取: 当前会话工作区, 明确配置的额外读取根, 执行当前工具所需的运行时读取根
- 写入: `workspace_write` 的当前工作区, 明确配置的额外写入根, 当前执行的私有临时目录
- 保护: Satrap 凭据/数据库/审批规则/控制端文件拒绝读取与写入; `.git`, `.agents`, `.codex` 与既有保护目录默认不可写
- 临时目录: 每次执行单独创建, 仅该目录可写, 不开放整个系统 TEMP 或其他会话临时目录
- 运行时: 显示实际授权的解释器/依赖目录, 不以“能启动 Python”为由默认允许读取整个用户目录
- 环境变量: 继续沿用密钥变量剥离机制, 显式放行项也必须来自受信配置

拒绝路径优先于读取/写入授权. 规范化处理相对路径, Windows 大小写与设备路径别名, junction/symlink/reparse point, UNC 等形态; 首版不能可靠表示的形态拒绝, 不只检查字符串前缀

现有 `sandbox_root` 若与新的强制保护区重叠, 配置校验失败并提示选择其他目录, 不自动移动旧文件, 不削弱保护项. 共享项目的工作区本身可能包含共享资料; 会话私有数据的隔离不等于项目资料按会话隔离

### 5.3 权限来源与配置写入

宿主保存环境权限上限与 revision; 会话配置只能在该上限内选择更小的权限. 工具参数与插件配置不能声明“我已隔离”, 不能修改后端能力, 不能扩大管理员上限

扩展读取/写入目录, 联网, 切换 host, 修改自动执行策略属于受信配置修改. Chat 通过已认证的宿主操作通道完成, 平台通过现有管理端授权完成; 普通群成员消息和模型工具参数不能直接切换

同步/异步会话, Chat/平台使用同一解析器与配置格式. 存储只增加一份共享执行配置, 管理前端通过宿主 API 读取有效值与来源; 插件配置页只显示引用与状态, 不复制另一份配置

## 6. 审批行为与规则优先级

### 6.1 审批策略

| 情况 | `always` | `on_escalation` | `never` |
| --- | --- | --- | --- |
| 可用沙箱内的普通请求 | 人工确认或匹配的有效授权 | 自动执行 | 自动执行 |
| 命中 ask 规则 | 人工确认 | 人工确认 | 拒绝 |
| 命中 deny / 强制禁止 | 拒绝 | 拒绝 | 拒绝 |
| 显式权限扩大, 且管理员允许申请 | 人工确认 | 人工确认 | 拒绝 |
| 沙箱不可用 / 无法兑现权限 | 拒绝 | 拒绝 | 拒绝 |
| 显式选择 host | 仅单次人工批准 | 仍须单次人工批准 | 拒绝 |

`always` 允许用户显式创建精确授权来替代重复确认. 不通过聊天的一次“同意”隐式创建会话或持久授权

固定决策顺序:

1. 校验调用者, 会话归属, 参数与受信配置
2. 计划模式, 强制保护, 明确禁止规则命中则拒绝
3. 校验所选后端实际能力与配置可表示性, 不可用则拒绝
4. 校验扩大请求是否允许提出以及是否超过管理员上限
5. host 路径始终要求单次人工批准, 不使用会话/持久 allow 来绕过
6. 对 sandboxed 请求检查命令规则: deny 优先于 ask, ask 优先于 allow
7. 若无更严格规则, 检查同边界精确授权, 再按审批策略决定
8. 执行前重新检查权限 revision, 计划状态和请求绑定

命令风险分类可以用于说明或拒绝显然禁止项, 但它不再提供“该命令是只读所以不需要隔离”的结论. 不尝试用正则完整证明 PowerShell, cmd 或 Python 的副作用

### 6.2 授权粒度

首版有三种授权:

- 单次: 绑定 `request_id`, 执行前原子消费, 即使启动失败也不复用
- 本会话同一调用: 当前运行实例内, 相同指纹才能复用, 重启/分叉/恢复后失效
- 持久精确规则: 管理端显式保存, 绑定工作区身份, 后端, 权限上限与完整调用; 可列出和撤销

绑定字段至少包含环境/会话身份, 工具操作种类, 实际可执行文件绝对路径, 完整 argv/脚本文本或 Python code, cwd, 有效文件/网络权限, timeout, 环境快照标识, 配置 revision. 会话授权额外绑定当前运行实例, 持久规则不包含该临时实例标识

环境变量值不写入审批日志或持久规则. 单次/会话授权绑定本轮冻结环境快照; 会话重启后失效. 持久规则绑定受信环境配置与运行时身份的稳定指纹, 启动时重新验证, 不存储临时快照 ID. 首版持久 allow 仅适用于无需显式密钥放行的环境; 配置/运行时或最终剥敏环境发生变化时指纹改变, 旧规则不匹配

批准绑定调用, 不保证执行期间工作区所有脚本及导入依赖的内容不变. 不把批准命令等同于批准未来所有同名脚本; 该边界依靠仍然生效的沙箱保护

### 6.3 命令规则的首版限制

规则使用版本化结构保存 exact 调用及 `allow/ask/deny`, 同一调用取最严格结果. PowerShell/cmd 的脚本参数完整比较, 包括复合语句和引用内容

不对 `powershell`, `cmd`, `python`, `git` 整个可执行程序授予前缀持久放行, 不把 `echo ok & ...` 按第一段当成安全命令. 将来若加入 argv 前缀规则, 必须另行设计脚本解析, 路径绑定与规则冲突, 不在本次暗中扩展

allow 只免除当前边界内的审批, 不改变可读/可写/网络权限, 不覆盖 deny, 不授权系统管理员提权

## 7. 请求与执行协议

### 7.1 共享对象

使用枚举, frozen dataclass, Protocol 等显式类型描述以下对象, 不用 `dict[str, Any]` 在核心流程间传递:

| 对象 | 主要内容 |
| --- | --- |
| `ExecutionRequest` | request_id, owner, operation, executable, argv/code, cwd, timeout, 环境快照 |
| `EffectiveExecutionPolicy` | 权限上限, 本轮有效权限, backend, approval_policy, revision |
| `BackendCapabilities` | 实际 backend/version, 文件/网络/进程树能力, 不可用原因 |
| `ExecutionApproval` | 请求绑定, 授权范围, 审批主体, 到期/消费状态 |
| `ExecutionResult` | 状态, exit_code, stdout/stderr, 截断标志, 实际 backend, 错误类别 |

反序列化边界从 `object` 严格校验为上述类型, 未知协议版本拒绝. 同步与异步包装器共用请求构建与纯决策代码

### 7.2 Python 与执行器通信

建议采用每次执行一个 helper 进程, 通过 stdin/stdout 的版本化 JSON 帧传递请求, stdout/stderr 工作负载输出由 helper 编码后作为数据帧返回. 子进程输出不能伪装成 helper 的授权结果或控制帧

helper 不开放网络监听端口, 不从 PATH 搜索同名执行器, 不在命令行或工作区临时文件中传递密钥. 执行器路径固定在发布目录, 校验发布清单中的版本与摘要, 且该目录对工作负载不可写

helper 启动时自行验证策略, 实际后端选择与路径有效性. 只有确认完整限制生效后才启动工作负载, 禁用 audit/unrestricted/其他后端回退选项. 所有不需要的宿主句柄关闭或禁止继承

Python 持有 helper 生命周期, helper 持有 sandboxed 工作负载进程树. 在调用方中止时必须通过系统级生命周期机制终止整个沙箱工作负载; 不能只依赖 Python 正常退出回调

host 兼容适配不要求机器先安装 MXC/helper, 保留现有明确批准后的宿主启动能力. 它不继承 sandboxed 的隔离与异常退出回收保证, 状态与验收报告分别记录; 不允许因为 sandboxed helper 缺失转进该兼容适配

### 7.3 编码, 输出与资源

- PowerShell/cmd 保留现有 UTF-8 强制前缀, Python 设置 UTF-8 模式, helper 协议统一 UTF-8
- stdout/stderr 分开消费, 限制保存缓冲区, 达到上限后继续排空并标记截断, 不因输出过大阻塞
- 首版保留现有 Shell 输出末尾约 20,000 字符的兼容行为, 原生层同时设置有界字节缓冲
- 使用宿主上限限制超时和并发, sandboxed 的 timeout 到期/用户取消/会话关闭均回收进程树; host 保持兼容行为并报告实际回收结果
- 首版不承诺硬性 CPU/内存/磁盘配额, 界面不显示未实现的资源隔离能力
- 不保留跨调用终端, 不允许 detached 工作负载在工具完成后继续运行

## 8. 执行流程与失败语义

正常执行:

```text
工具参数
  -> 解析为不可变请求
  -> 宿主解析有效权限与 revision
  -> 确认后端可执行该策略
  -> 强制限制/规则/审批决策
  -> 必要时等待绑定该请求的授权
  -> 重新校验并消费授权
  -> helper 建立隔离后启动
  -> 收集输出, 回收全部子进程
  -> 返回结构化结果并适配现有工具输出
```

普通沙箱执行不会暂停等待人工批准越界行为. 命令先按已有权限执行, 越界访问由系统拒绝. 模型随后可以提出新的扩大请求, 但原请求不会自动重跑

扩大请求首版只支持管理员上限内的额外路径或联网. 参数采用明确字段, 显示权限前后差异; 不通过命令说明里的“请批准”自动推断权限. 固定保护项与后端选择不能通过单次扩大请求修改

同一命令可能在失败前已经修改允许范围内的文件. 所以即使用户后来批准扩大权限, 也只能作为新的显式执行请求启动, 不把失败视为无副作用

| 结果状态 | 处理 |
| --- | --- |
| `completed` | 返回真实退出码, 非零退出码仍属于命令执行结果 |
| `denied` | 规则/计划/权限上限拒绝, 不启动工作负载 |
| `approval_required` | 缺少人工授权通道或请求待审批, 不执行 |
| `approval_stale` | 等待期间策略/请求发生变化, 原批准无效 |
| `sandbox_unavailable` | 后端缺失/能力不满足, 不回退 |
| `sandbox_setup_failed` | 建立隔离失败, 不启动工作负载 |
| `timed_out` / `cancelled` | 返回已捕获输出, 确认进程树回收 |
| `execution_failed` | 可执行文件不存在等启动错误 |
| `unknown_failure` | 无法可靠分类, 不根据 stderr 猜测并提权 |

不把“stderr 出现 permission denied”作为可自动扩大权限的依据, 因为输出可由工作负载任意构造

## 9. 审批通道, 计划模式与子代理

Chat 中由已认证的当前操作者审批. 平台若没有能校验审批主体的可靠管理端审批通道, 需要审批的操作返回不可执行, 不向群里随便问一句并把任何人的 y 当作授权

首版可以复用现有 user_input_provider 展示, 但审批状态在宿主执行域中保存并绑定 request_id, 授权主体由宿主提供, 不从回答文本自报. 老的纯字符串/布尔回调只适用于原有 host 单次确认兼容路径, 不授予沙箱扩权或持久规则权限

每个请求只产生一次执行审批. 插件预检查, 执行服务和 helper 不分别弹三次确认. 如果现有宿主有额外强制工具审批, 接入时必须核查并明确其规则; 未验证可统一前不承诺所有页面都只出现一次

计划模式首版保持现有 coding 行为: Shell 全部禁用, 文件工具只读. 同会话的 code_sandbox.run/run_file 也被共享执行服务禁止, 防止换入口执行. 对计划模式下的 base_take 文件保存/删除, 复用同一计划写入限制

子代理继承父会话工作区和权限上限, 不复用单次授权, 不因复制工具对象重绑到更宽工作区. 会话精确授权是否共享取决于同一受信会话归属, 必须由宿主传入, 不能用模型提供的 session_id 决定

进入计划模式, 权限撤销, 工作区重绑时使待审批请求失效, 并取消该会话正在运行的执行任务. sandboxed 必须确认整棵进程树回收; host 无法确认后代回收时显式报告, 不显示已完全撤销. 普通无关配置修改只使后续请求使用新 revision, 不把 UI 更新当作已回收进程的证据

## 10. 对已有配置与 API 的兼容

1. 新执行配置缺失时使用 `host + always`, 不迁移成自动执行
2. 现有 `workspace_root`, `sandbox_root`, `protected_dirs`, `allowed_env_vars`, `shell_timeout` 继续解析; 明确各自含义, 超出宿主上限时拒绝
3. `/approve mode user|auto-agent|full` 保留现有文件工具策略语义, 不自动改变新的执行隔离/审批配置; 状态输出明确显示这两个作用范围
4. auto-agent 仍只能拒绝或转人工, 不产生扩大权限授权. 本次不引入模型替人批准
5. 旧 `permissions.json` 的操作/风险规则只供既有文件工具使用. 旧 shell 规则不迁移为新命令 allow
6. 新精确执行规则带 version, 使用独立命名的结构保存, 不覆盖旧文件. 写入复用或补足真正的跨进程互斥与原子替换, 不能把现有线程锁视为跨进程锁
7. 新增 `/approve execution status` 查看有效配置与后端状态. 策略/规则修改走受信管理端, 不默认开放给普通平台发言者
8. `CodeSandbox` 直接调用者未注入执行服务时继续走明确标识的旧 host 行为, 不宣传为系统沙箱. 内置工具必须注入共享服务, 不得因漏注入而退回旧执行路径
9. 保留现有工具名, 常规参数与返回键. 结构化失败通过现有错误字段适配, 新增执行模式/后端信息不伪造命令 stdout
10. 不批量修改用户历史, 不清理已有 sandbox 文件, 不改变现有模型配置

## 11. 管理界面与可观察性

管理端新增共享的“代码执行”配置与状态区域, Chat/平台读取同一组件与 API 契约, 不做两个可漂移的表单

界面至少展示配置执行模式, 实际 backend/version, 探测状态与不可用原因, 当前工作区, 实际读取/写入/拒绝目录, 网络开关, 审批策略和配置来源. 未完成能力探测时显示 unknown, 不能显示为已隔离

host 明确显示“宿主机执行, 每次人工批准”; sandboxed 配置但不可用时显示拒绝原因, 不显示“自动兼容”. 修改配置后区分已保存, 等待应用, 已生效, 应用失败; 不能仅凭前端保存成功更新状态

审批卡显示完整调用, cwd, 实际执行模式, 权限差异和授权有效范围. 首版 host 只有本次批准/拒绝; sandboxed 可额外明确选择会话精确授权, 持久规则在管理页单独创建

日志记录 request_id, 工具入口, 会话/工作区标识, 配置 revision, 实际后端, 规则来源, 审批范围, 最终状态和回收结果. 不记录环境变量值, 不默认复制完整 Python 源码或命令中的密钥到新审计日志; 完整调用仅按现有受控审批展示策略处理

## 12. 拟修改模块与职责

以下新增路径是目录设计, 不是已经存在的文件:

| 路径 | 职责 |
| --- | --- |
| `satrap/core/execution/models.py` | 受信请求, 策略, 结果与枚举类型 |
| `satrap/core/execution/policy.py` | 权限合成, 固定优先级, 纯决策 |
| `satrap/core/execution/approvals.py` | 请求绑定, 授权消费, 精确规则 |
| `satrap/core/execution/service.py` | 公共执行流程, 并发, 取消与宿主接入 |
| `satrap/core/execution/backends.py` | host 与 MXC 适配, helper 协议与能力验证 |
| `native/execution-helper/` | MXC SDK 薄封装, 不承载 Satrap 的会话审批逻辑 |
| `satrap/core/config/` | 共享执行配置存储与受信管理修改 |
| coding 的 `shell.py`, 两个 interaction 入口, `approval.py`, `permission.py` | 接入服务, 删除重复 Shell 启动/决策路径, 保留文件工具策略 |
| `satrap/core/utils/sandbox/__init__.py` 与 `sandbox_tools/` | Python 执行接入, 兼容返回与文件操作 |
| 两插件工具工厂与 coding `state.py` | 注入同一个会话执行服务, 不再各自创建执行权限真相源 |
| `satrap/display/service.py`, 平台实际会话构建与管理端路由 | 绑定宿主执行环境, 提供受信审批与状态 |
| `satrap-ui/` 中配置 API 与共享配置组件 | 展示配置和后端有效状态 |

具体拆文件数量以实现职责为准, 不为每个枚举或小函数新建模块. 合并重复的 sync/async 逻辑, 保留各自等待/取消方式, 不用重复 to_thread 包装制造两套状态

## 13. 分阶段交付与审核点

| 阶段 | 交付 | 允许出现的行为变化 | 通过条件 |
| --- | --- | --- | --- |
| P0 后端验证 | 最小 helper 原型, SDK 固定与兼容/隔离实测报告 | 仅隔离测试目录中的原型实验 | 第 4.3 节全部有结果, 缺项不能当通过 |
| P1 公共执行域 | 类型, 共享配置, host 适配, 审批绑定 | 旧 host 行为保持逐次批准 | 双入口共用流程, 无自动权限扩大 |
| P2 MXC 接入 | 真正沙箱启动, 输出, 超时/取消, Python/Shell 接入 | 仅显式启用 sandboxed 的会话可自动执行 | 真实系统边界测试通过 |
| P3 规则与权限请求 | 精确授权, 受上限限制的扩大请求, 计划/子代理约束 | 同边界精确授权可复用 | 授权失效与并发消费测试通过 |
| P4 管理端与发布 | 共用表单, 实际状态, 构建分发, 文档与迁移说明 | 管理员可审查并启用 | 前后端状态一致, 发布产物验证通过 |

P1 不单独关闭旧无条件 Shell 审批后交付为问题已解决. P2/P3 必须整体保证新决策与真实后端相连; 未安装 helper 的发布版本始终只能显示沙箱不可用

提交按上述责任域组织, 可以评审/回退. 不在本次计划阶段执行这些提交, 不把本文档写入视为实施授权

## 14. 测试与验收矩阵

按完整流程整合到已有 coding configuration/security/flows 测试模块, 原生后端使用一个集中场景模块, 不为每个小函数建立测试文件

| 场景组 | 必须覆盖的结果 |
| --- | --- |
| 兼容流程 | 无新配置仍逐次审批; 旧 full 不自动授权 host; 旧 shell 规则无新授权效果; 单独 base_take 也遵守默认行为 |
| 审批矩阵 | 三策略 x allow/ask/deny x host/sandboxed; 无 provider, 非授权主体, 拒绝, 超时, 重复回复均不误执行 |
| 绑定与恢复 | command/cwd/网络/目录/环境/timeout/revision 改变时旧授权失效; 分叉与重启不恢复会话批准; 单次批准并发只消费一次 |
| 真实文件边界 | 工作区写入, 外部哨兵读写拒绝, 保护子路径, symlink/junction 与设备别名, 同项目不同会话私有目录, 运行时目录权限 |
| 真实网络边界 | TCP, UDP/DNS, localhost, 子进程与清理代理变量后的禁网行为 |
| 双入口与子进程 | Shell 调 Python, Python 调 Shell, 新子进程与 detached 尝试均不能脱离相同边界; coding 开关切换不留下旧 host 工具 |
| 沙箱生命周期 | 成功, 非零退出, 启动失败, 超时, 用户取消, 会话关闭, 父进程退出, helper 崩溃后无残留进程; host 结果独立记录, 不冒充该保证 |
| 输出与协议 | 中文 UTF-8, 大量 stdout/stderr, 截断标志, 伪造 JSON/permission denied 输出不影响控制决策 |
| 故障与发布 | helper 缺失/被替换/版本不符, SDK 后端不可用, 不支持策略, 发布机无 Rust, 不发生静默回退 |
| 管理状态 | Chat/平台保存同一格式, 保存与应用状态分开, 撤销使等待批准失效并取消运行任务, 普通群成员不能修改上限 |

普通命令测试只操作测试目录与哨兵文件, 网络测试使用受控本地服务, 不访问真实账号数据, 不清理用户目录, 不启动无限驻留服务

纯决策测试使用受控后端; 它们不能替代真实隔离测试. 不支持 MXC 的机器可将原生测试标记为 unsupported, 但该结果不满足发布中“支持 MXC 自动执行”的验收. 支持的 Windows 发布平台必须有真实运行证据

新增 Python 核心类型通过项目 Pylance/Pyright 检查, Rust helper 通过编译与必要检查, 前端通过相关配置流程测试. 只运行相关完整场景与受影响回归, 不扩展成无关全库测试清理

## 15. 回滚与剩余限制

关闭 sandboxed 配置前先停止接受新请求, 取消并等待现有执行树回收, 使待审批授权失效. 不将已经提交的 sandboxed 请求自动改为 host 执行

回滚到旧版本前停掉执行服务/helper 并确认无残留, 新配置与新规则可保留但旧代码不读取它们. 原配置与用户文件不删除. 因沙箱命令已经产生的正常工作区修改不自动回滚

首版仍存在的明确限制:

- Windows 原生能力受 OS 与管理策略约束, 老系统没有自动执行沙箱后端
- Linux/macOS 真正沙箱后端属于后续工作, 不随接口预留自动变为支持
- 网络只有关闭/开放, 没有站点级授权
- 没有跨调用终端, 后台服务与硬资源配额
- 授权不是依赖文件内容的事务快照, 并行工作区修改仍可能改变程序行为
- 已在宿主执行的任意第三方插件不在这一边界内, 本计划不能把整个 Satrap 宣称为插件隔离平台

## 16. 参考来源与需要评审的选择

已核查的 Codex 源码固定在 `bb64b894fd67158be0f874872e6d7bfe717e7368`:

- [Issue #19](https://github.com/liminalselves/Satrap/issues/19)
- [Codex 命令决策](https://github.com/openai/codex/blob/bb64b894fd67158be0f874872e6d7bfe717e7368/codex-rs/core/src/exec_policy.rs)
- [Codex 执行与审批编排](https://github.com/openai/codex/blob/bb64b894fd67158be0f874872e6d7bfe717e7368/codex-rs/core/src/tools/orchestrator.rs)
- [Codex 授权缓存与沙箱覆盖](https://github.com/openai/codex/blob/bb64b894fd67158be0f874872e6d7bfe717e7368/codex-rs/core/src/tools/sandboxing.rs)
- [Codex Windows MXC 依赖](https://github.com/openai/codex/blob/bb64b894fd67158be0f874872e6d7bfe717e7368/codex-rs/mxc-sandbox/Cargo.toml)
- [Microsoft MXC](https://github.com/microsoft/mxc)
- [MXC SDK API](https://github.com/microsoft/mxc/blob/main/docs/api-reference/README.md)
- [MXC Windows 系统要求](https://github.com/microsoft/mxc/blob/main/docs/backends/process-container/os-version-support.md)

Codex 的审批/隔离分层是参考依据. 本方案有意采取不同的首版约束: allow 不绕过沙箱, 持久规则只支持完整调用, 不自动重试提权, 不接入旧 Windows 回退后端. 这些是 Satrap 的拟议设计, 不是对 Codex 行为的描述

评审优先确认以下取舍:

| 选择 | 建议值 | 代价 |
| --- | --- | --- |
| 后端实现 | 薄 Rust helper + MXC SDK | 增加原生构建与发布维护 |
| 首版平台 | 支持原生 MXC 的 Windows | 老 Windows/Linux/macOS 仍需 host 逐次批准 |
| 升级默认 | 保持 host + always | 用户需主动启用新模式 |
| 权限扩大 | 只扩路径/联网, 不支持单次脱离沙箱 | 某些系统操作只能另走显式 host 模式 |
| 命令规则 | 完整调用 exact, 无 Shell 前缀放行 | 参数常变时规则复用率较低 |
| 模型审批 | 保持只能拒绝/转人工 | 不能用 auto-agent 自动批准扩大 |
| 旧 approve 命令 | 文件语义兼容, 执行策略单列 | 配置展示需解释两个作用范围 |

当前待审状态不代表这些选择已获认可. 审核后先锁定范围与后端取舍, 再按 P0 至 P4 推进
