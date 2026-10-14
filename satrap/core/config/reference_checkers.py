"""CLI 命名资源引用检查器的公共装配, 保留 ASR 与 Agent 的扫描领域"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal, TYPE_CHECKING

from satrap.core.storage import StorageLayout

if TYPE_CHECKING:
    from satrap.core.backend.BackendManager import BackendConfig


def build_reference_checker(target: Literal["asr", "session_class", "edictum"], config: BackendConfig,
                            *, session_classes: Callable[[], dict[str, dict[str, Any]]] | None = None) -> Callable[[str], list[dict[str, str]]]:
    """
    为离线配置管理装配引用检查, 会话类声明在实际检查时读取

    参数:
    - target: 引用领域
    - config: 当前 CLI 配置
    - session_classes: 会话类配置的实时读取器

    返回:
    - 输入配置名称并返回完整引用清单的检查器
    """
    model_layout = StorageLayout(config.data_root) if target == "asr" else None

    def check(name: str) -> list[dict[str, str]]:
        """保留各领域原有数据目录与默认绑定读取时机"""
        if target == "asr":
            from satrap.core.config.asr_references import list_asr_config_references
            return list_asr_config_references(name, platforms=list(config.platforms), layout=model_layout)
        from satrap.core.config.agent_references import list_agent_references
        return list_agent_references(target, name, platforms=config.platforms, layout=StorageLayout(config.data_root),
            default_session_type=config.default_session_type,
            session_classes=session_classes() if session_classes is not None else None)

    return check
