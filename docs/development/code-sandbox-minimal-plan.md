# code_sandbox 最小变更方案

状态: 已实施, Windows/Linux 验收通过, macOS 未验收

日期: 2026-10-15

本方案仅处理 `base_take` 提供的 `code_sandbox`. 保留 7 种 operation, 所有操作改在每次新建的 Python 进程中运行, 使用执行器自写猴子补丁拦截 Python 网络 API 与外部进程启动, 模型使用库白名单, 取消代码审批. 不修改 `satrap_coding` 的 Shell, 文件审批或权限模式

按用户最新决定, 采用独立 Python worker + 自写猴子补丁 + 库白名单/受限执行的共同实现, 不依赖 hermetic, 不开发 Windows/Linux/macOS 三套原生沙箱后端. 不新增网络/进程拦截的第三方依赖, RestrictedPython 与业务库仍按本方案保留. 这是防止普通模型代码意外联网或扩大操作范围的应用层约束, 不承诺对抗恶意代码的系统级隔离; 原生扩展, 反射和补丁绕过仍是边界

执行定位为普通功能约束: 防止意外联网, 非允许导入和误报成功. 导入检查与简单违规记录服务于结果准确性, 不增加对抗恶意代码的强制终止或防伪协议

本文保留设计与验收范围, 实施结果见第 12.1 节. 已完成独立 worker 与工具接入, 未重启后端或修改系统网络配置. macOS 暂无可用环境, 按用户决定标记未验收

## 1. 变更范围

| 项目 | 决定 |
| --- | --- |
| 工具名称 | 继续使用 `code_sandbox` |
| 工具参数 | 保留 `operation`, `code`, `path` |
| 操作集合 | 保留 `run`, `run_file`, `save`, `read`, `delete`, `delete_dir`, `list` |
| 执行进程 | 每次调用独立 Python worker, 不复用解释器 |
| 网络 | 自写猴子补丁固定拦截 Python 网络 API, 不允许 localhost/域名例外, 不增加联网开关或联网审批 |
| 系统适配 | Windows/Linux/macOS 共用 Python worker 与自写 guards, 不编写三套原生隔离后端 |
| 库 | 管理员配置公开白名单, 未允许的导入直接报错 |
| 审批 | 不再调用执行审批回调或 user_input_provider |
| 持久状态 | 仅保留目录内文件, 不保留 Python 变量与导入状态 |
| 产物送达 | 首版仅生成/保存文件并可返回路径, 不自动展示图片或发送附件 |
| 路径 | 继续使用宿主绑定的 sandbox_root, 不自动获得项目工作区权限 |
| 限制边界 | 路径与库 API 在执行器内校验, 无内核级文件或网络隔离, 独立进程仍使用宿主账号权限 |
| 失败 | 直接返回错误, 不转普通宿主执行, 不申请提权 |

不增加通用 Shell, pip 安装, 子代理, 命令授权缓存, 权限扩大, 常驻 Python 内核, 新管理页面或共享执行权限引擎. Chat 当前启用 coding 后停用 code_sandbox 的协调行为保持不变

## 2. 当前实现具体改哪里

改动前 `run/run_file` 已启动 Python 子进程, 但其他 5 种操作在主进程执行, Python 执行也没有网络拦截与库限制. 现已将这些路径合并为一个受限 worker 入口

| 文件 | 改动 |
| --- | --- |
| `satrap/core/utils/sandbox/__init__.py` | 保留 `CodeSandbox` 类及公开方法, 改为构造请求并调用 worker 的兼容门面 |
| `satrap/expend/tools/sandbox_tools/sync.py` | 删除运行前人工审批, 保留参数与返回格式适配 |
| `satrap/expend/tools/sandbox_tools/async_.py` | 同上, 增加异步取消能终止 worker 的处理 |
| `satrap/expend/tools/sandbox_tools/utils.py` | 保留代码块提取与操作参数校验, 返回 worker 的业务结果及错误, 删除不用的 authorizer 类型 |
| `satrap/expend/plugins/base_take/tools/__init__.py` | 注入生效白名单/执行参数, 同步生成可用库说明, 停止创建执行审批回调 |
| `satrap/expend/plugins/base_take/tools/utils.py` | 删除仅服务于 code_sandbox 审批的四个辅助函数, 保留文档读取等其他逻辑 |
| `satrap/expend/plugins/base_take/meta.yaml` | 增加库白名单配置, 更新工具描述, 复用现有通用配置表单 |
| 依赖与发布清单 | 固定 RestrictedPython 与业务库版本, 网络/进程拦截只使用标准库, 验证各平台 Python/依赖兼容; 不增加 MXC SDK/Rust helper/Docker 镜像或系统过滤器依赖 |

新增内部包放在 `satrap/core/utils/sandbox/`, 不把 worker 或白名单文件直接散放 core 根目录:

原 sandbox.py 已迁为该包的 __init__.py, 继续支持 from satrap.core.utils.sandbox import CodeSandbox. 执行器与兼容门面集中在同一目录, worker 仍通过固定脚本路径独立启动

| 文件 | 职责 |
| --- | --- |
| `__init__.py` | CodeSandbox 公开门面与导出, 保留既有导入路径 |
| `models.py` | 请求, 响应, 白名单配置和错误类型 |
| `runner.py` | 父进程启动, 协议, 超时, 输出与取消 |
| `worker.py` | 子进程入口, 操作分派, 文件操作与用户源码执行 |
| `policy.py` | 受限编译, builtins, 导入及属性访问策略 |
| `guards.py` | 在 worker 内安装自写网络/进程补丁, 记录违规, 校验安装状态, 转换策略异常 |

不新增 backends 目录, 不为每种操作建立一个文件. 进程启动/取消的少量系统差异集中在 runner, 不发展为通用执行引擎. `CodeSandbox` 的现有 import 路径与普通方法签名保持兼容, 内部受限运行时不导入整个 Satrap 应用

## 3. 一次调用的执行流程

执行协议采用一次请求/一次结果, 不建立 READY 握手或额外控制管道:

```text
code_sandbox(operation, code, path)
  -> 主进程纯参数校验/代码块提取
  -> 冻结 sandbox 根, 生效白名单与超时配置
  -> 使用既定解释器路径启动可信 worker
  -> 父进程向 stdin 写入一个 JSON 请求, 随即关闭写端
  -> worker 读取并解析请求, 先安装固定补丁和语言/路径策略
  -> 初始化成功后才导入所需业务库并执行操作
  -> 捕获业务输出, 结合违规记录生成一个 JSON 结果
  -> worker 写出结果并退出, 父进程解析和适配
```

父进程可以提前发送请求, 是否允许执行由可信 worker 的固定代码顺序控制. 初始化失败返回错误, 不导入业务库或执行模型源码. 请求内容不会因已经到达 worker 就被执行, 无需用握手证明这一点

主进程不替 worker 读取待运行文件, 不代为保存/读取/删除业务文件, 不调用沙箱内文件进行配置探测. 宿主为会话预建根目录属于会话初始化, 子目录创建等实际操作全部由 worker 完成

一个 operation 只启动一次 Python worker. run_file 在 worker 内读取源文件后使用与 run 相同的执行函数, 不再启动第二个业务解释器

启动参数固定为 [self.python_executable, "-I", "-S", "-B", "-X", "utf8", worker_path]. 沿用 CodeSandbox 已保存的解释器路径; base_take 继续传入 sys.executable, 不通过 PATH 查找名为 python 的命令, 不把本机 F:/conda 路径写死进实现

worker_path 与依赖目录来自可信安装位置. -B 显式禁止字节码写入, 不依赖会被 -I 忽略的 PYTHONDONTWRITEBYTECODE. 显式加载固定依赖目录, 不加载工作区启动模块, .pth, PYTHONPATH 或用户 site-packages. -I 负责导入隔离, 自写 guards 负责 Python 网络 API 拦截, 两者都不等于操作系统断网

可信入口只先加载请求解析和安装补丁所需的标准库/执行器模块, 补丁只影响本次 worker. 模型不获得交互 stdin, input 不加入公开 builtins; 父进程关闭请求写端后其他 stdin 读取也只会遇到 EOF, 不等待用户输入

源码与请求不拼接进 Shell 命令, 不使用 shell=True, 不写入模型可修改的临时启动脚本. 所有协议与输出按 UTF-8 处理

## 4. 7 种操作的逐项行为

| 操作 | worker 内部行为 | 保留结果 |
| --- | --- | --- |
| `run` | 校验源码导入, 受限编译后执行 | stdout, stderr, returncode |
| `run_file` | 检查路径, 在 worker 内读取源文件, 使用 run 的同一检查与执行流程 | 同上及 path |
| `save` | 校验目标路径, 创建父目录, UTF-8 写入给定内容 | operation, path, status |
| `read` | 校验路径, 读取文本 | operation, path, content, status |
| `delete` | 校验路径, 删除指定文件 | operation, path, status |
| `delete_dir` | 校验路径, 递归删除指定子目录, 根目录禁止删除 | operation, path, status |
| `list` | 在允许目录下递归列出文件, 不跟随越界链接 | operation, path, files |

产物限制: read 继续只返回 UTF-8 文本, 不增加二进制/base64 读取模式. PNG/SVG/PDF/DOCX/PPTX/XLSX 可保存到 sandbox, 路径可由脚本输出或 list 查询, 不代表已经在 Chat 展示或发送到群聊. 群聊现可显式调用 group_chat_import_sandbox_image 登记当前会话的 PNG/JPEG/WebP/GIF, 再使用 asset.asset_id 准备群回复; 此入口属于群聊插件, 不改变沙箱 7 种操作, 不自动发送或开放通用附件

save 只保存内容, 不审批内容, 不检查将来是否执行. 因此保存含禁止 import 的脚本可成功, 但之后的 run_file 必须拒绝该脚本

run_file 执行的是本次读取到的源码, 设置可供诊断的文件名与受控 __file__, 相对文件访问以 sandbox 根为准, 不把脚本目录加入 import 搜索路径. 首版不允许从 sandbox 导入自定义 Python 模块, 防止 run_file 通过邻接文件绕过源码策略

继续拒绝路径穿越, 越界绝对路径, 删除根目录和越界 symlink/junction. 对无法可靠处理的 Windows 设备路径/重解析点等形态直接拒绝. 运行中的 Python 文件访问通过受控 open 与已登记库 API 校验, 不只检查 operation.path; 此约束没有操作系统文件边界兜底, 不能保证抵御链接竞态或原生代码绕过

## 5. 库白名单的明确语义

### 5.1 白名单限制谁

白名单指模型代码可直接导入和访问的公开模块, 不是“父应用环境里只安装这些包”

分为三类固定集合:

| 集合 | 来源 | 是否向模型开放 |
| --- | --- | --- |
| 执行器内部模块 | worker 实现需要的 os, sys, pathlib 等 | 否 |
| 公开模块 | 管理员从已支持模块中选择 | 是, 受公开 API 策略限制 |
| 固定内部依赖 | 公开模块经过验证的依赖清单 | 不因是依赖而自动公开 |

可信模块内部依赖必须事先固定并验证, 不根据模型代码临时扩大. 库自身延迟加载了未登记的依赖时返回依赖配置错误, 不自动补白名单. 不用“调用栈看起来来自某个库”来授权模型的 import

这意味着库白名单是模型可用能力的白名单. 如果要求解释器内部任何模块也必须同时出现在模型公开白名单, 常用库及 worker 本身将难以工作; 本方案明确不采用这种混合语义

### 5.2 配置形式和首版模块

新增 `sandbox_allowed_modules` 字符串配置, 使用逗号分隔完整模块名. 缺省采用内置默认值, 显式空字符串表示不允许 import, 不把空值解释为全部放行

工具实例构建时从同一份解析后的生效白名单生成可用库描述, 同步/异步共用描述生成函数, 不维护写死的第二份库列表. 配置生效并重建工具时同步更新描述, 不增加每次调用探测或独立描述缓存

标准库默认值为以下集合; 按后续用户要求加入 time:

```text
math,statistics,decimal,fractions,json,csv,re,datetime,time,collections,itertools,functools,random
```

time 公开读取时间, monotonic/perf_counter, 时间格式转换和 sleep 等接口. sleep 计入整次调用超时, 不能用长 sleep 保留后台进程; 不开放 clock_settime/clock_settime_ns 等修改系统时钟的接口. 依据: [time 官方文档](https://docs.python.org/3/library/time.html)

按后续用户要求, 将 NumPy, pandas, matplotlib.pyplot 和 Word/PPTX/XLSX/PDF 处理加入首版目标, 使用第 5.5 节的受限支持档案. 这些库不因宿主已经安装就自动开放; 每个档案必须通过受限运行时验收后才能列入有效白名单. 直接公开 PIL 暂不纳入本轮, Pillow 可作为绘图/文档/图片嵌入的内部依赖

配置只接受已验证模块, 不接受 `*`, 任意路径, `包名.*` 或自动允许所有子模块. 子模块按支持档案显式登记, 比如允许包名本身不会自动允许该包的任意辅助工具

os, sys, subprocess, socket, ctypes, importlib, inspect, builtins, pickle, marshal 等不进入首版公开集合. 配置写错或请求尚未支持的库返回 configuration_error, 不加载后再尝试修补

### 5.3 如何拦截

建议使用受维护且兼容目标 Python 的 RestrictedPython 版本, 锁定依赖, 为 run/run_file 提供相同的编译与运行策略. 不自行用几条正则或 AST 节点过滤声称实现完整限制

分两层检查:

1. 编译前检查 import/from-import, 碰到非公开模块立即拒绝整段源码. 这是保守检查, 即使 import 位于不会执行的分支也拒绝, 保证这类错误发生前不执行前面的代码
2. 执行环境使用受控 import, 受限 builtins 与属性读写守卫. 对动态导入, 已加载模块引用, 私有属性/函数 globals/栈帧等访问进行限制, 不暴露原始 worker globals 或完整 sys.modules

模型侧不开放 eval/exec/compile/importlib 和原始 worker globals, 不将完整模块内部依赖作为公开能力. 对已登记的 API 和对象使用所需的局部适配, 不为全部第三方对象构建通用代理或全图审查机制

静态禁止导入在执行前返回错误. 动态导入/API/网络/进程守卫在登记的调用发生前拒绝, 记入本次首次违规并抛出普通策略异常; 汇总结果时有记录就返回错误, 防止代码捕获异常后误报成功

不增加 fatal 退出专用路径, 不要求每次违规都不可捕获或立即终止进程. 已捕获异常后仍可能执行其他允许语句, 违规前后的文件副作用不回滚; 无限循环等由整次调用超时终止. 该机制用于结果准确性, 不承诺防止恶意代码篡改内部状态

RestrictedPython 需要显式配置 builtins/import/属性守卫, 本身不提供系统隔离, 与猴子补丁组合也不构成恶意代码的安全边界. 依据: [官方策略文档](https://restrictedpython.readthedocs.io/en/stable/usage/policy.html), [基本用法](https://restrictedpython.readthedocs.io/en/stable/usage/basic_usage.html)

### 5.4 Python 兼容性边界

普通运算, 循环, 函数, 容器, 异常与受支持模块应可使用; 不承诺完整 CPython 的反射, 动态加载或任意第三方库兼容性. 受限语法返回 code_not_supported, 不能暗中改用普通 exec

为代码提供限定 sandbox 范围的 open, 常规读写与 with 用法保持可用; 不开放任意文件描述符, closefd=False 等借用宿主资源的模式. 执行器内部业务文件操作不经过模型可修改的 open

受限编译器采用 RestrictedPython 8.5, 已完成 Windows/Linux 真实脚本回归. 不支持的脚本应回报并修订支持范围, 不能用取消守卫来补兼容

### 5.5 数值分析与文档库的扩展名单

扩展库开放的是下面列出的功能子集, 不是整个包的全部公开对象. Windows/Linux 已验证数值分析, 绘图与文档业务闭环, 不代表下表每个第三方 API 都逐项测试. 精确模块与入口以 models.py 的模块集合及 policy.py 的公开导出/调用规则为准, macOS 尚未验收

| 用途 | 安装包 / 主要导入名 | 首版目标 |
| --- | --- | --- |
| 数值运算 | numpy / numpy | 数组, 切片, 聚合, 常见数学/线性代数/随机运算, 安全数组文件读写 |
| 表格分析 | pandas / pandas | DataFrame/Series, 筛选, 分组, 合并, CSV/JSON/XLSX 读写 |
| 绘图 | matplotlib / matplotlib.pyplot, 常用别名 plt | 折线/柱状/散点/直方图, 子图, 标签/图例与 PNG/SVG/PDF 静态文件输出 |
| Word | python-docx / docx | DOCX 读取与生成, 段落, 表格, 图片, 常用样式与保存 |
| PowerPoint | python-pptx / pptx | PPTX 读取与生成, 文本, 表格, 图片, 常用形状, 图表与保存 |
| Excel | openpyxl / openpyxl | XLSX 读写, 工作表, 单元格/公式文本, 样式与常用图表; 不执行公式 |
| PDF 页面处理 | pypdf / pypdf | 读取, 文本提取, 页面合并/拆分/旋转与写出 |
| PDF 表格提取 | pdfplumber / pdfplumber | 文本, 表格与页面信息提取, 复用项目已有依赖 |
| PDF 生成 | reportlab / 显式登记子模块 | canvas 及常用段落/表格排版, 本地字体, 图片与 PDF 输出 |

文档库接口依据: [python-docx](https://python-docx.readthedocs.io/en/latest/api/document.html), [python-pptx](https://python-pptx.readthedocs.io/en/latest/user/presentations.html), [openpyxl](https://openpyxl.readthedocs.io/en/stable/), [pypdf 页面合并](https://pypdf.readthedocs.io/en/stable/user/merging-pdfs.html), [pdfplumber](https://github.com/jsvine/pdfplumber), [ReportLab PDF 生成](https://docs.reportlab.com/reportlab/userguide/ch2_graphics/)

支持档案中的完整导入名已加入默认配置. 常用子模块明确登记 docx.shared/docx.enum.text/docx.enum.table/docx.enum.section, pptx.util/pptx.enum.text/pptx.enum.shapes/pptx.dml.color, openpyxl.styles/openpyxl.utils/openpyxl.chart, numpy.linalg/numpy.random. ReportLab 登记 reportlab.pdfgen.canvas, reportlab.lib.pagesizes/units/styles, reportlab.platypus, reportlab.pdfbase.pdfmetrics/ttfonts; 父模块仅提供受控导航, 不因此开放其所有成员

这些登记仍遵守 exact 模块匹配, 不增加任意包名通配. 只对首版登记的功能和必要对象接口做适配, 同一入口的普通别名调用沿用相同规则; 不承诺审查整个依赖对象图或抵御刻意构造的反射访问

绘图显式登记 matplotlib.pyplot, 不要求模型使用特殊别名, `import matplotlib.pyplot as plt` 与普通导入使用相同策略. 首版提供常见 pyplot 与 Figure/Axes 操作; matplotlib 其他公开子模块需另行登记, 不因 pyplot 可用而全部放行

worker 在首次导入 pyplot 前固定使用无界面的 Agg 后端, MPLCONFIGDIR 与缓存放在当前执行的私有目录, 不读取 sandbox 中模型自建的 matplotlibrc. PNG/SVG/PDF 由固定登记的静态输出实现完成, 不自动选择 GUI/WebAgg/PGF 或第三方 backend. 依据: [Matplotlib 后端](https://matplotlib.org/stable/users/explain/figure/backends.html), [配置与缓存目录](https://matplotlib.org/stable/install/environment_variables_faq.html)

典型用法仍为普通绘图代码, 不增加工具 operation:

```python
import matplotlib.pyplot as plt

fig, ax = plt.subplots()
ax.plot([1, 2, 3], [2, 4, 3])
ax.set_title("Example")
fig.savefig("chart.png", dpi=150)
plt.close(fig)
```

### 5.6 扩展库的固定限制

| 库 | 必须限制的接口/参数 |
| --- | --- |
| time | 禁止修改系统时钟, sleep 仍受 worker 总超时控制 |
| NumPy | 禁止 ctypeslib, f2py, 动态库加载和 ndarray.ctypes 等指针接口; np.load 强制 allow_pickle=False, 禁止 pickle/object 序列化与任意内存映射入口; 文件只在 sandbox 内 |
| pandas | 禁止 read_pickle/to_pickle, pandas.eval, DataFrame.eval/query 和原生内存/动态执行间接入口; 禁止 clipboard, SQL, HDF 等未登记后端; 路径参数不接受 URL/远程存储协议, XLSX 固定 openpyxl 引擎 |
| matplotlib.pyplot | 固定无界面后端, 禁止 show/GUI/网页服务, use/switch_backend 及 module:// 自定义后端; text.usetex 固定 False, 禁止 PGF/外部 LaTeX/动画编码器; 输出/图片/显式字体文件路径受限, 系统字体目录按第 6.5 节归为只读运行时资源, 拒绝 URL 样式与任意后端参数 |
| DOCX/PPTX | 输入文档, 输出文件, 图片, 模板和字体等外部资源都限定允许目录; 不公开原始 XML 解析器/任意 XPath 扩展/内部模块透出, 不启动 Office/LibreOffice/COM |
| openpyxl | 首版只处理 XLSX, 不保留 VBA, 不解析/获取外部链接目标, 禁止读取外部实体; 使用固定安全 XML 解析策略 |
| pypdf/pdfplumber | 只开放读写/提取接口, 不运行文档脚本或外部渲染程序; 保留库的解析保护参数, 受输入规模与总超时限制 |
| ReportLab | 只开放确定的 canvas/排版/字体接口, 禁止 DocExec/DocAssign/DocIf/DocWhile 等再次执行或解释表达式的编程 flowable, 禁止外部资源 URL |

对上表已登记接口与参数执行检查, 使用局部包装/属性守卫保留普通别名调用的约束, 不只匹配源码里的函数名. 不开放的登记接口返回 api_not_allowed, 禁止参数返回 argument_not_allowed; 不扩展为识别全部第三方对象身份的通用执法框架

依据: pandas 官方明确提示 [query 可执行任意代码](https://pandas.pydata.org/docs/reference/api/pandas.DataFrame.query.html), [read_pickle 可载入不安全对象](https://pandas.pydata.org/docs/reference/api/pandas.read_pickle.html); NumPy 说明 [load 的 pickle 风险](https://numpy.org/doc/stable/reference/generated/numpy.load.html), [ctypeslib 的动态库接口](https://numpy.org/doc/stable/reference/routines.ctypeslib.html); ReportLab 的 [编程 flowable](https://docs.reportlab.com/reportlab/userguide/ch8_programming_flowables/) 包含表达式与语句执行能力

绘图配置限制同时覆盖 rcParams/rc_context/样式与 Artist/Text 设置, 并在渲染前验证有效状态; 不能只在启动时设置一次 usetex=False 就允许模型随后开启. Matplotlib 的 [LaTeX 渲染](https://matplotlib.org/stable/users/explain/text/usetex.html) 涉及外部程序, 本轮仅支持内部文本/数学排版与已授权的本地字体. savefig 的 format/backend 参数固定在登记的静态输出集合, 不能借 [savefig 的后端覆盖参数](https://matplotlib.org/stable/api/_as_gen/matplotlib.pyplot.savefig.html) 绕过

read_csv/read_json/read_excel/Document/Presentation/PdfReader/Canvas 等库内部通常自行打开文件, 因而不能只包装 builtins.open. 支持档案必须校验所有输入/输出及次级资源路径. 对文件类对象只接受受控 open/内存流返回的对象, 不接受任意整数文件描述符. 未适配的内部文件入口存在漏检风险, 网络/进程补丁不替代这些适配, 也没有系统文件边界兜底

DOCX/PPTX/XLSX 按现代 Office Open XML 格式支持, 本轮不承诺旧 DOC/PPT/XLS 或格式转换. PDF 文本提取不包含扫描件 OCR, 生成 PDF 不等于将 Office 文档直接转换为 PDF

文档与数组输入不因文件位于 sandbox 内就可信. 文档 ZIP/XML 的展开大小/项数与 PDF 页数/解码大小需要受控, 不关闭解析器安全限制来兼容异常输入. openpyxl 官方推荐额外安装 [defusedxml](https://openpyxl.readthedocs.io/en/stable/); 它及 lxml/xml/zipfile 等属于固定内部依赖, 不因此进入模型公开库名单

### 5.7 依赖与受限运行时验收

2026-10-15 已复核 Windows Python 3.13.5 依赖并实测: RestrictedPython 8.5, NumPy 2.1.3, pandas 2.2.3, matplotlib 3.10.0, python-docx 1.2.0, python-pptx 1.0.2, openpyxl 3.1.5, pypdf 6.20.1, pdfplumber 0.11.10, reportlab 5.0.1, XlsxWriter 3.2.9, defusedxml 0.7.1. Linux Python 3.13.16 的独立测试环境采用相同直接依赖版本, 间接依赖按平台解析

requirements.txt 与 setup.py 的 sandbox extra 已固定上述直接依赖版本, all extra 包含 sandbox, 便携版依赖入口继续使用 all. 字体取宿主已有资源, 不自动下载. [python-pptx 依赖说明](https://python-pptx.readthedocs.io/en/latest/user/install.html). 网络/进程补丁不引入 hermetic 或其他第三方库, 工具运行时不现场 pip 安装. 完整便携版构建尚未验证

NumPy 的 C 扩展, pandas 的索引器, 文档对象及图像/字体内部依赖会增加受限适配工作. 必须验证实际操作, 不能仅以 import 成功作为支持依据:

- time 的计时/格式转换正常, 超长 sleep 能被终止
- ndarray 索引赋值/切片/ufunc/linalg 与 DataFrame.loc/iloc/布尔筛选/赋值/groupby/merge 正常, 危险对象属性仍被阻断
- pyplot 与 Figure/Axes 的常见绘图, 本地字体, PNG/SVG/PDF 保存正常, 中文/图例/子图可用; 多次调用不保留图形状态, 结束时释放图形资源
- pyplot, pandas.plot 及其返回 Axes 适用相同的后端/路径/LaTeX 限制, savefig 后端覆盖与 rc_context 不能绕过, 不弹窗/不监听端口/不启动外部程序
- CSV/JSON/XLSX 读写及 pandas 与 openpyxl 交互正常, 不会自动选择其他可选引擎
- DOCX 段落/表格/图片/样式写出后可重新打开, PPTX 文本/形状/图片/图表写出后可重新打开
- pypdf 页面合并拆分, pdfplumber 表格提取, ReportLab 含本地字体与表格的生成闭环正常
- 恶意 pickle, URL 输入, 越界次级资源路径, NumPy 指针入口, pandas/ReportLab 二次执行接口均被阻断
- 白名单库内部依赖未被错误暴露, 且首次导入/操作仍处于同一调用的超时与自写网络补丁范围

若某档案在所选 RestrictedPython/自写补丁组合中不可兼容, 报告该模块未通过验收并修订, 不退回 unrestricted exec 或静默开放整包. 支持这些库使实际适配工作多于“配置里多写几个包名”, 但不增加工具种类或审批机制

## 6. 自写猴子补丁与文件限制

### 6.1 选型和保证范围

网络/进程拦截由执行器 guards.py 使用 Python 标准库实现, 仅在一次性 worker 内安装. 不引入 hermetic, 不开发 AppContainer/LPAC, seccomp/Landlock 或 Seatbelt 后端, 不需要 Docker, Rust helper, 过滤驱动或全局防火墙规则

本方案防止普通模型代码意外联网或调用外部程序. 独立 worker 避免修改 Satrap 主进程的 Python 全局状态, 但不会降低 worker 的操作系统账号权限. 库白名单, RestrictedPython, 路径与业务 API 校验仍分别负责自己的限制

网络/进程补丁是应用层约束, 不承诺整个进程不可绕过地断网或系统级文件隔离. 原生扩展直接调用系统 API, 已保存的原始函数引用, 反射/补丁恢复等路径仍可能绕过, 自写补丁不改变这一边界

### 6.2 补丁覆盖范围

固定禁网且不提供配置豁免, 不需要实现域名列表, localhost 放行, 代理策略或动态切换. 补丁注册表记录目标模块/属性, 原始类型及拦截类别, 根据已支持 Python 版本与平台可用接口安装

| 入口 | 拦截要求 |
| --- | --- |
| socket 创建/恢复 | 阻断 socket.socket 与 SocketType 的常规创建入口, 以及 fromfd/fromshare 等从已有句柄构造入口; socketpair 首版也不开放 |
| 连接与发送 | 覆盖公开类上的 connect/connect_ex, send/sendall/sendto 及可用的 sendmsg 等入口, 不只封 TCP connect 而遗漏无连接 UDP |
| 监听与接收 | 覆盖 bind/listen/accept 与接收入口, 不放行入站/localhost 服务 |
| DNS/名称解析 | 覆盖 getaddrinfo/gethostbyname/gethostbyname_ex/gethostbyaddr/getnameinfo 等入口, 防止名称查询产生网络访问 |
| 便捷与包装接口 | 覆盖 create_connection/create_server, 检查 SSL/HTTP/异步标准库路径最终进入已登记守卫; 不为每个第三方 HTTP 库各写一套补丁 |
| 外部程序 | 覆盖 subprocess.Popen 及 run/call/check_call/check_output 等公开入口, os.system/popen 与可用的 spawn/exec/fork/forkpty/posix_spawn 系列 |
| Windows 进程入口 | 登记 _winapi.CreateProcess 与 os.startfile, 覆盖 multiprocessing 的直接 spawn 与 ShellExecute 关联程序/浏览器启动, 不假定这些路径经过 subprocess.Popen |
| 间接进程启动 | 验证 asyncio.create_subprocess_* 与 multiprocessing 等常规路径被底层守卫拒绝; 模型不能通过它们绕过公开白名单 |

实际目标清单在实施前置验证时按 Python 版本形成, 上表是需要覆盖的能力, 不是声称所有 CPython C 类型都能原地改写. 不强行给不可写的 _socket 原生类型设置属性; 使用保留 socket 类结构的受控子类/方法包装, 同步处理公开别名, 不将 socket.socket 粗暴替换为普通函数破坏 ssl 继承和类型检查. subprocess 的类入口同样保留必要类型结构

_socket/_ssl/_winapi/_posixsubprocess/posix/nt 等底层模块不公开给模型. 公开层补丁不能等同于覆盖这些原生模块的全部入口; 允许库可使用固定内部依赖, 直接原生系统调用仍属于剩余边界. 缺失的平台可选 API 可不登记, 但必需入口存在却无法正确拦截时拒绝初始化, 不静默跳过

worker 协议使用匿名管道, 不依赖网络 socket 或 socketpair. 首版模型业务不承诺自行创建 asyncio 事件循环或多进程任务, 避免为内部自唤醒 socket 开放额外通道; 数值库自身的计算线程不因而禁止

依据: [socket 官方 API](https://docs.python.org/3/library/socket.html), [subprocess 官方 API](https://docs.python.org/3/library/subprocess.html), [os 进程接口](https://docs.python.org/3/library/os.html#process-management)

### 6.3 安装时机与业务兼容

可信启动代码先建立内部策略状态, 加载安装补丁需要的标准库, 随后安装全部固定补丁并检查安装状态, 再导入 RestrictedPython 与业务库. 不允许业务库先捕获未包装函数后再补装, 不在 Satrap 主进程打补丁

每个 worker 只安装一次, 存续到退出, 不提供公开解除/临时放行接口. 原始对象如为实现类兼容而必须保留, 仅留在执行器内部, 不传入模型 globals, 不把“不公开”宣称为对抗反射的安全保证

每次初始化检查必需补丁和别名安装状态, 失败返回 sandbox_setup_failed, 不执行业务. 实际 DNS/回环连接/进程启动拒绝探测集中放在 Phase A 和集成测试, 不要求每次冷启动都重复网络探测或额外握手

保留已登记 NumPy/pandas/文档/绘图库的原生扩展, 不增加全局原生扩展禁用补丁. 模型白名单继续拒绝 ctypes/cffi/_socket 等模块及危险属性. 业务库导入和实际操作必须验证, 如字体探测或缓存生成试图启动外部程序, 选择已验证的纯 Python/进程内路径或明确报不支持, 不临时恢复原始函数

pyplot 可信导入期间, 对固定的 fc-list/system_profiler 字体发现查询返回空结果, 使用固定目录的进程内字体扫描, 随后恢复原有阻断守卫. 此过程不恢复未受限的进程启动函数, 其他查询仍拒绝. 优先选择已有中文字体, 无可用字体时回退默认字体, 不承诺所有环境都有中文字形

不拦截执行器自己需要的 compile/exec, 不全局禁止文件写入, 不扩大为全套 Python 行为防火墙. 语言与文件访问继续由第 5 节策略负责; 网络/进程补丁无需新增外部依赖, 不表示删除 RestrictedPython 或业务依赖

### 6.4 策略错误与结果优先级

guards.py 自有类型化异常携带 error_code 与 action, 可选包含被阻断的 API 名, 不依赖外部库异常或 stderr 字符串猜测. 错误信息不回显请求密钥或完整敏感参数

每个拒绝函数先记入 worker 内部的首次违规记录, 再抛出策略异常, 不调用原始网络/进程函数. 记录不放入模型 globals, 只保存固定类别与清理后的动作信息, 不因模型反复尝试而无界增长

- 网络调用返回 network_disabled, 外部进程调用返回 process_not_allowed
- 即使模型 try/except 捕获异常, 本次动作仍未执行; worker 汇总结果时看到违规记录就返回策略失败, 不返回成功
- 模型未捕获时同样按已记录类别返回, 不伪装为普通代码错误
- 未发生已记录违规的普通代码异常按原执行失败处理, 其他自有策略拒绝使用 policy_violation
- 导入/API 的动态违规使用同一首次记录机制, 后续成功不能覆盖; 静态禁止 import 在模型语句执行前返回, 不使用额外强制退出路径

策略异常被模型捕获后可能继续运行其他允许语句, 因此不承诺第一次违规后立即终止整段源码, 也不承诺回滚之前/之后的文件副作用. 总超时仍覆盖整个 worker

### 6.5 文件访问和环境

7 操作的路径检查, 受控 open 以及已登记库的文件参数校验保留. 允许业务读写当前 sandbox 根及本次私有临时目录, 不将项目根/其他会话/配置/凭据路径加入业务允许集合. 解释器及依赖仍按宿主账号权限读取所需运行时资源. 解释器目录及标准库 ZIP 路径, 固定系统时区/MIME 文件和字体目录归入只读运行时资源, font.family 的普通字体选择不逐次拦截; 模型显式指定的字体文件路径仍走业务路径检查

网络补丁不承担文件隔离, 不把运行目录设为 sandbox 当作路径保护, 不全局开启只读后再绕过补丁执行 save/delete. 逐项校验文档模板, 图片, 字体和数组数据的次级路径, 对未验证 API 不开放. 库内部漏检, 链接竞态和原生扩展仍可能绕过应用层文件检查, 没有内核规则兜底

清理敏感环境变量, 移除启动/导入控制变量, 将缓存和临时文件放在受控私有目录. 兼容 allowed_env_vars 中的非敏感允许项, 显式放行密钥或覆盖启动/导入控制的配置返回 configuration_error. 不继承不必要的描述符/句柄或已打开网络连接, 不把协议通道交给模型

### 6.6 跨平台和进程回收范围

三平台复用 worker/guards/policy, runner 只处理标准子进程生命周期和平台必要的终止差异. 不引入独立原生隔离后端或常驻监督器

超时/取消/正常关闭时, 父进程显式终止并等待自己创建的 worker, 必要时强制结束, 不只取消等待协程. 不为宿主异常退出新增心跳/监控管道或监督器, 父进程崩溃后的自动回收不作为首版保证. 补丁拦截常规子进程启动, 不保证回收绕过后自行创建的后代

Windows/Linux/macOS 都列为目标, 分别验证 Python/依赖安装, 标准库补丁覆盖与回收路径. 跨平台共用代码不等于已在所有系统实测, 未测试版本明确标为未验收

所有 7 操作都必须在已启用固定补丁的新 worker 中执行. 必要运行时/依赖缺失或补丁初始化失败统一报错, 不因 read/save 看似低风险而切回主进程

## 7. 协议与错误结果

请求采用固定版本的 JSON, 只传 operation, code/path, 受信根目录, 库配置与执行参数. 主进程和 worker 都严格解析为枚举/dataclass/TypedDict, 不在内部流程到处传 Any

使用标准 stdin/stdout/stderr, 不新增 Windows 第三控制管道或句柄传递协议. 请求通过 stdin 写一次并关闭, worker 以有界方式读取后解析. 初始化与业务代码的 Python 标准输出/错误由 worker 捕获进结果字段, stdout 最后只写一个 JSON 结果; 未捕获的底层 stderr 作为诊断处理

父进程严格解析完整结果与字段, 不从任意输出行中寻找看起来像成功的片段. 原生库直接写 stdout 等情况若破坏协议, 返回 worker_failed, 不按成功处理. 本协议解决常规 print 干扰, 不提供防伪控制帧/独立可信通道或对抗恶意 worker 的保证

保留原有成功字段, 新增失败的 error_code, 必要时包含 module 与 line. 例如:

```json
{
  "operation": "run",
  "returncode": -20,
  "stdout": "",
  "stderr": "模块 requests 不在允许列表中",
  "error": "模块 requests 不在允许列表中",
  "error_code": "module_not_allowed",
  "module": "requests",
  "line": 1
}
```

新增 returncode 数值仅作拟议示例, 实施时集中定义并避开已有错误码. 工具层必须保留 error_code, 不能像当前适配一样只截取 stdout/stderr 而丢失策略失败信息

| error_code | 含义 |
| --- | --- |
| module_not_allowed | 模型使用白名单之外模块 |
| api_not_allowed / argument_not_allowed | 允许库内未开放的接口或参数 |
| code_not_supported | 不支持的语法或动态执行能力 |
| library_dependency_error | 允许模块的固定依赖不满足 |
| path_outside_sandbox | 路径越界 |
| network_disabled / process_not_allowed / policy_violation | 自写守卫记录的网络/进程/其他策略违规, 优先于成功结果返回, 不靠 stderr 分类 |
| sandbox_unavailable | 必要 Python 运行时或受限执行依赖不可用 |
| sandbox_setup_failed | 本次补丁安装或策略初始化失败, 不执行业务 |
| configuration_error | 模块名/根目录/运行时或环境配置无效 |
| timeout / cancelled | 执行超时或取消 |
| output_limit_exceeded / worker_failed | 结果超限或子进程/协议异常 |

普通系统错误作为执行失败返回, 自写守卫携带确定的错误类型与动作标识, 不伪造 network_disabled. 所有错误都不引起审批/自动安装/网络放开/换宿主运行

## 8. 生命周期, 输出和文件副作用

- 默认沿用 30 秒超时, 覆盖 worker 启动, 文件操作与代码执行全过程; 继续支持现有执行器 timeout 参数
- 同步超时, 异步取消和会话关闭时显式终止并等待本次 worker, 必要时强制结束; 不只取消 asyncio.to_thread 的等待
- 验证正常关闭时回收 worker; 按第 6.6 节不为宿主崩溃新增自动回收体系
- run 输出有界保存, stdout/stderr 各保留末尾 20,000 字符并标记截断, 达到上限后继续排空
- 请求/协议结果设 8 MiB 上限; read/list 超限返回明确错误, 不返回伪装成完整内容的截断文本
- 保留文件持久化, 不跨调用保留解释器状态, 不在启动时清空用户 sandbox
- 动态违规或超时前已发生的文件修改保留, 不承诺事务回滚; 静态禁止 import 在执行前拒绝
- 首版不承诺 CPU/内存/磁盘的硬配额, 不在 UI 或文档中宣传未实现的限制

超时与结果上限的具体默认值是实现前可调整的配置取舍, 不增加一整套资源管理 UI. 同根目录并发文件操作沿用普通文件系统语义, 不承诺跨进程事务或串行快照

## 9. 兼容与删除范围

工具名与 7 种 operation 不变, CodeSandbox 公开方法与常用成功返回保持兼容. 内部文件操作失败通过 worker 错误映射成原来的异常/错误返回, 不保留主进程业务 I/O 备用路径

移除 CodeSandboxTool/AsyncCodeSandboxTool 的 execution_authorizer 及内部调用, 同时更新仓库调用方与测试. 不保留一个会被忽略的授权参数制造误解; 外部直接调用该旧构造参数需要迁移, 变更记录明确标注

base_take.tools.utils 中仅用于审批的 `_call_user_input_provider`, `_execution_approved`, `_make_sync_execution_authorizer`, `_make_async_execution_authorizer` 删除. coding 的同名审批辅助函数属于另一模块, 不删除或合并

CodeSandbox 路径安全与业务实现移入 worker, 门面不复制一份规则. 默认 sandbox_root 与宿主会话绑定优先级不改; 独立调用者若显式共用同一根目录, 文件仍共享, 本次不自动迁移为私有目录

更新工具描述与相关文档, 通用操作文案保持固定, 可用库列表在工具构建时按生效白名单生成, 同步/异步共用实现. 明确 Python 网络 API 拦截, 无代码审批, 不保留变量和产物不自动送达的首版限制. 修正当前不支持操作的错误提示遗漏 read/run_file 的问题

升级后不再具有任意 CPython/全部已安装库的执行能力. 必要运行时/依赖或自写补丁固定策略不可用时原有工具会不可用, 不保留旧审批后任意宿主执行模式. 这两项兼容代价必须在发布说明中明示

## 10. 实施顺序与停止条件

| 阶段 | 交付 | 通过条件 |
| --- | --- | --- |
| A 前置验证 | 固定 Python/RestrictedPython/业务依赖, 建立补丁目标与兼容矩阵, 记录冷启动计时 | 网络/Windows 进程入口被拦截, socket/SSL 与业务闭环正常, 启动/导入/总耗时有实测记录 |
| B 统一 worker | 7 操作进入相同子进程路径, 单请求/单结果协议, 门面兼容 | 每次调用为新 PID, 补丁先于业务, stdin 写完即关闭, 无额外控制管道, 超时/取消终止 worker |
| C 白名单与免审批 | run/run_file 同策略, 简单违规记录, 生效白名单生成描述, 移除审批 | 禁止导入报错, 捕获策略异常不误报成功, 描述与运行策略一致, user_input_provider 从不调用 |
| D 发布与文档 | Python 依赖打包, 配置说明, 三平台集中回归 | 无 hermetic/原生后端/管理员或驱动部署需求, 文档明确应用层限制边界 |

A-C 已完成并通过 Windows/Linux 回归, D 的依赖与文档已更新, macOS 和完整便携版构建仍待验收. 已核对补丁入口, 早期安装与业务库兼容. 必需入口不能正确拦截或业务库不兼容时回报并修订, 不静默放行, 不自行恢复外部库/三平台原生后端

Phase A 分别记录简单标准库任务, pandas 和 matplotlib 绘图任务的新进程启动/策略初始化/首次导入/总耗时, 对照默认 30 秒总预算决定是否需调整超时, 不预设冷启动秒数. 不为此新增预热服务, 常驻内核或进程复用

不单独发布“先取消审批, 网络/库限制以后补”的中间状态. 补丁与语言/路径策略通过后才切换内置工具到免审批路径

## 11. 集中验收场景

测试整合进已有 coding security/configuration 流程模块, 真实 worker 场景集中组织, 不为 7 个操作分别创建小测试文件

| 场景 | 验收结果 |
| --- | --- |
| 文件生命周期 | save -> read -> list -> delete -> delete_dir 返回兼容结果, 每次为新 PID |
| Python 生命周期 | run 与 run_file 正常运算, 多次调用无变量/导入状态残留, 文件可持久保留 |
| 直接导入 | import/from/别名/子模块/条件分支中的非白名单导入拒绝, 错误包含模块名 |
| 扩展库操作 | time, ndarray/DataFrame, pyplot, DOCX/PPTX/XLSX/PDF 各完成第 5.7 节业务闭环, 不只验证 import |
| 扩展库限制 | 危险 API/参数通过别名和已支持对象方法访问仍被拒绝, 固定内部依赖不自动公开 |
| 间接入口 | 已知动态加载, 私有属性/函数 globals/栈帧, 模块透出和同名文件路径被拒绝; 不据此宣称消除所有绕过 |
| 导入/API 违规 | 静态检查执行前报错, 动态守卫记录后即使异常被捕获也返回错误, 不依赖强制退出 |
| 保存与执行 | 保存禁止库脚本成功, run_file 拒绝; 邻接模块不能绕过检查 |
| 网络补丁 | 在受控本地服务下验证 DNS/TCP/无连接 UDP/HTTP/HTTPS/回环与监听入口, 服务端确认未发生预期禁止的收发 |
| 别名与类型 | socket 公开别名/受控类覆盖一致, SSL 与登记业务库正常导入, 不使用普通函数替换导致继承错误 |
| 违规结果 | 未捕获/捕获自有策略异常均按内部记录返回错误, 未调用原始函数, 首次违规不会被成功结果覆盖 |
| 启动顺序 | 请求可提前发送, 但补丁/策略初始化失败时不执行业务, 业务库在 guard 后导入 |
| 宿主不受影响 | Satrap 及其他 Python 进程仍能联网, 主进程未安装补丁, 不修改系统防火墙 |
| 路径约束 | operation.path, 受控 open 与登记库的越界读写失败, 链接/重解析点和根目录删除保护有效 |
| 外部程序与退出 | subprocess/os 与 Windows _winapi.CreateProcess/os.startfile 均被拒绝, Windows multiprocessing 直接 spawn 不能漏过; 超时/取消/正常关闭后等待 worker 退出 |
| 协议与输出 | 中文 UTF-8, 有界业务输出进入 JSON, 完整结果解析, 协议损坏报错, stdin 关闭后无交互等待 |
| 解释器与启动 | 使用既定解释器路径与 -B, 不解析 PATH, 不依赖被 -I 忽略的字节码环境变量 |
| 描述与产物 | 描述与生效白名单一致; 图表/文档生成后存在于 sandbox, read 仅文本, 不承诺自动展示或发送 |
| 冷启动 | 记录标准库/pandas/绘图任务启动与首次导入耗时, 对照总超时, 不引入进程复用 |
| 字体资源 | 系统字体作为只读运行时资源可用, 显式字体文件仍按业务路径校验 |
| 无审批 | 7 操作均不调用 provider, 无 provider 也能执行合法请求 |
| 配置/故障 | 空白名单, 不支持模块, 运行时/受限执行依赖缺失, 必需 API 无法安装补丁, 初始化失败均明确报错, 无普通执行回退 |
| 作用域回归 | coding Shell 与审批策略不变, Chat 工具协调不变, 搜索与文档读取不受影响 |

使用临时 sandbox, 外部哨兵文件与受控本地网络服务, 不访问用户真实数据. 网络/进程入口通过可信测试入口或固定测试依赖探测, 不为测试扩大模型公开白名单. 不以禁止 import 或远端超时替代补丁生效的证据, 受控服务应有允许访问的基线校验

故障注入可以验证协议, 真实 worker/补丁/业务依赖仍须实测. Windows/Linux/macOS 分别报告结果, 未测试平台标为未验收. 验收证明已覆盖入口和兼容性, 不将原生扩展/反射等剩余绕过描述为已经隔离

## 12. 已确定路线与待验证细节

用户已确定保留 7 操作, 每次新 Python 进程, 自写猴子补丁拦截网络/外部进程, 库白名单, run/run_file 违规报错和免代码审批. 已完成实施, 实测范围和未验收项见第 12.1 节

| 选择 | 当前方案 | 影响 |
| --- | --- | --- |
| 网络/进程策略 | 标准库猴子补丁, 无本地/域名例外, 无公开卸载接口 | 不引入 hermetic, 三平台共用 Python API 约束, 不是内核隔离 |
| 生效时机 | worker 固定顺序先初始化补丁/策略, 再导入业务库与执行 | 无握手/额外控制管道, 初始化失败直接报错 |
| 拦截兼容 | 保留 socket/进程类必要结构, 显式处理别名和版本差异 | 维护责任在执行器, 不声称几行替换就覆盖全部标准库 |
| 语言约束 | 保留 RestrictedPython + 明确的导入/属性策略 | 独立负责白名单与动态能力限制, 仍需外部依赖和业务兼容验证 |
| 原生扩展 | 保留已登记库的固定原生依赖, 不全局禁用 | 保留 NumPy/pandas/文档/绘图库, 明确剩余原生绕过风险 |
| 文件访问 | 路径/受控 open/业务库参数校验, 不启用全局只读 | 保留 7 操作, 无系统文件隔离保证 |
| 默认白名单 | 标准库含 time, 增加第 5.5 节数值/文档档案 | 验收业务闭环后有效, 不是只写包名 |
| 内部依赖 | 固定依赖与模型公开模块分开 | 不把依赖当作模型可用模块 |
| 自定义模块 | 首版不支持 sandbox 内 import | 多步任务写在同一脚本, 或通过文件传递数据 |
| save 校验 | 只保存, 执行时检查 | 可保存任意文本, run_file 执行相同检查 |
| 策略错误 | 普通异常 + 首次违规记录, 最终优先返回错误 | 防误报成功, 不新增强制退出或防篡改机制, 不回滚文件副作用 |
| 不可用处理 | 7 操作统一失败 | 不保留主进程文件操作或审批后任意执行退路 |
| 生命周期 | 父进程负责超时/取消/正常关闭 | 不新增监控管道/监督器, 不保证父进程崩溃后的自动回收 |
| 产物 | 首版仅生成文件, 返回/查询路径 | read 不变为二进制通道, 自动展示/发送另接宿主资产能力 |
| 工具描述 | 构建时复用生效白名单生成可用库说明 | 不新增能力探测或第二份配置缓存 |

本文件覆盖旧大方案中 code_sandbox 的处理意见. satrap_coding 的实施计划继续暂停, 未修改其 Shell 或审批策略

## 12.1 实施与验收记录

日期: 2026-10-15. 未重启运行中的后端

| 环境/检查 | 结果 |
| --- | --- |
| Windows, CPython 3.13.5 | 迁移后 coding security/configuration 两个现有流程模块共 151 项通过, 27.67 秒 |
| Linux, WSL Ubuntu, CPython 3.13.16 | 迁移后真实 worker 集中子集 14 项通过, 33 项未选, 26.17 秒 |
| macOS | 未验收, 用户确认暂无可用环境 |
| 其他 Python 版本 | 未本地实测, CI 矩阵登记 3.11/3.13; 最低支持 Python 3.11 |
| CI | 已新增 Windows/Ubuntu/macOS 矩阵, 尚未触发运行 |
| 类型检查 | 迁移后涉及的 15 个生产文件 Pyright 0 错误, 0 警告 |
| 便携版 | 依赖入口已覆盖, 未执行完整构建验收 |

覆盖 7 操作的新进程/持久文件, 白名单与保存后执行, 输出/协议/缺失依赖, 真实网络/进程入口拒绝, 超时和异步取消回收, 生效配置与描述一致, 无代码审批, 数值/图表/Office/PDF 业务流程. Windows 生成图表的中文标题已经目视检查. Linux 验证发现并修复了只读时区/MIME/标准库 ZIP 资源与字体探测兼容问题. 这些结果不表示第三方库的全部接口均已测试

以下为单次新 worker 的实测秒数, 库导入计入业务耗时, 不提供性能保证:

| 环境 | 任务 | 策略初始化 | 业务含首次导入 | 总耗时 |
| --- | --- | --- | --- | --- |
| Windows | 标准库运算 | 0.0283 | 0.0009 | 0.2316 |
| Windows | pandas | 0.0324 | 0.4976 | 0.8385 |
| Windows | pyplot PNG | 0.0273 | 1.3829 | 1.7423 |
| Linux | 标准库运算 | 0.0736 | 0.0007 | 0.4798 |
| Linux | pandas | 0.0642 | 1.8889 | 2.4012 |
| Linux | pyplot PNG | 0.0682 | 2.5840 | 3.0569 |

当前继续沿用 30 秒整次调用超时. Linux 测试使用用户缓存目录下的独立虚拟环境, 未修改系统 Python 或系统防火墙

升级注意: CodeSandboxTool/AsyncCodeSandboxTool 的 execution_authorizer 构造参数已删除, 外部直接调用方需要移除该参数. 不再支持任意 CPython/所有已安装库, 不支持沙箱内自定义模块导入; 必要依赖不可用时 7 操作统一失败. 产物仅保存并返回/查询路径, 不自动展示或发送. 这仍是应用层防意外约束, 不是恶意代码的系统隔离边界
