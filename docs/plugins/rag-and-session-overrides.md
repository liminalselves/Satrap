# RAG 与会话参数覆盖

RAG 插件为当前会话模型提供工具, 返回资料片段和来源, 由当前模型组织最终回答

## 使用流程

1. 在模型配置页面建立 Embedding 配置, 按需建立 ReRank 配置
2. 打开后台的知识库页面, 或 Chat / 平台会话中的知识库入口
3. 选择全局或具体会话, 新建知识库并绑定 Embedding 配置
4. 导入文本或文件, 使用检索测试检查召回结果
5. 启用 `rag` 插件, 在全局插件配置或会话插件参数中选择检索范围与知识库

后台与会话页面共用知识库管理组件和领域服务, 会话上下文只能选择本会话的私有库及全局库

## 知识库参数

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `name` | 必填 | 展示名称, 最多 120 个字符 |
| `scope` | 创建时选择 | `global` 或 `session`, 会话库需要明确的会话身份 |
| `embed` | 必填 | 后端 Embedding 配置名称 |
| `chunk_size` | 800 | 每块字符数, 范围 1–100000 |
| `chunk_overlap` | 120 | 相邻块重叠字符数, 必须小于块大小 |
| `batch_size` | 32 | 单次向量化批量大小, 范围 1–1000 |
| `duplicate_policy` | `skip` | 相同来源名称采用跳过或 `replace` 替换 |
| `is_default` | 首个会话库为默认库 | 会话未显式选择库时使用的默认库 |

文档来源名称用于识别同一文档, 内容摘要用于返回来源和去重信息

Embedding 绑定到知识库, 模型名称、服务地址或向量维度变化需要重建, 更新同一模型的凭据不要求重建
分块大小和重叠参数变更也需要显式重建, 重建完成后切换索引版本, 失败保持原配置和原索引可用
完成重建后清理旧版本, 失败产生的临时版本也会清理, 删除知识库会同时删除其索引文件

## RAG 插件参数

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `db_scope` | `session` | `global` / `session` / `session_global` |
| `global_db_ids` | `[]` | 允许检索的全局库 ID, 空数组不代表全部 |
| `session_db_ids` | `[]` | 允许检索的当前会话库 ID, 空数组使用当前会话默认库 |
| `write_db_id` | `""` | 默认导入库, 必须位于允许范围内 |
| `rerank` | `""` | 可选的后端 ReRank 配置名称 |
| `candidate_k` | 20 | 每库召回候选数, 1–1000, 不能小于 `top_k` |
| `top_k` | 5 | 合并、去重和排序后的最多返回片段数, 1–100 |
| `similarity_threshold` | `null` | 向量相似度阈值, 空值不额外过滤 |
| `rerank_min_score` | `null` | 重排最低分, 空值沿用后端 ReRank 配置 |
| `rerank_failure_policy` | `fallback` | 重排失败时返回融合排名并附警告, 或选择 `error` 返回错误 |
| `max_result_chars` | 12000 | 返回正文总字符预算, 1–100000, 不包含来源元数据 |

联合检索默认向当前会话的默认库写入, 多个库且无法唯一确定目标时要求指定写入库
工具参数只能缩小配置允许的知识库范围

## 模型可调用的工具

| 工具 | 参数 | 结果 |
| --- | --- | --- |
| `rag_search` | `query`, 可选 `kb_id` | 正文、库及文档来源、相似度、融合分数、可选重排分数和截断信息 |
| `rag_list` | 可选 `kb_id` | 允许访问的库, 指定库时附文档概要 |
| `rag_ingest` | `text` 或 `path`, `source`, 可选 `kb_id` | 导入状态和片段数 |

文本导入必须提供来源名称, 文件导入默认使用文件名
文件路径必须位于当前工作区或会话允许的文件目录, 支持已有文档解析器支持的格式
单次文本上限为 1000000 个字符, 管理页面上传文件上限为 32 MiB
检索无命中返回 `empty`, 配置或模型调用失败返回错误, 两种情况分别处理

## 管理页面文档上传

知识库列表的“上传文档”按钮打开独立导入弹窗, 可以一次选择或拖入多个文件, 也可以切换到粘贴文本
新建知识库成功后自动选中并打开导入弹窗, 弹窗明确显示目标知识库及全局或会话归属
每个文件使用独立来源名称, 默认使用文件名, 可逐项修改; 同名来源使用知识库的 skip/replace 策略
前端队列按顺序调用单文件接口, 每个文件仍受 32 MiB 限制, 不将整批内容合并到一个请求
文件格式或大小不合法时标记该项, 其余合法文件仍可导入; 解析或构建失败后继续处理后续文件
上传期间锁定提交及关闭操作, 每项显示上传进度、解析和构建状态、成功块数或具体失败原因
批次结束后刷新文档列表, 保留队列结果及统计供查看; “重试失败文件”只提交失败项, 已成功或跳过项不重复提交
解析、向量化或构建、重建异常通过两个接口统一返回 error 和 stage, 界面优先显示后端原因
Chat 错误响应同样携带获准来源的 CORS 头, 避免浏览器将真实 HTTP 错误屏蔽为 Network Error
真实网络中断或超时时明确提示结果未知, 应刷新知识库确认后再重试

控制接口为 POST /config/rag/upload, Chat 接口为 POST /api/chat/rag/upload
请求体为原始文件字节, Content-Type 为 application/octet-stream
查询参数为 kb_id、file_name、可选 source 和 session_id, 控制接口另外接收 platform_id
两个上传路由均允许最大 32 MiB 请求体, 普通接口的请求体限制保持原值
GET 知识库列表返回 upload 对象, 包含扩展名、文件大小上限、文本字符上限、编码要求和缺失的解析依赖

通用解析实现位于 satrap/core/utils/documents.py:
- extract_document(path): 用于完整入库, 拒绝无效 UTF-8、空文本及超过 1000000 字符的文档, 不静默截断
- extract_text(path, max_length=...): 保留已有阅读工具的限长读取语义, 同样严格检查文本编码
- document_capabilities(): 返回供界面与接口共用的解析能力和约束
- 原 base_take/core/docread.py 保留兼容导出, 新代码直接依赖 utils

支持 .txt、.md、.log、.csv、.json、.yaml、.yml、.toml、.xml、.html、.py、.js、.ts、.pdf、.docx、.xlsx
文本允许 UTF-8 BOM, HTML 与代码按源文本读取; Word 提取段落与表格, Excel 按工作表提取单元格, PDF 提取已有文字
Word、Excel、PDF 分别按需加载 python-docx、openpyxl、pdfplumber, 依赖缺失会明确提示
扫描 PDF 暂不支持 OCR, .doc、.xls、演示文稿、图片、音视频、压缩包和无扩展名文件不在首版范围内
解析阶段保留文件大小、OOXML 解压大小与压缩比、PDF 页数和 Excel 行列总量限制
临时文件仅在收到文件后创建, 成功和失败都清理; 保存的是提取文本及向量, 不提供原文件下载

## 插件 YAML 模型引用约定

通用插件用 `config_schema` 的类型声明识别模型选择器, 推荐字段名与依赖类型一致:

```yaml
config_schema:
  llm:
    type: llm
    default: ""
    required: true
    description: "此插件内部使用的语言模型"
  embed:
    type: embed
    default: ""
    required: true
    description: "向量模型"
  rerank:
    type: rerank
    default: ""
    description: "可选重排模型"
```

字段值仅保存后端模型配置名称, `embed` 对应后端的 `embedding` 配置分类
插件按实际需求声明字段, RAG 的 Embedding 由知识库管理, 插件本身仅声明可选 ReRank 引用

工厂可使用 `get_tools(session, config, resources)` 或关键字参数 `resources`
`resources["embed"]` 等按声明字段获取客户端, 同步和异步会话分别注入对应客户端
客户端首次获取时构造, 卸载或安装回滚时释放, 配置字典中不保存客户端或模型凭据
旧的零参数、单参数和双参数工厂保持可用

## 可复用的会话覆写模板

存储位于已有 `platform.db` 的 `session_config_overrides` 表, 主键为 `session_id + namespace`
插件使用 `plugins.<插件名>` 配置域, 其他功能可通过 `SessionOverrideService.register` 注册独立配置域和验证器

```python
from satrap.core.config.session_overrides import SessionOverrideService, SessionOverrideStore

service = SessionOverrideService(SessionOverrideStore(platform_database))
service.register("feature.example", validate_explicit_values)
state = service.resolve(session_id, "feature.example", [
    ("default", default_values),
    ("global", global_explicit_values),
])
service.save(session_id, "feature.example", {"enabled": False}, expected_revision=state["revision"])
```

插件合并优先级为: YAML 默认值 < 全局插件配置 < Edictum 命名配置 < 当前会话覆盖

- 只保存主动修改的字段, 不保存合并后的配置
- `false`、`0`、`""`、`[]` 和 schema 允许的 `null` 都是显式值
- 删除键表示恢复继承, 数组和对象按字段整体替换
- `session_overridable: false` 禁止会话修改该字段
- 保存携带最近读取的 `expected_revision`, 旧版本请求返回 409
- 清空已有覆盖保留空记录及递增修订号, 防止旧编辑器在清空后重新覆盖
- 未使用覆盖的会话不会为该配置域创建记录

Chat 通过 `/api/chat/session-plugin-config` 读写, 参数为 `conversation_id` 和 `plugin`
平台会话通过控制接口 `/config/session-plugin-config` 读写, 参数为 `platform_id`、`session_id` 和 `plugin`
GET 返回生效配置、显式覆盖、继承值、字段来源和修订号, PUT 请求体为 `{"overrides": {...}, "expected_revision": 1}`
正在运行的会话在下一轮应用修改, 空闲 Chat 会话立即协调, 冷会话下次激活时读取

## 文件和生命周期

普通文本会话与覆盖配置使用数据库, 上传文件、沙箱产物或本地 RAG 索引使用按需创建的目录
会话列表分页查询数据库, 大小显示读取缓存, 独立的大小刷新操作才扫描磁盘

删除会话时覆盖记录和私有知识库随现有会话归档一起回收, 恢复归档时恢复对应数据
RAG 索引操作与会话回收使用同一把可重入的跨进程锁, 锁位于平台目录, 避免移动正在写入的索引
Chat 分支继承分支时的当前覆盖和私有知识库快照, 为私有库分配新 ID 并替换 RAG 引用, 全局库引用继续共享
分支副本后续写入不会修改源会话的知识库, 分支复制的配置和资料是操作时状态, 不是所选历史轮次的配置快照

## 验证命令

```text
python -m pyright -p .pyrightcfg/pyrightconfig.json
python -m pytest tests/unit/test_rag_service.py tests/unit/test_session_overrides.py tests/unit/test_plugin_resources.py
cd satrap-ui
node e2e/rag-settings.mjs
node e2e/rag-upload.mjs
npm test
npm run build
```

浏览器测试使用受控接口响应, 后端 RAG 测试使用确定性的离线模型替身及真实 SQLite / FAISS 存储

文件上传浏览器回归使用真实 HTTP、文档解析、SQLite 和 FAISS, 仅 Embedding 调用使用离线替身
可通过 SATRAP_TEST_PYTHON 指定后端测试所用的 Python 解释器
