"""集中类型包的公共身份、序列化与导入边界"""
from __future__ import annotations

import importlib
from pathlib import Path
import pickle
import subprocess
import sys
import typing

import satrap.core.type as contracts


def test_public_facades_share_contract_identities() -> None:
    """旧公共 facade 与集中叶模块必须引用同一个对象"""
    checks = [
        ("satrap.core.components", "BaseMessageComponent", contracts.BaseMessageComponent),
        ("satrap.core.conversation", "ConversationRoute", contracts.ConversationRoute),
        ("satrap.core.call_context", "CallOrigin", contracts.CallOrigin),
        ("satrap.core.config.platform_messages", "MessageScope", contracts.MessageScope),
        ("satrap.core.APICall.ImageCall", "ImageGenParams", contracts.ImageGenParams),
        ("satrap.core.approval", "ApprovalRequest", contracts.ApprovalRequest),
        ("satrap.core.plugin_interop", "ImageArtifact", contracts.ImageArtifact),
        ("satrap.core.plugin_authorization", "AuthorizationDecision", contracts.AuthorizationDecision),
        ("satrap.core.plugin_authorization", "PluginEntryBinding", contracts.PluginEntryBinding),
        ("satrap.edictum", "PluginEnvironment", contracts.PluginEnvironment),
        ("satrap.edictum.flow", "FlowMessage", contracts.FlowMessage),
        ("satrap.core.framework.providers", "SessionProvider", contracts.SessionProvider),
    ]
    for module, name, expected in checks:
        assert getattr(importlib.import_module(module), name) is expected
    assert len(contracts.__all__) == len(set(contracts.__all__))


def test_current_contract_pickle_and_runtime_annotations() -> None:
    """当前版本可信 pickle 与运行时类型解析保持稳定"""
    values = [
        contracts.LLMConfig(),
        contracts.ConversationRoute("user", "platform", "private"),
        contracts.PluginSpec("demo"),
        contracts.FlowMessage("m", 1, "u", "n", "text", False, False, 1.0),
    ]
    for value in values:
        assert type(pickle.loads(pickle.dumps(value))) is type(value)
    assert typing.get_type_hints(contracts.FlowDefinition)["nodes"] == dict[str, contracts.FlowNodeInstance]
    llm_hints = typing.get_type_hints(contracts.LLMConfig)
    assert llm_hints["thinking_fields"] == typing.Optional[typing.List[str]]
    assert llm_hints["thinking_levels"] == typing.Optional[typing.List[str]]
    from satrap.core.APICall.LLMCall import AsyncLLM, LLM
    from satrap.core.framework.Base import AsyncSession, Session

    hints = typing.get_type_hints(
        contracts.SessionProvider.create_session,
        globalns={
            "SessionConfig": contracts.SessionConfig,
            "LLM": LLM,
            "AsyncLLM": AsyncLLM,
            "Session": Session,
            "AsyncSession": AsyncSession,
        },
    )
    assert hints["session_config"] is contracts.SessionConfig
    assert hints["llm"] == LLM | AsyncLLM | None
    assert hints["return"] == Session | AsyncSession


def test_clean_process_import_orders() -> None:
    """聚合入口和叶模块首导入均不能依赖预先加载其他模块"""
    root = Path(__file__).resolve().parents[2]
    modules = [
        "satrap.core.type",
        "satrap.core.type.flow.registry",
        "satrap.core.type.plugin.catalog",
        "satrap.core.type.model_protocol",
        "satrap.core.type.session_provider",
        "satrap",
    ]
    for module in modules:
        code = f"import sys; sys.path.insert(0, {str(root)!r}); import {module}"
        result = subprocess.run(
            [sys.executable, "-I", "-c", code],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        assert result.returncode == 0, (module, result.stdout, result.stderr)
