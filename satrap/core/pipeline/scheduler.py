"""
平台事件处理管线调度器

将平台消息转换为用户调用并交给目标会话执行,
统一应用限流, 超时, 错误反馈和执行前后处理钩子
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
import asyncio
from time import monotonic, time
import inspect
from typing import (
    Any,
    Awaitable,
    Callable,
    List,
    cast,
)

from satrap.core.framework.SessionManager import SessionManager
from satrap.core.config.platform_policy import normalize_group_whitelist, policy_default
from satrap.core.config.wake_overrides import resolve_wake_settings
from satrap.core.framework.UserManager import UserManager
from satrap.core.pipeline.rate_limiter import RateLimiter
from satrap.core.platform.event import MessageChain, MessageEvent
from satrap.core.components import PlatformComponentType
from satrap.core.conversation import ConversationRoute
from satrap.core.pipeline.wake_policy import WakeDecision, evaluate_wake
from satrap.core.pipeline.wake_window import PendingText, WakeWindow
from satrap.core.pipeline.wake_timers import WakeTimers
from satrap.core.pipeline.attachments import AsrResolver, resolve_attachments
from satrap.core.pipeline.input_projection import media_sources, project_input, resolve_forwards, resolve_quotes
from satrap.core.pipeline.manual_wake import ManualWakeRequests, ManualWakeTicket
from satrap.core.pipeline.manual_wake_store import ManualWakeStore, ManualWakeStoreError, SendAttemptRecord
from satrap.core.pipeline.request_diagnostics import RequestDiagnostic, RequestDiagnosticLog
from satrap.core.platform import PlatformAdapter
from satrap.core.type import UserCall, safe_getattr, safe_getattr_str

from satrap.core.log import logger


_RECEIPT_TO_REQUEST_STATUS = {"success": "sent", "partial": "partial", "failed": "failed", "unknown": "unknown"}
"""手动请求终态取自发送回执, 错误分支的 detail 优先于反馈消息回执"""

_SETTLEMENT_WAIT_SECONDS = 1.5
"""发送收尾等待上限: 超时先按当时证据归并, 记录保持未确认可被后续确认精化"""
_SETTLEMENT_POLL_SECONDS = 0.05
"""发送收尾等待轮询间隔"""


class PipelineScheduler:
    """
    消息管线调度器

    接收 MessageEvent, 依次执行:
    来源范围与 preprocessor 链
    停止状态与权限检查
    唤醒词/@ 检查与模型调用限流
    会话路由与 LLM 请求 (超时保护)
    后处理: 兜底发送回复
    """
    def __init__(
        self,
        session_manager: SessionManager,
        rate_limiter: RateLimiter | None = None,
        llm_timeout: float = 120.0,
        error_feedback: bool = True,
        user_manager: UserManager | None = None,
    ):
        """
        参数:
        - session_manager: 会话管理器实例
        - rate_limiter: 可选的限流器, 传 None 禁用限流
        - llm_timeout: LLM 调用超时(秒), 默认 120
        - error_feedback: 出错/限流时是否发送反馈消息给用户
        - user_manager: 用户管理器, 传 None 禁用上下文路由
        """
        self.session_manager = session_manager
        self.rate_limiter = rate_limiter
        self.llm_timeout = llm_timeout
        self.error_feedback = error_feedback
        self.user_manager = user_manager
        self.preprocessors: List[Callable[[MessageEvent], Awaitable[bool] | bool]] = []
        self._session_turns: dict[tuple[int, str], tuple[asyncio.Lock, int]] = {}
        self.platform_runtimes: dict[str, tuple[SessionManager, UserManager]] = {}
        self.wake_window = WakeWindow()
        self.wake_timers = WakeTimers(self.wake_window)
        self.manual_wakes = ManualWakeRequests()
        self.manual_wake_store: ManualWakeStore | None = None
        self.request_diagnostics = RequestDiagnosticLog()
        self.asr_resolver: AsrResolver | None = None

    async def clear_manual_wakes(self, adapter_id: str) -> None:
        """
        撤销平台残留手动请求并把持久化记录推进到停止语义

        参数:
        - adapter_id: 平台实例 ID

        持久化回写在线程池执行: 文件锁争用最多等一个锁超时, 不能在事件循环里同步等待;
        回写失败只告警, 存储不可用不阻断平台停止与热重载
        """
        self.manual_wakes.clear_adapter(adapter_id)
        store = self.manual_wake_store
        if store is not None:
            try:
                await asyncio.to_thread(store.adapter_stopped, adapter_id)
            except (ManualWakeStoreError, OSError) as error:
                logger.warning(
                    f"[PipelineScheduler] 平台停止状态未落盘 adapter={adapter_id}: {type(error).__name__}",
                )
        self.request_diagnostics.clear_adapter(adapter_id)

    def _record_diagnostic(
        self, event: MessageEvent, stage: str, status: str, *, reason_code: str = "", reason: str = "",
        decision: str = "", attachments: str = "", notes: str = "", turn_id: str = "", send_status: str = "",
    ) -> None:
        """
        在决策/限流/补全/模型/发送各阶段就地采集诊断

        参数:
        - event: 当前事件, 定位字段取自冻结的 call_origin
        - stage: wake_decision/rate_limit/projection/model/send
        - status: ok/partial/failed/unknown/dropped/skipped
        - reason_code: 脱敏原因码, 与展示用 reason 分开
        - reason: 展示用原因
        - decision: 判定结果, 缺省与 status 一致
        - attachments: 附件失败或跳过类型摘要
        - notes: 截断等说明
        - turn_id: 关联的发送尝试标识
        - send_status: 发送回执状态

        采集只写进程内环形结构, 不执行阻塞磁盘操作, 失败不影响管线执行
        """
        try:
            origin = event.call_origin
            self.request_diagnostics.record(RequestDiagnostic(
                adapter_id=origin.adapter_id, session_id=event.session_id, actor_id=event.get_sender_id(),
                stage=stage, decision=decision or status, reason=reason, recorded_at=time(), status=status,
                reason_code=reason_code, attachments=attachments, notes=notes, turn_id=turn_id,
                send_status=send_status, message_id=origin.source_message_id, request_id=origin.request_id,
                self_id=origin.self_id,
            ))
        except Exception as error:
            logger.debug(f"[PipelineScheduler] 诊断采集失败 stage={stage}: {type(error).__name__}")

    def _record_rejection(self, event: MessageEvent, stage: str, reason: str, *, send_status: str = "",
                          reason_code: str = "") -> None:
        """在决策/限流拒绝点就地采集环形记录, 采集失败不影响管线执行"""
        self._record_diagnostic(
            event, stage, "dropped", reason_code=reason_code or stage, reason=reason,
            decision="dropped", send_status=send_status,
        )

    async def _update_manual_request(
        self, event: MessageEvent, ticket: ManualWakeTicket, status: str, detail: str, *, refine: bool = False,
    ) -> None:
        """持久化手动请求状态, 落盘失败不阻断业务但必须对运维可见"""
        store = self.manual_wake_store
        if store is None:
            return
        adapter_id = event.call_origin.adapter_id
        try:
            outcome = await asyncio.to_thread(
                store.update_request, adapter_id, ticket.request_id, status, detail, refine=refine,
            )
        except Exception as error:
            logger.warning(
                f"[PipelineScheduler] 手动请求状态回写异常 adapter={adapter_id} "
                f"request_id={ticket.request_id} status={status}: {type(error).__name__}",
            )
            return
        if outcome == "persisted":
            return
        if outcome in {"no_op", "degraded"}:
            # 同目标终态的重复写入与历史 unknown 不改写属预期结论, 降级已在状态转换时记录
            logger.debug(
                f"[PipelineScheduler] 手动请求状态未推进 adapter={adapter_id} "
                f"request_id={ticket.request_id} status={status} outcome={outcome}",
            )
            return
        message = (
            f"[PipelineScheduler] 手动请求状态未落盘 adapter={adapter_id} "
            f"request_id={ticket.request_id} status={status} outcome={outcome}"
        )
        if outcome == "invalid":
            # 状态取值来自管线自身, 非法说明调用点写错
            logger.error(message)
        elif outcome == "missing":
            # 受理过的手动请求必须有记录: 状态推进真的没生效或被过早清理, 不能按正常未推进静默
            logger.warning(f"{message} 记录不存在")
        elif outcome == "conflict":
            # 平台停止等路径已写终态而管线结论不同: 保留原终态不覆盖, 但冲突必须可见
            logger.warning(f"{message} 已有终态与新结论冲突")
        else:
            logger.warning(message)

    async def _adjudicate_manual_request(
        self, event: MessageEvent, ticket: ManualWakeTicket, manual_detail: str | None,
    ) -> tuple[str, str]:
        """
        按同一 request_id 的全部业务发送尝试归并请求终态

        参数:
        - event: 触发本次执行的事件
        - ticket: 已受理的手动唤醒票据
        - manual_detail: 管线错误出口原因, 无错误为 None

        返回:
        - tuple[str, str]: 请求终态与脱敏原因; 已提交但未确认的副作用保守判 unknown,
          不接受"模型超时即失败"覆盖工具已经写出的内容
        """
        store = self.manual_wake_store
        adapter_id = event.call_origin.adapter_id
        # 业务回执只作为进程内补充证据: 错误反馈回执不参与请求结论
        receipt = event.last_business_receipt
        outcome: dict[str, Any] | None = None
        if store is not None:
            outcome = await self._await_send_settlement(store, ticket.request_id, adapter_id)
        if outcome is None or (outcome["attempts"] == 0 and receipt is not None):
            # 存储不可用或平台不记录发送尝试: 按业务回执裁决, 不因缺少段证据而放宽
            if ticket.cancelled:
                return "failed", "cancelled"
            if manual_detail is not None:
                return "failed", manual_detail
            if receipt is None:
                return "failed", "no_response"
            return _RECEIPT_TO_REQUEST_STATUS[receipt.status], receipt.reason
        pending, planned, confirmed, failed = outcome["pending"], outcome["planned"], outcome["confirmed"], outcome["failed"]
        # 取消来源有两类: 事件被外部撤销, 以及本轮执行协程被取消或超时打断
        cancelled = ticket.cancelled or manual_detail == "cancelled"
        reasons = list(outcome["reasons"])
        if cancelled:
            reasons.append("cancelled")
        if manual_detail is not None and manual_detail != "cancelled":
            reasons.append(manual_detail)
        if pending or outcome["legacy_confirmed"]:
            # 有已提交未确认的动作 (或用途未知的历史记录) 一律保守为 unknown
            return "unknown", ";".join(reasons) or "in_flight_unconfirmed"
        if confirmed and (failed or planned or manual_detail is not None):
            return "partial", ";".join([*reasons, "confirmed_prefix"])
        if confirmed:
            return "sent", ";".join(reasons)
        if cancelled:
            return "failed", "cancelled_before_send"
        return "failed", manual_detail or "no_confirmed_send"

    def _record_send_diagnostic(self, event: MessageEvent, manual_detail: str | None) -> None:
        """
        发送收尾阶段诊断: 与补全阶段的失败分开, 展示发送证据而不是覆盖成单一失败

        参数:
        - event: 当前事件
        - manual_detail: 管线错误出口原因, 无错误为 None
        """
        if not event.is_wake:
            # 未被唤醒的事件没有任何发送动作, 决策阶段的记录已经是完整结论
            return
        outcome = None
        store = self.manual_wake_store
        if store is not None:
            try:
                outcome = store.request_send_outcome(event.call_origin.request_id, event.call_origin.adapter_id)
            except Exception as error:
                logger.debug(f"[PipelineScheduler] 发送证据查询失败: {type(error).__name__}")
        receipt = event.last_business_receipt
        if outcome is None or outcome["attempts"] == 0:
            if receipt is None:
                # 既无业务尝试也无业务回执: 本轮没有发出业务内容, 不把管线出口原因冒充成发送失败
                self._record_diagnostic(
                    event, "send", "skipped", reason_code="no_business_send",
                    reason="没有业务发送尝试与回执", notes=manual_detail or "",
                )
                return
            self._record_diagnostic(
                event, "send", _RECEIPT_TO_REQUEST_STATUS[receipt.status], reason_code=receipt.status,
                reason=receipt.reason or "平台不记录发送尝试, 按进程内回执裁决", send_status=receipt.status,
            )
            return
        if outcome["pending"] or outcome["legacy_confirmed"]:
            status, code = "unknown", "in_flight_unconfirmed"
        elif outcome["confirmed"] and (outcome["failed"] or outcome["planned"]):
            status, code = "partial", "confirmed_prefix"
        elif outcome["confirmed"]:
            status, code = "sent", "all_segments_confirmed"
        else:
            status, code = "failed", manual_detail or "no_confirmed_send"
        self._record_diagnostic(
            event, "send", status, reason_code=code,
            reason=f"业务段 已确认 {outcome['confirmed']} 未确认 {outcome['pending']} 失败 {outcome['failed']}",
            turn_id=",".join(cast(list[str], outcome["turn_ids"])),
            notes="|".join(cast(list[str], outcome["reasons"])),
        )

    def _request_attempts(self, event: MessageEvent) -> list[SendAttemptRecord]:
        """本请求已登记的发送尝试, 只读内存状态, 不执行磁盘操作"""
        store = self.manual_wake_store
        if store is None:
            return []
        try:
            return store.lookup_attempts(event.call_origin.request_id, event.call_origin.adapter_id)
        except Exception as error:
            logger.debug(f"[PipelineScheduler] 发送尝试查询失败: {type(error).__name__}")
            return []

    def _request_turn_ids(self, event: MessageEvent) -> str:
        """本请求的发送尝试标识 (最多 4 条), 供诊断关联到具体尝试"""
        return ",".join(item["turn_id"] for item in self._request_attempts(event)[:4])

    def _record_projection(
        self, event: MessageEvent, quote_status: str, forward_status: str,
        attachments: Sequence[Any], projected: Any,
    ) -> None:
        """
        补全与投影阶段诊断: 附件与引用补全结果独立成条, 不与最终发送结果混同

        参数:
        - event: 当前事件
        - quote_status: 引用回源结果
        - forward_status: 转发补全结果
        - attachments: 附件处理结果 (AttachmentResult 序列)
        - projected: 投影结果, 提供 notes 与 attachment_status
        """
        codes: list[str] = []
        resolved = 0
        failed = 0
        for item in attachments:
            status = str(safe_getattr_str(item, "status"))
            kind = str(safe_getattr_str(item, "kind")) or "attachment"
            if status == "resolved":
                resolved += 1
                continue
            reason = str(safe_getattr_str(item, "reason"))
            # 只记录类别, 状态与原因码: 不保存转写内容与文件名
            codes.append(f"{kind}:{status}:{reason or 'none'}")
            if status in {"failed", "too_large", "unsupported"}:
                failed += 1
        for label, status in (("quote", quote_status), ("forward", forward_status)):
            if status not in {"none", "resolved", ""}:
                codes.append(f"{label}:{status}")
        if failed and not resolved:
            verdict = "failed"
        elif failed or any(code.endswith(":skipped:none") for code in codes):
            verdict = "partial"
        elif codes:
            verdict = "skipped"
        else:
            verdict = "ok"
        self._record_diagnostic(
            event, "projection", verdict,
            reason_code=str(safe_getattr_str(projected, "attachment_status")) or "none",
            reason=f"附件已解析 {resolved} 项, 未完成 {len(codes)} 项",
            attachments=",".join(codes[:8]), notes="|".join(getattr(projected, "notes", ()) or ()),
            turn_id=self._request_turn_ids(event),
        )

    async def _await_send_settlement(self, store: ManualWakeStore, request_id: str, adapter_id: str) -> dict[str, Any] | None:
        """
        有界等待发送尝试收尾后再归并, 收尾超时仍返回当时证据

        参数:
        - store: 发送尝试存储
        - request_id: 逻辑请求标识
        - adapter_id: 平台实例 ID

        返回:
        - dict[str, Any] | None: 发送证据; 查询失败返回 None
        """
        deadline = monotonic() + _SETTLEMENT_WAIT_SECONDS
        outcome: dict[str, Any] | None = None
        while True:
            try:
                outcome = await asyncio.to_thread(store.request_send_outcome, request_id, adapter_id)
            except Exception as error:
                # 查询失败会让归并退化为仅按业务回执裁决, 属于需要可见的存储故障
                logger.warning(
                    f"[PipelineScheduler] 发送证据查询失败 request_id={request_id} "
                    f"adapter={adapter_id}: {type(error).__name__}",
                )
                return None
            if outcome["attempts"] == 0 or outcome["pending"] == 0 or monotonic() >= deadline:
                return outcome
            await asyncio.sleep(_SETTLEMENT_POLL_SECONDS)

    def add_preprocessor(self, fn: Callable[[MessageEvent], Awaitable[bool] | bool]):
        """
        添加预处理器, 在权限与唤醒检查前依次调用; 返回 False 则丢弃事件

        参数:
        - fn: 待调用函数, 支持同步或异步, 返回值为可等待对象时自动等待
        """
        self.preprocessors.append(fn)

    def set_platform_runtimes(
        self,
        runtimes: dict[str, tuple[SessionManager, UserManager]],
    ) -> None:
        """
        设置按平台实例隔离的运行时管理器

        参数:
        - runtimes: 平台实例 ID 到会话和用户管理器的映射
        """
        self.platform_runtimes = dict(runtimes)

    async def execute(self, event: MessageEvent) -> None:
        """
        执行完整管线

        参数:
        - event: 消息事件
        """
        manual_detail: str | None = None
        try:
            manual_ticket = self.manual_wakes.tickets.get(event)
            if manual_ticket is not None and manual_ticket.cancelled:
                return
            deadline_ticket = self.wake_timers.tickets.get(event)
            if deadline_ticket is not None and deadline_ticket.cancelled:
                return
            source_platform_id = event.get_platform_id()
            platform_runtime = self.platform_runtimes.get(source_platform_id)
            session_manager = platform_runtime[0] if platform_runtime else self.session_manager
            user_manager = platform_runtime[1] if platform_runtime else self.user_manager

            if event.is_stopped() or not event.call_llm or not self._allows_source(event):
                return
            for processor in self.preprocessors:
                if event.is_stopped() or not event.call_llm:
                    return
                verdict = processor(event)
                if inspect.isawaitable(verdict):
                    verdict = await cast(Awaitable[bool], verdict)
                if not verdict:
                    logger.debug(f"[PipelineScheduler] preprocessor 丢弃事件: {event.session_id}")
                    return

            # Step.1 停止状态和权限先于唤醒及模型额度检查
            if event.is_stopped() or not event.call_llm:
                return
            if not self._allows_source(event) or not await self._check_permission(event):
                return

            # Step.2 评估独立唤醒规则并记录命中原因
            self._apply_wake_policy(event)
            if not event.is_private_chat() and not event.is_wake_up() and event.policy_settings.get("wake_on_quote_self") is True:
                await self._apply_quote_wake(event)
            pending = manual_ticket.snapshot if manual_ticket is not None else ()
            automatic = False
            if manual_ticket is not None:
                event.is_wake = True
            elif not event.is_private_chat() and event.policy_settings.get("wake_mode", policy_default("wake_mode")) in {"frequency", "necessity"} and self._automatic_policy_current(event):
                pending = deadline_ticket.snapshot if deadline_ticket is not None else self.wake_window.observe(event)
                if not event.is_wake_up():
                    decision = self.wake_window.decide(event, pending, deadline=deadline_ticket is not None)
                    event.set_extra("wake_decision", decision)
                    if decision.triggered:
                        automatic = True
                        event.is_wake = True
            if not event.is_private_chat() and not event.is_wake_up():
                decision = event.get_extra("wake_decision")
                if isinstance(decision, WakeDecision):
                    self._record_rejection(
                        event, "wake_decision", f"{decision.rule}: {decision.reason}", reason_code=decision.rule,
                    )
                else:
                    self._record_rejection(
                        event, "wake_decision", "not_woken: 未命中唤醒条件", reason_code="not_woken",
                    )
                if deadline_ticket is None:
                    self.wake_timers.schedule(event)
                else:
                    # 到期复查未触发: 仅冷却中才重排, 且不早于冷却结束, 避免零延迟忙循环
                    if isinstance(decision, WakeDecision) and decision.rule == "cooldown":
                        self.wake_timers.schedule(event, earliest=monotonic() + self.wake_window.cooldown_remaining(event))
                return

            message = event.get_message_str()
            top_components = event.get_messages()
            has_context = any(
                c.type in {PlatformComponentType.Reply, PlatformComponentType.Forward, PlatformComponentType.Record, PlatformComponentType.File}
                for c in top_components
            )
            if not message and not has_context and not media_sources(top_components, "image") and not media_sources(top_components, "video"):
                return

            # Step.3 只有已唤醒且允许处理的请求消耗模型额度
            if self.rate_limiter:
                rate_key = f"{source_platform_id}:{event.session_id}"
                allowed, wait = await self.rate_limiter.check(rate_key)
                if not allowed:
                    logger.debug(
                        f"[PipelineScheduler] 限流丢弃事件: session={event.session_id}, "
                        f"需等待 {wait:.1f}s"
                    )
                    if self.error_feedback:
                        await self._send_feedback(event, "请求频率过高, 请稍后再试")
                    receipt = event.last_send_receipt
                    self._record_rejection(
                        event, "rate_limit", f"请求频率限制, 需等待 {wait:.1f}s", reason_code="rate_limited",
                        send_status=receipt.status if receipt is not None else "",
                    )
                    manual_detail = "rate_limited"
                    return

            # Step.5 通过 UserManager 解析目标会话 (路由不依赖投影, 仍在会话锁外)
            session_id = event.session_id
            route: ConversationRoute | None = None
            settings = event.policy_settings
            scope = str(settings.get("context_scope", "legacy_user")) if not event.is_private_chat() else "legacy_user"
            if scope != "legacy_user" and (not user_manager or not event.session_type):
                raise ValueError("隔离上下文需要 UserManager 和命名会话配置")
            if user_manager and event.session_type:
                platform_id, extra_params = self._resolve_route_adapter(event)
                route = ConversationRoute(
                    user_id=event.get_sender_id(), platform=platform_id,
                    session_type=event.session_type, provider=event.session_provider,
                    scope=scope, self_id=event.get_self_id(), group_id=event.get_group_id(),
                )
                route_args = {"route": route} if scope != "legacy_user" else {}
                resolved = user_manager.resolve_session(
                    user_id=event.get_sender_id(),
                    platform=platform_id,
                    session_provider=event.session_provider,
                    session_type=event.session_type,
                    class_cfg_mgr=safe_getattr(session_manager, 'class_cfg_mgr'),
                    extra_params=extra_params,
                    **route_args,
                )
                if not resolved:
                    logger.warning(f"[PipelineScheduler] 未解析到会话, 消息丢弃 session={event.session_id} provider={event.session_provider}")
                    return
                session_id = resolved

            async with self._session_turn(session_manager, session_id):
                if event.is_stopped() or not event.call_llm or not self._allows_source(event) or not await self._check_permission(event):
                    return
                if automatic and not self._automatic_policy_current(event):
                    return
                if deadline_ticket is not None and deadline_ticket.cancelled:
                    return
                if manual_ticket is not None and manual_ticket.cancelled:
                    return
                batch: tuple[PendingText, ...] = ()
                if pending:
                    batch = self.wake_window.claim(event, pending, automatic, deadline=deadline_ticket is not None)
                    if automatic and not batch:
                        return
                    if manual_ticket is not None and manual_ticket.snapshot and not batch:
                        return
                    if batch:
                        self.wake_timers.cancel_route(event)
                # Step.4 认领成功后才按预算补全引用/转发/附件并投影, 未认领批次不浪费下载与转写
                quote_status = await resolve_quotes(event)
                forward_status = await resolve_forwards(event)
                attachments = await resolve_attachments(event, self.asr_resolver)
                projected = project_input(event, quote_status, forward_status, attachments)
                event.set_extra("input_projection", projected)
                self._record_projection(event, quote_status, forward_status, attachments, projected)
                message, images, videos = projected.message, list(projected.images), list(projected.videos)
                if not message and not images and not videos:
                    manual_detail = "empty_message"
                    return
                user_call = UserCall(
                    session_id=session_id,
                    session_provider=event.session_provider,
                    session_type=event.session_type,
                    message=message,
                    img_urls=images,
                    video_urls=videos,
                    route=route,
                    origin=event.call_origin,
                )
                if batch:
                    window_synthetic = deadline_ticket is not None or (manual_ticket is not None and bool(manual_ticket.snapshot))
                    text_limit = int(event.policy_settings.get("input_text_limit", policy_default("input_text_limit")))
                    window_note: str | None = None
                    if window_synthetic:
                        # 窗口类合成事件 (待处理手动唤醒/定时补偿): projected 正文来自快照拼接或陈旧副本,
                        # 以实际成功 claim 的内容为唯一输入, 不叠加 projected 避免重复整段窗口
                        user_call.message = "\n".join(f"[用户 {item.actor_id}, 消息 {item.message_id}] {item.text}" for item in batch)
                        if len(user_call.message) > text_limit:
                            user_call.message = user_call.message[: text_limit - 1] + "…"
                            window_note = "window_budget_truncated"
                    else:
                        # 真实当前消息: 保留引用/转发/附件补全投影, 批次剔除自身后作为先前窗口上下文追加
                        current_request_id = event.call_origin.request_id
                        others = tuple(item for item in batch if item.request_id != current_request_id)
                        if others:
                            lines = "\n".join(f"- [用户 {item.actor_id}, 消息 {item.message_id}] {item.text}" for item in others)
                            window_block = f"[先前窗口消息 {len(others)} 条:\n{lines}]"
                            # 窗口块消耗投影剩余额度, 分隔符与截断提示计入总量
                            remaining = text_limit - len(user_call.message) - 1 if user_call.message else text_limit
                            if remaining >= 2:
                                if len(window_block) > remaining:
                                    window_block = window_block[: remaining - 1] + "…"
                                    window_note = "window_budget_truncated"
                                user_call.message = f"{user_call.message}\n{window_block}" if user_call.message else window_block
                            else:
                                window_note = "window_budget_dropped"
                    if window_note is not None:
                        event.set_extra("input_projection", replace(projected, notes=(*projected.notes, window_note)))
                # Step.6 执行会话并限制等待时间
                if manual_ticket is not None:
                    # 已受理请求要求发送证据: 记录不可用时工具与回复不得冒充可恢复
                    event.set_extra("require_send_tracking", True)
                    await self._update_manual_request(event, manual_ticket, "executing", "")
                try:
                    response = await asyncio.wait_for(
                        session_manager.handle_call_async(user_call),
                        timeout=self.llm_timeout,
                    )
                except asyncio.TimeoutError:
                    logger.error(f"[PipelineScheduler] LLM 调用超时: {event.session_id}")
                    self._record_diagnostic(
                        event, "model", "unknown", reason_code="llm_timeout", reason="模型调用超时",
                        turn_id=self._request_turn_ids(event),
                    )
                    if self.error_feedback:
                        await self._send_feedback(event, "请求超时, 请稍后重试")
                    manual_detail = "llm_timeout"
                    return
                self._record_diagnostic(
                    event, "model", "ok", reason_code="completed",
                    reason=f"模型输出 {len(response)} 字符" if response else "模型无输出",
                    turn_id=self._request_turn_ids(event),
                )

                if response and not event.has_send_operation():
                    await event.send(MessageChain.from_text(response))
                # ---------- 后处理: 兜底发送回复 ----------
                # 如果 Session 内部已通过 content_callback 发送过消息
                # event.has_send_operation() 返回 True, 避免重复发送

        except asyncio.CancelledError:
            # 取消与超时都可能在发送已经发生之后到达, 记原因后交给 finally 归并真实副作用
            manual_detail = "cancelled"
            raise
        except (ValueError, TypeError, KeyError) as e:
            # 配置或输入结构问题属于运维可见的日志, 不向每条消息的发送者刷反馈
            logger.error(f"[PipelineScheduler] 管线配置或输入错误 session={event.session_id} request={event.call_origin.request_id}: {type(e).__name__}: {e}")
            manual_detail = f"pipeline_input:{type(e).__name__}"
        except Exception as e:
            logger.error(f"[PipelineScheduler] 管线执行错误 session={event.session_id} request={event.call_origin.request_id}: {type(e).__name__}: {e}")
            manual_detail = f"pipeline_error:{type(e).__name__}"
            if self.error_feedback:
                await self._send_feedback(event, "处理失败, 请稍后重试")
        finally:
            self._record_send_diagnostic(event, manual_detail)
            ticket = self.manual_wakes.tickets.get(event)
            if ticket is not None:
                if ticket.status == "pending":
                    ticket.status = "processed"
                if self.manual_wake_store is not None:
                    final_status, final_detail = await self._adjudicate_manual_request(event, ticket, manual_detail)
                    await self._update_manual_request(event, ticket, final_status, final_detail, refine=True)
            event.cleanup_temporary_local_files()

    # ---------- 可覆写钩子 ----------

    @asynccontextmanager
    async def _session_turn(self, manager: SessionManager, session_id: str) -> AsyncIterator[None]:
        """
        按最终会话串行执行模型和回复, 不保留空闲锁

        参数:
        - manager: 隔离的会话管理器
        - session_id: 路由解析后的最终会话 ID

        返回:
        - 持有本轮执行权的异步上下文
        """
        key = (id(manager), session_id)
        existing = self._session_turns.get(key)
        if existing is None:
            lock, users = asyncio.Lock(), 0
        else:
            lock, users = existing
        # get 默认值会每条消息实例化 Lock, 仅首次创建
        self._session_turns[key] = (lock, users + 1)
        try:
            async with lock:
                yield
        finally:
            _, users = self._session_turns[key]
            if users == 1:
                del self._session_turns[key]
            else:
                self._session_turns[key] = (lock, users - 1)

    @staticmethod
    def _allows_source(event: MessageEvent) -> bool:
        """
        检查平台启停与群范围, 包括绕过适配器入口的事件

        参数:
        - event: 待处理事件

        返回:
        - 是否允许进入模型管线
        """
        adapter = event.adapter
        if not isinstance(adapter, PlatformAdapter):
            return True
        if not adapter.config.enable:
            return False
        settings = adapter.config.settings
        if adapter.config.type not in {"onebot", "aiocqhttp"}:
            return True
        if event.is_private_chat():
            return bool(settings.get("enable_private", True))
        groups = normalize_group_whitelist(settings.get("group_whitelist", []))
        return bool(settings.get("enable_group", True)) and (not groups or event.get_group_id() in groups)

    @staticmethod
    def _automatic_policy_current(event: MessageEvent) -> bool:
        """
        防止已失效的自动规则向新窗口提交内容

        参数:
        - event: 携带接收时策略快照的事件

        返回:
        - bool: 自动参与相关配置是否仍与当前实例一致
        """
        if not isinstance(event.adapter, PlatformAdapter):
            return False
        current = resolve_wake_settings(event.adapter.config.settings, event.call_origin.chat_id if not event.is_private_chat() else "")
        keys = {key for key in set(current) | set(event.policy_settings) if key.startswith("wake_") or key == "context_scope"}
        return all(current.get(key) == event.policy_settings.get(key) for key in keys)

    @staticmethod
    async def _apply_quote_wake(event: MessageEvent) -> None:
        """
        用引用回源预算确认被引用者是否为机器人, 是则视为明确唤醒

        参数:
        - event: 未被其他规则唤醒且开启 wake_on_quote_self 的群消息
        """
        if not any(c.type == PlatformComponentType.Reply for c in event.get_messages()):
            return
        status = await resolve_quotes(event)
        reply = next(c for c in event.get_messages() if c.type == PlatformComponentType.Reply)
        if status == "resolved" and safe_getattr_str(reply, "sender_id") == event.call_origin.self_id:
            event.is_wake = True
            event.set_extra("wake_decision", WakeDecision(True, "quote_self", "引用了机器人的消息", event.call_origin.self_id))
        # 回源结果保留在 Reply 字段上, 后续投影不重复请求; 失败或引用他人不改变未唤醒状态

    @staticmethod
    def _apply_wake_policy(event: MessageEvent) -> None:
        """
        根据可信组件设置唤醒标志并保留上游明确标志

        参数:
        - event: 待评估事件, 仅扫描顶层正文与提及
        """
        decision = evaluate_wake(event)
        event.set_extra("wake_decision", decision)
        if decision.triggered and not event.is_private_chat():
            event.is_wake = True

    async def _check_permission(self, event: MessageEvent) -> bool:
        """
        权限检查, 默认通过; 子类可覆写

        参数:
        - event: 事件

        返回:
        - bool: 权限检查, 默认通过; 子类可覆写
        """
        return True

    async def _send_feedback(self, event: MessageEvent, text: str):
        """
        发送反馈消息给用户

        参数:
        - event: 事件
        - text: 待处理文本

        反馈消息按 error_feedback 记录: 不进入业务送达证据, 提示发送成功不改变请求结论
        """
        try:
            await event.send(MessageChain.from_text(text), purpose="error_feedback")
        except Exception as e:
            logger.warning(f"[PipelineScheduler] 发送反馈消息失败: {e}")

    def _resolve_route_adapter(self, event: MessageEvent) -> tuple[str, dict[str, str] | None]:
        """
        解析事件应绑定到哪个适配器实例

        参数:
        - event: 事件

        返回:
        - tuple[str, dict[str, str] | None]: 解析事件应绑定到哪个适配器实例
        """
        source_adapter_id = event.get_platform_id()
        if not source_adapter_id:
            return "", None
        return source_adapter_id, {"adapter_id": source_adapter_id}
