"""只读汇总平台动作, 不创建业务存储或承担审批执行"""
from __future__ import annotations

import asyncio
import base64
from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time
from typing import Literal, TypedDict, cast

from satrap.core.utils.async_worker import BoundedAsyncWorker


ActionSource = Literal['group', 'invitation', 'friend']
ACTION_READS = BoundedAsyncWorker('action-catalog', workers=4, capacity=32)
STATES = {'all', 'pending', 'ready', 'executing', 'succeeded', 'failed', 'unknown', 'rejected', 'expired'}
PUBLIC_PARAMS = frozenset({'user_id', 'request_id', 'approve', 'duration', 'enable', 'reject_add_request', 'name',
                           'card', 'title', 'special_title', 'is_dismiss', 'dismiss', 'message_id', 'reason', 'remark', 'message', 'sub_type', 'anonymous_id', 'expected_revision'})


@dataclass(frozen=True)
class ActionScope:
    """由宿主绑定的平台数据库与当前账号, 查询参数不能改变数据库路径"""

    platform_id: str
    database: Path
    current_account: str
    online: bool


class PlatformAction(TypedDict):
    """不包含平台处理凭据的审批中心记录"""

    platform_id: str
    self_id: str
    source: ActionSource
    action_id: str
    group_id: str
    action_type: str
    params: dict[str, object]
    actor_kind: str
    actor_id: str
    state: str
    created_at: float
    expires_at: float | None
    result: dict[str, object] | None
    target: dict[str, object]
    can_decide: bool
    unavailable_reason: str


class PlatformActionsInfo(TypedDict):
    """单个平台的账号选项和待审批覆盖情况"""

    platform_id: str
    current_account: str
    online: bool
    accounts: list[str]
    pending_count: int | None


class ActionCatalog(TypedDict):
    """汇总列表与摘要共用的响应, 部分失败不伪装成完整空列表"""

    items: list[PlatformAction]
    total: int
    pending_count: int
    complete: bool
    platforms: list[PlatformActionsInfo]
    warnings: list[str]
    has_more: bool
    next_cursor: str | None


def _object(value: str | None) -> dict[str, object]:
    """
    读取已保存的 JSON 对象, 拒绝损坏的结构

    参数:
    - value: 数据库 JSON 字段

    返回:
    - 仅包含文本键的对象
    """
    decoded: object = json.loads(value or '{}')
    if not isinstance(decoded, dict) or any(not isinstance(key, str) for key in decoded):
        raise ValueError('动作记录的对象字段损坏')
    return cast(dict[str, object], decoded)


def _key(item: PlatformAction) -> tuple[float, str, str, str, str]:
    """
    以完整身份稳定排序, 防止跨表动作 ID 碰撞

    参数:
    - item: 已归一化的动作

    返回:
    - 时间倒序, 其余身份正序的排序键
    """
    return (-item['created_at'], item['platform_id'], item['self_id'], item['source'], item['action_id'])


def _read(scope: ActionScope, filters: dict[str, str], limit: int, before: list[object] | None,
          now: float, summary: bool) -> tuple[list[PlatformAction], int, PlatformActionsInfo]:
    """
    在单个平台的只读快照内读取目录和计数

    参数:
    - scope: 宿主提供的固定范围
    - filters: 已校验的筛选
    - limit: 当前平台最多返回的条数
    - before: 同范围游标
    - now: 本次汇总统一使用的到期时间
    - summary: 仅查询待审批计数

    返回:
    - 当前平台有界列表, 匹配总数和账号摘要
    """
    info: PlatformActionsInfo = {'platform_id': scope.platform_id, 'current_account': scope.current_account,
                                'online': scope.online, 'accounts': [], 'pending_count': 0 if scope.current_account else None}
    if not scope.database.is_file():
        return [], 0, info
    with closing(sqlite3.connect(scope.database.resolve().as_uri() + '?mode=ro', uri=True, timeout=3)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        # Step.1 固定只读快照, 只访问已经存在的业务表
        sources = []
        if 'group_actions' in tables:
            sources.append("SELECT CASE WHEN action_type='handle_group_request' AND json_extract(params_json,'$.sub_type')='invite' "
                           "THEN 'invitation' ELSE 'group' END AS source, action_id, self_id, group_id, action_type, params_json, "
                           "actor_kind, '' AS actor_id, created_at, expires_at, state, result_json, '{}' AS target_json FROM group_actions")
        if 'friend_actions' in tables:
            sources.append("SELECT 'friend' AS source, action_id, self_id, '' AS group_id, action_type, params_json, actor_kind, "
                           "actor_id, created_at, expires_at, state, result_json, target_json FROM friend_actions")
        if not sources:
            return [], 0, info
        normalized = ("SELECT :platform AS platform_id, actions.*, CASE WHEN state='pending' AND expires_at IS NOT NULL "
                      "AND expires_at<=:now THEN 'expired' ELSE state END AS effective_state FROM (" + ' UNION ALL '.join(sources) + ') actions')
        args: dict[str, object] = {'platform': scope.platform_id, 'now': now, 'current': scope.current_account}
        # Step.2 归一化来源和到期状态, 摘要读取不解析记录正文
        if scope.current_account:
            info['pending_count'] = int(connection.execute(f"SELECT COUNT(*) FROM ({normalized}) WHERE self_id=:current AND effective_state='pending'", args).fetchone()[0])
        if summary:
            return [], 0, info
        info['accounts'] = [str(row[0]) for row in connection.execute(f'SELECT DISTINCT self_id FROM ({normalized}) ORDER BY self_id', args)]
        conditions = ['1=1']
        account = filters['account']
        if account != 'current' or scope.current_account:
            args['account'] = scope.current_account if account == 'current' else account
            conditions.append('self_id=:account')
        for name, column in (('source', 'source'), ('state', 'effective_state'), ('action_id', 'action_id')):
            if filters[name] and filters[name] != 'all':
                conditions.append(f'{column}=:{name}')
                args[name] = filters[name]
        if filters['target']:
            conditions.append("(group_id=:target OR json_extract(params_json,'$.user_id')=:target OR json_extract(target_json,'$.user_id')=:target)")
            args['target'] = filters['target']
        selected = f'SELECT * FROM ({normalized}) WHERE ' + ' AND '.join(conditions)
        total = int(connection.execute(f'SELECT COUNT(*) FROM ({selected})', args).fetchone()[0])
        if before is not None:
            for index, value in enumerate(before):
                args[f'cursor{index}'] = value
            selected = f'SELECT * FROM ({selected}) WHERE (-created_at,platform_id,self_id,source,action_id)>(:cursor0,:cursor1,:cursor2,:cursor3,:cursor4)'
        args['limit'] = limit
        rows = connection.execute(selected + ' ORDER BY created_at DESC,platform_id,self_id,source,action_id LIMIT :limit', args).fetchall()
        # Step.3 完成筛选和分页后, 仅解析当前页允许公开的字段
        items: list[PlatformAction] = []
        for row in rows:
            params = {key: value for key, value in _object(row['params_json']).items() if key in PUBLIC_PARAMS}
            result = _object(row['result_json']) if row['result_json'] else None
            result = {key: value for key, value in result.items() if key in {'reason', 'message', 'retcode', 'verification'}} if result else None
            reason = ('当前状态无需人工审批' if row['effective_state'] != 'pending' else '平台未连接' if not scope.online
                      else '历史账号记录仅供查看' if row['self_id'] != scope.current_account else '')
            target = {key: value for key, value in _object(row['target_json']).items() if key in {'user_id', 'nickname', 'remark'}}
            items.append({'platform_id': scope.platform_id, 'self_id': row['self_id'], 'source': row['source'],
                          'action_id': row['action_id'], 'group_id': row['group_id'], 'action_type': row['action_type'],
                          'params': params, 'actor_kind': row['actor_kind'], 'actor_id': row['actor_id'],
                          'state': row['effective_state'], 'created_at': float(row['created_at']), 'expires_at': row['expires_at'],
                          'result': result, 'target': target, 'can_decide': not reason, 'unavailable_reason': reason})
        return items, total, info


async def platform_action_catalog(scopes: list[ActionScope], *, platform_id: str = '', account: str = 'current',
                                  source: str = 'all', state: str = 'pending', target: str = '', action_id: str = '',
                                  limit: int = 50, cursor: str = '', summary: bool = False) -> ActionCatalog:
    """
    有界并发读取平台动作, 筛选与游标均不改变宿主授权范围

    参数:
    - scopes: 宿主已核验的平台范围
    - platform_id: 可选平台筛选
    - account: current 或历史账号
    - source: all, group, invitation 或 friend
    - state: 目标状态或 all
    - target: 可选群或用户 ID
    - action_id: 原入口定位的动作 ID
    - limit: 每页 1 到 100 条
    - cursor: 同筛选范围的上一页游标
    - summary: 仅返回待审批摘要

    返回:
    - 有覆盖信息的合并页, 不因单个平台失败丢弃其他结果
    """
    if source not in {'all', 'group', 'invitation', 'friend'} or state not in STATES or not 1 <= limit <= 100:
        raise ValueError('动作筛选或分页参数无效')
    if not account or any(len(value) > 256 for value in (platform_id, account, target, action_id)):
        raise ValueError('动作身份参数无效')
    if platform_id and not any(scope.platform_id == platform_id for scope in scopes):
        raise ValueError('平台实例不存在')
    chosen = [scope for scope in scopes if not platform_id or scope.platform_id == platform_id]
    # Step.1 验证筛选, 将游标绑定当前平台身份和查询范围
    filters = {'account': account, 'source': source, 'state': state, 'target': target, 'action_id': action_id}
    fingerprint = hashlib.sha256(json.dumps([platform_id, filters, [(scope.platform_id, scope.current_account) for scope in chosen]], sort_keys=True).encode()).hexdigest()
    before: list[object] | None = None
    if cursor:
        try:
            if len(cursor) > 2048:
                raise ValueError()
            envelope = _object(base64.urlsafe_b64decode(cursor.encode()).decode())
            key = envelope['key']
            if envelope.get('scope') != fingerprint or not isinstance(key, list) or len(key) != 5:
                raise ValueError()
            if type(key[0]) not in {int, float} or not math.isfinite(key[0]) or any(not isinstance(value, str) for value in key[1:]):
                raise ValueError()
            before = key
        except (KeyError, ValueError, UnicodeError, TypeError) as error:
            raise ValueError('动作游标无效或筛选范围已变化') from error
    now = time.time()
    responses = await asyncio.gather(*(ACTION_READS.run(_read, scope, filters, limit + 1, before, now, summary) for scope in chosen), return_exceptions=True)
    # Step.2 有界并发读取, 各平台分别维持自己的只读快照
    result: ActionCatalog = {'items': [], 'total': 0, 'pending_count': 0, 'complete': True, 'platforms': [], 'warnings': [], 'has_more': False, 'next_cursor': None}
    result['platforms'] = [{'platform_id': scope.platform_id, 'current_account': scope.current_account, 'online': scope.online,
                            'accounts': [scope.current_account] if scope.current_account else [], 'pending_count': None} for scope in scopes]
    for scope, response in zip(chosen, responses):
        if isinstance(response, BaseException):
            if isinstance(response, asyncio.CancelledError):
                raise response
            result['complete'] = False
            result['warnings'].append(f'{scope.platform_id}: 动作记录读取失败, {type(response).__name__}')
            continue
        items, total, info = response
        result['items'].extend(items)
        result['total'] += total
        result['platforms'][next(index for index, value in enumerate(result['platforms']) if value['platform_id'] == scope.platform_id)] = info
        if info['pending_count'] is None:
            result['complete'] = False
            result['warnings'].append(f'{scope.platform_id}: 当前机器人账号尚未确认')
        else:
            result['pending_count'] += info['pending_count']
    result['items'].sort(key=_key)
    # Step.3 合并有限候选行, 同时保留部分读取失败的覆盖信息
    result['has_more'] = len(result['items']) > limit
    result['items'] = result['items'][:limit]
    if result['has_more']:
        result['next_cursor'] = base64.urlsafe_b64encode(json.dumps({'scope': fingerprint, 'key': _key(result['items'][-1])}).encode()).decode()
    return result
