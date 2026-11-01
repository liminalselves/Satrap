"""同步与异步生图工具, 动态声明和执行共用不可变参数策略"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, TypeVar

from satrap.core.APICall.ImageCall import ImageGen, AsyncImageGen, ImageGenError
from satrap.core.approval.state import session_approval
from satrap.core.file_access.artifacts import FileArtifact, artifact_from_metadata, artifact_lease
from satrap.core.log import logger
from satrap.core.plugin_authorization import bind_plugin_factory_tools, require_plugin_entry_permission
from satrap.core.plugin_interop.environment import EnvironmentSnapshot
from satrap.core.utils.TCBuilder import Tool, AsyncTool
from satrap.core.utils.TCBuilder.utils import create_tool_defined, tool_error
from satrap.edictum import SimpleSession, AsyncSimpleSession, PluginHost
from satrap.edictum.plugin_resources import PluginResources

from satrap.expend.plugins.image_generation.artifacts import check_capacity, load_image, save_images
from satrap.expend.plugins.image_generation.policy import ImagePolicy


_T = TypeVar("_T")


async def _io(operation: Callable[..., _T], *args: Any) -> _T:
    """在线程中完成文件操作, 取消时等待本次保存结束, 不重发生成请求"""
    task = asyncio.create_task(asyncio.to_thread(operation, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.gather(task, return_exceptions=True)
        raise


class _ImageCore:
    """模型策略, 宿主状态复核与结果构造"""

    recovery_policy = "manual"
    policy: ImagePolicy
    client: ImageGen | AsyncImageGen
    config: dict[str, Any]
    _session: SimpleSession | AsyncSimpleSession
    _plugin_host: PluginHost
    tool_name: str

    def get_tool_defined(self) -> dict[str, Any]:
        """固定值仅进入描述, 只有被允许的字段进入参数 schema"""
        if self.tool_name == "send_generated_image":
            return create_tool_defined(self.tool_name, "重发当前会话已经生成的图片, 不调用生图模型; 仅在明确发送失败或用户要求重发时使用, unknown 状态不要自动重发",
                                       {"file_id": {"type": "string", "pattern": r"^sf_[0-9a-f]{32}$", "description": "generate_image 返回的图片文件 ID"}}, ["file_id"])
        size = self.policy.size_fixed if self.policy.size_mode == "fixed" else self.policy.size_default
        count = self.policy.count_fixed if self.policy.count_mode == "fixed" else self.policy.count_default
        description = f"根据提示词生成图片并交付当前会话; 尺寸{('固定为' if self.policy.size_mode == 'fixed' else '默认为')} {size}, 数量{('固定为' if self.policy.count_mode == 'fixed' else '默认为')} {count}; 每次调用只请求一次, 数量不足不自动补生成; 生成成功但发送失败时使用 send_generated_image"
        description += "; " + self.policy.prompt_instructions
        if self.policy.keywords_only:
            description += "; 当前仅允许关键词, 必须先将用户描述整理为 keywords 数组, 不接受自由文本 prompt"
        return create_tool_defined(self.tool_name, description, self.policy.properties(), [self.policy.prompt_field])

    def _context(self) -> tuple[EnvironmentSnapshot, Callable[[], None]]:
        """冻结宿主与入口, 阻止计划模式和失效安装触发计费请求"""
        host = self._plugin_host
        runtime = host._runtime()
        invocation = runtime.require_execution(host)
        environment = host.environment_snapshot()
        root = environment.artifacts_root
        if root is None or environment.session_root is None or root != environment.session_root / "artifacts":
            raise ValueError("当前宿主没有提供会话文件产物目录")

        def check() -> None:
            """等待后复核原入口, 目录和计划模式"""
            if host.environment_snapshot() != environment or runtime.require_execution(host) is not invocation:
                raise PermissionError("生图调用的宿主或执行入口已经变化")
            if session_approval(self._session).plan_mode:
                raise PermissionError("计划模式不能生成或发送图片")
            binding = getattr(self, "_plugin_entry_binding", None)
            if binding is not None:
                require_plugin_entry_permission(binding, refresh=True)

        check()
        return environment, check

    def _arguments(self, arguments: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """严格区分生成与重发参数, 校验发生在网络调用之前"""
        if self.tool_name == "send_generated_image":
            if set(arguments) != {"file_id"} or not isinstance(arguments["file_id"], str):
                raise ValueError("重发图片只接受 file_id")
            return arguments["file_id"], {}
        return self.policy.prepare_prompt(arguments)

    @staticmethod
    def _result(records: list[dict[str, Any]], deliveries: list[dict[str, Any]], requested: int, *, generated: bool) -> dict[str, Any]:
        """分别报告生成与交付状态, 不把字节或本机路径返回给模型"""
        succeeded = sum(result.get("ok") is True and result.get("status") in {"available", "uploaded", "published"} for result in deliveries)
        status = "succeeded" if succeeded == len(records) else "partial" if succeeded else "failed"
        if any(result.get("status") == "unknown" for result in deliveries):
            status = "unknown"
        return {"ok": status == "succeeded", "generation_status": "succeeded" if generated else "reused",
                "delivery_status": status, "requested_count": requested, "actual_count": len(records),
                "images": [{**record, "delivery": delivery} for record, delivery in zip(records, deliveries)],
                "retry_generation": False}

    def _failure(self, error: Exception, *, generated: bool = False, started: bool = False) -> dict[str, Any]:
        """返回稳定脱敏错误, 上游状态未知时禁止自动重新生成"""
        if isinstance(error, ValueError) and not started:
            return tool_error(self.tool_name, str(error), "invalid_arguments")
        if isinstance(error, PermissionError):
            return tool_error(self.tool_name, "当前调用没有生成或交付图片的权限", "permission_denied")
        code = error.code if isinstance(error, ImageGenError) else "storage_error" if generated else "execution_error"
        logger.warning(f"[生图插件] {code}, 类型={type(error).__name__}")
        return {**tool_error(self.tool_name, "图片操作未完成, 请检查模型配置或稍后由用户决定是否重试", code),
                "generation_status": "succeeded" if generated else "unknown" if started else "not_started", "retry_generation": False}


class GenerateImageTool(_ImageCore, Tool):
    """同步生成或重发图片, 等待当前平台交付回执"""

    def execute(self, **arguments: Any) -> dict[str, Any]:
        """执行一次生成或已有图片重发, 不自动重试计费请求"""
        generated = started = False
        try:
            value, values = self._arguments(arguments)
            environment, check = self._context()
            root = environment.artifacts_root
            assert root is not None and isinstance(self.client, ImageGen)
            with artifact_lease(root):
                if self.tool_name == "send_generated_image":
                    artifact, record = load_image(root, value)
                    check()
                    delivery = self._deliver(artifact)
                    return self._result([record], [delivery], 1, generated=False)
                params = self.policy.params(self.client, values)
                check_capacity(root, params.n or 1)
                check()
                started = True
                response = self.client.generate(value, params=params)
                if response is None or not response.images:
                    raise ImageGenError("empty_response")
                generated = True
                check()
                records = save_images(root, response, check)
                deliveries: list[dict[str, Any]] = []
                for record in records:
                    check()
                    deliveries.append(self._deliver(artifact_from_metadata(root, record["file"])))
                return self._result(records, deliveries, params.n or 1, generated=True)
        except Exception as error:
            return self._failure(error, generated=generated, started=started)

    def _deliver(self, artifact: FileArtifact) -> dict[str, Any]:
        """发送异常按回执未知处理, 保留文件供用户决定是否重发"""
        try:
            return self._plugin_host.artifacts.deliver_file_sync(artifact)
        except Exception as error:
            logger.warning(f"[生图插件] 图片交付异常, 类型={type(error).__name__}")
            return {"ok": False, "status": "unknown", "reason": "delivery_receipt_unavailable"}


class AsyncGenerateImageTool(_ImageCore, AsyncTool):
    """异步生成或重发图片, 文件操作移至线程"""

    async def execute(self, **arguments: Any) -> dict[str, Any]:
        """异步执行一次调用, 取消直接传播且不重新生成"""
        generated = started = False
        try:
            value, values = self._arguments(arguments)
            environment, check = self._context()
            root = environment.artifacts_root
            assert root is not None and isinstance(self.client, AsyncImageGen)
            with artifact_lease(root):
                if self.tool_name == "send_generated_image":
                    artifact, record = await _io(load_image, root, value)
                    check()
                    delivery = await self._deliver(artifact)
                    return self._result([record], [delivery], 1, generated=False)
                params = self.policy.params(self.client, values)
                await _io(check_capacity, root, params.n or 1)
                check()
                started = True
                response = await self.client.generate(value, params=params)
                if response is None or not response.images:
                    raise ImageGenError("empty_response")
                generated = True
                check()
                records = await _io(save_images, root, response, check)
                deliveries: list[dict[str, Any]] = []
                for record in records:
                    check()
                    deliveries.append(await self._deliver(artifact_from_metadata(root, record["file"])))
                return self._result(records, deliveries, params.n or 1, generated=True)
        except Exception as error:
            return self._failure(error, generated=generated, started=started)

    async def _deliver(self, artifact: FileArtifact) -> dict[str, Any]:
        """异步交付异常保留产物, 取消不会转成可重试失败"""
        try:
            return await self._plugin_host.artifacts.deliver_file_async(artifact)
        except Exception as error:
            logger.warning(f"[生图插件] 图片交付异常, 类型={type(error).__name__}")
            return {"ok": False, "status": "unknown", "reason": "delivery_receipt_unavailable"}


def get_tools(session: SimpleSession | AsyncSimpleSession, config: dict[str, Any] | None = None,
              resources: PluginResources | None = None, *, plugin_host: PluginHost | None = None) -> list[GenerateImageTool | AsyncGenerateImageTool]:
    """
    构造共用策略和托管模型资源的工具, 安装阶段不发送请求

    参数:
    - session: 当前同步或异步会话
    - config: 插件配置, 模型引用使用 img_model
    - resources: 宿主注入并管理生命周期的图片模型资源
    - plugin_host: 当前安装代次的文件交付入口

    返回:
    - generate_image 与 send_generated_image 工具
    """
    if resources is None or plugin_host is None:
        raise ValueError("生图插件需要宿主模型资源和图片交付接口")
    cfg = dict(config or {})
    policy = ImagePolicy.from_config(cfg)
    client = resources.get("img_model")
    asynchronous = isinstance(session, AsyncSimpleSession)
    if not isinstance(client, AsyncImageGen if asynchronous else ImageGen):
        raise ValueError("请选择与会话执行方式匹配的 img 模型")
    policy.validate_model(client)
    constructor = AsyncGenerateImageTool if asynchronous else GenerateImageTool
    tools: list[GenerateImageTool | AsyncGenerateImageTool] = []
    for name in ("generate_image", "send_generated_image"):
        tool = constructor(name, "图片生成与交付", {})
        tool.policy, tool.client, tool.config = policy, client, cfg
        tool._session, tool._plugin_host = session, plugin_host
        tools.append(tool)
    bind_plugin_factory_tools(tools, __file__)
    return tools
