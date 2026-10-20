# base_take 插件: 基础能力集

`base_take` 是官方预设目录 (`satrap/expend/plugins`) 下的目录插件, 提供通用基础能力: 网页搜索、网页抓取、代码沙箱、文档与图片读取。与 `satrap_coding` 插件互补, 可独立或组合使用。

## 安装

```python
from satrap import SimpleSession

session = SimpleSession("conv-1", llm, db_path="chat.db")
session.install_plugin("./satrap/expend/plugins/base_take")

plugin = session.list_plugins()[0]
plugin.name          # "base_take"
plugin.tools         # 4 个工具
plugin.handlers      # 无记忆处理器
plugin.commands      # 无记忆命令
```

异步版 `AsyncSimpleSession` 同样支持 (`await session.install_plugin(path)`)。

## 能力清单

| 类别 | 能力 | 说明 |
| --- | --- | --- |
| 搜索 | search / fetch_page | 网页搜索与抓取 (复用 expend.tools.search) |
| 沙箱 | code_sandbox | 在隔离目录中执行 Python 代码 |
| 文档 | read_document | 读取本地图片, 提取文档文本及分页查看 PDF 图像 |

## 插件配置

meta.yaml 声明 `config_schema`, 支持以下配置项 (全局默认 + 按会话覆盖两级):

| 配置键 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| sandbox_root | path | 空 | 独立调用的兜底值; 平台运行时始终使用会话私有 sandbox |
| workspace_root | path | 项目根 | read_document 免审批工作区根, 外部单文件按宿主模式审批 |
| search_timeout | number | 10 | 整次搜索/网页读取的总时限秒数, 包括排队, DNS, 备用入口和重定向 |
| allowed_env_vars | string | 空 | 代码子进程显式放行的环境变量名, 多个名称用逗号分隔 |

安装时经 `install_plugin(path, config={...})` 传入会话级覆盖; 全局默认存于 `.satrap/config/plugins/base_take.json`。

## 网页搜索与读取

`search` 和 `fetch_page` 默认使用 `curl_cffi` 的 Chrome 请求配置, 不需要搜索 API Key 或安装浏览器. 请求头与 TLS/HTTP 指纹由同一个配置生成, 每次工具调用独享临时会话, Cookie 仅在该次调用内使用, 不读取日常浏览器资料或写入磁盘

两个工具继续使用已有公网地址限制. 每一跳都重新校验 DNS 并绑定实际连接地址, 禁止自动重定向到私网, 最多跟随五次重定向. 默认直接出站, 不继承系统代理环境变量. 每次响应的解压正文最多 8 MiB, 超限时立即停止下载; `max_length` 仅控制返回给模型的正文字符数

同一进程中的会话共享站点限流: 每站点最多一个进行中的请求, 前一请求结束后至少等待一秒再发起下一请求. Bing 的中国与国际入口按同一站点管理, 仅连接错误或 5xx 允许在剩余总时限内尝试一次备用入口. 明确的 403 或验证页面冷却 60 秒, 429 优先遵守 `Retry-After`, 缺失或无效时冷却 60 秒; 冷却期间直接返回原因和剩余等待时间

搜索成功结果缓存 60 秒, 最多 128 条且总容量不超过 8 MiB, 仅保存在进程内存. 查询词与结果数量共同作为键, 不缓存失败结果, 网页原文或 Cookie; `fetch_page` 正文不缓存. 多个后端进程各自维护限额, 不跨进程协调

搜索成功仍返回 `title/url/snippet` 列表, 明确无结果返回空列表. 网页成功仍返回 `url/title/content/status_code`. 失败保留 `error` 和 `url`, 追加 `code`, 有 HTTP 响应或冷却信息时附带 `status_code` 和 `retry_after`. 常见类别包括 `access_denied`, `rate_limited`, `verification_required`, `parse_error`, `timeout`, `connection_error`, `unsafe_url`, `response_too_large` 和 `redirect_limit`. 未识别的搜索页面不能证明无结果; 明确验证页也不会作为正文返回

浏览器请求配置只能改善部分客户端特征导致的拦截, 不保证解决出口 IP 限制, 登录要求或 JavaScript 验证. 本实现不执行网页脚本, 失败时应换用其它公开来源, 不把搜索摘要冒充已读取的全文

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
