"""expend 目录结构测试

验证:
- tools 子包可导入 (agent / mem0 / rag / sandbox_tools / search)
- 顶层 re-export 保持可用
- 旧模块路径 satrap.expend.<mod> 别名兼容
- mcp 预留包可导入
"""
import importlib


def test_tools_subpackage_imports():
    """tools 子包各模块可导入且有核心符号"""
    from satrap.expend.tools import (  # noqa: F401
        AsyncCodeSandboxTool,
        AsyncFetchPageTool,
        AsyncSearchTool,
        CodeSandboxTool,
        DataBaseRAG,
        LiteVectorRAG,
        Mem0Memory,
        SearchTool,
    )
    from satrap.expend.tools.agent import (  # noqa: F401
        AsyncSubAgent,
        SubAgent,
    )
    from satrap.expend.tools.rag import LiteVectorRAG as LVR2
    assert LVR2 is LiteVectorRAG


def test_top_level_reexports():
    """顶层 from satrap.expend import X 保持可用"""
    from satrap.expend import (  # noqa: F401
        AsyncCodeSandboxTool,
        AsyncFetchPageTool,
        AsyncSearchTool,
        CodeSandboxTool,
        DataBaseRAG,
        FetchPageTool,
        LiteVectorRAG,
        Mem0Memory,
        SearchTool,
    )


def test_legacy_module_path_alias():
    """旧路径 satrap.expend.<mod> 经 sys.modules 别名兼容"""
    for mod_name in ("mem0", "rag", "sandbox_tools", "search"):
        legacy = importlib.import_module(f"satrap.expend.{mod_name}")
        new = importlib.import_module(f"satrap.expend.tools.{mod_name}")
        assert legacy is new

    legacy_rag = importlib.import_module("satrap.expend.rag")
    new_rag = importlib.import_module("satrap.expend.tools.rag")
    assert legacy_rag.LiteVectorRAG is new_rag.LiteVectorRAG


def test_mcp_package_placeholder():
    """mcp 预留包可导入且为空实现"""
    mcp_pkg = importlib.import_module("satrap.expend.mcp")
    assert mcp_pkg.__doc__ is not None
