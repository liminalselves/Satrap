from __future__ import annotations

import json
import yaml
from pathlib import Path
from typing import Any

from satrap.core.backend.BackendManager import BackendConfig
from satrap.core.config_loader import ConfigLoader


CONFIG_CANDIDATES = ("config.yaml", "config.yml", "config.json")
"""自动检测配置文件的候选路径"""


def find_config_path(cwd: str | Path | None = None) -> Path:
    """查找当前配置文件, 不存在时返回 satrap/config.yaml"""
    for path in ConfigLoader.candidate_paths(cwd):
        if path.exists():
            return path
    return ConfigLoader.default_config_path(cwd)


def config_exists(cwd: str | Path | None = None) -> bool:
    """判断当前工作目录是否已有配置文件"""
    return any(path.exists() for path in ConfigLoader.candidate_paths(cwd))


def create_default_config(cwd: str | Path | None = None) -> BackendConfig:
    """创建默认配置文件并返回配置对象"""
    path = ConfigLoader.ensure_default_config(cwd)
    config = ConfigLoader.from_yaml(path) if path.suffix.lower() in (".yaml", ".yml") else ConfigLoader.from_json(path)
    return ConfigLoader.merge_env(config)


def _load_yaml(text: str) -> dict[str, Any]:
    """解析 YAML 文本"""
    data = yaml.safe_load(text) if text.strip() else {}
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError("配置文件根节点必须是对象")
    return data


def _load_yaml_any(text: str) -> Any:
    """解析任意 YAML 文本"""
    data = yaml.safe_load(text) if text.strip() else None
    return data


def _dump_yaml(data: dict[str, Any]) -> str:
    """序列化 YAML 文本"""
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)


def load_config_document(path: str | Path) -> dict[str, Any]:
    """读取配置文件为 dict"""
    path = Path(path)
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        data = json.loads(text) if text.strip() else {}
        if not isinstance(data, dict):
            raise ValueError("配置文件根节点必须是对象")
        return data
    return _load_yaml(text)


def dump_config_document(path: str | Path, data: dict[str, Any]) -> str:
    """按路径格式序列化配置"""
    path = Path(path)
    if path.suffix.lower() == ".json":
        return json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    return _dump_yaml(data)


def load_raw_config(path: str | Path) -> str:
    """读取原始配置文本"""
    path = Path(path)
    if not path.exists():
        return dump_config_document(path, {})
    return path.read_text(encoding="utf-8")


def parse_raw_config(path: str | Path, raw_text: str) -> dict[str, Any]:
    """按路径格式解析原始配置文本"""
    path = Path(path)
    if path.suffix.lower() == ".json":
        data = json.loads(raw_text) if raw_text.strip() else {}
        if not isinstance(data, dict):
            raise ValueError("配置文件根节点必须是对象")
        return data
    return _load_yaml(raw_text)


def parse_platforms_text(text: str) -> list[dict[str, Any]]:
    """解析平台配置文本, 支持 JSON 或 YAML 列表"""
    if not text.strip():
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = _load_yaml_any(text)
    if data is None:
        return []
    if not isinstance(data, list):
        raise ValueError("platforms 必须是列表")
    return validate_platforms(data)


def validate_config_document(data: dict[str, Any]) -> BackendConfig:
    """校验配置文档并返回 BackendConfig"""
    validate_platforms(data.get("platforms", []))
    return BackendConfig.from_dict(data)


def save_config_document(path: str | Path, data: dict[str, Any]) -> BackendConfig:
    """校验并保存配置文档"""
    path = Path(path)
    config = validate_config_document(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump_config_document(path, data), encoding="utf-8")
    return config


def validate_platforms(platforms: Any) -> list[dict[str, Any]]:
    """校验 platforms 列表结构"""
    if platforms is None:
        return []
    if not isinstance(platforms, list):
        raise ValueError("platforms 必须是列表")
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for item in platforms:
        if not isinstance(item, dict):
            raise ValueError("platforms 中的每一项必须是对象")
        pid = str(item.get("id", "")).strip()
        ptype = str(item.get("type", "")).strip()
        settings = item.get("settings", {})
        if not pid:
            raise ValueError("平台 id 不能为空")
        if not ptype:
            raise ValueError(f"平台 {pid} 的 type 不能为空")
        if not isinstance(settings, dict):
            raise ValueError(f"平台 {pid} 的 settings 必须是对象")
        if pid in seen:
            raise ValueError(f"平台 id 重复: {pid}")
        seen.add(pid)
        copied = dict(item)
        copied["id"] = pid
        copied["type"] = ptype
        copied["settings"] = dict(settings)
        result.append(copied)
    return result


def upsert_platform(
    platforms: list[dict[str, Any]],
    *,
    original_id: str | None,
    platform_id: str,
    platform_type: str,
    settings: dict[str, Any],
) -> list[dict[str, Any]]:
    """新增或更新平台配置"""
    pid = platform_id.strip()
    ptype = platform_type.strip()
    if not pid:
        raise ValueError("平台 id 不能为空")
    if not ptype:
        raise ValueError("平台 type 不能为空")
    if not isinstance(settings, dict):
        raise ValueError("平台 settings 必须是对象")

    old_id = (original_id or "").strip()
    updated = [dict(item) for item in platforms]
    target_index: int | None = None
    for index, item in enumerate(updated):
        item_id = str(item.get("id", "")).strip()
        if item_id == pid and item_id != old_id:
            raise ValueError(f"平台 id 已存在: {pid}")
        if old_id and item_id == old_id:
            target_index = index

    entry = {"id": pid, "type": ptype, "settings": dict(settings)}
    if target_index is None:
        updated.append(entry)
    else:
        updated[target_index] = entry
    return validate_platforms(updated)


def delete_platform(platforms: list[dict[str, Any]], platform_id: str) -> list[dict[str, Any]]:
    """删除指定平台配置"""
    pid = platform_id.strip()
    return validate_platforms([dict(item) for item in platforms if str(item.get("id", "")).strip() != pid])


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
    """更新常用配置字段
    
    参数:
    - data: 原始配置数据
    - api_host: API Host
    - api_port: API Port
    - default_session_type: 默认会话类型
    - max_sessions: 最大会话数
    - idle_timeout: 空闲超时时间
    - llm_timeout: LLM 超时时间
    - rate_limit: 速率限制
    - rate_burst: 速率突发
    - platforms: 平台配置列表
    """
    updated = dict(data)
    api = dict(updated.get("api") or {})
    api["host"] = api_host
    api["port"] = int(api_port)
    updated["api"] = api
    updated["default_session_type"] = default_session_type
    updated["max_sessions"] = int(max_sessions)
    updated["idle_timeout"] = int(idle_timeout)
    updated["llm_timeout"] = float(llm_timeout)
    updated["rate_limit"] = float(rate_limit)
    updated["rate_burst"] = int(rate_burst)
    updated["platforms"] = platforms
    return updated


def configured_platform_types(config_data: dict[str, Any], health: dict | None = None) -> list[str]:
    """汇总配置和运行态中的适配器类型"""
    types: set[str] = set()
    for item in config_data.get("platforms", []) or []:
        typ = str(item.get("type", "")).strip()
        if typ:
            types.add(typ)
    for info in (health or {}).get("adapters", {}).values():
        typ = str(info.get("config_type") or info.get("type") or "").strip()
        if typ:
            types.add(typ)
    return sorted(types)
