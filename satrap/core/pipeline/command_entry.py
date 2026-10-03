"""
平台命令入口

从事件顶层组件识别仅用于寻址的 self 提及并在正文之前剥离,
冻结纯命令正文供调度器在唤醒窗口与输入投影之前确定命令候选;
同时承载高权限命令的平台级 operator 判定接缝
"""
from __future__ import annotations

from collections.abc import Sequence

from satrap.core.framework.command.base import DEFAULT_COMMAND_PARAM_SPLIT, DEFAULT_COMMAND_PREFIX
from satrap.core.platform.event import MessageEvent
from satrap.core.components import At, AtAll, BaseMessageComponent, Plain, Reply, is_self_mention


OPERATOR_ONLY_COMMANDS: frozenset[str] = frozenset({"approve", "plan"})
"""需要已授权操作员才能执行的命令名

`/approve` 与 `/plan` 改的是同一个 PermissionEngine 的安全状态 (`/plan off` 会放宽写操作限制),
因此同级保护; `/goal` 会启动 yolo 自动推进但按裁定不纳入本轮名集

这是过渡手段而不是长期权限模型: 后续迁移到命令注册 metadata 的权限级别 (需要能表达级别而不是单一布尔位,
因为 `/goal` 要独占一级)。迁移的结构性障碍是平台层在路由之前拿不到会话的命令注册表,
所以要么把判定挪进会话层并让平台把身份判定结果带过去, 要么让平台能预读注册信息
"""

OPERATOR_REQUIRED_FEEDBACK = "该命令仅允许已授权操作员执行。"
"""受保护命令被非操作员触发时的固定拒绝文案, 不提供配置项"""


def is_operator_only_command(name: str) -> bool:
    """
    判断命令名是否属于 operator 保护范围

    参数:
    - name: 会话层解析出的命令名, 大小写敏感

    返回:
    - bool: 命中受保护名集时为 True
    """
    return name in OPERATOR_ONLY_COMMANDS


def command_name_from_text(text: str) -> str:
    """
    从冻结正文中取出命令名

    参数:
    - text: 平台入口冻结的正文

    返回:
    - str: 前缀之后的首个非空片段; 正文没有前缀或没有命令名时返回空串

    与会话层 `_parse` 同一口径 (默认前缀与默认参数分隔符), 平台入口只做名集匹配,
    命令是否注册与启用仍由会话层判定
    """
    stripped = text.strip()
    if not stripped.startswith(DEFAULT_COMMAND_PREFIX):
        return ""
    rest = stripped[len(DEFAULT_COMMAND_PREFIX):]
    parts = [part for part in rest.split(DEFAULT_COMMAND_PARAM_SPLIT) if part]
    return parts[0] if parts else ""


def extract_command_candidate(event: MessageEvent) -> str | None:
    """
    提取命令候选正文

    参数:
    - event: 待判定事件, 只读顶层组件与冻结来源身份

    返回:
    - str | None: 剥离寻址段后的纯命令正文 (已 strip); 非命令候选时返回 None
    """
    components = event.get_messages()
    self_id = event.call_origin.self_id
    index = 0
    addressed = False
    # Step.1 跳过寻址段: self 提及, 全空白文本与位于最前的引用标记都不属于命令正文
    while index < len(components):
        component = components[index]
        if isinstance(component, Plain) and not component.text.strip():
            index += 1
            continue
        if is_self_mention(component, self_id):
            addressed = True
            index += 1
            continue
        if index == 0 and isinstance(component, Reply):
            index += 1
            continue
        break
    # Step.2 群聊要求显式 @bot; 私聊不要求, 但寻址段里的 self 提及同样被剥离
    if not event.is_private_chat() and not addressed:
        return None
    # Step.3 正文由组件渲染, 不复制 message_str, 避免把平台渲染差异带进命令名与参数
    body = _render_command_body(components[index:])
    if body is None:
        return None
    # Step.4 前缀判定使用默认前缀常量, 会话自定义前缀的构造不在平台层跟随
    stripped = body.strip()
    if not stripped.startswith(DEFAULT_COMMAND_PREFIX):
        return None
    return stripped


def _render_command_body(components: Sequence[BaseMessageComponent]) -> str | None:
    """
    渲染正文起点之后的组件文本

    参数:
    - components: 正文起点之后的组件序列

    返回:
    - str | None: 拼接文本; 出现非文本组件时返回 None, 该消息按普通消息处理
    """
    parts: list[str] = []
    for component in components:
        # AtAll 是 At 的子类, 必须先判定, 否则会被渲染成普通提及
        if isinstance(component, AtAll):
            parts.append("@全体成员")
            continue
        if isinstance(component, At):
            parts.append(f"@{component.name or component.qq}")
            continue
        if isinstance(component, Plain):
            parts.append(component.text)
            continue
        return None
    return "".join(parts)
