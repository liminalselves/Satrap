"""插件宿主接口, 专用服务及交互错误的公共导出"""

from satrap.core.plugin_interop.capabilities import PluginHost
from satrap.core.plugin_interop.contracts import ImageArtifact, PluginInteractionError
from satrap.core.plugin_interop.service import AsyncService, Service

__all__ = ["ImageArtifact", "PluginHost", "PluginInteractionError", "Service", "AsyncService"]
