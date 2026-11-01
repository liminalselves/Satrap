"""图片模型的插件全局, 命名 Agent, 会话和群配置引用保护"""
from __future__ import annotations

import json
from pathlib import Path

from satrap.core.config.asr_references import (
    REFERENCE_SCAN_LOCK, _plugin_global_references, _session_override_references,
)
from satrap.core.config.document import load_config_document
from satrap.core.config.plugin_references import reference_fields
from satrap.core.storage import CHAT_PLATFORM_ID, LOCAL_PLATFORM_ID, StorageLayout


class ImgReferenceScanError(RuntimeError):
    """来源读取不完整时拒绝推断为无引用"""


def list_img_config_references(
    config_name: str, *, config_path: Path | None = None, platforms: list[object] | None = None,
    layout: StorageLayout | None = None, plugin_config_dir: Path | None = None,
    plugins_dir: Path | None = None, edictum_config_path: str | Path | None = None,
) -> list[dict[str, str]]:
    """
    批量扫描图片模型的声明字段引用, 不创建或迁移数据库

    参数:
    - config_name: 图片模型名称
    - config_path: 可选主配置路径
    - platforms: 已加载的平台声明
    - layout: 平台数据库布局
    - plugin_config_dir: 插件全局配置目录
    - plugins_dir: 用户插件目录
    - edictum_config_path: 命名 Agent 配置来源

    返回:
    - 含 summary 和来源定位字段的引用列表
    """
    with REFERENCE_SCAN_LOCK:
        try:
            document = load_config_document(config_path) if config_path is not None else {}
            if platforms is None:
                platforms = document.get("platforms", [])
            if not isinstance(platforms, list):
                raise ValueError("平台声明不是数组")

            def failure(directory: Path, error: Exception | None) -> ImgReferenceScanError:
                return ImgReferenceScanError(f"插件 {directory.name} 元数据无法解析")

            fields = reference_fields("img", plugins_dir, failure=failure, strip_names=True, include_empty=False)
            references = _plugin_global_references(plugin_config_dir, fields, config_name)
            if not edictum_config_path:
                from satrap.edictum.config import EdictumConfigManager

                raw_path = document.get("edictum_config_path")
                edictum_config_path = Path(str(raw_path)) if raw_path else EdictumConfigManager._default_storage_path()
                if raw_path and not edictum_config_path.is_absolute() and config_path is not None:
                    edictum_config_path = config_path.parent / edictum_config_path
            edictum_config_path = Path(edictum_config_path)
            if fields and edictum_config_path.exists() and not edictum_config_path.is_file():
                raise ImgReferenceScanError("命名 Agent 配置来源不是文件")
            if fields and edictum_config_path.is_file():
                named = json.loads(edictum_config_path.read_text(encoding="utf-8"))
                if not isinstance(named, dict):
                    raise ValueError("命名 Agent 配置不是对象")
                for agent, entry in named.items():
                    if not isinstance(entry, dict) or not isinstance(entry.get("plugins", []), list):
                        raise ValueError("命名 Agent 插件配置结构无效")
                    for plugin in entry.get("plugins", []):
                        if isinstance(plugin, str):
                            continue
                        if not isinstance(plugin, dict) or not isinstance(plugin.get("config", {}), dict):
                            raise ValueError("命名 Agent 插件项结构无效")
                        for field in fields.get(str(plugin.get("name")), []):
                            if plugin.get("config", {}).get(field) == config_name:
                                references.append({"summary": f"Agent {agent} 插件 {plugin['name']} 的 {field} 字段",
                                                   "kind": "named_plugin", "agent": agent, "plugin": plugin["name"], "field": field})
            if layout is not None:
                ids = list(dict.fromkeys([str(item.get("id") or "") for item in platforms if isinstance(item, dict)]
                                         + [CHAT_PLATFORM_ID, LOCAL_PLATFORM_ID]))
                references.extend(_session_override_references(layout, ids, fields, config_name))
                from satrap.core.config.group_references import list_group_references

                references.extend(list_group_references("img", config_name, layout=layout, platform_ids=ids, plugins_dir=plugins_dir))
            return references
        except ImgReferenceScanError:
            raise
        except Exception as error:
            raise ImgReferenceScanError(f"图片模型引用扫描失败: {type(error).__name__}") from error
