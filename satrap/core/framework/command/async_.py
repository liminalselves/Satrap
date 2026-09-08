from typing import Any, Optional, Callable, Dict, List, Awaitable
from satrap.core.log import logger

from .base import _CommandRegistry


class AsyncCommandHandler(_CommandRegistry):
    """异步命令处理类; 负责注册, 解析和执行命令 (仅支持异步处理函数)"""

    def __init__(
        self,
        output_callback: Optional[Callable[[str], Awaitable[None]]] = None,
        cmd_prefix: str = "/",
        param_split: str = " ",
    ):
        """
        参数:
        - output_callback: 异步输出命令执行结果的回调函数
        - cmd_prefix: 命令前缀, 默认为 "/"
        - param_split: 参数分割符, 默认为 " "
        """
        self.output_callback = output_callback
        self.commands: Dict[str, Any] = {}  # 命令名 -> 异步处理函数
        self.intros: Dict[str, str] = {}  # 命令名 -> 简介
        self.prefix = cmd_prefix
        self.pref_len = len(cmd_prefix)
        self.param_split = param_split
        self.param_split_len = len(param_split)
        self._disabled_commands: set[str] = set()

        self.register_help()  # 默认注册帮助命令

    async def _execute(self, cmd: str, args: List[str]) -> Any:
        """
        异步执行命令处理函数

        参数:
        - cmd: 命令名
        - args: 参数列表

        返回:
        - Any: 异步执行命令处理函数
        """
        try:
            if cmd in self.commands and cmd not in self._disabled_commands:
                handler = self.commands[cmd]
                # 直接异步调用, handler 必须为异步函数
                return await handler(*args)
            else:
                logger.warning(f"未注册命令: {cmd}")
                return None

        except Exception as e:
            logger.error(f"命令执行错误: {cmd}, {e}")
            return None

    def register_help(self):
        """注册帮助命令, 输出已注册命令列表及简介"""

        async def help_cmd():
            intros = self._get_registered_commands()
            if not intros:
                return "没有已注册的命令"

            lines = ["已注册的命令:"]
            for cmd, intro in intros.items():
                lines.append(f"{self.prefix}{cmd}: {intro}\n")
            return "\n".join(lines)

        self.register_command("help", help_cmd, intro="显示帮助信息")

    async def process_message(self, message: str) -> tuple[Any, bool]:
        """
        异步处理输入消息, 执行对应命令

        参数:
        - message: 输入消息

        返回:
        - (Any) 命令执行结果, 或 None 如果不是命令消息
        - (bool) 是否有命令
        """
        try:
            exist = self._is_cmd(message)  # 断言是否是命令消息
            if not exist:
                return None, False  # 不是命令消息, 不处理

            cmd, args = self._parse(message)
            if cmd is not None and args is not None:
                result = await self._execute(cmd, args)

                if self.output_callback and result is not None:
                    await self.output_callback(result)
                return result, True  # 命令消息, 处理成功

            else:
                logger.warning(f"无效命令: {message}")
                return None, True

        except Exception as e:
            logger.error(f"[命令处理] 处理消息错误: {e}")
            return None, True
