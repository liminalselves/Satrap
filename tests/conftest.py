from pathlib import Path
import sys

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def pytest_addoption(parser):
    parser.addoption(
        "--run-integration",
        action="store_true",
        default=False,
        help="运行需要外部服务或真实模型的集成测试",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-integration"):
        return

    skip_integration = pytest.mark.skip(
        reason="集成测试默认跳过, 使用 --run-integration 显式运行",
    )
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_integration)
