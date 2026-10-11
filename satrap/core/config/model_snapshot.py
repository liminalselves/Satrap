"""运行应用的只读模型来源, 不写文件或缓存调用授权"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
from typing import Any, Protocol, TypeVar

from satrap.core.framework.BackGroundManager import ConfigTarget, ModelConfigManager
from satrap.core.type import ASRConfig, EmbeddingConfig, LLMConfig, ReRankConfig


class ModelConfigReader(Protocol):
    """插件资源需要的只读模型配置接口"""

    def has_config(self, target: ConfigTarget, name: str) -> bool: ...
    def get_llm_config(self, name: str = "default") -> LLMConfig: ...
    def get_embedding_config(self, name: str = "default") -> EmbeddingConfig: ...
    def get_rerank_config(self, name: str = "default") -> ReRankConfig: ...
    def get_asr_config(self, name: str = "default") -> ASRConfig: ...


TModel = TypeVar("TModel", LLMConfig, EmbeddingConfig, ReRankConfig, ASRConfig)


def llm_config_fingerprint(config: LLMConfig) -> str:
    """
    计算实际模型与上下文参数的内部指纹, 不输出凭据正文

    参数:
    - config: 完整模型配置

    返回:
    - 内部比较用 SHA-256 摘要
    """
    return hashlib.sha256(json.dumps(asdict(config), ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


class ModelConfigSnapshot:
    """同一次应用中独立持有的模型配置, 只有读取接口"""

    def __init__(self, storage_path: Path, values: dict[str, dict[str, Any]]) -> None:
        """
        保存已解析的独立配置

        参数:
        - storage_path: 原模型文件, 仅用于选择本次来源
        - values: 各类型的命名模型对象
        """
        self.storage_path = storage_path
        self._values = deepcopy(values)

    @classmethod
    def read(cls, manager: ModelConfigManager) -> ModelConfigSnapshot:
        """
        严格只读解析实际文件, 不调用会吞错误或回写文件的 reload

        参数:
        - manager: 模型来源管理器

        返回:
        - 独立只读配置; 来源缺失或损坏时抛出, 应用入口记录并捕获
        """
        path = manager.storage_path.resolve()
        with path.open("r", encoding="utf-8") as stream:
            payload = json.load(stream)
        if not isinstance(payload, dict):
            raise ValueError("模型配置必须是对象")

        def parse(target: str, kind: type[TModel]) -> dict[str, TModel]:
            """
            校验一类模型并复用既有旧格式迁移语义, 不写来源文件

            参数:
            - target: 模型类型
            - kind: 对应配置数据类

            返回:
            - 该类型的命名配置
            """
            raw = payload.get(target, {})
            if not isinstance(raw, dict):
                raise ValueError(f"模型配置 {target} 格式错误")
            legacy = any(key in kind.__dataclass_fields__ for key in raw)
            if not legacy and any(not isinstance(value, dict) for value in raw.values()):
                raise ValueError(f"模型配置 {target} 格式错误")
            return ModelConfigManager._deserialize_named_configs(raw, kind, ModelConfigManager.DEFAULT_NAME)

        values: dict[str, dict[str, Any]] = {
            "llm": parse("llm", LLMConfig), "embedding": parse("embedding", EmbeddingConfig),
            "rerank": parse("rerank", ReRankConfig), "asr": parse("asr", ASRConfig),
        }
        return cls(path, values)

    def has_config(self, target: ConfigTarget, name: str) -> bool:
        """
        检查捕获来源中是否包含指定名称

        参数:
        - target: 模型类型
        - name: 模型名称

        返回:
        - 是否存在, 不回退当前管理器
        """
        return ModelConfigManager._normalize_name(name) in self._values[target]

    def _get(self, target: str, name: str, kind: type[TModel]) -> TModel:
        """
        返回独立副本, 缺失名称沿用主模型管理器的空配置契约

        参数:
        - target: 模型类型
        - name: 模型名称
        - kind: 配置数据类

        返回:
        - 独立模型对象
        """
        key = ModelConfigManager._normalize_name(name)
        value = self._values[target].get(key)
        return deepcopy(value) if value is not None else kind(name=key)

    def get_llm_config(self, name: str = "default") -> LLMConfig:
        """读取捕获的主模型参数, 返回独立副本"""
        return self._get("llm", name, LLMConfig)

    def get_embedding_config(self, name: str = "default") -> EmbeddingConfig:
        """读取捕获的向量模型参数, 返回独立副本"""
        return self._get("embedding", name, EmbeddingConfig)

    def get_rerank_config(self, name: str = "default") -> ReRankConfig:
        """读取捕获的重排模型参数, 返回独立副本"""
        return self._get("rerank", name, ReRankConfig)

    def get_asr_config(self, name: str = "default") -> ASRConfig:
        """读取捕获的转写模型参数, 返回独立副本"""
        return self._get("asr", name, ASRConfig)
