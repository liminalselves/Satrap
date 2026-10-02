from pathlib import Path
import pytest
import sys
import os
import tempfile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def pytest_addoption(parser: pytest.Parser):
    parser.addoption(
        "--run-integration",
        action="store_true",
        default=False,
        help="运行需要外部服务或真实模型的集成测试",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]):
    if config.getoption("--run-integration"):
        return

    skip_integration = pytest.mark.skip(
        reason="集成测试默认跳过, 使用 --run-integration 显式运行",
    )
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_integration)


def pytest_configure(config):
    """测试收集前隔离默认存储目录, 子进程继承相同环境"""
    config._satrap_original_data_root = os.environ.get("SATRAP_DATA_ROOT")
    config._satrap_test_data = tempfile.TemporaryDirectory(prefix="satrap-tests-")
    os.environ["SATRAP_DATA_ROOT"] = config._satrap_test_data.name


def pytest_unconfigure(config):
    """恢复环境并清理隔离测试目录"""
    original = getattr(config, "_satrap_original_data_root", None)
    if original is None:
        os.environ.pop("SATRAP_DATA_ROOT", None)
    else:
        os.environ["SATRAP_DATA_ROOT"] = original
    temporary = getattr(config, "_satrap_test_data", None)
    if temporary is not None:
        temporary.cleanup()
