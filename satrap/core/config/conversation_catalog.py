"""
平台无关的对话分类目录

只读合并会话, 路由, 用户和展示元数据, 优先采用精确上下文归属,
通过适配器扩展标签, 旧数据缺少的来源和时间保持未知
"""
from __future__ import annotations

from typing import Any, TypedDict
from collections.abc import Callable
from itertools import islice
import traceback
import sqlite3
import json

from satrap.core.storage.layout import StorageLayout, CHAT_PLATFORM_ID, LOCAL_PLATFORM_ID
from satrap.core.platform import registry
from satrap.core.log import logger


class ConversationAggregates(TypedDict):
    """同一已筛选目录内的去重资源计数"""

    platforms: int
    conversations: int
    contexts: int
    display_turns: int


def conversation_aggregates(items: list[dict[str, Any]]) -> ConversationAggregates:
    """
    从已有分类元数据计算摘要, 不再次读取任何消息正文

    参数:
    - items: 应用全部目录筛选之后的记录

    返回:
    - 平台, 对话, 实际上下文和展示轮次的去重计数
    """
    roots = {(item["platform_id"], item["conversation_id"]): int(item["history_count"]) for item in items}
    contexts = {(item["platform_id"], context["id"]) for item in items for context in item.get("contexts", [])}
    return ConversationAggregates(platforms=len({key[0] for key in roots}), conversations=len(roots), contexts=len(contexts), display_turns=sum(roots.values()))

MAX_CONVERSATION_ID_LENGTH = 4096   # 约束外部标识驱动的前缀枚举工作量


def platform_catalog(layout: StorageLayout, document: dict[str, Any]) -> list[dict[str, Any]]:
    """
    合并配置和存储清单生成动态平台目录

    参数:
    - layout: 数据布局
    - document: 当前后端配置

    返回:
    - 平台类型, 实例及能力, 已移除的平台仍保留实例并标明未知类型
    """
    values = {
        LOCAL_PLATFORM_ID: {"id": LOCAL_PLATFORM_ID, "type": "local", "label": "本地", "supports_history": False},
        CHAT_PLATFORM_ID: {"id": CHAT_PLATFORM_ID, "type": "chat", "label": "Chat", "supports_history": True},
    }
    for manifest in layout.platforms_root.glob("*/platform.json"):
        try:
            value = json.loads(manifest.read_text(encoding="utf-8"))
            identity = value["platform_id"]
            if not isinstance(identity, str) or not identity.strip():
                raise ValueError("无效的平台实例标识")
            values.setdefault(identity, {"id": identity, "type": value.get("platform_type") or "unknown", "label": identity, "supports_history": False})
        except (OSError, ValueError, KeyError, TypeError) as error:
            logger.warning(f"[对话目录] 平台清单无效: {manifest}, {error}")
    for item in document.get("platforms", []):
        if not isinstance(item, dict) or not item.get("id"):
            logger.warning("[对话目录] 跳过缺少实例标识的平台配置")
            continue
        identity = str(item["id"])
        adapter_type = str(item.get("type") or "unknown")
        adapter = registry.get(adapter_type)
        values[identity] = {"id": identity, "type": adapter_type, "label": identity, "type_label": adapter.display_name or adapter_type if adapter else adapter_type, "supports_history": False}
    for value in values.values():
        value.setdefault("type_label", value["type"])
    return sorted(values.values(), key=lambda item: (item["type"], item["id"]))


def require_catalog_platform(layout: StorageLayout, document: dict[str, Any], platform_id: object,
                             error: Callable[[str, str], Exception]) -> str:
    """
    核验管理目标在配置或保留存储目录中存在, 无需启动适配器

    参数:
    - layout: 当前数据布局
    - document: 当前平台配置
    - platform_id: 请求选择的平台实例
    - error: 入口原有的领域异常构造器

    返回:
    - 已存在的字符串实例 ID, 不创建数据库或推断新实例
    """
    if not isinstance(platform_id, str) or not any(item["id"] == platform_id for item in platform_catalog(layout, document)):
        raise error("not_found", "平台实例不存在")
    return platform_id


def _rows(connection: sqlite3.Connection, tables: set[str], table: str, where: str = "", params: tuple[object, ...] = ()) -> list[dict[str, Any]]:
    """
    读取已有元数据表, 不迁移旧数据库

    参数:
    - connection: 只读连接
    - tables: 已确认存在的表名
    - table: 内部指定的元数据表
    - where: 内部构造的筛选表达式
    - params: 筛选使用的绑定参数

    返回:
    - 命名行列表, 缺表时返回空列表
    """
    return [dict(row) for row in connection.execute(f"SELECT * FROM {table}" + (" WHERE " + where if where else ""), params)] if table in tables else []


def route_metadata(row: dict[str, Any]) -> dict[str, str]:
    """
    解码完整路由键, 旧版用户范围不推断成私聊或某个群

    参数:
    - row: context_sessions 中的路由记录

    返回:
    - 通用路由字段, 无法解码时使用未知范围并记录日志
    """
    result = {key: str(row.get(key) or "") for key in ("user_id", "platform", "provider_name", "session_type", "session_id", "context_key")}
    result["scope"] = "legacy_user"
    key = str(row.get("context_key") or "")
    if key.startswith(("scoped:v1:", "scoped:v2:")):
        try:
            parts = json.loads(key.split(":", 2)[2])
            expected_lengths = {11} if key.startswith("scoped:v2:") else {7, 8}
            if not isinstance(parts, list) or len(parts) not in expected_lengths or any(not isinstance(value, str) for value in parts[:7]):
                raise ValueError("路由字段无效")
            result.update(zip(("platform", "provider_name", "session_type", "scope", "self_id", "group_id", "user_id"), parts[:7]))
            if key.startswith("scoped:v2:"):
                if not isinstance(parts[8], str) or not isinstance(parts[9], str) or type(parts[10]) is not int:
                    raise ValueError("对话路由身份无效")
                result.update(conversation_kind=parts[8], conversation_id=parts[9], binding_generation=str(parts[10]))
        except (ValueError, TypeError) as error:
            logger.warning(f"[对话目录] 路由解码失败: {row.get('session_id')}, {error}")
            result["scope"] = "unknown"
    elif key.startswith("scoped:"):
        logger.warning(f"[对话目录] 未知路由格式: {row.get('session_id')}")
        result["scope"] = "unknown"
    return result


_route_metadata = route_metadata   # 保留旧目录解码入口


def conversation_records(connection: sqlite3.Connection, platform: dict[str, Any] | None = None, requested: str = '') -> list[dict[str, Any]]:
    """
    合并已有数据生成对话和工作流目录

    参数:
    - connection: 使用 sqlite3.Row 的只读连接
    - platform: 动态平台描述, 未提供时保留存储层兼容入口
    - requested: 可选精确对话或上下文 ID, 在 SQL 中限定元数据读取和历史聚合; 超过长度上限时抛出 ValueError

    返回:
    - 带分类标签和真实活动时间的对话记录, 按最近活动排序
    """
    if len(requested) > MAX_CONVERSATION_ID_LENGTH:
        raise ValueError(f"conversation_id 不能超过 {MAX_CONVERSATION_ID_LENGTH} 个字符")
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    sources = [(table, key) for table, key in (
        ('session_configs', 'session_id'), ('conversation_meta', 'conversation_id'),
        ('display_turns', 'conversation_id'), ('context_sessions', 'session_id'), ('context_catalog', 'session_id'),
    ) if table in tables]
    if {'context_sessions', 'context_session_history'}.issubset(tables):
        sources.append(('context_session_history h JOIN context_sessions c ON c.context_key=h.context_key AND h.session_id<>c.session_id', 'h.session_id'))
    # 与下方 roots 的来源一致, 历史路由仅保留仍有关联上下文的记录
    identity = requested
    if requested and 'context_catalog' in tables:
        explicit = connection.execute("SELECT session_id FROM context_catalog WHERE context_id=?", (requested,)).fetchone()
        if explicit and explicit[0]:
            identity = str(explicit[0])
    if requested and identity == requested:
        exact = any(connection.execute(f"SELECT 1 FROM {table} WHERE {key}=?", (identity,)).fetchone() for table, key in sources)
        if not exact:
            positions = (index for index in range(len(requested) - 1, 0, -1) if requested[index] == '_')
            while candidates := [requested[:index] for index in islice(positions, 64)]:
                placeholders = ','.join('?' for _ in candidates)
                matches = [str(row[0]) for table, key in sources
                           for row in connection.execute(f"SELECT DISTINCT {key} FROM {table} WHERE {key} IN ({placeholders})", candidates)]
                if matches:
                    identity = max(matches, key=len)
                    break
            # 先检查所有表的精确键, 再按长度递减分批比较跨表前缀, 不一次性分配全部子串
    configs = {row["session_id"]: row for row in _rows(connection, tables, "session_configs", "session_id=?" if requested else "", (identity,) if requested else ())}
    chat = {row["conversation_id"]: row for row in _rows(connection, tables, "conversation_meta", "conversation_id=?" if requested else "", (identity,) if requested else ())}
    project = chat.get(identity, {}).get('project_id')
    projects = {row["project_id"]: row for row in _rows(connection, tables, "projects", "project_id=?" if requested else "", (project,) if requested else ())}
    context_where = "session_id=? OR context_id=? OR (context_id>=? AND context_id<?)"
    context_args = (identity, identity, identity + '_', identity + '`')
    catalog = {row["context_id"]: row for row in _rows(connection, tables, "context_catalog", context_where if requested else "", context_args if requested else ())}
    routes: dict[str, list[dict[str, Any]]] = {}
    for row in _rows(connection, tables, "context_sessions", "session_id=?" if requested else "", (identity,) if requested else ()):
        routes.setdefault(row["session_id"], []).append(row)
    if {"context_sessions", "context_session_history"}.issubset(tables):
        for row in connection.execute(
            "SELECT c.context_key, c.user_id, c.platform, c.provider_name, c.session_type, "
            "h.session_id, h.first_used_at AS created_at, h.last_used_at "
            "FROM context_session_history h JOIN context_sessions c ON c.context_key=h.context_key "
            "WHERE h.session_id<>c.session_id" + (" AND h.session_id=?" if requested else ""),
            (identity,) if requested else (),
        ):
            routes.setdefault(row["session_id"], []).append(dict(row))
    user_ids = tuple({str(row['user_id']) for rows in routes.values() for row in rows if row.get('user_id')})
    user_where = "user_id IN (" + ",".join("?" for _ in user_ids) + ")" if user_ids else "0"
    users = {str(row["user_id"]): row for row in _rows(connection, tables, "user_info", user_where if requested else "", user_ids if requested else ())}
    turns: dict[str, list[dict[str, Any]]] = {}
    if "display_turns" in tables:
        for row in connection.execute('SELECT t.conversation_id, MIN(t.turn_index) AS turn_index, MAX(t.created_at) AS created_at, COUNT(*) AS turn_count, (SELECT SUBSTR(first.user_input, 1, 100) FROM display_turns first WHERE first.conversation_id=t.conversation_id ORDER BY first.turn_index LIMIT 1) AS user_input FROM display_turns t' + (' WHERE t.conversation_id=?' if requested else '') + ' GROUP BY t.conversation_id', (identity,) if requested else ()):
            turns.setdefault(row["conversation_id"], []).append(dict(row))
    roots = set(configs) | set(chat) | set(turns) | set(routes) | {row["session_id"] for row in catalog.values() if row["session_id"]}
    if requested:
        for table, key in sources:
            roots.update(str(row[0]) for row in connection.execute(
                f"SELECT DISTINCT {key} FROM {table} WHERE {key}=? OR ({key}>=? AND {key}<?)",
                (identity, identity + '_', identity + '`'),
            ))
        # 精确查询也需知道命名空间中的独立子会话根, 避免将它们的历史折叠进父会话
    selected_contexts = tuple(set(catalog) | {identity})
    history_where = "conversation_id IN (" + ",".join("?" for _ in selected_contexts) + ") OR (conversation_id>=? AND conversation_id<?)"
    history_args = (*selected_contexts, identity + '_', identity + '`')
    counts = dict(connection.execute("SELECT conversation_id, COUNT(*) FROM chat_history" + (" WHERE " + history_where if requested else "") + " GROUP BY conversation_id", history_args if requested else ())) if "chat_history" in tables else {}
    if "conversation_data_backups" in tables:
        for row in connection.execute("SELECT DISTINCT conversation_id FROM conversation_data_backups" + (" WHERE " + history_where if requested else ""), history_args if requested else ()):
            counts.setdefault(row[0], 0)
    owners: dict[str, list[str]] = {root: [] for root in roots}
    inferred: set[str] = set()
    ordered_roots = sorted(roots, key=len, reverse=True)
    for context in set(counts) | set(catalog):
        explicit = catalog.get(context, {}).get("session_id")
        owner = explicit or next((root for root in ordered_roots if context == root or context.startswith(root + "_")), context)
        if not explicit and owner != context:
            inferred.add(owner)
        owners.setdefault(owner, []).append(context)
    items = []
    for identity, contexts in owners.items():
        if requested and requested != identity and requested not in contexts:
            continue
        contexts.sort(key=lambda value: (counts.get(value, 0) == 0, value != identity + "_main", catalog.get(value, {}).get("context_kind") != "main", value))
        config = configs.get(identity, {})
        meta = chat.get(identity, {})
        facets: dict[str, list[str]] = {}
        tags: list[str] = []
        labels: dict[str, str] = {}
        def add(key: str, value: object, label: str | None = None) -> None:
            if value is None or value == "":
                return
            text = str(value)
            if text not in facets.setdefault(key, []):
                facets[key].append(text)
            if label:
                labels[key + ":" + text] = label
        add("agent", config.get("session_type_name"))
        add("provider", config.get("provider_name"))
        add("project", meta.get("project_id"), projects.get(meta.get("project_id"), {}).get("name"))
        try:
            settings = json.loads(config.get("session_config") or "{}")
            add("model", meta.get("model") or settings.get("model_name") or settings.get("llm_config_name"))
        except (ValueError, TypeError, AttributeError) as error:
            logger.warning(f"[对话目录] 会话配置无法读取: {identity}, {error}")
        for row in routes.get(identity, []):
            route = _route_metadata(row)
            extra: dict[str, str] = {}
            adapter = registry.get(str((platform or {}).get("type", "")))
            if adapter:
                try:
                    extra = adapter.conversation_catalog_metadata(connection, route)
                    if not isinstance(extra, dict) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in extra.items()):
                        raise ValueError("适配器目录标签必须为文本映射")
                except Exception as error:
                    extra = {}
                    logger.error(f"[对话目录] 适配器标签读取失败: {identity}, {error}\n{traceback.format_exc()}")
                    tags.append("部分标签读取失败")
            scope = route["scope"]
            add("scope", scope, {"legacy_user": "旧版用户共享", "group": "群共享", "group_member": "群成员独立", "private": "私聊独立", "conversation": "对话共享", "unknown": "范围未知"}.get(scope, scope))
            kind = route.get("conversation_kind")
            if kind:
                add("conversation_kind", kind, adapter.conversation_kinds.get(kind, kind) if adapter else kind)
            add("agent", route.get("session_type"))
            add("provider", route.get("provider_name"))
            user = route.get("user_id")
            namespace = str((platform or {}).get("id", ""))
            add("user", f"{namespace}/{user}" if namespace and user else user, users.get(user or "", {}).get("user_nickname") or user)
            target = route.get("group_id")
            if target:
                add("target", ((namespace + "/") if namespace else "") + route.get("self_id", "") + "/" + target, extra.pop("target", None) or target)
            for key, label in extra.items():
                if isinstance(key, str) and isinstance(label, str):
                    add(key, route.get(key) or label, label)
        source = next((catalog[value]["source_kind"] for value in contexts if value in catalog and catalog[value]["source_kind"] != "unknown"), "unknown")
        if routes.get(identity):
            source = "platform"
        elif identity in chat or identity in turns:
            source = "chat"
        add("source", source, {"unknown": "来源未知", "platform": "平台消息", "chat": "Chat", "cli": "命令行", "tui": "终端界面"}.get(source, source))
        legacy_child = identity.startswith("sub_agent_") and identity not in roots
        add("kind", "legacy_child" if legacy_child else "session" if identity in roots else "standalone", "旧子代理记录" if legacy_child else "独立上下文" if identity not in roots else "会话")
        if legacy_child:
            tags.append("旧记录, 归属未验证")
        if identity in inferred:
            tags.append("部分上下文归属按旧命名推断")
        history = sorted(turns.get(identity, []), key=lambda item: item["turn_index"])
        title = str(history[0].get("user_input") or identity)[:100] if history else identity
        for key in ("target", "user"):
            if facets.get(key):
                title = labels.get(key + ":" + facets[key][0], facets[key][0])
                break
        times = [config.get("last_used_at"), *[row.get("created_at") for row in history], *[catalog[value].get("last_message_at") for value in contexts if value in catalog]]
        last_activity = max((value for value in times if isinstance(value, (int, float)) and value > 0), default=None)
        adapter = registry.get(str((platform or {}).get("type", "")))
        field_names = getattr(adapter, "conversation_catalog_fields", {}) if adapter else {}
        if not isinstance(field_names, dict) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in field_names.items()):
            logger.warning(f"[对话目录] 适配器分类字段名称无效: {(platform or {}).get('type')}")
            field_names = {}
        items.append({"conversation_id": identity, "title": title, "message_count": sum(counts.get(value, 0) for value in contexts), "history_count": int(history[0]["turn_count"]) if history else 0, "context_ids": contexts or [identity], "platform_id": (platform or {}).get("id", ""), "platform_type": (platform or {}).get("type", "unknown"), "supports_history": "display_turns" in tables, "facets": facets, "facet_labels": labels, "facet_names": field_names, "tags": tags, "last_activity_at": last_activity, "contexts": [{"id": value, "kind": catalog.get(value, {}).get("context_kind", "unknown"), "name": catalog.get(value, {}).get("workflow_name")} for value in contexts]})
    items.sort(key=lambda item: (-(item["last_activity_at"] or 0), item["platform_id"], item["conversation_id"]))
    return items


def filter_records(items: list[dict[str, Any]], query: str = "", filters: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """
    在通用字段和平台扩展分类中筛选, 默认隐藏旧子代理记录

    参数:
    - items: 合并后的目录记录
    - query: 搜索关键词
    - filters: 分类字段和值, kind=all 时显示全部类别

    返回:
    - 保持原排序的匹配记录
    """
    filters = filters or {}
    matched = []
    for item in items:
        facets = item["facets"]
        if not filters.get("kind") and "legacy_child" in facets.get("kind", []):
            continue
        if any(value and value != "all" and value not in facets.get(key, []) for key, value in filters.items()):
            continue
        text = f"{item['conversation_id']} {item['title']} {' '.join(item['context_ids'])} {json.dumps(facets, ensure_ascii=False)} {json.dumps(item['facet_labels'], ensure_ascii=False)}"
        if query.casefold() in text.casefold():
            matched.append(item)
    return matched


def record_facets(items: list[dict[str, Any]]) -> dict[str, list[dict[str, str]]]:
    """
    从实际记录生成可扩展的筛选选项

    参数:
    - items: 当前平台范围内的完整目录

    返回:
    - 字段到值和标签列表的映射
    """
    options: dict[str, dict[str, str]] = {}
    for item in items:
        for key, values in item["facets"].items():
            for value in values:
                options.setdefault(key, {})[value] = item["facet_labels"].get(key + ":" + value, value)
    return {key: [{"value": value, "label": label} for value, label in sorted(values.items(), key=lambda pair: (pair[1], pair[0]))] for key, values in options.items()}
