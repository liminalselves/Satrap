from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, fields
from pathlib import Path
from typing import Any, Dict, Literal, TypeVar

from satrap.core.log import logger
from satrap.core.type import EmbeddingConfig, LLMConfig, ReRankConfig


ConfigTarget = Literal["llm", "embedding", "rerank"]
ResetTarget = Literal["llm", "embedding", "rerank", "all"]
TConfig = TypeVar("TConfig", LLMConfig, EmbeddingConfig, ReRankConfig)


class ModelConfigManager:
    """模型配置管理器

    支持:
    - LLM/Embedding/ReRank 多配置(按 name 区分)
    - 配置持久化到 JSON
    - 兼容旧版单配置格式, 自动迁移为 `default`

    注意:
    - api_key 按需求明文存储, 便于运行时直接读取使用
    - 脱敏仅用于展示场景
    """

    DEFAULT_NAME = "default"

    def __init__(self, storage_path: str | Path | None = None, auto_create: bool = True):
        self._lock = threading.RLock()
        self.storage_path = Path(storage_path) if storage_path else self._default_storage_path()

        self._llm_configs: Dict[str, LLMConfig] = {
            self.DEFAULT_NAME: LLMConfig(name=self.DEFAULT_NAME)
        }
        self._embedding_configs: Dict[str, EmbeddingConfig] = {
            self.DEFAULT_NAME: EmbeddingConfig(name=self.DEFAULT_NAME)
        }
        self._rerank_configs: Dict[str, ReRankConfig] = {
            self.DEFAULT_NAME: ReRankConfig(name=self.DEFAULT_NAME)
        }

        if auto_create:
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self.reload()

    @staticmethod
    def _default_storage_path() -> Path:
        """获取默认模型配置存储路径"""
        env_path = os.getenv("SATRAP_MODEL_CONFIG_PATH")
        if env_path:
            return Path(env_path)
        return Path.cwd() / ".satrap" / "model_config.json"

    @staticmethod
    def _safe_key(api_key: str | None) -> str | None:
        """脱敏 API 密钥"""
        if not api_key:
            return api_key
        if len(api_key) <= 4:
            return "*" * len(api_key)
        return f"{'*' * (len(api_key) - 4)}{api_key[-4:]}"

    @staticmethod
    def _from_dict(payload: Dict[str, Any], cls: type[TConfig]) -> TConfig:
        """从字典创建配置实例"""
        allow = {f.name for f in fields(cls)}
        data = {k: v for k, v in payload.items() if k in allow}
        return cls(**data)

    @classmethod
    def _deserialize_named_configs(
        cls,
        raw: Any,
        config_cls: type[TConfig],
        default_name: str,
    ) -> Dict[str, TConfig]:
        """反序列化命名配置"""
        # 新格式: {"name1": {...}, "name2": {...}}
        # 旧格式: {"model": "...", ...}
        result: Dict[str, TConfig] = {}

        if not isinstance(raw, dict) or not raw:
            default_cfg = config_cls(name=default_name)
            return {default_name: default_cfg}

        dataclass_fields = {f.name for f in fields(config_cls)}
        looks_like_old_single = any(key in dataclass_fields for key in raw.keys())

        if looks_like_old_single:
            cfg = cls._from_dict(raw, config_cls)
            cfg.name = cfg.name or default_name
            return {cfg.name: cfg}

        for name, value in raw.items():
            if not isinstance(value, dict):
                continue
            cfg = cls._from_dict(value, config_cls)
            cfg_name = cfg.name or str(name)
            cfg.name = cfg_name
            result[cfg_name] = cfg

        if not result:
            default_cfg = config_cls(name=default_name)
            return {default_name: default_cfg}
        return result

    def _mask_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """脱敏配置字典"""
        cfg = dict(payload)
        if cfg.get("lock_api_key", True):
            cfg["api_key"] = self._safe_key(cfg.get("api_key"))
        return cfg

    def _to_payload_locked(self, mask_api_key: bool = False) -> Dict[str, Dict[str, Dict[str, Any]]]:
        """序列化配置"""
        def dump_named(source: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
            output: Dict[str, Dict[str, Any]] = {}
            for name, cfg in source.items():
                payload = asdict(cfg)
                payload["name"] = payload.get("name") or name
                output[name] = self._mask_payload(payload) if mask_api_key else payload
            return output

        return {
            "llm": dump_named(self._llm_configs),
            "embedding": dump_named(self._embedding_configs),
            "rerank": dump_named(self._rerank_configs),
        }

    def _save_locked(self):
        """保存配置"""
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        with self.storage_path.open("w", encoding="utf-8") as f:
            json.dump(self._to_payload_locked(mask_api_key=False), f, ensure_ascii=False, indent=2)

    def reload(self):
        """重新加载配置"""
        with self._lock:
            if not self.storage_path.exists():
                self._save_locked()
                return

            try:
                with self.storage_path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as e:
                logger.error(f"[ModelConfigManager] 读取配置失败: {e}")
                return

            try:
                if not isinstance(data, dict):
                    logger.warning("[ModelConfigManager] 配置文件格式错误，已忽略并保留内存默认值")
                    return

                self._llm_configs = self._deserialize_named_configs(
                    data.get("llm", {}), LLMConfig, self.DEFAULT_NAME
                )
                self._embedding_configs = self._deserialize_named_configs(
                    data.get("embedding", {}), EmbeddingConfig, self.DEFAULT_NAME
                )
                self._rerank_configs = self._deserialize_named_configs(
                    data.get("rerank", {}), ReRankConfig, self.DEFAULT_NAME
                )
                self._save_locked()
            except Exception as e:
                logger.error(f"[ModelConfigManager] 解析配置失败: {e}")

    def save(self):
        """保存配置"""
        with self._lock:
            self._save_locked()

    @staticmethod
    def _normalize_name(name: str | None) -> str:
        """归一化配置名称"""
        return (name or ModelConfigManager.DEFAULT_NAME).strip() or ModelConfigManager.DEFAULT_NAME

    def _target_store(self, target: ConfigTarget) -> Dict[str, Any]:
        """获取指定配置目标的存储字典"""
        if target == "llm":
            return self._llm_configs
        if target == "embedding":
            return self._embedding_configs
        return self._rerank_configs

    # -------- LLM --------
    def get_llm_config(self, name: str = DEFAULT_NAME) -> LLMConfig:
        """获取 LLM 配置"""
        with self._lock:
            key = self._normalize_name(name)
            cfg = self._llm_configs.get(key)
            if cfg is None:
                return LLMConfig(name=key)
            return LLMConfig(**asdict(cfg))

    def list_llm_configs(self, mask_api_key: bool = False) -> Dict[str, Dict[str, Any]]:
        """列出所有 LLM 配置"""
        with self._lock:
            return self._to_payload_locked(mask_api_key=mask_api_key)["llm"]

    def set_llm_config(self, config: LLMConfig, name: str | None = None):
        """设置 LLM 配置"""
        with self._lock:
            key = self._normalize_name(name or config.name)
            payload = asdict(config)
            payload["name"] = key
            self._llm_configs[key] = LLMConfig(**payload)
            self._save_locked()

    def update_llm_config(self, name: str = DEFAULT_NAME, **kwargs):
        """更新 LLM 配置"""
        with self._lock:
            key = self._normalize_name(name)
            current = asdict(self._llm_configs.get(key, LLMConfig(name=key)))
            current.update(kwargs)
            current["name"] = key
            self._llm_configs[key] = self._from_dict(current, LLMConfig)
            self._save_locked()

    def remove_llm_config(self, name: str) -> bool:
        """删除 LLM 配置"""
        with self._lock:
            key = self._normalize_name(name)
            if key not in self._llm_configs:
                return False
            if len(self._llm_configs) <= 1:
                self._llm_configs[key] = LLMConfig(name=key)
            else:
                self._llm_configs.pop(key, None)
            self._save_locked()
            return True

    # -------- Embedding --------
    def get_embedding_config(self, name: str = DEFAULT_NAME) -> EmbeddingConfig:
        """获取 Embedding 配置"""
        with self._lock:
            key = self._normalize_name(name)
            cfg = self._embedding_configs.get(key)
            if cfg is None:
                return EmbeddingConfig(name=key)
            return EmbeddingConfig(**asdict(cfg))

    def list_embedding_configs(self, mask_api_key: bool = False) -> Dict[str, Dict[str, Any]]:
        """列出所有 Embedding 配置"""
        with self._lock:
            return self._to_payload_locked(mask_api_key=mask_api_key)["embedding"]

    def set_embedding_config(self, config: EmbeddingConfig, name: str | None = None):
        """设置 Embedding 配置"""
        with self._lock:
            key = self._normalize_name(name or config.name)
            payload = asdict(config)
            payload["name"] = key
            self._embedding_configs[key] = EmbeddingConfig(**payload)
            self._save_locked()

    def update_embedding_config(self, name: str = DEFAULT_NAME, **kwargs):
        """更新 Embedding 配置"""
        with self._lock:
            key = self._normalize_name(name)
            current = asdict(self._embedding_configs.get(key, EmbeddingConfig(name=key)))
            current.update(kwargs)
            current["name"] = key
            self._embedding_configs[key] = self._from_dict(current, EmbeddingConfig)
            self._save_locked()

    def remove_embedding_config(self, name: str) -> bool:
        """删除 Embedding 配置"""
        with self._lock:
            key = self._normalize_name(name)
            if key not in self._embedding_configs:
                return False
            if len(self._embedding_configs) <= 1:
                self._embedding_configs[key] = EmbeddingConfig(name=key)
            else:
                self._embedding_configs.pop(key, None)
            self._save_locked()
            return True

    # -------- ReRank --------
    def get_rerank_config(self, name: str = DEFAULT_NAME) -> ReRankConfig:
        """获取 ReRank 配置"""
        with self._lock:
            key = self._normalize_name(name)
            cfg = self._rerank_configs.get(key)
            if cfg is None:
                return ReRankConfig(name=key)
            return ReRankConfig(**asdict(cfg))

    def list_rerank_configs(self, mask_api_key: bool = False) -> Dict[str, Dict[str, Any]]:
        """列出所有 ReRank 配置"""
        with self._lock:
            return self._to_payload_locked(mask_api_key=mask_api_key)["rerank"]

    def set_rerank_config(self, config: ReRankConfig, name: str | None = None):
        """设置 ReRank 配置"""
        with self._lock:
            key = self._normalize_name(name or config.name)
            payload = asdict(config)
            payload["name"] = key
            self._rerank_configs[key] = ReRankConfig(**payload)
            self._save_locked()

    def update_rerank_config(self, name: str = DEFAULT_NAME, **kwargs):
        """更新 ReRank 配置"""
        with self._lock:
            key = self._normalize_name(name)
            current = asdict(self._rerank_configs.get(key, ReRankConfig(name=key)))
            current.update(kwargs)
            current["name"] = key
            self._rerank_configs[key] = self._from_dict(current, ReRankConfig)
            self._save_locked()

    def remove_rerank_config(self, name: str) -> bool:
        """删除 ReRank 配置"""
        with self._lock:
            key = self._normalize_name(name)
            if key not in self._rerank_configs:
                return False
            if len(self._rerank_configs) <= 1:
                self._rerank_configs[key] = ReRankConfig(name=key)
            else:
                self._rerank_configs.pop(key, None)
            self._save_locked()
            return True

    # -------- Common --------
    def get_all_configs(self, mask_api_key: bool = False) -> Dict[str, Dict[str, Dict[str, Any]]]:
        """获取所有配置"""
        with self._lock:
            return self._to_payload_locked(mask_api_key=mask_api_key)

    def get_runtime_configs(self) -> Dict[str, Dict[str, Dict[str, Any]]]:
        """获取运行时配置"""
        with self._lock:
            return self._to_payload_locked(mask_api_key=False)

    def has_config(self, target: ConfigTarget, name: str) -> bool:
        """检查是否存在指定配置"""
        with self._lock:
            key = self._normalize_name(name)
            return key in self._target_store(target)

    def reset(self, target: ResetTarget = "all"):
        """重置指定配置"""
        with self._lock:
            if target in ("llm", "all"):
                self._llm_configs = {self.DEFAULT_NAME: LLMConfig(name=self.DEFAULT_NAME)}
            if target in ("embedding", "all"):
                self._embedding_configs = {
                    self.DEFAULT_NAME: EmbeddingConfig(name=self.DEFAULT_NAME)
                }
            if target in ("rerank", "all"):
                self._rerank_configs = {self.DEFAULT_NAME: ReRankConfig(name=self.DEFAULT_NAME)}
            self._save_locked()
