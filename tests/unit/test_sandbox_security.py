"""代码沙箱路径边界与执行授权测试"""
from __future__ import annotations

from pathlib import Path
import pytest
import sys

from satrap.expend.tools.sandbox_tools import AsyncCodeSandboxTool, CodeSandboxTool
from satrap.core.utils.sandbox import CodeSandbox


@pytest.fixture
def sandbox(tmp_path: Path) -> CodeSandbox:
    """
    创建隔离的代码沙箱

    参数:
    - tmp_path: 临时目录

    返回:
    - 使用当前 Python 解释器的代码沙箱
    """
    return CodeSandbox(str(tmp_path / "sandbox"), sys.executable)


@pytest.mark.parametrize("path", ["../outside.py", "nested/../../outside.py"])
def test_path_traversal_is_rejected_for_all_file_operations(
    sandbox: CodeSandbox,
    tmp_path: Path,
    path: str,
) -> None:
    """
    保存, 读取, 执行和删除均在越界时直接拒绝

    参数:
    - sandbox: 隔离代码沙箱
    - tmp_path: 临时目录
    - path: 参数化目录穿越路径
    """
    with pytest.raises(ValueError, match="越出沙箱范围"):
        sandbox.save_to_file("print('x')", path)
    assert sandbox.read_file(path)["returncode"] == -12
    with pytest.raises(ValueError, match="越出沙箱范围"):
        sandbox.run_file(path)
    with pytest.raises(ValueError, match="越出沙箱范围"):
        sandbox.delete_file(path)
    with pytest.raises(ValueError, match="越出沙箱范围"):
        sandbox.delete_directory(path)
    assert not (tmp_path / "outside.py").exists()


def test_absolute_outside_path_is_rejected(sandbox: CodeSandbox, tmp_path: Path) -> None:
    """
    绝对路径不能绕过沙箱根目录

    参数:
    - sandbox: 隔离代码沙箱
    - tmp_path: 临时目录
    """
    outside = tmp_path / "outside.py"
    with pytest.raises(ValueError, match="越出沙箱范围"):
        sandbox.save_to_file("print('x')", str(outside))
    with pytest.raises(ValueError, match="越出沙箱范围"):
        sandbox.list_files(str(tmp_path))


@pytest.mark.parametrize("path", ["", "."])
def test_delete_sandbox_root_is_rejected(sandbox: CodeSandbox, path: str) -> None:
    """
    删除目录操作不得删除沙箱根目录

    参数:
    - sandbox: 隔离代码沙箱
    - path: 参数化沙箱根目录表示
    """
    marker = Path(sandbox.sandbox_path) / "marker.txt"
    sandbox.save_to_file("keep", "marker.txt")
    with pytest.raises(PermissionError, match="沙箱根目录"):
        sandbox.delete_directory(path)
    assert marker.read_text(encoding="utf-8") == "keep"


def test_internal_file_lifecycle(sandbox: CodeSandbox) -> None:
    """
    合法的沙箱内文件操作保持可用

    参数:
    - sandbox: 隔离代码沙箱
    """
    sandbox.save_to_file("print('ok')", "nested/main.py")
    assert sandbox.read_file("nested/main.py")["content"] == "print('ok')"
    assert sandbox.list_files("nested") == [str(Path("nested") / "main.py")]
    sandbox.delete_file("nested/main.py")
    sandbox.delete_directory("nested")
    assert not Path(sandbox.sandbox_path, "nested").exists()


def test_code_execution_timeout(tmp_path: Path) -> None:
    """
    死循环代码在配置的截止时间后返回超时错误

    参数:
    - tmp_path: 临时目录
    """
    limited = CodeSandbox(str(tmp_path / "sandbox"), sys.executable, execution_timeout=0.1)
    result = limited.run("while True: pass")
    assert result["returncode"] == -4
    assert "超时" in result["stderr"]


def test_code_execution_requires_explicit_authorization(sandbox: CodeSandbox) -> None:
    """
    同步代码执行默认拒绝, 仅明确授权后运行

    参数:
    - sandbox: 隔离代码沙箱
    """
    blocked = CodeSandboxTool(sandbox)
    assert "需要用户明确批准" in blocked.execute("run", code="print('blocked')")["error"]

    prompts: list[str] = []
    allowed = CodeSandboxTool(sandbox, lambda description: prompts.append(description) or True)
    result = allowed.execute("run", code="print('allowed')")
    assert result["returncode"] == 0
    assert str(result["stdout"]).strip() == "allowed"
    assert "print('allowed')" in prompts[0]


@pytest.mark.asyncio
async def test_async_code_execution_requires_explicit_authorization(sandbox: CodeSandbox) -> None:
    """
    异步代码执行默认拒绝, 并支持异步授权回调

    参数:
    - sandbox: 隔离代码沙箱
    """
    blocked = AsyncCodeSandboxTool(sandbox)
    assert "需要用户明确批准" in (await blocked.execute("run", code="print('blocked')"))["error"]

    async def authorize(description: str) -> bool:
        """
        仅批准包含 allowed 的执行说明

        参数:
        - description: 执行说明

        返回:
        - 说明包含 allowed 时返回 True
        """
        return "allowed" in description

    allowed = AsyncCodeSandboxTool(sandbox, authorize)
    result: dict[str, object] = await allowed.execute("run", code="print('allowed')")
    assert result["returncode"] == 0
    assert str(result["stdout"]).strip() == "allowed"
