"""
编程审批的不可变请求与单次决定

人工回填和模型审查共用精确动作快照, 请求绑定会话, 轮次与权限版本
本模块只定义数据契约, 不执行工具或扩大宿主权限
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
import hashlib
from typing import Literal, cast
import json
import time
import uuid

ApprovalMode = Literal["user", "auto", "full-access"]
APPROVAL_MODES: tuple[ApprovalMode, ...] = ("user", "auto", "full-access")
Decision = Literal["allow", "deny", "ask", "error"]


@dataclass(frozen=True)
class ApprovalRequest:
    """冻结一次实际操作, JSON 文本避免审批期间被嵌套参数修改"""

    request_id: str
    session_id: str
    turn_id: str
    actor_id: str
    tool: str
    operation: str
    risk: int
    description: str
    mode: ApprovalMode
    revision: int
    action_json: str
    evidence_json: str
    expires_at: float

    @classmethod
    def create(cls, *, session_id: str, turn_id: str, actor_id: str, tool: str,
               operation: str, risk: int, description: str, mode: ApprovalMode,
               revision: int, action: dict[str, object], evidence: list[dict[str, object]]) -> ApprovalRequest:
        """
        冻结动作和相关对话证据, 为每次审批分配独立请求 ID

        参数:
        - session_id: 宿主会话标识
        - turn_id: 当前来源轮次, 本地直接调用可为空
        - actor_id: 可信调用者标识, 不从模型参数读取
        - tool: 实际工具名
        - operation: 风险策略使用的操作类别
        - risk: 已分类风险级别
        - description: 面向用户的操作说明
        - mode: 创建请求时的审批模式
        - revision: 创建请求时的权限版本
        - action: 实际路径, 修改内容或固定命令参数
        - evidence: 有界的相关对话和工具证据, 不包含隐藏思考

        返回:
        - 有效期十分钟的不可变请求, 数据不可序列化时抛出异常
        """
        return cls(uuid.uuid4().hex, session_id, turn_id, actor_id, tool, operation, risk,
                   description, mode, revision,
                   json.dumps(action, ensure_ascii=False, sort_keys=True),
                   json.dumps(evidence, ensure_ascii=False), time.time() + 600)

    @property
    def digest(self) -> str:
        """
        取得绑定身份, 模式与具体动作的摘要

        返回:
        - 单次请求的 SHA-256 摘要, 不接受相似操作复用
        """
        return hashlib.sha256(json.dumps(asdict(self), ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()

    def payload(self) -> dict[str, object]:
        """
        为审批展示生成结构化请求副本

        返回:
        - 包含完整动作和证据的字典, 修改副本不影响原请求
        """
        value: dict[str, object] = dict(asdict(self))
        value.pop("action_json")
        value.pop("evidence_json")
        value["action"] = cast(dict[str, object], json.loads(self.action_json))
        value["evidence"] = cast(list[dict[str, object]], json.loads(self.evidence_json))
        value["digest"] = self.digest
        return value


@dataclass(frozen=True)
class ApprovalResult:
    """只对一个请求有效的结构化决定, 不创建持久放行规则"""

    request_id: str
    digest: str
    decision: Decision
    reason: str
    reviewer: Literal["user", "model", "policy"]

    @classmethod
    def for_request(cls, request: ApprovalRequest, decision: Decision, reason: str,
                    reviewer: Literal["user", "model", "policy"]) -> ApprovalResult:
        """
        将一个决定绑定到精确请求

        参数:
        - request: 实际等待审批的请求
        - decision: allow, deny, ask 或 error
        - reason: 可返回调用方的审查依据
        - reviewer: user, model 或 policy

        返回:
        - 不能用于其它请求的不可变结果
        """
        return cls(request.request_id, request.digest, decision, reason, reviewer)
