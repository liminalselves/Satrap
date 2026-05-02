from __future__ import annotations

import importlib
import inspect
import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional, Type

from satrap.core.framework.Base import AsyncSession, Session
from satrap.core.log import logger


class SessionClassConfigManager:
    """会话类配置管理器

    管理 Session/AsyncSession 子类的类级配置(模型, 工具, 命令等),
    与 SessionConfigStore(实例级上下文)互补

    支持:
    - 注册 Session 子类, 自动发现 __init__ 自定义参数
    - 配置持久化到 JSON
    - 运行时按名称获取 class 对象
    """

    DEFAULT_NAME = "default"

    def __init__(self, storage_path: str | Path | None = None, auto_create: bool = True):
        self._lock = threading.RLock()
        self.storage_path = Path(storage_path) if storage_path else self._default_storage_path()

        self._configs: Dict[str, Dict[str, Any]] = {}
        self._class_cache: Dict[str, Type[Session] | Type[AsyncSession]] = {}

        if auto_create:
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self.reload()

    @staticmethod
    def _default_storage_path() -> Path:
        """获取默认会话类配置存储路径"""
        env_path = os.getenv("SATRAP_SESSION_CLASS_CONFIG_PATH")
        if env_path:
            return Path(env_path)
        return Path.cwd() / ".satrap" / "session_class_config.json"

    @staticmethod
    def _normalize_name(name: str | None) -> str:
        """归一化配置名称"""
        return (name or SessionClassConfigManager.DEFAULT_NAME).strip() \
            or SessionClassConfigManager.DEFAULT_NAME

    # -------- 参数反射 --------
    @staticmethod
    def _detect_params(
        session_class: Type[Session] | Type[AsyncSession],
    ) -> Dict[str, inspect.Parameter]:
        """反射 __init__ 签名, 提取自定义参数(排除已知自动注入参数)"""
        exclude = {"self", "session_id", "content_callback", "command_handler", "session_config"}
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
        """根据类型注解生成占位值模板"""
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
            ann_str = ann if isinstance(ann, str) else getattr(ann, "__name__", str(ann))
            template[name] = type_map.get(ann_str, None)
        return template

    # -------- class 导入 --------
    @staticmethod
    def _load_class(class_path: str) -> Type[Session] | Type[AsyncSession]:
        """动态导入 class"""
        try:
            module_path, class_name = class_path.rsplit(".", 1)
            module = importlib.import_module(module_path)
            cls = getattr(module, class_name)
            if not inspect.isclass(cls) or not issubclass(cls, (Session, AsyncSession)):
                raise ValueError(f"{class_path} 不是 Session/AsyncSession 子类")
            return cls
        except (ImportError, AttributeError, ValueError) as e:
            raise ValueError(f"导入 class 失败: {class_path}, 错误: {e}") from e

    # -------- 序列化 --------
    def _to_payload_locked(self) -> Dict[str, Dict[str, Any]]:
        """序列化配置为 JSON 安全结构"""
        output: Dict[str, Dict[str, Any]] = {}
        for name, entry in self._configs.items():
            output[name] = {
                "class_path": entry["class_path"],
                "is_async": entry["is_async"],
                "description": entry.get("description", ""),
                "params": dict(entry.get("params", {})),
            }
        return output

    def _save_locked(self):
        """保存配置到 JSON 文件"""
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        with self.storage_path.open("w", encoding="utf-8") as f:
            json.dump(self._to_payload_locked(), f, ensure_ascii=False, indent=2)

    # -------- 持久化 --------
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
                self._configs = {}
                for name, entry in data.items():
                    if not isinstance(entry, dict):
                        continue
                    self._configs[str(name)] = {
                        "class_path": str(entry.get("class_path", "")),
                        "is_async": bool(entry.get("is_async", False)),
                        "description": str(entry.get("description", "")),
                        "params": dict(entry.get("params", {})),
                    }
            except Exception as e:
                logger.error(f"[SessionClassConfigManager] 读取配置失败: {e}")

    def save(self):
        """保存配置"""
        with self._lock:
            self._save_locked()

    # -------- 注册 --------
    def register(
        self,
        name: str,
        session_class: Type[Session] | Type[AsyncSession],
        description: str = "",
    ):
        """注册会话类, 自动发现 __init__ 自定义参数并生成占位模板

        参数:
        - name: 会话类配置名称
        - session_class: Session/AsyncSession 子类
        - description: 可选描述
        """
        with self._lock:
            if not inspect.isclass(session_class) or not issubclass(
                session_class, (Session, AsyncSession)
            ):
                raise TypeError("session_class 必须继承 Session 或 AsyncSession")

            key = self._normalize_name(name)
            class_path = f"{session_class.__module__}.{session_class.__qualname__}"
            is_async = issubclass(session_class, AsyncSession)

            params_info = self._detect_params(session_class)
            template = self._generate_template(params_info)

            self._class_cache[key] = session_class
            self._configs[key] = {
                "class_path": class_path,
                "is_async": is_async,
                "description": description,
                "params": template,
            }
            self._save_locked()
            logger.info(f"[SessionClassConfigManager] 已注册会话类: {key} -> {class_path}")

    # -------- 查询 --------
    def get_config(self, name: str) -> Optional[Dict[str, Any]]:
        """获取完整配置条目"""
        with self._lock:
            key = self._normalize_name(name)
            entry = self._configs.get(key)
            if entry is None:
                return None
            return {
                "class_path": entry["class_path"],
                "is_async": entry["is_async"],
                "description": entry.get("description", ""),
                "params": dict(entry.get("params", {})),
            }

    def get_params(self, name: str) -> Dict[str, Any]:
        """获取参数字典(即 SessionConfig.session_config 待填充内容)"""
        with self._lock:
            key = self._normalize_name(name)
            entry = self._configs.get(key)
            if entry is None:
                return {}
            return dict(entry.get("params", {}))

    def get_class(self, name: str) -> Type[Session] | Type[AsyncSession]:
        """获取注册的 class 对象(惰性导入并缓存)"""
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
        """列出所有已注册配置"""
        with self._lock:
            return {k: dict(v) for k, v in self._configs.items()}

    def has_config(self, name: str) -> bool:
        """检查配置是否存在"""
        with self._lock:
            key = self._normalize_name(name)
            return key in self._configs

    def get_class_template(self, name: str) -> Dict[str, Any]:
        """获取自动发现的参数模板(占位值)"""
        with self._lock:
            key = self._normalize_name(name)
            if key not in self._configs:
                return {}
            cls = self.get_class(key)
            params_info = self._detect_params(cls)
            return self._generate_template(params_info)

    # -------- 写操作 --------
    def set_config(self, name: str, params: Dict[str, Any]):
        """替换 params 并落盘"""
        with self._lock:
            key = self._normalize_name(name)
            if key not in self._configs:
                raise ValueError(f"未知的会话类配置: {key}")
            self._configs[key]["params"] = dict(params)
            self._save_locked()

    def update_config(self, name: str, **kwargs):
        """部分更新 params"""
        with self._lock:
            key = self._normalize_name(name)
            if key not in self._configs:
                raise ValueError(f"未知的会话类配置: {key}")
            self._configs[key]["params"].update(kwargs)
            self._save_locked()

    def remove_config(self, name: str) -> bool:
        """移除配置及 class 缓存"""
        with self._lock:
            key = self._normalize_name(name)
            if key not in self._configs:
                return False
            self._configs.pop(key, None)
            self._class_cache.pop(key, None)
            self._save_locked()
            return True

    def reset(self, target: str = "all"):
        """重置所有配置"""
        with self._lock:
            self._configs = {}
            self._class_cache = {}
            self._save_locked()
