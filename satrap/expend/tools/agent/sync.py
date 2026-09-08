import multiprocessing
import threading
import pickle
from typing import Any, cast
import queue
import time
from uuid import uuid4
from satrap.core.APICall.LLMCall import LLM
from satrap.core.utils.TCBuilder import ToolsManager
from satrap.core.utils.TCBuilder import Tool
from satrap.core.framework.Base import ModelWorkflowFramework
from satrap.core.log import logger
from .utils import (
    SUB_AGENT_SYSTEM_PROMPT,
    MAX_SUB_AGENT_TASKS,
    DEFAULT_SUB_AGENT_TIMEOUT,
    DEFAULT_SUB_AGENT_WORKERS,
    _PendingToolCall,
    _RunningSubTask,
    _serialize_llm,
    _execute_parent_tool,
    _run_sub_task_process,
)
from .utils import _parse_tasks


class SubAgentModel(ModelWorkflowFramework):
    def __init__(self, llm: LLM, context_id: str, tools_manager: ToolsManager):
        """
        初始化子代理模型

        参数:
        - llm: LLM 模型实例
        - context_id: 子代理上下文 ID
        - tools_manager: 工具管理器实例
        """
        super().__init__(
            llm,
            context_id=f"sub_agent_{context_id}",
            tools_manager=tools_manager,
            system_prompt=SUB_AGENT_SYSTEM_PROMPT,
        )

    def forward(self, task: str):
        """
        运行子代理任务

        参数:
        - task: 子代理要执行的任务描述

        返回:
        - 运行子代理任务
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
            tool_name="sub_agent",
            description="子代理，运行在独立上下文中的子代理，用于高效处理特定任务",
            params_dict={
                "task": (
                    "array",
                    "要执行的任务描述数组，形式为: [sub_task1, sub_task2, sub_task3]",
                )
            },
        )  # 初始化父类工具

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
        task_list, error = _parse_tasks(task, "SubAgent")
        if error is not None:
            return error
        from . import multiprocessing

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
                    parent_connection, child_connection = process_context.Pipe(
                        duplex=True
                    )
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
                            succeeded, tool_result = (
                                pending_tool_call.result_queue.get_nowait()
                            )
                        except queue.Empty:
                            pass
                        else:
                            message_kind = "tool_result" if succeeded else "tool_error"
                            try:
                                current.connection.send((message_kind, tool_result))
                            except (
                                BrokenPipeError,
                                EOFError,
                                OSError,
                                TypeError,
                                pickle.PickleError,
                            ):
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
                    elif (
                        current.pending_tool_call is None and current.connection.poll()
                    ):
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
                            result_queue: queue.Queue[tuple[bool, object]] = (
                                queue.Queue(maxsize=1)
                            )
                            current.pending_tool_call = _PendingToolCall(result_queue)
                            threading.Thread(
                                target=_execute_parent_tool,
                                args=(self.tools_manager, call_info, result_queue),
                                daemon=True,
                                name=f"satrap-sub-agent-tool-{index}",
                            ).start()
                        elif message_kind == "final" and isinstance(payload, tuple):
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
                                results_dict[index] = (
                                    f"子代理{index}执行失败: {result}\n"
                                )
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
