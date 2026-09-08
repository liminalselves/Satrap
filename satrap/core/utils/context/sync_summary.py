from __future__ import annotations
from typing import List, Dict, Optional, Any, cast, TYPE_CHECKING
import copy
from satrap.core.type import TokenUsage
from satrap.core.log import logger
from .utils import (
    TokenEstimateMethod,
    ContextStrategy,
    _SUMMARY_PROMPT_VERSION,
    _SUMMARY_RETRY_OUTPUT_TOKENS,
    _summary_output_budget,
    ContextOverflowError,
    PreparedModelContext,
    _llm_model_name,
    _summary_lines,
    _split_summary_chunks,
)

if TYPE_CHECKING:
    from satrap.core.APICall.LLMCall import LLM, AsyncLLM
from .sync_checkpoints import _SyncCheckpoints


class _SyncSummary(_SyncCheckpoints):

    def _call_summary_llm(
        self, llm: LLM, text_chunks: List[str], prior_summary: str
    ) -> str:
        """
        逐块调用同步 LLM 并滚动归并摘要

        参数:
        - llm: 用于总结的同步 LLM
        - text_chunks: 待总结文本块
        - prior_summary: 已有摘要

        返回:
        - str: 新摘要
        """
        summary = prior_summary
        for chunk in text_chunks:
            previous = f"\n\n已有摘要:\n{summary}" if summary else ""
            prompt = f"{self._SUMMARY_PROMPT}{previous}\n\n新增对话:\n{chunk}"
            result = llm.chat(
                [{"role": "user", "content": prompt}],
                thinking="off",
                max_tokens=_summary_output_budget(self.floor_tokens // 2),
            )
            if not isinstance(result, str) or not result.strip():
                logger.warning("[上下文管理] 摘要正文为空, 使用扩展输出预算重试")
                result = llm.chat(
                    [{"role": "user", "content": prompt}],
                    thinking="off",
                    max_tokens=_SUMMARY_RETRY_OUTPUT_TOKENS,
                )
            if not isinstance(result, str) or not result.strip():
                raise RuntimeError("上下文总结模型未返回有效文本")
            summary = result.strip()
        return summary

    def _tighten_summary(self, llm: LLM, target_tokens: int) -> None:
        """
        将缓存摘要进一步压缩到指定目标附近

        参数:
        - llm: 用于压缩的同步 LLM
        - target_tokens: 摘要目标 token 数
        """
        prompt = (
            f"请把以下对话摘要进一步压缩到约 {target_tokens} tokens 以内, "
            "保留关键事实、决定和待办事项:\n\n"
            f"{self._runtime_state.summary}"
        )
        result = llm.chat(
            [{"role": "user", "content": prompt}],
            thinking="off",
            max_tokens=_summary_output_budget(target_tokens),
        )
        if not isinstance(result, str) or not result.strip():
            logger.warning("[上下文管理] 二次压缩正文为空, 使用扩展输出预算重试")
            result = llm.chat(
                [{"role": "user", "content": prompt}],
                thinking="off",
                max_tokens=_SUMMARY_RETRY_OUTPUT_TOKENS,
            )
        if not isinstance(result, str) or not result.strip():
            raise RuntimeError("上下文摘要二次压缩未返回有效文本")
        self._runtime_state.summary = result.strip()
        self._save_runtime_state()

    def summarize_and_compress(self, llm: LLM, keep_recent_turns: int) -> str:
        """
        总结最近 keep_recent_turns 轮之前的对话并缓存, 不修改完整消息历史

        参数:
        - llm: 用于总结的 LLM 实例(调 chat 得字符串)
        - keep_recent_turns: 原样保留的最近对话轮数

        返回:
        - str: 总结文本; 对话轮次不足 keep_recent_turns 时返回空字符串(无可压缩)
        """
        if keep_recent_turns < 0:
            raise ValueError("keep_recent_turns 不能小于 0")
        turns = self._conversation_turns(self._messages)
        if len(turns) <= keep_recent_turns:
            return ""  # 对话轮次不足, 无可压缩

        target_covered = len(turns) - keep_recent_turns
        state = self._runtime_state
        if (
            state.summary_prompt_version != _SUMMARY_PROMPT_VERSION
            or state.covered_turn_count > target_covered
        ):
            self._invalidate_summary()
            state = self._runtime_state

        new_turns = turns[state.covered_turn_count : target_covered]
        if not new_turns:
            return state.summary
        lines = _summary_lines(new_turns)
        chunk_budget = max(1024, min(self.floor_tokens, self.history_budget // 2))
        chunks = _split_summary_chunks(lines, chunk_budget)
        summary = self._call_summary_llm(llm, chunks, state.summary)

        state.summary = summary
        state.covered_turn_count = target_covered
        state.summary_model = _llm_model_name(llm)
        state.summary_prompt_version = _SUMMARY_PROMPT_VERSION
        self._save_runtime_state()
        logger.info(
            f"[上下文管理] 总结缓存更新完成: 覆盖 {target_covered} 轮, "
            f"保留 {keep_recent_turns} 轮完整历史, ID: {self.conversation_id}"
        )
        return summary

    def prepare_model_context(
        self,
        *,
        llm: Optional[LLM] = None,
        pending_messages: Optional[List[Dict[str, Any]]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        img_urls: Optional[List[str]] = None,
        method: TokenEstimateMethod = "tokenizer",
        strategy: Optional[ContextStrategy] = None,
        keep_recent_turns: Optional[int] = None,
    ) -> PreparedModelContext:
        """
        为一次真实模型请求准备非破坏性上下文

        参数:
        - llm: 总结策略使用的 LLM, 同时用于识别校准模型
        - pending_messages: 工具循环中尚未写入历史的临时消息
        - tools: 本次请求携带的工具定义
        - img_urls: 本次请求携带的额外图片
        - method: 本地 token 估算方法
        - strategy: 覆盖当前超限处理策略
        - keep_recent_turns: 总结策略必须原样保留的最近轮数, 默认使用构造参数

        返回:
        - PreparedModelContext: 模型消息副本及 token 元数据
        """
        selected_strategy = strategy or cast(ContextStrategy, self.exceed_process)
        if selected_strategy == "summary":
            selected_strategy = "summarize"
        recent_turns = (
            self.summary_keep_recent_turns
            if keep_recent_turns is None
            else keep_recent_turns
        )
        if recent_turns < 0:
            raise ValueError("keep_recent_turns 不能小于 0")
        model = _llm_model_name(llm)
        messages = copy.deepcopy(self._messages)
        if pending_messages:
            messages.extend(copy.deepcopy(pending_messages))
        original_turns = len(self._conversation_turns(messages))
        original_estimate = self.estimate_request_tokens(
            messages, method, tools, img_urls
        )
        factor = self._calibration_factor(model)
        original_effective = max(0, round(original_estimate * factor))
        prepared_messages = messages

        if original_effective > self.trigger_tokens:
            local_floor = max(1, int(self.floor_tokens / factor))
            if selected_strategy == "summarize":
                if llm is None:
                    raise ValueError("总结压缩策略需要传入 llm")
                self.summarize_and_compress(llm, recent_turns)
                prepared_messages = self._summary_context(messages, recent_turns)
            elif selected_strategy == "sliding":
                prepared_messages = self._apply_sliding_truncation(
                    messages, local_floor, method
                )
            elif selected_strategy == "mid_truncate":
                prepared_messages = self._apply_mid_truncation(
                    messages, local_floor, method
                )
            else:
                raise ValueError(f"未知上下文处理策略: {selected_strategy}")

        prepared_estimate = self.estimate_request_tokens(
            prepared_messages, method, tools, img_urls
        )
        prepared_effective = max(0, round(prepared_estimate * factor))
        compressed = prepared_messages != messages
        if (
            prepared_effective > self.history_budget
            and selected_strategy == "summarize"
            and self._runtime_state.summary
            and llm is not None
        ):
            turns = self._conversation_turns(messages)
            recent = turns[-recent_turns:] if recent_turns > 0 else []
            without_summary = self._system_messages(messages) + self._flatten_turns(
                recent
            )
            fixed_estimate = self.estimate_request_tokens(
                without_summary, method, tools, img_urls
            )
            fixed_effective = max(0, round(fixed_estimate * factor))
            if fixed_effective <= self.history_budget:
                target_tokens = max(
                    64, int((self.history_budget - fixed_effective) / factor * 0.8)
                )
                self._tighten_summary(llm, target_tokens)
                prepared_messages = self._summary_context(messages, recent_turns)
                prepared_estimate = self.estimate_request_tokens(
                    prepared_messages, method, tools, img_urls
                )
                prepared_effective = max(0, round(prepared_estimate * factor))
        if prepared_effective > self.history_budget:
            raise ContextOverflowError(
                f"保留内容仍超过历史上下文预算: {prepared_effective} > {self.history_budget}"
            )
        return PreparedModelContext(
            messages=prepared_messages,
            estimated_input_tokens=prepared_estimate,
            effective_input_tokens=prepared_effective,
            original_estimated_input_tokens=original_estimate,
            token_source="api_calibrated" if factor != 1.0 else method,
            strategy=selected_strategy,
            compressed=compressed,
            original_turns=original_turns,
            prepared_turns=len(self._conversation_turns(prepared_messages)),
            history_budget=self.history_budget,
            trigger_tokens=self.trigger_tokens,
            floor_tokens=self.floor_tokens,
            model=model,
        )

    def record_model_usage(
        self, prepared: PreparedModelContext, usage: Optional[TokenUsage]
    ) -> None:
        """
        保存已完成请求的真实 API usage, 供后续请求校准

        参数:
        - prepared: 本次请求的准备结果
        - usage: API 返回的 token 使用量
        """
        if usage is None:
            self._runtime_state.api_output_tokens = None
            self._runtime_state.api_total_tokens = None
            self._runtime_state.api_cached_tokens = None
            self._save_runtime_state()
            return
        state = self._runtime_state
        state.usage_model = prepared.model
        state.api_input_tokens = usage.input_tokens
        state.estimated_input_tokens = prepared.estimated_input_tokens
        state.api_output_tokens = usage.output_tokens
        state.api_total_tokens = usage.total_tokens
        state.api_cached_tokens = usage.cached_tokens
        self._save_runtime_state()
