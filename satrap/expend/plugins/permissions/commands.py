"""
宿主审批模式的用户命令

仅注册可信原生命令, 不导出给模型或其他插件;
状态归宿主持有, 卸载本插件不会改变执行策略
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from satrap.core.approval.contracts import APPROVAL_MODES
from satrap.core.approval.state import validate_approval_root
from satrap.edictum import AsyncSimpleSession, SimpleSession, PluginHost

from satrap.core.log import logger


def build_commands(
    session: SimpleSession | AsyncSimpleSession, config: dict[str, object] | None = None,
    *, plugin_host: PluginHost | None = None,
) -> tuple[dict[str, Callable[..., str]], dict[str, Callable[..., object]]]:
    """
    配置宿主审批并构造同步与异步原生命令

    参数:
    - session: 当前宿主会话
    - config: 权限插件生效配置, 默认使用会话目录与当前模型
    - plugin_host: 安装器注入的宿主接口, 缺失时拒绝构造管理命令

    返回:
    - 同步与异步 approve 命令映射, 无模型可调用入口
    """
    if plugin_host is None:
        raise ValueError("权限命令需要有效的 plugin_host")
    values = config or {}
    data_root = str(values.get("data_root") or "").strip()
    validate_approval_root(session, Path(data_root) if data_root else None)

    def command(*args: str) -> str:
        """
        同步处理审批状态查询与模式修改

        参数:
        - args: 用户原生命令参数, 不接受模型转发

        返回:
        - 状态, 切换回执或明确错误; 实际管理授权由宿主复核
        """
        assert plugin_host is not None
        try:
            state = plugin_host.approvals.snapshot()
            sub = args[0] if args else "mode"
            if sub == "status":
                notice = state["migration_notice"]
                return f"当前审批模式: {state['mode']}; 计划模式: {state['plan_mode']}" + (f"\n{notice}" if notice else "")
            if sub in ("mode", "策略") and len(args) == 2 and args[1] in APPROVAL_MODES:
                plugin_host.approvals.set_mode(args[1])
                return f"审批策略已切换: {args[1]}"
            if sub in ("rules", "规则", "rule", "添加规则"):
                return "操作类别级放行规则已移除, 请使用 /approve mode user|auto|full-access"
            return f"用法: /approve mode <{'|'.join(APPROVAL_MODES)}> | status; 当前策略: {state['mode']}"
        except (OSError, ValueError, PermissionError) as error:
            logger.warning(f"[permissions] 审批命令失败: {error}")
            return f"权限或配置错误: {error}"

    async def command_async(*args: str) -> str:
        """
        异步处理审批状态查询与模式修改

        参数:
        - args: 用户原生命令参数

        返回:
        - 与同步入口相同的状态或管理结果
        """
        return command(*args)

    setattr(command, "requires_native_scope", True)
    setattr(command_async, "requires_native_scope", True)
    return {"approve": command}, {"approve": command_async}
