from __future__ import annotations
from typing import List, Dict, Optional, Any, TYPE_CHECKING
import copy
from satrap.core.state import StateStore
from satrap.core.type import ContextUsageSnapshot, StateScope
from satrap.core.log import logger
from .utils import TokenEstimateMethod, _ContextRuntimeState, _estimate_request_tokens

if TYPE_CHECKING:
    from satrap.core.APICall.LLMCall import LLM, AsyncLLM


class _ContextCore:
    db_path: str
    conversation_id: str
    keep_in_memory: bool
    auto_checkpoint: bool
    state_store: StateStore | None
    _messages: List[Dict[str, Any]]
    _runtime_state: _ContextRuntimeState
    _runtime_state_dirty: bool
    _saved_count: int
    max_context: int
    history_ratio: float
    context_threshold: float
    truncation_floor: float
    exceed_process: str
    summary_keep_recent_turns: int
    history_budget: int
    trigger_tokens: int
    floor_tokens: int
    output_budget: int

    def _scope(self) -> StateScope:
        """
        当前对话对应的状态作用域

        返回:
        - StateScope: 当前对话对应的状态作用域
        """
        return StateScope(namespace="conversation", scope_id=self.conversation_id)

    def _require_state_store(self) -> StateStore:
        """
        获取状态存储, 未启用时抛出 ValueError

        返回:
        - StateStore: 状态存储, 未启用时抛出 ValueError
        """
        if self.state_store is None:
            raise ValueError(
                "未启用状态检查点, 请传入 state_store 或设置 enable_checkpoint=True"
            )
        return self.state_store

    def get_context(self) -> List[Dict[str, Any]]:
        """
        获取当前上下文中的所有消息

        返回:
        - List[Dict[str, str]]: 消息列表
        """
        return self._messages

    def get_model_context(
        self, method: TokenEstimateMethod = "tokenizer"
    ) -> List[Dict[str, Any]]:
        """
        获取发送给模型的上下文 (保证在最大上下文长度内)

        参数:
        - method: token 估算方法, 同 estimate_token()

        返回:
        - 截断后的消息列表副本, 适合直接用于 API 调用
        """
        return self._apply_truncation(self._messages, method)

    def static_message(self) -> int:
        """
        统计上下文中的消息数量

        返回:
        - int: 消息总数
        """
        return len(self._messages)

    def _group_messages_by_turns(
        self, messages: List[Dict[str, Any]]
    ) -> List[List[Dict[str, Any]]]:
        """
        将消息列表按对话轮次分组
        每组以 user 消息开头, 包含随后的 assistant 和 tool 消息
        第一组可能以 system 消息开头

        参数:
        - messages: 待分组的消息列表

        返回:
        - List[List[Dict]]: 分组后的轮次列表
        """
        turns: List[List[Dict[str, Any]]] = []
        current_turn: List[Dict[str, Any]] = []
        for msg in messages:
            role = msg.get("role")
            if role == "system" and not current_turn:
                current_turn.append(msg)
            elif role == "user":
                if current_turn:
                    turns.append(current_turn)
                current_turn = [msg]
            else:
                current_turn.append(msg)
        if current_turn:
            turns.append(current_turn)
        return turns

    def _flatten_turns(self, turns: List[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
        """
        将分组后的轮次列表还原为扁平消息列表

        参数:
        - turns: 对话轮次

        返回:
        - List[Dict[str, Any]]: 将分组后的轮次列表还原为扁平消息列表
        """
        return [msg for turn in turns for msg in turn]

    def _conversation_turns(
        self, messages: List[Dict[str, Any]]
    ) -> List[List[Dict[str, Any]]]:
        """
        返回不含开头 system 消息的完整对话轮次

        参数:
        - messages: 消息列表

        返回:
        - List[List[Dict[str, Any]]]: 对话轮次
        """
        turns = self._group_messages_by_turns(messages)
        if turns and all(msg.get("role") == "system" for msg in turns[0]):
            turns.pop(0)
        return turns

    def _system_messages(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        提取开头连续的 system 消息副本

        参数:
        - messages: 消息列表

        返回:
        - List[Dict[str, Any]]: system 消息副本
        """
        result: List[Dict[str, Any]] = []
        for message in messages:
            if message.get("role") != "system":
                break
            result.append(copy.deepcopy(message))
        return result

    def _summary_context(
        self, messages: List[Dict[str, Any]], keep_recent_turns: int
    ) -> List[Dict[str, Any]]:
        """
        使用缓存摘要构建非破坏性的模型上下文

        参数:
        - messages: 完整消息及本轮临时消息
        - keep_recent_turns: 原样保留的最近轮数

        返回:
        - List[Dict[str, Any]]: 摘要与最近轮次组成的模型视图
        """
        turns = self._conversation_turns(messages)
        recent = turns[-keep_recent_turns:] if keep_recent_turns > 0 else []
        result = self._system_messages(messages)
        if self._runtime_state.summary:
            result.append(
                {
                    "role": "system",
                    "content": f"<对话历史摘要>\n{self._runtime_state.summary}\n</对话历史摘要>",
                }
            )
        return result + self._flatten_turns(recent)

    def estimate_token(
        self,
        messages: List[Dict[str, Any]] | None = None,
        method: TokenEstimateMethod = "tokenizer",
    ) -> int:
        """
        估计当前上下文中的 token 数量

        参数:
        - messages: 待估算 token 数的消息列表, 默认当前上下文中的所有消息
        - method: 估计方法, 可选值为 "tokenizer" 或 "experience"

        返回:
        - int: token 数量
        """
        if messages is None:
            messages = self._messages
        return _estimate_request_tokens(messages, method)

    def get_context_usage(
        self,
        method: TokenEstimateMethod = "tokenizer",
    ) -> ContextUsageSnapshot:
        """
        返回当前历史预算和最近一次模型 usage 快照

        参数:
        - method: 当前完整历史的 token 估算方法

        返回:
        - ContextUsageSnapshot: 上下文预算与最近一次模型 usage
        """
        return ContextUsageSnapshot(
            history_tokens=self.estimate_token(method=method),
            context_window_tokens=self.max_context,
            reserved_output_tokens=self.output_budget,
            history_upper_tokens=self.history_budget,
            history_lower_tokens=self.floor_tokens,
            last_output_tokens=self._runtime_state.api_output_tokens,
            cache_hit_tokens=self._runtime_state.api_cached_tokens,
            history_token_source=method,
        )

    def estimate_request_tokens(
        self,
        messages: List[Dict[str, Any]],
        method: TokenEstimateMethod = "tokenizer",
        tools: Optional[List[Dict[str, Any]]] = None,
        img_urls: Optional[List[str]] = None,
    ) -> int:
        """
        估算包含工具定义和图片的完整模型请求

        参数:
        - messages: 请求消息
        - method: token 估算方法
        - tools: 工具定义列表
        - img_urls: 额外图片 URL

        返回:
        - int: 请求输入 token 估算值
        """
        return _estimate_request_tokens(messages, method, tools, img_urls)

    def _calibration_factor(self, model: Optional[str]) -> float:
        """
        计算本地估算到同模型真实 API input usage 的校准系数

        参数:
        - model: 当前模型名

        返回:
        - float: 校准系数, 无可信 usage 时为 1
        """
        state = self._runtime_state
        if (
            state.api_input_tokens is None
            or state.estimated_input_tokens is None
            or state.estimated_input_tokens <= 0
            or state.usage_model != model
        ):
            return 1.0
        return max(
            0.1, min(10.0, state.api_input_tokens / state.estimated_input_tokens)
        )

    def _apply_sliding_truncation(
        self,
        messages: List[Dict[str, Any]],
        threshold: int,
        method: TokenEstimateMethod,
    ) -> List[Dict[str, Any]]:
        """
        滑动窗口截断: 保留系统消息, 从最早的对话轮次开始整轮删除, 直到 token 数不超过阈值

        参数:
        - messages: 原始消息列表
        - threshold: token 上限阈值
        - method: 估算方法

        返回:
        - 截断后的新消息列表
        """
        turns = self._group_messages_by_turns(messages)
        if not turns:
            return messages.copy()

        system_turn = None
        # 提取并保留纯系统轮次
        if all(msg.get("role") == "system" for msg in turns[0]):
            system_turn = turns.pop(0)

        truncated_turns = turns[:]
        # 逐轮删除最早的非系统轮次
        while (
            truncated_turns
            and self.estimate_token(
                self._flatten_turns(
                    ([system_turn] if system_turn else []) + truncated_turns
                ),
                method=method,
            )
            > threshold
        ):
            truncated_turns.pop(0)

        result_turns = ([system_turn] if system_turn else []) + truncated_turns
        return self._flatten_turns(result_turns)

    def _apply_mid_truncation(
        self,
        messages: List[Dict[str, Any]],
        threshold: int,
        method: TokenEstimateMethod,
    ) -> List[Dict[str, Any]]:
        """
        保留头尾并按整轮移除中间消息直到满足阈值

        参数:
        - messages: 原始消息列表
        - threshold: 本地估算 token 上限
        - method: token 估算方法

        返回:
        - List[Dict[str, Any]]: 截断后的消息副本
        """
        turns = self._group_messages_by_turns(messages)
        system_turn = None
        if turns and all(msg.get("role") == "system" for msg in turns[0]):
            system_turn = turns.pop(0)
        if len(turns) <= 4:
            logger.debug(
                f"[上下文管理] 轮次过少, 中间截断退化为滑动窗口, ID: {self.conversation_id}"
            )
            return self._apply_sliding_truncation(messages, threshold, method)

        def token_of_turn_list(turn_list: List[List[Dict[str, Any]]]) -> int:
            return self.estimate_token(
                self._flatten_turns(([system_turn] if system_turn else []) + turn_list),
                method=method,
            )

        kept_turns = turns[:]
        while token_of_turn_list(kept_turns) > threshold and len(kept_turns) > 2:
            del kept_turns[len(kept_turns) // 2]
        return self._flatten_turns(([system_turn] if system_turn else []) + kept_turns)

    def _apply_truncation(
        self, messages: List[Dict[str, Any]], method: TokenEstimateMethod = "tokenizer"
    ) -> List[Dict[str, Any]]:
        """
        对消息列表应用截断策略, 返回截断后的新列表 (不修改原列表)

        滞回截断: 未达触发线(trigger_tokens)不处理, 超过则截断到截断底线(floor_tokens),
        触发线与底线之间的缓冲带内前缀稳定, 避免贴线抖动导致服务器 prefix cache 失效

        参数:
        - messages: 待截断的消息列表
        - method: token 估算方法, 同 estimate_token() 参数

        返回:
        - List[Dict]: 截断后的新列表
        """
        current_tokens = self.estimate_token(messages, method=method)
        if current_tokens <= self.trigger_tokens:
            return messages.copy()  # 未达触发线, 无需截断

        logger.debug(
            f"模型上下文达触发线 ({current_tokens} > {self.trigger_tokens})，应用 {self.exceed_process} 截断到 {self.floor_tokens}"
        )

        turns = self._group_messages_by_turns(messages)
        system_turn = None
        if turns and all(msg.get("role") == "system" for msg in turns[0]):
            system_turn = turns.pop(0)

        if self.exceed_process == "sliding":  # 滑动窗口截断
            return self._apply_sliding_truncation(messages, self.floor_tokens, method)

        elif (
            self.exceed_process == "mid_truncate"
        ):  # 中间截断: 保留头部和尾部, 删除中间轮次
            return self._apply_mid_truncation(messages, self.floor_tokens, method)

        else:
            logger.error(
                f"[上下文管理] 未知截断策略 {self.exceed_process}, 返回原列表, ID: {self.conversation_id}"
            )
            return messages.copy()

    _SUMMARY_PROMPT = (
        "请将以下对话历史浓缩为一段简洁的摘要, 保留关键事实、用户意图、已做的决策和待办事项。"
        "摘要将注入 system prompt 作为后续对话的上下文, 请用第三人称客观描述, 不要遗漏影响后续交互的信息。"
    )
