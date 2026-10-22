"""同步和异步文件交付工具, 共用宿主单文件读取审批与产物接口"""
from __future__ import annotations

from pathlib import Path

from satrap.core.file_access.artifacts import FileArtifact, artifact_lease, display_name, freeze_file
from satrap.core.file_access.reads import FileRead, prepare_file_read
from satrap.core.storage.file_lock import FileLease
from satrap.core.approval.state import session_approval
from satrap.core.log import logger
from satrap.core.utils.TCBuilder import Tool, AsyncTool
from satrap.edictum import SimpleSession, AsyncSimpleSession
from .base import _DocumentCore
from .utils import _doc_workspace_root


class _FileCore(_DocumentCore):
    """只交付本次明确指定的单文件, 不隐式发送整个目录"""

    tool_name = "send_file"
    description = "把已生成或明确指定的本地文件交给当前用户或当前群, 支持 PDF/DOCX/XLSX/ZIP 等普通文件, 最多 32 MiB; path 相对当前工作区, 未找到时查当前沙箱或上传目录, 也可填绝对路径; 目录外文件遵循当前审批模式. Chat 返回 available 可下载, 平台 uploaded/published 才表示成功, unknown 不要重复发送"
    params_dict = {"path": ("string", "本地单文件路径, 相对工作区或当前沙箱"),
                   "name": ("string", "可选显示文件名, 不包含目录")}
    recovery_policy = "manual"

    def get_tool_defined(self) -> dict[str, object]:
        """
        只要求文件路径, 显示名称可省略, 不开放发送目标参数

        返回:
        - 统一的同步异步工具声明
        """
        return {"type": "function", "function": {"name": self.tool_name, "description": self.description,
                "parameters": {"type": "object", "properties": {
                    "path": {"type": "string", "minLength": 1, "maxLength": 4096, "description": self.params_dict["path"][1]},
                    "name": {"type": "string", "minLength": 1, "maxLength": 240, "description": self.params_dict["name"][1]}},
                    "required": ["path"], "additionalProperties": False}}}

    def _prepare_file(self, path: str, name: str | None) -> tuple[FileRead, Path, FileLease]:
        """
        固定读取对象和本次宿主产物目录, 发送目标不属于模型参数

        参数:
        - path: 明确指定的本地单文件
        - name: 可选显示名称

        返回:
        - 读取审批计划, 产物根和需要关闭的维护租约
        """
        if self._plugin_host is None:
            raise ValueError("file_delivery_unavailable: 当前宿主未接入文件交付")
        if self._session is not None and session_approval(self._session).plan_mode:
            raise PermissionError("plan_mode: 计划模式不交付文件, 请先关闭计划模式")
        if name is not None:
            display_name(name)
        environment = self._environment()
        root = environment.artifacts_root
        if root is None or environment.session_root is None or root != environment.session_root / "artifacts":
            raise ValueError("file_delivery_unavailable: 当前宿主未提供文件产物目录")
        workspace = _doc_workspace_root(self, environment)
        if isinstance(path, str) and not Path(path).is_absolute() and environment.sandbox_root is not None:
            if not (workspace / path).exists() and (environment.sandbox_root / path).exists():
                path = str(environment.sandbox_root / path)
        plan = prepare_file_read(self, path, workspace=workspace, environment=environment,
                                 current_environment=self._environment, parameters={"operation": "send_file", "name": name})
        return plan, root, artifact_lease(root)


class SendFileTool(_FileCore, Tool):
    """同步读取审批后交付文件, 等待平台确认"""

    def _bind(self, session: SimpleSession) -> None:
        """
        绑定真实宿主会话

        参数:
        - session: 同步会话
        """
        self._session = session

    def execute(self, path: str, name: str | None = None) -> dict[str, object]:
        """
        同步冻结并交付单文件

        参数:
        - path: 本地文件路径
        - name: 可选显示名称

        返回:
        - 交付结果或明确失败, 不返回原始文件内容
        """
        artifacts: list[FileArtifact] = []
        lease: FileLease | None = None
        keep = False
        try:
            plan, root, lease = self._prepare_file(path, name)
            def copy(target: Path) -> FileArtifact:
                """
                记录已复制对象, 后置校验失败时仍能回收

                参数:
                - target: 已审批的单文件

                返回:
                - 固定内容的文件引用
                """
                artifact = freeze_file(target, root, name)
                artifacts.append(artifact)
                return artifact
            artifact = plan.read_sync(copy)
            result = self._plugin_host.artifacts.deliver_file_sync(artifact) if self._plugin_host is not None else {}
            keep = result.get("status") == "available" and result.get("ok") is True
            return result
        except Exception as error:
            logger.warning(f"[文件交付] 同步操作失败, 类型={type(error).__name__}: {error}")
            return {"ok": False, "status": "failed", "reason": str(error)}
        finally:
            if not keep:
                for artifact in artifacts:
                    (artifact.root / artifact.file_id).unlink(missing_ok=True)
            if lease is not None:
                lease.close()


class AsyncSendFileTool(_FileCore, AsyncTool):
    """异步读取审批后交付文件, Chat 登记到当前回复版本"""

    def _bind(self, session: AsyncSimpleSession) -> None:
        """
        绑定真实宿主会话

        参数:
        - session: 异步会话
        """
        self._session = session

    async def execute(self, path: str, name: str | None = None) -> dict[str, object]:
        """
        异步冻结并交付单文件

        参数:
        - path: 本地文件路径
        - name: 可选显示名称

        返回:
        - Chat 可下载登记或平台实际状态, 取消直接传播
        """
        artifacts: list[FileArtifact] = []
        lease: FileLease | None = None
        keep = False
        try:
            plan, root, lease = self._prepare_file(path, name)
            def copy(target: Path) -> FileArtifact:
                """
                记录已复制对象, 后置校验失败时仍能回收

                参数:
                - target: 已审批的单文件

                返回:
                - 固定内容的文件引用
                """
                artifact = freeze_file(target, root, name)
                artifacts.append(artifact)
                return artifact
            artifact = await plan.read_async(copy)
            host = self._plugin_host
            assert host is not None
            result = await host.artifacts.deliver_file_async(artifact)
            keep = result.get("status") == "available" and result.get("ok") is True
            return result
        except Exception as error:
            logger.warning(f"[文件交付] 异步操作失败, 类型={type(error).__name__}: {error}")
            return {"ok": False, "status": "failed", "reason": str(error)}
        finally:
            if not keep:
                for artifact in artifacts:
                    (artifact.root / artifact.file_id).unlink(missing_ok=True)
            if lease is not None:
                lease.close()
