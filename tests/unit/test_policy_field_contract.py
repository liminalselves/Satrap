"""策略字段契约: 派生集合冻结对比, 生成物逐字节一致, 共享样例与默认值单一来源

- 冻结迁移前的 hot_keys / GROUP_KEYS / AUTOMATIC_KEYS / POLICY_DEFAULTS, 与契约派生结果逐项对比
- 契约 JSON 由 scripts/sync_wake_policy_contract.py 生成, 库内文件必须与序列化结果逐字节一致
- tests/fixtures/wake_policy_cases.json 的样例在前后端各跑一遍, 两侧结论必须一致
- 独立导入烟测覆盖 platform_policy 与 wake_overrides 任一先导入的进程
- 运行时默认值只来自契约: 改写 POLICY_DEFAULTS 后消费方行为必须随之改变 (字面量已清除)
"""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any
import json
import subprocess
import sys

import pytest

from unittest.mock import AsyncMock

from satrap.core.config.platform_policy import (
    POLICY_DEFAULTS,
    POLICY_FIELD_CONTRACT,
    fields_with_scope,
    hot_reload_keys,
    policy_default,
    validate_wake_policy,
)
from satrap.core.config.wake_overrides import AUTOMATIC_KEYS, GROUP_KEYS
from satrap.core.pipeline.input_projection import project_input
from satrap.core.pipeline.wake_dry_run import dry_run_wake
from satrap.core.pipeline.wake_window import PendingMessage, WakeWindow
from satrap.core.platform import PlatformConfig
from satrap.core.platform.event import MessageChain, MessageEvent
from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.components import Plain

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "wake_policy_cases.json"
GENERATED = ROOT / "satrap-ui" / "src" / "generated" / "wake-policy-contract.json"

FROZEN_HOT_KEYS = frozenset({
    "wake_max_wait", "wake_group_overrides", "wake_time_rules", "wake_score_threshold",
    "wake_question_weight", "wake_address_weight", "wake_backlog_weight", "wake_reply_penalty",
    "wake_mode", "wake_message_threshold", "wake_talk_value", "wake_cooldown", "wake_aliases",
    "wake_words", "group_whitelist", "context_scope", "enable_group", "enable_private",
    "input_text_limit", "input_media_limit", "message_text_limit", "asr_model", "voice_transcribe",
    "attachment_extract", "media_trusted_hosts", "media_insecure_tls", "media_plaintext_http",
    # 命令入口的 operator 名单属平台级热更新字段: 登记后无需重启即可生效
    "command_operators",
})
"""迁移前的 BackendManager 手写 hot_keys"""

FROZEN_AUTOMATIC_KEYS = frozenset({
    "wake_mode", "wake_message_threshold", "wake_cooldown", "wake_score_threshold", "wake_max_wait",
    "wake_question_weight", "wake_address_weight", "wake_backlog_weight", "wake_reply_penalty",
    "wake_talk_value",
})
"""迁移前的 wake_overrides.AUTOMATIC_KEYS"""

FROZEN_GROUP_KEYS = FROZEN_AUTOMATIC_KEYS | {
    "wake_words", "wake_aliases", "reply_with_quote", "reply_with_mention", "quote_lookup",
    "wake_on_quote_self", "forward_lookup",
}
"""迁移前的 wake_overrides.GROUP_KEYS"""

FROZEN_DEFAULTS: dict[str, object] = {
    "message_text_limit": 2000, "input_text_limit": 20000, "input_media_limit": 8,
    "wake_mode": "explicit", "wake_message_threshold": 3, "wake_cooldown": 30,
    "wake_max_wait": 0, "wake_score_threshold": 0.65, "wake_question_weight": 0.55,
    "wake_address_weight": 0.15, "wake_backlog_weight": 0.30, "wake_reply_penalty": 0.40,
}
"""迁移前的 platform_policy.POLICY_DEFAULTS"""


def _cases() -> list[dict[str, Any]]:
    """共享样例列表"""
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return list(payload["cases"])


def _validate_case(case: Mapping[str, Any]) -> None:
    """按上下文把样例包成真实校验入口接受的结构"""
    context = str(case["context"])
    # 样例取值来自 JSON, 显式标注避免 Any 扩散到校验入口
    value: dict[str, object] = case["value"]
    if context == "platform":
        validate_wake_policy(value)
    elif context == "group":
        validate_wake_policy({"wake_group_overrides": {"123": value}})
    elif context == "time":
        rule: dict[str, object] = {"start": "00:00", "end": "06:00", "settings": value}
        validate_wake_policy({"wake_time_rules": [rule]})
    else:
        raise AssertionError(f"未知上下文: {context}")


async def _event(adapter: OneBotAdapter, text: str = "正文", message_id: str = "1") -> MessageEvent:
    """经真实 OneBot 入站构造事件"""
    await adapter._handle_group_message({
        "self_id": 10, "group_id": 20, "user_id": 30, "message_id": message_id,
        "message_type": "group", "message": [{"type": "text", "data": {"text": text}}],
    })
    return adapter._event_queue.get_nowait()


class _CountingAdapter(OneBotAdapter):
    """不发网络请求、只统计发送次数的适配器替身"""

    def __init__(self, settings: dict[str, object]) -> None:
        super().__init__(PlatformConfig(id="bot", type="onebot", settings=settings))
        # 发送路径要求客户端已初始化, 用替身替代真实连接 (由 _dispatch_action 统计次数)
        self._bot = AsyncMock()
        self.sent: list[str] = []

    async def _dispatch_action(self, session_id: str, private_action: str, group_action: str, **params: Any) -> Any:
        self.sent.append(group_action)
        return {"message_id": len(self.sent)}


class TestFrozenSets:
    def test_hot_keys_match_frozen(self):
        assert hot_reload_keys() == FROZEN_HOT_KEYS | {"message_archive_retention_days"}

    def test_override_scope_keys_match_frozen(self):
        assert AUTOMATIC_KEYS == FROZEN_AUTOMATIC_KEYS
        assert GROUP_KEYS == FROZEN_GROUP_KEYS

    def test_policy_defaults_match_frozen(self):
        assert POLICY_DEFAULTS == {**FROZEN_DEFAULTS, "message_archive_retention_days": 30}

    def test_contract_covers_union_of_known_keys(self):
        # 表覆盖校验字段, 覆盖范围, 默认值, 热更新与前端编辑字段的并集, 不局限于热更新键
        assert FROZEN_HOT_KEYS | FROZEN_GROUP_KEYS | set(FROZEN_DEFAULTS) <= set(POLICY_FIELD_CONTRACT)
        # 不在旧 hot_keys 内的字段也在表里, 但保持需要重启
        for key in ("reply_with_quote", "quote_lookup", "notice_types"):
            assert POLICY_FIELD_CONTRACT[key]["hot_reload"] is False
        assert "group_whitelist" in POLICY_FIELD_CONTRACT
        assert "context_scope" in POLICY_FIELD_CONTRACT

    def test_scope_derivations_are_consistent(self):
        assert fields_with_scope("time") == AUTOMATIC_KEYS
        assert fields_with_scope("time", "group") == GROUP_KEYS
        assert fields_with_scope("platform") == {
            key for key, field in POLICY_FIELD_CONTRACT.items() if field["scope"] == "platform"
        }

    def test_contract_entries_declare_consistent_ranges(self):
        kinds = {
            "int", "number", "bool", "enum", "text", "list", "notice_types",
            "words", "group_ids", "scope", "group_map", "time_rules",
        }
        for key, field in POLICY_FIELD_CONTRACT.items():
            assert field["kind"] in kinds, key
            assert field["scope"] in {"platform", "group", "time"}, key
            # 同一字段不同时声明含端点上界与排他上界
            assert not ("max" in field and "max_exclusive" in field), key
            if field["kind"] in {"int", "number"}:
                assert "min" in field or "max" in field or "max_exclusive" in field, key
            if field["kind"] == "enum":
                assert field.get("enum"), key
            if field["kind"] == "int":
                assert field.get("integer") is True, key
            if field["kind"] in {"text", "list"}:
                assert "max_length" in field or "max_items" in field, key


class TestGeneratedContract:
    def test_generated_json_matches_serialization_byte_for_byte(self):
        from scripts.sync_wake_policy_contract import build_payload, render

        assert GENERATED.is_file(), "契约 JSON 缺失, 请运行 python scripts/sync_wake_policy_contract.py"
        assert GENERATED.read_bytes() == render(build_payload())

    def test_generated_json_uses_lf_and_trailing_newline(self):
        raw = GENERATED.read_bytes()
        assert b"\r" not in raw
        assert raw.endswith(b"\n")

    def test_generated_json_excludes_backend_only_message(self):
        payload = json.loads(GENERATED.read_text(encoding="utf-8"))
        assert payload["version"] == 2
        for entry in payload["fields"]:
            assert "message" not in entry

    def test_generated_json_carries_command_operators(self):
        # 命令入口的操作员名单必须进入生成契约, 前端编辑器与表单字段由它构建
        payload = json.loads(GENERATED.read_text(encoding="utf-8"))
        entry = next(field for field in payload["fields"] if field["key"] == "command_operators")
        assert entry == {
            "key": "command_operators", "kind": "list", "scope": "platform",
            "hot_reload": True, "display_in_preview": False, "max_items": 32, "max_length": 64,
        }


class TestSharedCases:
    @pytest.mark.parametrize("case", _cases(), ids=lambda case: str(case["id"]))
    def test_fixture_case_conclusion(self, case: dict[str, Any]):
        if case["valid"]:
            _validate_case(case)
            return
        with pytest.raises(ValueError):
            _validate_case(case)

    def test_fixture_uses_json_values_only(self):
        raw = FIXTURE.read_text(encoding="utf-8")
        assert "NaN" not in raw and "Infinity" not in raw

    def test_text_length_counts_code_points(self):
        # 与前端 textLength 同一口径: 非 BMP 字符按 1 个码点计, 不按 UTF-16 码元
        assert len("😀" * 128) == 128
        validate_wake_policy({"asr_model": "😀" * 128})
        with pytest.raises(ValueError):
            validate_wake_policy({"asr_model": "😀" * 129})
        validate_wake_policy({"media_trusted_hosts": ["例" * 126 + "😀" * 127]})
        with pytest.raises(ValueError):
            validate_wake_policy({"media_trusted_hosts": ["例" * 126 + "😀" * 128]})

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_numbers_rejected(self, value: float):
        # NaN/Infinity 无法写进共享 JSON, 由两侧各自补测
        for key in ("wake_cooldown", "wake_score_threshold", "wake_max_wait", "wake_talk_value"):
            with pytest.raises(ValueError):
                validate_wake_policy({key: value})

    def test_legacy_cooldown_over_one_day_still_accepted(self):
        # 存量配置不受影响: 冷却不新增上限, 校验与适配器构造都按同一契约
        settings: dict[str, object] = {"wake_cooldown": 90000.0}
        validate_wake_policy(settings)
        adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings=dict(settings)))
        assert adapter.config.settings["wake_cooldown"] == 90000.0

    def test_missing_override_field_does_not_gain_default(self):
        # 缺失仍表示继承: 校验不向覆盖对象注入默认值
        override: dict[str, object] = {"wake_mode": "frequency"}
        validate_wake_policy({"wake_group_overrides": {"123": override}})
        assert override == {"wake_mode": "frequency"}


class TestImportOrder:
    @pytest.mark.parametrize(
        "module", ["satrap.core.config.platform_policy", "satrap.core.config.wake_overrides"],
    )
    def test_each_module_imports_first(self, module: str):
        result = subprocess.run(
            [sys.executable, "-c", f"import {module} as m; print(m.__name__)"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=120,
        )
        assert result.returncode == 0, result.stderr
        assert module in result.stdout


class TestPolicyDefaultSingleSource:
    """运行时默认值只能来自契约: 改写 POLICY_DEFAULTS 后消费方行为随之改变"""

    @pytest.mark.asyncio
    async def test_wake_window_cooldown_default(self, monkeypatch: pytest.MonkeyPatch):
        adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings={"wake_mode": "frequency"}))
        window = WakeWindow()
        event = await _event(adapter)
        snapshot = window.observe(event, 0)
        window.claim(event, snapshot, False, 0)
        monkeypatch.setitem(POLICY_DEFAULTS, "wake_cooldown", 600)
        assert window.decide(event, snapshot, 1).rule == "cooldown"
        assert window.cooldown_remaining(event, 1) > 0
        monkeypatch.setitem(POLICY_DEFAULTS, "wake_cooldown", 0)
        assert window.cooldown_remaining(event, 1) == 0
        # 冷却不再拦截: 回到阈值判断 (默认阈值 3, 一条正文不触发)
        assert window.decide(event, snapshot, 1).rule == "frequency"

    @pytest.mark.asyncio
    async def test_wake_window_score_threshold_default(self, monkeypatch: pytest.MonkeyPatch):
        adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings={"wake_mode": "necessity"}))
        window = WakeWindow()
        event = await _event(adapter)
        snapshot = window.observe(event, 0)
        monkeypatch.setitem(POLICY_DEFAULTS, "wake_score_threshold", 0.0)
        assert window.decide(event, snapshot, 1).triggered is True
        monkeypatch.setitem(POLICY_DEFAULTS, "wake_score_threshold", 1.0)
        assert window.decide(event, snapshot, 1).triggered is False

    @pytest.mark.asyncio
    async def test_wake_window_max_wait_default(self, monkeypatch: pytest.MonkeyPatch):
        adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings={"wake_mode": "frequency"}))
        window = WakeWindow()
        event = await _event(adapter)
        snapshot: tuple[PendingMessage, ...] = (
            PendingMessage(request_id="r1", actor_id="30", message_id="1", text="正文", received_at=0.0),
        )
        monkeypatch.setitem(POLICY_DEFAULTS, "wake_max_wait", 0)
        assert window.decide(event, snapshot, 10, deadline=True).rule != "max_wait"
        monkeypatch.setitem(POLICY_DEFAULTS, "wake_max_wait", 5)
        assert window.decide(event, snapshot, 10, deadline=True).rule == "max_wait"

    def test_message_threshold_constant_follows_contract(self):
        from satrap.core.pipeline.wake_policy import DEFAULT_MESSAGE_THRESHOLD

        assert DEFAULT_MESSAGE_THRESHOLD == policy_default("wake_message_threshold")

    @pytest.mark.asyncio
    async def test_adapter_text_limit_default(self, monkeypatch: pytest.MonkeyPatch):
        adapter = _CountingAdapter({"self_id": "10"})
        monkeypatch.setitem(POLICY_DEFAULTS, "message_text_limit", 8)
        await adapter._send_message("group%20", MessageChain([Plain(text="x" * 32)]))
        split_calls = len(adapter.sent)
        adapter.sent.clear()
        monkeypatch.setitem(POLICY_DEFAULTS, "message_text_limit", 4096)
        await adapter._send_message("group%20", MessageChain([Plain(text="x" * 32)]))
        assert split_calls > 1
        assert len(adapter.sent) == 1

    @pytest.mark.asyncio
    async def test_input_projection_text_limit_default(self, monkeypatch: pytest.MonkeyPatch):
        adapter = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings={}))
        event = await _event(adapter, "x" * 200)
        monkeypatch.setitem(POLICY_DEFAULTS, "input_text_limit", 10)
        assert len(project_input(event, "none").message) <= 10
        monkeypatch.setitem(POLICY_DEFAULTS, "input_text_limit", 10000)
        assert project_input(event, "none").message == "[用户 30, 消息 1] " + "x" * 200

    @pytest.mark.asyncio
    async def test_wake_mode_default_in_dry_run(self, monkeypatch: pytest.MonkeyPatch):
        payload: dict[str, object] = {"settings": {"self_id": "10"}, "group_id": "20", "steps": [{"text": "正文"}]}
        monkeypatch.setitem(POLICY_DEFAULTS, "wake_mode", "frequency")
        result = await dry_run_wake(payload)
        assert result["automatic"]["mode"] == "frequency"
