"""
会话类配置管理器

维护可用会话类的配置与启用状态, 扫描指定目录发现会话实现,
并通过类路径完成动态加载和运行时注册
"""
from __future__ import annotations

import importlib.util
import importlib
import threading
import inspect
from pathlib import Path
from typing import Any, Dict, Optional, Type, cast
import json
import sys
import os

from satrap.core.framework.session_discovery import (
    build_session_module_catalog,
    ensure_session_scan_paths,
)
from satrap.core.framework.Base import AsyncSession, Session
from satrap.core.utils.paths import get_data_dir

from satrap.core.log import logger


class SessionClassConfigManager:
    """
    会话类配置管理器

    管理 Session/AsyncSession 子类的类级配置(模型, 工具, 命令等),
    与 SessionConfigStore(实例级上下文)互补

    支持:
    - 注册 Session 子类, 自动发现 __init__ 自定义参数
    - 配置持久化到 JSON
    - 运行时按名称获取 class 对象
    """

    DEFAULT_NAME = "default"

    def __init__(
        self,
        storage_path: str | Path | None = None,
        auto_create: bool = True,
        session_scan_paths: list[str] | None = None
    ):
        """
        初始化 SessionClassConfigManager

        参数:
        - storage_path: 存储路径
        - auto_create: auto创建
        - session_scan_paths: 会话scanpaths
        """
        self._lock = threading.RLock()
        self.storage_path = Path(storage_path) if storage_path else self._default_storage_path()
        self.session_scan_paths = list(session_scan_paths or [".satrap/session"])
        ensure_session_scan_paths(self.session_scan_paths)
        self._builtin_package_root = Path(__file__).resolve().parents[2]

        self._configs: Dict[str, Dict[str, Any]] = {}
        self._class_cache: Dict[str, Type[Session] | Type[AsyncSession]] = {}

        if auto_create:
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self.reload()

    @staticmethod
    def _default_storage_path() -> Path:
        """
        获取默认会话类配置存储路径

        返回:
        - Path: 默认会话类配置存储路径
        """
        env_path = os.getenv("SATRAP_SESSION_CLASS_CONFIG_PATH")
        if env_path:
            return Path(env_path)
        return get_data_dir() / "session_class_config.json"

    @staticmethod
    def _normalize_name(name: str | None) -> str:
        """
        归一化配置名称

        参数:
        - name: 名称

        返回:
        - str: 归一化配置名称
        """
        return (name or SessionClassConfigManager.DEFAULT_NAME).strip() \
            or SessionClassConfigManager.DEFAULT_NAME

    # ---------- 参数反射 ----------
    @staticmethod
    def _detect_params(
        session_class: Type[Session] | Type[AsyncSession],
    ) -> Dict[str, inspect.Parameter]:
        """
        反射 __init__ 签名, 提取自定义参数(排除已知自动注入参数)

        参数:
        - session_class: 会话类

        返回:
        - Dict[str, inspect.Parameter]: 反射 __init__ 签名, 提取自定义参数(排除已知自动注入参数)
        """
        exclude = {"self", "session_id", "content_callback", "command_handler", "session_config", "llm"}
        try:
            sig = inspect.signature(session_class.__init__)
        except Exception:
            return {}
        params: Dict[str, inspect.Parameter] = {}
        for name, param in sig.parameters.items():
            if name in exclude:
                continue
            if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                continue
            params[name] = param
        return params

    @staticmethod
    def _generate_template(params_info: Dict[str, inspect.Parameter]) -> Dict[str, Any]:
        """
        根据类型注解生成占位值模板

        参数:
        - params_info: 参数集合info

        返回:
        - Dict[str, Any]: 根据类型注解生成占位值模板
        """
        type_map: Dict[str, Any] = {
            "str": "",
            "int": 0,
            "float": 0.0,
            "bool": False,
            "list": [],
            "dict": {},
        }
        template: Dict[str, Any] = {}
        for name, param in params_info.items():
            ann = param.annotation
            if ann is inspect.Parameter.empty:
                template[name] = None
                continue
            ann_str = ann if isinstance(ann, str) else getattr(ann, "__name__", str(ann))   # 类型注解反射, 保留裸 getattr
            template[name] = type_map.get(ann_str, None)
        return template

    # ---------- class 导入 ----------
    def _trusted_module_source(self, class_path: str) -> tuple[str, str, Path]:
        """
        校验类路径是否属于内置包或已配置的会话扫描目录

        参数:
        - class_path: 类路径

        返回:
        - tuple[str, str, Path]: 模块名, 类名和可信源码路径
        """
        try:
            module_path, class_name = class_path.rsplit(".", 1)
        except ValueError as e:
            raise ValueError("class_path 必须是完整类路径") from e
        if not module_path or not class_name:
            raise ValueError("class_path 必须是完整类路径")

        catalog = build_session_module_catalog(self.session_scan_paths)
        expected_source = catalog.get(module_path)
        is_builtin = expected_source is None and module_path.startswith("satrap.")
        if is_builtin:
            try:
                spec = importlib.util.find_spec(module_path)
            except (ImportError, AttributeError, ValueError) as e:
                raise ValueError(f"无法解析内置会话模块: {module_path}") from e
            origin = spec.origin if spec is not None else None
            if not origin:
                raise ValueError(f"无法解析内置会话模块: {module_path}")
            expected_source = Path(origin).resolve()
            if not expected_source.is_relative_to(self._builtin_package_root):
                raise ValueError(f"会话类模块不在可信代码根中: {module_path}")
        if expected_source is None:
            raise ValueError(f"会话类模块不在可信代码根中: {module_path}")

        loaded_module = sys.modules.get(module_path)
        loaded_source = getattr(loaded_module, "__file__", None)
        if loaded_module is not None and (
            not loaded_source or Path(loaded_source).resolve() != expected_source
        ):
            raise ValueError(f"已加载的会话类模块来源与可信代码根不一致: {module_path}")

        if not is_builtin:
            parent_name, _, _ = module_path.rpartition(".")
            loaded_parent = sys.modules.get(parent_name) if parent_name else None
            parent_paths = getattr(loaded_parent, "__path__", None)
            if loaded_parent is not None and (
                parent_paths is None
                or not any(Path(path).resolve() == expected_source.parent for path in parent_paths)
            ):
                raise ValueError(f"已加载的会话模块包来源与可信代码根不一致: {parent_name}")
        return module_path, class_name, expected_source

    def _load_class(self, class_path: str) -> Type[Session] | Type[AsyncSession]:
        """
        动态导入 class

        参数:
        - class_path: 类路径

        返回:
        - Type[Session] | Type[AsyncSession]: 动态导入 class
        """
        module_path, class_name, expected_source = self._trusted_module_source(class_path)
        try:
            module = importlib.import_module(module_path)
            cls = getattr(module, class_name)   # 动态类加载, 类名运行时决定, 保留裸 getattr
            if not inspect.isclass(cls) or not issubclass(cls, (Session, AsyncSession)):
                raise ValueError(f"{class_path} 不是 Session/AsyncSession 子类")
            if cls.__module__ != module_path:
                raise ValueError(f"{class_path} 不是模块内声明的会话类")
            source = inspect.getsourcefile(cls)
            if source is None or Path(source).resolve() != expected_source:
                raise ValueError(f"{class_path} 的源码不在可信代码根中")
            return cls
        except (ImportError, AttributeError, ValueError) as e:
            raise ValueError(f"导入 class 失败: {class_path}, 错误: {e}") from e

    # ---------- 序列化 ----------
    def _to_payload_locked(self) -> Dict[str, Dict[str, Any]]:
        """
        序列化配置为 JSON 安全结构

        返回:
        - Dict[str, Dict[str, Any]]: 序列化配置为 JSON 安全结构
        """
        output: Dict[str, Dict[str, Any]] = {}
        for name, entry in self._configs.items():
            output[name] = {
                "class_path": entry["class_path"],
                "is_async": entry["is_async"],
                "enabled": entry.get("enabled", True),
                "context_key": entry.get("context_key", ""),
                "model_key": entry.get("model_key", ""),
                "description": entry.get("description", ""),
                "params": dict(entry.get("params", {})),
            }
        return output

    def _save_locked(self):
        """保存配置到 JSON 文件"""
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        with self.storage_path.open("w", encoding="utf-8") as f:
            json.dump(self._to_payload_locked(), f, ensure_ascii=False, indent=2)

    # ---------- 持久化 ----------
    def reload(self):
        """从文件重载配置"""
        with self._lock:
            self._class_cache.clear()
            if not self.storage_path.exists():
                self._configs = {}
                self._save_locked()
                return
            try:
                with self.storage_path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    logger.warning("[SessionClassConfigManager] 配置文件格式错误, 已忽略")
                    self._configs = {}
                    return
                data = cast(dict[str, Any], data)
                self._configs = {}
                for _name, entry in data.items():
                    if not isinstance(entry, dict):
                        continue
                    entry = cast(dict[str, Any], entry)
                    self._configs[str(_name)] = {
                        "class_path": str(entry.get("class_path", "")),
                        "is_async": bool(entry.get("is_async", False)),
                        "enabled": bool(entry.get("enabled", True)),
                        "context_key": str(entry.get("context_key", "")),
                        "model_key": str(entry.get("model_key", "")),
                        "description": str(entry.get("description", "")),
                        "params": dict(entry.get("params", {})),
                    }
            except Exception as e:
                logger.error(f"[SessionClassConfigManager] 读取配置失败: {e}")

    def save(self):
        """保存配置"""
        with self._lock:
            self._save_locked()

    # ---------- 注册 ----------
    def register(
        self,
        name: str,
        session_class: Type[Session] | Type[AsyncSession],
        description: str = "",
        context_key: str = "",
        model_key: str = "",
    ):
        """
        注册会话类, 自动发现 __init__ 自定义参数并生成占位模板

        参数:
        - name: 会话类配置名称
        - session_class: Session/AsyncSession 子类
        - description: 可选描述
        - context_key: params 中的哪个字段作为上下文区分键
        - model_key: params 中的哪个字段引用 ModelConfigManager 中的 LLM 名称
        """
        with self._lock:
            key = self._normalize_name(name)
            class_path = f"{session_class.__module__}.{session_class.__qualname__}"
            is_async = issubclass(session_class, AsyncSession)

            params_info = self._detect_params(session_class)
            template = self._generate_template(params_info)

            self._class_cache[key] = session_class
            self._configs[key] = {
                "class_path": class_path,
                "is_async": is_async,
                "enabled": True,
                "context_key": context_key,
                "model_key": model_key,
                "description": description,
                "params": template,
            }
            self._save_locked()
            logger.info(f"[SessionClassConfigManager] 已注册会话类: {key} -> {class_path}")

    def register_by_class_path(
        self,
        name: str,
        class_path: str,
        description: str = "",
        context_key: str = "",
        model_key: str = "",
    ):
        """
        通过 class_path 注册会话类

        参数:
        - name: 名称
        - class_path: 类路径
        - description: 说明文本
        - context_key: 上下文密钥
        - model_key: 模型密钥
        """
        ensure_session_scan_paths(self.session_scan_paths)
        session_class = self._load_class(class_path)
        self.register(
            name,
            session_class,
            description=description,
            context_key=context_key,
            model_key=model_key,
        )

    def register_config_entry(
        self,
        name: str,
        class_path: str,
        *,
        is_async: bool = False,
        enabled: bool = True,
        context_key: str = "",
        model_key: str = "",
        description: str = "",
        params: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """
        注册会话类配置但不导入或实例化对应类

        参数:
        - name: 会话类配置名称
        - class_path: 类路径
        - is_async: 是否为异步会话类
        - enabled: 是否启用
        - context_key: 上下文键
        - model_key: 模型键
        - description: 描述
        - params: 参数配置

        返回:
        - Dict[str, Any]: 已保存的完整配置
        """
        with self._lock:
            key = self._normalize_name(name)
            if key in self._configs:
                raise ValueError(f"会话类配置名称已存在: {key}")
            self._trusted_module_source(class_path)
            self._configs[key] = {
                "class_path": class_path,
                "is_async": is_async,
                "enabled": enabled,
                "context_key": context_key,
                "model_key": model_key,
                "description": description,
                "params": dict(params or {}),
            }
            self._save_locked()
            entry = self._configs[key]
            return {
                "class_path": entry["class_path"],
                "is_async": entry["is_async"],
                "enabled": entry["enabled"],
                "context_key": entry["context_key"],
                "model_key": entry["model_key"],
                "description": entry["description"],
                "params": dict(entry["params"]),
            }

    # ---------- 查询 ----------
    def get_config(self, name: str) -> Optional[Dict[str, Any]]:
        """
        获取完整配置条目

        参数:
        - name: 名称

        返回:
        - Optional[Dict[str, Any]]: 完整配置条目
        """
        with self._lock:
            key = self._normalize_name(name)
            entry = self._configs.get(key)
            if entry is None:
                return None
            return {
                "class_path": entry["class_path"],
                "is_async": entry["is_async"],
                "enabled": entry.get("enabled", True),
                "context_key": entry.get("context_key", ""),
                "model_key": entry.get("model_key", ""),
                "description": entry.get("description", ""),
                "params": dict(entry.get("params", {})),
            }

    def get_params(self, name: str) -> Dict[str, Any]:
        """
        获取参数字典(即 SessionConfig.session_config 待填充内容)

        参数:
        - name: 名称

        返回:
        - Dict[str, Any]: 参数字典(即 SessionConfig.session_config 待填充内容)
        """
        with self._lock:
            key = self._normalize_name(name)
            entry = self._configs.get(key)
            if entry is None:
                return {}
            return dict(entry.get("params", {}))

    def get_class(self, name: str) -> Type[Session] | Type[AsyncSession]:
        """
        获取注册的 class 对象(惰性导入并缓存)

        参数:
        - name: 名称

        返回:
        - Type[Session] | Type[AsyncSession]: 注册的 class 对象(惰性导入并缓存)
        """
        with self._lock:
            key = self._normalize_name(name)
            if key in self._class_cache:
                return self._class_cache[key]
            entry = self._configs.get(key)
            if entry is None:
                raise ValueError(f"未知的会话类配置: {key}")
            cls = self._load_class(entry["class_path"])
            self._class_cache[key] = cls
            return cls

    def list_configs(self) -> Dict[str, Dict[str, Any]]:
        """
        列出所有已注册配置

        返回:
        - Dict[str, Dict[str, Any]]: 列出所有已注册配置
        """
        with self._lock:
            return self._to_payload_locked()

    def has_config(self, name: str) -> bool:
        """
        检查配置是否存在

        参数:
        - name: 名称

        返回:
        - bool: 检查结果
        """
        with self._lock:
            key = self._normalize_name(name)
            return key in self._configs

    # ---------- 启用/停用 ----------
    def enable(self, name: str):
        """
        启用会话类配置

        参数:
        - name: 名称
        """
        with self._lock:
            key = self._normalize_name(name)
            if key not in self._configs:
                raise ValueError(f"未知的会话类配置: {key}")
            self._configs[key]["enabled"] = True
            self._save_locked()

    def disable(self, name: str):
        """
        停用会话类配置

        参数:
        - name: 名称
        """
        with self._lock:
            key = self._normalize_name(name)
            if key not in self._configs:
                raise ValueError(f"未知的会话类配置: {key}")
            self._configs[key]["enabled"] = False
            self._save_locked()

    def is_enabled(self, name: str) -> bool:
        """
        检查会话类配置是否启用(不存在时返回 False)

        参数:
        - name: 名称

        返回:
        - bool: 检查结果
        """
        with self._lock:
            key = self._normalize_name(name)
            entry = self._configs.get(key)
            if entry is None:
                return False
            return bool(entry.get("enabled", True))

    def get_context_key(self, name: str) -> str:
        """
        获取上下文区分键字段名

        参数:
        - name: 名称

        返回:
        - str: 上下文区分键字段名
        """
        with self._lock:
            key = self._normalize_name(name)
            entry = self._configs.get(key)
            if entry is None:
                return ""
            return str(entry.get("context_key", ""))

    def set_context_key(self, name: str, context_key: str):
        """
        设置上下文区分键字段名

        参数:
        - name: 名称
        - context_key: 上下文密钥
        """
        with self._lock:
            key = self._normalize_name(name)
            if key not in self._configs:
                raise ValueError(f"未知的会话类配置: {key}")
            self._configs[key]["context_key"] = context_key
            self._save_locked()

    def get_model_key(self, name: str) -> str:
        """
        获取模型引用键字段名

        参数:
        - name: 名称

        返回:
        - str: 模型引用键字段名
        """
        with self._lock:
            key = self._normalize_name(name)
            entry = self._configs.get(key)
            if entry is None:
                return ""
            return str(entry.get("model_key", ""))

    def set_model_key(self, name: str, model_key: str):
        """
        设置模型引用键字段名

        参数:
        - name: 名称
        - model_key: 模型密钥
        """
        with self._lock:
            key = self._normalize_name(name)
            if key not in self._configs:
                raise ValueError(f"未知的会话类配置: {key}")
            self._configs[key]["model_key"] = model_key
            self._save_locked()

    def get_class_template(self, name: str) -> Dict[str, Any]:
        """
        获取自动发现的参数模板(占位值)

        参数:
        - name: 名称

        返回:
        - Dict[str, Any]: 自动发现的参数模板(占位值)
        """
        with self._lock:
            key = self._normalize_name(name)
            if key not in self._configs:
                return {}
            cls = self.get_class(key)
            params_info = self._detect_params(cls)
            return self._generate_template(params_info)

    # ---------- 写操作 ----------
    def set_config(self, name: str, params: Dict[str, Any]):
        """
        替换 params 并落盘

        参数:
        - name: 名称
        - params: 参数集合
        """
        with self._lock:
            key = self._normalize_name(name)
            if key not in self._configs:
                raise ValueError(f"未知的会话类配置: {key}")
            self._configs[key]["params"] = dict(params)
            self._save_locked()

    def update_entry(
        self,
        name: str,
        *,
        new_name: str | None = None,
        class_path: str | None = None,
        is_async: bool | None = None,
        enabled: bool | None = None,
        params: Dict[str, Any] | None = None,
        description: str | None = None,
        context_key: str | None = None,
        model_key: str | None = None,
    ) -> Dict[str, Any]:
        """
        原子更新会话类配置中的可编辑字段

        参数:
        - name: 会话类配置名称
        - new_name: 新配置名称
        - class_path: 类路径
        - is_async: 是否为异步会话类
        - enabled: 是否启用
        - params: 完整参数配置
        - description: 描述
        - context_key: 上下文键
        - model_key: 模型键

        返回:
        - Dict[str, Any]: 更新后的完整配置
        """
        with self._lock:
            key = self._normalize_name(name)
            if key not in self._configs:
                raise ValueError(f"未知的会话类配置: {key}")
            target_key = self._normalize_name(new_name) if new_name is not None else key
            if target_key != key and target_key in self._configs:
                raise ValueError(f"会话类配置名称已存在: {target_key}")

            entry = dict(self._configs[key])
            entry["params"] = dict(entry.get("params", {}))
            original_class_path = str(entry.get("class_path", ""))
            if class_path is not None:
                self._trusted_module_source(class_path)
                entry["class_path"] = class_path
            if is_async is not None:
                entry["is_async"] = is_async
            if enabled is not None:
                entry["enabled"] = enabled
            if params is not None:
                entry["params"] = dict(params)
            if description is not None:
                entry["description"] = description
            if context_key is not None:
                entry["context_key"] = context_key
            if model_key is not None:
                entry["model_key"] = model_key

            cached_class = self._class_cache.pop(key, None)
            if target_key != key:
                self._configs.pop(key)
            self._configs[target_key] = entry
            if cached_class is not None and str(entry.get("class_path", "")) == original_class_path:
                self._class_cache[target_key] = cached_class
            self._save_locked()
            updated = self.get_config(target_key)
            if updated is None:
                raise ValueError(f"未知的会话类配置: {target_key}")
            return updated

    def update_config(self, name: str, **kwargs: Any):
        """
        部分更新 params

        参数:
        - name: 名称
        - kwargs: 额外关键字参数
        """
        with self._lock:
            key = self._normalize_name(name)
            if key not in self._configs:
                raise ValueError(f"未知的会话类配置: {key}")
            self._configs[key]["params"].update(kwargs)
            self._save_locked()

    def remove_config(self, name: str) -> bool:
        """
        移除配置及 class 缓存

        参数:
        - name: 名称

        返回:
        - bool: 移除配置及 class 缓存
        """
        with self._lock:
            key = self._normalize_name(name)
            if key not in self._configs:
                return False
            self._configs.pop(key, None)
            self._class_cache.pop(key, None)
            self._save_locked()
            return True

    def reset(self, target: str = "all"):
        """
        重置所有配置

        参数:
        - target: 目标
        """
        with self._lock:
            self._configs = {}
            self._class_cache = {}
            self._save_locked()
