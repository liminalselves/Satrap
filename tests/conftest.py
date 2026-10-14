from pathlib import Path
import pytest
import sys
import os
import tempfile
import gc


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture
def caplog(caplog: pytest.LogCaptureFixture):
    """直接捕获隔离后的项目控制台 logger, 不依赖生产代码向根 logger 传播"""
    from satrap.core.log import logger

    logger.stdout_logger.addHandler(caplog.handler)
    try:
        yield caplog
    finally:
        logger.stdout_logger.removeHandler(caplog.handler)


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
    config._satrap_original_paths = {key: os.environ.get(key) for key in ("SATRAP_DATA_ROOT", "SATRAP_LOG_ROOT", "SATRAP_LOG_CONFIG", "SATRAP_CONFIG_ROOT", "SATRAP_CREDENTIALS_ROOT", "SATRAP_RUNTIME_ROOT")}
    config._satrap_test_data = tempfile.TemporaryDirectory(prefix="satrap-tests-")
    os.environ["SATRAP_DATA_ROOT"] = config._satrap_test_data.name
    os.environ["SATRAP_LOG_ROOT"] = str(Path(config._satrap_test_data.name) / "logs")
    os.environ["SATRAP_LOG_CONFIG"] = str(Path(config._satrap_test_data.name) / "logging.json")
    os.environ["SATRAP_CONFIG_ROOT"] = str(Path(config._satrap_test_data.name) / "config")
    os.environ["SATRAP_CREDENTIALS_ROOT"] = str(Path(config._satrap_test_data.name) / "credentials")
    os.environ["SATRAP_RUNTIME_ROOT"] = str(Path(config._satrap_test_data.name) / "runtime")


def pytest_unconfigure(config):
    """恢复环境并清理隔离测试目录"""
    gc.collect()   # 删除测试数据库前回收循环引用, 触发上下文连接的析构关闭
    if "satrap.core.log" in sys.modules:
        sys.modules["satrap.core.log"].logger.close()
    for key, original in getattr(config, "_satrap_original_paths", {}).items():
        if original is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = original
    temporary = getattr(config, "_satrap_test_data", None)
    if temporary is not None:
        temporary.cleanup()
