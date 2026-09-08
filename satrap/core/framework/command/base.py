from typing import Any, Optional, Dict, List, Tuple


class _CommandRegistry:
    commands: Dict[str, Any]
    intros: Dict[str, str]
    prefix: str
    pref_len: int
    param_split: str
    _disabled_commands: set[str]

    def _parse(self, message: str) -> Tuple[Optional[str], Optional[List[str]]]:
        """
        解析消息, 提取命令和参数

        参数:
        - message: 输入消息

        返回:
        - (命令名, 参数列表) 或 (None, None) 如果不是有效命令
        """
        message = message.strip()
        if not message or message[: self.pref_len] != self.prefix:
            return None, None  # 不以指定前缀开头, 不是命令

        rest = message[self.pref_len :]
        # 去掉前缀, 得到剩余部分

        parts = rest.split(self.param_split)
        parts = [p for p in parts if p]
        # 按参数分隔符分割, 并过滤空字符串

        if not parts:
            return None, None  # 只有前缀没有命令名

        cmd = parts[0]  # 第一个非空部分为命令名
        args = parts[1:]  # 其余部分为参数列表
        return cmd, args

    def _is_cmd(self, message: str) -> bool:
        """
        断言消息是否是有效命令消息

        参数:
        - message: 输入消息

        返回:
        - bool: 是否是命令消息
        """
        message = message.strip()
        if not message or message[: self.pref_len] != self.prefix:
            return False  # 不以指定前缀开头, 不是命令

        for cmd in self.commands.keys():
            if cmd in self._disabled_commands:
                continue
            if (
                message[self.pref_len :].startswith(cmd + self.param_split)
                or message[self.pref_len :] == cmd
            ):
                return True  # 消息以某个已注册命令开头, 是命令消息

        return False

    def _get_registered_commands(self) -> Dict[str, str]:
        """
        获取已注册的命令及其简介

        返回:
        - Dict[命令名, 简介]
        """
        return self.intros.copy()

    def register_command(self, name: str, handler: Any, intro: str = "None"):
        """
        注册命令处理函数

        参数:
        - name: 命令名
        - handler: 处理函数
        - intro: 命令简介 (可选), 默认为 "None"
        """
        self.commands[name] = handler
        self.intros[name] = intro

    def unregister_command(self, name: str) -> bool:
        """
        注销命令

        参数:
        - name: 命令名

        返回:
        - bool: 是否存在并已注销
        """
        if name not in self.commands:
            return False
        self.commands.pop(name, None)
        self.intros.pop(name, None)
        self._disabled_commands.discard(name)
        return True

    def enable_command(self, name: str) -> bool:
        """
        启用命令

        参数:
        - name: 命令名

        返回:
        - bool: 是否存在并已启用
        """
        if name not in self.commands:
            return False
        self._disabled_commands.discard(name)
        return True

    def disable_command(self, name: str) -> bool:
        """
        停用命令 (停用后消息不再按命令处理)

        参数:
        - name: 命令名

        返回:
        - bool: 是否存在并已停用
        """
        if name not in self.commands:
            return False
        self._disabled_commands.add(name)
        return True

    def is_command_enabled(self, name: str) -> bool:
        """
        检查命令是否启用

        参数:
        - name: 命令名

        返回:
        - bool: 检查结果
        """
        return name in self.commands and name not in self._disabled_commands

    def list_commands(self) -> Dict[str, str]:
        """
        列出已注册命令及其简介

        返回:
        - Dict[命令名, 简介]
        """
        return self.intros.copy()
