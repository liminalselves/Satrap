"""用户查看与强制使用技能的原生命令"""
from __future__ import annotations

from collections.abc import Callable
import asyncio
from typing import cast

from satrap.expend.plugins.skills.management import skill_management
from satrap.core.type import CommandAction
from satrap.edictum import AsyncSimpleSession, SimpleSession, PluginHost


def build_commands(session: SimpleSession | AsyncSimpleSession, config: dict[str, object],
                   *, plugin_host: PluginHost | None = None) -> tuple[dict[str, Callable[..., str | CommandAction]], dict[str, Callable[..., object]]]:
    """
    构造查看, 强制加载与管理员 URL 导入命令

    参数:
    - session: 当前会话, 状态通过宿主接口取得
    - config: 当前插件有效配置
    - plugin_host: 当前插件的宿主接口

    返回:
    - 同步与异步 skill 命令映射
    """
    if plugin_host is None:
        raise ValueError("技能命令需要有效的 plugin_host")

    def command(*args: str) -> str | CommandAction:
        """
        按用户命令读取或强制使用技能, 导入只开放管理员子命令

        参数:
        - args: 用户原生命令参数

        返回:
        - 目录, 正文, 导入回执或本轮执行动作
        """
        assert plugin_host is not None
        try:
            sub = args[0] if args else "list"
            if sub == "list":
                return "可用技能:\n" + ("\n".join(f"{name}: {intro}" for name, intro in plugin_host.skills.catalog()) or "无")
            if sub == "import" and len(args) >= 2:
                result = skill_management("POST", "", {"url": args[1], "name": args[2] if len(args) > 2 else "",
                                                        "description": " ".join(args[3:])})
                item = cast(dict[str, object], result["item"])
                return f"技能已导入, 下次任务生效: user.{item['name']}"
            if sub in {"show", "use"} and len(args) >= 2:
                name = args[1]
                candidates = [key for key, _ in plugin_host.skills.catalog() if key == name or key == f"user.{name}"]
                if len(candidates) != 1:
                    raise ValueError("技能不存在或名称不明确, 请使用 /skill list 中的精确标识")
                return plugin_host.skills.read(candidates[0]) if sub == "show" else plugin_host.skills.use(candidates[0], " ".join(args[2:]))
            return "用法: /skill list; /skill show <标识>; /skill use <标识> [任务]; 管理员: /skill import <HTTPS Markdown 地址> [名称] [简介]"
        except (OSError, ValueError, RuntimeError) as error:
            return f"技能操作失败: {error}"

    async def command_async(*args: str) -> str | CommandAction:
        """
        在线程中处理文件与下载, 保留实际调用身份

        参数:
        - args: 用户原生命令参数

        返回:
        - 与同步入口一致的操作结果
        """
        return await asyncio.to_thread(command, *args)

    for callback in (command, command_async):
        setattr(callback, "subcommands", {"list": "查看目录", "show": "查看说明", "use": "强制使用", "import": "管理员导入技能"})
    return {"skill": command}, {"skill": command_async}
