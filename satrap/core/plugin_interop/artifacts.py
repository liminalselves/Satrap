"""
宿主沙箱产物的有界读取

只读取当前宿主绑定目录内的相对路径, 冻结内容并保留本次执行来源;
不登记群资产, 不发送消息, 不开放任意服务器文件
"""
from __future__ import annotations

from pathlib import PureWindowsPath, Path
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable
import asyncio
import stat
import os

from satrap.core.plugin_interop.contracts import ImageArtifact, PluginInteractionError
from satrap.core.call_context import current_call_origin, current_tool_workflow

if TYPE_CHECKING:
    from satrap.core.plugin_interop.capabilities import PluginHost


MAX_IMAGE_BYTES = 10 * 1024 * 1024


class ArtifactReadError(RuntimeError):
    """宿主文件读取失败, 由领域入口映射为既有错误类型"""

    def __init__(self, code: str, message: str) -> None:
        """
        保存领域可识别的失败原因

        参数:
        - code: 路径, 媒体或文件不可用原因
        - message: 可读错误说明
        """
        super().__init__(message)
        self.code = code


def read_sandbox_image(root: Path, path: str) -> bytes:
    """
    有界读取当前会话沙箱内的普通图片文件, 不登记或发送

    参数:
    - root: 宿主绑定的会话沙箱根, 不接受模型配置或全局目录回退
    - path: 相对沙箱根的文件路径, 不允许绝对路径, 设备路径或上级目录

    返回:
    - 待交给资产登记器校验格式的原始字节; 路径越界或文件不可用时抛出 ArtifactReadError
    """
    # Step.1 拒绝远程地址, 绝对路径与平台相关的路径歧义
    relative = Path(path.replace("\\", "/"))
    if (not path or len(path) > 4096 or "\0" in path or ":" in path or relative.is_absolute()
            or path.startswith(("/", "\\")) or ".." in relative.parts):
        raise ArtifactReadError("invalid_argument", "请填写当前会话沙箱内的相对图片路径")
    if os.name == "nt":
        reserved = getattr(os.path, "isreserved", lambda part: PureWindowsPath(part).is_reserved())
        if any(reserved(part) or part.endswith((".", " ")) for part in relative.parts):
            raise ArtifactReadError("invalid_argument", "图片路径不能包含 Windows 设备名或含歧义的路径段")
    try:
        # Step.2 逐段拒绝链接和 Windows 重解析点, 不借用其它会话文件
        for target in (root, *[root.joinpath(*relative.parts[:index]) for index in range(1, len(relative.parts) + 1)]):
            metadata = target.lstat()
            if (stat.S_ISLNK(metadata.st_mode)
                    or getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
                raise ArtifactReadError("invalid_argument", "图片路径不能包含符号链接或重解析点")
        target = (root / relative).resolve(strict=True)
        if not target.is_relative_to(root.resolve(strict=True)):
            raise ArtifactReadError("invalid_argument", "图片路径越出当前会话沙箱")
        metadata = target.stat()
        if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= MAX_IMAGE_BYTES:
            raise ArtifactReadError("invalid_media", "图片必须是非空普通文件且不超过 10 MiB")

        # Step.3 只打开普通文件并限制实际读取大小, 不把模型路径直接交给登记 SDK
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(target, flags), "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= MAX_IMAGE_BYTES:
                raise ArtifactReadError("invalid_media", "图片必须是非空普通文件且不超过 10 MiB")
            payload = stream.read(MAX_IMAGE_BYTES + 1)
        if not 0 < len(payload) <= MAX_IMAGE_BYTES:
            raise ArtifactReadError("invalid_media", "图片内容为空或超过 10 MiB")
        return payload
    except (OSError, ValueError) as exc:
        raise ArtifactReadError("asset_unavailable", "沙箱图片不存在或无法读取") from exc


@dataclass(frozen=True)
class ArtifactAccess:
    """当前插件的沙箱图片读取入口"""

    _host: PluginHost

    def read_sandbox_image(self, path: str) -> ImageArtifact:
        """
        在当前环境版本下冻结沙箱图片及其执行来源

        参数:
        - path: 当前沙箱内的相对路径

        返回:
        - 仅可在同次执行与环境内使用的图片引用
        """
        return self._reader(path)()

    async def read_sandbox_image_async(self, path: str) -> ImageArtifact:
        """
        调度前冻结环境和调用来源, 在线程中有界读取图片

        参数:
        - path: 当前沙箱内的相对路径

        返回:
        - 同次执行内有效的图片引用, 环境或插件失效时拒绝返回
        """
        reader = self._reader(path)
        return await asyncio.to_thread(reader)

    def _reader(self, path: str) -> Callable[[], ImageArtifact]:
        """
        固定宿主路径与执行链, 供同步或异步入口执行读取

        参数:
        - path: 沙箱内相对路径

        返回:
        - 读取前后均验证原始绑定的闭包
        """
        from satrap.core.plugin_interop.capabilities import _INVOCATION

        runtime = self._host._runtime()
        scope = runtime.require_execution(self._host)
        root_scope = scope
        while root_scope.parent is not None:
            root_scope = root_scope.parent
        snapshot = self._host.environment_snapshot()
        if snapshot.sandbox_root is None:
            raise PluginInteractionError("dependency_unavailable", "当前会话未绑定沙箱目录")
        origin, workflow = current_call_origin(), current_tool_workflow()
        root = snapshot.sandbox_root

        def valid() -> bool:
            """
            判定图片是否仍位于原始执行和环境版本

            返回:
            - 调用链, 用户来源和环境仍匹配时为 True
            """
            current = _INVOCATION.get()
            if not runtime.host_valid(self._host) or current is None or not scope.is_current():
                return False
            while current is not None:
                if not current.active or not current.is_current():
                    return False
                if current is root_scope:
                    return (current_call_origin() is origin and current_tool_workflow() is workflow
                            and self._host.environment_snapshot() == snapshot)
                current = current.parent
            return False

        def read() -> ImageArtifact:
            """
            读取冻结目录内的图片并检查执行是否仍有效

            返回:
            - 可受控传递的图片引用
            """
            if not valid():
                raise PluginInteractionError("capability_stale", "图片读取来源已失效")
            payload = read_sandbox_image(root, path)
            if not valid():
                raise PluginInteractionError("capability_stale", "读取图片期间宿主环境已改变")
            return ImageArtifact(payload, valid)

        return read
