"""
插件节点实例生命周期池 (P2-2)

类写法 FlowNode 按节点 id 持一个实例: 会话创建时 setup; 生效配置
或实现变化时新建实例替换, 进行中的求值用旧实例完成后 close;
函数写法无实例状态, 直接返回函数本身; 会话释放时统一 close
"""
from __future__ import annotations

from satrap.core.type.flow.ports import FlowNode

from dataclasses import dataclass, field
from typing import Any, Callable
import asyncio
import hashlib
import inspect
import json

from satrap.core.log import logger


def config_fingerprint(values: dict[str, Any]) -> str:
    """
    生效配置的稳定指纹: 用于判断是否需要重建节点实例

    参数:
    - values: resolve_node_config 的生效值

    返回:
    - str: 序列化后的 SHA256 摘要; 不可序列化的值按 repr 处理
    """
    try:
        raw = json.dumps(values, sort_keys=True, ensure_ascii=False, default=repr)
    except (TypeError, ValueError):
        raw = repr(sorted(values.items()))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class _NodeSlot:
    """一个节点 id 的当前实例与退役实例"""

    current: Any = None
    """当前生效的可调用对象 (实例或函数)"""
    refs: int = 0
    """当前实例的进行中调用数"""
    fingerprint: str = ""
    """当前实例对应的生效配置指纹"""
    impl_id: int = 0
    """当前实现的身份标识, 插件重载后 id() 变化即视为实现变化"""
    retired: dict[int, tuple[Any, int]] = field(default_factory=dict)
    """{id(实例): (实例, 进行中引用数)}; 归零时 close"""


class FlowNodeInstancePool:
    """按节点 id 管理插件节点实例的创建, 替换与关闭"""

    def __init__(self) -> None:
        """初始化空池"""
        self._slots: dict[str, _NodeSlot] = {}
        self._failed: dict[str, str] = {}
        """{节点 id: setup 失败原因}; 失败节点不可用, 下次配置变化重试"""

    # ---------- 实例准备 ----------

    async def ensure(
        self,
        node_id: str,
        impl: Any,
        fingerprint: str,
        ctx: Any = None,
    ) -> Any:
        """
        拿到节点当前应使用的可调用对象, 实现或生效配置变化时重建

        参数:
        - node_id: 节点实例 id
        - impl: 插件实现 (函数或 FlowNode 子类)
        - fingerprint: 生效配置指纹 (config_fingerprint)
        - ctx: 传给 setup(ctx) 的上下文

        返回:
        - 可调用对象; 函数写法返回函数本身, 类写法返回已 setup 的实例;
          setup 失败返回 None (节点不可用, 由调用方按失败处理)
        """
        slot = self._slots.setdefault(node_id, _NodeSlot())
        if (slot.current is not None and slot.impl_id == id(impl)
                and slot.fingerprint == fingerprint and node_id not in self._failed):
            return slot.current
        instance: Any = impl
        if inspect.isclass(impl) and issubclass(impl, FlowNode):
            instance = impl()
            try:
                result = instance.setup(ctx)
                if inspect.isawaitable(result):
                    await result
            except Exception as error:
                self._failed[node_id] = str(error)
                logger.warning(f"[流程节点] {node_id} setup 失败, 节点不可用: {error}")
                return None
        self._failed.pop(node_id, None)
        old = slot.current
        if isinstance(old, FlowNode):
            # 旧实例带着它的进行中引用退役, 引用归零才 close
            slot.retired[id(old)] = (old, slot.refs)
        slot.refs = 0
        slot.current = instance
        slot.fingerprint = fingerprint
        slot.impl_id = id(impl)
        # 无进行中引用的退役实例立即 close
        for key, (retired, refs) in list(slot.retired.items()):
            if refs <= 0:
                del slot.retired[key]
                await self._close_quietly(node_id, retired)
        return instance

    # ---------- 调用方引用 ----------

    def acquire(self, node_id: str) -> Any:
        """
        取当前实例并加引用计数; 调用方完成后必须 release_ref

        参数:
        - node_id: 节点实例 id

        返回:
        - 可调用对象; 没有可用实例返回 None
        """
        slot = self._slots.get(node_id)
        if slot is None or slot.current is None or node_id in self._failed:
            return None
        slot.refs += 1
        return slot.current

    async def release_ref(self, node_id: str, instance: Any) -> None:
        """
        归还 acquire 的引用; 退役实例引用归零时 close

        参数:
        - node_id: 节点实例 id
        - instance: acquire 返回的对象
        """
        slot = self._slots.get(node_id)
        if slot is None:
            return
        if instance is slot.current:
            slot.refs = max(0, slot.refs - 1)
            return
        entry = slot.retired.pop(id(instance), None)
        if entry is None:
            return
        old, refs = entry
        if refs <= 1:
            await self._close_quietly(node_id, old)
        else:
            slot.retired[id(old)] = (old, refs - 1)

    async def call(self, node_id: str, ctx: Any, *args: Any) -> Any:
        """
        经池子调用节点: 取当前实例, 调用后归还引用

        参数:
        - node_id: 节点实例 id
        - ctx: 节点函数的第一个参数
        - args: 其余实参

        返回:
        - 节点返回值; 无可用实例抛 RuntimeError
        """
        instance = self.acquire(node_id)
        if instance is None:
            raise RuntimeError(f"节点 {node_id} 没有可用实现 (setup 失败或未加载)")
        try:
            result = instance(ctx, *args)
            if inspect.isawaitable(result):
                return await result
            return result
        finally:
            await self.release_ref(node_id, instance)

    # ---------- 关闭 ----------

    async def _close_quietly(self, node_id: str, instance: Any) -> None:
        """close 失败只写日志"""
        try:
            result = instance.close()
            if inspect.isawaitable(result):
                await result
        except Exception as error:
            logger.warning(f"[流程节点] {node_id} close 失败: {error}")

    async def close_all(self) -> None:
        """会话释放: 关掉全部 FlowNode 实例 (当前与退役), close 失败只写日志"""
        for node_id, slot in self._slots.items():
            pending: list[Any] = []
            if isinstance(slot.current, FlowNode):
                pending.append(slot.current)
            pending.extend(inst for inst, _ in slot.retired.values())
            for instance in pending:
                await self._close_quietly(node_id, instance)
        self._slots.clear()
        self._failed.clear()

    def status(self) -> dict[str, dict[str, Any]]:
        """运行状态接口用: 每个节点是否有可用实例与 setup 失败原因"""
        return {
            node_id: {
                "ready": slot.current is not None and node_id not in self._failed,
                "error": self._failed.get(node_id, ""),
            }
            for node_id, slot in self._slots.items()
        }
