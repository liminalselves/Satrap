"""插件兼容性数据与异常。"""
from __future__ import annotations
from dataclasses import dataclass

@dataclass(frozen=True)
class PluginEnvironment:
    """由宿主提供的插件运行环境"""

    session_type: str = "embedded"
    """宿主会话类型, 可为 chat, platform 或 embedded, 默认 embedded"""
    platform_type: str | None = None
    """平台适配器类型, 仅 platform 会话必须提供, 其他会话为 None"""

    def __post_init__(self) -> None:
        if self.session_type not in {"chat", "platform", "embedded"}:
            raise ValueError("未知的插件会话类型")
        if self.session_type == "platform" and not self.platform_type:
            raise ValueError("平台会话必须指定适配器类型")
        if self.session_type != "platform" and self.platform_type is not None:
            raise ValueError("仅平台会话可以指定适配器类型")


@dataclass(frozen=True)
class CompatibilityResult:
    """可供安装接口和界面共同使用的判定结果"""

    allowed: bool
    """插件是否通过当前环境和框架版本的兼容性检查"""
    reason_code: str = ""
    """机器可读的拒绝原因, 默认空串表示没有拒绝原因"""
    message: str = ""
    """供调用方展示的兼容性说明, 默认空串"""
    warnings: tuple[str, ...] = ()
    """不阻止安装的提示, 默认空元组表示没有警告"""

    def require(self) -> None:
        if not self.allowed:
            raise PluginCompatibilityError(self)


class PluginCompatibilityError(ValueError):
    """包含机器可读原因的兼容性错误"""

    def __init__(self, result: CompatibilityResult):
        self.result = result
        super().__init__(result.message)
