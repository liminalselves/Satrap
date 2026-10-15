"""
worker 内的 Python 网络与外部进程守卫

保留 socket 和 Popen 的类结构, 包装公开别名及可用原生进程入口
所有拒绝共用首次违规记录, 不修改宿主进程或系统网络配置
"""

from __future__ import annotations

import importlib
import os
import socket
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from types import ModuleType

try:
    from .models import Violations
except ImportError:
    from models import Violations


@dataclass
class InstalledGuards:
    """保存安装目标, 仅用于启动检查与可信测试"""

    targets: list[tuple[object, str, object]] = field(default_factory=list)

    def verify(self) -> None:
        """
        确认登记目标仍指向安装的替代对象

        补丁未保持预期对象身份时抛出 RuntimeError, 不继续初始化
        """
        for target, name, replacement in self.targets:
            if getattr(target, name) is not replacement:
                raise RuntimeError(f"沙箱补丁安装失败: {name}")


def install(violations: Violations) -> InstalledGuards:
    """
    安装固定拒绝规则并检查补丁身份

    参数:
    - violations: 本次 worker 共用的首次违规记录

    返回:
    - 保存已安装目标的检查对象; 初始化异常交给 worker 返回失败
    """
    # Step.1 创建共享违规包装与安装目标记录
    installed = InstalledGuards()

    def denied(code: str, action: str) -> Callable[..., object]:
        """
        为一个固定入口创建不调用原始函数的拒绝包装

        参数:
        - code: network_disabled 或 process_not_allowed
        - action: 返回模型的固定 API 标识

        返回:
        - 记录违规并抛出 PolicyViolation 的替代函数
        """
        def reject(*args: object, **kwargs: object) -> object:
            """
            拒绝调用并记录固定错误类别, 不保留业务实参

            参数:
            - args: 兼容原入口的位置参数, 不读取其内容
            - kwargs: 兼容原入口的关键字参数, 不读取其内容

            返回:
            - 始终抛出 PolicyViolation, 不返回业务值
            """
            violations.reject(code, "沙箱禁止联网" if code == "network_disabled" else "沙箱禁止启动外部程序", action=action)
        return reject

    def patch(target: object, name: str, code: str, prefix: str) -> None:
        """
        替换平台存在的入口并登记其检查信息

        参数:
        - target: 待包装的模块或类
        - name: 入口属性名, 平台不存在时不处理
        - code: 固定错误类别
        - prefix: 用于组成动作标识的模块或类名
        """
        if hasattr(target, name):
            replacement = denied(code, prefix + "." + name)
            setattr(target, name, replacement)
            installed.targets.append((target, name, replacement))

    # Step.2 保留 socket 类继承关系并覆盖构造, 收发和 DNS 别名
    original_socket = socket.socket

    class OfflineSocket(original_socket):
        def __init__(self, *args: object, **kwargs: object) -> None:
            """
            拒绝 socket 构造并保留原类的继承结构

            参数:
            - args: 原 socket 构造位置参数, 不实际使用
            - kwargs: 原 socket 构造关键字参数, 不实际使用

            始终记录 network_disabled 并抛出 PolicyViolation
            """
            violations.reject("network_disabled", "沙箱禁止创建网络 socket", action="socket.socket")

    for name in ("connect", "connect_ex", "send", "sendall", "sendto", "sendmsg", "sendfile",
                 "bind", "listen", "accept", "recv", "recv_into", "recvfrom", "recvfrom_into", "recvmsg"):
        patch(OfflineSocket, name, "network_disabled", "socket.socket")
    socket.socket = OfflineSocket
    socket.SocketType = OfflineSocket
    installed.targets.extend(((socket, "socket", OfflineSocket), (socket, "SocketType", OfflineSocket)))
    for name in ("fromfd", "fromshare", "socketpair", "create_connection", "create_server", "getaddrinfo",
                 "gethostbyname", "gethostbyname_ex", "gethostbyaddr", "getnameinfo"):
        patch(socket, name, "network_disabled", "socket")

    # Step.3 拒绝高层进程与 Shell 入口, 保留 Popen 的类结构
    patch(subprocess.Popen, "__init__", "process_not_allowed", "subprocess.Popen")
    for name in ("run", "call", "check_call", "check_output", "getoutput", "getstatusoutput"):
        patch(subprocess, name, "process_not_allowed", "subprocess")
    for name in dir(os):
        if name in ("system", "popen", "startfile", "fork", "forkpty", "posix_spawn", "posix_spawnp") or name.startswith(("spawn", "exec")):
            patch(os, name, "process_not_allowed", "os")

    # Step.4 补充原生进程入口, 包括 Windows multiprocessing 的直接启动路径
    platform_module: ModuleType = importlib.import_module("_winapi" if os.name == "nt" else "posix")
    names = ("CreateProcess",) if os.name == "nt" else ("fork", "forkpty", "posix_spawn", "posix_spawnp", "execv", "execve")
    for name in names:
        patch(platform_module, name, "process_not_allowed", platform_module.__name__)
    if os.name != "nt":
        try:
            patch(importlib.import_module("_posixsubprocess"), "fork_exec", "process_not_allowed", "_posixsubprocess")
        except ImportError:
            pass
        # 部分解释器没有该可选内部模块, 已有 os 与公开进程入口仍保持阻断
    # Step.5 在返回初始化成功之前确认所有登记替代对象已生效
    installed.verify()
    return installed
