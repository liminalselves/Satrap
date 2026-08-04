"""领域注册表: 维护领域注册与恢复顺序"""
from typing import Dict, List

from satrap.core.log import logger
from satrap.core.type import SnapshotDomain


class DomainRegistry:
    """领域注册表

    注册顺序即恢复顺序: 清理与恢复按注册顺序执行,
    跨领域存在外键引用时, 被引用领域必须先注册。
    """
    def __init__(self) -> None:
        self._domains: Dict[str, SnapshotDomain] = {}

    def register(self, domain: SnapshotDomain) -> None:
        """注册领域, 同名注册会覆盖 (幂等)

        参数:
        - domain: 领域注册声明
        """
        if not domain.name:
            raise ValueError("领域名称不能为空")
        if domain.name in self._domains:
            logger.debug(f"[DomainRegistry] 领域已存在, 将被覆盖: {domain.name}")
        self._domains[domain.name] = domain

    def get(self, name: str) -> SnapshotDomain:
        """按名称获取领域

        参数:
        - name: 领域名称

        返回:
        - SnapshotDomain: 领域注册声明

        异常:
        - ValueError: 领域未注册
        """
        domain = self._domains.get(name)
        if domain is None:
            raise ValueError(f"领域未注册: {name}")
        return domain

    def names(self) -> List[str]:
        """返回已注册领域名列表 (按注册顺序)"""
        return list(self._domains.keys())

    def all(self) -> List[SnapshotDomain]:
        """返回全部领域 (按注册顺序)"""
        return list(self._domains.values())
