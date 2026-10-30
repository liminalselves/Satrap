"""
对象级高级诊断的用户原生命令

仅通过宿主接口改变持久设置, 不向模型注册工具或插件服务
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable

from satrap.edictum import AsyncSimpleSession, PluginHost, SimpleSession


def build_commands(
    session: SimpleSession | AsyncSimpleSession, config: dict[str, object] | None = None,
    *, plugin_host: PluginHost | None = None,
) -> tuple[dict[str, Callable[..., str]], dict[str, Callable[..., object]]]:
    """
    构建同步与异步原生命令, 宿主核验权限和真实对象

    参数:
    - session: 当前安装会话
    - config: 插件配置, 无独立对象设置
    - plugin_host: 安装器提供的宿主入口

    返回:
    - debug 命令映射, 缺少宿主时拒绝构造
    """
    if plugin_host is None:
        raise ValueError("Debug 命令需要有效的 plugin_host")

    def command(*args: str) -> str:
        """
        查询或修改当前对象和整体默认

        参数:
        - args: 原生命令参数, 不接受任意目标对象

        返回:
        - 当前状态, 明确回执或权限错误
        """
        assert plugin_host is not None
        try:
            access = plugin_host.debug_logging
            if not args or args == ("status",):
                status = access.status()
                return f"当前对象 Debug: {status['mode']}; 生效: {'开启' if status['enabled'] else '关闭'}; revision={status['revision']}"
            if len(args) == 1 and args[0] in {"on", "off", "reset"}:
                mode = {"on": "on", "off": "off", "reset": "inherit"}[args[0]]
                from satrap.core.log.debug.contracts import DebugMode
                from typing import cast

                result = access.set_mode(cast(DebugMode, mode))
                label = {"on": "已开启", "off": "已关闭", "inherit": "已恢复继承默认"}[mode]
                return f"当前对象高级 Debug {label} ({mode}), 从下一请求生效; 设置绑定对象, /new 后继续生效"
            if len(args) == 2 and args[0] == "default" and args[1] in {"status", "on", "off"}:
                result = access.default_status() if args[1] == "status" else access.set_default(args[1] == "on")
                return f"整体 Debug 默认: {'开启' if result['default_enabled'] else '关闭'}; 仅影响继承默认的对象"
            return "用法: /debug on|off|reset|status; /debug default on|off|status"
        except (OSError, ValueError, PermissionError, TimeoutError) as error:
            return f"高级 Debug 设置失败: {error}"

    async def command_async(*args: str) -> str:
        """
        在线程中完成设置文件操作, 不阻塞事件循环

        参数:
        - args: 原生命令参数

        返回:
        - 与同步命令相同的管理结果
        """
        return await asyncio.to_thread(command, *args)

    setattr(command, "requires_native_scope", True)
    setattr(command_async, "requires_native_scope", True)
    return {"debug": command}, {"debug": command_async}
