"""
插件专用服务基类

服务与模型工具分别注册, 业务参数由 execute 的真实签名定义;
同步与异步实现保持独立, 不自动桥接执行方式
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class _ServiceBase(ABC):
    """维护插件专用能力的名称, 说明和独立开关"""

    service_name: str = ""
    description: str = ""
    _enabled: bool = True   # 子类可自由定义构造方法, 开关变更只写入当前实例

    def enable(self) -> None:
        """启用服务独立开关"""
        self._enabled = True

    def disable(self) -> None:
        """停用服务独立开关"""
        self._enabled = False

    def is_enabled(self) -> bool:
        """
        读取服务独立开关

        返回:
        - 独立启用状态, 不代表所属插件和当前调用权限均满足
        """
        return self._enabled


class Service(_ServiceBase):
    """仅供插件程序调用的同步服务"""

    @abstractmethod
    def execute(self, *args: Any, **kwargs: Any) -> object:
        """
        同步执行业务, 子类用明确类型声明自身参数与返回值

        参数:
        - args: 兼容子类不同业务签名的动态边界, 分派器不传入位置参数
        - kwargs: 按子类真实签名绑定的业务参数

        返回:
        - 业务数据, 实际数据范围由受控分派边界核验
        """
        raise NotImplementedError


class AsyncService(_ServiceBase):
    """仅供插件程序调用的异步服务"""

    @abstractmethod
    async def execute(self, *args: Any, **kwargs: Any) -> object:
        """
        异步执行业务, 子类用明确类型声明自身参数与返回值

        参数:
        - args: 兼容子类不同业务签名的动态边界, 分派器不传入位置参数
        - kwargs: 按子类真实签名绑定的业务参数

        返回:
        - 业务数据, 实际数据范围由受控分派边界核验
        """
        raise NotImplementedError
