"""命名配置引用扫描的公共拒绝与错误映射, 调用方负责锁定写事务"""
from __future__ import annotations

from collections.abc import Callable
import traceback

from satrap.core.framework.BackGroundManager import ConfigInUseError, ConfigReferenceScanError
from satrap.core.log import logger


def guard_agent_reference(provider: str, name: str, checker: Callable[[str], list[dict[str, str]]] | None) -> None:
    """
    拒绝删除或重命名仍被 Agent 路由绑定的配置

    参数:
    - provider: session_class 或 edictum
    - name: 配置名称
    - checker: 当前事务的引用检查器, None 表示未装配
    """
    if checker is None:
        return
    try:
        references = checker(name)
    except Exception as error:
        logger.error(f"[Agent 配置] {provider}/{name} 引用扫描失败: {error}\n{traceback.format_exc()}")
        raise ConfigReferenceScanError(provider, name, type(error).__name__) from error
    if references:
        logger.warning(f"[Agent 配置] 拒绝变更被引用的 {provider}/{name}: {references}")
        raise ConfigInUseError(provider, name, references)
