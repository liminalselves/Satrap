"""模型配置共享领域服务"""
from __future__ import annotations

from dataclasses import fields
from typing import Any, cast

from satrap.core.framework.BackGroundManager import ConfigTarget, ModelConfigManager
from satrap.core.type import EmbeddingConfig, LLMConfig, ReRankConfig, validate_thinking_levels


_CONFIG_CLASSES = {
    "llm": LLMConfig,
    "embedding": EmbeddingConfig,
    "rerank": ReRankConfig,
}
"""模型配置类型到数据类的映射"""


class ModelConfigService:
    """供平台后端和控制服务共用的模型配置增删改查服务"""

    def __init__(self, manager: ModelConfigManager) -> None:
        """
        初始化模型配置服务

        参数:
        - manager: 模型配置管理器
        """
        self.manager = manager

    def list_configs(self, target: str) -> dict[str, dict[str, Any]]:
        """
        列出指定类型的脱敏模型配置

        参数:
        - target: 模型配置类型

        返回:
        - dict[str, dict[str, Any]]: 按名称组织的脱敏配置
        """
        normalized = self._validate_target(target)
        if normalized == "llm":
            return self.manager.list_llm_configs(mask_api_key=True)
        if normalized == "embedding":
            return self.manager.list_embedding_configs(mask_api_key=True)
        return self.manager.list_rerank_configs(mask_api_key=True)

    def create(self, target: str, name: str, payload: object) -> None:
        """
        创建或覆盖模型配置

        参数:
        - target: 模型配置类型
        - name: 配置名称
        - payload: 配置字段
        """
        normalized = self._validate_target(target)
        config_name = self._validate_name(name)
        cleaned = self._validate_payload(normalized, payload, allow_masked_key=False)
        cleaned["name"] = config_name
        if normalized == "llm":
            self.manager.set_llm_config(LLMConfig(**cleaned), name=config_name)
        elif normalized == "embedding":
            self.manager.set_embedding_config(EmbeddingConfig(**cleaned), name=config_name)
        else:
            self.manager.set_rerank_config(ReRankConfig(**cleaned), name=config_name)

    def update(self, target: str, name: str, payload: object) -> None:
        """
        更新模型配置并保留未修改字段

        参数:
        - target: 模型配置类型
        - name: 配置名称
        - payload: 待更新字段
        """
        normalized = self._validate_target(target)
        config_name = self._validate_name(name)
        cleaned = self._validate_payload(normalized, payload, allow_masked_key=True)
        requested_name = cleaned.pop("name", config_name)
        new_name = self._validate_name(str(requested_name or ""))
        self.manager.update_named_config(
            normalized,
            config_name,
            cleaned,
            new_name=new_name,
        )

    def delete(self, target: str, name: str) -> bool:
        """
        删除模型配置

        参数:
        - target: 模型配置类型
        - name: 配置名称

        返回:
        - bool: 配置是否存在并已删除或重置
        """
        normalized = self._validate_target(target)
        config_name = self._validate_name(name)
        if normalized == "llm":
            return self.manager.remove_llm_config(config_name)
        if normalized == "embedding":
            return self.manager.remove_embedding_config(config_name)
        return self.manager.remove_rerank_config(config_name)

    @staticmethod
    def _validate_target(target: str) -> ConfigTarget:
        """
        校验模型配置类型

        参数:
        - target: 模型配置类型

        返回:
        - ConfigTarget: 合法的模型配置类型
        """
        if target not in _CONFIG_CLASSES:
            raise ValueError(f"未知模型类型: {target}")
        return cast(ConfigTarget, target)

    @staticmethod
    def _validate_name(name: str) -> str:
        """
        校验模型配置名称

        参数:
        - name: 配置名称

        返回:
        - str: 去除首尾空格后的名称
        """
        normalized = str(name or "").strip()
        if not normalized:
            raise ValueError("模型配置名称不能为空")
        return normalized

    @staticmethod
    def _validate_payload(
        target: ConfigTarget,
        payload: object,
        *,
        allow_masked_key: bool,
    ) -> dict[str, Any]:
        """
        校验配置字段并处理展示用脱敏密钥

        参数:
        - target: 模型配置类型
        - payload: 配置字段
        - allow_masked_key: 是否允许忽略脱敏后的 API Key

        返回:
        - dict[str, Any]: 可写入管理器的配置字段
        """
        if not isinstance(payload, dict):
            raise ValueError("模型配置必须是对象")
        config_class = _CONFIG_CLASSES[target]
        allowed = {field.name for field in fields(config_class)}
        raw = dict(cast(dict[str, Any], payload))
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise ValueError(f"未知模型配置字段: {', '.join(unknown)}")
        api_key = raw.get("api_key")
        if isinstance(api_key, str) and "*" in api_key:
            if not allow_masked_key:
                raise ValueError("不能使用脱敏后的 API Key 创建配置")
            raw.pop("api_key", None)
        if target == "llm" and "thinking_levels" in raw:
            raw["thinking_levels"] = validate_thinking_levels(raw["thinking_levels"])
        return raw
