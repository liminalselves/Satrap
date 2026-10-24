"""使用独立内存 SQLite 测量生产会话目录查询, 不访问用户数据库"""
from __future__ import annotations

import ast
import json
from pathlib import Path
import sqlite3
import statistics
import time
from types import SimpleNamespace
from typing import Any, cast


def run() -> None:
    """加载生产查询函数, 使用固定数据记录三十次正式采样"""
    root = Path(__file__).resolve().parents[2]
    source = (root / 'satrap/display/recorder.py').read_text(encoding='utf-8')
    names = {'query_conversations', 'list_conversations', '_conversation_rows'}
    functions = '\n\n'.join(ast.get_source_segment(source, node) or '' for node in ast.parse(source).body
                            if isinstance(node, ast.FunctionDef) and node.name in names)
    results = []

    class MemoryPath:
        """提供内存数据库存在性, 避免生产函数创建磁盘文件"""

        def __init__(self, value: str) -> None:
            """
            绑定合成数据库 URI

            参数:
            - value: 当前内存数据库地址
            """
            self.value = value

        def exists(self) -> bool:
            """
            声明合成数据库已存在

            返回:
            - 固定为 True
            """
            return True

        def __str__(self) -> str:
            """
            返回合成数据库 URI

            返回:
            - 构造时绑定的地址
            """
            return self.value

    for count in (100, 1000, 5000):
        uri = f'file:loading_{count}?mode=memory&cache=shared'
        with sqlite3.connect(uri, uri=True) as connection:
            connection.executescript('CREATE TABLE conversation_meta(conversation_id TEXT PRIMARY KEY, project_id TEXT, model TEXT, think TEXT, created_at REAL); CREATE TABLE display_turns(id INTEGER PRIMARY KEY, conversation_id TEXT, turn_index INTEGER, user_input TEXT, created_at REAL); CREATE INDEX idx_conv_turn ON display_turns(conversation_id, turn_index);')
            connection.executemany('INSERT INTO conversation_meta VALUES(?, NULL, ?, ?, ?)', ((str(index), 'model', 'off', index) for index in range(count)))
            connection.executemany('INSERT INTO display_turns(conversation_id, turn_index, user_input, created_at) VALUES(?, ?, ?, ?)', ((str(index), turn, 'synthetic', index + turn) for index in range(count) for turn in range(10)))
            connection.commit()
            namespace: dict[str, Any] = {'sqlite3': SimpleNamespace(connect=lambda *args, **kwargs: sqlite3.connect(uri, uri=True)), 'Path': MemoryPath, 'cast': cast, 'Any': Any, 'time': time}
            exec(compile('from __future__ import annotations\n' + functions, '<query-loading>', 'exec'), namespace)
            for name, arguments in (('query_conversations', {'db_path': uri, 'page_size': 50}), ('list_conversations', {'db_path': uri})):
                samples = []
                for iteration in range(32):
                    started = time.perf_counter()
                    namespace[name](**arguments)
                    elapsed = (time.perf_counter() - started) * 1000
                    if iteration >= 2:
                        samples.append(elapsed)
                results.append({'conversations': count, 'turns': count * 10, 'function': name, 'p50_ms': statistics.median(samples), 'p95_ms': sorted(samples)[28], 'samples_ms': samples})
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    run()
