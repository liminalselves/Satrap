"""插件兼容声明与环境边界测试"""
import pytest

from satrap.edictum.plugin_compatibility import (
    PluginEnvironment,
    check_plugin_compatibility,
)


@pytest.mark.parametrize("session_type,allowed", [("chat", False), ("embedded", False), ("platform", True)])
def test_platform_only(session_type, allowed):
    environment = PluginEnvironment(session_type, "onebot" if session_type == "platform" else None)
    result = check_plugin_compatibility(
        {"applicability": {"session_types": ["platform"], "platforms": "*"}},
        environment,
        "0.1.0",
    )
    assert result.allowed is allowed


def test_version_and_platform_constraints():
    meta = {"compatibility": {"satrap": ">=0.1,<0.2"}, "applicability": {"session_types": ["platform"], "platforms": ["onebot"]}}
    assert check_plugin_compatibility(meta, PluginEnvironment("platform", "onebot"), "0.1.0").allowed
    assert check_plugin_compatibility(meta, PluginEnvironment("platform", "misskey"), "0.1.0").reason_code == "platform_not_supported"
    assert check_plugin_compatibility(meta, PluginEnvironment("platform", "onebot"), "0.2.0").reason_code == "framework_version_mismatch"


@pytest.mark.parametrize("meta", [
    {"compatibility": None},
    {"compatibility": {"satrap": "wrong"}},
    {"applicability": {"session_types": []}},
    {"applicability": {"session_types": ["typo"]}},
    {"applicability": {"platforms": ["*"]}},
    {"applicability": {"hosts": ["chat"]}},
])
def test_invalid_manifest_is_not_unrestricted(meta):
    assert check_plugin_compatibility(meta, PluginEnvironment(), "0.1.0").reason_code == "invalid_manifest"


def test_legacy_manifest_remains_available():
    result = check_plugin_compatibility({}, PluginEnvironment(), "0.1.0")
    assert result.allowed
    assert result.warnings


@pytest.mark.parametrize("session_type,platform", [("platform", None), ("chat", "onebot"), ("typo", None)])
def test_environment_validation(session_type, platform):
    with pytest.raises(ValueError):
        PluginEnvironment(session_type, platform)


@pytest.mark.asyncio
@pytest.mark.parametrize("async_mode", [False, True])
async def test_incompatible_plugin_never_imported(tmp_path, async_mode):
    from satrap import LLM, AsyncLLM, SimpleSession, AsyncSimpleSession
    from satrap.edictum.plugin_compatibility import PluginCompatibilityError

    plugin = tmp_path / "platform_plugin"
    plugin.mkdir()
    (plugin / "meta.yaml").write_text(
        "name: platform_plugin\napplicability:\n  session_types: [platform]\n",
        encoding="utf-8",
    )
    (plugin / "tools.py").write_text("raise AssertionError('plugin imported')\n", encoding="utf-8")
    llm = (AsyncLLM if async_mode else LLM)(api_key="test", model="test")
    session = (AsyncSimpleSession if async_mode else SimpleSession)(
        "compatibility-test", llm, db_path=str(tmp_path / "chat.db"),
        plugin_environment=PluginEnvironment("chat"),
    )
    try:
        with pytest.raises(PluginCompatibilityError):
            if async_mode:
                await session.install_plugin(str(plugin))
            else:
                session.install_plugin(str(plugin))
        if async_mode:
            assert session._wf is None
        assert not session.list_plugins()
    finally:
        if async_mode:
            await llm.client.close()
        else:
            llm.client.close()
