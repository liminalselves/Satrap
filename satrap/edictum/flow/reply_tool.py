"""
平台回复节点的 flow_reply 工具与后台发送通道

flow_reply 由宿主注入到连着平台回复节点的 WF, 参数 {text, quote_trigger?};
发送路径按有无来源消息选择: 有来源走 ReplyTurn, 无来源走 ScheduledTarget;
后台发送的发送证据写入 platform.db 的 flow_send_log 表
"""
from __future__ import annotations

from satrap.core.config.platform_messages import MessageScope
from satrap.core.platform.receipt import SendReceipt
from satrap.core.platform.scheduled import ScheduledTarget
from satrap.core.utils.TCBuilder import AsyncTool

from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable
import json
import sqlite3
import time
import uuid

from satrap.core.log import logger


@dataclass(frozen=True)
class FlowSendPath:
    """发送路径选择结果"""

    foreground: bool
    """有来源消息 (ReplyTurn 可用)"""
    target: Any = None
    """后台发送时的 ScheduledTarget 冻结对象"""


class AsyncFlowReplyTool(AsyncTool):
    """宿主注入的 flow_reply: 模型调用即发送, 不调用即沉默"""

    tool_name = "flow_reply"
    description = "把文本发送到当前对话; 不调用即不回复 (沉默)"
    params_dict = {
        "text": ("string", "要发送的文本"),
        "quote_trigger": ("bool", "是否引用触发消息, 默认 false; 后台发送时忽略"),
    }

    def __init__(self, send: Callable[[str, bool], Awaitable[bool]], quote: bool = False) -> None:
        """
        初始化回复工具

        参数:
        - send: 实际发送回调 (text, quote_trigger) -> 是否成功
        - quote: 默认引用设置 (模型可逐次覆盖)
        """
        super().__init__()
        self._send = send
        self._quote = quote
        self.config: dict[str, Any] = {}
        self.called = False
        """模型是否已调用过, 宿主据此区分沉默与已回复"""

    async def execute(self, text: str, quote_trigger: bool | None = None) -> dict[str, Any]:
        """
        发送回复文本

        参数:
        - text: 发送内容
        - quote_trigger: 引用触发消息; None 用工具默认

        返回:
        - {ok, sent}; 发送失败返回 ok=False, 不算 WF 失败由宿主决定
        """
        if not text.strip():
            return {"ok": False, "error": "发送内容为空"}
        self.called = True
        sent = await self._send(text, self._quote if quote_trigger is None else quote_trigger)
        return {"ok": sent, "sent": sent}


class FlowSendRecorder:
    """
    流程后台发送的 ScheduledRecorder: 把发送证据落进 platform.db 的 flow_send_log

    每次尝试一行 (attempt_id 主键), 分段状态与最终结算写同一行的
    segments_json/status, 供事后核查"这次后台发送到底发没发出去"
    """

    def __init__(self, database: Path | str, scope_key: str, attempt_id: str = "") -> None:
        """
        初始化记录器

        参数:
        - database: platform.db 路径
        - scope_key: 目标对话键
        - attempt_id: 本次尝试 id; 缺省生成
        """
        self.database = Path(database)
        self.scope_key = scope_key
        self.attempt_id = attempt_id or f"flow-{uuid.uuid4().hex[:16]}"
        self._created = False

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database.as_uri() + "?mode=rwc", uri=True, timeout=5)
        from satrap.core.config.platform_schema import ensure_platform_tables
        ensure_platform_tables(connection)
        connection.commit()
        return connection

    def _write(self, status: str, segments: list[dict[str, Any]]) -> bool:
        """把一行证据原子写回; 写失败返回 False 让发送方停下"""
        try:
            now = time.time()
            with closing(self._connect()) as connection, connection:
                connection.execute(
                    "INSERT INTO flow_send_log(attempt_id, scope_key, status, segments_json, created_at, updated_at) "
                    "VALUES(?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(attempt_id) DO UPDATE SET status=excluded.status, "
                    "segments_json=excluded.segments_json, updated_at=excluded.updated_at",
                    (self.attempt_id, self.scope_key, status,
                     json.dumps(segments, ensure_ascii=False), now if not self._created else now, now),
                )
            self._created = True
            return True
        except Exception as error:
            logger.warning(f"[流程发送] 发送证据落库失败 attempt={self.attempt_id}: {error}")
            return False

    def plan(self, target: ScheduledTarget, segments: list[dict[str, Any]]) -> bool:
        """登记分段计划并取得发送权"""
        rows = [{"index": i, "status": "planned", "chars": len(str(s.get("text") or s))}
                for i, s in enumerate(segments)]
        return self._write("planned", rows)

    def submitted(self, index: int) -> bool:
        """登记某段提交标记"""
        try:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    "SELECT segments_json FROM flow_send_log WHERE attempt_id=?",
                    (self.attempt_id,),
                ).fetchone()
            segments = json.loads(row[0]) if row else []
            if 0 <= index < len(segments):
                segments[index]["status"] = "submitted"
            return self._write("sending", segments)
        except Exception as error:
            logger.warning(f"[流程发送] 段 {index} 提交标记落库失败: {error}")
            return False

    def result(self, index: int, receipt: Any) -> bool:
        """登记某段回执"""
        try:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    "SELECT segments_json FROM flow_send_log WHERE attempt_id=?",
                    (self.attempt_id,),
                ).fetchone()
            segments = json.loads(row[0]) if row else []
            if 0 <= index < len(segments):
                segments[index]["status"] = getattr(receipt, "status", "unknown")
                ids = getattr(receipt, "message_ids", None)
                if ids:
                    segments[index]["message_ids"] = list(ids)
            return self._write("sending", segments)
        except Exception as error:
            logger.warning(f"[流程发送] 段 {index} 回执落库失败: {error}")
            return False

    def complete(self, receipt: Any) -> bool:
        """整体结算"""
        try:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    "SELECT segments_json FROM flow_send_log WHERE attempt_id=?",
                    (self.attempt_id,),
                ).fetchone()
            segments = json.loads(row[0]) if row else []
            return self._write(getattr(receipt, "status", "unknown"), segments)
        except Exception as error:
            logger.warning(f"[流程发送] 结算落库失败: {error}")
            return False


# 兼容旧名: 早期版本的内存记录器已被落库实现替代
_FlowScheduledRecorder = FlowSendRecorder


async def send_flow_reply(
    *,
    send_scheduled: Callable[[MessageScope, str], Awaitable[Any]] | None,
    scope: MessageScope | None,
    text: str,
    foreground_sender: Callable[[str], Awaitable[bool]] | None = None,
    quote_trigger: bool = False,
) -> bool:
    """
    按有无来源消息选择发送路径

    参数:
    - send_scheduled: 后台发送回调 (scope, text) -> receipt; 无来源消息时使用
    - scope: 当前对话范围; 后台发送必须有值
    - text: 发送文本
    - foreground_sender: 有来源消息时的前台发送回调; 缺省只能走后台
    - quote_trigger: 是否引用触发消息, 仅前台生效

    返回:
    - 是否已发出
    """
    if foreground_sender is not None:
        return await foreground_sender(text)
    if send_scheduled is None or scope is None:
        return False
    receipt = await send_scheduled(scope, text)
    return getattr(receipt, "status", "") == "success"
