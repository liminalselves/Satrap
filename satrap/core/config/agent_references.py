"""平台默认, 对话类型和单群 Agent 绑定的统一引用保护"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any
import traceback

from satrap.core.config.agent_routing import validate_session_bindings
from satrap.core.config.group_references import list_group_references
from satrap.core.log import logger
from satrap.core.storage import StorageLayout


def list_agent_references(
    target: str, name: str, *, platforms: list[dict[str, Any]], layout: StorageLayout,
    default_session_type: str = "", session_classes: Mapping[str, object] | None = None,
) -> list[dict[str, str]]:
    """
    合并平台默认, 对话类型与单群引用, 扫描失败时禁止推断为无引用

    参数:
    - target: session_class 或 edictum
    - name: 配置名称
    - platforms: 冷配置中声明的平台, 包括停用实例
    - layout: 平台存储布局
    - default_session_type: 未显式绑定时的框架默认配置
    - session_classes: 已声明的会话类配置, 用于解析同名回退

    返回:
    - 可供错误响应展示的结构化引用列表
    """
    if target not in {"session_class", "edictum"}:
        raise ValueError("不支持的 Agent 引用目标")
    try:
        references: list[dict[str, str]] = []
        ids: list[str] = []
        for platform in platforms:
            if not isinstance(platform, dict) or not platform.get("id"):
                raise ValueError("平台配置缺少身份")
            platform_id = str(platform["id"])
            if platform_id not in ids:
                ids.append(platform_id)
            provider = platform.get("session_provider") or "session_class"
            config_name = str(platform.get("session_type") or "").strip()
            if not config_name and provider == "session_class":
                platform_type = str(platform.get("type") or "")
                config_name = platform_type if session_classes is not None and platform_type in session_classes else default_session_type
            elif not config_name and provider == "edictum":
                config_name = default_session_type
            if provider == target and config_name == name:
                references.append({"summary": f"平台 {platform_id} 的默认 Agent", "kind": "platform_binding",
                                   "platform_id": platform_id})
            for kind, binding in validate_session_bindings(platform.get("session_bindings")).items():
                if binding.get("mode") == "value" and binding["provider"] == target and binding["config_name"] == name:
                    references.append({"summary": f"平台 {platform_id} 的 {kind} Agent", "kind": "conversation_kind_binding",
                                       "platform_id": platform_id, "conversation_kind": kind})
        references.extend(list_group_references(target, name, layout=layout, platform_ids=ids))
        return list({tuple(sorted(item.items())): item for item in references}.values())
    except Exception as error:
        logger.error(f"[Agent 引用] {target}/{name} 扫描失败: {error}\n{traceback.format_exc()}")
        raise
