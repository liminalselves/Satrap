"""
诊断正文的不可变共享编码

消息正文按内容共享, 有序列表通过固定块和二叉目录共享前缀,
避免每个节点完整复制历史引用, 原始用户 JSON 与内部节点类型分离
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import zlib
from typing import cast

from .contracts import DebugData


def encoded_json(value: DebugData) -> bytes:
    """
    固定诊断 JSON 编码, 保留消息顺序且拒绝非有限数值

    参数:
    - value: 已在采集边界冻结的 JSON 数据

    返回:
    - 可用于共享摘要的 UTF-8 字节
    """
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


class DebugBodyCodec:
    """在调用方短事务中保存正文和引用关系, 不持有业务数据库"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        """
        绑定当前批次连接, 已见集合只在本批有效

        参数:
        - connection: 诊断数据库连接
        """
        self.db = connection
        self._seen: set[str] = set()

    @staticmethod
    def initialize(db: sqlite3.Connection) -> None:
        """
        创建独立正文池及有向引用边

        参数:
        - db: 已开启外键校验的连接
        """
        db.executescript("""
            CREATE TABLE IF NOT EXISTS debug_bodies (
                id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, encoding TEXT NOT NULL,
                raw_bytes INTEGER NOT NULL, body BLOB NOT NULL
            );
            CREATE TABLE IF NOT EXISTS debug_body_edges (
                parent INTEGER NOT NULL REFERENCES debug_bodies(id) ON DELETE CASCADE,
                child INTEGER NOT NULL REFERENCES debug_bodies(id) ON DELETE CASCADE,
                PRIMARY KEY(parent, child)
            ) WITHOUT ROWID;
            CREATE INDEX IF NOT EXISTS debug_body_children ON debug_body_edges(child);
        """)

    def _put(self, kind: str, value: DebugData, children: tuple[str, ...] = ()) -> str:
        """
        保存有类型标识的不可变节点

        参数:
        - kind: 内部节点类型, 不由用户正文决定
        - value: 节点资料
        - children: 已保存的子节点摘要

        返回:
        - 包含格式版本和类型的内容摘要
        """
        raw = encoded_json(value)
        key = hashlib.sha256(b"debug-v1\0" + kind.encode("ascii") + b"\0" + raw).hexdigest()
        if key not in self._seen:
            compressed = zlib.compress(raw, 1) if len(raw) >= 256 else raw
            use_compressed = len(compressed) + 16 < len(raw)
            self.db.execute("INSERT OR IGNORE INTO debug_bodies(key,kind,encoding,raw_bytes,body) VALUES(?,?,?,?,?)",
                            (key, kind, "zlib1" if use_compressed else "json", len(raw), compressed if use_compressed else raw))
            if children:
                identities = (key, *set(children))
                ids = dict(self.db.execute("SELECT key,id FROM debug_bodies WHERE key IN (" + ",".join("?" for _ in identities) + ")", identities))
                self.db.executemany("INSERT OR IGNORE INTO debug_body_edges VALUES(?,?)", ((ids[key], ids[child]) for child in set(children)))
            self._seen.add(key)
        return key

    def put(self, value: DebugData) -> str:
        """
        共享实际请求中的消息与固定参数

        参数:
        - value: 已脱敏且有界的实际正文

        返回:
        - 可独立还原的根节点摘要, 不依赖当前聊天历史
        """
        if isinstance(value, dict) and isinstance(value.get("messages"), list):
            messages = cast(list[DebugData], value["messages"])
            fields = self._put("json", {name: item for name, item in value.items() if name != "messages"})
            sequence = self._sequence(messages)
            return self._put("request", {"fields": fields, "messages": sequence}, (fields, sequence))
        return self._put("json", value)

    def _sequence(self, messages: list[DebugData]) -> str:
        """
        用稳定分块与平衡目录共享有序列表

        参数:
        - messages: 实际发送消息, 顺序保持不变

        返回:
        - 有限深度的序列根, 相同历史块不重复写入
        """
        blocks: list[str] = []
        for offset in range(0, len(messages), 32):
            items = tuple(self._put("json", value) for value in messages[offset:offset + 32])
            blocks.append(self._put("sequence_leaf", list(items), items))
        if not blocks:
            return self._put("sequence_leaf", [])

        def directory(start: int, stop: int) -> str:
            """
            按最大二次幂前缀建立稳定二叉目录

            参数:
            - start: 包含的块起点
            - stop: 不包含的块终点

            返回:
            - 可共享前缀的目录节点
            """
            size = stop - start
            if size == 1:
                return blocks[start]
            prefix = 1 << ((size - 1).bit_length() - 1)
            left, right = directory(start, start + prefix), directory(start + prefix, stop)
            return self._put("sequence_branch", [left, right], (left, right))

        return directory(0, len(blocks))

    def read(self, key: str, *, max_bytes: int = 64_000_000) -> DebugData:
        """
        批量读取有界引用集合并校验正文摘要

        参数:
        - key: 经事件关联校验后传入的根摘要
        - max_bytes: 允许解码的唯一节点原始字节合计

        返回:
        - 原始 JSON 值; 缺失, 损坏, 超预算或循环引用时拒绝读取
        """
        if len(key) != 64 or any(char not in "0123456789abcdef" for char in key):
            raise ValueError("诊断正文标识无效")
        rows = self.db.execute("""
            WITH RECURSIVE reachable(id) AS (
                SELECT id FROM debug_bodies WHERE key=? UNION SELECT e.child FROM debug_body_edges e JOIN reachable r ON e.parent=r.id LIMIT 50001
            ) SELECT b.key,b.kind,b.encoding,b.raw_bytes,length(b.body) FROM debug_bodies b
              JOIN reachable r ON b.id=r.id LIMIT 50001
        """, (key,)).fetchall()
        if not rows or len(rows) > 50000:
            raise ValueError("诊断正文不存在或引用数量超限")
        if any(row[3] < 0 or row[4] < 0 for row in rows) or sum(max(row[3], row[4]) for row in rows) > max_bytes:
            raise ValueError("诊断正文解码预算不足")
        decoded: dict[str, tuple[str, DebugData]] = {}
        sizes: dict[str, int] = {}
        for identity, kind, encoding, raw_bytes, _ in rows:
            body = self.db.execute("SELECT body FROM debug_bodies WHERE key=?", (identity,)).fetchone()[0]
            if not 0 <= raw_bytes <= max_bytes:
                raise ValueError("诊断正文长度无效")
            if encoding == "zlib1":
                inflater = zlib.decompressobj()
                raw = inflater.decompress(body, raw_bytes + 1)
                if not inflater.eof or inflater.unused_data or inflater.unconsumed_tail:
                    raise ValueError("诊断压缩正文无效")
            elif encoding == "json":
                raw = body
            else:
                raise ValueError("诊断正文编码不支持")
            if len(raw) != raw_bytes or hashlib.sha256(b"debug-v1\0" + kind.encode("ascii") + b"\0" + raw).hexdigest() != identity:
                raise ValueError("诊断正文完整性校验失败")
            decoded[identity] = (kind, cast(DebugData, json.loads(raw)))
            sizes[identity] = raw_bytes
        active: set[str] = set()
        expansion = [max_bytes, 100000]

        def restore(identity: str) -> DebugData:
            """
            还原受控节点类型, 不解释用户 JSON 中的相似标记

            参数:
            - identity: 当前节点摘要

            返回:
            - 消息或列表原文
            """
            if identity in active or len(active) > 64 or identity not in decoded:
                raise ValueError("诊断正文引用缺失或循环")
            expansion[1] -= 1
            if expansion[1] < 0:
                raise ValueError("诊断正文展开次数超限")
            active.add(identity)
            kind, value = decoded[identity]
            if kind in {"sequence_leaf", "sequence_branch"}:
                if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                    raise ValueError("诊断序列格式无效")
                items: list[DebugData] = []
                for child in value:
                    part = restore(cast(str, child))
                    if kind == "sequence_branch":
                        if not isinstance(part, list):
                            raise ValueError("诊断序列目录无效")
                        items.extend(part)
                    else:
                        items.append(part)
                result: DebugData = items
            elif kind == "request":
                if not isinstance(value, dict) or not isinstance(value.get("fields"), str) or not isinstance(value.get("messages"), str):
                    raise ValueError("诊断模型请求格式无效")
                fields, messages = restore(cast(str, value["fields"])), restore(cast(str, value["messages"]))
                if not isinstance(fields, dict) or not isinstance(messages, list):
                    raise ValueError("诊断模型请求引用无效")
                result = {**fields, "messages": messages}
            elif kind == "json":
                expansion[0] -= sizes[identity]
                if expansion[0] < 0:
                    raise ValueError("诊断正文展开字节超限")
                result = value
            else:
                raise ValueError("诊断正文类型不支持")
            active.remove(identity)
            return result

        return restore(key)

    def read_delta(self, key: str, previous_key: str = "", *, max_bytes: int = 64_000_000) -> DebugData:
        """
        生成模型输入的浏览增量, 原始记录与完整导出保持独立

        参数:
        - key: 当前已授权正文根
        - previous_key: 同请求同父步骤内的上一份模型输入, 首次调用为空
        - max_bytes: 两份快照合计解码预算, 默认 64 MB

        返回:
        - 新增或变更的消息及参数, 无 messages 的正文原样返回
        """
        # Step.1 独立还原当前快照, 共享存储不转换为依赖历史请求的差量记录
        current = self.read(key, max_bytes=max_bytes // 2)
        if not isinstance(current, dict) or not isinstance(current.get("messages"), list):
            return current
        messages = cast(list[DebugData], current["messages"])
        previous: DebugData = None
        unavailable = False
        if previous_key:
            try:
                previous = self.read(previous_key, max_bytes=max_bytes // 2)
            except ValueError:
                unavailable = True   # 旧正文缺失不伪装为没有变化, 当前快照仍可单独浏览
        old = previous.get("messages") if isinstance(previous, dict) else None
        # Step.2 仅省略确认相同的前后片段, 中间修改和删除均明确报告
        prefix, suffix = 0, 0
        if isinstance(old, list):
            while prefix < min(len(messages), len(old)) and messages[prefix] == old[prefix]:
                prefix += 1
            while suffix < min(len(messages), len(old)) - prefix and messages[-1 - suffix] == old[-1 - suffix]:
                suffix += 1
            removed = len(old) - prefix - suffix
            basis = "previous_model_input"
        else:
            last_user = next((index for index in range(len(messages) - 1, -1, -1)
                              if isinstance(messages[index], dict) and cast(dict[str, DebugData], messages[index]).get("role") == "user"), None)
            prefix = last_user if last_user is not None else 0
            removed, basis = 0, "latest_user_message" if last_user is not None else "initial_input"
        # Step.3 参数变化单独展示, 模型切换和上下文重写不伪装成仅有新消息
        changed = {name: value for name, value in current.items() if name != "messages"
                   and (not isinstance(old, list) or not isinstance(previous, dict) or name not in previous or previous[name] != value)}
        removed_parameters = [name for name in previous if name != "messages" and name not in current] if isinstance(previous, dict) and isinstance(old, list) else []
        return {"view": "delta", "basis": basis, "baseline_unavailable": unavailable,
                "total_messages": len(messages), "hidden_prefix_messages": prefix, "hidden_suffix_messages": suffix,
                "removed_messages": removed, "messages": messages[prefix:len(messages) - suffix if suffix else len(messages)],
                "changed_parameters": changed, "removed_parameters": cast(list[DebugData], removed_parameters)}
