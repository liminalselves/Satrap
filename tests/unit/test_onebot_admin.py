"""OneBot 群管理动作封装的参数校验, 群范围与错误归一"""
from unittest.mock import AsyncMock
import asyncio
import base64

from aiocqhttp.exceptions import ActionFailed
import pytest

from satrap.core.platform.onebot.admin import (
    ADMIN_CAPABILITIES,
    AdminActionRejected,
    AdminActionUnconfirmed,
    OneBotAdmin,
    UnsupportedAdminAction,
)
from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.platform import PlatformAdapter, PlatformConfig


def _adapter(**settings: object) -> OneBotAdapter:
    adapter = OneBotAdapter(PlatformConfig(id="ob", type="onebot", settings=dict(settings)))
    adapter._bot = AsyncMock()
    return adapter


class TestOneBotAdminValidation:
    @pytest.mark.asyncio
    async def test_group_and_user_id_must_be_decimal(self):
        admin = _adapter().admin
        with pytest.raises(ValueError, match="群 ID"):
            await admin.get_group_info("abc")
        with pytest.raises(ValueError, match="用户 ID"):
            await admin.get_group_member_info("456", "12a")
        with pytest.raises(ValueError, match="群 ID"):
            await admin.kick_group_member("", "123")

    @pytest.mark.asyncio
    async def test_group_whitelist_is_enforced_before_action(self):
        adapter = _adapter(group_whitelist=["789"])
        with pytest.raises(AdminActionRejected, match="允许范围"):
            await adapter.admin.kick_group_member("456", "123")
        adapter._bot.set_group_kick.assert_not_called()

    @pytest.mark.asyncio
    async def test_ban_duration_bounds_and_bool_rejected(self):
        admin = _adapter().admin
        with pytest.raises(ValueError, match="禁言时长"):
            await admin.ban_group_member("456", "123", 2592001)
        with pytest.raises(ValueError, match="禁言时长"):
            await admin.ban_group_member("456", "123", True)
        with pytest.raises(ValueError, match="enable"):
            await admin.set_group_whole_ban("456", "yes")
        with pytest.raises(ValueError, match="enable"):
            await admin.set_group_admin("456", "123", 1)

    @pytest.mark.asyncio
    async def test_text_field_limits(self):
        admin = _adapter().admin
        with pytest.raises(ValueError, match="群名片"):
            await admin.set_group_card("456", "123", "x" * 61)
        with pytest.raises(ValueError, match="群名"):
            await admin.set_group_name("456", "  ")
        with pytest.raises(ValueError, match="头衔"):
            await admin.set_group_special_title("456", "123", "t" * 19)
        with pytest.raises(ValueError, match="荣誉类型"):
            await admin.get_group_honor_info("456", "unknown")
        with pytest.raises(ValueError, match="消息 ID"):
            await admin.recall_message("456", "abc")
        with pytest.raises(ValueError, match="flag"):
            await admin.handle_friend_request("a b", True)
        with pytest.raises(ValueError, match="sub_type"):
            await admin.handle_group_request("456", "f1", "other", True)


class TestOneBotAdminCalls:
    @pytest.mark.asyncio
    async def test_group_list_narrows_fields_and_skips_foreign(self):
        adapter = _adapter()
        adapter._bot.get_group_list.return_value = [
            {"group_id": 456, "group_name": "测试群", "member_count": 3, "max_member_count": 500, "secret": "x"},
            "bad",
        ]
        groups = await adapter.admin.get_group_list()
        assert groups == [{"group_id": 456, "group_name": "测试群", "member_count": 3, "max_member_count": 500}]

    @pytest.mark.asyncio
    async def test_member_info_and_recall_and_requests_call_actions(self):
        adapter = _adapter()
        adapter._bot.get_group_member_info.return_value = {"user_id": 123, "role": "member", "raw_extra": 1}
        info = await adapter.admin.get_group_member_info("456", "123")
        adapter._bot.get_group_member_info.assert_awaited_once_with(group_id=456, user_id=123)
        assert "raw_extra" not in info and info["role"] == "member"
        adapter._bot.get_msg.return_value = {"message_type": "group", "group_id": 456}
        await adapter.admin.recall_message("456", "77")
        adapter._bot.get_msg.assert_awaited_once_with(message_id=77)
        adapter._bot.delete_msg.assert_awaited_once_with(message_id=77)
        adapter.request_flags.register("friend", "flag-1", user_id="99")
        await adapter.admin.handle_friend_request("flag-1", True, "备注")
        adapter._bot.set_friend_add_request.assert_awaited_once_with(flag="flag-1", approve=True, remark="备注")
        adapter.request_flags.register("group", "flag-2", group_id="456", sub_type="invite", user_id="98")
        await adapter.admin.handle_group_request("456", "flag-2", "invite", False, "理由")
        adapter._bot.set_group_add_request.assert_awaited_once_with(flag="flag-2", sub_type="invite", approve=False, reason="理由")
        await adapter.admin.leave_group("456", True)
        adapter._bot.set_group_leave.assert_awaited_once_with(group_id=456, is_dismiss=True)

    @pytest.mark.asyncio
    async def test_action_failures_are_normalized(self):
        adapter = _adapter()
        adapter._bot.set_group_kick.side_effect = ActionFailed({"retcode": 10002})
        with pytest.raises(UnsupportedAdminAction):
            await adapter.admin.kick_group_member("456", "123")
        adapter._bot.set_group_kick.side_effect = ActionFailed({"retcode": 1200})
        with pytest.raises(AdminActionRejected, match="retcode=1200"):
            await adapter.admin.kick_group_member("456", "123")
        adapter._bot.set_group_kick.side_effect = RuntimeError("network")
        with pytest.raises(AdminActionUnconfirmed):
            await adapter.admin.kick_group_member("456", "123")

    @pytest.mark.asyncio
    async def test_missing_client_is_unconfirmed(self):
        adapter = OneBotAdapter(PlatformConfig(id="ob", type="onebot", settings={}))
        with pytest.raises(AdminActionUnconfirmed, match="未连接"):
            await adapter.admin.get_group_list()


class TestVoiceActions:
    @pytest.mark.asyncio
    async def test_get_record_decodes_base64_and_validates_params(self):
        adapter = _adapter()
        wav = b"RIFF" + bytes(4) + b"WAVEfmt "
        adapter._bot.get_record.return_value = {"base64": base64.b64encode(wav).decode(), "out_format": "wav"}
        assert await adapter.admin.get_record("abc.amr", "wav", max_bytes=1024) == wav
        adapter._bot.get_record.assert_awaited_once_with(file="abc.amr", out_format="wav")
        with pytest.raises(ValueError, match="out_format"):
            await adapter.admin.get_record("abc.amr", "silk", max_bytes=1024)
        with pytest.raises(ValueError, match="语音标识"):
            await adapter.admin.get_record("  ", "wav", max_bytes=1024)
        with pytest.raises(ValueError, match="语音标识"):
            await adapter.admin.get_record("bad" + chr(0) + "id", "wav", max_bytes=1024)
        with pytest.raises(ValueError, match="语音标识"):
            await adapter.admin.get_record("x" * 513, "wav", max_bytes=1024)

    @pytest.mark.asyncio
    async def test_get_record_rejects_oversize_missing_and_invalid_payload(self):
        adapter = _adapter()
        adapter._bot.get_record.return_value = {"base64": base64.b64encode(b"x" * 20).decode()}
        with pytest.raises(AdminActionRejected, match="上限"):
            await adapter.admin.get_record("f", "wav", max_bytes=10)
        adapter._bot.get_record.return_value = {"file": "/tmp/x.wav"}
        with pytest.raises(UnsupportedAdminAction, match="base64"):
            await adapter.admin.get_record("f", "wav", max_bytes=10)
        adapter._bot.get_record.return_value = {"base64": "@@@@"}
        with pytest.raises(AdminActionUnconfirmed, match="base64"):
            await adapter.admin.get_record("f", "wav", max_bytes=10)
        adapter._bot.get_record.side_effect = ActionFailed({"retcode": 1404})
        with pytest.raises(UnsupportedAdminAction):
            await adapter.admin.get_record("f", "wav", max_bytes=10)

    @pytest.mark.asyncio
    async def test_fetch_ptt_text_returns_text_or_empty(self):
        adapter = _adapter()
        adapter._bot.fetch_ptt_text.return_value = {"text": "你好"}
        assert await adapter.admin.fetch_ptt_text("123") == "你好"
        adapter._bot.fetch_ptt_text.assert_awaited_once_with(message_id=123)
        adapter._bot.fetch_ptt_text.return_value = {"text": None}
        assert await adapter.admin.fetch_ptt_text(-5) == ""
        with pytest.raises(ValueError, match="消息 ID"):
            await adapter.admin.fetch_ptt_text("abc")

    def test_voice_actions_are_read_capabilities(self):
        assert ADMIN_CAPABILITIES["get_record"][0] == "read"
        assert ADMIN_CAPABILITIES["fetch_ptt_text"][0] == "read"


class TestAdminCapabilities:
    def test_onebot_reports_all_supported_when_client_ready(self):
        adapter = _adapter()
        caps = adapter.admin_capabilities()
        assert set(caps) == set(ADMIN_CAPABILITIES)
        assert set(caps.values()) == {"supported"}
        adapter._bot = None
        assert set(adapter.admin_capabilities().values()) == {"unavailable"}

    def test_base_adapter_has_no_known_capabilities(self):
        from satrap.core.platform.event import PlatformMetadata

        class _Bare(PlatformAdapter):
            adapter_type = "bare"

            async def run(self) -> None:
                return None

            def meta(self) -> PlatformMetadata:
                return PlatformMetadata(name="bare", id="b")

        bare = _Bare(PlatformConfig(id="b", type="bare"))
        assert bare.admin_capabilities() == {}

    def test_stats_include_capabilities(self):
        adapter = _adapter()
        assert adapter.get_stats()["capabilities"]["get_group_list"] == "supported"


class TestDurationNormalization:
    @pytest.mark.asyncio
    async def test_ban_duration_non_numeric_rejected_with_clear_message(self):
        """非数字时长在 int() 转换前被类型校验拦截, 报错文案稳定"""
        admin = _adapter().admin
        with pytest.raises(ValueError, match="禁言时长"):
            await admin.ban_group_member("456", "123", "abc")
        with pytest.raises(ValueError, match="禁言时长"):
            await admin.ban_anonymous("456", "flag", None)
        with pytest.raises(ValueError, match="头衔有效期"):
            await admin.set_group_special_title("456", "123", "t", "x")


class TestRecallOwnership:
    """撤回执行前的 get_msg 回源归属核验"""

    @pytest.mark.asyncio
    async def test_recall_rejects_message_from_other_group(self):
        adapter = _adapter()
        adapter._bot.get_msg.return_value = {"message_type": "group", "group_id": 999}
        with pytest.raises(AdminActionRejected, match="无法确认"):
            await adapter.admin.recall_message("456", "77")
        adapter._bot.get_msg.assert_awaited_once_with(message_id=77)
        adapter._bot.delete_msg.assert_not_called()

    @pytest.mark.asyncio
    async def test_recall_rejects_non_group_message(self):
        adapter = _adapter()
        adapter._bot.get_msg.return_value = {"message_type": "private", "user_id": 456}
        with pytest.raises(AdminActionRejected, match="无法确认"):
            await adapter.admin.recall_message("456", "77")
        adapter._bot.delete_msg.assert_not_called()

    @pytest.mark.asyncio
    async def test_recall_rejects_when_lookup_fails(self):
        adapter = _adapter()
        adapter._bot.get_msg.side_effect = ActionFailed({"retcode": 1200})
        with pytest.raises(AdminActionRejected, match="retcode=1200"):
            await adapter.admin.recall_message("456", "77")
        adapter._bot.delete_msg.assert_not_called()

    @pytest.mark.asyncio
    async def test_recall_rechecks_group_scope_after_lookup(self, monkeypatch: pytest.MonkeyPatch):
        """回源等待期间群范围被收紧时, 执行前复查拦截"""
        adapter = _adapter()
        adapter._bot.get_msg.return_value = {"message_type": "group", "group_id": 456}
        checks = iter([True, False])

        def flip_scope(group_id: str) -> bool:
            return next(checks)

        monkeypatch.setattr(adapter, "allows_group", flip_scope)
        with pytest.raises(AdminActionRejected, match="允许范围"):
            await adapter.admin.recall_message("456", "77")
        adapter._bot.delete_msg.assert_not_called()


class TestRequestFlagOccupancy:
    """审批 flag 的登记, 原子占用与终态迁移"""

    @pytest.mark.asyncio
    async def test_unregistered_flag_rejected(self):
        adapter = _adapter()
        with pytest.raises(AdminActionRejected, match="未登记"):
            await adapter.admin.handle_group_request("456", "flag-x", "add", True)
        adapter._bot.set_group_add_request.assert_not_called()

    @pytest.mark.asyncio
    async def test_wrong_group_or_sub_type_rejected(self):
        adapter = _adapter()
        adapter.request_flags.register("group", "flag-g", group_id="456", sub_type="add", user_id="1")
        with pytest.raises(AdminActionRejected, match="不符"):
            await adapter.admin.handle_group_request("789", "flag-g", "add", True)
        adapter2 = _adapter(group_whitelist=["456", "789"])
        adapter2.request_flags.register("group", "flag-h", group_id="456", sub_type="add", user_id="1")
        with pytest.raises(AdminActionRejected, match="不符"):
            await adapter2.admin.handle_group_request("456", "flag-h", "invite", True)
        adapter._bot.set_group_add_request.assert_not_called()
        adapter2._bot.set_group_add_request.assert_not_called()

    @pytest.mark.asyncio
    async def test_success_consumes_flag_and_replay_rejected(self):
        adapter = _adapter()
        adapter.request_flags.register("group", "flag-r", group_id="456", sub_type="add", user_id="1")
        await adapter.admin.handle_group_request("456", "flag-r", "add", True)
        adapter._bot.set_group_add_request.assert_awaited_once()
        with pytest.raises(AdminActionRejected, match="已被处理"):
            await adapter.admin.handle_group_request("456", "flag-r", "add", True)
        assert adapter._bot.set_group_add_request.await_count == 1

    @pytest.mark.asyncio
    async def test_timeout_marks_unknown_and_replay_rejected(self):
        adapter = _adapter()
        adapter.request_flags.register("group", "flag-t", group_id="456", sub_type="add", user_id="1")
        adapter._bot.set_group_add_request.side_effect = asyncio.TimeoutError()
        with pytest.raises(AdminActionUnconfirmed):
            await adapter.admin.handle_group_request("456", "flag-t", "add", True)
        with pytest.raises(AdminActionRejected, match="已被处理"):
            await adapter.admin.handle_group_request("456", "flag-t", "add", True)
        assert adapter._bot.set_group_add_request.await_count == 1

    @pytest.mark.asyncio
    async def test_concurrent_occupy_has_single_winner(self):
        adapter = _adapter()
        adapter.request_flags.register("group", "flag-c", group_id="456", sub_type="add", user_id="1")
        gate = asyncio.Event()

        async def slow_approve(**kwargs: object) -> None:
            await gate.wait()

        adapter._bot.set_group_add_request.side_effect = slow_approve

        async def release_gate() -> None:
            await asyncio.sleep(0.05)
            gate.set()

        releaser = asyncio.create_task(release_gate())
        results = await asyncio.gather(
            adapter.admin.handle_group_request("456", "flag-c", "add", True),
            adapter.admin.handle_group_request("456", "flag-c", "add", True),
            return_exceptions=True,
        )
        await releaser
        assert sum(1 for item in results if item is None) == 1
        assert sum(1 for item in results if isinstance(item, AdminActionRejected)) == 1

    @pytest.mark.asyncio
    async def test_friend_and_group_tables_are_separate(self):
        adapter = _adapter()
        adapter.request_flags.register("group", "same-flag", group_id="456", sub_type="invite", user_id="1")
        adapter.request_flags.register("friend", "same-flag", user_id="2")
        await adapter.admin.handle_friend_request("same-flag", True)
        adapter._bot.set_friend_add_request.assert_awaited_once()
        await adapter.admin.handle_group_request("456", "same-flag", "invite", True)
        adapter._bot.set_group_add_request.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_duplicate_inbound_does_not_reset_occupied_state(self):
        adapter = _adapter()
        adapter.request_flags.register("group", "flag-d", group_id="456", sub_type="add", user_id="1")
        adapter.request_flags.occupy("group", "flag-d", group_id="456", sub_type="add")
        assert adapter.request_flags.register("group", "flag-d", group_id="456", sub_type="add", user_id="1") is False
        with pytest.raises(AdminActionRejected, match="已被处理"):
            await adapter.admin.handle_group_request("456", "flag-d", "add", True)
        adapter._bot.set_group_add_request.assert_not_called()


class TestRequestFlagRegistryUnit:
    def test_expired_flag_cannot_be_occupied(self):
        from satrap.core.platform.onebot.request_registry import RequestFlagRegistry

        registry = RequestFlagRegistry(limit=8, ttl=10.0)
        registry.register("group", "flag-old", group_id="456", sub_type="add", user_id="1", now=1000.0)
        with pytest.raises(LookupError, match="过期"):
            registry.occupy("group", "flag-old", group_id="456", sub_type="add", now=1011.0)

    def test_capacity_evicts_oldest(self):
        from satrap.core.platform.onebot.request_registry import RequestFlagRegistry

        registry = RequestFlagRegistry(limit=2, ttl=600.0)
        registry.register("group", "f1", group_id="1", sub_type="add", now=1000.0)
        registry.register("group", "f2", group_id="1", sub_type="add", now=1001.0)
        registry.register("group", "f3", group_id="1", sub_type="add", now=1002.0)
        with pytest.raises(LookupError):
            registry.occupy("group", "f1", group_id="1", sub_type="add", now=1003.0)
        registry.occupy("group", "f3", group_id="1", sub_type="add", now=1003.0)

    def test_settle_only_from_executing_and_only_terminal(self):
        from satrap.core.platform.onebot.request_registry import RequestFlagRegistry

        registry = RequestFlagRegistry()
        registry.register("friend", "f", user_id="1")
        registry.settle("friend", "f", "completed")
        registry.occupy("friend", "f")
        with pytest.raises(ValueError):
            registry.settle("friend", "f", "available")
        registry.settle("friend", "f", "unknown")
        with pytest.raises(LookupError):
            registry.occupy("friend", "f")


class TestRequestRegistration:
    """入站 request 事件的 flag 登记"""

    @pytest.mark.asyncio
    async def test_handle_request_registers_group_and_friend_flags(self, monkeypatch: pytest.MonkeyPatch):
        adapter = _adapter(self_id="10")
        emit = AsyncMock()
        monkeypatch.setattr(adapter, "_emit_notice", emit)
        await adapter._handle_request({"self_id": 10, "request_type": "group", "sub_type": "add",
                                       "group_id": 456, "user_id": 77, "flag": "g-flag"})
        await adapter._handle_request({"self_id": 10, "request_type": "friend", "user_id": 88, "flag": "f-flag"})
        adapter.request_flags.occupy("group", "g-flag", group_id="456", sub_type="add")
        adapter.request_flags.occupy("friend", "f-flag")
        assert emit.await_count == 2

    @pytest.mark.asyncio
    async def test_foreign_or_missing_account_not_registered(self, monkeypatch: pytest.MonkeyPatch):
        adapter = _adapter(self_id="10")
        monkeypatch.setattr(adapter, "_emit_notice", AsyncMock())
        await adapter._handle_request({"self_id": 999, "request_type": "group", "sub_type": "add",
                                       "group_id": 456, "user_id": 77, "flag": "alien"})
        await adapter._handle_request({"request_type": "friend", "user_id": 88, "flag": "no-self"})
        with pytest.raises(LookupError):
            adapter.request_flags.occupy("group", "alien", group_id="456", sub_type="add")
        with pytest.raises(LookupError):
            adapter.request_flags.occupy("friend", "no-self")
