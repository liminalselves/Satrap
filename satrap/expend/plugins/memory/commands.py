"""
长期记忆插件的同步与异步命令入口

通过 state.py 与记忆工具和注入处理器共享 MemoryService 及 MemoryStore,
群聊操作在来源平台循环执行, 同步入口通过工作线程桥接, 异步入口直接等待,
命令结果转换为可读文字, 保存, 审批和失败状态保持与宿主结果一致
"""
from __future__ import annotations

from typing import Any, Callable, cast

import asyncio

from satrap.expend.plugins.memory.state import get_plugin_state
from satrap.core.memory.service import MemoryService
from satrap.expend.plugins.memory.runtime import active_config, await_host, group_call, wait_host
from satrap.core.call_context import current_call_origin
from satrap.edictum import AsyncSimpleSession, SimpleSession

_MEMORY_MODES = ("disabled", "base", "full")


def _parse_args(args: list[str], default: str = "") -> str:
    """
    命令参数列表 -> 单字符串 (保留空格)

    参数:
    - args: 额外位置参数
    - default: 默认值

    返回:
    - str: 命令参数列表 -> 单字符串 (保留空格)
    """
    if not args:
        return default
    return " ".join(str(a) for a in args).strip()


SessionType = SimpleSession | AsyncSimpleSession
"""插件支持的会话类型"""


def _format_group_result(result: dict[str, Any], operation: str) -> str:
    """
    将群记忆宿主结果转换为可读文字, 保留保存和审批状态的区别

    参数:
    - result: MemoryService 返回的结构化结果, 包含查询记录或变更状态及失败原因
    - operation: 已分派的宿主操作, 限定为 list, create 或 delete

    返回:
    - 查询列表或空列表提示, 变更确认, 同名记录提示或待审批提示; 失败时返回操作名称及原因
    """
    # Step.1 保留宿主失败原因, 避免将拒绝结果展示为成功
    action = {"list": "查询", "create": "添加", "delete": "删除"}[operation]
    if not result.get("ok"):
        return f"{action}失败: {result.get('error', '记忆操作暂不可用')}"
    # Step.2 展示当前可见记录, 只统计当前页并提示未展示的剩余记录
    if operation == "list":
        items = result["items"]
        if not items:
            return "当前没有可查看的长期记忆"
        lines = [f"当前显示 {len(items)} 条可查看的长期记忆:"]
        for item in items:
            kind = "群规则" if item["kind"] == "group_rule" else "我的偏好"
            lines.append(f"- {item['id']} [{item['title']}] {item['content']} ({kind})")
        if result.get("has_more"):
            lines.append("还有更多记忆, 请在管理界面查看")
        return "\n".join(lines)
    # Step.3 区分已应用的变更, 已有记录和仍待审批的申请
    if result.get("status") == "pending":
        return f"{action}申请已提交, 等待管理员审批"
    if operation == "delete":
        return f"记忆已删除: {result['memory_id']}"
    memory = result["memory"]
    status = "同名记忆已存在" if result.get("status") == "already_exists" else "记忆已添加"
    return f"{status}: [{memory['title']}] {memory['content']}\n记忆 ID: {memory['id']}"


async def _cmd_group_memory_impl(state: dict[str, Any], args: list[str]) -> str:
    """
    异步执行群记忆命令, 为同步桥接与异步入口共享参数解析和结果格式

    参数:
    - state: 当前会话的插件共享状态, 包含 MemoryService 和安装配置快照
    - args: 命令参数列表, 首项为子命令; 空列表默认查询当前群的可见记忆

    返回:
    - 查询或变更结果文字; 参数无效时返回用法, 权限限制或宿主失败时返回明确的拒绝原因
    """
    # Step.1 绑定本轮可信来源, 在宿主等待前后复核当前插件状态
    service = state["service"]
    assert isinstance(service, MemoryService)
    origin = current_call_origin()
    assert origin is not None
    sub = args[0] if args else "list"
    access = lambda: active_config(service.session, state["config"], "commands", "memory")
    # Step.2 根据子命令调用群记忆宿主, 仅允许保存本人的偏好
    if sub in {"mode", "模式", "clear", "清空"}:
        return "群内不能通过命令更改记忆权限或批量删除; 请在管理界面操作, 或明确删除自己的某条偏好"
    if sub in {"list", "查看"}:
        operation = "list"
        result = await service.group_operation("list", {}, access=access, principal="command")
    elif sub in {"add", "添加"}:
        rest = _parse_args(args[1:]).split(" ", 1)
        if len(rest) != 2:
            return "用法: /memory add <标题> <内容>; 群内命令只保存本人的偏好"
        operation = "create"
        result = await service.group_operation("create", {"kind": "member_preference", "key": rest[0], "title": rest[0], "content": rest[1],
                                                        "source_message_ids": [origin.source_message_id]}, access=access, principal="command")
    elif sub in {"del", "delete", "删除"} and len(args) == 2:
        detail = await service.group_operation("get", {"memory_id": args[1]}, access=access, principal="command")
        # 先读取当前修订号, 避免删除读取后已被他人修改的记录
        if not detail.get("ok"):
            return f"删除失败: {detail.get('error', '读取失败')}"
        operation = "delete"
        result = await service.group_operation("delete", {"memory_id": args[1], "expected_revision": detail["memory"]["revision"],
                                                        "request_message_id": origin.source_message_id}, access=access, principal="command")
    else:
        return "用法: /memory list | add <标题> <内容> | del <完整记忆 ID>"
    # Step.3 转换宿主结果, 不向命令使用者暴露内部结构化字段
    return _format_group_result(result, operation)


def _cmd_memory_impl(state: dict[str, Any], args: list[str]) -> str:
    """
    同步执行记忆命令, 群聊通过工作线程桥接, 普通会话直接访问绑定存储

    参数:
    - state: 当前会话的插件共享状态, 包含 MemoryService 和安装配置快照
    - args: 命令参数列表, 首项为子命令; 空列表默认查询当前范围的记忆

    返回:
    - 查询或变更结果文字; 参数无效时返回用法, 权限限制, 桥接失败或存储失败时返回原因
    """
    # Step.1 按可信来源区分群范围与会话范围, 群聊提交到原平台循环
    store = state["service"]
    assert isinstance(store, MemoryService)
    sub = args[0] if args else "list"
    if group_call():
        result = wait_host(_cmd_group_memory_impl(state, args))
        return result if isinstance(result, str) else str(result.get("error", "记忆操作失败"))
    # Step.2 在普通会话的绑定范围执行操作, 保留存储的模式限制与失败结果
    if sub in ("list", "查看"):
        memories = store.execute("list_all")
        if isinstance(memories, dict):
            return str(memories.get("error", "读取失败"))
        if not isinstance(memories, list):
            return "读取失败"
        rows = cast(list[dict[str, Any]], memories)
        if not rows:
            return "当前没有长期记忆"
        lines = [f"共 {len(rows)} 条记忆:"]
        for m in rows:
            tags = f" [{', '.join(m['tags'])}]" if m["tags"] else ""
            lines.append(f"- {m['id']} [{m['title']}] {m['content']}{tags} (重要度 {m['importance']})")
        return "\n".join(lines)
    if sub in ("add", "添加"):
        rest = _parse_args(args[1:])
        if not rest:
            return "用法: /memory add <标题> <内容>"
        parts = rest.split(" ", 1)
        result = store.execute("add", parts[0], parts[1] if len(parts) > 1 else "")
        if not isinstance(result, dict):
            return "添加失败: 返回格式异常"
        added = cast(dict[str, Any], result)
        return f"记忆已添加: [{added['title']}] {added['content']}" if added.get("ok") else f"添加失败: {added.get('error')}"
    if sub in ("del", "delete", "删除"):
        if len(args) < 2:
            return "用法: /memory del <记忆 ID>"
        result = store.execute("delete", args[1])
        if not isinstance(result, dict):
            return "删除失败: 返回格式异常"
        deleted = cast(dict[str, Any], result)
        return f"记忆已删除: {args[1]}" if deleted.get("ok") else f"删除失败: {deleted.get('error')}"
    if sub in ("clear", "清空"):
        if not store.store.can_write():
            return store.store.write_denied_reason("清空")
        result = store.execute("clear")
        return str(result.get("error")) if isinstance(result, dict) else f"已清空 {result} 条记忆"
    if sub in ("mode", "模式"):
        if len(args) < 2 or args[1] not in _MEMORY_MODES:
            return f"用法: /memory mode <{'|'.join(_MEMORY_MODES)}>"
        result = store.execute("set_mode", args[1])
        if isinstance(result, dict):
            return str(result.get("error"))
        return f"记忆模式已切换: {args[1]}"
    return "用法: /memory list | add <标题> <内容> | del <ID> | clear | mode <disabled|base|full>"


def build_commands(session: SessionType, config: dict[str, Any] | None = None) -> tuple[dict[str, Callable[..., Any]], dict[str, Callable[..., Any]]]:
    """
    构建共享记忆状态的同步与异步命令映射

    参数:
    - session: 安装插件的主会话, 用于绑定记忆范围及复核插件状态
    - config: 已解析的插件配置, 可选, 默认为 None; 未提供时使用已有共享状态或插件默认配置

    返回:
    - 按同步, 异步顺序返回命令映射, 两者均包含 memory 且共享当前会话的记忆状态
    """
    # Step.1 绑定当前会话的共享状态, 保持工具, 注入处理器和命令的记忆范围一致
    state = get_plugin_state(session, config)

    # Step.2 构建同步与异步入口, 按各自的执行环境调用同一份命令逻辑
    def cmd_memory(*args: str) -> str:
        """
        管理长期记忆: list/add/del/clear/mode; 群聊支持 list/add/del

        同步入口从工作线程桥接群聊操作, 普通会话直接访问绑定存储

        参数:
        - args: 子命令及其参数, 不提供时默认查询当前范围的记忆

        返回:
        - 查询或变更结果文字; 参数无效时返回用法, 权限限制, 桥接失败或存储失败时返回原因
        """
        return _cmd_memory_impl(state, list(args))

    async def cmd_memory_async(*args: str) -> str:
        """
        管理长期记忆: list/add/del/clear/mode; 群聊支持 list/add/del

        异步入口直接等待群聊宿主, 普通会话的同步存储操作交给工作线程

        参数:
        - args: 子命令及其参数, 不提供时默认查询当前范围的记忆

        返回:
        - 查询或变更结果文字; 参数无效时返回用法, 权限限制, 等待超时或存储失败时返回原因
        """
        # Step.1 直接等待群聊宿主, 避免在来源循环内同步等待同一循环
        if group_call():
            result = await await_host(_cmd_group_memory_impl(state, list(args)))
            return result if isinstance(result, str) else str(result.get("error", "记忆操作失败"))
        # Step.2 在线程中访问普通会话存储, 保留本轮来源与命令权限上下文
        return await asyncio.to_thread(_cmd_memory_impl, state, list(args))

    # Step.3 按会话类型导出命令映射, 由插件安装流程选择对应入口
    sync_map: dict[str, Callable[..., Any]] = {
        "memory": cmd_memory,
    }
    async_map: dict[str, Callable[..., Any]] = {
        "memory": cmd_memory_async,
    }
    return sync_map, async_map
