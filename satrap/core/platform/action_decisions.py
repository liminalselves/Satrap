"""
统一审批请求与业务事务内的决策绑定

面板身份由已认证宿主提供, 每项修订与策略修订分离,
原群, 好友和记忆服务仍负责实际授权与执行, 本模块不直接访问平台接口
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from contextlib import contextmanager
from contextlib import closing
from weakref import WeakKeyDictionary
import asyncio
import sqlite3
from typing import Literal, cast
import time
import json

from satrap.core.platform.action_catalog import ACTION_READS, ActionScope
from satrap.core.telemetry.contracts import JsonValue

from satrap.core.log import logger


ActionSource = Literal["group", "invitation", "friend", "memory"]
DecisionTable = Literal["group_actions", "friend_actions", "memory_proposals"]


class DecisionConflict(ValueError):
    """记录修订或状态已变化, 本次决定不能再取得执行权"""


@dataclass(frozen=True)
class DecisionActor:
    """处理人来自真实认证边界, 共用面板密码无法标识具体个人"""
    kind: Literal["panel", "platform_user", "system"] = "panel"
    id: str | None = None


@dataclass(frozen=True)
class DecisionItem:
    """完整动作身份与本次唯一允许处理的修订"""
    platform_id: str
    self_id: str
    source: ActionSource
    action_id: str
    expected_revision: int
    approve: bool
    note: str = ""

    @property
    def key(self) -> dict[str, str]:
        """
        导出不会混淆跨账号动作的完整身份

        返回:
        - 平台, 账号, 来源与动作 ID
        """
        return {name: str(getattr(self, name)) for name in ("platform_id", "self_id", "source", "action_id")}


@dataclass(frozen=True)
class _DecisionBinding:
    """仅在一次业务分派内传播的事务约束"""
    item: DecisionItem
    actor: DecisionActor
    receipt: _DecisionReceipt = field(default_factory=lambda: _DecisionReceipt())


@dataclass
class _DecisionReceipt:
    """区分本次事务是否实际到达写决定步骤, 不把并发请求的决定冒认为本次"""
    recorded: bool = False


_DECISION: ContextVar[_DecisionBinding | None] = ContextVar("platform_action_decision", default=None)


def parse_decisions(payload: object) -> list[DecisionItem]:
    """
    在任何业务写入前校验整个批次, 重复项不能触发第二次执行

    参数:
    - payload: 已认证入口收到的 JSON 对象

    返回:
    - 1-20 项完整请求, 任何格式错误拒绝整批
    """
    if not isinstance(payload, dict):
        raise ValueError("批量审批必须为对象")
    payload = cast(dict[str, object], payload)
    if set(payload) != {"items"}:
        raise ValueError("批量审批只接受 items 字段")
    entries = payload["items"]
    if not isinstance(entries, list) or not 1 <= len(cast(list[object], entries)) <= 20:
        raise ValueError("批量审批需要 1-20 项")
    result: list[DecisionItem] = []
    identities: set[tuple[str, ...]] = set()
    for entry in cast(list[object], entries):
        if not isinstance(entry, dict):
            raise ValueError("审批项必须为对象")
        entry = cast(dict[str, object], entry)
        if set(entry) - {"key", "expected_revision", "approve", "note"} or not {"key", "expected_revision", "approve"} <= set(entry):
            raise ValueError("审批项字段无效")
        key = entry["key"]
        if not isinstance(key, dict):
            raise ValueError("审批项缺少完整身份")
        key = cast(dict[str, object], key)
        if set(key) != {"platform_id", "self_id", "source", "action_id"}:
            raise ValueError("审批项缺少完整身份")
        if any(not isinstance(value, str) or not 1 <= len(value) <= 256 for value in key.values()):
            raise ValueError("审批身份必须是非空且有界的字符串")
        key = cast(dict[str, str], key)
        if key["source"] not in {"group", "invitation", "friend", "memory"}:
            raise ValueError("审批来源无效")
        revision, approve, note = entry["expected_revision"], entry["approve"], entry.get("note", "")
        if not isinstance(revision, int) or isinstance(revision, bool) or not 1 <= revision < 2**63 or not isinstance(approve, bool) or not isinstance(note, str) or len(note) > 1000:
            raise ValueError("审批修订, 决定或附言无效")
        identity = tuple(key[name] for name in ("platform_id", "self_id", "source", "action_id"))
        if identity in identities:
            raise ValueError("批量审批不能包含重复动作")
        identities.add(identity)
        result.append(DecisionItem(key["platform_id"], key["self_id"], cast(ActionSource, key["source"]), key["action_id"], revision, approve, note))
    return result


@contextmanager
def bind_decision(item: DecisionItem, actor: DecisionActor) -> Iterator[_DecisionReceipt]:
    """
    把可信处理人与项修订传至原业务事务, asyncio.to_thread 保留该绑定

    参数:
    - item: 已校验的单项请求
    - actor: 宿主提供的身份, 不能来自请求正文

    返回:
    - 仅当前分派范围有效的绑定
    """
    binding = _DecisionBinding(item, actor)
    token = _DECISION.set(binding)
    try:
        yield binding.receipt
    finally:
        _DECISION.reset(token)


def decision_metadata(connection: sqlite3.Connection, table: DecisionTable, row: Mapping[str, object], approve: bool) -> None:
    """
    在业务已有写事务内核对项修订并保存处理资料, 不提前提交

    参数:
    - connection: 原业务 BEGIN IMMEDIATE 事务
    - table: 固定白名单中的动作表
    - row: 已核验账号和目标范围的当前记录
    - approve: 原业务实际采用的决定
    """
    binding = _DECISION.get()
    identity_column = "proposal_id" if table == "memory_proposals" else "action_id"
    identity = str(row[identity_column])
    if binding is not None:
        source = "memory" if table == "memory_proposals" else "friend" if table == "friend_actions" else "group"
        if table == "group_actions" and row["action_type"] == "handle_group_request":
            params = json.loads(str(row["params_json"]))
            if isinstance(params, dict) and cast(dict[str, object], params).get("sub_type") == "invite":
                source = "invitation"
        if (binding.item.action_id != identity or binding.item.approve != approve or row["state"] != "pending"
                or binding.item.source != source
                or row["action_revision"] != binding.item.expected_revision
                or table != "memory_proposals" and binding.item.self_id != row["self_id"]):
            raise DecisionConflict("审批项修订或状态已变化, 请刷新后重试")
    actor = json.dumps(asdict(binding.actor if binding is not None else DecisionActor()), ensure_ascii=False)
    note = binding.item.note if binding is not None else ""
    where = f"{identity_column}=?"
    args: list[object] = ["approved" if approve else "rejected", actor, note, identity]
    if table == "friend_actions":
        where += " AND self_id=?"
        args.append(row["self_id"])
    connection.execute(f"UPDATE {table} SET decision=?,decided_by_json=?,decision_note=? WHERE {where}", args)
    if binding is not None:
        binding.receipt.recorded = True


@dataclass(frozen=True)
class DecisionTarget:
    """仅供宿主分派使用的业务定位, 不带平台处理凭据"""
    revision: int
    state: str
    source_state: str
    decision: str | None
    group_id: str
    base_revision: int = 0

    def public(self) -> dict[str, JsonValue]:
        """
        返回决定和执行结果的最小公开形状

        返回:
        - 项修订, 统一状态, 来源状态与人工决定
        """
        return {'revision': self.revision, 'state': self.state, 'source_state': self.source_state, 'decision': self.decision}


def read_decision_target(scope: ActionScope, item: DecisionItem) -> DecisionTarget:
    """
    从只读快照读取业务定位, 不构造会触发恢复的存储对象

    参数:
    - scope: 经过宿主核验的平台路径
    - item: 完整动作身份

    返回:
    - 当前目标, 不属于实际账号或来源时拒绝
    """
    if item.platform_id != scope.platform_id or not scope.database.is_file():
        raise LookupError('审批动作不存在')
    table: DecisionTable = 'memory_proposals' if item.source == 'memory' else 'friend_actions' if item.source == 'friend' else 'group_actions'
    identity = 'proposal_id' if table == 'memory_proposals' else 'action_id'
    with closing(sqlite3.connect(scope.database.resolve().as_uri() + '?mode=ro', uri=True, timeout=3)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is None:
            raise LookupError('审批动作不存在')
        row = db.execute(f'SELECT * FROM {table} WHERE {identity}=?' + (' AND self_id=?' if table != 'memory_proposals' else ''),
                         (item.action_id, item.self_id) if table != 'memory_proposals' else (item.action_id,)).fetchone()
        if row is None:
            raise LookupError('审批动作不存在')
        keys = set(row.keys())
        if 'action_revision' not in keys:
            raise PermissionError('审批库尚未升级, 不能通过新接口决策')
        group_id = str(row['group_id']) if table == 'group_actions' else ''
        base_revision = 0
        if table == 'memory_proposals':
            key = str(row['scope_key'])
            parts = json.loads(key[12:]) if key.startswith('messages:v1:') else None
            if not isinstance(parts, list) or len(cast(list[object], parts)) != 4:
                raise LookupError('审批动作不属于当前账号')
            parts = cast(list[object], parts)
            if parts[:3] != [item.platform_id, item.self_id, 'group'] or not isinstance(parts[3], str):
                raise LookupError('审批动作不属于当前账号')
            group_id, base_revision = parts[3], int(row['base_revision'])
        elif table == 'group_actions':
            params = json.loads(row['params_json'])
            source = 'invitation' if row['action_type'] == 'handle_group_request' and params.get('sub_type') == 'invite' else 'group'
            if source != item.source:
                raise LookupError('审批动作来源不匹配')
        source_state = str(row['state'])
        state = {'approved': 'succeeded', 'conflicted': 'failed'}.get(source_state, source_state) if item.source == 'memory' else source_state
        if state == 'pending' and row['expires_at'] is not None and row['expires_at'] <= time.time():
            state = 'expired'
        return DecisionTarget(int(row['action_revision']), state, source_state, row['decision'], group_id, base_revision)


DecisionDispatcher = Callable[[DecisionItem, DecisionTarget], Awaitable[object]]
_SLOTS: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = WeakKeyDictionary()


async def run_decisions(items: list[DecisionItem], scopes: list[ActionScope], dispatch: DecisionDispatcher,
                        *, actor: DecisionActor | None = None) -> dict[str, JsonValue]:
    """
    用原业务服务完成合法批次, 跨批次执行并发也限制为二

    参数:
    - items: 已整批验证且去重的 1-20 项
    - scopes: 本次实际配置的平台范围
    - dispatch: 宿主的原群, 好友或记忆服务分派
    - actor: 认证边界提供的身份, 默认共用面板且无个人 ID

    返回:
    - 逐项结果, 一个失败不会阻断其它项, 未确认结果不会自动重发
    """
    if not 1 <= len(items) <= 20:
        raise ValueError('批量审批需要 1-20 项')
    slots = _SLOTS.setdefault(asyncio.get_running_loop(), asyncio.Semaphore(2))
    scope_map = {scope.platform_id: scope for scope in scopes}

    async def decide(item: DecisionItem) -> JsonValue:
        """
        在占用执行槽后重新读状态, 决策 CAS 仍由原业务事务完成

        参数:
        - item: 当前批次中的一项

        返回:
        - 不含异常正文或平台处理凭据的单项结果
        """
        key = item.key
        def result(kind: str, target: DecisionTarget | None, code: str | None = None, message: str = '') -> dict[str, JsonValue]:
            """
            构造固定的决策回执

            参数:
            - kind: applied, conflict, unavailable, failed 或 unknown
            - target: 可确认的最新状态
            - code: 可选公开错误码
            - message: 不包含平台凭据的固定说明

            返回:
            - 完整身份与决定状态
            """
            return {'key': cast(dict[str, JsonValue], key), 'result': kind, 'action': target.public() if target else None,
                    'error': {'code': code, 'message': message} if code else None}
        async with slots:
            scope = scope_map.get(item.platform_id)
            if scope is None or scope.current_account != item.self_id or not scope.online and item.source != 'memory':
                logger.warning(f'[统一审批] 目标不可操作 platform={item.platform_id} source={item.source}')
                return result('unavailable', None, 'action_unavailable', '平台离线, 账号未确认或记录属于历史账号')
            target = None
            dispatched = False
            receipt = None
            try:
                target = await ACTION_READS.run(read_decision_target, scope, item)
                if target.revision != item.expected_revision or target.state != 'pending':
                    return result('conflict', target, 'revision_conflict', '审批项修订或状态已变化')
                with bind_decision(item, actor or DecisionActor()) as receipt:
                    dispatched = True
                    await dispatch(item, target)
                updated = await ACTION_READS.run(read_decision_target, scope, item)
                if not receipt.recorded:
                    return result('conflict', updated, 'revision_conflict', '当前项已由其它请求处理或已失效')
                if updated.decision != ('approved' if item.approve else 'rejected') or updated.revision <= target.revision:
                    return result('unavailable', updated, 'decision_not_applied', '业务状态已变化, 此决定没有写入')
                return result('unknown' if updated.state in {'unknown', 'executing'} else 'applied', updated,
                              'execution_unconfirmed' if updated.state in {'unknown', 'executing'} else None,
                              '决定已写入, 执行结果尚未确认')
            except DecisionConflict:
                logger.warning(f'[统一审批] 项修订冲突 platform={item.platform_id} action={item.action_id}')
                try:
                    updated = await ACTION_READS.run(read_decision_target, scope, item)
                except Exception as error:
                    logger.warning(f'[统一审批] 冲突后查询失败: {type(error).__name__}')
                    updated = None
                return result('conflict', updated, 'revision_conflict', '审批项修订或状态已变化')
            except Exception as error:
                logger.warning(f'[统一审批] 处理失败 platform={item.platform_id} source={item.source} action={item.action_id}: {type(error).__name__}')
                updated = None
                try:
                    updated = await ACTION_READS.run(read_decision_target, scope, item)
                except Exception:
                    logger.warning(f'[统一审批] 失败后无法确认记录 platform={item.platform_id} action={item.action_id}')
                if updated and target and updated.revision > target.revision:
                    if receipt is None or not receipt.recorded:
                        return result('conflict', updated, 'revision_conflict', '当前项已由其它请求处理')
                if updated and target and updated.revision > target.revision and updated.decision == ('approved' if item.approve else 'rejected'):
                    return result('applied' if updated.state in {'succeeded', 'failed', 'rejected'} else 'unknown', updated,
                                  None if updated.state in {'succeeded', 'failed', 'rejected'} else 'execution_unconfirmed', '决定已写入, 不要重复提交')
                unavailable = isinstance(error, (LookupError, PermissionError))
                if dispatched and updated is None:
                    return result('unknown', None, 'decision_unconfirmed', '已调用业务服务但无法确认状态, 请先查询原记录')
                return result('unavailable' if unavailable else 'failed', updated,
                              'action_unavailable' if unavailable else 'decision_failed', '审批目标当前不可操作' if unavailable else '本项处理失败, 请刷新记录后确认')

    results = await asyncio.gather(*(decide(item) for item in items))
    return {'items': list(results)}
