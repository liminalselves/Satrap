"""子代理任务创建与执行工具"""
from satrap.core.framework.Base import ModelWorkflowFramework, AsyncModelWorkflowFramework
from satrap.core.utils.TCBuilder import ToolsManager, AsyncToolsManager
from satrap.core.utils.TCBuilder import Tool, AsyncTool
from satrap.core.APICall.LLMCall import LLM, AsyncLLM
from uuid import uuid4
import asyncio
import json
import multiprocessing
from multiprocessing.process import BaseProcess
import pickle
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Protocol, cast

from satrap.core.log import logger


SUB_AGENT_SYSTEM_PROMPT = """
你是一个子代理，你需要根据任务描述，合理使用工具来完成任务并返回结果
"""

MAX_SUB_AGENT_TASKS = 16
DEFAULT_SUB_AGENT_TIMEOUT = 120.0
DEFAULT_SUB_AGENT_WORKERS = 8


class _ProcessConnection(Protocol):
    """父子进程交换子任务消息所需的管道接口"""

    def poll(self, timeout: float = 0.0) -> bool:
        """检查管道是否已有结果"""
        ...

    def recv(self) -> object:
        """读取管道消息"""
        ...

    def send(self, obj: object) -> None:
        """
        发送管道消息

        参数:
        - obj: 可序列化消息
        """
        ...

    def close(self) -> None:
        """关闭管道端点"""
        ...


@dataclass
class _PendingToolCall:
    """父进程中等待完成的工具调用"""

    result_queue: queue.Queue[tuple[bool, object]]


@dataclass
class _RunningSubTask:
    """父进程中正在执行的同步子任务"""

    index: int
    task: str
    process: BaseProcess
    connection: _ProcessConnection
    deadline: float
    pending_tool_call: _PendingToolCall | None = None


def _serialize_llm(llm: LLM) -> bytes:
    """
    序列化同步子代理使用的 LLM

    参数:
    - llm: 同步模型实例

    返回:
    - 可由隔离进程恢复的字节数据
    """
    if type(llm) is LLM:
        client_api_key = getattr(llm.client, "api_key", "")
        config: dict[str, object] = {
            "api_key": str(client_api_key),
            "base_url": llm.base_url,
            "model": llm.model,
            "temperature": llm.temperature,
            "top_p": llm.top_p,
            "max_tokens": llm.max_tokens,
            "suppress_error": llm.suppress_error,
            "return_false": llm.return_false,
            "lock_api_key": True,
            "allow_insecure_base_url": True,
            "thinking_field_name": llm.thinking_field_name,
            "thinking_fields": llm.thinking_fields,
            "omit_none_thinking_fields": llm.omit_none_thinking_fields,
        }
        return pickle.dumps(("config", config))
    return pickle.dumps(("object", llm))


def _restore_llm(payload: bytes) -> LLM:
    """
    在隔离进程中恢复同步 LLM

    参数:
    - payload: 序列化模型数据

    返回:
    - 恢复后的同步模型实例
    """
    kind, value = pickle.loads(payload)
    if kind == "config":
        return LLM(**value)
    return value


class _ProxyToolsManager(ToolsManager):
    """把隔离进程中的工具调用转发回父进程"""

    def __init__(
        self,
        definitions: list[dict[str, Any]],
        connection: _ProcessConnection,
    ) -> None:
        """
        初始化工具定义与通信管道

        参数:
        - definitions: 可供模型使用的工具定义
        - connection: 与父进程通信的双向管道
        """
        self._definitions = definitions
        self._connection = connection

    def get_tools_definitions(self) -> list[dict[str, Any]]:
        """
        返回父进程提供的工具定义

        返回:
        - 工具定义列表
        """
        return self._definitions

    def execute_tool_call(self, call_info: dict[str, Any]) -> Any:
        """
        请求父进程执行真实工具

        参数:
        - call_info: 模型生成的工具调用信息

        返回:
        - 父进程工具管理器生成的工具消息与结果
        """
        self._connection.send(("tool_call", call_info))
        response = self._connection.recv()
        response_tuple = (
            cast(tuple[object, ...], response)
            if isinstance(response, tuple)
            else ()
        )
        if (
            len(response_tuple) == 2
            and response_tuple[0] == "tool_result"
        ):
            return response_tuple[1]
        error = (
            str(response_tuple[1])
            if len(response_tuple) == 2
            else "父进程未返回有效工具结果"
        )
        call_id = str(call_info.get("id", ""))
        return (
            {
                "role": "tool",
                "content": json.dumps({"ok": False, "error": error}, ensure_ascii=False),
                "tool_call_id": call_id,
            },
            {"ok": False, "error": error},
        )


def _execute_parent_tool(
    tools_manager: ToolsManager,
    call_info: dict[str, Any],
    result_queue: queue.Queue[tuple[bool, object]],
) -> None:
    """
    在守护线程中执行父进程工具调用

    参数:
    - tools_manager: 真实工具管理器
    - call_info: 工具调用信息
    - result_queue: 工具结果队列
    """
    try:
        result_queue.put((True, tools_manager.execute_tool_call(call_info)))
    except BaseException as error:
        result_queue.put((False, f"{type(error).__name__}: {error}"))


def _run_sub_task_process(
    llm_payload: bytes,
    tool_definitions: list[dict[str, Any]],
    context_id: str,
    task: str,
    connection: _ProcessConnection,
) -> None:
    """
    在可终止子进程中执行单个同步子任务

    参数:
    - llm_payload: 序列化模型数据
    - tool_definitions: 可供模型使用的工具定义
    - context_id: 子代理上下文 ID
    - task: 子任务说明
    - connection: 向父进程返回结果的管道
    """
    try:
        llm = _restore_llm(llm_payload)
        tools_manager = _ProxyToolsManager(tool_definitions, connection)
        result = SubAgentModel(llm, context_id, tools_manager).forward(task)
        connection.send(("final", (True, str(result))))
    except BaseException as error:
        connection.send(("final", (False, f"{type(error).__name__}: {error}")))
    finally:
        connection.close()

class SubAgentModel(ModelWorkflowFramework):
    def __init__(self, llm: LLM, context_id: str, tools_manager: ToolsManager):
        """
        初始化子代理模型

        参数:
        - llm: LLM 模型实例
        - context_id: 子代理上下文 ID
        - tools_manager: 工具管理器实例
        """
        super().__init__(llm, context_id = f"sub_agent_{context_id}", tools_manager = tools_manager, system_prompt = SUB_AGENT_SYSTEM_PROMPT)

    def forward(self, task: str):
        """
        运行子代理任务

        参数:
        - task: 子代理要执行的任务描述

        返回:
        - 运行子代理任务
        """
        return self.tools_agent(task)
    

class AsyncSubAgentModel(AsyncModelWorkflowFramework):
    def __init__(self, llm: AsyncLLM, context_id: str, tools_manager: AsyncToolsManager):
        """
        初始化异步子代理模型

        参数:
        - llm: AsyncLLM 模型实例
        - context_id: 子代理上下文 ID
        - tools_manager: 异步工具管理器实例
        """
        super().__init__(llm, context_id = f"sub_agent_{context_id}", tools_manager = tools_manager, system_prompt = SUB_AGENT_SYSTEM_PROMPT)

    def forward(self, task: str):
        """
        运行异步子代理任务

        参数:
        - task: 子代理要执行的任务描述

        返回:
        - 运行异步子代理任务
        """
        return self.tools_agent(task)


class SubAgent(Tool):
    def __init__(
        self,
        llm: LLM,
        tools_manager: ToolsManager,
        task_timeout: float = DEFAULT_SUB_AGENT_TIMEOUT,
        max_workers: int = DEFAULT_SUB_AGENT_WORKERS,
    ):
        """
        初始化子代理工具

        参数:
        - llm: LLM 模型实例
        - tools_manager: 工具管理器实例
        - task_timeout: 整批子任务超时秒数
        - max_workers: 最大并行子任务数
        """
        super().__init__(
            tool_name = "sub_agent",
            description = "子代理，运行在独立上下文中的子代理，用于高效处理特定任务",
            params_dict = {"task": ("array", "要执行的任务描述数组，形式为: [sub_task1, sub_task2, sub_task3]")},
        )   # 初始化父类工具

        self.llm = llm
        self.tools_manager = tools_manager
        self.task_timeout = max(0.1, float(task_timeout))
        self.max_workers = max(1, min(MAX_SUB_AGENT_TASKS, int(max_workers)))

    def execute(self, task: str) -> str:
        """
        执行子代理任务

        参数:
        - task: 子代理要执行的任务描述数组

        返回:
        - str: 执行子代理任务
        """
        # Step.1 安全解析 (处理模型可能传字符串或数组的情况)
        try:
            task_list: str | list[str] | int | float | bool | None = json.loads(task)
            if isinstance(task_list, str):
                task_list = [task_list]
            elif not isinstance(task_list, list):
                return f"错误：传入的task不是数组，而是 {type(task_list)}"

        except json.JSONDecodeError:
            logger.warning(f"[SubAgent] 警告：传入的task不是JSON数组，尝试当作单个任务处理")
            task_list = [task]   # 解析失败就当单个任务处理

        # Step.2 如果没有任务, 直接返回
        if not task_list:
            return "未收到任何子任务"
        if len(task_list) > MAX_SUB_AGENT_TASKS:
            return f"错误: 子任务数量不能超过 {MAX_SUB_AGENT_TASKS}"
        if not all(isinstance(sub_task, str) and sub_task.strip() for sub_task in task_list):
            return "错误: 每个子任务都必须是非空字符串"
        
        # Step.3 序列化隔离进程输入
        results_dict: dict[int, str] = {}
        try:
            llm_payload = _serialize_llm(self.llm)
            tool_definitions = self.tools_manager.get_tools_definitions()
            pickle.dumps(tool_definitions)
        except (pickle.PickleError, TypeError, AttributeError) as error:
            logger.error(f"[SubAgent] 无法隔离同步子代理依赖: {error}")
            return f"错误: 同步子代理模型或工具定义无法安全传入隔离进程: {error}"

        # Step.4 在可终止子进程中并行执行
        process_context = multiprocessing.get_context("spawn")
        pending = list(enumerate(task_list, start=1))
        running: dict[int, _RunningSubTask] = {}
        batch_id = uuid4().hex
        try:
            while pending or running:
                while pending and len(running) < self.max_workers:
                    index, sub_task = pending.pop(0)
                    parent_connection, child_connection = process_context.Pipe(duplex=True)
                    process = process_context.Process(
                        target=_run_sub_task_process,
                        args=(
                            llm_payload,
                            tool_definitions,
                            f"{batch_id}_{index}",
                            sub_task,
                            child_connection,
                        ),
                    )
                    process.start()
                    child_connection.close()
                    running[index] = _RunningSubTask(
                        index=index,
                        task=sub_task,
                        process=process,
                        connection=parent_connection,
                        deadline=time.monotonic() + self.task_timeout,
                    )

                now = time.monotonic()
                for index, current in list(running.items()):
                    pending_tool_call = current.pending_tool_call
                    if pending_tool_call is not None:
                        try:
                            succeeded, tool_result = pending_tool_call.result_queue.get_nowait()
                        except queue.Empty:
                            pass
                        else:
                            message_kind = "tool_result" if succeeded else "tool_error"
                            try:
                                current.connection.send((message_kind, tool_result))
                            except (BrokenPipeError, EOFError, OSError, TypeError, pickle.PickleError):
                                try:
                                    current.connection.send(
                                        ("tool_error", "工具结果无法跨进程序列化")
                                    )
                                except (BrokenPipeError, EOFError, OSError):
                                    pass
                            current.pending_tool_call = None

                    if now >= current.deadline:
                        logger.warning(
                            f"[SubAgent] 子任务执行超时: index={index}, {self.task_timeout} 秒"
                        )
                        current.process.terminate()
                        current.process.join(timeout=1)
                        if current.process.is_alive():
                            current.process.kill()
                            current.process.join(timeout=1)
                        current.connection.close()
                        results_dict[index] = (
                            f"子代理{index}执行失败: 超过 {self.task_timeout} 秒\n"
                        )
                        running.pop(index)
                    elif current.pending_tool_call is None and current.connection.poll():
                        try:
                            message = current.connection.recv()
                        except (EOFError, OSError):
                            message = None
                        if not isinstance(message, tuple):
                            continue
                        message_tuple = cast(tuple[object, ...], message)
                        if len(message_tuple) != 2:
                            continue
                        message_kind, payload = message_tuple
                        if message_kind == "tool_call" and isinstance(payload, dict):
                            call_info = dict(cast(dict[str, Any], payload))
                            result_queue: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)
                            current.pending_tool_call = _PendingToolCall(result_queue)
                            threading.Thread(
                                target=_execute_parent_tool,
                                args=(self.tools_manager, call_info, result_queue),
                                daemon=True,
                                name=f"satrap-sub-agent-tool-{index}",
                            ).start()
                        elif (
                            message_kind == "final"
                            and isinstance(payload, tuple)
                        ):
                            final_payload = cast(tuple[object, ...], payload)
                            if len(final_payload) != 2:
                                continue
                            succeeded = bool(final_payload[0])
                            result = str(final_payload[1])
                            if succeeded:
                                results_dict[index] = (
                                    f"子代理{index}执行任务: {current.task}，结果: {result}\n"
                                )
                            else:
                                results_dict[index] = f"子代理{index}执行失败: {result}\n"
                            current.process.join(timeout=0.2)
                            current.connection.close()
                            running.pop(index)
                    elif not current.process.is_alive():
                        current.process.join(timeout=0.2)
                        current.connection.close()
                        results_dict[index] = f"子代理{index}执行失败: 子进程异常退出\n"
                        running.pop(index)
                if running:
                    time.sleep(0.01)
        finally:
            for current in running.values():
                if current.process.is_alive():
                    current.process.terminate()
                    current.process.join(timeout=1)
                current.connection.close()

        # Step.5 按原始顺序拼接输出
        final_results = ""
        for i in range(1, len(task_list) + 1):
            final_results += results_dict.get(i, f"子代理{i}执行失败: 未返回结果\n")

        return final_results
    

class AsyncSubAgent(AsyncTool):
    def __init__(
        self,
        llm: AsyncLLM,
        tools_manager: AsyncToolsManager,
        task_timeout: float = DEFAULT_SUB_AGENT_TIMEOUT,
        max_workers: int = DEFAULT_SUB_AGENT_WORKERS,
    ):
        """
        初始化异步子代理工具

        参数:
        - llm: AsyncLLM 模型实例
        - tools_manager: 异步工具管理器实例
        - task_timeout: 单个子任务超时秒数
        - max_workers: 最大并行子任务数
        """
        super().__init__(
            tool_name = "sub_agent",
            description = "异步子代理，运行在独立上下文中的异步子代理，用于高效处理特定任务",
            params_dict = {"task": ("array", "要执行的任务描述数组，形式为: [sub_task1, sub_task2, sub_task3]")},
        )   # 初始化父类工具

        self.llm = llm
        self.tools_manager = tools_manager
        self.task_timeout = max(0.1, float(task_timeout))
        self.max_workers = max(1, min(MAX_SUB_AGENT_TASKS, int(max_workers)))

    async def execute(self, task: str) -> str:
        """
        执行异步子代理任务

        参数:
        - task: 子代理要执行的任务描述数组

        返回:
        - str: 执行异步子代理任务
        """
        # Step.1 安全解析 (处理模型可能传字符串或数组的情况)
        try:
            task_list: str | list[str] | int | float | bool | None = json.loads(task)
            if isinstance(task_list, str):
                task_list = [task_list]
            elif not isinstance(task_list, list):
                return f"错误：传入的task不是数组，而是 {type(task_list)}"

        except json.JSONDecodeError:   # 解析失败时当作单个任务处理
            logger.warning(f"[AsyncSubAgent] 警告：传入的task不是JSON数组，尝试当作单个任务处理")
            task_list = [task]

        if not task_list:
            return "未收到任何子任务"
        if len(task_list) > MAX_SUB_AGENT_TASKS:
            return f"错误: 子任务数量不能超过 {MAX_SUB_AGENT_TASKS}"
        if not all(isinstance(sub_task, str) and sub_task.strip() for sub_task in task_list):
            return "错误: 每个子任务都必须是非空字符串"

        # Step.2 定义单个子任务的执行函数
        async def run_single(index: int, sub_task: str) -> dict[str, Any]:
            sub_agent = await AsyncSubAgentModel.create(self.llm, index, self.tools_manager)
            result = await sub_agent.forward(sub_task)
            return {
                "index": index,
                "sub_task": sub_task,
                "result": result
            }   # 返回包含索引, 子任务和结果的字典
        

        # Step.3 并行执行
        semaphore = asyncio.Semaphore(self.max_workers)

        async def run_limited(index: int, sub_task: str) -> dict[str, object]:
            """
            在并发与超时上限内执行单个子任务

            参数:
            - index: 子任务序号
            - sub_task: 子任务说明

            返回:
            - 包含任务结果或超时错误的结构化记录
            """
            async with semaphore:
                try:
                    return await asyncio.wait_for(
                        run_single(index, sub_task), timeout=self.task_timeout,
                    )
                except TimeoutError:
                    return {
                        "index": index,
                        "sub_task": sub_task,
                        "error": f"超过 {self.task_timeout} 秒",
                    }

        coros = [run_limited(i, sub_task) for i, sub_task in enumerate(task_list, start=1)]
        # 创建所有协程任务

        results = await asyncio.gather(*coros, return_exceptions=True)
        # 使用 gather 并发执行, return_exceptions=True 可防止某个子任务崩溃影响整体

        output_lines: list[str] = []
        for res in results:
            if isinstance(res, dict) and "index" in res and "error" not in res:
                output_lines.append(
                    f"子代理{res['index']}执行任务: {res['sub_task']}，结果: {res['result']}"
                )   # 正常结果

            elif isinstance(res, dict) and "index" in res:
                output_lines.append(f"子代理{res['index']}执行失败: {res.get('error', '未知错误')}")

            else:
                output_lines.append(f"子代理执行失败: {str(res)}")
                # 异常或其他意外类型
        return "\n".join(output_lines)
