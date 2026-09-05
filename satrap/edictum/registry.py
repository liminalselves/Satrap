"""
Edictum 会话类型注册表

通过稳定的类型名称和工厂契约描述 Edictum 会话能力,
新增会话实现时只需注册类型定义, 无需修改冷配置或 Provider 分发设计
"""
from __future__ import annotations

from dataclasses import dataclass, field
import threading
from typing import Any, Awaitable, Callable, cast

from satrap.core.framework.Base import AsyncSession, Session


EDICTUM_PROVIDER = "edictum"
"""Edictum Provider 名称"""

EdictumFactory = Callable[..., Session | AsyncSession]
"""Edictum 会话工厂签名"""

EdictumPluginInstaller = Callable[
    [Session | AsyncSession, str, dict[str, Any] | None],
    object | Awaitable[object],
]
"""Edictum 类型的插件安装适配器, 子能力启停时返回值需作为插件句柄"""

EdictumPluginUninstaller = Callable[
    [Session | AsyncSession, str],
    bool | Awaitable[bool],
]
"""Edictum 类型的插件卸载适配器"""


@dataclass(frozen=True)
class EdictumTypeDefinition:
    """一个可注册的 Edictum 会话实现定义"""

    name: str
    factory: EdictumFactory
    is_async: bool
    description: str = ""
    config_schema: dict[str, Any] = field(default_factory=dict[str, Any])
    supports_plugins: bool = True
    supports_mcp: bool = True
    supports_stream: bool = True
    plugin_installer: EdictumPluginInstaller | None = None
    plugin_uninstaller: EdictumPluginUninstaller | None = None

    def to_dict(self) -> dict[str, Any]:
        """
        返回可通过 API 暴露的类型元数据

        返回:
        - dict[str, Any]: 不包含运行时工厂的类型元数据
        """
        return {
            "name": self.name,
            "is_async": self.is_async,
            "description": self.description,
            "config_schema": dict(self.config_schema),
            "capabilities": {
                "plugins": self.supports_plugins,
                "mcp": self.supports_mcp,
                "stream": self.supports_stream,
            },
        }


class EdictumTypeRegistry:
    """Edictum 会话类型注册表, 不依赖具体会话实现"""

    def __init__(self) -> None:
        """初始化空类型注册表"""
        self._definitions: dict[str, EdictumTypeDefinition] = {}
        self._lock = threading.RLock()

    def register(
        self,
        definition: EdictumTypeDefinition,
        *,
        replace: bool = False,
    ) -> None:
        """
        注册 Edictum 会话类型

        参数:
        - definition: 类型定义
        - replace: 是否允许覆盖同名定义
        """
        name = definition.name.strip()
        if not name:
            raise ValueError("Edictum 类型名称不能为空")
        if name != definition.name:
            raise ValueError("Edictum 类型名称不能包含首尾空格")
        with self._lock:
            if name in self._definitions and not replace:
                raise ValueError(f"Edictum 类型已存在: {name}")
            self._definitions[name] = definition

    def unregister(self, name: str) -> bool:
        """
        注销 Edictum 会话类型

        参数:
        - name: 类型名称

        返回:
        - bool: 是否找到并移除类型
        """
        with self._lock:
            return self._definitions.pop(name.strip(), None) is not None

    def get(self, name: str) -> EdictumTypeDefinition | None:
        """
        获取 Edictum 会话类型

        参数:
        - name: 类型名称

        返回:
        - EdictumTypeDefinition | None: 类型不存在时返回 None
        """
        with self._lock:
            return self._definitions.get(name.strip())

    def require(self, name: str) -> EdictumTypeDefinition:
        """
        获取 Edictum 会话类型, 不存在时抛出异常

        参数:
        - name: 类型名称

        返回:
        - EdictumTypeDefinition: 类型定义
        """
        definition = self.get(name)
        if definition is None:
            raise ValueError(f"未知 Edictum 类型: {name}")
        return definition

    def list_types(self) -> list[dict[str, Any]]:
        """
        列出全部 Edictum 类型元数据

        返回:
        - list[dict[str, Any]]: 按名称排序的类型元数据
        """
        with self._lock:
            definitions = list(self._definitions.values())
        return [item.to_dict() for item in sorted(definitions, key=lambda item: item.name)]

    def create(self, type_name: str, **kwargs: Any) -> Session | AsyncSession:
        """
        通过已注册工厂创建会话实例

        参数:
        - type_name: Edictum 类型名称
        - kwargs: 传给类型工厂的参数

        返回:
        - Session | AsyncSession: 已创建的会话实例
        """
        return self.require(type_name).factory(**kwargs)


def create_default_edictum_type_registry() -> EdictumTypeRegistry:
    """
    创建包含内置同步和异步简单会话的类型注册表

    返回:
    - EdictumTypeRegistry: 已注册内置类型的独立注册表
    """
    from satrap.edictum.simple_session import AsyncSimpleSession, SimpleSession

    def install_plugin(
        session: Session | AsyncSession,
        path: str,
        config: dict[str, Any] | None,
    ) -> object | Awaitable[object]:
        """
        调用内置简单会话的插件安装接口

        参数:
        - session: Edictum 会话实例
        - path: 插件目录
        - config: 会话级插件配置

        返回:
        - object | Awaitable[object]: 插件对象或异步安装任务
        """
        installer = getattr(session, "install_plugin", None)
        if not callable(installer):
            raise TypeError(f"Edictum 类型不支持插件安装: {type(session).__name__}")
        return installer(path, config=config)

    def uninstall_plugin(
        session: Session | AsyncSession,
        name: str,
    ) -> bool | Awaitable[bool]:
        """
        调用内置简单会话的插件卸载接口

        参数:
        - session: Edictum 会话实例
        - name: 插件名称

        返回:
        - bool | Awaitable[bool]: 是否成功卸载或异步卸载任务
        """
        uninstaller = getattr(session, "uninstall_plugin", None)
        if not callable(uninstaller):
            return False
        return cast(bool | Awaitable[bool], uninstaller(name))

    schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": True,
        "properties": {
            "system_prompt": {"type": ["string", "null"]},
            "db_path": {"type": "string"},
            "enable_checkpoint": {"type": "boolean"},
            "stream": {"type": "boolean"},
            "return_thinking": {"type": "boolean"},
        },
    }
    registry = EdictumTypeRegistry()
    registry.register(
        EdictumTypeDefinition(
            name="simple",
            factory=SimpleSession,
            is_async=False,
            description="同步单工作流 Edictum 会话",
            config_schema=schema,
            plugin_installer=install_plugin,
            plugin_uninstaller=uninstall_plugin,
        )
    )
    registry.register(
        EdictumTypeDefinition(
            name="async_simple",
            factory=AsyncSimpleSession,
            is_async=True,
            description="异步单工作流 Edictum 会话",
            config_schema=schema,
            plugin_installer=install_plugin,
            plugin_uninstaller=uninstall_plugin,
        )
    )
    return registry
