from typing import Optional, Callable, Dict, List, Tuple, Awaitable

from satrap.core.log import logger


class CommandHandler:
    """命令处理类; 负责注册, 解析和执行命令"""
    def __init__(self, output_callback: Optional[Callable[[str], None]] = None, cmd_prefix: str = "/", param_split: str = " "):
        """
        参数:
        - output_callback: 输出命令执行结果的回调函数
        - cmd_prefix: 命令前缀, 默认为 "/"
        - param_split: 参数分割符, 默认为 " "
        """
        self.output_callback = output_callback
        self.commands: Dict[str, Callable] = {}   # 命令名 -> 处理函数
        self.intros: Dict[str, str] = {}          # 命令名 -> 简介
        self.prefix = cmd_prefix
        self.pref_len = len(cmd_prefix)
        self.param_split = param_split
        self.param_split_len = len(param_split)

        self.register_help()   # 默认注册帮助命令

    def _parse(self, message: str) -> Tuple[Optional[str], Optional[List[str]]]:
        """解析消息, 提取命令和参数
        
        参数:
        - message: 输入消息
        
        返回:
        - (命令名, 参数列表) 或 (None, None) 如果不是有效命令
        """
        message = message.strip()
        if not message or message[:self.pref_len] != self.prefix:
            return None, None   # 不以指定前缀开头, 不是命令

        rest = message[self.pref_len:]
        # 去掉前缀, 得到剩余部分

        parts = rest.split(self.param_split)
        parts = [p for p in parts if p]
        # 按参数分隔符分割, 并过滤空字符串

        if not parts:
            return None, None   # 只有前缀没有命令名

        cmd = parts[0]          # 第一个非空部分为命令名
        args = parts[1:]        # 其余部分为参数列表
        return cmd, args

    def _is_cmd(self, message: str) -> bool:
        """断言消息是否是有效命令消息
        
        参数:
        - message: 输入消息
        
        返回:
        - bool: 是否是命令消息
        """
        message = message.strip()
        if not message or message[:self.pref_len] != self.prefix:
            return False   # 不以指定前缀开头, 不是命令
        
        for cmd in self.commands.keys():
            if message[self.pref_len:].startswith(cmd + self.param_split) or message[self.pref_len:] == cmd:
                return True    # 消息以某个已注册命令开头, 是命令消息

        return False

    def _execute(self, cmd: str, args: List[str]):
        """执行命令处理函数
        
        参数:
        - cmd: 命令名
        - args: 参数列表
        """
        try:
            if cmd in self.commands:
                return self.commands[cmd](*args)
            else:
                logger.warning(f"未注册命令: {cmd}")
                return None

        except Exception as e:
            logger.error(f"命令执行错误: {cmd}, {e}")
            return None
        
    def _get_registered_commands(self) -> Dict[str, str]:
        """获取已注册的命令及其简介
        
        返回:
        - Dict[命令名, 简介]
        """
        return self.intros.copy()
    
    def set_callback(self, callback: Callable[[str], None]):
        """设置输出命令执行结果的回调函数
        
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
                lines.append(f"{self.prefix}{cmd}: {intro}")
            return "\n".join(lines)

        self.register_command("help", help_cmd, intro="显示帮助信息")

    def register_command(self, name: str, handler: Callable, intro: str = "None"):
        """注册命令处理函数

        参数:
        - name: 命令名
        - handler: 处理函数
        - intro: 命令简介 (可选), 默认为 "None"
        """
        self.commands[name] = handler
        self.intros[name] = intro

    def process_message(self, message: str):
        """处理输入消息, 执行对应命令
        
        参数:
        - message: 输入消息

        返回:
        - (Any) 命令执行结果, 或 None 如果不是命令消息
        - (bool) 是否有命令
        """
        try:
            exist = self._is_cmd(message)  # 断言是否是命令消息
            if not exist:
                return None, False   # 不是命令消息, 不处理

            cmd, args = self._parse(message)
            if cmd is not None and args is not None:
                result = self._execute(cmd, args)
            
                if self.output_callback and result is not None:
                    self.output_callback(result)
                return result, True   # 命令执行成功

            else:
                logger.warning(f"无效命令: {message}")
                return None, True   # 是命令消息但无效
            
        except Exception as e:
             logger.error(f"[命令处理] 处理消息错误: {e}")
             return None, True   # 命令执行失败
 

class AsyncCommandHandler:
    """异步命令处理类; 负责注册, 解析和执行命令 (仅支持异步处理函数)"""
    def __init__(self, output_callback: Optional[Callable[[str], Awaitable[None]]] = None, cmd_prefix: str = "/", param_split: str = " "):
        """
        参数:
        - output_callback: 异步输出命令执行结果的回调函数
        - cmd_prefix: 命令前缀, 默认为 "/"
        - param_split: 参数分割符, 默认为 " "
        """
        self.output_callback = output_callback
        self.commands: Dict[str, Callable] = {}   # 命令名 -> 异步处理函数
        self.intros: Dict[str, str] = {}          # 命令名 -> 简介
        self.prefix = cmd_prefix
        self.pref_len = len(cmd_prefix)
        self.param_split = param_split
        self.param_split_len = len(param_split)

        self.register_help()   # 默认注册帮助命令

    def _parse(self, message: str) -> Tuple[Optional[str], Optional[List[str]]]:
        """解析消息, 提取命令和参数
        
        参数:
        - message: 输入消息
        
        返回:
        - (命令名, 参数列表) 或 (None, None) 如果不是有效命令
        """
        message = message.strip()
        if not message or message[:self.pref_len] != self.prefix:
            return None, None   # 不以指定前缀开头, 不是命令

        rest = message[self.pref_len:]
        # 去掉前缀, 得到剩余部分

        parts = rest.split(self.param_split)
        parts = [p for p in parts if p]
        # 按参数分隔符分割, 并过滤空字符串

        if not parts:
            return None, None   # 只有前缀没有命令名

        cmd = parts[0]          # 第一个非空部分为命令名
        args = parts[1:]        # 其余部分为参数列表
        return cmd, args

    def _is_cmd(self, message: str) -> bool:
        """断言消息是否是有效命令消息
        
        参数:
        - message: 输入消息
        
        返回:
        - bool: 是否是命令消息
        """
        message = message.strip()
        if not message or message[:self.pref_len] != self.prefix:
            return False   # 不以指定前缀开头, 不是命令
        
        for cmd in self.commands.keys():
            if message[self.pref_len:].startswith(cmd + self.param_split) or message[self.pref_len:] == cmd:
                return True    # 消息以某个已注册命令开头, 是命令消息

        return False

    async def _execute(self, cmd: str, args: List[str]):
        """异步执行命令处理函数
        
        参数:
        - cmd: 命令名
        - args: 参数列表
        """
        try:
            if cmd in self.commands:
                handler = self.commands[cmd]
                # 直接异步调用, handler 必须为异步函数
                return await handler(*args)
            else:
                logger.warning(f"未注册命令: {cmd}")
                return None

        except Exception as e:
            logger.error(f"命令执行错误: {cmd}, {e}")
            return None
        
    def _get_registered_commands(self) -> Dict[str, str]:
        """获取已注册的命令及其简介
        
        返回:
        - Dict[命令名, 简介]
        """
        return self.intros.copy()
     
    def register_help(self):
        """注册帮助命令, 输出已注册命令列表及简介"""
        async def help_cmd():
            intros = self._get_registered_commands()
            if not intros:
                return "没有已注册的命令"

            lines = ["已注册的命令:"]
            for cmd, intro in intros.items():
                lines.append(f"{self.prefix}{cmd}: {intro}")
            return "\n".join(lines)

        self.register_command("help", help_cmd, intro="显示帮助信息")

    def register_command(self, name: str, handler: Callable, intro: str = "None"):
        """注册命令处理函数

        参数:
        - name: 命令名
        - handler: 处理函数 (可以是同步或异步函数)
        - intro: 命令简介 (可选), 默认为 "None"
        """
        self.commands[name] = handler
        self.intros[name] = intro

    async def process_message(self, message: str):
        """异步处理输入消息, 执行对应命令
        
        参数:
        - message: 输入消息
        """
        try:
            exist = self._is_cmd(message)  # 断言是否是命令消息
            if not exist:
                return None   # 不是命令消息, 不处理

            cmd, args = self._parse(message)
            if cmd is not None and args is not None:
                result = await self._execute(cmd, args)
            
                if self.output_callback and result is not None:
                    await self.output_callback(result)
                return result

            else:
                logger.warning(f"无效命令: {message}")
                return None
                
        except Exception as e:
            logger.error(f"[命令处理] 处理消息错误: {e}")
            return None

