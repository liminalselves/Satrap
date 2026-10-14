"""明确用户命令转发原消息, 命令授权不由模型工具参数提供"""
from __future__ import annotations

from typing import Any
import json
import traceback

from satrap.core.framework.Base import Session, AsyncSession
from satrap.core.log import logger
from satrap.core.platform.loop_bridge import run_on_platform_loop
from satrap.core.plugin_authorization import require_plugin_entry_permission
from satrap.core.message_forward import ForwardError
from satrap.expend.plugins.message_forward.tools import resolve

USAGE = "用法: /forward read <消息 ID> | merge <group/private> <目标 ID> <消息 ID...> | relay <group/private> <目标 ID> <卡片消息 ID>"


def build_commands(session: Session | AsyncSession, config: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """
    通过真实命令注册表复核入口, 不借用模型工具授权

    参数:
    - session: 当前同步或异步会话
    - config: 已合成的插件配置, None 使用空配置

    返回:
    - 同步与异步命令映射, 仅填充与会话类型匹配的一侧
    """
    settings = dict(config or {})

    async def execute(args: tuple[str, ...]) -> str:
        """
        解析明确转发命令并捕获平台失败

        参数:
        - args: 命令注册表解析的参数

        返回:
        - 使用说明, 平台回执或失败原因
        """
        try:
            if not args or args[0] not in {"read", "merge", "relay"}:
                return USAGE
            subcommand = args[0]
            if subcommand == "read":
                if len(args) != 2:
                    return USAGE
                params = {"source_message_id": args[1]}
                operation = "read"
            else:
                if len(args) < 4 or subcommand == "relay" and len(args) != 4:
                    return USAGE
                params = {"message_ids": list(args[3:]), "mode": "merge" if subcommand == "merge" else "existing_forward",
                          "target": {"conversation_kind": args[1], "conversation_id": args[2]}}
                operation = "send"
            def check() -> None:
                """按真实命令入口复核子命令权限与发送开关"""
                registry = getattr(session, "command_handler", None) or getattr(session, "cmd_handler", None)
                binding = getattr(registry, "_permission_bindings", {}).get("forward")
                if binding is None:
                    raise ForwardError("permission_denied", "命令没有有效的原生授权入口")
                require_plugin_entry_permission(binding, subcommand=subcommand, refresh=True)
                if operation == "send" and settings.get("send_enabled") is not True:
                    raise ForwardError("permission_denied", "转发发送功能尚未开启")
            adapter, origin = resolve(settings, check, params, command=True)
            def preflight() -> None:
                """等待后复核来源账号与命令权限"""
                current, current_origin = resolve(settings, check, params, command=True)
                if current is not adapter or current_origin != origin:
                    raise ForwardError("stale_account", "转发来源已变化")
            data = await adapter.message_forward(operation, origin, params, preflight)
            return json.dumps(data, ensure_ascii=False)
        except (ForwardError, PermissionError, ValueError) as error:
            logger.warning(f"[message_forward] 命令失败, 原因={type(error).__name__}")
            return str(error)
        except Exception:
            logger.error(f"[message_forward] 命令执行异常: {traceback.format_exc()}")
            return "转发结果无法确认, 请查看实际消息, 不要重复发送"

    if isinstance(session, AsyncSession):
        async def async_command(*args: str) -> str:
            """
            异步查看或按明确目标转发原消息

            参数:
            - args: 命令参数

            返回:
            - 使用说明, 平台回执或失败原因
            """
            return await execute(args)
        setattr(async_command, "subcommands", {"read": "查看转发内容", "merge": "合并原消息", "relay": "转发已有卡片"})
        return {}, {"forward": async_command}

    def command(*args: str) -> str:
        """
        在来源平台循环同步执行明确转发命令

        参数:
        - args: 命令参数

        返回:
        - 使用说明, 平台回执或失败原因
        """
        try:
            adapter, _ = resolve(settings, lambda: None, {}, command=True)
            return run_on_platform_loop(execute(args), adapter._loop, 40)
        except Exception:
            logger.error(f"[message_forward] 同步命令失败: {traceback.format_exc()}")
            return "转发结果未确认, 请核查实际消息"
    setattr(command, "subcommands", {"read": "查看转发内容", "merge": "合并原消息", "relay": "转发已有卡片"})
    return {"forward": command}, {}
