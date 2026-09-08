from __future__ import annotations
import threading
from satrap.edictum.plugin import Plugin
from satrap.core.log import logger
from .utils import _assert_sync_callbacks, SessionHandler


class _HandlerRegistryMixin:
    """Handler 注册表: 增删启停 + 优先级 + 排序 (同步/异步会话共用)"""

    def _init_handler_registry(self) -> None:
        """初始化注册表与插件表 (宿主 __init__ 必须调用; 忘调时合成过滤 AttributeError 快速失败)"""
        self._handlers: dict[str, SessionHandler] = {}
        self._plugins: dict[str, Plugin] = {}
        self._handler_seq = 0
        """全局注册序号计数器: 同 priority 时按 _seq 排序"""
        self._registry_lock = threading.RLock()
        """注册表锁: 防护跨线程并发 (同线程协程交错由 run_lock 串行化兜底)"""
        self._active_runs = 0
        """进行中的 run 计数 (延迟 close 决策依据)"""
        self._pending_close: list[SessionHandler] = []
        """延迟 close 队列: run 结束后冲刷 (防 run 快照持有的 handler 被提前 close)"""

    def _validate_handler(self, handler: SessionHandler) -> None:
        """
        公共校验: name 非空 + timeout 非负 (add_handler 与插件安装共用)

        参数:
        - handler: 处理器
        """
        if not handler.name:
            raise ValueError("处理器 name 不能为空")
        if handler.timeout is not None and handler.timeout < 0:
            raise ValueError(f"处理器 {handler.name} 的 timeout 不能为负数")

    def add_handler(self, handler: SessionHandler, replace: bool = False) -> bool:
        """
        注册处理器 (协议级校验: 拒绝异步回调; 同名冲突抛 ValueError; replace=True 覆盖并 close 旧对象)

        参数:
        - handler: 处理器
        - replace: replace 输入值

        返回:
        - bool: 注册处理器 (协议级校验: 拒绝异步回调; 同名冲突抛 ValueError; replace=True 覆盖并 close 旧对象)
        """
        self._validate_handler(handler)
        _assert_sync_callbacks(handler)
        old: SessionHandler | None = None
        with self._registry_lock:
            prev = self._handlers.get(handler.name)
            if prev is not None and prev is not handler:
                if not replace:
                    raise ValueError(
                        f"处理器 {handler.name} 已存在 (replace=True 显式覆盖)"
                    )
                if prev.owner_plugin is not None:
                    logger.info(
                        f"[edictum] 覆盖插件 {prev.owner_plugin} 的处理器 {handler.name}, 继承归属"
                    )
                    handler.owner_plugin = handler.owner_plugin or prev.owner_plugin
                self._sync_owner_roster(prev)
                if self._active_runs > 0:
                    self._pending_close.append(prev)
                else:
                    old = prev
            self._handler_seq += 1
            handler._seq = self._handler_seq
            self._handlers[handler.name] = handler
        if old is not None:
            self._close_handler(old.name, old)
        return True

    def remove_handler(self, name: str) -> bool:
        """
        注销处理器 (run 进行中延迟 close 到 run 结束; 否则立即 close)

        参数:
        - name: 名称

        返回:
        - bool: 注销处理器 (run 进行中延迟 close 到 run 结束; 否则立即 close)
        """
        with self._registry_lock:
            existed = name in self._handlers
            handler = self._take_handler_locked(name)
        if handler is not None:
            self._close_handler(name, handler)
        return existed

    def enable_handler(self, name: str) -> bool:
        """
        启用处理器 (独立位)

        参数:
        - name: 名称

        返回 True 表示独立位已更新; 实际生效 = 独立位 AND 插件聚合开关,
        以 list_capabilities 合成值为准

        返回:
        - bool: 启用处理器 (独立位)
        """
        with self._registry_lock:
            handler = self._handlers.get(name)
            if handler is None:
                return False
            handler.enabled = True
        return True

    def disable_handler(self, name: str) -> bool:
        """
        停用处理器 (独立位; 所属插件禁用时执行路径合成仍过滤)

        参数:
        - name: 名称

        返回:
        - bool: 停用处理器 (独立位; 所属插件禁用时执行路径合成仍过滤)
        """
        with self._registry_lock:
            handler = self._handlers.get(name)
            if handler is None:
                return False
            handler.enabled = False
        return True

    def list_handlers(self) -> list[SessionHandler]:
        """
        列出处理器 (按优先级升序, 同值按注册序)

        返回:
        - list[SessionHandler]: 列出处理器 (按优先级升序, 同值按注册序)
        """
        with self._registry_lock:
            return sorted(self._handlers.values(), key=lambda h: (h.priority, h._seq))

    def set_handler_priority(self, name: str, priority: int) -> bool:
        """
        调整处理器优先级 (越小越先执行)

        参数:
        - name: 名称
        - priority: 优先级

        返回:
        - bool: 调整处理器优先级 (越小越先执行)
        """
        with self._registry_lock:
            handler = self._handlers.get(name)
            if handler is None:
                return False
            handler.priority = priority
        return True

    def _enabled_handlers(self) -> list[SessionHandler]:
        """
        返回生效中的处理器快照: 独立启用 AND 所属插件启用, 按优先级升序 (同值按注册序)

        一致性快照: 同一次 run 内 handler 增删/启停/优先级变更不生效, 下次 run 生效

        返回:
        - list[SessionHandler]: 生效中的处理器快照: 独立启用 AND 所属插件启用, 按优先级升序 (同值按注册序)
        """
        with self._registry_lock:
            return sorted(
                (
                    h
                    for h in self._handlers.values()
                    if h.enabled
                    and (
                        h.owner_plugin is None
                        or (
                            self._plugins.get(h.owner_plugin) is not None
                            and self._plugins[h.owner_plugin].enabled
                        )
                    )
                ),
                key=lambda h: (h.priority, h._seq),
            )

    def _take_handler_locked(self, name: str) -> SessionHandler | None:
        """
        从注册表移除 handler (调用方须持 _registry_lock)

        参数:
        - name: 名称

        同步清理所属插件名册; run 进行中 -> 入延迟 close 队列并返回 None, 否则返回 handler

        返回:
        - SessionHandler | None: 从注册表移除 handler (调用方须持 _registry_lock)
        """
        handler = self._handlers.pop(name, None)
        if handler is None:
            return None
        self._sync_owner_roster(handler)
        if self._active_runs > 0:
            self._pending_close.append(handler)
            return None
        return handler

    def _sync_owner_roster(self, handler: SessionHandler) -> None:
        """
        remove/replace 时同步清理所属插件的 handlers 名册, 防 list_capabilities 残留
        (调用方须持 _registry_lock)

        参数:
        - handler: 处理器
        """
        owner = handler.owner_plugin
        if owner is None:
            return
        plugin = self._plugins.get(owner)
        if plugin is not None:
            plugin.handlers.pop(handler.name, None)

    @staticmethod
    def _close_handler(name: str, handler: SessionHandler) -> None:
        """
        调用 handler.close(), 异常记日志不阻断

        参数:
        - name: 名称
        - handler: 处理器
        """
        try:
            handler.close()
        except Exception as e:
            logger.warning(f"[edictum] 处理器 {name}.close() 异常: {e}")
