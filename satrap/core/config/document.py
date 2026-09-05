"""提供配置文档的校验, 原子保存和平台配置操作"""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, cast
import json
import os
import re

from satrap.core.backend.BackendManager import BackendConfig
from satrap.core.config.loader import ConfigLoader
from satrap.core.config._yaml import safe_yaml_dump, safe_yaml_load


MASKED_SECRET = "********"
_SECRET_FIELD_NAMES = {
    "access_token",
    "api_key",
    "api_secret",
    "api_token",
    "authorization",
    "bot_token",
    "credential",
    "misskey_token",
    "password",
    "passwd",
    "private_key",
    "secret",
    "token",
}


def is_secret_field(name: str) -> bool:
    """
    按字段名识别配置凭据, 避免把 max_tokens 等普通字段误判为密钥

    参数:
    - name: 配置字段名

    返回:
    - 字段用于保存凭据时返回 True
    """
    normalized = name.strip().lower().replace("-", "_")
    return normalized in _SECRET_FIELD_NAMES or normalized.endswith((
        "_access_token",
        "_api_key",
        "_api_secret",
        "_api_token",
        "_credential",
        "_password",
        "_private_key",
        "_secret",
        "_token",
    ))


def is_masked_secret(value: object) -> bool:
    """
    判断字符串是否为服务端返回的脱敏占位值

    参数:
    - value: 待检查配置值

    返回:
    - 值符合脱敏占位格式时返回 True
    """
    return isinstance(value, str) and re.fullmatch(r"\*{4,}[^*]*", value) is not None


def redact_config_document(data: object) -> object:
    """
    递归复制配置并对所有已知凭据字段脱敏

    参数:
    - data: 任意配置文档节点

    返回:
    - 保持原有容器形态且凭据已脱敏的副本
    """
    if isinstance(data, dict):
        output: dict[str, object] = {}
        for raw_key, value in cast(dict[object, object], data).items():
            key = str(raw_key)
            if is_secret_field(key) and value not in (None, ""):
                output[key] = MASKED_SECRET
            else:
                output[key] = redact_config_document(value)
        return output
    if isinstance(data, list):
        return [redact_config_document(item) for item in cast(list[object], data)]
    return data


def merge_masked_secrets(existing: object, submitted: object, field_name: str = "") -> object:
    """
    把提交数据中的脱敏占位值替换为已有密钥, 其他值按用户输入保存

    参数:
    - existing: 当前持久化配置节点
    - submitted: 用户提交的配置节点
    - field_name: 当前节点字段名, 根节点默认为空字符串

    返回:
    - 保留被遮罩凭据并合并其他提交值后的配置节点
    """
    if is_secret_field(field_name) and is_masked_secret(submitted):
        if existing is None or is_masked_secret(existing):
            raise ValueError(f"字段 {field_name} 缺少可保留的原始凭据")
        return existing
    if isinstance(submitted, dict):
        existing_dict = cast(dict[str, object], existing) if isinstance(existing, dict) else {}
        return {
            str(key): merge_masked_secrets(existing_dict.get(str(key)), value, str(key))
            for key, value in cast(dict[object, object], submitted).items()
        }
    if isinstance(submitted, list):
        existing_list = cast(list[object], existing) if isinstance(existing, list) else []
        existing_by_id: dict[str, object] = {}
        for item in existing_list:
            if not isinstance(item, dict):
                continue
            item_dict = cast(dict[object, object], item)
            item_id = item_dict.get("id")
            if item_id is not None:
                existing_by_id[str(item_id)] = item
        merged: list[object] = []
        for index, item in enumerate(cast(list[object], submitted)):
            previous = existing_list[index] if index < len(existing_list) else None
            if isinstance(item, dict):
                item_dict = cast(dict[object, object], item)
                item_id = item_dict.get("id")
                if item_id is not None:
                    previous = existing_by_id.get(str(item_id), previous)
            merged.append(merge_masked_secrets(previous, cast(object, item), field_name))
        return merged
    return submitted


def find_config_path(cwd: str | Path | None = None) -> Path:
    """
    查找当前生效的配置文件, 未找到时返回默认创建路径

    参数:
    - cwd: 配置查找根目录

    返回:
    - Path: 当前配置文件或默认创建路径
    """
    for path in ConfigLoader.candidate_paths(cwd):
        if path.exists():
            return path
    return ConfigLoader.default_config_path(cwd)


def config_exists(cwd: str | Path | None = None) -> bool:
    """
    检查配置查找路径中是否存在配置文件

    参数:
    - cwd: 配置查找根目录

    返回:
    - bool: 是否存在配置文件
    """
    return any(path.exists() for path in ConfigLoader.candidate_paths(cwd))


def load_config_document(path: str | Path) -> dict[str, Any]:
    """
    读取 YAML 配置文档

    参数:
    - path: 配置文件路径

    返回:
    - dict[str, Any]: 配置文档, 文件不存在时返回空字典
    """
    config_path = Path(path)
    if not config_path.exists():
        return {}
    data = safe_yaml_load(config_path.read_text(encoding="utf-8"))
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError("配置根节点必须是对象")
    return dict(cast(dict[str, Any], data))


def load_raw_config(path: str | Path) -> str:
    """
    读取原始配置文本

    参数:
    - path: 配置文件路径

    返回:
    - str: 原始配置文本, 文件不存在时返回空字符串
    """
    config_path = Path(path)
    return config_path.read_text(encoding="utf-8") if config_path.exists() else ""


def parse_raw_config(path: str | Path, text: str) -> dict[str, Any]:
    """
    按配置文件扩展名解析并校验原始配置文本

    参数:
    - path: 配置文件路径
    - text: 原始配置文本

    返回:
    - dict[str, Any]: 已校验的配置文档
    """
    if Path(path).suffix.lower() == ".json":
        data: object = json.loads(text)
    else:
        data = safe_yaml_load(text)
    return validate_config_document(data)


def parse_platforms_text(text: str) -> list[dict[str, Any]]:
    """
    解析 JSON 或 YAML 格式的平台配置列表

    参数:
    - text: 平台配置文本

    返回:
    - list[dict[str, Any]]: 已校验的平台配置列表
    """
    if not text.strip():
        return []
    try:
        data: object = json.loads(text)
    except json.JSONDecodeError:
        data = safe_yaml_load(text)
    return validate_platforms(data)


def validate_platforms(platforms: object) -> list[dict[str, Any]]:
    """
    校验并规范化平台配置列表

    参数:
    - platforms: 待校验的平台配置

    返回:
    - list[dict[str, Any]]: 规范化的平台配置列表
    """
    if platforms is None:
        return []
    if not isinstance(platforms, list):
        raise ValueError("platforms 必须是列表")

    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    raw_platforms = cast(list[object], platforms)
    for raw_item in raw_platforms:
        if not isinstance(raw_item, dict):
            raise ValueError("platforms 中的每一项必须是对象")
        item = cast(dict[str, Any], raw_item)
        platform_id = str(item.get("id", "")).strip()
        platform_type = str(item.get("type", "")).strip()
        session_provider = str(item.get("session_provider", "session_class")).strip() or "session_class"
        session_type = str(item.get("session_type", "")).strip()
        settings = item.get("settings", {})
        if not platform_id:
            raise ValueError("平台 id 不能为空")
        if not platform_type:
            raise ValueError(f"平台 {platform_id} 的 type 不能为空")
        if not isinstance(settings, dict):
            raise ValueError(f"平台 {platform_id} 的 settings 必须是对象")
        if platform_id in seen:
            raise ValueError(f"平台 id 重复: {platform_id}")
        seen.add(platform_id)
        normalized = dict(item)
        normalized["id"] = platform_id
        normalized["type"] = platform_type
        normalized["session_provider"] = session_provider
        if session_type:
            normalized["session_type"] = session_type
        else:
            normalized.pop("session_type", None)
        normalized["settings"] = dict(cast(dict[str, Any], settings))
        result.append(normalized)
    return result


def validate_config_document(data: object) -> dict[str, Any]:
    """
    校验并规范化完整配置文档

    参数:
    - data: 待校验的配置文档

    返回:
    - dict[str, Any]: 规范化的配置文档
    """
    if not isinstance(data, dict):
        raise ValueError("配置根节点必须是对象")
    normalized = dict(cast(dict[str, Any], data))
    normalized["platforms"] = validate_platforms(normalized.get("platforms", []))
    BackendConfig.from_dict(normalized)
    return normalized


def save_config_document(path: str | Path, data: object) -> dict[str, Any]:
    """
    校验并原子保存配置文档

    参数:
    - path: 配置文件路径
    - data: 待保存的配置文档

    返回:
    - dict[str, Any]: 已保存的规范化配置文档
    """
    config_path = Path(path)
    normalized = validate_config_document(data)
    if config_path.suffix.lower() == ".json":
        dumped_value = json.dumps(normalized, ensure_ascii=False, indent=2) + "\n"
    else:
        dumped_value = safe_yaml_dump(normalized)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{config_path.name}.",
        suffix=".tmp",
        dir=config_path.parent,
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as temporary_file:
            temporary_file.write(dumped_value)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, config_path)
    except Exception:
        if temporary_path.exists():
            temporary_path.unlink()
        raise
    return normalized


def create_default_config(path: str | Path, *, overwrite: bool = False) -> dict[str, Any]:
    """
    创建默认配置文件

    参数:
    - path: 配置文件路径
    - overwrite: 是否覆盖已有配置

    返回:
    - dict[str, Any]: 已创建或已存在的配置文档
    """
    config_path = Path(path)
    if config_path.exists() and not overwrite:
        return load_config_document(config_path)
    return save_config_document(config_path, ConfigLoader.default_config_document())


def upsert_platform(
    platforms: object,
    platform: object,
    *,
    original_id: str | None = None,
) -> list[dict[str, Any]]:
    """
    新增或更新平台配置

    参数:
    - platforms: 当前平台配置列表
    - platform: 待保存的平台配置
    - original_id: 更新前的平台 id

    返回:
    - list[dict[str, Any]]: 更新后的平台配置列表
    """
    current = validate_platforms(platforms)
    candidate = validate_platforms([platform])[0]
    old_id = (original_id or "").strip()
    target_index: int | None = None
    for index, item in enumerate(current):
        item_id = str(item["id"])
        if item_id == candidate["id"] and item_id != old_id:
            raise ValueError(f"平台 id 已存在: {item_id}")
        if old_id and item_id == old_id:
            target_index = index
    if old_id and target_index is None:
        raise ValueError(f"平台不存在: {old_id}")
    if target_index is None:
        current.append(candidate)
    else:
        current[target_index] = candidate
    return validate_platforms(current)


def delete_platform(platforms: object, platform_id: str) -> list[dict[str, Any]]:
    """
    删除指定平台配置

    参数:
    - platforms: 当前平台配置列表
    - platform_id: 待删除的平台 id

    返回:
    - list[dict[str, Any]]: 删除后的平台配置列表
    """
    current = validate_platforms(platforms)
    normalized_id = platform_id.strip()
    if not any(item["id"] == normalized_id for item in current):
        raise ValueError(f"平台不存在: {normalized_id}")
    return [item for item in current if item["id"] != normalized_id]


def update_common_fields(
    data: dict[str, Any],
    *,
    api_host: str,
    api_port: int,
    default_session_type: str,
    max_sessions: int,
    idle_timeout: int,
    llm_timeout: float,
    rate_limit: float,
    rate_burst: int,
    platforms: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    更新管理界面常用配置字段

    参数:
    - data: 原始配置文档
    - api_host: API 地址
    - api_port: API 端口
    - default_session_type: 默认会话类型
    - max_sessions: 最大会话数
    - idle_timeout: 会话闲置超时
    - llm_timeout: LLM 超时
    - rate_limit: 限流速率
    - rate_burst: 限流突发数
    - platforms: 平台配置列表

    返回:
    - dict[str, Any]: 更新后的配置文档
    """
    updated = dict(data)
    api = dict(cast(dict[str, Any], updated.get("api") or {}))
    api.update({"host": api_host, "port": int(api_port)})
    updated.update({
        "api": api,
        "default_session_type": default_session_type,
        "max_sessions": int(max_sessions),
        "idle_timeout": int(idle_timeout),
        "llm_timeout": float(llm_timeout),
        "rate_limit": float(rate_limit),
        "rate_burst": int(rate_burst),
        "platforms": validate_platforms(platforms),
    })
    return updated


def configured_platform_types(
    config_data: dict[str, Any],
    health: dict[str, Any] | None = None,
) -> list[str]:
    """
    汇总配置和运行态中的适配器类型

    参数:
    - config_data: 配置文档
    - health: 后端健康信息

    返回:
    - list[str]: 已排序的适配器类型
    """
    types = {item["type"] for item in validate_platforms(config_data.get("platforms", []))}
    adapters = (health or {}).get("adapters", {})
    if isinstance(adapters, dict):
        for raw_info in cast(dict[str, object], adapters).values():
            if not isinstance(raw_info, dict):
                continue
            info = cast(dict[str, Any], raw_info)
            platform_type = str(info.get("config_type") or info.get("type") or "").strip()
            if platform_type:
                types.add(platform_type)
    return sorted(types)
