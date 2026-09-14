"""插件兼容性与会话适用范围, 在导入插件代码前判定"""
from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import Any, cast

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version


@dataclass(frozen=True)
class PluginEnvironment:
    """由宿主提供的插件运行环境"""

    session_type: str = "embedded"
    platform_type: str | None = None

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
    reason_code: str = ""
    message: str = ""
    warnings: tuple[str, ...] = ()

    def require(self) -> None:
        if not self.allowed:
            raise PluginCompatibilityError(self)


class PluginCompatibilityError(ValueError):
    """包含机器可读原因的兼容性错误"""

    def __init__(self, result: CompatibilityResult):
        self.result = result
        super().__init__(result.message)


def framework_version() -> str:
    """读取安装版本, 源码运行时使用项目当前版本"""
    try:
        return version("satrap")
    except PackageNotFoundError:
        return "0.1.0"


def parse_compatibility(meta: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """校验声明格式, 缺省字段保留旧插件行为"""
    compatibility = meta.get("compatibility", {})
    applicability = meta.get("applicability", {})
    if not isinstance(compatibility, dict) or not isinstance(applicability, dict):
        raise ValueError("compatibility 和 applicability 必须是对象")
    compatibility = cast(dict[str, Any], compatibility)
    applicability = cast(dict[str, Any], applicability)
    if set(compatibility) - {"satrap"}:
        raise ValueError("compatibility 包含未知字段")
    if set(applicability) - {"session_types", "platforms"}:
        raise ValueError("applicability 包含未知字段")
    if "satrap" in compatibility:
        constraint = compatibility["satrap"]
        if not isinstance(constraint, str) or not constraint.strip():
            raise ValueError("satrap 版本范围必须是非空字符串")
        SpecifierSet(constraint)
    for key in ("session_types", "platforms"):
        if key not in applicability:
            continue
        values = applicability[key]
        if key == "platforms" and values == "*":
            continue
        if not isinstance(values, list) or not values or any(
            not isinstance(item, str) or not item.strip() or item != item.strip()
            for item in cast(list[Any], values)
        ):
            raise ValueError(f"{key} 必须是非空字符串列表")
        if key == "session_types" and set(cast(list[str], values)) - {"chat", "platform", "embedded"}:
            raise ValueError("session_types 包含未知会话类型")
        if "*" in values:
            raise ValueError("通配平台必须写为 platforms: '*'")
    return dict(compatibility), dict(applicability)


def check_plugin_compatibility(
    meta: dict[str, Any],
    environment: PluginEnvironment,
    satrap_version: str | None = None,
) -> CompatibilityResult:
    """统一判定框架版本和运行环境, 不执行插件代码"""
    try:
        compatibility, applicability = parse_compatibility(meta)
        current = Version(satrap_version or framework_version())
    except (ValueError, InvalidSpecifier, InvalidVersion) as exc:
        return CompatibilityResult(False, "invalid_manifest", str(exc))
    constraint = compatibility.get("satrap")
    if constraint is not None and current not in SpecifierSet(constraint):
        return CompatibilityResult(False, "framework_version_mismatch", f"插件要求 Satrap {constraint}, 当前版本为 {current}")
    session_types = applicability.get("session_types")
    if session_types is not None and environment.session_type not in session_types:
        return CompatibilityResult(False, "session_type_not_supported", f"插件不适用于 {environment.session_type} 会话")
    platforms = applicability.get("platforms", "*")
    if environment.session_type == "platform" and platforms != "*" and environment.platform_type not in platforms:
        return CompatibilityResult(False, "platform_not_supported", f"插件不支持平台 {environment.platform_type}")
    warnings = () if constraint and session_types is not None else ("插件未完整声明兼容版本与适用会话类型",)
    return CompatibilityResult(True, warnings=warnings)
