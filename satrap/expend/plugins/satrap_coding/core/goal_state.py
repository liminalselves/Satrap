"""
satrap_coding 持续目标状态机: 目标 + 子任务 todo + 持久化

- /goal <目标>: 设置持续追求的目标, 注入每轮模型输入直至完成
- 状态: active / done, 附带子任务列表 (todo)
- 持久化: .satrap/coding/goal.json (按 session_id 隔离), 重开会话仍在
"""
from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from satrap.core.utils.paths import get_data_dir

DEFAULT_GOAL_FILE = get_data_dir() / "coding" / "goal.json"

_FILE_LOCK = threading.Lock()
# 状态文件全局锁: 串行化所有实例的读-改-写, 防多会话交错写丢失更新


class GoalState:
    """持续目标状态机 (线程安全)"""

    def __init__(self, file_path: str | Path | None = None) -> None:
        """
        参数:
        - file_path: 状态文件路径, 默认 .satrap/coding/goal.json
        """
        self.file_path = Path(file_path or DEFAULT_GOAL_FILE)
        self._lock = threading.RLock()
        self._goals: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        """加载持久化目标状态"""
        try:
            raw = json.loads(self.file_path.read_text(encoding="utf-8"))
            data = cast(dict[str, Any], raw).get("goals", {})
            self._goals = {
                str(k): cast(dict[str, Any], v)
                for k, v in data.items()
                if isinstance(k, str) and isinstance(v, dict)
            }
        except (FileNotFoundError, json.JSONDecodeError, ValueError, TypeError):
            self._goals = {}

    def _save(self) -> None:
        """原子写状态文件"""
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"goals": self._goals}
        tmp = self.file_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.file_path)

    def _refresh_from_disk(self) -> None:
        """写前以磁盘为基线重建目标状态 (需持有 _FILE_LOCK), 防多会话丢失更新"""
        self._load()

    # ---------- 目标 CRUD ----------

    def set_goal(self, session_id: str, text: str) -> dict[str, Any]:
        """
        设置 (或覆盖) 会话目标, 状态回到 active

        参数:
        - session_id: 会话 ID
        - text: 待处理文本

        返回:
        - dict[str, Any]: 设置 (或覆盖) 会话目标, 状态回到 active
        """
        if not text.strip():
            raise ValueError("目标不能为空")
        now = datetime.now().isoformat(timespec="seconds")
        with _FILE_LOCK:
            self._refresh_from_disk()
            with self._lock:
                goal: dict[str, Any] = {
                    "text": text.strip(),
                    "status": "active",
                    "todos": [],
                    "created_at": now,
                    "updated_at": now,
                }
                self._goals[session_id] = goal
                self._save()
        return goal

    def get_goal(self, session_id: str) -> dict[str, Any] | None:
        """
        获取会话目标 (不存在返回 None)

        参数:
        - session_id: 会话 ID

        返回:
        - dict[str, Any] | None: 会话目标 (不存在返回 None)
        """
        with self._lock:
            goal = self._goals.get(session_id)
            return dict(goal) if goal is not None else None

    def complete_goal(self, session_id: str) -> bool:
        """
        标记目标完成

        参数:
        - session_id: 会话 ID

        返回:
        - bool: 标记目标完成
        """
        with _FILE_LOCK:
            self._refresh_from_disk()
            with self._lock:
                goal = self._goals.get(session_id)
                if goal is None:
                    return False
                goal["status"] = "done"
                goal["updated_at"] = datetime.now().isoformat(timespec="seconds")
                self._save()
                return True

    def clear_goal(self, session_id: str) -> bool:
        """
        清除会话目标

        参数:
        - session_id: 会话 ID

        返回:
        - bool: 清除会话目标
        """
        with _FILE_LOCK:
            self._refresh_from_disk()
            with self._lock:
                removed = self._goals.pop(session_id, None) is not None
                if removed:
                    self._save()
                return removed

    def is_active(self, session_id: str) -> bool:
        """
        目标是否处于 active 状态

        参数:
        - session_id: 会话 ID

        返回:
        - bool: 目标是否处于 active 状态
        """
        goal = self.get_goal(session_id)
        return goal is not None and goal["status"] == "active"

    # ---------- 子任务 todo ----------

    def add_todo(self, session_id: str, item: str) -> bool:
        """
        追加子任务

        参数:
        - session_id: 会话 ID
        - item: 条目

        返回:
        - bool: 追加子任务
        """
        if not item.strip():
            return False
        with _FILE_LOCK:
            self._refresh_from_disk()
            with self._lock:
                goal = self._goals.get(session_id)
                if goal is None or goal["status"] != "active":
                    return False
                goal["todos"].append({"text": item.strip(), "done": False})
                goal["updated_at"] = datetime.now().isoformat(timespec="seconds")
                self._save()
                return True

    def complete_todo(self, session_id: str, index: int) -> bool:
        """
        标记子任务完成 (按序号)

        参数:
        - session_id: 会话 ID
        - index: 索引

        返回:
        - bool: 标记子任务完成 (按序号)
        """
        with _FILE_LOCK:
            self._refresh_from_disk()
            with self._lock:
                goal = self._goals.get(session_id)
                if goal is None:
                    return False
                todos = cast(list[Any], goal["todos"])
                if not 0 <= index < len(todos):
                    return False
                todos[index]["done"] = True
                goal["updated_at"] = datetime.now().isoformat(timespec="seconds")
                self._save()
                return True

    # ---------- 注入格式化 ----------

    def to_context_block(self, session_id: str) -> str:
        """
        生成可注入模型输入的目标块 (无 active 目标返回空串)

        参数:
        - session_id: 会话 ID

        返回:
        - str: 空串)
        """
        goal = self.get_goal(session_id)
        if goal is None or goal["status"] != "active":
            return ""
        lines = [f"<active-goal>{goal['text']}</active-goal>"]
        todos = cast(list[Any], goal.get("todos") or [])
        if todos:
            progress = "\n".join(
                f"- [{'x' if t.get('done') else ' '}] {t.get('text', '')}" for t in todos
            )
            lines.append(f"<goal-progress>\n{progress}\n</goal-progress>")
        lines.append("目标未完成前, 每次回复都应围绕该目标推进, 不得提前结束")
        return "\n".join(lines)

    def format_status(self, session_id: str) -> str:
        """
        生成用户可读的目标状态文本 (/goal status)

        参数:
        - session_id: 会话 ID

        返回:
        - str: 生成用户可读的目标状态文本 (/goal status)
        """
        goal = self.get_goal(session_id)
        if goal is None:
            return "当前没有设置目标"
        todos = cast(list[Any], goal.get("todos") or [])
        lines = [f"目标: {goal['text']}", f"状态: {goal['status']}"]
        if todos:
            lines.append("进度:")
            lines.extend(
                f"  {i}. [{'x' if t.get('done') else ' '}] {t.get('text', '')}"
                for i, t in enumerate(todos)
            )
        return "\n".join(lines)
