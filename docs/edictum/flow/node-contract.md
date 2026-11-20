# 流程节点契约: 配置示例

issue #4 已实现的节点契约 (P0–P3 完成于 issue-4 分支)。每个示例逐行注释。

这一版按"能推断的不写, 交给宿主的不写, 首版用不上的先不做"简化, 并合入了两轮第三方审计确认成立的修正。第二轮的改动: 插件全局默认改为单独文件 (示例 K), 各层默认值带版本并先迁移后校验 (示例 N), passthrough 只按必填输入推断 (第 2 节), visible_when 只管显示, 预算分软硬两层 (示例 E)。

执行语义 (条件三值, 共享求值, 取消, 预算计量, 迁移流程) 见 [runtime-contract.md](runtime-contract.md)。

目录:

1. 作者要写什么
2. 宿主推断和代劳什么
3. 示例 A: 最小的条件节点 (免打扰时段)
4. 示例 B: transform, 一进一出 (关键词过滤)
5. 示例 C: 条件节点, 需要单条消息 (含链接)
6. 示例 D: 条件节点, 读状态槽 (快照新鲜)
7. 示例 E: 条件节点, 读宿主计数 (调用预算)
8. 示例 F: transform, 调用模型 (上下文压缩)
9. 示例 G: transform, 可选输入 + 知识库 (检索补充)
10. 示例 H: transform, 多个输出 (按发言人拆分)
11. 示例 I: 同一插件里的工具和节点
12. 示例 J: 装在 WF 上的插件 (`flow.workflow`)
13. 示例 K: 插件全局默认放在哪
14. 示例 L: 流程存档
15. 示例 M: 需要初始化资源时用类写法
16. 示例 N: 升级与迁移
17. 宿主提供的对象
18. 安装时会被拒绝的写法

---

## 1. 作者要写什么

### 1.1 meta.yaml 里一个节点

`flow_nodes` 是一个列表, 每项是一个节点:

```yaml
flow_nodes:
  - name: in_hours                       # 列表项第一行写节点名
    label: 免打扰时段内                   # 其余字段和 name 对齐
    out: condition
  - name: another_node                   # 下一个节点
    label: ...
```

| 字段 | 必填 | 默认 | 含义 |
| --- | --- | --- | --- |
| `name` | 是 | | 节点名; 和插件名拼成完整类型名 `插件名.节点名`; 同一插件内不能重复 |
| `label` | 是 | | 节点面板和卡片标题, 直接写中文 |
| `out` | 是 | | 输出: 写一个类型 (单输出), 或 `{端口名: 类型}` (多输出) |
| `in` | 否 | `{}` | 输入: `{端口名: 类型}`; 类型后加 `?` 表示可选。注意: 写在单行 `{}` 里时 `?` 会让 YAML 解析失败, 要么加引号 `{a: "state?"}`, 要么分行写 (见示例 G) |
| `description` | 否 | `""` | 悬停提示和属性面板说明 |
| `config_schema` | 否 | `{}` | 节点配置; 字段写法和插件 `config_schema` 相同; 前两个字段显示在卡片上 |
| `needs` | 否 | 无 | 仅条件节点: 求值需要的上下文, 只能是 `message` 或 `batch` |
| `version` | 否 | `1` | 节点契约版本; 只有改名或删除字段/端口时才需要提高 |
| `renamed` | 否 | 无 | 每个版本的改名记录, 宿主据此自动迁移 |
| `reading` | 否 | 无 | 连线上的读取说明, `{端口名: 文字}`; 文字里可用 `{字段名}` |
| `passthrough` | 推断不出时必填 | 推断 | 启用条件为假时每个输出取哪个输入, 或 `null` 表示空值; 见第 2 节 |

不用写的: 角色, 分组, 图标, 端口显示名, 端口位置, 启用条件口, 是否调用模型, 卡片显示哪些字段, 单流程最多几个。这些都由宿主推断, 见第 2 节。`passthrough` 大多数节点也不用写。

### 1.2 flow_nodes.py 里一个节点

```python
async def 节点名(ctx, 输入端口名, ...):   # transform: 同步或异步都行
    return 输出值                           # 单输出直接返回值; 多输出返回 {端口名: 值}

def 节点名(ctx) -> bool:                   # 条件节点: 必须同步, 不做 IO
    return True
```

### 1.3 端口类型 (固定 7 种, 插件可用其中 5 种)

| 类型 | 插件可用 | 运行时的值 | 空值 |
| --- | --- | --- | --- |
| `messages` | 输入, 输出 | `tuple[FlowMessage, ...]`, 按档案顺序 | `()` |
| `context` | 输入, 输出 | `tuple[ContextBlock, ...]` | `()` |
| `text` | 输入, 输出 | `str` | `""` |
| `state` | 仅输入 | `StateView` | 版本 0 的空视图 |
| `condition` | 仅输出, 且只能单独作为唯一输出 | `bool` | |
| `event` | 否 | 宿主内部 | |
| `handoff` | 否 | 宿主内部 | |

### 1.4 config_schema 新增项

现有字段类型和键全部沿用 (`satrap/core/type/plugin/config.py` 的 `ConfigField` 与 `schema_to_payload`, `satrap/edictum/plugin/config.py` 的解析)。以下三项需要在字段声明、解析、严格校验和 payload 里支持, 插件自己的配置表单也能用:

| 新增 | 含义 |
| --- | --- |
| `type: time` | `HH:MM`; 结束早于开始表示跨零点 |
| `group` | 属性面板分组名; 不写则不分组 |
| `visible_when` | `{字段: 值}` 或 `{字段: [值, 值]}`; 全部满足才显示。只控制显示: 后端照常校验所有字段, 隐藏字段的值保留; 是否必填不随显示变化 |

### 1.5 命名规则

| 名字 | 规则 |
| --- | --- |
| 节点名 (`name`) | `[a-z][a-z0-9_]{0,31}`, 不能有点号; 同时是 Python 函数名; 同一插件内重复时安装失败 (`flow_contract_name`) |
| 端口名 | 同上; `when` 保留给宿主 |
| 完整类型名 | `插件名.节点名`, 按最后一个点拆分 (插件名允许带点, `plugin/config.py:41`) |
| 不带点的类型名 | 保留给内置节点 (`react_workflow`, `wake`, `and` 等) |

---

## 2. 宿主推断和代劳什么

| 作者不写 | 宿主怎么定 |
| --- | --- |
| 角色 | `out: condition` 是条件节点, 其他都是 transform |
| 面板分组 | 条件节点进"条件"组, transform 进"插件"组; 组内按插件分小组, 显示插件名 |
| 图标 | 用插件图标 (`meta.yaml` 现有 `icon` 字段), 没有就用通用图标 |
| 端口显示名, 位置 | 显示名用类型的中文名 (消息/上下文/文本/状态/真假); 数据口上进下出, 信号口左进右出 |
| 启用条件口 | 每个 transform 自动带一个左侧"启用条件"口; 不接线等于永远启用 |
| 条件为假时的输出 | 每个输出只在**必填**输入里找同类型的: 恰好一个就原样传过去。找不到或找到多个时安装失败, 要求手写 `passthrough`。可选输入不参与推断, 所以以后新增可选输入不会改变已有节点的行为 |
| 是否调用模型 | `config_schema` 里有 `llm` / `embed` / `rerank` / `asr` / `img` 字段即视为调用模型, 卡片显示"插件模型"标记 |
| 卡片显示内容 | `config_schema` 前两个字段的生效值; select 显示选项名, 多行文本显示条目数 |
| 输入是否必填 | 不带 `?` 的都是必填 |
| 求值次数 | 同一个触发事件里, 同一个 transform 只求值一次, 被多个 WF 拉取时共享结果; 不同事件各算各的 |
| 条件异常 | 函数抛异常, 或 `needs` 要的上下文不存在时, 结果为"错误"; 错误在逻辑门里单独传递, 触发最终收到错误时不触发并写诊断 |
| 输出校验 | 返回值缺端口或类型不对: 等待它的 WF 本次运行都失败, 阈值位置不推进, 状态槽不提交 |
| 超时, 取消 | 共享计算按等待者计数: 一个 WF 取消只是不再等待; 所有等待者都取消后才中断计算; 超时取等待者中最长的一个 |
| 同步函数 | 放到工作线程运行, 不阻塞事件循环; 工作线程里的同步函数无法被中断, 取消时只丢弃结果 |

详细规则见 [runtime-contract.md](runtime-contract.md)。

---

## 3. 示例 A: 最小的条件节点

免打扰时段。全文只有 `label`, `out` 和配置。

```yaml
# quiet_hours/meta.yaml
name: quiet_hours                        # 插件名 (现有字段)
version: 0.3.0                           # 插件发行版本 (现有字段); 变化时宿主重装插件
description: 提供"在免打扰时段内"条件     # 插件说明 (现有字段)

flow_nodes:                              # 新增分组: 本插件提供的节点
  - name: in_hours                       # 节点名; 完整类型名 quiet_hours.in_hours; 对应 flow_nodes.py 里的 in_hours()
    label: 免打扰时段内                   # 节点标题
    description: 触发时刻在时段内为真, 通常接 NOT 使用   # 悬停提示
    out: condition                       # 唯一输出是真/假 → 宿主把它当作条件节点
    config_schema:                       # 节点配置; 前两个字段显示在卡片上, 显示为"22:00 – 07:00"
      start: {type: time, label: 开始, default: "22:00"}   # time 是新增类型
      end: {type: time, label: 结束, default: "07:00", description: 早于开始时间表示跨零点}
```

```python
# quiet_hours/flow_nodes.py
def in_hours(ctx) -> bool:               # 函数名 = 节点名; 条件节点必须同步
    return ctx.local_time_between(ctx.config["start"], ctx.config["end"])   # 按群所在时区判断
```

---

## 4. 示例 B: transform, 一进一出

关键词过滤。放在桶和拼装节点之间。

```yaml
# keyword_filter/meta.yaml
name: keyword_filter
version: 0.2.0
compatibility:                           # 现有字段
  satrap: ">=0.4,<0.5"                   # 没有流程会话的旧宿主装不上

flow_nodes:
  - name: filter                         # 完整类型名 keyword_filter.filter
    label: 关键词过滤
    description: 丢弃或遮盖命中屏蔽词的消息
    in: {messages: messages}             # 一个必填输入, 端口名 messages, 类型 messages
    out: {kept: messages}                # 一个输出, 端口名 kept; 写成 {名: 类型} 是为了起名, 也可以只写 out: messages (端口名默认 out)
                                         # 宿主自动加启用条件口; 条件为假时 kept 原样等于 messages (同类型的必填输入只有它一个)
    reading:                             # 可选: 连到 messages 口的那条线上显示的说明
      messages: "{mode}, 屏蔽词 {blocked_words}"   # 显示为"丢弃整条, 屏蔽词 2 个"
    config_schema:
      mode:                              # 第一个字段, 显示在卡片上
        type: select
        label: 处理方式
        default: drop
        options: [drop, mask]
        option_labels: {drop: 丢弃整条, mask: 遮盖命中词}   # 卡片和读取说明里显示这些中文
      blocked_words:                     # 第二个字段, 显示在卡片上 (多行文本显示为"N 个")
        type: textarea
        label: 屏蔽词
        default: ""
        description: 每行一个, 不区分大小写
      mask_char:
        type: string
        label: 遮盖字符
        default: "*"
        visible_when: {mode: mask}       # 只在遮盖模式下显示; 切回丢弃模式时值保留, 后端照常校验
      include_bot:
        type: bool
        label: 也过滤机器人发言
        default: false
```

```python
# keyword_filter/flow_nodes.py
from satrap.edictum.flow import node


@node("filter")                          # 函数名和 Python 内置函数 filter 重名, 用装饰器指定节点名
def keyword_filter(ctx, messages):       # 同步函数, 宿主放到工作线程运行; 参数名 = 输入端口名
    words = [w.strip().lower() for w in ctx.config["blocked_words"].splitlines() if w.strip()]
    kept = []
    for m in messages:                   # messages 是不可变的 tuple[FlowMessage, ...]
        if m.is_bot and not ctx.config["include_bot"]:
            kept.append(m)               # 机器人发言直接放行
            continue
        hits = [w for w in words if w in m.text.lower()]   # 找出全部命中词, 不只第一个
        if not hits:
            kept.append(m)
        elif ctx.config["mode"] == "mask":
            text = m.text
            for w in hits:
                text = ctx.replace_ignore_case(text, w, ctx.config["mask_char"] * len(w))
            kept.append(m.with_text(text))   # FlowMessage 不可变, with_text 返回新对象
        else:
            ctx.diagnostic("dropped", message_id=m.message_id, words=hits)   # 写进运行事件面板
    return kept                          # 单输出直接返回值; 宿主转成 tuple 并校验类型
```

---

## 5. 示例 C: 条件节点, 需要单条消息

含链接。只在群唤醒这种"一条消息一次触发"的路径上有意义。

```yaml
# link_guard/meta.yaml
name: link_guard
version: 0.1.0

flow_nodes:
  - name: has_link                       # link_guard.has_link
    label: 含链接
    description: 触发消息包含网址或群号时为真
    out: condition
    needs: message                       # 需要单条消息
                                         # 编辑时: 接在阈值/交接路径上报 flow_condition_context
                                         # 运行时: 没有单条消息时结果为"错误", 不触发
    config_schema:
      detect:
        type: select
        label: 检测
        default: both
        options: [url, group_number, both]
        option_labels: {url: 网址, group_number: 群号, both: 网址和群号}
      allow_domains:
        type: textarea
        label: 白名单域名
        default: ""
        description: 每行一个, 子域名也算; 白名单里的网址不算链接
        visible_when: {detect: [url, both]}   # 列表写法: 检测网址或两者时都显示; 只检测群号时隐藏, 但值仍保留并校验
```

```python
# link_guard/flow_nodes.py
import re
from urllib.parse import urlsplit

URL = re.compile(r"https?://\S+", re.I)
GROUP = re.compile(r"(?<!\d)\d{6,11}(?!\d)")


def _host(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()   # 去掉端口和大小写差异
    return host.encode("idna").decode() if host else ""   # 中文域名转成 punycode 再比较


def has_link(ctx) -> bool:
    text = ctx.message.text              # needs: message 保证 ctx.message 存在; 不存在时宿主不会调用这里
    if ctx.config["detect"] in ("url", "both"):
        allow = [_host("http://" + d.strip()) for d in ctx.config["allow_domains"].splitlines() if d.strip()]
        for u in URL.findall(text):
            h = _host(u)
            if h and not any(h == a or h.endswith("." + a) for a in allow):
                return True
    if ctx.config["detect"] in ("group_number", "both"):
        return bool(GROUP.search(text))
    return False
```

---

## 6. 示例 D: 条件节点, 读状态槽

快照是否新鲜。条件节点唯一允许的输入是 `state`。

```yaml
# snapshot_guard/meta.yaml
name: snapshot_guard
version: 0.1.0

flow_nodes:
  - name: fresh                          # snapshot_guard.fresh
    label: 快照新鲜
    description: 所连状态槽在指定时间内有新版本时为真; 从未写入时为假
    in: {state: state}                   # 接状态槽的读取口
    out: condition
    config_schema:
      max_age_minutes: {type: number, label: 最长间隔, default: 60, minimum: 1, maximum: 10080, integer: true}
```

```python
# snapshot_guard/flow_nodes.py
def fresh(ctx, state) -> bool:           # state 是只读 StateView
    if state.version == 0:
        return False
    return (ctx.now - state.committed_at).total_seconds() <= ctx.config["max_age_minutes"] * 60
```

---

## 7. 示例 E: 条件节点, 读宿主计数

调用预算。计数由宿主维护, 条件节点只读。

```yaml
# llm_budget/meta.yaml
name: llm_budget
version: 0.2.0

flow_nodes:
  - name: within                         # llm_budget.within
    label: 调用预算内
    description: 最近一段时间的模型调用次数低于上限时为真
    out: condition
    config_schema:
      max_calls: {type: number, label: 上限, default: 30, minimum: 1, integer: true}
      window_minutes: {type: number, label: 统计窗口, default: 60, minimum: 1, maximum: 1440, integer: true}
      count:
        type: select
        label: 计入
        default: all
        options: [wf, all]
        option_labels:
          wf: 只算 WF 调用
          all: WF 和插件模型调用        # 默认包括插件节点内部的模型调用, 堵住预算漏洞
      scope:
        type: select
        label: 统计范围
        default: target
        options: [target, flow]
        option_labels:
          target: 本次将被触发的 WF
          flow: 整个流程
```

```python
# llm_budget/flow_nodes.py
def within(ctx) -> bool:
    used = ctx.calls(                    # 宿主计数: 已完成的模型调用次数
        kind=ctx.config["count"],
        scope=ctx.config["scope"],
        window_seconds=ctx.config["window_minutes"] * 60,
    )
    return used < ctx.config["max_calls"]
```

这个条件节点是**软限制**: 只在触发前判断"要不要开始", 不保证上限。一次 Agent 运行可能有十几轮模型调用, 插件节点内部也会调模型, 条件节点管不住。

需要保证上限时用宿主的**硬限制**, 它不是节点, 写在流程或 WF 配置里:

```jsonc
// params.flow 节选
{
  "budget": {                            // 整个流程 (单个群) 的硬限制, 可选
    "max_calls": 200,                    // 窗口内最多多少次模型调用
    "window_minutes": 60,
    "count": "all"                       // all: WF 和插件模型调用都算; wf: 只算 WF
  },
  "nodes": [
    {
      "id": "agent",
      "type": "react_workflow",
      "config": {
        "max_calls_per_run": 15          // 单次运行最多多少次模型调用, 可选; 和 max_steps 并列
      }
    }
  ]
}
```

硬限制的执行方式: 每次模型调用前, 宿主原子地检查并扣减计数 (WF 和插件节点的调用走同一个计数器)。超出时这次调用失败, 走错误路径: WF 本次运行失败, 阈值位置不推进, 状态槽不提交。详见运行时契约第 5 节。

---

## 8. 示例 F: transform, 调用模型

上下文压缩。`config_schema` 里有 `llm` 字段, 宿主自动标记"插件模型"。

```yaml
# context_compress/meta.yaml
name: context_compress
version: 0.2.1

flow_nodes:
  - name: compress                       # context_compress.compress
    label: 上下文压缩
    description: 输入超过目标长度时用小模型压缩
    in: {context: context}
    out: context                         # 单输出, 端口名默认 out
                                         # 条件为假时 out 原样等于 context (推断)
    reading:
      context: "超过 {target_chars} 字时压缩"
    config_schema:
      model_name: {type: llm, label: 压缩模型, required: true, group: 模型}   # 有 llm 字段 → 视为调用模型
      target_chars: {type: number, label: 目标长度, default: 6000, minimum: 1000, maximum: 50000, integer: true, group: 压缩}
      keep_snapshot: {type: bool, label: 快照不压缩, default: true, group: 压缩}
```

```python
# context_compress/flow_nodes.py
from satrap.edictum.flow import ContextBlock


async def compress(ctx, context):
    target = ctx.config["target_chars"]
    if sum(len(b.text) for b in context) <= target:
        return context                   # 没超长, 原样输出
    keep = [b for b in context if ctx.config["keep_snapshot"] and b.kind == "snapshot"]
    rest = [b for b in context if b not in keep]
    budget = target - sum(len(b.text) for b in keep)
    if budget <= 0:                      # 保留的快照本身就超长
        ctx.diagnostic("snapshot_over_target")
        return context                   # 不压缩, 原样输出, 让下游的长度检查处理
    llm = ctx.resources["model_name"]    # 沿用现有 PluginResources 写法 (plugin/resources.py:147,156)
    summary = await llm.chat(            # 调用次数由宿主计入 plugin 计数
        f"把下面的群聊压缩到 {budget} 字以内, 保留人物, 时间和约定:\n" + "\n".join(b.text for b in rest)
    )
    return keep + [ContextBlock(kind="summary", text=summary)]
```

---

## 9. 示例 G: transform, 可选输入 + 知识库

检索补充。消息必填, 快照可选; 有快照时把快照也作为检索依据。

```yaml
# rag_context/meta.yaml
name: rag_context
version: 0.1.0
interop:                                 # 现有字段
  requires:
    rag: true                            # 依赖 rag 插件; 未安装时本插件装不上

flow_nodes:
  - name: retrieve                       # rag_context.retrieve
    label: 知识库检索
    description: 用最近几条消息检索知识库, 产出参考资料块
    in:
      messages: messages                 # 必填
      snapshot: state?                   # 带 ? 表示可选; 不连线时参数为 None
    out: context
    passthrough: {out: null}             # 必须手写: 必填输入里没有 context 类型, 推断不出
                                         # null 表示条件为假时输出空值 (), 即"不检索就没有参考资料"
    config_schema:
      knowledge_bases: {type: knowledge_bases, label: 知识库, required: true, scope: global}   # 现有字段类型
      top_k: {type: number, label: 返回条数, default: 4, minimum: 1, maximum: 20, integer: true}
      query_messages: {type: number, label: 检索用消息数, default: 5, minimum: 1, maximum: 50, integer: true}
      rerank_model: {type: rerank, label: 重排模型, nullable: true}   # 可以不选
```

```python
# rag_context/flow_nodes.py
from satrap.edictum.flow import ContextBlock


async def retrieve(ctx, messages, snapshot=None):   # 可选端口给默认值 None
    query = "\n".join(m.text for m in messages[-ctx.config["query_messages"]:])
    if snapshot is not None and snapshot.version > 0:   # 连了线但状态槽为空时是版本 0 的空视图, 不是 None
        query = snapshot.content[:500] + "\n" + query
    rag = ctx.service("rag", "search")   # 调用依赖插件的服务 (现有 services 机制)
    hits = await rag(query=query, knowledge_bases=ctx.config["knowledge_bases"], top_k=ctx.config["top_k"])
    return [ContextBlock(kind="reference", text=h["text"]) for h in hits]
```

---

## 10. 示例 H: transform, 多个输出

按发言人拆分成两路。

```yaml
# message_split/meta.yaml
name: message_split
version: 0.1.0

flow_nodes:
  - name: by_role                        # message_split.by_role
    label: 按发言人拆分
    description: 把消息拆成成员发言和机器人发言两路
    in: {messages: messages}
    out:                                 # 多输出必须写 {端口名: 类型}
      members: messages                  # 推断: 必填输入里 messages 类型只有一个 → 条件为假时 members 拿到全部消息
      bot: messages                      # 推断结果同上, 也会拿到全部消息; 这里不想要, 所以下面手写
    passthrough:                         # 手写时每个输出都要列出, 不允许部分省略
      members: messages                  # 条件为假时: 全部消息都算成员发言
      bot: null                          # 条件为假时: 机器人这一路为空
    config_schema:
      treat_as_bot: {type: textarea, label: 视为机器人的账号, default: "", description: 每行一个账号 ID}
```

```python
# message_split/flow_nodes.py
def by_role(ctx, messages):
    extra = {x.strip() for x in ctx.config["treat_as_bot"].splitlines() if x.strip()}
    bot = [m for m in messages if m.is_bot or m.sender_id in extra]
    members = [m for m in messages if m not in bot]
    return {"members": members, "bot": bot}   # 多输出返回 dict, 键必须和 out 一致
```

---

## 11. 示例 I: 同一插件里的工具和节点

插件原有的 `tools` / `config_schema` 和新的 `flow_nodes` 互不影响。

```yaml
# group_insight/meta.yaml
name: group_insight
version: 1.0.0
config_schema:                           # 插件自己的配置; 和节点配置是两套
  stats_retention_days: {type: number, default: 30}
tools:                                   # 现有能力: 给 WF 用的工具
  group_activity: 查询群活跃度

flow_nodes:
  - name: hot_topics                     # group_insight.hot_topics
    label: 热门话题
    in: {messages: messages}
    out: text                            # 产出文本, 可以接 WF 的数据口
    passthrough: {out: null}             # 必须手写: 没有 text 类型的必填输入; 条件为假时输出 ""
    config_schema:
      top_n: {type: number, label: 话题数, default: 3, minimum: 1, maximum: 10, integer: true}
```

```python
# group_insight/flow_nodes.py
async def hot_topics(ctx, messages):
    ...
    return "1. 周六看展\n2. 午饭吃云南菜"
```

---

## 12. 示例 J: 装在 WF 上的插件 (`flow.workflow`)

不是节点, 是现有插件装到 WF 上时的限制。只用于编辑时的静态检查; 运行时仍按现有权限, 执行开关和能力状态判断, 两者不一致时以运行时为准。

流程的发送由平台回复节点自带的 `flow_reply` 工具完成, 不依赖任何插件。group_chat 是可选插件: 要查成员, 看历史消息, 发 @ / 图片 / 表情这类富消息时才装。

```yaml
# group_chat/meta.yaml (节选)
flow:                                    # 新增分组
  workflow:                              # 装到 WF 节点上时
    foreground_only: true                # 只能装在前台 WF; 装在纯后台 WF 上报 flow_plugin_placement
                                         # (group_chat_reply 需要来源回复轮次, 后台运行没有)
```

```yaml
# memory/meta.yaml (节选)
flow:
  workflow:
    provides: [memory_write]             # 会写长期记忆; 装在后台 WF 上时提示 flow_memory_write_background
```

```yaml
# flow_snapshot/meta.yaml (内置插件, 节选)
flow:
  workflow:
    provides: [state_write]              # WF 写状态槽却没有 state_write 提供者 → flow_snapshot_plugin_missing
                                         # 校验器不写死插件名 flow_snapshot
```

```yaml
# image_generation/meta.yaml (节选)
interop:                                 # 依赖关系继续用现有字段
  requires:
    group_chat: {optional: true}         # 同一个 WF 上没装 group_chat → flow_plugin_dependency;
                                         # optional 让普通会话里 image_generation 单独可装,
                                         # 但流程编辑时仍按"需要同装"检查 (发图依赖 group_chat 富消息通道)
flow:
  workflow:
    foreground_only: true
```

`flow.workflow` 只有两个字段: `foreground_only` (默认 false) 和 `provides` (默认空)。`provides` 的取值首版固定为 `memory_write` / `state_write`。没写 `flow` 分组的插件按默认处理。

平台回复节点的发送方式 (节点配置 `send_mode`):

| 取值 | 发送 | 可用范围 |
| --- | --- | --- |
| `flow_reply` (默认) | 宿主注入工具 `flow_reply(text, quote_trigger?)`; 不调用即沉默 | 群, 私聊, 前台, 后台 |
| `group_chat_reply` | 用 group_chat 的富消息回复; WF 必须装 group_chat | 只在群的前台运行; 后台或私聊报错 |
| `final_text` | 把 WF 最终文本直接发出 | 同 `flow_reply` |

---

## 13. 示例 K: 插件全局默认放在哪

单独存一个文件 `.satrap/config/plugins/<插件名>.flow_nodes.json`, 不和插件自己的全局配置混在一起。

上一版写的是放进插件原有文件的保留键 `__flow_nodes`, 核对代码后改掉了, 原因:

| 问题 | 代码位置 |
| --- | --- |
| `save_global` 只写 schema 声明过的键, 不合并旧文件, 插件配置页保存一次就会清掉 `__flow_nodes` | `satrap/edictum/plugin/config.py:484-517` |
| 现有保留键 `__capability_migration` 能保住, 是靠两处写死 `friend_manager` 的特例 | 同上 `:452`, `:511-514` |
| `load_global` 遇到未声明的键会打警告, 每次读取都会报 | 同上 `:456-457` |
| 插件配置页的并发检查用整个文件的 SHA256; 共用文件时, 改节点默认会让正在编辑插件参数的页面保存冲突, 反之亦然 | `satrap/core/config/plugin_service.py:97-98`, `:138` |

单独文件的读写复用 `PluginConfigManager` 的锁和原子写 (`FileLock`, `atomic_write_json`), 并发检查同样用文件 SHA256, 但和插件参数互不影响。不会触发插件重装: 现有插件重装只看 `config` 和 `resources_revision` (`satrap/edictum/plugin/runtime.py:347-352`), 后者只是模型引用的指纹 (`satrap/edictum/plugin/resources.py:191-203`), 都不读这个文件。

下面的注释只是说明, 实际文件是纯 JSON。

```jsonc
// .satrap/config/plugins/quiet_hours.flow_nodes.json
{
  "format": 1,                           // 文件格式版本, 宿主自己用
  "nodes": {
    "in_hours": {                        // 节点名
      "version": 1,                      // 这组默认值按哪个节点契约版本写的; 迁移靠它判断, 见示例 N
      "config": {                        // 只写要覆盖的字段; 版本和配置分开, 插件字段叫 version 也不冲突
        "start": "23:00",                // 覆盖类型默认 22:00, 对所有流程生效
        "end": "08:00"
      }
    }
  }
}
```

需要新增的接口: `GET/PUT /config/plugins/<插件名>/flow-nodes`, 请求和返回的形状与现有 `/config/plugins/<插件名>/config` 一致: PUT 体为 `{config, expected_revision}` (现有接口见 `satrap/core/backend/control_server.py:3547-3552`), 这里的 `config` 是 `{节点名: {version, config}}`。插件配置页多一个"节点默认"分区, 表单由节点的 `config_schema` 生成。

---

## 14. 示例 L: 流程存档

`edictum_session_config.json` 中一个命名配置的节选。下面的注释只是说明。

```jsonc
{
  "edictum_type": "flow",
  "params": {
    "flow": {
      "version": 3,                      // 流程格式版本 (和节点契约版本, 插件发行版本是三个不同的东西)
      "defaults": {                      // 流程默认, 按完整类型名分组
        "quiet_hours.in_hours": {
          "version": 1,                  // 这组默认值按哪个节点契约版本写的, 和节点实例的 version 同义
          "config": { "end": "09:00" }
        }
      },
      "nodes": [
        {
          "id": "kf_1",                  // 节点 id, 流程内唯一
          "type": "keyword_filter.filter", // 完整类型名, 按最后一个点拆分
          "version": 2,                  // 保存时的节点契约版本; 低于当前版本时先迁移
          "title": "过滤广告",
          "config": { "blocked_words": "加群\n代理" }   // 只存和上层不同的字段
        },
        {
          "id": "quiet",
          "type": "quiet_hours.in_hours",
          "version": 1,
          "title": "免打扰时段内",
          "config": {}                   // 全部继承; 合并顺序见下
        }
      ],
      "edges": [
        { "from": "bucket.out", "to": "kf_1.messages" },
        { "from": "kf_1.kept", "to": "rp_input.messages", "label": "过滤后" },   // label 是用户注释, 可选
        { "from": "daytime.out", "to": "kf_1.when" }      // when 是宿主自动加的启用条件口
      ],
      "layout": { "kf_1": { "x": 400, "y": 260 } }
    }
  }
}
```

配置合并顺序 (后者覆盖前者), 以 `quiet` 节点的 `end` 为例:

| 层 | 来源 | 值 |
| --- | --- | --- |
| 类型默认 | `meta.yaml` 的 `default` | `07:00` |
| 插件全局默认 | `quiet_hours.flow_nodes.json` 的 `nodes.in_hours.config` | `08:00` |
| 流程默认 | `params.flow.defaults["quiet_hours.in_hours"].config` | `09:00` |
| 本节点 | `nodes[].config` | 未写 |
| 生效 | | `09:00` |

规则:

- 某层没有这个键 = 继承上一层; 编辑器里"恢复继承"就是删掉本层的键
- 某层写了 `null`: 字段 `nullable: true` 时表示"明确为空", 否则校验报错; `null` 不等于继承
- 对象和数组整体替换, 不深合并
- 首版不支持单个群覆盖流程节点的字段, `session_overridable` 对流程节点不生效; 需要不同值时另建一个流程配置绑定到该群

插件停用后, 存档原样保留, 校验报 `flow_node_plugin_disabled`, 流程不可运行; 宿主不自动删节点, 也不跳过节点运行。

---

## 15. 示例 M: 需要初始化资源时用类写法

大多数节点用函数即可。只有需要建连接, 加载大文件这类一次性初始化时才用类。

```python
# sensitive_words/flow_nodes.py
from satrap.edictum.flow import FlowNode


class Detect(FlowNode):                  # 继承 FlowNode 表示类写法
    node_name = "detect"                 # 对应 flow_nodes 里 name: detect 的那一项

    async def setup(self, ctx):          # 会话创建, 或本节点配置变化后重建实例时调用
        self.trie = await load_trie(ctx.config["dict_path"])   # 加载词库, 可以较慢

    def __call__(self, ctx, messages):   # 和函数写法的签名完全一样
        return [m for m in messages if not self.trie.match(m.text)]

    async def close(self):               # 会话释放, 或实例被替换时调用
        self.trie = None
```

实例规则:

- 每个流程会话里, 每个节点 id 一个实例
- 本节点生效配置变化 (包括上层默认变化导致的) 时, 宿主新建实例并 `setup`, 正在运行的那次用旧实例跑完, 然后 `close` 旧实例; 和现有插件"配置变化即重装"一致 (`plugin/runtime.py:347-352`)
- 实例不应该跨运行保存业务状态; 需要跨运行的数据用状态槽或插件自己的存储

---

## 16. 示例 N: 升级与迁移

`keyword_filter.filter` 从 v1 升到 v2: 字段 `words` 改名为 `blocked_words`, 输入端口 `in` 改名为 `messages`, 新增字段 `include_bot`。

```yaml
flow_nodes:
  - name: filter
    version: 2                           # 有改名, 所以版本加 1
    renamed:                             # 改名记录; 键是"升到哪个版本"
      2:
        fields: {words: blocked_words}   # 配置字段改名
        ports: {in: messages}            # 端口改名 (输入输出都在这里写)
    in: {messages: messages}
    out: {kept: messages}
    config_schema:
      blocked_words: {type: textarea, label: 屏蔽词, default: ""}
      include_bot: {type: bool, label: 也过滤机器人发言, default: false}   # 新增字段不用记录, 宿主按默认补齐
```

需要迁移的有四处, 每处都带自己的版本号, 各自独立判断:

| 位置 | 版本记在哪 |
| --- | --- |
| 节点实例配置 | `nodes[].version` |
| 流程默认 | `defaults["keyword_filter.filter"].version` |
| 插件全局默认 | `keyword_filter.flow_nodes.json` 的 `nodes.filter.version` |
| 连线端口 | 跟随所连节点实例的 `version` |

版本号是必需的: 只看键名分不出"还没迁移"和"本来就叫这个名字"。比如 v2 把 `a` 改名成 `b`, v3 又新增一个叫 `a` 的字段, 不看版本就会迁错。

宿主对每处配置, 从记录的版本逐版本升到当前版本, **全部迁移完成后才校验**:

```text
from = 记录的版本 (缺失按 1)
for v in from+1 .. 当前 version:
    1. 按 renamed[v].fields 改键名
    2. 有 migrate() 时调用 migrate(节点名, v, config)
最后用当前 config_schema 校验一次; 新增字段按默认补齐
```

连线按所连节点实例的版本, 逐版本应用 `renamed[v].ports`。

以 v1 升到 v2 为例:

1. 节点 `kf_1` (`version: 1`): `words` 改成 `blocked_words`
2. 流程默认 (`version: 1`): 同样改键名
3. 插件全局默认 (`version: 1`): 同样改键名
4. 连线 `to: kf_1.in` 改成 `to: kf_1.messages`
5. 三处都用 v2 的 `config_schema` 校验
6. 内存里按 v2 运行。迁移结果什么时候写回:
   - 节点实例, 流程默认, 连线: 用户下次保存流程时写成 `version: 2`
   - 插件全局默认: 插件升级后宿主首次加载时写回一次 (同一个文件只有一份, 不用等用户操作)

改名表达不了的变化 (拆分字段, 换算单位) 才需要写函数:

```python
# keyword_filter/flow_nodes.py
def migrate(node: str, to_version: int, config: dict) -> dict:
    # 每升一个版本调用一次, 在该版本的 renamed 处理完之后
    # 拿到的是迁移中间结果, 还没校验; 返回值也不在这里校验, 全部版本迁移完后统一校验
    if node == "filter" and to_version == 3:
        config["blocked_words"] = config["blocked_words"].replace(",", "\n")   # 例: v3 把逗号分隔改成每行一个
    return config
```

| 变化 | 需要做什么 |
| --- | --- |
| 新增字段或可选输入 | 什么都不用做, 不用提高版本 |
| 字段或端口改名 | 提高 `version`, 写 `renamed` |
| 删除字段 | 提高 `version`; 旧值被丢弃, 宿主写一条诊断 |
| 删除端口, 改端口类型, 可选输入改成必填 | 不兼容; 连到旧端口的线报 `flow_port_unknown` 或 `flow_port_type_mismatch`, 需要用户在编辑器里处理 |
| 新增必填输入 | 不兼容; 已有流程里这个口没连线, 报 `flow_port_required` |
| 改 `passthrough` | 行为变化, 提高 `version`; 不需要 `renamed` |
| 字段含义变化 | 提高 `version`, 写 `migrate()` |

迁移失败 (函数抛异常, 或结果校验不过) 时报 `flow_node_config_stale`, 指出是哪一处, 流程不可运行, 原始配置不改动。记录的版本高于当前版本 (插件被降级) 时不迁移, 报 `flow_node_version_ahead`。

---

## 17. 宿主提供的对象

都在 `satrap.edictum.flow`。所有传给节点的数据都是只读的, 节点之间共享同一份结果, 不能原地修改。

| 对象 | 成员 |
| --- | --- |
| `ctx` (transform) | `config` (生效配置, 只读), `resources` (现有 `PluginResources`, 按模型字段取客户端), `service(插件, 服务名)`, `node_id`, `scope` (群), `now`, `diagnostic(code, **data)`, `replace_ignore_case()` |
| `ctx` (条件) | `config`, `now`, `local_time_between(start, end)`, `kind` (`message` / `batch` / `call`), `message` (仅 `kind == message`), `batch` (仅 `kind == batch`), `calls(kind, scope, window_seconds)` |
| `FlowMessage` (frozen) | `message_id`, `position` (桶内单调序号), `sender_id`, `nickname`, `text`, `is_bot`, `mentions_bot`, `time`, `with_text(text)` |
| `ContextBlock` (frozen) | `kind` (`snapshot` / `messages` / `summary` / `reference`), `text`, `source_node` |
| `StateView` (frozen) | `content`, `version`, `covers_to`, `committed_at` |
| `node(name)` | 装饰器, 函数名不能用作节点名时指定节点名 |
| `FlowNode` | 类写法基类, 可选 `setup(ctx)` / `close()` |

条件节点的 `ctx` 没有 `resources` 和 `service`: 条件在每条消息到达时都可能求值, 不允许 IO。需要外部数据的判断, 由插件自己的后台任务定期更新缓存, 条件只读缓存 (首版不提供专门接口)。

---

## 18. 安装时会被拒绝的写法

插件安装或更新时检查, 不通过就安装失败并回滚。

```yaml
flow_nodes:
  - name: Bad-Name                       # 节点名不合法 → flow_contract_name
    label: x
    out: text
  - name: bad_type
    label: x
    out: json                            # 不在可用类型里 → flow_contract_port_type
  - name: bad_signal
    label: x
    out: event                           # 插件不能产出 event / handoff → flow_contract_port_type
  - name: bad_condition_mix
    label: x
    out: {ok: condition, rest: messages} # condition 只能作为唯一输出 → flow_contract_port_type
  - name: bad_condition_input
    label: x
    in: {m: messages}                    # 条件节点只能接 state → flow_contract_port_type
    out: condition
  - name: reserved_port
    label: x
    in: {when: messages}                 # when 是保留名 → flow_contract_name
    out: text
  - name: bad_visible_when
    label: x
    out: text
    config_schema:
      a: {type: string, visible_when: {mode: x}}   # 引用了不存在的字段 → flow_contract_visible_when
  - name: bad_needs
    label: x
    in: {m: messages}
    out: text
    needs: message                       # needs 只能用于条件节点 → flow_contract_needs
  - name: bad_renamed
    label: x
    version: 2
    renamed: {3: {fields: {a: b}}}       # 记录的版本高于当前 version → flow_contract_renamed
    out: text
```

```yaml
flow_nodes:
  - name: no_passthrough
    label: x
    in: {messages: messages}
    out: text                            # 必填输入里没有 text 类型, 推断不出 → flow_contract_passthrough
                                         # 修法: 加 passthrough: {out: null}
  - name: ambiguous_passthrough
    label: x
    in: {a: messages, b: messages}
    out: messages                        # 必填输入里有两个 messages, 不知道选哪个 → flow_contract_passthrough
  - name: partial_passthrough
    label: x
    in: {messages: messages}
    out: {x: messages, y: messages}
    passthrough: {x: messages}           # 手写时必须列出全部输出, 缺 y → flow_contract_passthrough
  - name: wrong_passthrough
    label: x
    in: {ctx: context}
    out: messages
    passthrough: {out: ctx}              # context 交给 messages, 类型不一致 → flow_contract_passthrough
  - name: optional_passthrough
    label: x
    in: {extra: "messages?"}             # 单行 {} 写法里带 ? 的类型必须加引号, 否则 YAML 解析失败
    out: messages
    passthrough: {out: extra}            # 不能指向可选输入 (不连线时没有值) → flow_contract_passthrough
```

加载 `flow_nodes.py` 时再检查:

| 问题 | 报错 |
| --- | --- |
| 声明了节点但找不到同名函数, 类或 `@node` | `flow_contract_unpaired` |
| 有 `@node` 或 `FlowNode` 子类但没在 `meta.yaml` 声明 | `flow_contract_unpaired` |
| 参数名和输入端口名对不上 (`ctx` 除外); 可选端口没有默认值 | `flow_contract_signature` |
| 条件节点是 `async def` | `flow_contract_signature` |

和现有 `tools` 规则的区别: `tools` 的声明只是描述, 以扫描结果为准 (`satrap/edictum/plugin/base.py:16-18`)。节点的端口和配置决定画布结构, 声明与实现必须严格一致。
