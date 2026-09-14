"""
异步对话上下文管理

复用公共消息编辑逻辑, 负责异步持久化和检查点调用
"""

from __future__ import annotations

from collections.abc import Sequence
import asyncio
from typing import List, Dict, Union, Optional, Any, TYPE_CHECKING
import copy
import json

from satrap.core.utils.vision import build_multimodal_content
from satrap.core.utils.paths import get_db_path
from satrap.core.state import StateStore
from .async_summary import _AsyncSummary
from .utils import _ContextRuntimeState, add_tool_message

from satrap.core.log import logger

if TYPE_CHECKING:
    from satrap.core.APICall.LLMCall import LLM, AsyncLLM
    from .sync import ContextManager
    from .async_ import AsyncContextManager


class AsyncContextManager(_AsyncSummary):
    """异步对话上下文管理器"""

    def __init__(
        self,
        conversation_id: Union[int, str],
        keep_in_memory: bool = False,
        db_path: str = get_db_path(),
        max_context: int = 128000,
        history_ratio: float = 0.7,
        context_threshold: float = 0.8,
        truncation_floor: float = 0.4,
        exceed_process: str = "sliding",
        state_store: Optional[StateStore] = None,
        enable_checkpoint: bool = False,
        auto_checkpoint: bool = True,
        summary_keep_recent_turns: int = 6,
    ):
        """
        初始化异步上下文管理器

        注意: 初始化后请 await manager.initialize() 或使用 async with 语句自动初始化

        参数:
        - conversation_id: 当前对话的唯一 ID
        - keep_in_memory:
            True: 加载数据后在内存操作, 需手动调用 save_context() 写入数据库 <br>
            False: (推荐) 每次修改操作自动同步到数据库, 保证数据不丢失
        - db_path: SQLite 数据库路径, 默认 local 平台的 platform.db
        - max_context: 最大上下文长度, 默认 128k
        - history_ratio: 历史上下文比例, 历史预算 = max_context x history_ratio, 默认 0.7
        - context_threshold: 上下文阈值(占历史预算比例), 触发线 = 历史预算 x context_threshold, 默认 0.8
        - truncation_floor: 上下文截断底线(占历史预算比例), 截断目标 = 历史预算 x truncation_floor, 默认 0.4
        - exceed_process: 超过触发线时的处理方式, 默认 "sliding" (滑动窗口)
            - "sliding": 滑动窗口策略, 删除旧消息, 保持上下文长度在截断底线以下
            - "mid_truncate": 中间截断策略, 从中间截断上下文, 不删除旧消息
            - "summarize": 总结旧轮次并保留最近 summary_keep_recent_turns 轮原文
        - summary_keep_recent_turns: 总结压缩时必须原样保留的最近轮数, 默认 6
        - state_store: 状态检查点存储实例, 传入后启用检查点/回滚/分支能力
        - enable_checkpoint: 为 True 时自动创建指向当前库的 StateStore, 与显式传入 state_store 二选一
        - auto_checkpoint: 启用检查点后, 每次写入用户/机器人消息自动保存稳定检查点 (同水位去重), 默认 True
        """
        if not (0 < truncation_floor < context_threshold <= 1):
            truncation_floor = 0.4
            logger.warning(
                f"截断底线必须小于上下文阈值: truncation_floor({truncation_floor}) < context_threshold({context_threshold})，退回默认值"
            )
        if not (0 < history_ratio <= 1):
            history_ratio = 0.7
            logger.warning(
                f"历史上下文比例必须在 (0, 1] 区间: history_ratio={history_ratio}，退回默认值"
            )

        self.db_path = db_path
        self.conversation_id = str(conversation_id)
        self.keep_in_memory = keep_in_memory
        self.auto_checkpoint = auto_checkpoint

        self.state_store = state_store  # 检查点存储: 显式传入优先, 否则按开关自动创建 (与消息同库, 保证事务原子性)

        if self.state_store is None and enable_checkpoint:
            self.state_store = StateStore(db_path=self.db_path)

        self._messages: List[Dict[str, Any]] = []  # 内存中的消息缓存
        self._runtime_state = _ContextRuntimeState()  # 可重建的总结与 usage 校准状态
        self._runtime_state_dirty = False  # 运行时状态待持久化标记
        self._saved_count = (
            0  # 已持久化到库的消息条数 (增量保存水位, -1 表示需全量重写)
        )
        self.max_context = max_context  # 最大上下文长度
        self.history_ratio = history_ratio  # 历史上下文比例
        self.context_threshold = context_threshold  # 上下文阈值(占历史预算比例)
        self.truncation_floor = truncation_floor  # 上下文截断底线(占历史预算比例)
        self.exceed_process = exceed_process  # 超过触发线时的处理方式
        self.summary_keep_recent_turns = max(
            0, summary_keep_recent_turns
        )  # 总结策略保留轮数

        self.history_budget = int(max_context * history_ratio)
        # 派生值: 滞回截断的触发线/底线/输出预算
        self.trigger_tokens = int(self.history_budget * context_threshold)
        self.floor_tokens = int(self.history_budget * truncation_floor)
        self.output_budget = max_context - self.history_budget
        logger.info(f"[异步上下文管理] 实例已创建，对话 ID: {self.conversation_id}")

    # ================= 核心功能实现 =================

    # ================= 检查点支持 =================

    async def add_user_message(self, message: str, img_urls: list[str] | None = None):
        """
        添加用户消息到上下文中

        参数:
        - message: 消息内容
        - img_urls: 图片 URL 或本地路径列表, 可选
        """
        self._messages.append(
            {"role": "user", "content": build_multimodal_content(message, img_urls)}
        )
        await self._sync()
        await self._maybe_auto_checkpoint()

    async def reset_system_prompt(self, message: str):
        """
        重置系统提示词

        参数:
        - message: 新的系统提示词
        """
        await self._protect_before_edit()
        self._messages = [m for m in self._messages if m.get("role") != "system"]
        # 在开头插入新的系统消息
        self._messages.insert(0, {"role": "system", "content": message})
        self._mark_dirty()
        await self._sync()

    async def add_bot_message(
        self,
        message: str,
        tools_calls: list[dict[str, Any]] | None = None,
        ignore_think: bool = True,
        reasoning: str | None = None,
    ):
        """
        添加机器人消息到上下文中

        参数:
        - message: 消息内容
        - tools_calls: 工具调用信息列表, 可选, 每个元素格式为 {"id": "工具 ID", "type": "function", "function": {"name": "工具名", "arguments": "参数 JSON 字符串"}}
        - ignore_think: 是否忽略消息中的思考过程, 默认True
        - reasoning: 思考过程, 可选
        """
        if tools_calls:
            self._messages.append(
                {
                    "role": "assistant",
                    "content": message,
                    "reasoning_content": (
                        reasoning
                        if reasoning is not None
                        and reasoning != ""
                        and not ignore_think
                        else None
                    ),
                    "tool_calls": tools_calls,
                }
            )
        else:
            self._messages.append(
                {
                    "role": "assistant",
                    "content": message,
                    "reasoning_content": (
                        reasoning
                        if reasoning is not None
                        and reasoning != ""
                        and not ignore_think
                        else None
                    ),
                }
            )

        await self._sync()
        await self._maybe_auto_checkpoint()

    async def add_chat(self, user_message: str, bot_message: str):
        """
        添加一条用户消息和对应的机器人消息到上下文中

        参数:
        - user_message: 用户消息
        - bot_message: 机器人消息
        """
        self._messages.append({"role": "user", "content": user_message})
        self._messages.append({"role": "assistant", "content": bot_message})
        await self._sync()
        await self._maybe_auto_checkpoint()

    async def add_tool_message(self, tool_call_id: str, tool_result: dict[str, Any] | str) -> None:
        """
        添加工具调用结果消息

        参数:
        - tool_call_id: 工具调用 ID
        - tool_result: 工具调用结果, 字典转为 JSON, 字符串原样保存
        """
        add_tool_message(self._messages, tool_call_id, tool_result)
        await self._sync()
        await self._maybe_auto_checkpoint()

    async def add_tool_call_flow(
        self,
        message: str,
        tool_messages: list[dict[str, Any]],
        tool_results: Sequence[dict[str, Any] | str],
    ):
        """
        添加完整的助手工具调用及结果消息流

        参数:
        - message: 助手消息内容
        - tool_messages: 工具调用消息列表
        - tool_results: 字典或字符串结果序列, 与 tool_messages 按顺序配对
        """
        await self.add_bot_message(message, tool_messages)
        for tool_msg, tool_res in zip(tool_messages, tool_results):
            await self.add_tool_message(tool_msg["id"], tool_res)
        await self._sync()

    async def add_at_system_start(self, message: str, separator: str = ""):
        """
        在原系统提示词的开头添加消息(修改第一条系统消息的内容)

        参数:
        - message: 要添加的消息内容
        - separator: 拼接时插入的分隔符, 默认为空字符串
        """
        await self._protect_before_edit()
        # 查找第一条系统消息
        for msg in self._messages:
            if msg.get("role") == "system":
                msg["content"] = message + separator + msg["content"]
                # 在开头拼接新内容
                break
        else:
            self._messages.insert(0, {"role": "system", "content": message})
            # 没有系统消息, 则新建一条并插入到开头
        self._mark_dirty()
        await self._sync()

    async def add_at_system_end(self, message: str, separator: str = ""):
        """
        在原系统提示词的结尾添加消息 (修改第一条系统消息的内容)

        参数:
        - message: 要添加的消息内容
        - separator: 拼接时插入的分隔符, 默认为空字符串
        """
        await self._protect_before_edit()
        # 查找第一条系统消息
        for msg in self._messages:
            if msg.get("role") == "system":
                msg["content"] = msg["content"] + separator + message
                # 在结尾拼接新内容
                break
        else:
            self._messages.insert(0, {"role": "system", "content": message})
            # 没有系统消息, 则新建一条并插入到开头
        self._mark_dirty()
        await self._sync()

    async def add_turn_messages(self, turn_messages: list[dict[str, Any]]):
        """
        添加多条消息到上下文中

        参数:
        - turn_messages: 要添加的消息列表
        """
        serialized: list[dict[str, Any]] = []
        for msg in turn_messages:
            new_msg = copy.deepcopy(msg)
            serialized.append(new_msg)

        self._messages.extend(serialized)
        await self._sync()
        await self._maybe_auto_checkpoint()

    async def del_context(self):
        """删除当前上下文中的所有消息, 保留系统消息"""
        await self._protect_before_edit()
        self._messages = [
            copy.deepcopy(msg) for msg in self._messages if msg.get("role") == "system"
        ]
        self._mark_dirty()
        await self._sync()
        logger.info(f"上下文已清空：{self.conversation_id}, ID: {self.conversation_id}")

    async def del_system_message(self):
        """删除上下文中的系统消息"""
        await self._protect_before_edit()
        self._messages[:] = [
            msg for msg in self._messages if msg.get("role") != "system"
        ]
        self._mark_dirty()
        await self._sync()

    async def del_message(self, index: int):
        """
        删除上下文中指定索引的消息

        参数:
        - index: 消息的索引
        """
        if 0 <= index < len(self._messages) or -len(self._messages) <= index < 0:
            await self._protect_before_edit()
            self._messages.pop(index)
        else:
            logger.error(
                f"[异步上下文管理] 删除消息失败: 索引 {index} 超出范围, ID: {self.conversation_id}"
            )
        self._mark_dirty()
        await self._sync()

    async def del_last_message(self, n: int = 1):
        """
        删除上下文中的最后 n 条消息

        参数:
        - n: 删除的数量
        """
        await self._protect_before_edit()
        for _ in range(n):
            if self._messages:
                self._messages.pop()
        self._mark_dirty()
        await self._sync()

    async def replace_messages(self, messages: list[dict[str, Any]]) -> None:
        """
        异步保存完整上下文快照, 用于回滚及兼容输入更新

        参数:
        - messages: 完整消息快照, 不修改调用方的数据
        """
        self._replace_messages_content(messages)
        await self._sync()

    async def del_last_chat(self, n: int = 1):
        """
        删除上下文中的最后若干组聊天消息

        参数:
        - n: 删除组数, 默认 1, 非正数保留既有行为并删除最后一组
        """
        await self._protect_before_edit()
        self._delete_last_chat_messages(n)
        self._mark_dirty()
        await self._sync()

    async def export_json(self, file_path: str):
        """
        导出当前上下文到 json 文件 (异步文件 IO)

        参数:
        - file_path: 导出路径
        """
        data: dict[str, Any] = {"id": self.conversation_id, "messages": self._messages}
        try:

            def _write_file():
                """在线程中写入上下文文件, 避免阻塞事件循环"""
                with open(file_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=4)

            await asyncio.to_thread(_write_file)
            logger.info(
                f"[异步上下文管理] 成功导出 json 文件：{file_path}, ID: {self.conversation_id}"
            )

        except Exception as e:
            logger.error(
                f"[异步上下文管理] 导出 json 文件失败：{e}, ID: {self.conversation_id}"
            )

    _SUMMARY_PROMPT = (
        "请将以下对话历史浓缩为一段简洁的摘要, 保留关键事实、用户意图、已做的决策和待办事项。"
        "摘要将注入 system prompt 作为后续对话的上下文, 请用第三人称客观描述, 不要遗漏影响后续交互的信息。"
    )


    async def _commit_turn_messages(self, messages: list[dict[str, Any]]) -> None:
        """
        提交成功执行的整轮消息, 保存失败时恢复内存历史

        参数:
        - messages: 本轮完整消息, 包括用户输入与全部模型和工具结果
        """
        previous_count = len(self._messages)
        previous_saved = self._saved_count
        self._messages.extend(messages)
        try:
            if not self.keep_in_memory:
                await self.save_context(raise_on_error=True)
        except BaseException:
            del self._messages[previous_count:]
            self._saved_count = previous_saved
            raise
        await self._maybe_auto_checkpoint()
