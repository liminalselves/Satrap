"""底层组件可选的调试接入, 不读取配置或导入采集服务"""

from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from typing import Protocol


class Recorder(Protocol):
    """与公共诊断字段一致的可选接收器"""

    def __call__(self, category: str, phase: str, *, status: str = "", metadata: Mapping[str, object] | None = None,
                 bodies: Mapping[str, object] | None = None, span_id: str = "", parent_span_id: str = "") -> None:
        """
        接收新增事件

        参数:
        - category: 事件类别
        - phase: 执行阶段
        - status: 实际状态
        - metadata: 元数据
        - bodies: 正文槽位
        - span_id: 步骤身份
        - parent_span_id: 父步骤身份
        """
        ...


class SpanFactory(Protocol):
    """由宿主关联步骤生命周期"""

    def __call__(self, category: str, *, metadata: Mapping[str, object] | None = None,
                 bodies: Mapping[str, object] | None = None, span_id: str = "") -> AbstractContextManager[str]:
        """
        构造一个诊断步骤

        参数:
        - category: 事件类别
        - metadata: 元数据
        - bodies: 正文槽位
        - span_id: 可选业务步骤身份

        返回:
        - 诊断范围
        """
        ...


_HOOKS: ContextVar[tuple[Recorder, SpanFactory] | None] = ContextVar("debug_hooks", default=None)


@contextmanager
def bind_debug_hooks(recorder: Recorder, spans: SpanFactory) -> Iterator[None]:
    """
    在宿主请求内注入诊断, 不修改库组件实例

    参数:
    - recorder: 事件接收器
    - spans: 步骤范围构造器

    返回:
    - 当前请求范围
    """
    token = _HOOKS.set((recorder, spans))
    try:
        yield
    finally:
        _HOOKS.reset(token)


def record_debug(category: str, phase: str, *, status: str = "", metadata: Mapping[str, object] | None = None,
                 bodies: Mapping[str, object] | None = None, span_id: str = "", parent_span_id: str = "") -> None:
    """
    转交已注入的观察者, 普通库调用直接跳过

    参数:
    - category: 事件类别
    - phase: 执行阶段
    - status: 实际状态
    - metadata: 元数据
    - bodies: 正文槽位
    - span_id: 步骤身份
    - parent_span_id: 父步骤身份
    """
    hooks = _HOOKS.get()
    if hooks is not None:
        hooks[0](category, phase, status=status, metadata=metadata, bodies=bodies, span_id=span_id, parent_span_id=parent_span_id)


@contextmanager
def debug_span(category: str, *, metadata: Mapping[str, object] | None = None,
               bodies: Mapping[str, object] | None = None, span_id: str = "") -> Iterator[str]:
    """
    无宿主时保留原调用和异常语义

    参数:
    - category: 事件类别
    - metadata: 元数据
    - bodies: 正文槽位
    - span_id: 可选步骤身份

    返回:
    - 宿主步骤身份或空字符串
    """
    hooks = _HOOKS.get()
    if hooks is None:
        yield ""
    else:
        with hooks[1](category, metadata=metadata, bodies=bodies, span_id=span_id) as identity:
            yield identity
