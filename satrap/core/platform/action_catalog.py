"""
平台动作与统一审批只读目录

在固定平台快照内归一群, 邀请, 好友和记忆账本, 支持稳定分页与同筛选摘要,
不创建业务存储或执行审批, 当前页之外的私密参数和记忆正文不会解码为响应
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from contextlib import closing
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import asyncio
import hashlib
from pathlib import Path
import sqlite3
import base64
from typing import Literal, TypedDict, cast
import json
import math
import time

from satrap.core.telemetry.contracts import JsonValue, MetricWindow
from satrap.core.utils.async_worker import BoundedAsyncWorker
from satrap.core.telemetry.store import json_text
from satrap.core.storage.layout import StorageLayout

from satrap.core.log import logger


ActionSource = Literal['group', 'invitation', 'friend', 'memory']
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
    invalid_count: int


class UnifiedAction(PlatformAction):
    """统一目录在原业务项之上增加项修订与可信处理资料"""
    key: dict[str, str]
    revision: int
    source_state: str
    decided_at: float | None
    decided_by: dict[str, JsonValue] | None
    decision: str | None
    note: str
    risk: str
    risk_source: str
    group_name: str | None
    requester: dict[str, JsonValue] | None
    request: dict[str, JsonValue]


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
                                'online': scope.online, 'accounts': [], 'pending_count': 0, 'invalid_count': 0}
    if not scope.database.is_file():
        return [], 0, info
    with closing(sqlite3.connect(scope.database.resolve().as_uri() + '?mode=ro', uri=True, timeout=3)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        # Step.1 固定只读快照, 只访问已经存在的业务表
        sources = []
        def valid_objects(*columns: str) -> str:
            """
            生成内部 JSON 对象校验条件, CASE 阻止损坏行触发 json_type 异常

            参数:
            - columns: 框架固定的 JSON 列名

            返回:
            - 可用于 SQL WHERE 的完整校验表达式
            """
            return " AND ".join("CASE WHEN json_valid(COALESCE(NULLIF(" + column + ",'') ,'{}')) THEN "
                                "json_type(COALESCE(NULLIF(" + column + ",'') ,'{}'))='object' ELSE 0 END" for column in columns)
        if 'group_actions' in tables:
            sources.append("SELECT CASE WHEN action_type='handle_group_request' AND json_extract(params_json,'$.sub_type')='invite' "
                           "THEN 'invitation' ELSE 'group' END AS source, action_id, self_id, group_id, action_type, params_json, "
                           "actor_kind, '' AS actor_id, created_at, expires_at, state, result_json, '{}' AS target_json FROM group_actions "
                           "WHERE " + valid_objects('params_json', 'result_json'))
            info['invalid_count'] += int(connection.execute("SELECT COUNT(*) FROM group_actions WHERE NOT (" + valid_objects('params_json', 'result_json') + ")").fetchone()[0])
        if 'friend_actions' in tables:
            sources.append("SELECT 'friend' AS source, action_id, self_id, '' AS group_id, action_type, params_json, actor_kind, "
                           "actor_id, created_at, expires_at, state, result_json, target_json FROM friend_actions WHERE " + valid_objects('params_json', 'result_json', 'target_json'))
            info['invalid_count'] += int(connection.execute("SELECT COUNT(*) FROM friend_actions WHERE NOT (" + valid_objects('params_json', 'result_json', 'target_json') + ")").fetchone()[0])
        if not sources:
            return [], 0, info
        normalized = ("SELECT :platform AS platform_id, actions.*, CASE WHEN state='pending' AND expires_at IS NOT NULL "
                      "AND expires_at<=:now THEN 'expired' ELSE state END AS effective_state FROM (" + ' UNION ALL '.join(sources) + ') actions')
        args: dict[str, object] = {'platform': scope.platform_id, 'now': now, 'current': scope.current_account}
        # Step.2 归一化来源和到期状态, 摘要读取不解析记录正文
        if not scope.current_account and not info['invalid_count']:
            if connection.execute(f"SELECT 1 FROM ({normalized}) LIMIT 1", args).fetchone() is None:
                return [], 0, info
        if not scope.current_account:
            info['pending_count'] = None
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
                            'accounts': [scope.current_account] if scope.current_account else [], 'pending_count': None, 'invalid_count': 0} for scope in scopes]
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
        if info['invalid_count']:
            result['complete'] = False
            result['warnings'].append(f"{scope.platform_id}: 已隔离 {info['invalid_count']} 条损坏动作记录, 列表与计数均不包含这些记录")
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


@dataclass(frozen=True)
class UnifiedActionQuery:
    """统一审批的固定筛选, 排序与时间窗口"""
    filters: dict[str, str]
    limit: int
    sort: str
    order: str
    start: float | None
    end: float | None
    timezone: str
    after: tuple[object, ...] = ()
    scope_revision: str | None = None

    @classmethod
    def parse(cls, values: dict[str, str], *, summary: bool = False, now: float | None = None) -> UnifiedActionQuery:
        """
        解析统一审批查询, 日期窗口在翻页期间保持不变

        参数:
        - values: 入口已拒绝重复参数的查询
        - summary: 摘要默认采用今日窗口
        - now: 可注入当前时间

        返回:
        - 绑定排序与筛选的查询, 未知或无效字段抛出 ValueError
        """
        allowed = {'platform_id', 'account', 'source', 'state', 'target', 'action_id', 'q', 'sort', 'order', 'from', 'to', 'tz', 'limit', 'cursor'}
        if set(values) - allowed or any(len(value) > (4096 if key == 'cursor' else 512) for key, value in values.items()):
            raise ValueError('审批目录包含未知或超长参数')
        filters = {key: values.get(key, default) for key, default in (('platform_id', ''), ('account', 'current'), ('source', 'all'),
                    ('state', 'all'), ('target', ''), ('action_id', ''), ('q', ''))}
        if filters['source'] not in {'all', 'group', 'invitation', 'friend', 'memory'} or filters['state'] not in STATES or not filters['account']:
            raise ValueError('审批来源, 状态或账号无效')
        limit = int(values.get('limit', '50'))
        sort, order = values.get('sort', 'created'), values.get('order', 'desc')
        if not 1 <= limit <= 100 or sort not in {'created', 'expires'} or order not in {'asc', 'desc'}:
            raise ValueError('审批分页或排序无效')
        params = {key: values[key] for key in ('from', 'to', 'tz') if key in values}
        after: tuple[object, ...] = ()
        scope_revision = None
        saved = None
        if values.get('cursor'):
            try:
                saved = _object(base64.urlsafe_b64decode(values['cursor']).decode())
                if saved.get('filters') != filters or saved.get('sort') != sort or saved.get('order') != order:
                    raise ValueError('审批游标不属于当前筛选')
                key = saved['after']
                if not isinstance(key, list):
                    raise ValueError('审批游标格式无效')
                key = cast(list[JsonValue], key)
                if (len(key) != 6 or type(key[0]) is not int or key[0] not in {0, 1}
                        or not isinstance(key[1], (int, float)) or isinstance(key[1], bool) or not math.isfinite(key[1])
                        or any(not isinstance(part, str) or len(part) > 512 for part in key[2:])):
                    raise ValueError('审批游标格式无效')
                after = tuple(key)
                scope_revision = saved.get('scope_revision')
                if scope_revision is not None and (not isinstance(scope_revision, str) or len(scope_revision) != 64):
                    raise ValueError('审批游标归属无效')
                for name in ('from', 'to', 'tz'):
                    previous = saved.get(name)
                    if name in params and (float(params[name]) != previous if name != 'tz' else params[name] != previous):
                        raise ValueError('审批游标窗口已变化')
                    if previous is not None:
                        params[name] = str(previous)
            except (KeyError, TypeError, ValueError, UnicodeError) as error:
                raise ValueError('审批游标无效或查询已变化') from error
        window = None
        if summary or 'from' in params or 'to' in params:
            window = MetricWindow.parse({**params, 'bucket': 'day'}, now=now)
        zone = params.get('tz', 'Asia/Shanghai')
        try:
            ZoneInfo(zone)
        except (ValueError, ZoneInfoNotFoundError) as error:
            raise ValueError('审批时区无效') from error
        return cls(filters, limit, sort, order, window.start if window else None, window.end if window else None, zone, after, scope_revision)

    def cursor(self, key: tuple[object, ...]) -> str:
        """
        保存当前稳定排序键与筛选

        参数:
        - key: 空到期标记, 排序时间和完整动作身份

        返回:
        - 无平台处理凭据的游标
        """
        return base64.urlsafe_b64encode(json_text({'filters': self.filters, 'sort': self.sort, 'order': self.order,
                    'from': self.start, 'to': self.end, 'tz': self.timezone, 'after': key, 'scope_revision': self.scope_revision}).encode()).decode()


def _unified_sources(connection: sqlite3.Connection, tables: set[str], platform_id: str) -> tuple[str, int]:
    """
    将既有业务表投影为固定列, 旧库仅补读取缺省值

    参数:
    - connection: 已开启的只读快照
    - tables: 已存在的表, 不初始化缺少的存储
    - platform_id: 当前存储的可信平台身份

    返回:
    - UNION ALL 投影与隔离的损坏记录数, 不读取记忆正文或平台凭据
    """
    projections: list[str] = []
    invalid = 0
    for table in ('group_actions', 'friend_actions', 'memory_proposals'):
        if table not in tables:
            continue
        columns = {row[1] for row in connection.execute(f'PRAGMA table_info({table})')}
        extras: list[str] = []
        for name, default in (('action_revision', '1'), ('decision', 'NULL'), ('decided_by_json', 'NULL'),
                              ('decision_note', "''"), ('risk', "'unknown'"), ('risk_source', "'unknown'")):
            extras.append(name if name in columns else f'{default} AS {name}')
        extras.append('1 AS decision_supported' if {'action_revision', 'decision', 'decided_by_json', 'decision_note'} <= columns else '0 AS decision_supported')
        valid = "1=1"
        for field in ('params_json', 'result_json', 'target_json', 'decided_by_json'):
            if field in columns:
                valid += f" AND CASE WHEN json_valid(COALESCE(NULLIF({field},''),'{{}}')) THEN json_type(COALESCE(NULLIF({field},''),'{{}}'))='object' ELSE 0 END"
        if 'action_revision' in columns:
            valid += " AND typeof(action_revision)='integer' AND action_revision>=1"
        safe_params = "CASE WHEN json_valid(params_json) THEN params_json ELSE '{}' END"
        safe_target = "CASE WHEN json_valid(target_json) THEN target_json ELSE '{}' END"
        safe_scope = "CASE WHEN json_valid(substr(scope_key,13)) THEN substr(scope_key,13) ELSE '[]' END"
        if table == 'memory_proposals':
            valid += " AND substr(scope_key,1,12)='messages:v1:' AND CASE WHEN json_valid(substr(scope_key,13)) THEN json_type(substr(scope_key,13))='array' AND json_array_length(substr(scope_key,13))=4 AND json_extract(substr(scope_key,13),'$[0]')=:platform AND json_extract(substr(scope_key,13),'$[2]')='group' "
            valid += " AND " + " AND ".join(f"json_type({safe_scope},'$[{index}]')='text'" for index in range(4)) + " ELSE 0 END"
            prefix = (f"SELECT 'memory' AS source, proposal_id AS action_id, json_extract({safe_scope},'$[1]') AS self_id, "
                      f"json_extract({safe_scope},'$[3]') AS group_id, operation AS action_type, '{{}}' AS params_json, 'model' AS actor_kind, "
                      "actor_id,created_at,expires_at,CASE state WHEN 'approved' THEN 'succeeded' WHEN 'conflicted' THEN 'failed' ELSE state END AS state, "
                      "NULL AS result_json,'{}' AS target_json,state AS source_state,decision_at,NULL AS executed_at,")
        elif table == 'group_actions':
            actor = "CASE WHEN json_valid(model_origin_json) THEN COALESCE(json_extract(model_origin_json,'$.actor_id'),'') ELSE '' END" if 'model_origin_json' in columns else "''"
            prefix = (f"SELECT CASE WHEN action_type='handle_group_request' AND json_extract({safe_params},'$.sub_type')='invite' THEN 'invitation' ELSE 'group' END AS source, "
                      f"action_id,self_id,group_id,action_type,{safe_params} AS params_json,actor_kind,{actor} AS actor_id,created_at,expires_at,state,result_json,'{{}}' AS target_json, "
                      "state AS source_state,decision_at,executed_at,")
        else:
            prefix = f"SELECT 'friend' AS source,action_id,self_id,'' AS group_id,action_type,{safe_params} AS params_json,actor_kind,actor_id,created_at,expires_at,state,result_json,{safe_target} AS target_json,state AS source_state,decision_at,executed_at,"
        projections.append(prefix + ','.join(extras) + f' FROM {table} WHERE ' + valid)
        invalid += int(connection.execute(f'SELECT count(*) FROM {table} WHERE NOT ({valid})', {'platform': platform_id}).fetchone()[0])
    return ' UNION ALL '.join(projections), invalid


def _unified_item(row: sqlite3.Row, scope: ActionScope) -> UnifiedAction:
    """
    在当前页内还原公开审批资料, 原文与处理凭据不进入目录

    参数:
    - row: 已经筛选的固定投影
    - scope: 宿主绑定的平台与账号

    返回:
    - 归一化状态, 项修订和可以解释的可操作性
    """
    source, state = row['source'], row['effective_state']
    key: dict[str, str] = {'platform_id': scope.platform_id, 'self_id': row['self_id'], 'source': source, 'action_id': row['action_id']}
    reason = ('当前状态无需人工审批' if state != 'pending' else '审批库尚未升级' if not row['decision_supported'] else '当前机器人账号尚未确认' if not scope.current_account
              else '历史账号记录仅供查看' if row['self_id'] != scope.current_account else '平台未连接' if not scope.online and source != 'memory' else '')
    params = cast(dict[str, JsonValue], {key: value for key, value in _object(row['params_json']).items() if key in PUBLIC_PARAMS})
    actor = _object(row['decided_by_json']) if row['decided_by_json'] else None
    if actor is not None and (actor.get('kind') not in {'panel', 'platform_user', 'system'} or not (actor.get('id') is None or isinstance(actor.get('id'), str))):
        raise ValueError('审批处理身份记录损坏')
    result = _object(row['result_json']) if row['result_json'] else None
    result = {name: value for name, value in result.items() if name in {'reason', 'message', 'retcode', 'verification'}} if result else None
    target = {name: value for name, value in _object(row['target_json']).items() if name in {'user_id', 'nickname', 'remark'}}
    return {'platform_id': scope.platform_id, 'self_id': row['self_id'], 'source': source, 'action_id': row['action_id'],
            'key': key, 'revision': row['action_revision'], 'group_id': row['group_id'], 'group_name': row['group_name'],
            'action_type': row['action_type'], 'state': state, 'source_state': row['source_state'], 'created_at': row['created_at'],
            'expires_at': row['expires_at'], 'decided_at': row['decision_at'], 'decided_by': cast(dict[str, JsonValue] | None, actor),
            'decision': row['decision'], 'note': row['decision_note'], 'risk': row['risk'], 'risk_source': row['risk_source'],
            'params': cast(dict[str, object], params), 'target': target, 'result': result, 'actor_kind': row['actor_kind'], 'actor_id': row['actor_id'],
            'requester': {'id': row['actor_id'], 'kind': row['actor_kind'], 'name': None} if row['actor_id'] else None,
            'request': {'action_type': row['action_type'], 'params': params}, 'can_decide': not reason, 'unavailable_reason': reason}


def _read_unified(scope: ActionScope, query: UnifiedActionQuery, *, summary: bool = False) -> dict[str, JsonValue]:
    """
    在单个平台只读快照内筛选, 汇总并分页, 不调用业务存储构造器

    参数:
    - scope: 固定平台路径与当前账号
    - query: 已校验的完整查询
    - summary: 不读取当前页详情

    返回:
    - 有界候选行, 总数, 状态计数及决策延迟的可合并分母
    """
    result: dict[str, JsonValue] = {'items': [], 'total': 0, 'by_state': {}, 'by_source': {}, 'today_decided': 0,
                                   'latency_sum_ms': 0, 'latency_count': 0, 'automatic_count': 0, 'expired_count': 0,
                                   'platform_id': scope.platform_id, 'current_account': scope.current_account,
                                   'online': scope.online, 'accounts': [], 'invalid_count': 0}
    if not scope.database.is_file():
        return result
    with closing(sqlite3.connect(scope.database.resolve().as_uri() + '?mode=ro', uri=True, timeout=3)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        sources, invalid = _unified_sources(db, tables, scope.platform_id)
        result['invalid_count'] = invalid
        if not sources:
            return result
        args: dict[str, object] = {'platform': scope.platform_id, 'now': time.time()}
        grouped = ("SELECT actions.*,CASE WHEN state='pending' AND expires_at IS NOT NULL AND expires_at<=:now THEN 'expired' "
                   "WHEN state IN ('pending','ready','executing','succeeded','failed','unknown','rejected','expired') THEN state ELSE 'unknown' END AS effective_state,"
                   ":platform AS platform_id," + ("d.group_name" if 'group_directory' in tables else 'NULL AS group_name')
                   + ' FROM (' + sources + ') actions' + (' LEFT JOIN group_directory d ON d.self_id=actions.self_id AND d.group_id=actions.group_id' if 'group_directory' in tables else ''))
        result['accounts'] = [str(row[0]) for row in db.execute('SELECT DISTINCT self_id FROM (' + grouped + ') ORDER BY self_id', args)]
        conditions = ['1=1']
        account = query.filters['account']
        if account == 'current':
            if scope.current_account:
                conditions.append('self_id=:account')
                args['account'] = scope.current_account
        elif account != 'all':
            conditions.append('self_id=:account')
            args['account'] = account
        for field, column in (('source', 'source'), ('state', 'effective_state'), ('action_id', 'action_id')):
            if query.filters[field] not in {'', 'all'}:
                conditions.append(column + '=:' + field)
                args[field] = query.filters[field]
        if query.filters['target']:
            conditions.append("(group_id=:target OR json_extract(params_json,'$.user_id')=:target OR json_extract(target_json,'$.user_id')=:target)")
            args['target'] = query.filters['target']
        if query.filters['q']:
            public_fields = ["action_id", "action_type", "group_name", "actor_id"]
            public_fields.extend(f"json_extract(params_json,'$.{name}')" for name in sorted(PUBLIC_PARAMS))
            public_fields.extend(f"json_extract(target_json,'$.{name}')" for name in ('user_id', 'nickname', 'remark'))
            conditions.append('(' + ' OR '.join(f"instr(lower(COALESCE(CAST({field} AS TEXT),'')),:q)>0" for field in public_fields) + ')')
            args['q'] = query.filters['q'].lower()
        base = 'SELECT * FROM (' + grouped + ') WHERE ' + ' AND '.join(conditions)
        if query.start is not None:
            args.update(start=query.start, end=query.end)
        selected = base + (' AND created_at>=:start AND created_at<:end' if query.start is not None else '')
        result['total'] = int(db.execute('SELECT count(*) FROM (' + selected + ')', args).fetchone()[0])
        result['by_state'] = {row[0]: row[1] for row in db.execute('SELECT effective_state,count(*) FROM (' + selected + ') GROUP BY effective_state', args)}
        result['by_source'] = {row[0]: row[1] for row in db.execute('SELECT source,count(*) FROM (' + selected + ') GROUP BY source', args)}
        stats = db.execute("SELECT count(*),COALESCE(sum(max(0,decision_at-created_at)*1000),0) FROM (" + base + ") WHERE decision IN ('approved','rejected') AND decided_by_json IS NOT NULL "
                           + ("AND decision_at>=:start AND decision_at<:end" if query.start is not None else ''), args).fetchone()
        result.update(today_decided=stats[0], latency_count=stats[0], latency_sum_ms=stats[1])
        result['expired_count'] = cast(dict[str, JsonValue], result['by_state']).get('expired', 0)
        result['automatic_count'] = int(db.execute("SELECT count(*) FROM (" + selected + ") WHERE state IN ('ready','executing','succeeded','failed','unknown') AND decision IS NULL", args).fetchone()[0])
        if not summary:
            null_key = '(expires_at IS NULL)' if query.sort == 'expires' else '0'
            column = 'COALESCE(expires_at,0)' if query.sort == 'expires' else 'created_at'
            signed = '-' + column if query.order == 'desc' else column
            ordered = 'SELECT *, ' + null_key + ' AS sort_null,' + signed + ' AS sort_time FROM (' + selected + ')'
            if query.after:
                for index, value in enumerate(query.after):
                    args['c' + str(index)] = value
                ordered = 'SELECT * FROM (' + ordered + ') WHERE (sort_null,sort_time,platform_id,self_id,source,action_id)>(:c0,:c1,:c2,:c3,:c4,:c5)'
            args['limit'] = query.limit + 1
            records = db.execute(ordered + ' ORDER BY sort_null,sort_time,platform_id,self_id,source,action_id LIMIT :limit', args).fetchall()
            result['items'] = cast(list[JsonValue], [{**_unified_item(row, scope), '_sort': [row['sort_null'], row['sort_time'], scope.platform_id, row['self_id'], row['source'], row['action_id']]} for row in records])
        return result


async def unified_action_catalog(scopes: list[ActionScope], query: UnifiedActionQuery, *, summary: bool = False) -> dict[str, JsonValue]:
    """
    合并固定平台的审批目录, 单个平台损坏时保留其它来源

    参数:
    - scopes: 宿主核验的平台范围
    - query: 已校验的查询
    - summary: 仅返回同筛选下的摘要

    返回:
    - 统一审批页或摘要, 缺失来源明确标为不完整
    """
    chosen = [scope for scope in scopes if not query.filters['platform_id'] or scope.platform_id == query.filters['platform_id']]
    if query.filters['platform_id'] and not chosen:
        raise ValueError('平台实例不存在')
    binding = hashlib.sha256(json_text(sorted((scope.platform_id, scope.current_account) for scope in chosen)).encode()).hexdigest()
    if query.scope_revision is not None and query.scope_revision != binding:
        raise ValueError('平台或当前账号已变化, 请重新查询审批目录')
    query = replace(query, scope_revision=binding)
    items: list[dict[str, JsonValue]] = []
    platforms: list[JsonValue] = []
    warnings: list[JsonValue] = []
    counts: dict[str, int] = {}
    by_source: dict[str, int] = {}
    total = decisions = latency_count = automatic = expired = 0
    latency_sum = 0.0
    complete = True
    def collect(scope: ActionScope, response: dict[str, JsonValue] | Exception) -> None:
        """
        立即归并一个来源并回收超出当前页的候选, 不累计全部平台的详情

        参数:
        - scope: 当前固定平台
        - response: 当前只读结果或已隔离失败
        """
        nonlocal complete, total, decisions, latency_count, latency_sum, automatic, expired
        if isinstance(response, Exception):
            logger.warning(f'[统一审批] 目录读取失败 platform={scope.platform_id}: {type(response).__name__}')
            warnings.append(f'{scope.platform_id}: action_storage_unavailable')
            complete = False
            platforms.append({'platform_id': scope.platform_id, 'current_account': scope.current_account, 'online': scope.online,
                              'accounts': [scope.current_account] if scope.current_account else [], 'invalid_count': 0, 'read_state': 'error'})
            return
        items.extend(cast(list[dict[str, JsonValue]], response['items']))
        total += cast(int, response['total'])
        decisions += cast(int, response['today_decided'])
        latency_count += cast(int, response['latency_count'])
        latency_sum += cast(float, response['latency_sum_ms'])
        automatic += cast(int, response['automatic_count'])
        expired += cast(int, response['expired_count'])
        for field, target in (('by_state', counts), ('by_source', by_source)):
            for name, count in cast(dict[str, int], response[field]).items():
                target[name] = target.get(name, 0) + count
        platforms.append({name: response[name] for name in ('platform_id', 'current_account', 'online', 'accounts', 'invalid_count')})
        if response['invalid_count'] or not scope.current_account:
            complete = False
            warnings.append(f'{scope.platform_id}: ' + ('invalid_action_records' if response['invalid_count'] else 'account_unconfirmed'))
        items.sort(key=lambda item: tuple(cast(list[object], item['_sort'])))
        del items[query.limit + 1:]

    concurrency = asyncio.Semaphore(4)
    async def read_scope(scope: ActionScope) -> None:
        """
        限制同一目录请求的入队数, 一个故障不取消其它平台

        参数:
        - scope: 当前配置的固定范围
        """
        async with concurrency:
            try:
                response = await ACTION_READS.run(_read_unified, scope, query, summary=summary)
            except Exception as error:
                collect(scope, error)
            else:
                collect(scope, response)
    await asyncio.gather(*(read_scope(scope) for scope in chosen))
    platforms.sort(key=lambda item: str(item.get('platform_id')) if isinstance(item, dict) else '')
    items.sort(key=lambda item: tuple(cast(list[object], item['_sort'])))
    has_more = len(items) > query.limit
    selected = items[:query.limit]
    cursor = query.cursor(tuple(cast(list[object], selected[-1]['_sort']))) if has_more else None
    for item in selected:
        item.pop('_sort', None)
    return {'generated_at': time.time(), 'items': cast(list[JsonValue], selected), 'total': total, 'complete': complete, 'warnings': warnings, 'platforms': platforms,
            'pending_count': counts.get('pending', 0), 'by_state': cast(dict[str, JsonValue], counts), 'by_source': cast(dict[str, JsonValue], by_source), 'today_decided': decisions,
            'automatic_count': automatic, 'expired_count': expired,
            'decision_latency_ms': latency_sum / latency_count if latency_count else None,
            'window': {'from': query.start, 'to': query.end, 'tz': query.timezone, 'bucket': None},
            'has_more': has_more, 'next_cursor': cursor}


def cold_action_scopes(layout: StorageLayout, platform_ids: list[str]) -> list[ActionScope]:
    """
    为离线目录读取当前配置中的平台, 当前账号采用最后一次明确绑定记录

    参数:
    - layout: 宿主配置对应的存储根
    - platform_ids: 配置中允许管理的平台, 不来自用户数据库路径

    返回:
    - 全部标为离线的范围, 账号无法唯一确认时保留未知
    """
    scopes: list[ActionScope] = []
    for platform_id in platform_ids:
        path = layout.platform_db(platform_id)
        account = ''
        if path.is_file():
            try:
                with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=3)) as db:
                    db.execute('PRAGMA query_only=ON')
                    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='group_accounts'").fetchone():
                        rows = db.execute('SELECT self_id,last_bound_at FROM group_accounts ORDER BY last_bound_at DESC LIMIT 2').fetchall()
                        if rows and (len(rows) == 1 or rows[0][1] > rows[1][1]):
                            account = str(rows[0][0])
            except (OSError, sqlite3.Error) as error:
                logger.warning(f'[统一审批] 离线账号读取失败 platform={platform_id}: {type(error).__name__}')
        scopes.append(ActionScope(platform_id, path, account, False))
    return scopes
