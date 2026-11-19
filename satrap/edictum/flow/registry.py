"""
流程会话节点类型注册表

按完整类型名登记内置节点和插件节点 (FlowNodeType),
供静态检查, 定义解析和运行引擎查找端口, 配置与迁移规则
"""
from __future__ import annotations

from typing import Any

from satrap.core.type.flow.ports import PortType
from satrap.core.type.flow.registry import EventContext, FlowNodeType, NodeRole, PortSpec, RenamedEntry
from satrap.core.type.plugin.config import ConfigField


def _ports(items: list[tuple[str, PortType | tuple[PortType, ...], str]]) -> tuple[PortSpec, ...]:
    """
    把 (名称, 类型或类型组, 中文名) 简写转成 PortSpec, 选项由调用方后处理

    参数:
    - items: 端口简写列表

    返回:
    - tuple[PortSpec, ...]: 仅含名称, 类型和中文名的端口声明
    """
    return tuple(
        PortSpec(name=name, types=(types,) if isinstance(types, str) else tuple(types), label=label)
        for name, types, label in items
    )


def _fields(items: dict[str, dict[str, Any]]) -> dict[str, ConfigField]:
    """
    把字段简写转成 ConfigField 表, 键顺序即声明顺序

    参数:
    - items: {字段名: 字段定义}, 定义键与 ConfigField 成员一致

    返回:
    - dict[str, ConfigField]: 配置字段表
    """
    return {name: ConfigField(name=name, **spec) for name, spec in items.items()}


def _time_field(label: str, default: str) -> dict[str, Any]:
    """时间段条件的 HH:MM 字段简写"""
    return {"type": "time", "label": label, "default": default,
            "description": "HH:MM, 跨零点时按次日计算"}


DEFAULT_SOURCE_MAX_MESSAGES = 200
"""source/bucket 直连 WF 或自定义 transform 时的默认消息硬上限。"""

WHEN_PORT = PortSpec(name="when", types=("condition",), multiple=True, side="left", label="启用条件")
"""transform 自动携带的左侧启用条件口, 不接线等于永远启用"""

_GATE_INPUTS: tuple[PortSpec, ...] = (
    PortSpec(name="a", types=("event", "condition"), required=True, side="left", label="A"),
    PortSpec(name="b", types=("event", "condition"), required=True, side="left", label="B"),
)
"""逻辑门输入, 同时接受触发和条件; 输出类型由输入推导 (derived)"""


def builtin_node_types() -> list[FlowNodeType]:
    """
    返回全部内置节点类型, 端口与配置以确认的 demo registry.ts 为准

    返回:
    - list[FlowNodeType]: 内置节点类型列表
    """
    # 与 demo 的差异 (以后端计划为准):
    # output 用 send_mode 取代 fallback_send
    # state 加 on_new, threshold 加 on_new
    # react_workflow 加 max_calls_per_run
    # workflow 的 input 口接受 context/messages/text/state 四种数据类型
    return [
        # ---------- 数据 ----------
        FlowNodeType(
            type="source", version=1, label="对话消息", category="数据", role="source",
            description="当前对话 (群聊或私聊) 的消息档案, 包含已确认发出的机器人消息", max_per_flow=1,
            outputs=_ports([("out", "messages", "消息")]),
            config_schema=_fields({
                "max_messages": {
                    "type": "number", "label": "直连消息上限", "default": DEFAULT_SOURCE_MAX_MESSAGES,
                    "minimum": 1, "maximum": 5000, "integer": True,
                    "description": "直连 WF 或自定义 transform 时最多读取最新多少条; assemble 使用自己的窗口"},
            }),
        ),
        FlowNodeType(
            type="bucket", version=2, label="桶", category="数据", role="transform",
            description="档案上的只读视图; 游标属于消费它的阈值磁贴",
            inputs=(
                PortSpec(name="in", types=("messages",), required=True, label="消息"),
                WHEN_PORT,
            ),
            outputs=_ports([("out", "messages", "消息")]),
            config_schema=_fields({
                "include_outbound": {"type": "bool", "label": "包含机器人发言", "default": True,
                                     "description": "机器人已确认发出的消息也进入桶"},
                "max_messages": {
                    "type": "number", "label": "直连消息上限", "default": DEFAULT_SOURCE_MAX_MESSAGES,
                    "minimum": 1, "maximum": 5000, "integer": True,
                    "description": "直连 WF 或自定义 transform 时最多输出最新多少条; assemble 使用自己的窗口"},
            }),
        ),
        FlowNodeType(
            type="state", version=2, label="状态槽", category="数据", role="state",
            description="每个版本记录内容, 版本号和覆盖到的消息位置; 只能有一个写入方",
            inputs=_ports([("write", "text", "写入")]),
            outputs=_ports([("read", "state", "读取")]),
            config_schema=_fields({
                "schema": {"type": "select", "label": "格式", "default": "text",
                           "options": ["text", "sections"], "option_labels": {"text": "自由文本", "sections": "分段"}},
                "sections": {"type": "textarea", "label": "段落", "default": "",
                             "description": "每行一个段名, 提示词可以按段引用",
                             "visible_when": {"schema": "sections"}},
                "max_chars": {"type": "number", "label": "长度上限", "default": 6000,
                              "minimum": 200, "maximum": 1000000, "integer": True},
                "writer_reads_previous": {"type": "bool", "label": "写入方读取上一版", "default": True,
                                          "description": "写入它的 WF 每次运行时自动拿到上一次提交的版本, 用于增量更新; 不需要再连回读线"},
                "on_new": {"type": "select", "label": "/new 时", "default": "keep",
                           "options": ["keep", "clear"],
                           "option_labels": {"keep": "保留内容", "clear": "清空并回到版本 0"},
                           "description": "clear 时 /new 生效后版本回到 0, 内容为空"},
            }),
        ),
        FlowNodeType(
            type="assemble", version=2, label="上下文拼装", category="数据", role="transform",
            description="被 WF 拉取时, 把快照, 快照未覆盖的消息和最新对话拼成模型输入",
            inputs=(
                PortSpec(name="snapshot", types=("state",), multiple=True, label="快照"),
                PortSpec(name="messages", types=("messages",), required=True, label="消息"),
                WHEN_PORT,
            ),
            outputs=_ports([("context", "context", "上下文")]),
            config_schema=_fields({
                "after_coverage": {"type": "bool", "label": "快照未覆盖的消息", "default": True,
                                   "description": "从所连快照覆盖到的位置之后开始截取"},
                "max_pending": {"type": "number", "label": "最多条数", "default": 80,
                                "minimum": 1, "maximum": 500, "integer": True,
                                "visible_when": {"after_coverage": True}},
                "max_pending_chars": {"type": "number", "label": "字符预算", "default": 8000,
                                      "minimum": 500, "maximum": 1000000, "integer": True,
                                      "visible_when": {"after_coverage": True}},
                "latest_n": {"type": "number", "label": "最新 n 条对话", "default": 20,
                             "minimum": 0, "maximum": 200, "integer": True,
                             "description": "不足 n 条时补入覆盖位置之前的消息", "session_overridable": True},
                "dedupe": {"type": "bool", "label": "按消息 ID 去重", "default": True},
            }),
        ),
        # ---------- Workflow ----------
        FlowNodeType(
            type="react_workflow", version=3, label="ReAct Workflow", category="Workflow", role="workflow",
            description="模型和工具循环调用, 直到模型不再调用工具 (现有 SimpleWorkflow); 流程里会调用 LLM 的节点",
            inputs=(
                PortSpec(name="input", types=("context", "messages", "text", "state"), multiple=True, label="数据"),
                PortSpec(name="trigger", types=("event",), multiple=True, side="left", label="触发"),
            ),
            outputs=(
                _ports([("text", "text", "输出")])[0],
                PortSpec(name="done", types=("event",), side="right", label="完成"),
                PortSpec(name="tool", types=("handoff",), side="right", multiple=True, label="交接"),
            ),
            config_schema=_fields({
                "model_name": {"type": "llm", "label": "模型配置", "default": "", "group": "模型"},
                "thinking": {"type": "select", "label": "思考强度", "default": "default",
                             "options": ["default", "off", "low", "medium", "high"],
                             "option_labels": {"default": "跟随模型默认", "off": "关闭", "low": "低", "medium": "中", "high": "高"},
                             "group": "模型"},
                "max_steps": {"type": "number", "label": "最大工具轮数", "default": 8,
                              "minimum": 1, "maximum": 64, "integer": True, "group": "模型"},
                "max_calls_per_run": {"type": "number", "label": "单次运行模型调用上限", "default": 0,
                                      "minimum": 0, "maximum": 1000, "integer": True, "group": "运行",
                                      "description": "0 表示不限制; 只算本 WF 自己发起的模型调用"},
                "timeout_seconds": {"type": "number", "label": "单次运行超时", "default": 60,
                                    "minimum": 1, "maximum": 3600, "integer": True, "group": "运行",
                                    "description": "包含输入拉取和模型工具循环; 超时后本次运行失败"},
                "on_busy": {"type": "select", "label": "运行中再次触发", "default": "queue",
                            "options": ["queue", "merge", "drop"],
                            "option_labels": {"queue": "排队依次运行", "merge": "合并为一次重跑", "drop": "丢弃"},
                            "group": "运行"},
                "prompt": {"type": "textarea", "label": "系统提示词", "default": "", "group": "提示词"},
                "plugins": {"type": "textarea", "label": "节点插件", "default": "", "group": "插件",
                            "description": "每行一个插件名, 只装在这个 WF 上, 其他节点看不到这些工具"},
                "keep_history": {"type": "bool", "label": "保留对话历史", "default": False, "group": "运行",
                                 "description": "关闭时每次只用拉取到的输入和触发带来的上游输出"},
            }),
        ),
        FlowNodeType(
            type="output", version=2, label="平台回复", category="输出", role="sink",
            description="通过流程自带的发送通道回复, 可接多个 WF; 可接发送条件",
            inputs=(
                PortSpec(name="reply", types=("text",), required=True, multiple=True, label="回复"),
                PortSpec(name="when", types=("condition",), multiple=True, side="left", label="发送条件"),
            ),
            config_schema=_fields({
                "send_mode": {"type": "select", "label": "发送方式", "default": "flow_reply",
                              "options": ["flow_reply", "group_chat_reply", "final_text"],
                              "option_labels": {"flow_reply": "工具发送 (可沉默)", "group_chat_reply": "群聊富消息", "final_text": "直发最终文本"},
                              "description": "group_chat_reply 仅群聊前台运行可用, 需要 WF 装 group_chat"},
            }),
        ),
        # ---------- 触发磁贴 ----------
        FlowNodeType(
            type="wake", version=1, label="唤醒", category="触发", role="trigger", emits="message",
            description="每条消息到达时判断是否唤醒; 沿用平台策略时群聊按 @, 唤醒词和频率, 私聊每条都唤醒; 一个磁贴可以驱动多个 WF",
            inputs=(
                PortSpec(name="messages", types=("messages",), required=True, label="消息"),
                PortSpec(name="when", types=("condition",), multiple=True, side="left", label="条件"),
            ),
            outputs=_ports([("event", "event", "触发")]),
            config_schema=_fields({
                "use_platform_policy": {"type": "bool", "label": "沿用平台唤醒策略", "default": True,
                                        "description": "群聊使用平台管理里该群的 @, 唤醒词, 频率和冷却; 私聊每条消息都唤醒"},
                "mention": {"type": "bool", "label": "被 @ 时唤醒", "default": True,
                            "description": "私聊消息视为已 @",
                            "visible_when": {"use_platform_policy": False}},
                "wake_words": {"type": "textarea", "label": "唤醒词", "default": "",
                               "description": "每行一个", "visible_when": {"use_platform_policy": False}},
                "new_messages": {"type": "number", "label": "新消息数", "default": 5,
                                 "minimum": 0, "integer": True,
                                 "description": "上次唤醒后累计到这么多条也唤醒, 0 表示不按数量",
                                 "visible_when": {"use_platform_policy": False}},
                "cooldown": {"type": "number", "label": "冷却", "default": 30,
                             "minimum": 0, "maximum": 3600, "integer": True, "session_overridable": True},
            }),
        ),
        FlowNodeType(
            type="threshold", version=1, label="阈值", category="触发", role="trigger", emits="batch",
            description="游标之后的消息满足任一阈值时触发; 每个被驱动的 WF 各有一份已提交位置",
            inputs=(
                PortSpec(name="messages", types=("messages",), required=True, label="消息"),
                PortSpec(name="when", types=("condition",), multiple=True, side="left", label="条件"),
            ),
            outputs=_ports([("event", "event", "触发")]),
            config_schema=_fields({
                "messages": {"type": "number", "label": "消息数", "default": 30, "minimum": 0, "integer": True},
                "chars": {"type": "number", "label": "字符数", "default": 4000, "minimum": 0, "integer": True},
                "idle_seconds": {"type": "number", "label": "距上次触发", "default": 600,
                                 "minimum": 0, "integer": True, "description": "0 表示不按时间"},
                "min_messages": {"type": "number", "label": "最少消息", "default": 1,
                                 "minimum": 1, "integer": True, "description": "按时间触发时游标后至少要有的新消息数"},
                "max_batch": {"type": "number", "label": "单批上限", "default": 200,
                              "minimum": 10, "maximum": 1000, "integer": True,
                              "description": "积压超过时分批触发"},
                "on_new": {"type": "select", "label": "/new 时", "default": "drop",
                           "options": ["drop", "drain"],
                           "option_labels": {"drop": "跳到最新位置", "drain": "先跑完积压批次"},
                           "description": "drain 在档案起点更新前先把每个落后 WF 的积压批次跑完"},
            }),
        ),
        FlowNodeType(
            type="tool", version=1, label="交接工具", category="触发", role="trigger", emits="call",
            description="调用方 WF 运行时获得这个工具, 由调用方模型决定是否调用; 调用时触发下游 WF, 工具参数作为输入",
            inputs=(
                PortSpec(name="caller", types=("handoff",), required=True, multiple=True, side="left", label="调用方"),
                PortSpec(name="when", types=("condition",), multiple=True, side="left", label="条件"),
            ),
            outputs=_ports([("event", "event", "触发")]),
            config_schema=_fields({
                "tool_name": {"type": "string", "label": "工具名", "default": "",
                              "description": "留空时为 handoff_<节点 id>"},
                "tool_description": {"type": "textarea", "label": "工具说明", "default": "",
                                     "description": "调用方模型看到的说明, 写清什么时候应该交接"},
                "max_calls": {"type": "number", "label": "每次运行最多调用", "default": 1,
                              "minimum": 1, "maximum": 10, "integer": True},
            }),
        ),
        # ---------- 条件 ----------
        FlowNodeType(
            type="cond_message", version=1, label="消息条件", category="条件", role="condition", needs="message",
            description="检查触发时的那条消息; 没有单条消息的触发 (阈值, 交接) 里恒为假",
            outputs=_ports([("out", "condition", "真/假")]),
            config_schema=_fields({
                "kind": {"type": "select", "label": "判断", "default": "mention",
                         "options": ["mention", "keyword", "sender"],
                         "option_labels": {"mention": "被 @", "keyword": "包含关键词", "sender": "发言人在名单中"},
                         "description": "私聊消息视为已 @"},
                "keywords": {"type": "textarea", "label": "关键词", "default": "",
                             "description": "每行一个, 命中任一即为真", "visible_when": {"kind": "keyword"}},
                "senders": {"type": "textarea", "label": "成员名单", "default": "",
                            "description": "每行一个成员 ID 或昵称", "visible_when": {"kind": "sender"}},
            }),
        ),
        FlowNodeType(
            type="cond_time", version=1, label="时间段", category="条件", role="condition",
            description="触发时刻在时间段内为真",
            outputs=_ports([("out", "condition", "真/假")]),
            config_schema=_fields({
                "start": _time_field("开始", "09:00"),
                "end": _time_field("结束", "22:00"),
            }),
        ),
        FlowNodeType(
            type="cond_state", version=1, label="快照条件", category="条件", role="condition",
            description="读取状态槽当前版本判断",
            inputs=(PortSpec(name="state", types=("state",), required=True, label="状态"),),
            outputs=_ports([("out", "condition", "真/假")]),
            config_schema=_fields({
                "check": {"type": "select", "label": "判断", "default": "nonempty",
                          "options": ["nonempty", "version"],
                          "option_labels": {"nonempty": "已有内容", "version": "版本不低于"}},
                "min_version": {"type": "number", "label": "最低版本", "default": 1,
                                "minimum": 1, "integer": True, "visible_when": {"check": "version"}},
            }),
        ),
        # ---------- 逻辑 ----------
        FlowNodeType(
            type="and", version=2, label="AND", category="逻辑", role="logic",
            description="A 和 B 都为真才为真. 一个触发加一个条件: 条件为真才放行触发; 两个触发: 在时间窗内都发生才触发. 更多输入时串联多个门",
            inputs=_GATE_INPUTS,
            outputs=(
                PortSpec(name="out", types=("condition",), side="right", label="输出", derived=True),
            ),
            config_schema=_fields({
                "window_seconds": {"type": "number", "label": "触发时间窗", "default": 120,
                                   "minimum": 1, "maximum": 86400, "integer": True,
                                   "description": "仅在有多个触发输入时使用"},
            }),
        ),
        FlowNodeType(
            type="or", version=2, label="OR", category="逻辑", role="logic",
            description="A 或 B 为真即为真. 两个输入必须同为触发或同为条件: 都是触发时任一触发都放行. 更多输入时串联多个门",
            inputs=_GATE_INPUTS,
            outputs=(
                PortSpec(name="out", types=("condition",), side="right", label="输出", derived=True),
            ),
        ),
        FlowNodeType(
            type="not", version=1, label="NOT", category="逻辑", role="logic",
            description="取反, 只接受一个条件; 触发不能取反",
            inputs=(
                PortSpec(name="in", types=("condition",), required=True, side="left", label="输入"),
            ),
            outputs=_ports([("out", "condition", "输出")]),
        ),
        # ---------- 其他 ----------
        FlowNodeType(
            type="note", version=1, label="注释", category="其他", role="note",
            description="画布上的说明文字, 不参与运行和检查",
            config_schema=_fields({
                "text": {"type": "textarea", "label": "内容", "default": ""},
                "color": {"type": "select", "label": "颜色", "default": "orange",
                          "options": ["orange", "accent", "green", "pink", "neutral"],
                          "option_labels": {"orange": "黄", "accent": "蓝", "green": "绿", "pink": "粉", "neutral": "灰"}},
            }),
        ),
    ]


class FlowNodeRegistry:
    """节点类型注册表: 内置节点 + 插件节点, 按完整类型名查找"""

    def __init__(self) -> None:
        """注册全部内置节点"""
        self._types: dict[str, FlowNodeType] = {t.type: t for t in builtin_node_types()}

    def get(self, type_name: str) -> FlowNodeType | None:
        """
        按完整类型名查找节点类型

        参数:
        - type_name: 完整类型名 (内置不带点, 插件为 插件名.节点名)

        返回:
        - FlowNodeType | None: 未注册时返回 None
        """
        return self._types.get(type_name.strip())

    def register_plugin_nodes(self, plugin_name: str, nodes: list[FlowNodeType]) -> None:
        """
        登记一个插件提供的全部节点类型, 同名类型直接覆盖 (插件升级)

        参数:
        - plugin_name: 插件稳定名称
        - nodes: 该插件解析出的节点类型
        """
        for node_type in nodes:
            self._types[node_type.type] = node_type

    def unregister_plugin(self, plugin_name: str) -> None:
        """
        移除一个插件登记的全部节点类型

        参数:
        - plugin_name: 插件稳定名称
        """
        for key in [key for key, t in self._types.items() if t.plugin == plugin_name]:
            del self._types[key]

    def all_types(self) -> list[FlowNodeType]:
        """返回全部已注册节点类型, 内置在前"""
        builtin = [t for t in self._types.values() if not t.plugin]
        plugin = [t for t in self._types.values() if t.plugin]
        return builtin + plugin


BUILTIN_REGISTRY = FlowNodeRegistry()
"""宿主级节点注册表, 插件安装/卸载时同步更新"""
