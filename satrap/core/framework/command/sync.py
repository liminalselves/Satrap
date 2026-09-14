"""
同步文本命令注册与分派

按命令前缀和参数分隔符解析输入, 调用已注册的处理函数并输出结果
"""

from typing import (
    Any,
    Optional,
    Callable,
    Dict,
    List,
)

from .base import _CommandRegistry

from satrap.core.log import logger


class CommandHandler(_CommandRegistry):
    """命令处理类; 负责注册, 解析和执行命令"""

    def __init__(
        self,
        output_callback: Optional[Callable[[str], None]] = None,
        cmd_prefix: str = "/",
        param_split: str = " ",
    ):
        """
        参数:
        - output_callback: 输出命令执行结果的回调函数
        - cmd_prefix: 命令前缀, 默认为 "/"
        - param_split: 参数分割符, 默认为 " "
        """
        self.output_callback = output_callback
        self.commands: Dict[str, Any] = {}  # 命令名 -> 处理函数
        self.intros: Dict[str, str] = {}  # 命令名 -> 简介
        self.prefix = cmd_prefix
        self.pref_len = len(cmd_prefix)
        self.param_split = param_split
        self._disabled_commands: set[str] = set()

        self.register_help()  # 默认注册帮助命令

    def _execute(self, cmd: str, args: List[str]):
        """
        执行命令处理函数

        参数:
        - cmd: 命令名
        - args: 参数列表

        返回:
        - 执行命令处理函数
        """
        try:
            if cmd in self.commands and cmd not in self._disabled_commands:
                return self.commands[cmd](*args)
            else:
                logger.warning(f"未注册命令: {cmd}")
                return None

        except Exception as e:
            logger.error(f"命令执行错误: {cmd}, {e}")
            return None

    def set_callback(self, callback: Callable[[str], None]):
        """
        设置输出命令执行结果的回调函数

        参数:
        - callback: 输出回调函数, 接受一个字符串参数
        """
        self.output_callback = callback

    def register_help(self):
        """注册帮助命令, 输出已注册命令列表及简介"""

        def help_cmd():
            intros = self._get_registered_commands()
            if not intros:
                return "没有已注册的命令"

            lines = ["已注册的命令:"]
            for cmd, intro in intros.items():
                lines.append(f"{self.prefix}{cmd}: {intro}\n")
            return "\n".join(lines)

        self.register_command("help", help_cmd, intro="显示帮助信息")

    def process_message(self, message: str) -> tuple[Any, bool]:
        """
        处理输入消息, 执行对应命令

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
                result = self._execute(cmd, args)

                if self.output_callback and result is not None:
                    self.output_callback(result)
                return result, True  # 命令执行成功

            else:
                logger.warning(f"无效命令: {message}")
                return None, True  # 是命令消息但无效

        except Exception as e:
            logger.error(f"[命令处理] 处理消息错误: {e}")
            return None, True  # 命令执行失败
