"""
模型写工具的宿主授权接缝

固定会话与工具身份, 审批时复核插件存活和独立开关;
业务插件各自提供权限检查, 不相互读取配置或调用工具
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any
import weakref

from satrap.core.config.group_action_origin import ModelActionAuthorization
from satrap.core.call_context import CallOrigin, bind_call_origin
from satrap.core.plugin_authorization import plugin_tool_live


def config_ids(value: Any) -> list[str]:
    """
    宽松解析业务配置里的逐行或列表 ID, 保留顺序并逐项转成文字

    只用于引用其他对象的业务配置 (迁移, 展示, 引用清单); 决定权限边界的名单请用
    satrap.core.plugin_authorization.permission_id_list, 它拒绝无效类型且去重排序

    参数:
    - value: 原始配置, 可以是逐行文字, 字符串列表或其他会被转成文字的值

    返回:
    - 去除空值后的 ID 列表, 顺序与原始配置一致
    """
    values = value if isinstance(value, list) else str(value or "").splitlines()
    return [str(item).strip() for item in values if str(item).strip()]


def bind_tool_session(tool: Any, session: Any) -> None:
    """
    保留来源会话弱引用, 不使审批延长会话存活

    参数:
    - tool: 当前工具
    - session: 安装工具的会话
    """
    try:
        tool._model_tool_session_ref = weakref.ref(session)
    except TypeError:
        tool._model_tool_session_ref = None
    tool._model_tool_session_id = str(getattr(session, "session_id", ""))


def model_tool_authorization(tool: Any, origin: CallOrigin, plugin_name: str,
                             permission: Callable[[Any, str], None]) -> ModelActionAuthorization:
    """
    固定模型写工具身份, 等待和审批时读取当前权限

    参数:
    - tool: 当前来源工具
    - origin: 宿主冻结的平台身份
    - plugin_name: 工具所属插件
    - permission: 业务插件自己的权限检查

    返回:
    - 可用于宿主审批的可信授权来源
    """
    tool_ref = weakref.ref(tool)
    session_ref = getattr(tool, "_model_tool_session_ref", None)
    if hasattr(tool, "_model_tool_session_ref") and session_ref is None:
        raise PermissionError("模型写工具无法复核来源会话")
    identity = {"adapter_id": origin.adapter_id, "self_id": origin.self_id,
                "chat_type": origin.chat_type, "chat_id": origin.chat_id, "actor_id": origin.actor_id,
                "session_id": str(getattr(tool, "_model_tool_session_id", "")), "tool_name": str(tool.tool_name)}

    def verify(target_group: str) -> None:
        """
        审批和平台写调用前核验工具及插件仍有效

        参数:
        - target_group: 固定的目标群
        """
        live = tool_ref()
        if live is None or not live.is_enabled():
            raise PermissionError("模型写工具已停用或来源失效")
        if session_ref is not None:
            session = session_ref()
            if session is None or plugin_tool_live(session, plugin_name, str(live.tool_name), live) is not None:
                raise PermissionError("模型写工具已从来源会话移除或停用")
        with bind_call_origin(origin):
            permission(live, target_group)

    return ModelActionAuthorization(identity, verify, session_ref)
