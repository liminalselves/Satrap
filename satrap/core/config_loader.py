from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict

from satrap.core.backend.BackendManager import BackendConfig
from satrap.core.log import logger


class ConfigLoader:
    """配置加载器

    支持 YAML / JSON / Dict 三种来源, 以及环境变量覆盖敏感字段
    示例:
        config = ConfigLoader.from_yaml("config.yaml")
        config = ConfigLoader.from_json("config.json")
        config = ConfigLoader.merge_env(config)
    """

    @staticmethod
    def default_config_document(config: BackendConfig | None = None) -> dict[str, Any]:
        """生成默认配置文档"""
        cfg = config or BackendConfig()
        return {
            "model_config_path": cfg.model_config_path,
            "session_class_config_path": cfg.session_class_config_path,
            "session_db_path": cfg.session_db_path,
            "user_db_path": cfg.user_db_path,
            "default_session_type": cfg.default_session_type,
            "max_sessions": cfg.max_sessions,
            "idle_timeout": cfg.idle_timeout,
            "rate_limit": cfg.rate_limit,
            "rate_burst": cfg.rate_burst,
            "llm_timeout": cfg.llm_timeout,
            "error_feedback": cfg.error_feedback,
            "session_classes": dict(cfg.session_classes),
            "session_scan_paths": list(cfg.session_scan_paths),
            "api": {
                "host": cfg.api_host,
                "port": cfg.api_port,
            },
            "platforms": list(cfg.platforms),
        }

    @staticmethod
    def candidate_paths(cwd: str | Path | None = None) -> list[Path]:
        """返回配置文件自动检测路径, 根目录优先, .satrap 目录其次, satrap 目录兜底"""
        root = Path(cwd) if cwd is not None else Path.cwd()
        names = ("config.yaml", "config.yml", "config.json")
        return (
            [root / name for name in names]
            + [root / ".satrap" / name for name in names]
            + [root / "satrap" / name for name in names]
        )

    @staticmethod
    def default_config_path(cwd: str | Path | None = None) -> Path:
        """返回无配置时应创建的默认配置路径"""
        root = Path(cwd) if cwd is not None else Path.cwd()
        return root / ".satrap" / "config.yaml"

    @staticmethod
    def ensure_default_config(cwd: str | Path | None = None) -> Path:
        """确保默认配置文件存在, 不覆盖已有配置"""
        for path in ConfigLoader.candidate_paths(cwd):
            if path.exists():
                return path

        path = ConfigLoader.default_config_path(cwd)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            import yaml
            text = yaml.safe_dump(
                ConfigLoader.default_config_document(),
                allow_unicode=True,
                sort_keys=False,
            )
        except ImportError:
            text = json.dumps(ConfigLoader.default_config_document(), ensure_ascii=False, indent=2) + "\n"
        path.write_text(text, encoding="utf-8")
        logger.info(f"[ConfigLoader] 已创建默认配置: {path}")
        return path

    @staticmethod
    def from_yaml(path: str | Path) -> BackendConfig:
        """从 YAML 文件加载配置; 需要 PyYAML 库"""
        path = Path(path)
        if not path.exists():
            logger.warning(f"[ConfigLoader] YAML 文件不存在: {path}, 返回默认配置")
            return BackendConfig()

        try:
            import yaml
            with path.open("r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if not isinstance(data, dict):
                raise ValueError("YAML 根节点必须是字典")
            return BackendConfig.from_dict(data)
        except ImportError:
            logger.error("[ConfigLoader] 需要 PyYAML 库: pip install pyyaml, 回退到 JSON")
            return ConfigLoader.from_json(path.with_suffix(".json"))
        except Exception as e:
            logger.error(f"[ConfigLoader] YAML 加载失败: {e}")
            return BackendConfig()

    @staticmethod
    def from_json(path: str | Path) -> BackendConfig:
        """从 JSON 文件加载配置"""
        path = Path(path)
        if not path.exists():
            logger.warning(f"[ConfigLoader] JSON 文件不存在: {path}, 返回默认配置")
            return BackendConfig()

        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("JSON 根节点必须是字典")
            return BackendConfig.from_dict(data)
        except Exception as e:
            logger.error(f"[ConfigLoader] JSON 加载失败: {e}")
            return BackendConfig()

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> BackendConfig:
        """从字典加载配置"""
        return BackendConfig.from_dict(data)

    @staticmethod
    def merge_env(config: BackendConfig) -> BackendConfig:
        """用环境变量覆盖配置中的敏感字段

        支持的环境变量:
        - SATRAP_MODEL_CONFIG_PATH
        - SATRAP_SESSION_CLASS_CONFIG_PATH
        - SATRAP_DB_DIR (同时更新 session_db 和 user_db 的目录)
        - SATRAP_LLM_TIMEOUT
        """
        env_map = {
            "SATRAP_MODEL_CONFIG_PATH": "model_config_path",
            "SATRAP_SESSION_CLASS_CONFIG_PATH": "session_class_config_path",
            "SATRAP_API_HOST": "api_host",
        }
        for env_key, attr in env_map.items():
            val = os.getenv(env_key)
            if val:
                setattr(config, attr, val)

        db_dir = os.getenv("SATRAP_DB_DIR")
        if db_dir:
            if config.session_db_path:
                config.session_db_path = str(Path(db_dir) / Path(config.session_db_path).name)
            if config.user_db_path:
                config.user_db_path = str(Path(db_dir) / Path(config.user_db_path).name)

        timeout = os.getenv("SATRAP_LLM_TIMEOUT")
        if timeout:
            try:
                config.llm_timeout = float(timeout)
            except ValueError:
                pass

        api_port = os.getenv("SATRAP_API_PORT")
        if api_port:
            try:
                config.api_port = int(api_port)
            except ValueError:
                pass

        return config

    @staticmethod
    def autodetect() -> BackendConfig:
        """自动检测并加载配置文件, 不存在时创建 .satrap/config.yaml"""
        for path in ConfigLoader.candidate_paths():
            if path.exists():
                loader = ConfigLoader.from_yaml if path.suffix in (".yaml", ".yml") else ConfigLoader.from_json
                config = loader(path)
                config = ConfigLoader.merge_env(config)
                logger.info(f"[ConfigLoader] 已加载配置: {path}")
                return config
        path = ConfigLoader.ensure_default_config()
        config = ConfigLoader.from_yaml(path)
        config = ConfigLoader.merge_env(config)
        logger.info(f"[ConfigLoader] 已加载默认配置: {path}")
        return config
