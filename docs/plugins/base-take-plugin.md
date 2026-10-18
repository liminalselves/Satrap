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
| search_timeout | number | 10 | 搜索超时 (秒) |
| allowed_env_vars | string | 空 | 代码子进程显式放行的环境变量名, 多个名称用逗号分隔 |

安装时经 `install_plugin(path, config={...})` 传入会话级覆盖; 全局默认存于 `.satrap/config/plugins/base_take.json`。

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
