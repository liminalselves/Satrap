"""
宿主操作的三档审批策略与版本状态

user 由宿主人工批准, auto 由独立审查上下文决定, full-access 跳过审批
计划模式和硬拒绝优先, 旧模式与宽泛规则只备份迁移, 不静默扩大权限
"""
from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from enum import IntEnum
import json
import time

from satrap.core.approval.contracts import APPROVAL_MODES, ApprovalMode, ApprovalRequest, ApprovalResult
from satrap.core.utils.paths import get_data_dir

from satrap.core.log import logger


class RiskLevel(IntEnum):
    """操作风险提示, FORBIDDEN 表示固定硬拒绝"""

    READ = 0
    WRITE = 1
    HIGH = 2
    FORBIDDEN = 3


class PermissionDecision(IntEnum):
    """固定策略决定, ASK 继续交给人工或模型审查"""

    ALLOW = 0
    DENY = 1
    ASK = 2


_FILE_LOCK = threading.RLock()
DEFAULT_RULES_FILE = get_data_dir() / "permissions" / "permissions.json"
DEFAULT_LOG_FILE = get_data_dir() / "permissions" / "approval_log.jsonl"


class PermissionEngine:
    """会话内共享审批状态, 不缓存操作类别级的执行授权"""

    def __init__(self, mode: str = "user", rules_file: str | Path | None = None,
                 log_file: str | Path | None = None) -> None:
        """
        加载三档审批状态并保守迁移旧规则

        参数:
        - mode: 新引擎默认模式, 只接受 user, auto, full-access
        - rules_file: 持久状态文件, 默认使用宿主审批目录
        - log_file: 不含动作正文的审计日志文件
        """
        if mode not in APPROVAL_MODES:
            raise ValueError("审批模式必须为 user, auto 或 full-access")
        self.mode: ApprovalMode = APPROVAL_MODES[APPROVAL_MODES.index(mode)]
        self.plan_mode = False
        self.revision = 0
        self.approval_model = ""
        self.migration_notice = ""
        self.rules_file = Path(rules_file or DEFAULT_RULES_FILE)
        self.log_file = Path(log_file or DEFAULT_LOG_FILE)
        self._lock = threading.RLock()
        self._load()

    def _load(self) -> None:
        """读取持久模式, 旧格式备份后回到 user, 不恢复操作类别级规则"""
        with _FILE_LOCK:
            try:
                original = self.rules_file.read_bytes()
                data = json.loads(original)
                if not isinstance(data, dict):
                    raise ValueError("审批配置必须是对象")
                if data.get("version") != 2:
                    backup = self.rules_file.with_suffix(".legacy-v1.json")
                    if not backup.exists():
                        backup.write_bytes(original)
                    self.mode = "user"
                    self.migration_notice = "旧审批模式和宽泛规则已备份, 请明确选择 user/auto/full-access"
                    self._save()
                    logger.warning("[审批] 旧审批状态已保守迁移为 user")
                elif data.get("mode") in APPROVAL_MODES:
                    self.mode = APPROVAL_MODES[APPROVAL_MODES.index(data["mode"])]
                else:
                    raise ValueError("持久审批模式无效")
            except FileNotFoundError:
                logger.debug("[审批] 尚无审批状态文件, 使用默认模式")
            except (OSError, ValueError, TypeError) as error:
                self.mode = "user"
                logger.warning(f"[审批] 审批配置读取失败, 使用 user: {error}")

    def _save(self) -> None:
        """原子保存版本化模式, 不保存动作级批准或旧规则"""
        self.rules_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.rules_file.with_suffix(".tmp")
        temporary.write_text(json.dumps({"version": 2, "mode": self.mode}, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.rules_file)

    def set_mode(self, mode: str, persist: bool = True, *, expected_revision: int | None = None) -> None:
        """
        显式切换审批模式并使正在等待的旧请求失效

        参数:
        - mode: user, auto 或 full-access, 不接受旧名称
        - persist: 默认 True 持久保存, False 仅修改当前实例
        - expected_revision: 可选的读取版本, 在状态锁内比较以拒绝并发覆盖
        """
        if mode not in APPROVAL_MODES:
            raise ValueError("审批模式必须为 user, auto 或 full-access")
        with _FILE_LOCK, self._lock:
            if expected_revision is not None and self.revision != expected_revision:
                raise ValueError("审批状态已变化, 请刷新后重试")
            previous = self.mode
            self.mode = APPROVAL_MODES[APPROVAL_MODES.index(mode)]
            try:
                if persist:
                    self._save()
            except OSError:
                self.mode = previous
                raise
            self.revision += 1

    def set_plan_mode(self, enabled: bool) -> None:
        """
        修改计划模式并撤销正在等待的旧请求

        参数:
        - enabled: True 禁止文件修改与任意 Shell
        """
        with self._lock:
            self.plan_mode = bool(enabled)
            self.revision += 1

    def set_approval_model(self, name: str) -> None:
        """
        更新审批模型并撤销使用旧配置创建的请求

        参数:
        - name: 专用审批模型名称, 空字符串表示使用当前会话模型
        """
        with self._lock:
            if name != self.approval_model:
                self.approval_model = name
                self.revision += 1

    def is_plan_mode_block(self, operation: str, risk: RiskLevel | int) -> bool:
        """
        判断计划模式是否禁止当前动作

        参数:
        - operation: file_write, file_delete 或 shell 等操作类别
        - risk: 已分类风险级别

        返回:
        - 写类操作或任意 Shell 在计划模式下返回 True
        """
        return self.plan_mode and operation in {"file_write", "file_delete", "shell"} and (
            operation == "shell" or int(risk) > 0)

    def evaluate(self, operation: str, risk: RiskLevel | int, description: str = "") -> PermissionDecision:
        """
        执行同步异步共用的固定规则, 不在状态锁内调用模型

        参数:
        - operation: 当前动作类别
        - risk: 风险级别, FORBIDDEN 始终拒绝
        - description: 兼容调用方的动作摘要, 不作为授权证据

        返回:
        - 硬拒绝和计划模式返回 DENY, full-access 或普通读取返回 ALLOW, 其它返回 ASK
        """
        with self._lock:
            if int(risk) >= RiskLevel.FORBIDDEN or self.is_plan_mode_block(operation, risk):
                return PermissionDecision.DENY
            if self.mode == "full-access" or operation != "shell" and int(risk) <= RiskLevel.READ:
                return PermissionDecision.ALLOW
            return PermissionDecision.ASK

    def valid(self, request: ApprovalRequest) -> bool:
        """
        复核审批期间的权限与有效期

        参数:
        - request: 等待后即将执行的请求

        返回:
        - 模式, 版本和计划状态仍匹配且未过期时返回 True
        """
        return (request.mode == self.mode and request.revision == self.revision
                and time.time() < request.expires_at and not self.is_plan_mode_block(request.operation, request.risk))

    def record(self, request: ApprovalRequest, result: ApprovalResult) -> None:
        """
        记录单次决定, 不记录文件正文和完整对话

        参数:
        - request: 实际操作请求
        - result: 宿主或审查模型的决定
        """
        try:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            entry = {"ts": datetime.now().isoformat(), "request_id": request.request_id,
                     "session_id": request.session_id, "turn_id": request.turn_id, "actor_id": request.actor_id,
                     "operation": request.operation, "risk": request.risk, "revision": request.revision, "tool": request.tool,
                     "mode": request.mode, "action_digest": request.digest, "decision": result.decision,
                     "reviewer": result.reviewer, "reason": result.reason[:1000]}
            with self._lock, self.log_file.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as error:
            logger.warning(f"[审批] 审批日志写入失败: {error}")
