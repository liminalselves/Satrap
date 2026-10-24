# base_take 插件: 基础能力集

`base_take` 是官方预设目录 (`satrap/expend/plugins`) 下的目录插件, 提供通用基础能力: 网页搜索、网页抓取、代码沙箱、文档与图片读取。与 `satrap_coding` 插件互补, 可独立或组合使用。

## 安装

```python
from satrap import SimpleSession

session = SimpleSession("conv-1", llm, db_path="chat.db")
session.install_plugin("./satrap/expend/plugins/base_take")

plugin = session.list_plugins()[0]
plugin.name          # "base_take"
plugin.tools         # 5 个工具
plugin.handlers      # 无记忆处理器
plugin.commands      # 无记忆命令
```

异步版 `AsyncSimpleSession` 同样支持 (`await session.install_plugin(path)`)。

## 能力清单

| 类别 | 能力 | 说明 |
| --- | --- | --- |
| 搜索 | search / fetch_page | 网页搜索与抓取 (复用 expend.tools.search) |
| 沙箱 | code_sandbox | 在隔离目录中执行 Python 代码 |
| 文档 | read_document / read_media | 读取文档、图片和已登记媒体，支持 PDF 分页及媒体时间区间 |

模型调用只要求 `search.query`、`fetch_page.url`、`code_sandbox.operation`、`read_document.path` 和 `read_media.media_id`。其余参数均可省略，默认值、枚举和范围以各自 JSON Schema 为准。

## 插件配置

meta.yaml 声明 `config_schema`, 支持以下配置项 (全局默认 + 按会话覆盖两级):

| 配置键 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| sandbox_root | path | 空 | 独立调用的兜底值; 平台运行时始终使用会话私有 sandbox |
| workspace_root | path | 项目根 | read_document 免审批工作区根, 外部单文件按宿主模式审批 |
| search_timeout | number | 10 | 整次搜索/网页读取的总时限秒数, 包括排队, DNS, 备用入口和重定向 |
| search_js_challenge | boolean | false | 可选轻量验证脚本, 仅支持带 renderData 的 Aliyun acw_sc__v2 内联模板; 需安装 web-js 可选依赖 |
| allowed_env_vars | string | 空 | 代码子进程显式放行的环境变量名, 多个名称用逗号分隔 |

安装时经 `install_plugin(path, config={...})` 传入会话级覆盖; 全局默认存于 `.satrap/config/plugins/base_take.json`。

## 网页搜索与读取

`search` 和 `fetch_page` 默认使用 `curl_cffi` 的 Chrome 请求配置, 不需要搜索 API Key 或安装浏览器. 请求头与 TLS/HTTP 指纹由同一个配置生成, 每次工具调用独享临时会话, Cookie 仅在该次调用内使用, 不读取日常浏览器资料或写入磁盘

两个工具继续使用已有公网地址限制. 每一跳都重新校验 DNS 并绑定实际连接地址, 禁止自动重定向到私网, 最多跟随五次重定向. 默认直接出站, 不继承系统代理环境变量. 每次响应的解压正文最多 8 MiB, 超限时立即停止下载; HTML 原始及解码后的 UTF-8 内容各最多 2 MiB 才进入正文提取, 超出返回 `extraction_limit`. `max_length` 仅控制返回给模型的正文字符数

同一进程中的会话共享站点限流: 每站点最多一个进行中的请求, 前一请求结束后至少等待一秒再发起下一请求. Bing 的中国与国际入口按同一站点管理, 仅连接错误或 5xx 允许在剩余总时限内尝试一次备用入口. 明确的 403 或验证页面冷却 60 秒, 429 优先遵守 `Retry-After`, 缺失或无效时冷却 60 秒; 冷却期间直接返回原因和剩余等待时间

搜索成功结果缓存 60 秒, 最多 128 条且总容量不超过 8 MiB, 仅保存在进程内存. 查询词与结果数量共同作为键, 不缓存失败结果, 网页原文或 Cookie. `fetch_page` 的提取正文另外保存 5 分钟的续读快照, 不作为按 URL 复用的下载缓存. 多个后端进程各自维护限额, 不跨进程协调

搜索成功仍返回 `title/url/snippet` 列表, 明确无结果返回空列表. 网页成功保留 `url/title/content/status_code`, 并附带正文来源及分页信息. 失败保留 `error` 和 `url`, 追加 `code`, 有 HTTP 响应或冷却信息时附带 `status_code` 和 `retry_after`. 常见类别包括 `access_denied`, `rate_limited`, `verification_required`, `parse_error`, `timeout`, `connection_error`, `unsafe_url`, `response_too_large` 和 `redirect_limit`. 未识别的搜索页面不能证明无结果; 明确验证页也不会作为正文返回

浏览器请求配置只能改善部分客户端特征导致的拦截, 不保证解决出口 IP 限制, 登录要求或 JavaScript 验证. 默认不执行网页脚本, 开启下述可选能力后也只执行已支持的验证模板, 不提供通用浏览器渲染. 失败时应换用其它公开来源, 不把搜索摘要冒充已读取的全文

### 可选轻量验证脚本

在运行服务的同一个 Python 环境安装可选依赖:

```sh
python -m pip install ".[web-js]"
```

然后在 base_take 插件配置中开启 `search_js_challenge`, 或独立创建 `FetchPageTool(js_challenge=True)` / `AsyncFetchPageTool(js_challenge=True)`. 模型工具参数不变, 模型不能通过 tool call 自行开启该选项. 仅安装依赖不会启用功能

当前使用 `mini-racer>=0.14.1,<0.15`, 仅在独立 Python 子进程中加载 V8. 宿主只读取依赖版本, 不导入 V8 到服务进程, 不启动 Chromium, WebView 或 Playwright. 未开启或读取普通网页时不创建该子进程. 可选依赖缺失时返回 `js_runtime_unavailable`, 不自动安装或静默降级为成功

`fetch_page` 开启后只识别带 `meta[name=aliyun_waf_aa]` 与 `textarea#renderData` 的已支持内联验证模板, 页面中有可读文章内容时不启动验证. 外部脚本, 其它模板和通用前端应用不在本轮范围内. 现有 403/429 仍直接失败并冷却, 不借脚本层解除拒绝

处理流程是: 公网 HTTP 响应 → 识别模板 → 运行少量 DOM 数据/Cookie 接口 → 校验 Cookie 与重载意图 → 使用同一临时 HTTP 会话对原地址重取一次 → 正文提取和快照续读. 重取继续检查 DNS 和实际目标, 遵守限流, 重定向上限及同一个 `search_timeout`. 重取仍是验证页时返回 `verification_required`, 冷却 60 秒, 不继续循环

JS 不持有 Python 网络, 文件或 Shell 回调, 不接管网络请求, 不导入个人浏览器或登录资料. 父进程只接受一个 `acw_sc__v2` Cookie, 限原主机而不扩大到子域, Cookie 仅在本次调用中保留. 页面提出的其它跳转不执行. 验证数据和混淆片段不会作为 fallback 正文返回, 关闭选项时该验证页也会明确报错

每进程最多一个 JS worker, 不排入无界任务队列. 原始源码合计最多 256 KiB, 最多三段内联脚本, renderData 最多 4 KiB, JSON 输入最多 512 KiB, 结果协议最多 8 KiB. JS 执行共享 2 秒预算, 子进程最多 5 秒且不超过工具剩余时间, 超时或取消后先终止并回收真实进程, 再释放名额. V8 堆预算为 64 MiB, 不等同子进程的总内存硬上限; 子进程用于故障隔离, 不宣称是操作系统级沙箱

其它失败包括 `js_challenge_unsupported`, `js_challenge_limit`, `js_execution_timeout`, `js_memory_limit`, `js_execution_failed` 和执行器繁忙的 `busy`. 不以“生成 Cookie”宣称已经读取文章, 只有重取并通过现有内容检查后才返回成功

### 正文提取与快照续读

HTML 使用 trafilatura 在本地提取正文, 保留段落, 标题, 列表, 表格和代码, 默认不附加评论. 失败时选择清理后的 article/main 候选, 最后回退清理后的整页文本. `extraction=main` 表示提取器结果, `fallback` 表示正文归属未确认并附带 warnings, `plain` 表示原响应是纯文本. 正文归属不等于事实已经核实, 视频页面文字不代表已观看视频

```python
first = fetch_page(url="https://example.com/article", max_length=5000)
next_part = fetch_page(
    url="https://example.com/article",
    page_id=first["page_id"],
    offset=first["next_offset"],
    max_length=5000,
)
```

上例表示模型工具的参数及解码后的 JSON 结果, 不是独立 Python 函数. 仅在 `has_more=true` 时按 `next_offset` 继续. 工具实例的 `execute` 返回 JSON 字符串, Python 调用者需先 `json.loads` 解码

| 参数 | 约束 |
| --- | --- |
| url | 唯一必填项, 首次 HTTP/HTTPS 地址, 最多 8192 UTF-8 字节; 续读原样填写首次调用地址, 包括查询串 |
| max_length | 默认 5000, 范围 1 至 20000 个字符, 超出报错而非静默缩小 |
| offset | 默认 0, 非负整数; 首次必须为 0, 非零必须提供 page_id |
| page_id | 首次省略, 续读填写当前工具实例返回的随机标识 |

成功结果中的 `total_length` 是完整提取正文的字符数, `offset/end_offset` 是本次左闭右开区间. `has_more/next_offset` 只指向后方剩余正文; `truncated` 表示本次没有返回完整正文, 包括前方已有省略的情况. 起点等于正文长度时返回空文本和 `has_more=false`, 大于长度报 `invalid_argument`. content 中不再拼入截断提示

快照只在当前工具实例范围内使用, 同步与异步实例不能相互取值. 续读不联网, 原页面变化也不会混入后续段落. 快照有效期从保存时算 5 分钟, 续读不延长. 过期, 淘汰, 工具重建, 后端重启或实例/URL 不匹配统一返回 `page_expired`, 不隐式重抓. 重新从头抓取是一次新的请求

全进程最多 128 份快照, 键与文本/元数据的编码容量合计最多 32 MiB, 满时按 LRU 淘汰. 单份正文最多 1 MiB UTF-8, 超出返回 `extraction_limit`, 不截掉尾部再宣称是完整快照. 快照不落盘, 不保存原 HTML, Cookie 或登录资料; 工具结果中已返回的文字仍遵循宿主已有会话历史存储规则

异步正文解析使用 2 个工作线程, 最多另有 8 个等待任务, 满时返回 `busy`. 超时或取消后, 已开始的解析仍占名额直到实际完成; 未开始的任务标记放弃, 实际出队时跳过解析并释放名额, 避免取消后的响应仍留在队列却允许继续排队. 总时限包括网络与提取, 超时不写入成功快照. 线程不能强制终止已开始的解析, 输入与并发限制不等于 CPU 硬超时

HTTP 200 也可能返回 `javascript_required` (明确 JS 提示且页面为空壳) 或 `content_unavailable` (无可读取内容但原因不明), 保留真实 `status_code=200`, 不冒充 403. 这两类错误不触发站点封禁冷却. 短文或文章提到验证码/JavaScript 不会单凭关键词被拒绝

每份成功结果标记 `source_type=external_web` 并附带 `content_notice`. 网页仍是工具返回的外部资料, 页面命令和身份声明不是用户授权, 不提升到 system prompt 或加载成 skill. 不自动执行页面指令, 不通过关键词过滤删除疑似注入原文, 也不承诺识别所有注入

### 本轮验收记录 (2026-10-20)

Windows / Python 3.13 / curl_cffi 0.16.3 已通过本地真实 HTTP 流程验收, 包括重定向地址校验, DNS 绑定, Cookie 隔离, 403/429 和验证页冷却, 压缩与流式响应限额, 排队及 DNS 总时限, 异步取消和内存缓存上限

联网对照使用三个中英文搜索词及两个公开文档地址, 普通客户端与浏览器请求配置均成功; 另行验证了异步搜索. 此样本未复现原来的 403, 因此没有据此宣称已消除封禁或提高成功率. Linux/macOS 及 Python 3.11 尚未在本轮实际运行验收, 已加入三平台 Python 3.11/3.13 CI 流程

## 沙箱协调

base_take 与 satrap_coding 在同一会话内共享该会话的私有 sandbox, 不与其他会话共享。当两插件同时启用时, ChatService 自动停用 base_take 的 `code_sandbox` 工具, 避免能力重复。

代码子进程默认移除密钥类环境变量, `allowed_env_vars` 可按名称显式放行; Windows 按不区分大小写的名称匹配. 目录约束和环境过滤不等同于操作系统级隔离

## 记忆能力拆分

长期记忆已独立为 [memory 插件](memory-plugin.md), 本插件不再持有记忆状态, 工具, 命令或注入处理器
旧 Agent 配置中的记忆设置与能力开关由配置迁移逻辑移交, 已有数据库记录与 ID 保留

## 文档解析

`read_document` 工具支持解析以下格式为纯文本:

- `.xlsx` — openpyxl 读取, 按 sheet 输出 TSV
- `.docx` — python-docx 读取, 段落 + 表格
- `.pdf` — pdfplumber 逐页提取文本
- 纯文本 — 直接读取 (utf-8)

工作区, 当前会话的 `uploads/` 和 `sandbox/` 内普通文件直接读取。相对路径基于工作区; 仅简单文件名未找到时回退匹配当前会话上传文件名。显式目录外单文件使用宿主共享审批: `user` 人工批准, `auto` 独立模型审查并在需要时转人工, `full-access` 直接读取; 无人工通道返回 `approval_required`。不要求安装 coding。输出按 `max_length` 截断 (默认 131072 字符)。

外部文件审批只包含真实路径, 文件指纹和本次文本/页面参数, 批准前不读取正文, 解码图片或渲染 PDF。等待后及读取前后复核文件, 环境, 模式和原始来源; 发生变化则取消。批准不扩展到后续读取或父目录。敏感文件及保护目录在所有模式下拒绝, 当前会话上传/沙箱的例外不开放整个 `.satrap/`。此工具不枚举外部目录, 不授予文件写入或群发送权限。

### 本地图片读取

`read_document(path="images/plot.png")` 可读取 PNG, JPEG, WebP, GIF, BMP 和 TIFF, 外部图片沿用上述单文件审批。无需通过 Shell 或文档文本解析器间接查看。

当前模型须启用图像与视频输入, `mode` 使用 `auto` 或 `visual`; `text` 不提供 OCR。工具返回实际图像内容, 与 PDF 页面共用工具媒体传递和持久化通道。图片自动校正 EXIF 方向, 长边最多 2048 像素; 文件上限 32 MiB, 解码上限 4000 万像素。动图和多页图片本次仅查看第一帧, 返回文字会明确说明。
