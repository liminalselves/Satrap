"""聊天插件注册表: 扫描插件目录 + 独立 json 记录启用状态

与平台后端隔离:
- 扫描 satrap/expend/plugins (官方) + .satrap/plugins (用户) 拿插件清单,
  但默认不安装 —— 清单来自 meta.yaml 的能力声明 (parse_capability_descriptions)
- 启用状态独立记录于 .satrap/chat_plugins.json (不碰 session_class_config.json),
  前端勾选后由 ChatService 对活动会话执行 install_plugin / uninstall_plugin

启用状态 json 结构:
{
  "satrap_coding": {
    "enabled": true,
    "capabilities": {"tools": {"shell": false}, "skills": {}, ...}
  }
}
- enabled: 插件聚合开关 (默认 false = 扫描到但不装)
- capabilities: 能力独立启用状态 (默认 true), 能力生效 = 插件启用 ∧ 独立启用
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, cast

from satrap.core.log import logger
from satrap.core.utils.paths import get_data_dir
from satrap.edictum.plugin import (
    PLUGINS_PRESET_DIR,
    USER_PLUGINS_DIR,
    CAPABILITY_KINDS,
    load_plugin_meta,
    parse_capability_descriptions,
)

# 能力类别中文名 (供前端展示)
CAPABILITY_LABELS = {
    "tools": "工具",
    "skills": "技能",
    "mcp": "MCP",
    "handlers": "处理器",
    "commands": "命令",
}


def _default_state_path() -> Path:
    """插件启用状态 json 默认路径 (.satrap/chat_plugins.json)"""
    return get_data_dir() / "chat_plugins.json"


class ChatPluginRegistry:
    """聊天插件注册表: 扫描清单 + 启用状态管理

    线程安全: json 读写与内存状态由锁保护
    """

    def __init__(self, state_path: str | Path | None = None) -> None:
        self._lock = threading.RLock()
        self.state_path = Path(state_path) if state_path else _default_state_path()
        # name -> {"enabled": bool, "capabilities": {kind: {cap: bool}}}
        self._states: dict[str, dict[str, Any]] = {}
        self._load()

    # ---------------- 状态持久化 ----------------

    def _load(self) -> None:
        """读 json 启用状态 (不存在则空)"""
        with self._lock:
            self._states = {}
            if not self.state_path.is_file():
                return
            try:
                raw = json.loads(self.state_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    for name, st in cast(dict[str, Any], raw).items():
                        if isinstance(st, dict):
                            st_dict = cast(dict[str, Any], st)
                            self._states[str(name)] = {
                                "enabled": bool(st_dict.get("enabled", False)),
                                "capabilities": self._normalize_caps(st_dict.get("capabilities")),
                            }
            except (OSError, ValueError) as e:
                logger.warning(f"[聊天插件] 读取启用状态失败, 已重置: {e}")

    @staticmethod
    def _normalize_caps(raw: Any) -> dict[str, dict[str, bool]]:
        """规范化能力状态为 {kind: {cap: bool}} (仅保留合法类别)"""
        caps: dict[str, dict[str, bool]] = {k: {} for k in CAPABILITY_KINDS}
        if not isinstance(raw, dict):
            return caps
        raw_dict = cast(dict[str, Any], raw)
        for kind in CAPABILITY_KINDS:
            sub: Any = raw_dict.get(kind)
            if isinstance(sub, dict):
                caps[kind] = {str(c): bool(v) for c, v in cast(dict[Any, Any], sub).items()}
        return caps

    def _save(self) -> None:
        """写 json 启用状态 (调用方需已持有锁)"""
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(
                json.dumps(self._states, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as e:
            logger.error(f"[聊天插件] 写入启用状态失败: {e}")

    # ---------------- 扫描 ----------------

    def scan(self) -> list[dict[str, Any]]:
        """扫描插件目录, 返回清单 (合并启用状态)

        官方目录优先 (同名冲突时覆盖用户目录); 能力清单来自 meta.yaml 声明
        """
        found: dict[str, Path] = {}
        for base in (USER_PLUGINS_DIR, PLUGINS_PRESET_DIR):
            if not base.is_dir():
                continue
            for child in sorted(base.iterdir()):
                if child.is_dir() and (child / "meta.yaml").is_file():
                    try:
                        meta = load_plugin_meta(child)
                        name = str(meta.get("name") or "").strip()
                        if name:
                            found[name] = child  # 后写覆盖: 官方目录后扫, 优先级高
                    except ValueError as e:
                        logger.warning(f"[聊天插件] 跳过无效插件 {child}: {e}")

        result: list[dict[str, Any]] = []
        with self._lock:
            for name, pdir in found.items():
                meta = load_plugin_meta(pdir)
                declared = parse_capability_descriptions(meta)
                state = self._states.get(name, {"enabled": False, "capabilities": {}})
                cap_states = state.get("capabilities", {})
                capabilities: dict[str, list[dict[str, Any]]] = {}
                for kind in CAPABILITY_KINDS:
                    items: list[dict[str, Any]] = []
                    for cap_name, desc in declared.get(kind, {}).items():
                        items.append({
                            "name": cap_name,
                            "description": desc,
                            "enabled": bool(cap_states.get(kind, {}).get(cap_name, True)),
                        })
                    capabilities[kind] = items
                result.append({
                    "name": name,
                    "version": str(meta.get("version") or ""),
                    "description": str(meta.get("description") or ""),
                    "dir": str(pdir),
                    "enabled": bool(state.get("enabled", False)),
                    "capabilities": capabilities,
                })
        return result

    def get_plugin_dir(self, name: str) -> Path | None:
        """取插件目录 (官方优先), 供 install_plugin 使用"""
        for base in (PLUGINS_PRESET_DIR, USER_PLUGINS_DIR):
            pdir = base / name
            if pdir.is_dir() and (pdir / "meta.yaml").is_file():
                return pdir
        return None

    # ---------------- 启用状态 ----------------

    def is_enabled(self, name: str) -> bool:
        """插件是否启用"""
        with self._lock:
            return bool(self._states.get(name, {}).get("enabled", False))

    def set_enabled(self, name: str, enabled: bool) -> None:
        """设置插件聚合开关并持久化"""
        with self._lock:
            st = self._states.setdefault(name, {"enabled": False, "capabilities": {}})
            st["enabled"] = bool(enabled)
            self._save()

    def set_capability(self, name: str, kind: str, cap: str, enabled: bool) -> bool:
        """设置能力独立启用状态并持久化; 类别非法返回 False"""
        if kind not in CAPABILITY_KINDS:
            return False
        with self._lock:
            st = self._states.setdefault(name, {"enabled": False, "capabilities": {}})
            caps = self._normalize_caps(st.get("capabilities"))
            caps[kind][cap] = bool(enabled)
            st["capabilities"] = caps
            self._save()
        return True

    def capability_enabled(self, name: str, kind: str, cap: str) -> bool:
        """能力独立启用状态 (默认 True)"""
        with self._lock:
            caps = self._states.get(name, {}).get("capabilities", {})
            return bool(caps.get(kind, {}).get(cap, True))
