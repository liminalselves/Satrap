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
        """自动检测并加载配置文件 (优先级: config.yaml > config.json > 默认)"""
        for candidate in ["config.yaml", "config.yml", "config.json"]:
            path = Path.cwd() / candidate
            if path.exists():
                loader = ConfigLoader.from_yaml if path.suffix in (".yaml", ".yml") else ConfigLoader.from_json
                config = loader(path)
                config = ConfigLoader.merge_env(config)
                logger.info(f"[ConfigLoader] 已加载配置: {path}")
                return config
        logger.info("[ConfigLoader] 未找到配置文件, 使用默认配置")
        return ConfigLoader.merge_env(BackendConfig())
