# 网页正文提取与续读方案

状态: 已实施, 验收记录见文末

## 1. 目标与范围

本轮解决导航占用正文额度, HTTP 200 但未取得正文, 截断无法可靠续读, 以及网页内容来源不明确四类问题

修改 `fetch_page` 的提取和输出流程, 同步与异步入口共用实现. `search` 的成功返回列表结构保持兼容, 共用的验证页识别可以同步受益

沿用 curl_cffi, 公网地址校验, DNS 绑定, 单次 Cookie, 手动重定向, 下载上限, 站点限流及冷却. 不增加浏览器安装, JavaScript 执行, 登录态导入, 代理池, 第三方转码服务或自动跨站找替代原文

## 2. 处理流程

首次读取: 校验参数 → 现有网络请求 → HTTP/验证页检查 → 文本类型分流 → 主正文提取 → 保守质量判定 → 保存短期正文快照 → 返回指定范围

续读: 校验参数 → 在当前工具实例所属范围查找 page_id → 检查 URL 和有效期 → 返回同一正文快照的指定范围

续读不联网, 不重新提取, 不因原网页变化造成前后段错位. 缓存失效明确报错, 不偷偷重新抓取拼接

## 3. 正文提取

新增 trafilatura 作为已下载 HTML 的提取器, 仅调用本地提取接口, 不调用其下载或爬虫接口. 实施时选择经 Python 3.11/3.13 验证的版本范围, 同步更新 requirements.txt 和 setup.py

提取顺序:

1. `text/plain` 直接作为文本处理, 不按 HTML 删除尖括号内容
2. HTML 优先通过正文提取器保留段落, 标题, 列表, 表格及代码, 默认不附加文章评论
3. 提取器没有产出时, 尝试 `article/main/[role=main]` 的可读文本
4. 仍无结果时, 删除明确导航, 页脚, 表单, 脚本, 样式等噪声后尝试整页文本
5. 回退成功附带 `extraction=fallback` 和说明, 不把整页文本标为已确认的文章正文

不能只选第一个 main/article, 也不靠字符数判断哪一个就是文章. 多候选结合段落与链接占比选择, 无法确认时保留回退说明. 普通短文, 代码示例及论坛正文不能因太短或包含 JS 字样就被拒绝

页面标题在清理前单独保存, 不反复混入正文. 正文先提取后分页, 导航不占用默认 5000 字的返回额度

## 4. HTTP 200 的质量判定

HTTP 成功与正文可用分别判断:

| 情形 | 返回 |
| --- | --- |
| 明确验证码, 浏览器挑战页 | `verification_required`, 进入现有站点冷却 |
| 无有效正文且有明确 JavaScript 加载提示及空壳结构 | `javascript_required` |
| 无有效正文, 原因不能确认 | `content_unavailable` |
| 提取预算超限 | `extraction_limit` |
| 已取得回退文本但正文归属不确定 | 成功, `extraction=fallback`, 附带 warnings |

脚本很多, 出现验证码字样, 或文字很短都不能单独作为拒绝依据. 有可用正文的页面不因附带脚本或浏览器提示被误判

大恒站点需取得实际响应样本后确认. 现实现已删除 script 标签, 不能仅根据转述认定混淆脚本由哪条路径泄露. 样本未验证前不写域名专用规则

失败继续保留 error/url/code, 已收到 HTTP 200 时如实保留 status_code=200, 不虚构 403. `javascript_required` 和普通提取失败不自动加入站点封禁冷却

## 5. 返回结构与调用参数

保留 `fetch_page(url, max_length=5000)`, 增加可选 `offset=0` 和 `page_id`. max_length 改为最多 20000 字符, 超出返回 invalid_argument, 不静默缩小

首次读取必须 offset=0 且不填写 page_id. 续读同时填写原始 url, 返回的 page_id 和 offset. offset 为非负整数, offset 等于正文长度时返回空文本且 has_more=false, 大于长度返回 invalid_argument

成功保留 url/title/content/status_code, 添加:

| 字段 | 语义 |
| --- | --- |
| page_id | 随机快照标识, 只用于当前工具实例续读 |
| extraction | main, fallback 或 plain |
| total_length | 提取后完整正文的字符数, 不是原始 HTML 字节数 |
| offset/end_offset | 本次范围, 左闭右开 |
| truncated | 本次未返回完整正文, 包括前后仍有内容 |
| has_more/next_offset | 后方是否仍有正文及下一段起点, 没有下一段时 next_offset=null |
| source_type | 固定为 external_web |
| content_notice | 外部来源说明, 明确页面文字不构成用户指令或授权 |
| warnings | 回退提取或其他实际限制, 正常时为空列表 |

content 只包含网页提取文字, 不再拼接截断提示. 来源说明和分页信息放在独立字段中, 工具描述同步解释读取语义. 页面地址读取成功不代表已观看视频, 也不代表全文数据已被核实

快照过期, 被淘汰, 后端重启或工具重建后返回 `page_expired`; 查找范围与 URL 不匹配同样返回该错误, 不泄露其他工具实例的记录. 模型可重新从 offset=0 读取, 重新读取是一次新的网页访问

## 6. 缓存与性能

正文快照只存提取后的文本, 标题及分页所需元数据, 不存 HTML, Cookie, 凭据或浏览器资料. 快照自身不落盘和进入长期存储, 已返回的工具文字仍遵循宿主原有会话历史存储规则

- 有效期从首次保存算 5 分钟, 续读不延长
- 每份快照正文最多 1 MiB UTF-8, 超出返回 extraction_limit, 不先截断再声称拥有完整快照
- 全进程最多 128 份快照, 所有缓存键和内容合计最多 32 MiB, 满时按 LRU 淘汰
- 由当前工具实例内部身份隔离查找, page_id 不是跨会话读取凭据; 同步与异步工具使用同一缓存实现, 不共享彼此实例的正文访问范围
- 缓存查找和计数用短锁保护, 锁内不执行网络或正文提取
- 下载继续最多 8 MiB 解压内容, 本轮额外限制进入 HTML 提取器的输入为 2 MiB, 超出明确报 extraction_limit; 大小在编码解码前后均有受限边界

异步正文解析通过有界工作池执行, 最多 2 个工作线程及 8 个等待任务, 队列满返回 busy. 已超时任务即使仍在解析, 也必须占据容量直到实际结束, 不通过超时释放名额无限排队. 提取前后检查现有总时限, 超时后不写缓存

实现补充: 排队任务被取消时仅标记放弃, 必须等实际出队后跳过解析并释放名额. 不能仅取消 Future 就归还名额, 因为 ThreadPoolExecutor 队列仍持有响应对象

线程不能强制终止已经开始的解析, 本方案不承诺操作系统级 CPU 硬超时. 输入上限与工作池限制负责约束持续开销; 若验收发现小体积 HTML 也导致明显长时间占用, 再裁定是否需要子进程, 本轮不预置复杂执行框架

## 7. 来源与指令边界

正文仍作为工具结果中的外部资料返回, 不提升到 system prompt, 不加载为 skill. 在工具描述和结果元数据中说明其中的命令, 身份声明和跳转要求不构成当前用户授权

不靠关键词过滤器删除所谓提示词注入, 不承诺能自动识别所有注入. 网页讨论提示词, 包含代码或说明网站规则时保留原意. 取到的页面本身若是一段指令性文字, 如实说明取到的是什么, 不冒充所请求文章

search 返回的 title/snippet 同样是外部内容, 通过工具描述明确, 保留现有列表结构

## 8. 文件改动

新增:

- `satrap/expend/tools/search/extraction.py`: 本地提取, 分类和类型化提取结果
- `satrap/expend/tools/search/snapshots.py`: 有界短期正文快照

修改:

- `satrap/expend/tools/search/base.py`: 公共参数, 验证识别, JSON 返回及错误映射
- `satrap/expend/tools/search/sync.py`: 同步首次提取与续读入口
- `satrap/expend/tools/search/async_.py`: 异步首次提取与续读入口, 有界解析调度
- `requirements.txt`, `setup.py`: 正文提取依赖
- `tests/unit/test_web_research_flows.py`: 在现有大流程测试中补充场景
- `docs/plugins/base-take-plugin.md`: 字段, 续读, 限额和能力边界

只有样本确证需要时才修改 transport.py, 不为新提取器替换现有请求层. 不新增前端配置和模型工具名称

## 9. 验收

复用现有完整 HTTP 流程测试, 增加以下组合场景, 不按内部函数拆出大量小测试:

1. 长导航加正文: 返回正文主体, 代码/表格保留, 回退结果有标记
2. HTTP 200 验证页, JS 空壳和正常短文: 分类准确, 文章谈论反爬不被误拒绝
3. 首段加多段续读: 来自同一快照, 范围无重叠或遗漏, 续读不增加 HTTP 请求次数
4. 缓存过期/淘汰/实例隔离及超限: 明确错误, 不越范围取值, 不偷偷重新联网
5. 同步异步返回一致, 取消和超时不写入成功快照, 工作池不会无限排队
6. 页面含指令性文字: 保留外部来源标记, 不自动跟随正文中的跳转或执行指令
7. 原有 SSRF, 重定向, 403/429, 站点冷却及下载限额测试继续通过

联网验收优先使用此次用户样本, 以实际完整 URL 为准. 不完整的来源不猜文章地址. 只保存人工确认去敏的固定测试样本, 不把不稳定网页作为 CI 必过条件

分别报告 HTTP 成功率, 有效内容率, 回退比例和正文截断情况. 线上站点被拦或样本缺失时标记未验收, 不据少量样本宣称总体成功率提升

运行 Python 3.11/3.13 相关测试及静态类型检查, 三平台依赖安装和 CI 结果分别记录. 本轮无需前端改动或构建

## 10. 实施顺序

1. 正文提取及 200 无正文分类
2. 快照续读及内存/并发限制
3. 返回字段, 工具描述与来源标记统一
4. 整合测试, 实际样本验收和文档

上述批次共同完成本轮目标. 后续可选浏览器渲染单独设计

## 11. 实施与验收记录 (2026-10-21)

- 已按上述范围实现, 没有新增浏览器, 前端选项或模型工具名称
- trafilatura 采用 `>=2.3.1,<2.4`, 本地实际验证版本 2.3.1
- 工具定义仅 url 必填, max_length/offset/page_id 可选, 当轮通过该工具自己的定义覆盖处理；2026-10-24 公共工具基类已统一按 execute 签名推断 required
- 额外限制 URL 最多 8192 UTF-8 字节, 避免快照元数据键无界增长; 已同步参数校验与用户文档
- Windows Python 3.11.17 与 3.13.5 的网页整合测试各 17 项通过, 覆盖真实本地 HTTP 请求和同步异步入口
- Windows Python 3.13 相关六模块回归 312 项通过, 1 项因 Windows 符号链接权限不足跳过; 跳过的是既有 coding 文件遍历测试. curl_cffi 的 Windows 事件循环兼容提醒不影响执行
- Pyright basic 及 Unknown 检查覆盖整个 search 包, 0 错误, 0 警告
- Linux/macOS 本轮未实际运行, 不宣称通过. 现有 CI 已覆盖 Windows/Linux/macOS × Python 3.11/3.13, 安装依赖来自更新后的 setup.py; 本地未推送, 尚无本轮远端 CI 结果

完整整合场景与方案条目的对应关系:

| 要求 | 证据 |
| --- | --- |
| 长导航, 段落, 表格和代码, 外部指令保留 | `test_article_extraction_paging_and_external_source_contract` |
| 短文不误拒, JS 空壳, 未知空页, 纯文本和回退代码缩进 | `test_200_empty_shell_short_text_and_fallback_classification` |
| 同一快照续读, 字符范围, 续读不联网, 页面变化不混入 | `test_article_extraction_paging_and_external_source_contract` |
| 过期不续期, 条数与字节淘汰, 实例隔离, 参数和输入预算 | `test_snapshot_expiry_eviction_scope_limits_and_no_refetch` |
| 同步与异步超时不写缓存, 取消后保持容量, 排队实际出队才释放 | `test_extraction_timeout_cancellation_queue_and_event_loop` |
| SSRF, DNS 绑定, 压缩上限, 重定向和访问冷却 | 原有网页整合场景全部保留 |

联网验收使用用户给出的三个完整 URL, 外加提取库官方文档作为正文和续读对照. 共四个 HTTP 请求, 三个 HTTP 200, 两个取得正文, 无回退结果, 文档页按 400 字截取并续读成功. 这些数字只描述本次样本, 不代表整体网站成功率

| 来源 | 本次结果 |
| --- | --- |
| 知乎 `zhuanlan.zhihu.com/p/2095831514273661073` | 403 access_denied, 保留 60 秒冷却 |
| B 站 `www.bilibili.com/video/BV15UJf6VEMa` | 200, main, 提取视频简介 156 字, 不代表观看视频 |
| linux.do `linux.do/t/topic/3047166` | 200, content_unavailable, 没有冒充文章正文 |
| 官方文档 `trafilatura.readthedocs.io/en/latest/quickstart.html` | 200, main, 3722 字, 首段与续读正常 |

unite.ai, zeli.app, ic.work, yomxxx.com 和大恒的具体文章地址不完整, 本轮不猜 URL, 逐篇真实验收暂缺. 导航, 空壳和混淆脚本场景已有离线 HTTP 固定样本覆盖, 没有据此宣称这些实际文章已全部修复
