"""子代理任务创建与执行工具"""

from multiprocessing.process import BaseProcess
from dataclasses import dataclass
import pickle
from typing import Any, Protocol, cast
import queue
import json
from satrap.core.APICall.LLMCall import LLM
from satrap.core.utils.TCBuilder import ToolsManager
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
            "supports_visual_input": llm.supports_visual_input,
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
            cast(tuple[object, ...], response) if isinstance(response, tuple) else ()
        )
        if len(response_tuple) == 2 and response_tuple[0] == "tool_result":
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
                "content": json.dumps(
                    {"ok": False, "error": error}, ensure_ascii=False
                ),
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
        from .sync import SubAgentModel

        llm = _restore_llm(llm_payload)
        tools_manager = _ProxyToolsManager(tool_definitions, connection)
        result = SubAgentModel(llm, context_id, tools_manager).forward(task)
        connection.send(("final", (True, str(result))))
    except BaseException as error:
        connection.send(("final", (False, f"{type(error).__name__}: {error}")))
    finally:
        connection.close()


def _parse_tasks(task: str, label: str) -> tuple[list[str], str | None]:
    """统一子任务解析与数量校验"""
    try:
        tasks = json.loads(task)
        if isinstance(tasks, str):
            tasks = [tasks]
        elif not isinstance(tasks, list):
            return [], f"错误：传入的task不是数组，而是 {type(tasks)}"
    except json.JSONDecodeError:
        logger.warning(f"[{label}] 警告：传入的task不是JSON数组，尝试当作单个任务处理")
        tasks = [task]
    if not tasks:
        return [], "未收到任何子任务"
    if len(tasks) > MAX_SUB_AGENT_TASKS:
        return [], f"错误: 子任务数量不能超过 {MAX_SUB_AGENT_TASKS}"
    if not all(isinstance(item, str) and item.strip() for item in tasks):
        return [], "错误: 每个子任务都必须是非空字符串"
    return tasks, None
