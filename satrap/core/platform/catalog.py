"""无需启动实例的适配器声明目录"""
from __future__ import annotations

import importlib
import importlib.util
import pkgutil
import traceback
from typing import Any

from satrap.core.log import logger
from satrap.core.platform import registry


def adapter_catalog() -> list[dict[str, Any]]:
    """
    发现已安装适配器并读取静态声明, 不建立平台连接

    返回:
    - 类型, 展示名称, 对话类型及声明读取状态列表
    """
    package = importlib.import_module("satrap.core.platform")
    failures: dict[str, str] = {}
    for module in pkgutil.iter_modules(package.__path__):
        if not module.ispkg:
            continue
        name = f"{package.__name__}.{module.name}.adapter"
        try:
            if importlib.util.find_spec(name) is not None:
                importlib.import_module(name)
        except Exception as error:
            logger.error(f"[适配器目录] {module.name} 声明加载失败: {error}\n{traceback.format_exc()}")
            failures[module.name] = "适配器模块无法加载"
    result: list[dict[str, Any]] = []
    for adapter_type in registry.list_types():
        adapter = registry.get(adapter_type)
        if adapter is None:
            continue
        try:
            kinds = adapter.conversation_kinds
            if not isinstance(kinds, dict) or not kinds or len(kinds) > 32:
                raise ValueError("对话类型声明必须是非空对象, 最多 32 项")
            for kind, label in kinds.items():
                if (not isinstance(kind, str) or not kind or len(kind) > 64 or not kind.isascii()
                        or not all(char.isalnum() or char in "_-" for char in kind)
                        or not isinstance(label, str) or not label.strip()):
                    raise ValueError("对话类型标识或展示名称无效")
            result.append({"type": adapter_type, "display_name": adapter.display_name or adapter_type,
                           "conversation_kinds": dict(kinds), "status": "available"})
        except Exception as error:
            logger.error(f"[适配器目录] {adapter_type} 对话类型声明无效: {error}\n{traceback.format_exc()}")
            failures[adapter_type] = "对话类型声明无效"
    valid = {item["type"] for item in result}
    result.extend({"type": name, "display_name": name, "conversation_kinds": {}, "status": "unavailable", "error": reason}
                  for name, reason in failures.items() if name not in valid)
    return sorted(result, key=lambda item: item["type"])
