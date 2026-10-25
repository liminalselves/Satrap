"""
Chat 整体默认设置的独立持久化

复用配置修订, 文件锁和原子保存, 只决定以后创建的会话,
不在此处维护会话绑定或复制模型凭据
"""
from __future__ import annotations

from pathlib import Path
from typing import TypedDict
import json

from satrap.core.config.document import ConfigRevisionConflict, config_document_revision
from satrap.core.storage.persist import atomic_write_text
from satrap.core.storage.file_lock import FileLock
from satrap.core.config_paths import get_config_path


class ChatSettings(TypedDict):
    """整体默认模型与读取时的配置版本"""
    default_model: str
    revision: str


class ChatSettingsStore:
    """按内容修订保存整体默认, 不缓存其它进程的旧配置"""

    def __init__(self, path: Path | None = None) -> None:
        """
        初始化设置位置

        参数:
        - path: 可选的隔离路径, 默认 .satrap/config/chat_settings.json
        """
        self.path = path or get_config_path("chat_settings.json")

    def read(self) -> ChatSettings:
        """
        读取整体默认设置

        返回:
        - 默认模型名和修订, 缺少文件时使用 default; 损坏文件向请求边界抛错
        """
        try:
            payload: object = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            payload = {"default_model": "default"}
        if not isinstance(payload, dict) or set(payload) != {"default_model"}:
            raise ValueError("Chat 设置格式无效")
        model: object = payload.get("default_model")
        if not isinstance(model, str) or not model.strip() or model != model.strip():
            raise ValueError("Chat 默认模型名无效")
        return ChatSettings(default_model=model, revision=config_document_revision(payload))

    def save(self, model: str, expected_revision: str) -> ChatSettings:
        """
        在修订一致时原子保存整体默认

        参数:
        - model: 已由服务层验证存在的模型配置名
        - expected_revision: 最近读取的配置修订, 不允许无条件覆盖

        返回:
        - 已保存设置; 修订冲突抛出 ConfigRevisionConflict, 文件错误由请求边界处理
        """
        if not isinstance(model, str) or not model.strip() or model != model.strip():
            raise ValueError("Chat 默认模型名无效")
        if not isinstance(expected_revision, str) or not expected_revision:
            raise ValueError("缺少 Chat 设置修订")
        with FileLock(self.path.with_name(f".{self.path.name}.lock"), timeout=5):
            if self.read()["revision"] != expected_revision:
                raise ConfigRevisionConflict("Chat 默认设置已更新, 请刷新后重试")
            payload = {"default_model": model}
            atomic_write_text(self.path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n", newline="\n", durable=True)
            return ChatSettings(default_model=model, revision=config_document_revision(payload))
