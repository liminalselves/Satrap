"""
satrap_coding 权限引擎: 风险分级 x 审批策略 x 规则记忆 + 持久化 + plan mode

策略三档 (set_mode):
- user:      默认, 高风险操作逐条询问用户 (工具层走用户输入通道)
- auto-agent: 模型仅可拒绝或转人工, 不构成写类操作的授权主体
- full:      普通操作直接放行; 本机 Shell 仍需逐次批准

规则优先级: L3 黑名单 > plan mode 限制 > Shell 单次批准 > 持久规则 > 会话内记忆化规则 > 策略

plan mode (/plan): 工作区写操作和任意 Shell 全部拒绝, 文件只读放行
"""
from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, cast
from enum import IntEnum
import json

from satrap.core.utils.paths import get_data_dir

from satrap.core.log import logger


class RiskLevel(IntEnum):
    """操作风险分级: 越大越危险"""

    READ = 0
    """只读, 直接放行"""
    WRITE = 1
    """常规写操作, 默认询问"""
    HIGH = 2
    """高危操作 (删除/环境修改), 必须人工或审批 agent"""
    FORBIDDEN = 3
    """永远拒绝 (黑名单)"""


class PermissionDecision(IntEnum):
    """审批决策结果"""

    ALLOW = 0
    DENY = 1
    ASK = 2
    """需要询问用户 (工具层走用户输入通道)"""


_WRITE_OPERATIONS = frozenset({
    "file_write",
    "file_delete",
    "shell",
})
# plan mode 下被压制的写类操作 (集合)
# 注: 记忆写操作有意不在此列 -- 记忆是元信息, 与工作区写操作隔离,
# 计划模式下仍允许增删改 (由 base_take 插件管理, 见 docs/satrap-coding-plugin.md)

DEFAULT_RULES_FILE = get_data_dir() / "coding" / "permissions.json"
DEFAULT_LOG_FILE = get_data_dir() / "coding" / "approval_log.jsonl"

_FILE_LOCK = threading.Lock()
# 规则文件全局锁: 串行化所有引擎实例的读-改-写, 防多会话交错写丢失更新


class PermissionEngine:
    """操作审批引擎 (线程安全)"""

    def __init__(
        self,
        mode: str = "user",
        rules_file: str | Path | None = None,
        log_file: str | Path | None = None,
    ) -> None:
        """
        参数:
        - mode: 审批策略, user / auto-agent / full
        - rules_file: 持久规则文件 (JSON), 默认 .satrap/coding/permissions.json
        - log_file: 审批日志文件 (JSONL), 默认 .satrap/coding/approval_log.jsonl
        """
        self.mode = mode
        self.plan_mode = False
        self._lock = threading.RLock()
        self._session_rules: dict[str, RiskLevel] = {}
        """会话内记忆化规则: 操作 -> 已批准的最高风险级 (用户批准一次, 会话内同类放行)"""
        self.rules_file = Path(rules_file or DEFAULT_RULES_FILE)
        self.log_file = Path(log_file or DEFAULT_LOG_FILE)
        self._persistent_rules: dict[str, RiskLevel] = self._load_rules()

    # ---------- 规则加载/持久化 ----------

    def _load_rules(self) -> dict[str, RiskLevel]:
        """
        加载持久规则文件 (同时恢复持久化审批策略)

        返回:
        - dict[str, RiskLevel]: 加载持久规则文件 (同时恢复持久化审批策略)
        """
        try:
            raw = json.loads(self.rules_file.read_text(encoding="utf-8"))
            data = cast(dict[str, Any], raw)
            mode = data.get("mode")
            if isinstance(mode, str) and mode in ("user", "auto-agent", "full"):
                self.mode = mode
            rules = data.get("rules", {})
            return {
                str(k): RiskLevel(int(v))
                for k, v in rules.items()
                if isinstance(k, str) and isinstance(v, (int, float))
            }
        except (FileNotFoundError, json.JSONDecodeError, ValueError, TypeError):
            return {}

    def _refresh_from_disk(self) -> None:
        """写前以磁盘为基线重建规则 (需持有 _FILE_LOCK), 防多会话丢失更新"""
        self._persistent_rules = self._load_rules()

    def _save_rules(self) -> None:
        """原子写持久规则"""
        self.rules_file.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "rules": {k: int(v) for k, v in self._persistent_rules.items()},
            "mode": self.mode,
        }
        tmp = self.rules_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.rules_file)

    def _append_log(
        self,
        operation: str,
        risk: RiskLevel,
        description: str,
        decision: PermissionDecision,
        mode: str,
        model_verdict: str | None = None,
    ) -> None:
        """
        追加审批日志 (审计)

        参数:
        - operation: 操作信息
        - risk: 风险级别
        - description: 说明文本
        - decision: 决策
        - mode: 模式
        - model_verdict: auto-agent 返回的原始判断, 未调用模型时为 None
        """
        try:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            entry: dict[str, Any] = {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "operation": operation,
                "risk": int(risk),
                "description": description,
                "decision": decision.name,
                "mode": mode,
            }
            if model_verdict is not None:
                entry["model_verdict"] = model_verdict[:100]
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as e:
            logger.warning(f"[satrap_coding] 审批日志写入失败: {e}")

    # ---------- 规则管理 ----------

    def add_persistent_rule(self, operation: str, level: RiskLevel | int) -> None:
        """
        添加持久规则 (跨会话生效): 指定操作在不超过该风险级时直接放行

        参数:
        - operation: 操作信息
        - level: 级别
        """
        with _FILE_LOCK:
            self._refresh_from_disk()
            with self._lock:
                self._persistent_rules[operation] = RiskLevel(level)
                self._save_rules()

    def remove_persistent_rule(self, operation: str) -> bool:
        """
        移除持久规则

        参数:
        - operation: 操作信息

        返回:
        - bool: 移除持久规则
        """
        with _FILE_LOCK:
            self._refresh_from_disk()
            with self._lock:
                removed = self._persistent_rules.pop(operation, None) is not None
                if removed:
                    self._save_rules()
                return removed

    def list_persistent_rules(self) -> dict[str, int]:
        """
        列出持久规则

        返回:
        - dict[str, int]: 列出持久规则
        """
        return {k: int(v) for k, v in self._persistent_rules.items()}

    def clear_session_rules(self) -> None:
        """清空会话内记忆化规则"""
        with self._lock:
            self._session_rules.clear()

    def approve(self, operation: str, risk: RiskLevel, remember: bool = True) -> None:
        """
        记录一次批准: 记入会话规则 (同类同风险级后续放行)

        参数:
        - operation: 操作信息
        - risk: 风险级别
        - remember: remember 输入值
        """
        with self._lock:
            if remember:
                existing = self._session_rules.get(operation, RiskLevel.READ)
                self._session_rules[operation] = max(existing, risk)

    def deny(self, operation: str, risk: RiskLevel, description: str = "") -> None:
        """
        记录一次拒绝 (仅日志)

        参数:
        - operation: 操作信息
        - risk: 风险级别
        - description: 说明文本
        """
        self._append_log(operation, risk, description, PermissionDecision.DENY, self.mode)

    # ---------- 策略切换 ----------

    def set_mode(self, mode: str, persist: bool = True) -> None:
        """
        切换审批策略: user / auto-agent / full

        参数:
        - mode: 模式
        - persist: 持久化

        persist=True 时随规则文件持久化 (跨会话); /approve all 的会话级放行传 False
        """
        if mode not in ("user", "auto-agent", "full"):
            raise ValueError(f"未知审批策略: {mode}, 可选 user / auto-agent / full")
        with _FILE_LOCK:
            if persist:
                self._refresh_from_disk()
            with self._lock:
                self.mode = mode
                if persist:
                    self._save_rules()

    def set_plan_mode(self, enabled: bool) -> None:
        """
        切换计划模式: 写类操作全部拒绝 (独立标志, 不污染规则状态)

        参数:
        - enabled: 是否启用
        """
        self.plan_mode = bool(enabled)

    def is_plan_mode_block(self, operation: str, risk: RiskLevel | int) -> bool:
        """
        判断操作是否因计划模式的工作区写入限制而被拒绝

        参数:
        - operation: 操作类型
        - risk: 操作风险级别

        返回:
        - 计划模式应阻止该操作时返回 True
        """
        return self.plan_mode and operation in _WRITE_OPERATIONS and (
            operation == "shell" or RiskLevel(risk) > RiskLevel.READ
        )

    # ---------- 评估 ----------

    def evaluate(
        self,
        operation: str,
        risk: RiskLevel | int,
        description: str = "",
        *,
        judge: Callable[[str, RiskLevel, str], str] | None = None,
    ) -> PermissionDecision:
        """
        同步评估操作是否放行

        参数:
        - operation: 操作信息
        - risk: 风险级别
        - description: 说明文本
        - judge: 判断函数

        返回 ASK 时由工具层走用户输入通道; auto-agent 模式调用 judge 判断
        (judge 签名: (operation, risk, description) -> "allow" / "deny" / "ask")

        返回:
        - PermissionDecision: 同步评估操作是否放行
        """
        risk = RiskLevel(risk)
        model_verdict: str | None = None
        with self._lock:
            decision = self._evaluate_core(operation, risk)
            if decision == PermissionDecision.ASK and self.mode == "auto-agent" and judge is not None:
                model_verdict = judge(operation, risk, description)
                decision = self._verdict_to_decision(model_verdict)
            self._append_log(
                operation,
                risk,
                description,
                decision,
                self.mode,
                model_verdict,
            )
            return decision

    async def evaluate_async(
        self,
        operation: str,
        risk: RiskLevel | int,
        description: str = "",
        *,
        judge: Callable[[str, RiskLevel, str], Any] | None = None,
    ) -> PermissionDecision:
        """
        异步评估 (auto-agent 模式支持异步 judge, 返回 awaitable)

        参数:
        - operation: 操作信息
        - risk: 风险级别
        - description: 说明文本
        - judge: 判断函数

        judge 调用在锁外执行, 避免持锁 await LLM 调用阻塞其它线程

        返回:
        - PermissionDecision:  awaitable)
        """
        risk = RiskLevel(risk)
        model_verdict: str | None = None
        with self._lock:
            decision = self._evaluate_core(operation, risk)
        if decision == PermissionDecision.ASK and self.mode == "auto-agent":
            if judge is None:
                decision = PermissionDecision.ASK
            else:
                verdict = judge(operation, risk, description)
                if hasattr(verdict, "__await__"):
                    verdict = await verdict
                model_verdict = str(verdict)
                decision = self._verdict_to_decision(model_verdict)
        self._append_log(
            operation,
            risk,
            description,
            decision,
            self.mode,
            model_verdict,
        )
        return decision

    def _evaluate_core(self, operation: str, risk: RiskLevel) -> PermissionDecision:
        """
        规则层评估 (不含策略与 judge)

        参数:
        - operation: 操作信息
        - risk: 风险级别

        返回:
        - PermissionDecision: 规则层评估 (不含策略与 judge)
        """
        if risk >= RiskLevel.FORBIDDEN:
            return PermissionDecision.DENY
        if self.is_plan_mode_block(operation, risk):
            return PermissionDecision.DENY
        if operation == "shell":
            return PermissionDecision.ASK   # 解释器命令不能由分类结果或通用历史规则获得本机权限
        allowed_level = self._persistent_rules.get(operation)
        if allowed_level is None:
            allowed_level = self._session_rules.get(operation)
        if allowed_level is not None and risk <= allowed_level:
            return PermissionDecision.ALLOW
        if risk <= RiskLevel.READ:
            return PermissionDecision.ALLOW
        if self.mode == "full":
            return PermissionDecision.ALLOW
        return PermissionDecision.ASK

    @staticmethod
    def _verdict_to_decision(verdict: str) -> PermissionDecision:
        """
        将审批模型判断收窄为拒绝或人工确认

        参数:
        - verdict: verdict 输入值

        返回:
        - PermissionDecision: deny 返回拒绝, 其他内容均返回人工确认
        """
        text = verdict.strip().lower()
        if text.startswith("deny"):
            return PermissionDecision.DENY
        return PermissionDecision.ASK
