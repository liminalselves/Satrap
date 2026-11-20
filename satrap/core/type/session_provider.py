"""会话 Provider 的公共定义与运行时接口"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from satrap.core.type.session import SessionConfig

if TYPE_CHECKING:
    from satrap.core.APICall.LLMCall import AsyncLLM, LLM
    from satrap.core.framework.Base import AsyncSession, Session


SESSION_CLASS_PROVIDER = "session_class"
"""传统扫描式 Session 类 Provider 名称"""


class BindingState(str, Enum):
    """命名会话绑定在运行时是否可执行的判定结果"""

    RUNNABLE = "runnable"
    """定义存在且未被禁用, 可以创建并运行会话"""

    DISABLED = "disabled"
    """定义存在但被禁用, 属于正常配置状态而非配置错误"""

    INVALID = "invalid"
    """Provider 未注册, 定义不存在或名称歧义, 属于配置错误"""


@dataclass(frozen=True)
class BindingStatus:
    """绑定判定结果, 保留既有诊断文本供生命周期与诊断输出复用"""

    state: BindingState
    """绑定可运行, 禁用或无效的判定状态"""
    reason: str = ""
    """供生命周期与诊断复用的判定说明, 默认空字符串表示无附加原因"""


@dataclass(frozen=True)
class SessionProviderDefinition:
    """Provider 暴露给运行时的统一命名会话定义"""

    name: str
    """Provider 内的命名会话定义名称, 用于解析实例绑定"""
    provider_name: str
    """提供此定义的 Provider 唯一名称, 与定义名称共同定位绑定"""
    is_async: bool
    """定义创建的会话是否异步, 用于选择对应模型和运行方式"""
    enabled: bool = True
    """定义是否允许创建和运行会话, 默认启用"""
    model_key: str = "model_name"
    """定义参数中指定模型的键名, 默认 model_name"""
    params: dict[str, Any] = field(default_factory=dict[str, Any])
    """Provider 的会话默认构造参数, 默认独立空字典, 可被实例配置覆盖"""
    description: str = ""
    """供目录和诊断展示的定义描述, 默认空字符串"""
    metadata: dict[str, Any] = field(default_factory=dict[str, Any])
    """Provider 专用定义元数据, 默认独立空字典, 例如类路径或 Edictum 类型"""


@runtime_checkable
class SessionProvider(Protocol):
    """命名会话定义的运行时 Provider 契约"""

    @property
    def provider_name(self) -> str:
        """返回 Provider 唯一名称"""
        ...

    def has_definition(self, name: str) -> bool:
        """
        判断命名会话定义是否存在

        参数:
        - name: 命名会话定义名称

        返回:
        - bool: 命名会话定义是否存在
        """
        ...

    def get_definition(self, name: str) -> SessionProviderDefinition | None:
        """
        获取统一命名会话定义

        参数:
        - name: 命名会话定义名称

        返回:
        - SessionProviderDefinition | None: 定义不存在时返回 None
        """
        ...

    def create_session(
        self,
        session_config: SessionConfig,
        llm: LLM | AsyncLLM | None = None,
        *,
        model_resolver: Any = None,
    ) -> Session | AsyncSession:
        """
        根据实例配置创建运行时会话

        参数:
        - session_config: 持久化的会话实例配置
        - llm: 已按同步或异步形态构建的顶层模型实例
        - model_resolver: 多工作流会话按节点模型名解析客户端的可选可调用对象

        返回:
        - Session | AsyncSession: 运行时会话实例
        """
        ...
