from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from satrap.core.backend.BackendManager import BackendConfig


CONFIG_CANDIDATES = ("config.yaml", "config.yml", "config.json")
"""自动检测配置文件的候选路径"""


def find_config_path(cwd: str | Path | None = None) -> Path:
    """查找当前配置文件, 不存在时返回默认 config.yaml"""
    root = Path(cwd) if cwd is not None else Path.cwd()
    for name in CONFIG_CANDIDATES:
        path = root / name
        if path.exists():
            return path
    return root / "config.yaml"


def _load_yaml(text: str) -> dict[str, Any]:
    """解析 YAML 文本"""
    try:
        import yaml
    except ImportError as e:
        raise RuntimeError("当前环境未安装 PyYAML, 无法编辑 YAML 配置") from e

    data = yaml.safe_load(text) if text.strip() else {}
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError("配置文件根节点必须是对象")
    return data


def _dump_yaml(data: dict[str, Any]) -> str:
    """序列化 YAML 文本"""
    try:
        import yaml
    except ImportError as e:
        raise RuntimeError("当前环境未安装 PyYAML, 无法保存 YAML 配置") from e
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
        data = _load_yaml(text)
    if not isinstance(data, list):
        raise ValueError("platforms 必须是列表")
    result: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            raise ValueError("platforms 中的每一项必须是对象")
        result.append(item)
    return result


def validate_config_document(data: dict[str, Any]) -> BackendConfig:
    """校验配置文档并返回 BackendConfig"""
    return BackendConfig.from_dict(data)


def save_config_document(path: str | Path, data: dict[str, Any]) -> BackendConfig:
    """校验并保存配置文档"""
    path = Path(path)
    config = validate_config_document(data)
    path.write_text(dump_config_document(path, data), encoding="utf-8")
    return config


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
    """更新常用配置字段"""
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
