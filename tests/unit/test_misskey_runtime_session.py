import importlib.util
from unittest.mock import AsyncMock
from pathlib import Path
import pytest
from typing import Any
from types import ModuleType, SimpleNamespace


def _load_misskey_session_module() -> ModuleType:
    """加载仓库中的 Misskey 会话实现"""
    module_path = Path(__file__).parents[1] / "manual" / "misskey_session.py"
    spec = importlib.util.spec_from_file_location("satrap_test_misskey_session", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 Misskey 会话模块: {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_misskey_session_builds_one_isolated_workflow(monkeypatch: pytest.MonkeyPatch):
    module = _load_misskey_session_module()
    create_calls: list[dict[str, Any]] = []

    class FakeWorkflow:
        """提供工作流上下文的最小测试替身"""

        def __init__(self):
            self.ctx = SimpleNamespace()

        async def forward(self, message: str) -> str:
            return message

    async def fake_create(**kwargs: Any) -> FakeWorkflow:
        create_calls.append(kwargs)
        return FakeWorkflow()

    monkeypatch.setattr(module.MainWF, "create", staticmethod(fake_create))
    session = module.MisskeySession("misskey:test:user:session", llm=object())
    session.session_ctx.initialize = AsyncMock()

    await session.run("/help")
    await session.run("/help")

    workflow_id = "misskey:test:user:session_main"
    assert len(create_calls) == 1
    assert create_calls[0]["context_id"] == workflow_id
    assert session.wf_list == [workflow_id]
    assert list(session._workflow_contexts) == [workflow_id]
    assert session.session_ctx.initialize.await_count == 1
