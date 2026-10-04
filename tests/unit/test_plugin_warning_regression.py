from unittest.mock import Mock
from pathlib import Path
import io
import logging
import asyncio

import pytest

from satrap.edictum.plugin_config import parse_config_schema, schema_to_payload
from satrap.core.log import Logger
from satrap.core.log.stream import standard_log_stream


def test_boolean_alias_keeps_switch_boolean_without_repeated_warning(monkeypatch):
    warning = Mock()
    monkeypatch.setattr("satrap.edictum.plugin_config.logger.warning", warning)
    for _ in range(200):
        schema = parse_config_schema({"config_schema": {"summary_enabled": {"type": "boolean", "default": True}}})
        field = schema["summary_enabled"]
        assert field.validate(False) is False and field.validate(True) is True
        assert schema_to_payload(schema)["summary_enabled"]["type"] == "bool"
        with pytest.raises(ValueError):
            field.validate_strict("false")
    warning.assert_not_called()
    parse_config_schema({"config_schema": {"invalid": {"type": "typo"}}})
    warning.assert_called_once()


@pytest.mark.asyncio
async def test_warning_has_one_console_live_and_file_record_with_root_handler(tmp_path: Path, monkeypatch):
    console, root_output = io.StringIO(), io.StringIO()
    monkeypatch.setattr("sys.stderr", console)
    root_handler = logging.StreamHandler(root_output)
    root = logging.getLogger()
    root.addHandler(root_handler)
    logger = Logger("isolated-probe", file_name=str(tmp_path / "probe.log"))
    standard_log_stream.clear()
    subscription = standard_log_stream.subscribe(history_limit=0)
    try:
        logger.warning("重复日志回归标记")
        await asyncio.sleep(0)
        assert console.getvalue().count("重复日志回归标记") == 1
        assert root_output.getvalue() == ""
        assert (tmp_path / "probe.log").read_text(encoding="utf-8").count("重复日志回归标记") == 1
        assert subscription.queue.qsize() == 1
        assert "重复日志回归标记" in subscription.queue.get_nowait().content
    finally:
        standard_log_stream.unsubscribe(subscription.subscription_id)
        logger.close()
        root.removeHandler(root_handler)
