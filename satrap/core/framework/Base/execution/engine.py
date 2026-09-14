"""
工作流统一执行协议与原生 I/O 驱动

普通与可恢复模式共用模型和工具循环, 仅执行记录与提交策略不同,
动态 JSON 字段限于模型请求和步骤存储边界, 固定指令与工作流使用明确类型
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from contextlib import nullcontext
import asyncio
import inspect
import copy
from typing import Any, Generator, TYPE_CHECKING, TypedDict, Unpack, cast

from satrap.core.utils.context import PreparedModelContext
from satrap.core.type import LLMCallResponse, TokenUsage
from satrap.core.utils.media import user_media_content, freeze_tool_result
from .errors import ModelCallError
from .store import RunStore, RunConflictError, RunNeedsAttention, fingerprint
from .flow import ModelStep, ToolStep, agent_flow

if TYPE_CHECKING:
    from ..async_workflow import AsyncModelWorkflowFramework
    from ..sync_workflow import ModelWorkflowFramework

    Workflow = ModelWorkflowFramework | AsyncModelWorkflowFramework


class ExecutionOptions(TypedDict, total=False):
    """同步和异步驱动器传入共用循环的可选参数"""

    user_input: str | None
    run_id: str | None
    stream: bool
    callback: bool
    thinking: str
    img_urls: list[str] | None
    video_urls: list[str] | None
    max_iterations: int
    recoverable: bool
    initial_response: LLMCallResponse | None


@dataclass
class FreezeInput:
    """在执行记录创建前固定用户媒体, 异步驱动使用工作线程"""

    text: str
    images: list[str] | None
    videos: list[str] | None


@dataclass
class Prepare:
    """准备本轮模型上下文与工具定义"""

    step: ModelStep
    tools: list[dict[str, Any]]
    images: list[str] | None


@dataclass
class InvokeModel:
    """调用模型或消费兼容接口提供的首次响应"""

    request: dict[str, Any]
    stream: bool
    callback: bool
    initial_response: LLMCallResponse | None = None


@dataclass
class InvokeTool:
    """使用工作流工具管理器执行单个调用"""

    call: dict[str, Any]


@dataclass
class Reload:
    """重新加载已提交历史, 可在成功后生成检查点"""

    checkpoint: bool = False


@dataclass
class RecordUsage:
    """保存模型请求的上下文准备统计与真实用量"""

    prepared: PreparedModelContext
    usage: TokenUsage | None


@dataclass
class Commit:
    """将完整一轮消息交给上下文存储, 不创建执行记录"""

    messages: list[dict[str, Any]]


@dataclass
class ReplaceHistory:
    """为兼容执行器预先保存的用户消息补齐固定媒体"""

    messages: list[dict[str, Any]]


Action = FreezeInput | Prepare | InvokeModel | InvokeTool | Reload | RecordUsage | Commit | ReplaceHistory


def configuration(wf: Workflow) -> str:
    """
    计算用于恢复校验的配置指纹, 不保存 API 密钥

    参数:
    - wf: 提供当前模型, 工具和插件配置的工作流

    返回:
    - 配置与工具实现的摘要字符串
    """
    model = {key: getattr(wf.llm, key, None) for key in (
        "model", "base_url", "temperature", "top_p", "max_tokens",
        "supports_visual_input", "thinking_fields", "omit_none_thinking_fields",
    )}
    tools: list[dict[str, Any]] = []
    registry = cast(dict[str, Any], wf.tools_manager.tools)
    for name, tool in sorted(registry.items()):
        try:
            source = inspect.getsource(cast(Any, type(tool)))
        except (OSError, TypeError):
            source = f"{type(tool).__module__}.{type(tool).__qualname__}"
        tools.append({"name": name, "source": source, "enabled": tool.is_enabled(),
                      "definition": tool.get_tool_defined(), "owner": tool.owner_plugin,
                      "recovery": getattr(tool, "recovery_policy", "manual")})
    return fingerprint({"model": model, "tools": tools, "plugins": getattr(wf, "recovery_plugin_fingerprint", "")})


def prepared_metadata(prepared: PreparedModelContext) -> dict[str, Any]:
    """
    提取上下文准备统计, 不复制消息正文

    参数:
    - prepared: 完整上下文准备结果

    返回:
    - 可持久化的统计字段字典
    """
    return {field.name: getattr(prepared, field.name) for field in fields(prepared) if field.name != "messages"}


def restore_prepared(metadata: dict[str, Any]) -> PreparedModelContext:
    """
    从已保存统计重建上下文准备对象

    参数:
    - metadata: 保存的统计字段, 兼容旧执行记录

    返回:
    - 消息列表为空的准备对象, 仅用于重建用量统计
    """
    return PreparedModelContext(**{"messages": [], **metadata})


def execute(
    wf: Workflow, *, user_input: str | None = "", run_id: str | None = None,
    stream: bool = False, callback: bool = True, thinking: str = "off",
    img_urls: list[str] | None = None, max_iterations: int = 10,
    video_urls: list[str] | None = None,
    recoverable: bool = True, initial_response: LLMCallResponse | None = None,
) -> Generator[Action, Any, str]:
    """
    驱动共用 Agent 循环, 按固定策略选择是否持久化步骤

    参数:
    - wf: 提供模型, 上下文和工具的工作流
    - user_input: 本轮用户输入, 默认空字符串; None 用于已有用户消息的兼容调用
    - run_id: 待恢复任务 ID, 默认 None 表示新任务
    - stream: 是否使用流式模型调用, 默认 False
    - callback: 是否回传模型内容, 默认 True
    - thinking: 模型思考强度, 默认 off
    - img_urls: 附加图片地址, 默认 None
    - video_urls: 附加视频地址, 默认 None
    - max_iterations: 最大工具轮数, 默认 10, 非正数按 1 处理
    - recoverable: 是否记录执行步骤, 默认 True; workflow 入口显式传入自身配置
    - initial_response: 已取得的首次模型响应, 默认 None, 仅用于非持久化兼容执行

    返回:
    - 固定执行指令生成器, 成功时返回最终回答, 失败或取消向外传播
    """
    if run_id is not None and not recoverable:
        raise ValueError("普通执行不能恢复任务")
    if initial_response is not None and recoverable:
        raise ValueError("已有模型响应不能作为可恢复任务的起点")
    store = RunStore(wf.ctx.db_path, wf.ctx.conversation_id) if recoverable else None
    # Step.1 获取执行权并准备新任务或恢复输入
    with store.claim() if store is not None else nullcontext():
        payload: dict[str, Any] = {
            "user_input": user_input, "stream": stream, "thinking": thinking,
            "img_urls": img_urls, "max_iterations": max_iterations,
            "origin": getattr(wf, "recovery_origin", {}),
            "context_start": len(wf.ctx.get_context()),
        }
        if run_id is None and (img_urls or video_urls):
            content = yield FreezeInput(user_input or "", img_urls, video_urls)
            if user_input is None:
                history = copy.deepcopy(wf.ctx.get_context())
                for message in reversed(history):
                    if message.get("role") == "user":
                        existing = message.get("content", "")
                        parts = existing if isinstance(existing, list) else [{"type": "text", "text": existing}]
                        message["content"] = parts + [part for part in content[1:] if part not in parts]
                        break
                else:
                    raise ValueError("媒体输入需要关联用户消息")
                yield ReplaceHistory(history)
            else:
                payload["user_input"] = content
            payload["img_urls"] = None
        context_fingerprint = ""
        if store is not None:
            yield Reload()
            config = configuration(wf)
            payload["context_start"] = len(wf.ctx.get_context())
            if run_id is None:
                run_id = store.create(payload, store.history_signature(), config)
            else:
                run = store.get(run_id)
                if run["status"] == "completed":
                    wf.last_run_id = run_id
                    wf.reset_context_stats()
                    with store.connect() as db:
                        keys = db.execute("SELECT step_key FROM agent_steps WHERE run_id=? AND kind='model' AND status='completed' ORDER BY CAST(substr(step_key,7) AS INTEGER)", (run_id,)).fetchall()
                    for row in keys:
                        completed = store.step(run_id, row["step_key"])
                        if completed is not None and "_prepared" in completed["input"]:
                            usage_data = completed["result"].get("usage")
                            yield RecordUsage(restore_prepared(completed["input"]["_prepared"]),
                                              TokenUsage(**usage_data) if usage_data is not None else None)
                    yield Reload(checkpoint=True)
                    return run["result"] or ""
                if run["status"] == "cancelled":
                    raise RunConflictError("已取消任务不能恢复")
                if run["context_fingerprint"] != store.history_signature():
                    raise RunConflictError("会话历史已修改, 请创建新任务或分支")
                if run["config_fingerprint"] != config:
                    raise RunConflictError("模型或工具配置已改变, 不能恢复旧任务")
                payload = run["payload"]
                store.update(run_id, status="running")
            context_fingerprint = store.get(run_id)["context_fingerprint"]
        wf.last_run_id = run_id
        wf.reset_context_stats()
        flow = agent_flow(payload["user_input"], payload["max_iterations"])
        step = next(flow)
        # Step.2 执行模型与工具步骤, 仅可恢复模式读取和保存执行记录
        try:
            while True:
                request: dict[str, Any] = {}
                if store is not None and context_fingerprint != store.history_signature():
                    raise RunConflictError("执行期间会话历史已改变, 已停止后续步骤")
                saved = store.step(run_id, step.key) if store is not None and run_id is not None else None
                if saved is not None and saved["status"] == "completed":
                    value = saved["result"]
                elif isinstance(step, ModelStep):
                    seeded = initial_response if step.key == "model:0" else None
                    if saved is None and seeded is None:
                        tools = [] if step.final else wf.tools_manager.get_tools_definitions()
                        prepared = yield Prepare(step, tools, payload["img_urls"])
                        request = {"messages": prepared.messages, "tools": tools,
                                   "thinking": payload["thinking"], "img_urls": payload["img_urls"],
                                   "_prepared": prepared_metadata(prepared)}
                        if store is not None and run_id is not None:
                            store.start_model_step(run_id, step.key, request)
                    elif saved is not None and store is not None and run_id is not None:
                        request, _ = store.model_request(run_id, step.key)
                    response = yield InvokeModel(request, payload["stream"], callback, seeded)
                    if not isinstance(response, LLMCallResponse):
                        raise ModelCallError("模型调用失败")
                    value = asdict(response)
                    if store is not None and run_id is not None:
                        store.finish_step(run_id, step.key, value)
                else:
                    if store is not None and run_id is not None:
                        if saved is None:
                            tool = wf.tools_manager.tools.get(step.call.get("name", ""))
                            policy = getattr(tool, "recovery_policy", "manual")
                            store.start_step(run_id, step.key, "tool", step.call, policy)
                        elif saved["recovery_policy"] != "retry":
                            raise RunNeedsAttention(f"工具步骤 {step.key} 的执行结果未知, 请确认是否重试")
                    value = yield InvokeTool(step.call)
                    if store is not None and run_id is not None:
                        store.finish_step(run_id, step.key, value)
                if isinstance(step, ModelStep):
                    stored_request = saved["input"] if saved is not None and saved["status"] == "completed" else request
                    if "_prepared" in stored_request:
                        usage = TokenUsage(**value["usage"]) if value.get("usage") is not None else None
                        yield RecordUsage(restore_prepared(stored_request["_prepared"]), usage)
                try:
                    step = flow.send(value)
                except StopIteration as ended:
                    messages, result = ended.value
                    # Step.3 成功后提交整轮消息, 可恢复模式同时提交任务完成状态
                    if store is not None and run_id is not None:
                        store.commit_messages(run_id, messages, result)
                        yield Reload(checkpoint=True)
                    else:
                        yield Commit(messages)
                    return result
        except BaseException as exc:
            if store is not None and run_id is not None and store.get(run_id)["status"] != "completed":
                state = "needs_attention" if isinstance(exc, RunNeedsAttention) else (
                    "cancelled" if isinstance(exc, asyncio.CancelledError) else
                    "interrupted" if not isinstance(exc, Exception) else "failed")
                store.update(run_id, status=state, error=str(exc))
            raise


def _request_kwargs(request: dict[str, Any]) -> dict[str, Any]:
    """
    提取模型调用参数并去掉内部统计字段

    参数:
    - request: 已准备或从执行记录恢复的模型请求

    返回:
    - 模型参数字典, thinking 为 off 时不显式传递该字段
    """
    return {key: value for key, value in request.items() if key not in {"messages", "_prepared"} and (key != "thinking" or value != "off")}


async def run_async(wf: AsyncModelWorkflowFramework, **kwargs: Unpack[ExecutionOptions]) -> str:
    """
    原生异步执行共用 Agent 指令

    参数:
    - wf: 已完成初始化的异步工作流
    - kwargs: execute 的可选参数, recoverable 默认 True, 普通入口须显式传入 False

    返回:
    - 最终模型回答, 执行失败或取消时抛出异常
    """
    driver = execute(wf, **kwargs)
    try:
        action = next(driver)
        while True:
            if isinstance(action, FreezeInput):
                value = await asyncio.to_thread(user_media_content, action.text, action.images, action.videos, wf.llm)
            elif isinstance(action, Prepare):
                value = await wf.ctx.prepare_model_context(llm=wf.llm, pending_messages=action.step.messages,
                                                           tools=action.tools, img_urls=action.images)
            elif isinstance(action, InvokeTool):
                tool_message, tool_result = await wf.tools_manager.execute_tool_call(action.call)
                value = (tool_message, await asyncio.to_thread(freeze_tool_result, tool_result, wf.llm))
            elif isinstance(action, Commit):
                value = await wf.ctx._commit_turn_messages(action.messages)
            elif isinstance(action, ReplaceHistory):
                value = await wf.ctx.replace_messages(action.messages)
            elif isinstance(action, Reload):
                value = await wf.ctx.load_context()
                if action.checkpoint:
                    await wf.ctx._maybe_auto_checkpoint()
            elif isinstance(action, RecordUsage):
                wf._record_context_request(action.prepared, action.usage)
                value = await wf.ctx.record_model_usage(action.prepared, action.usage)
            elif isinstance(action, InvokeModel):
                value = None
                request = action.request
                if action.initial_response is not None:
                    value = action.initial_response
                    if action.callback:
                        if wf.return_thinking and value.thinking:
                            await (wf.thinking_callback or wf._content_callback)(value.thinking)
                        await wf._content_callback(value.content)
                elif action.stream:
                    async for event in wf.llm.stream_call(request["messages"], **_request_kwargs(request)):
                        if event.kind == "done":
                            value = event.response
                        elif action.callback and event.kind == "content_delta":
                            await wf._content_callback(event.delta)
                        elif action.callback and event.kind == "thinking_delta" and wf.return_thinking:
                            await (wf.thinking_callback or wf._content_callback)(event.delta)
                else:
                    value = await wf.llm.call(request["messages"], **_request_kwargs(request))
                    if action.callback and isinstance(value, LLMCallResponse):
                        if wf.return_thinking and value.thinking:
                            await (wf.thinking_callback or wf._content_callback)(value.thinking)
                        await wf._content_callback(value.content)
            else:
                raise RuntimeError("未知执行指令")
            action = driver.send(value)
    except StopIteration as ended:
        return ended.value
    except BaseException as exc:
        try:
            driver.throw(exc)
        finally:
            driver.close()
        raise


def run_sync(wf: ModelWorkflowFramework, **kwargs: Unpack[ExecutionOptions]) -> str:
    """
    原生同步执行共用 Agent 指令

    参数:
    - wf: 同步工作流
    - kwargs: execute 的可选参数, recoverable 默认 True, 普通入口须显式传入 False

    返回:
    - 最终模型回答, 执行失败或取消时抛出异常
    """
    driver = execute(wf, **kwargs)
    try:
        action = next(driver)
        while True:
            if isinstance(action, FreezeInput):
                value = user_media_content(action.text, action.images, action.videos, wf.llm)
            elif isinstance(action, Prepare):
                value = wf.ctx.prepare_model_context(llm=wf.llm, pending_messages=action.step.messages,
                                                     tools=action.tools, img_urls=action.images)
            elif isinstance(action, InvokeTool):
                tool_message, tool_result = wf.tools_manager.execute_tool_call(action.call)
                value = (tool_message, freeze_tool_result(tool_result, wf.llm))
            elif isinstance(action, Commit):
                value = wf.ctx._commit_turn_messages(action.messages)
            elif isinstance(action, ReplaceHistory):
                value = wf.ctx.replace_messages(action.messages)
            elif isinstance(action, Reload):
                value = wf.ctx.load_context()
                if action.checkpoint:
                    wf.ctx._maybe_auto_checkpoint()
            elif isinstance(action, RecordUsage):
                wf._record_context_request(action.prepared, action.usage)
                value = wf.ctx.record_model_usage(action.prepared, action.usage)
            elif isinstance(action, InvokeModel):
                value = None
                request = action.request
                if action.initial_response is not None:
                    value = action.initial_response
                    if action.callback:
                        if wf.return_thinking and value.thinking:
                            (wf.thinking_callback or wf._content_callback)(value.thinking)
                        wf._content_callback(value.content)
                elif action.stream:
                    for event in wf.llm.stream_call(request["messages"], **_request_kwargs(request)):
                        if event.kind == "done":
                            value = event.response
                        elif action.callback and event.kind == "content_delta":
                            wf._content_callback(event.delta)
                        elif action.callback and event.kind == "thinking_delta" and wf.return_thinking:
                            (wf.thinking_callback or wf._content_callback)(event.delta)
                else:
                    value = wf.llm.call(request["messages"], **_request_kwargs(request))
                    if action.callback and isinstance(value, LLMCallResponse):
                        if wf.return_thinking and value.thinking:
                            (wf.thinking_callback or wf._content_callback)(value.thinking)
                        wf._content_callback(value.content)
            else:
                raise RuntimeError("未知执行指令")
            action = driver.send(value)
    except StopIteration as ended:
        return ended.value
    except BaseException as exc:
        try:
            driver.throw(exc)
        finally:
            driver.close()
        raise
