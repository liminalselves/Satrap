"""OneBot 群管理动作封装的参数校验, 群范围与错误归一"""
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock
from time import monotonic
import asyncio
import base64
import json
import threading
import time

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
from satrap.core.platform.onebot.request_registry import (
    LEDGER_INSTANCE_CAPACITY,
    LEDGER_TOTAL_CAPACITY,
    REQUEST_FLAG_TTL,
    RequestApprovalLedger,
    RequestFlagRegistry,
)
from satrap.core.platform.receipt import SendReceipt
from satrap.core.platform import PlatformAdapter, PlatformConfig


def _adapter(**settings: object) -> OneBotAdapter:
    adapter = OneBotAdapter(PlatformConfig(id="ob", type="onebot", settings=dict(settings)))
    adapter._bot = AsyncMock()
    if not adapter.bot_self_id:
        adapter.bot_self_id = "10000"
    return adapter


async def _register_flag(
    adapter: OneBotAdapter, kind: str, flag: str, *,
    group_id: str = "", sub_type: str = "", user_id: str = "", now: float | None = None,
) -> bool:
    """测试辅助: 以当前绑定账号登记审批 flag"""
    registry = adapter.request_flags
    return await registry.register(
        kind, flag, self_id=adapter.bot_self_id, group_id=group_id, sub_type=sub_type, user_id=user_id, now=now,
    )


async def _occupy_flag(
    adapter: OneBotAdapter, kind: str, flag: str, *,
    group_id: str = "", sub_type: str = "", now: float | None = None,
) -> None:
    """测试辅助: 以当前绑定账号占用审批 flag"""
    registry = adapter.request_flags
    await registry.occupy(
        kind, flag, self_id=adapter.bot_self_id, group_id=group_id, sub_type=sub_type, now=now,
    )


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
        await _register_flag(adapter, "friend", "flag-1", user_id="99")
        await adapter.admin.handle_friend_request("flag-1", True, "备注")
        adapter._bot.set_friend_add_request.assert_awaited_once_with(flag="flag-1", approve=True, remark="备注")
        await _register_flag(adapter, "group", "flag-2", group_id="456", sub_type="invite", user_id="98")
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
    def test_capabilities_follow_connection_and_passive_learning(self):
        adapter = _adapter()
        caps = adapter.admin_capabilities()
        assert set(caps) == set(ADMIN_CAPABILITIES)
        # 服务未运行时一律 unavailable, 不以 _bot 对象存在充当已连接
        assert set(caps.values()) == {"unavailable"}
        adapter._running = True
        # meta 订阅未挂上时无法判定连接, 未学习的动作保持 unknown
        assert set(adapter.admin_capabilities().values()) == {"unknown"}
        adapter.note_action_outcome("get_group_list", True)
        adapter.note_action_outcome("set_group_kick", False)
        caps = adapter.admin_capabilities()
        assert caps["get_group_list"] == "supported"
        assert caps["kick_group_member"] == "unsupported"
        assert caps["get_group_info"] == "unknown"
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
        adapter._running = True
        adapter.note_action_outcome("get_group_list", True)
        assert adapter.get_stats()["capabilities"]["get_group_list"] == "supported"

    @pytest.mark.asyncio
    async def test_admin_call_outcome_feeds_capability_learning(self):
        adapter = _adapter()
        adapter._running = True
        adapter._bot.get_group_list.return_value = []
        await adapter.admin.get_group_list()
        assert adapter.admin_capabilities()["get_group_list"] == "supported"
        adapter._bot.set_group_kick.side_effect = ActionFailed({"retcode": 10002})
        with pytest.raises(UnsupportedAdminAction):
            await adapter.admin.kick_group_member("456", "123")
        assert adapter.admin_capabilities()["kick_group_member"] == "unsupported"


class TestCapabilityConnectionStates:
    """meta 事件驱动的连接状态与按连接代次失效的能力缓存"""

    @pytest.mark.asyncio
    async def test_meta_connect_tracks_connection_and_resets_learning(self):
        adapter = _adapter()
        adapter._running = True
        adapter._meta_hooked = True
        # meta 订阅已挂上但无连接证据: unavailable 而非 unknown
        assert set(adapter.admin_capabilities().values()) == {"unavailable"}
        await adapter._handle_meta({"self_id": "10000", "meta_event_type": "lifecycle", "sub_type": "connect"})
        assert adapter.bot_self_id == "10000"
        assert set(adapter.admin_capabilities().values()) == {"unknown"}
        adapter.note_action_outcome("get_group_list", True)
        assert adapter.admin_capabilities()["get_group_list"] == "supported"
        # 新连接代次: 已学习状态降级回 unknown, 被动重新学习
        await adapter._handle_meta({"self_id": "10000", "meta_event_type": "lifecycle", "sub_type": "connect"})
        assert adapter.admin_capabilities()["get_group_list"] == "unknown"

    @pytest.mark.asyncio
    async def test_meta_from_foreign_account_is_rejected(self):
        adapter = _adapter()
        adapter.bot_self_id = "10000"
        await adapter._handle_meta({"self_id": "20000", "meta_event_type": "lifecycle", "sub_type": "connect"})
        assert adapter._client_connected is False
        assert adapter._ingress_rejections["account"] == 1

    @pytest.mark.asyncio
    async def test_lifecycle_disable_and_heartbeat_staleness(self):
        adapter = _adapter()
        adapter._running = True
        adapter._meta_hooked = True
        await adapter._handle_meta({"self_id": "10000", "meta_event_type": "lifecycle", "sub_type": "connect"})
        await adapter._handle_meta({"self_id": "10000", "meta_event_type": "heartbeat"})
        await adapter._handle_meta({"self_id": "10000", "meta_event_type": "heartbeat"})
        assert set(adapter.admin_capabilities().values()) == {"unknown"}
        # 心跳流已建立却长期静默, 视为连接已断开
        adapter._last_heartbeat_at = monotonic() - 120
        assert set(adapter.admin_capabilities().values()) == {"unavailable"}
        await adapter._handle_meta({"self_id": "10000", "meta_event_type": "heartbeat"})
        assert set(adapter.admin_capabilities().values()) == {"unknown"}
        # 实现上报 disable 生命周期, 直接标记断开
        await adapter._handle_meta({"self_id": "10000", "meta_event_type": "lifecycle", "sub_type": "disable"})
        assert set(adapter.admin_capabilities().values()) == {"unavailable"}


class TestReadAndForwardTools:
    """get_message/get_forward_message 的归属核验与 send_group_forward 的公共发送路径"""

    @pytest.mark.asyncio
    async def test_get_message_verifies_group_and_narrows_fields(self):
        adapter = _adapter()
        adapter._bot.get_msg.return_value = {
            "message_id": 77, "message_type": "group", "group_id": 456, "time": 1700000000,
            "sender": {"user_id": 123, "nickname": "成员", "card": "卡", "role": "member", "secret": "x"},
            "message": [{"type": "text", "data": {"text": "你好"}}], "raw_extra": 1,
        }
        data = await adapter.admin.get_message("456", "77")
        adapter._bot.get_msg.assert_awaited_once_with(message_id=77)
        assert data["sender"] == {"user_id": 123, "nickname": "成员", "card": "卡", "role": "member"}
        assert data["message"] == [{"type": "text", "data": {"text": "你好"}}]
        assert "raw_extra" not in data

    @pytest.mark.asyncio
    async def test_get_message_rejects_cross_group_and_out_of_scope(self):
        adapter = _adapter()
        adapter._bot.get_msg.return_value = {"message_type": "group", "group_id": 999}
        with pytest.raises(AdminActionRejected, match="已拒绝读取"):
            await adapter.admin.get_message("456", "77")
        scoped = _adapter(group_whitelist=["789"])
        with pytest.raises(AdminActionRejected, match="允许范围"):
            await scoped.admin.get_message("456", "77")
        scoped._bot.get_msg.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_forward_message_requires_source_proof(self):
        adapter = _adapter()
        adapter.bot_self_id = "10000"
        adapter._bot.get_msg.return_value = {
            "message_id": 77, "message_type": "group", "group_id": 456, "self_id": 10000,
            "message": [{"type": "text", "data": {"text": "看这个"}}, {"type": "forward", "data": {"id": "fwd-1"}}],
        }
        adapter._bot.get_forward_msg.return_value = {
            "messages": [
                {"user_id": 123, "nickname": "甲", "time": 1,
                 "content": [{"type": "text", "data": {"text": "第一条"}}]},
                {"user_id": 124, "nickname": "乙", "time": 2,
                 "content": [{"type": "image", "data": {"file": "x.jpg"}}]},
            ],
        }
        nodes = await adapter.admin.get_forward_message("456", "fwd-1", "77")
        adapter._bot.get_msg.assert_awaited_once_with(message_id=77)
        adapter._bot.get_forward_msg.assert_awaited_once_with(id="fwd-1")
        assert nodes[0]["name"] == "甲" and nodes[0]["text"] == "第一条"
        assert nodes[1]["text"] == "[Image]"
        assert adapter.admin_capabilities()["get_forward_message"] == "unavailable"  # 服务未运行时连接状态优先
        adapter._running = True
        assert adapter.admin_capabilities()["get_forward_message"] == "supported"

    @pytest.mark.asyncio
    async def test_get_forward_message_unknown_when_lookup_fails(self):
        adapter = _adapter()
        adapter.bot_self_id = "10000"
        adapter._bot.get_msg.return_value = {
            "message_id": 77, "message_type": "group", "group_id": 456,
            "message": [{"type": "forward", "data": {"id": "fwd-1"}}],
        }
        adapter._bot.get_forward_msg.side_effect = RuntimeError("network")
        with pytest.raises(AdminActionUnconfirmed, match="转发回源失败"):
            await adapter.admin.get_forward_message("456", "fwd-1", "77")
        scoped = _adapter(group_whitelist=["789"])
        with pytest.raises(AdminActionRejected, match="允许范围"):
            await scoped.admin.get_forward_message("456", "fwd-1", "77")
        scoped._bot.get_msg.assert_not_called()
        scoped._bot.get_forward_msg.assert_not_called()


class TestForwardSourceProof:
    """合并转发读取必须证明来源消息与 forward_id 的关联 (B1 反例)"""

    @pytest.mark.asyncio
    async def test_source_message_from_other_group_is_rejected(self):
        adapter = _adapter()
        adapter.bot_self_id = "10000"
        adapter._bot.get_msg.return_value = {
            "message_id": 77, "message_type": "group", "group_id": 999,
            "message": [{"type": "forward", "data": {"id": "foreign-id"}}],
        }
        with pytest.raises(AdminActionRejected, match="来源消息属于目标群"):
            await adapter.admin.get_forward_message("456", "foreign-id", "77")
        adapter._bot.get_forward_msg.assert_not_called()

    @pytest.mark.asyncio
    async def test_forward_id_absent_from_source_message_is_rejected(self):
        adapter = _adapter()
        adapter.bot_self_id = "10000"
        # 正文里出现相同字符串不构成授权, 只认顶层 forward 段
        adapter._bot.get_msg.return_value = {
            "message_id": 77, "message_type": "group", "group_id": 456,
            "message": [{"type": "forward", "data": {"id": "other-id"}},
                        {"type": "text", "data": {"text": "fwd-1"}}],
        }
        with pytest.raises(AdminActionRejected, match="未出现在来源消息"):
            await adapter.admin.get_forward_message("456", "fwd-1", "77")
        adapter._bot.get_forward_msg.assert_not_called()

    @pytest.mark.asyncio
    async def test_nested_forward_does_not_authorize(self):
        adapter = _adapter()
        adapter.bot_self_id = "10000"
        adapter._bot.get_msg.return_value = {
            "message_id": 77, "message_type": "group", "group_id": 456,
            "message": [{"type": "forward", "data": {
                "id": "outer-id",
                "content": [{"type": "node", "data": {"user_id": 1, "content": [
                    {"type": "forward", "data": {"id": "inner-id"}},
                ]}}],
            }}],
        }
        adapter._bot.get_forward_msg.return_value = {"messages": []}
        with pytest.raises(AdminActionRejected, match="未出现在来源消息"):
            await adapter.admin.get_forward_message("456", "inner-id", "77")
        adapter._bot.get_forward_msg.assert_not_called()
        # 顶层 ID 通过归属证明后才允许回源
        assert await adapter.admin.get_forward_message("456", "outer-id", "77") == []
        adapter._bot.get_forward_msg.assert_awaited_once_with(id="outer-id")

    @pytest.mark.asyncio
    async def test_account_and_message_id_mismatch_are_rejected(self):
        adapter = _adapter()
        adapter.bot_self_id = "10000"
        adapter._bot.get_msg.return_value = {
            "message_id": 78, "message_type": "group", "group_id": 456, "self_id": 20000,
            "message": [{"type": "forward", "data": {"id": "fwd-1"}}],
        }
        with pytest.raises(AdminActionRejected, match="账号与当前绑定账号不一致"):
            await adapter.admin.get_forward_message("456", "fwd-1", "78")
        adapter._bot.get_msg.return_value = {
            "message_id": 78, "message_type": "group", "group_id": 456,
            "message": [{"type": "forward", "data": {"id": "fwd-1"}}],
        }
        with pytest.raises(AdminActionRejected, match="请求消息 ID 不一致"):
            await adapter.admin.get_forward_message("456", "fwd-1", "77")
        adapter._bot.get_forward_msg.assert_not_called()

    @pytest.mark.asyncio
    async def test_lookup_failure_and_stale_generation_block_read(self):
        adapter = _adapter()
        adapter.bot_self_id = "10000"
        adapter._bot.get_msg.side_effect = RuntimeError("network")
        with pytest.raises(AdminActionUnconfirmed):
            await adapter.admin.get_forward_message("456", "fwd-1", "77")
        adapter._bot.get_forward_msg.assert_not_called()

        stale = _adapter()
        stale.bot_self_id = "10000"

        async def reconnect(**_: object) -> dict[str, object]:
            stale._connection_generation += 1
            return {"message_id": 77, "message_type": "group", "group_id": 456,
                    "message": [{"type": "forward", "data": {"id": "fwd-1"}}]}

        stale._bot.get_msg.side_effect = reconnect
        with pytest.raises(AdminActionUnconfirmed, match="来源证明失效"):
            await stale.admin.get_forward_message("456", "fwd-1", "77")
        stale._bot.get_forward_msg.assert_not_called()

    @pytest.mark.asyncio
    async def test_response_with_contradictory_group_is_rejected(self):
        adapter = _adapter()
        adapter.bot_self_id = "10000"
        adapter._bot.get_msg.return_value = {
            "message_id": 77, "message_type": "group", "group_id": 456,
            "message": [{"type": "forward", "data": {"id": "fwd-1"}}],
        }
        adapter._bot.get_forward_msg.return_value = {"group_id": 999, "messages": [
            {"user_id": 1, "nickname": "甲", "content": [{"type": "text", "data": {"text": "FOREIGN_SECRET"}}]},
        ]}
        with pytest.raises(AdminActionUnconfirmed, match="转发回源失败"):
            await adapter.admin.get_forward_message("456", "fwd-1", "77")

    @pytest.mark.asyncio
    async def test_missing_source_message_id_is_a_parameter_error(self):
        adapter = _adapter()
        with pytest.raises(ValueError, match="来源消息 ID 必须为整数"):
            await adapter.admin.get_forward_message("456", "fwd-1", "")
        adapter._bot.get_msg.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_group_forward_uses_public_send_path(self, monkeypatch: pytest.MonkeyPatch):
        adapter = _adapter()
        adapter.bot_self_id = "10000"
        adapter._running = True
        adapter._bot.send_group_forward_msg.return_value = {"message_id": 555}
        calls: list[str] = []
        original_run = adapter._outbound.run

        async def spy_run(target: str, operation: Callable[[], Awaitable[SendReceipt]]) -> SendReceipt:
            calls.append(target)
            return await original_run(target, operation)

        monkeypatch.setattr(adapter._outbound, "run", spy_run)
        result = await adapter.admin.send_group_forward("456", [{"content": "第一段"}, {"content": "第二段", "name": "助手"}])
        assert result["status"] == "success" and result["message_ids"] == ["555"]
        # 经公共发送路径: OutboundTurns 整轮排序与拆分约束未被绕过
        assert calls == ["group%456"]
        payload = adapter._bot.send_group_forward_msg.await_args.kwargs["messages"]
        assert [item["data"]["nickname"] for item in payload] == ["Satrap", "助手"]
        assert all(item["data"]["user_id"] == "10000" for item in payload)
        # 群侧转发动作已学习, 私聊侧未学习, 聚合状态保持 unknown
        assert adapter.admin_capabilities()["send_forward"] == "unknown"
        adapter.note_action_outcome("send_private_forward_msg", True)
        assert adapter.admin_capabilities()["send_forward"] == "supported"

    @pytest.mark.asyncio
    async def test_send_group_forward_validates_nodes_and_account(self):
        adapter = _adapter()
        adapter.bot_self_id = "10000"
        with pytest.raises(ValueError, match="nodes"):
            await adapter.admin.send_group_forward("456", [])
        with pytest.raises(ValueError, match="nodes"):
            await adapter.admin.send_group_forward("456", [{"content": "x"}] * 31)
        with pytest.raises(ValueError, match="节点正文"):
            await adapter.admin.send_group_forward("456", [{"content": ""}])
        with pytest.raises(ValueError, match="节点正文"):
            await adapter.admin.send_group_forward("456", [{"content": "x" * 2001}])
        adapter.bot_self_id = ""
        with pytest.raises(AdminActionUnconfirmed, match="机器人账号未知"):
            await adapter.admin.send_group_forward("456", [{"content": "x"}])
        adapter._bot.send_group_forward_msg.assert_not_called()


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
        await _register_flag(adapter, "group", "flag-g", group_id="456", sub_type="add", user_id="1")
        with pytest.raises(AdminActionRejected, match="不符"):
            await adapter.admin.handle_group_request("789", "flag-g", "add", True)
        adapter2 = _adapter(group_whitelist=["456", "789"])
        await _register_flag(adapter2, "group", "flag-h", group_id="456", sub_type="add", user_id="1")
        with pytest.raises(AdminActionRejected, match="不符"):
            await adapter2.admin.handle_group_request("456", "flag-h", "invite", True)
        adapter._bot.set_group_add_request.assert_not_called()
        adapter2._bot.set_group_add_request.assert_not_called()

    @pytest.mark.asyncio
    async def test_success_consumes_flag_and_replay_rejected(self):
        adapter = _adapter()
        await _register_flag(adapter, "group", "flag-r", group_id="456", sub_type="add", user_id="1")
        await adapter.admin.handle_group_request("456", "flag-r", "add", True)
        adapter._bot.set_group_add_request.assert_awaited_once()
        with pytest.raises(AdminActionRejected, match="已被处理"):
            await adapter.admin.handle_group_request("456", "flag-r", "add", True)
        assert adapter._bot.set_group_add_request.await_count == 1

    @pytest.mark.asyncio
    async def test_timeout_marks_unknown_and_replay_rejected(self):
        adapter = _adapter()
        await _register_flag(adapter, "group", "flag-t", group_id="456", sub_type="add", user_id="1")
        adapter._bot.set_group_add_request.side_effect = asyncio.TimeoutError()
        with pytest.raises(AdminActionUnconfirmed):
            await adapter.admin.handle_group_request("456", "flag-t", "add", True)
        with pytest.raises(AdminActionRejected, match="已被处理"):
            await adapter.admin.handle_group_request("456", "flag-t", "add", True)
        assert adapter._bot.set_group_add_request.await_count == 1

    @pytest.mark.asyncio
    async def test_concurrent_occupy_has_single_winner(self):
        adapter = _adapter()
        await _register_flag(adapter, "group", "flag-c", group_id="456", sub_type="add", user_id="1")
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
        await _register_flag(adapter, "group", "same-flag", group_id="456", sub_type="invite", user_id="1")
        await _register_flag(adapter, "friend", "same-flag", user_id="2")
        await adapter.admin.handle_friend_request("same-flag", True)
        adapter._bot.set_friend_add_request.assert_awaited_once()
        await adapter.admin.handle_group_request("456", "same-flag", "invite", True)
        adapter._bot.set_group_add_request.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_duplicate_inbound_does_not_reset_occupied_state(self):
        adapter = _adapter()
        await _register_flag(adapter, "group", "flag-d", group_id="456", sub_type="add", user_id="1")
        await _occupy_flag(adapter, "group", "flag-d", group_id="456", sub_type="add")
        assert await _register_flag(adapter, "group", "flag-d", group_id="456", sub_type="add", user_id="1") is False
        with pytest.raises(AdminActionRejected, match="已被处理"):
            await adapter.admin.handle_group_request("456", "flag-d", "add", True)
        adapter._bot.set_group_add_request.assert_not_called()


class TestRequestFlagRegistryUnit:
    @pytest.mark.asyncio
    async def test_expired_flag_cannot_be_occupied(self):
        registry = RequestFlagRegistry("ob", limit=8, ttl=10.0)
        await registry.register("group", "flag-old", self_id="10000", group_id="456", sub_type="add",
                                user_id="1", now=1000.0)
        with pytest.raises(LookupError, match="时限"):
            await registry.occupy("group", "flag-old", self_id="10000", group_id="456", sub_type="add", now=1011.0)

    @pytest.mark.asyncio
    async def test_cache_eviction_keeps_ledger_identity(self):
        """反例: 近期缓存淘汰只影响性能, 不得让已登记的 flag 掉出审批资格或变成可重复审批"""
        registry = RequestFlagRegistry("ob", limit=1, ttl=600.0)
        await registry.register("group", "f1", self_id="10000", group_id="1", sub_type="add", now=1000.0)
        await registry.register("group", "f2", self_id="10000", group_id="1", sub_type="add", now=1001.0)
        assert registry.cached_state("group", "f1") is None    # 最旧一条已被淘汰
        await registry.occupy("group", "f1", self_id="10000", group_id="1", sub_type="add", now=1002.0)
        assert await registry.ledger.occupy("ob", "10000", "group", "f2", group_id="1", sub_type="add",
                                            now=1003.0) is not None
        with pytest.raises(LookupError, match="已被处理"):
            await registry.occupy("group", "f1", self_id="10000", group_id="1", sub_type="add", now=1004.0)

    @pytest.mark.asyncio
    async def test_settle_only_from_executing_and_only_terminal(self):
        registry = RequestFlagRegistry("ob")
        await registry.register("friend", "f", self_id="10000", now=1000.0)
        await registry.settle("friend", "f", self_id="10000", state="completed")   # 未占用不生效
        assert registry.cached_state("friend", "f") == "available"
        await registry.occupy("friend", "f", self_id="10000", now=1000.0)
        with pytest.raises(ValueError):
            await registry.settle("friend", "f", self_id="10000", state="available")
        await registry.settle("friend", "f", self_id="10000", state="unknown")
        assert registry.cached_state("friend", "f") == "unknown"
        with pytest.raises(LookupError):
            await registry.occupy("friend", "f", self_id="10000", now=1000.0)


class TestApprovalLedger:
    """审批账本的持久反重放语义: 并发, 取消, 容量, 重启与损坏"""

    def _ledger(
        self, tmp_path: Path, *, ttl: float = REQUEST_FLAG_TTL, instance_capacity: int = LEDGER_INSTANCE_CAPACITY,
        total_capacity: int = LEDGER_TOTAL_CAPACITY,
    ) -> RequestApprovalLedger:
        return RequestApprovalLedger(
            tmp_path / "request_ledger.json",
            ttl=ttl, instance_capacity=instance_capacity, total_capacity=total_capacity,
        )

    @staticmethod
    def _ledger_states(tmp_path: Path) -> list[str]:
        path = tmp_path / "request_ledger.json"
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        raw = cast(dict[str, Any], payload) if isinstance(payload, dict) else {}
        entries = raw.get("entries")
        if not isinstance(entries, dict):
            return []
        states: list[str] = []
        for item in cast(dict[str, Any], entries).values():
            state = cast(dict[str, Any], item).get("state") if isinstance(item, dict) else None
            states.append(state if isinstance(state, str) else "")
        return states

    @pytest.mark.asyncio
    async def test_dual_instance_concurrent_occupy_single_winner(self, tmp_path: Path):
        """反例: 两个 store 实例并发占用同一 flag 时只允许一个成功"""
        first, second = self._ledger(tmp_path), self._ledger(tmp_path)
        results = await asyncio.gather(
            first.register("ob", "10000", "group", "flag-x", group_id="456", sub_type="add", now=1000.0),
            second.register("ob", "10000", "group", "flag-x", group_id="456", sub_type="add", now=1000.0),
            return_exceptions=True,
        )
        assert "registered" in results
        occupied = await asyncio.gather(
            first.occupy("ob", "10000", "group", "flag-x", group_id="456", sub_type="add", now=1001.0),
            second.occupy("ob", "10000", "group", "flag-x", group_id="456", sub_type="add", now=1001.0),
            return_exceptions=True,
        )
        assert sum(1 for item in occupied if isinstance(item, LookupError)) == 1
        assert sum(1 for item in occupied if isinstance(item, dict)) == 1
        assert self._ledger_states(tmp_path) == ["executing"]

    @pytest.mark.asyncio
    async def test_cancel_during_occupy_keeps_executing_and_sends_nothing(self, tmp_path: Path):
        """反例: 占用落盘期间取消不得回滚成 available, 结果未知时不得再发网络动作"""
        from satrap.core.storage.file_lock import FileLock

        ledger = self._ledger(tmp_path)
        await ledger.register("ob", "10000", "friend", "flag-c", user_id="7", now=1000.0)
        hold, release = threading.Event(), threading.Event()

        def _hold_lock() -> None:
            with FileLock(tmp_path / ".request_ledger.json.lock"):
                hold.set()
                release.wait(10.0)

        blocker = threading.Thread(target=_hold_lock, daemon=True)
        blocker.start()
        assert hold.wait(5.0)
        gateway = AsyncMock()
        task = asyncio.create_task(self._approve(ledger, gateway, "flag-c"))
        await asyncio.sleep(0.2)     # 让工作线程停在文件锁上, 取消发生在占用落盘过程中
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        blocker.join(5.0)
        assert gateway.await_count == 0
        for _ in range(200):
            if self._ledger_states(tmp_path) == ["executing"]:
                break
            await asyncio.sleep(0.01)
        assert self._ledger_states(tmp_path) == ["executing"]
        with pytest.raises(LookupError, match="结果未知"):
            await ledger.occupy("ob", "10000", "friend", "flag-c", now=1002.0)

    async def _approve(self, ledger: RequestApprovalLedger, gateway: AsyncMock, flag: str) -> None:
        await ledger.occupy("ob", "10000", "friend", flag, now=1001.0)
        await gateway(flag)

    @pytest.mark.asyncio
    async def test_restart_after_cancel_keeps_unknown_not_available(self, tmp_path: Path):
        """反例: 取消后重启, 该 flag 仍不可审批"""
        ledger = self._ledger(tmp_path)
        await ledger.register("ob", "10000", "friend", "flag-a", user_id="7", now=1000.0)
        await ledger.occupy("ob", "10000", "friend", "flag-a", now=1001.0)
        revived = self._ledger(tmp_path)
        with pytest.raises(LookupError, match="结果未知"):
            await revived.occupy("ob", "10000", "friend", "flag-a", now=1002.0)

    @pytest.mark.asyncio
    async def test_capacity_refuses_new_and_old_identity_stays_consumed(self, tmp_path: Path):
        """反例: 容量达限拒绝新登记, 不淘汰旧身份, 旧已消费身份不得回到可审批"""
        ledger = self._ledger(tmp_path, instance_capacity=2)
        await ledger.register("ob", "10000", "group", "k1", group_id="1", sub_type="add", now=1000.0)
        await ledger.register("ob", "10000", "group", "k2", group_id="1", sub_type="add", now=1001.0)
        assert await ledger.register("ob", "10000", "group", "k3", group_id="1", sub_type="add", now=1002.0) == "capacity"
        await ledger.occupy("ob", "10000", "group", "k1", group_id="1", sub_type="add", now=1003.0)
        await ledger.settle("ob", "10000", "group", "k1", "completed")
        assert await ledger.register("ob", "10000", "group", "k1", group_id="1", sub_type="add", now=1004.0) == "duplicate"
        with pytest.raises(LookupError, match="已被处理"):
            await ledger.occupy("ob", "10000", "group", "k1", group_id="1", sub_type="add", now=1005.0)
        assert self._ledger_states(tmp_path) == ["completed", "available"]

    @pytest.mark.asyncio
    async def test_duplicate_after_expiry_does_not_refresh_ttl(self, tmp_path: Path):
        """反例: 重复入站不刷新可审批时限, 过期墓碑保持不可审批"""
        ledger = self._ledger(tmp_path, ttl=10.0)
        await ledger.register("ob", "10000", "group", "k1", group_id="1", sub_type="add", now=1000.0)
        assert await ledger.register("ob", "10000", "group", "k1", group_id="1", sub_type="add", now=1020.0) == "duplicate"
        assert ledger.lookup("ob", "10000", "group", "k1") is not None
        with pytest.raises(LookupError, match="拒绝"):
            await ledger.occupy("ob", "10000", "group", "k1", group_id="1", sub_type="add", now=1021.0)
        assert await ledger.register("ob", "10000", "group", "k1", group_id="1", sub_type="add", now=1030.0) == "duplicate"
        assert self._ledger_states(tmp_path) == ["expired"]

    @pytest.mark.asyncio
    async def test_restart_sweeps_executing_to_unknown(self, tmp_path: Path):
        """反例: 重启后无法确认的占用不得回到可审批"""
        ledger = self._ledger(tmp_path)
        await ledger.register("ob", "10000", "friend", "k1", user_id="7", now=1000.0)
        await ledger.occupy("ob", "10000", "friend", "k1", now=1001.0)
        revived = self._ledger(tmp_path)
        with pytest.raises(LookupError, match="结果未知"):
            await revived.occupy("ob", "10000", "friend", "k1", now=1002.0)
        assert self._ledger_states(tmp_path) == ["unknown"]

    @pytest.mark.asyncio
    async def test_same_key_conflict_rejected_and_kinds_isolated(self, tmp_path: Path):
        """反例: 同 key 归属不符拒绝登记; 好友与群请求分域互不占用"""
        ledger = self._ledger(tmp_path)
        assert await ledger.register("ob", "10000", "group", "k1", group_id="1", sub_type="add", now=1000.0) == "registered"
        assert await ledger.register("ob", "10000", "group", "k1", group_id="2", sub_type="add", now=1001.0) == "conflict"
        await ledger.register("ob", "10000", "friend", "k1", user_id="7", now=1002.0)
        await ledger.occupy("ob", "10000", "friend", "k1", now=1003.0)
        group_entry = ledger.lookup("ob", "10000", "group", "k1")
        assert group_entry is not None and group_entry["state"] == "available"

    @pytest.mark.asyncio
    async def test_unknown_account_not_registered(self, tmp_path: Path):
        ledger = self._ledger(tmp_path)
        assert await ledger.register("ob", "", "group", "k1", group_id="1", sub_type="add", now=1000.0) == "unknown_account"
        assert ledger.pending_counts()["entries_total"] == 0

    @pytest.mark.asyncio
    async def test_corrupt_ledger_degrades_and_survives_restart(self, tmp_path: Path):
        """反例: 账本损坏按降级处理, 重启后仍是降级而不是空账本"""
        path = tmp_path / "request_ledger.json"
        path.write_text("{ 不是 JSON", encoding="utf-8")
        ledger = self._ledger(tmp_path)
        assert ledger.degraded is True
        assert await ledger.register("ob", "10000", "group", "k1", group_id="1", sub_type="add", now=1000.0) == "degraded"
        with pytest.raises(LookupError, match="不可用"):
            await ledger.occupy("ob", "10000", "group", "k1", group_id="1", sub_type="add", now=1001.0)
        assert ledger.recover() is False     # 坏文件已隔离, 没有可校验的原文件
        revived = self._ledger(tmp_path)
        assert revived.degraded is True

    @pytest.mark.asyncio
    async def test_corrupt_ledger_keeps_degraded_after_repair(self, tmp_path: Path):
        """反例: 补回合法账本后降级保持, 必须显式 recover 才能继续登记"""
        path = tmp_path / "request_ledger.json"
        path.write_text("{ 不是 JSON", encoding="utf-8")
        ledger = self._ledger(tmp_path)
        assert ledger.degraded is True
        path.write_text(json.dumps({"version": 1, "entries": {}}), encoding="utf-8")
        assert await ledger.register("ob", "10000", "group", "k1", group_id="1", sub_type="add", now=1000.0) == "degraded"
        assert ledger.recover() is True
        assert ledger.degraded is False
        assert await ledger.register("ob", "10000", "group", "k1", group_id="1", sub_type="add", now=1001.0) == "registered"

    @pytest.mark.asyncio
    async def test_missing_manifest_with_existing_file_is_migrated(self, tmp_path: Path):
        """反例: 账本文件存在但清单缺失不得当作首次初始化, 应迁移并保留既有身份"""
        ledger = self._ledger(tmp_path)
        await ledger.register("ob", "10000", "group", "k1", group_id="1", sub_type="add", user_id="7", now=1000.0)
        await ledger.occupy("ob", "10000", "group", "k1", group_id="1", sub_type="add", now=1001.0)
        (tmp_path / "request_ledger.manifest.json").unlink()
        migrated = self._ledger(tmp_path)
        assert migrated.degraded is False
        assert migrated.pending_counts()["entries_total"] == 1
        assert self._ledger_states(tmp_path) == ["unknown"]     # 迁移时按重启规则结束无法确认的占用
        with pytest.raises(LookupError, match="结果未知"):
            await migrated.occupy("ob", "10000", "group", "k1", group_id="1", sub_type="add", now=1002.0)


class TestApprovalAcrossRestart:
    """适配器重启后同一 flag 不可重放, 且不得再发出网络动作"""

    @pytest.mark.asyncio
    async def test_occupied_flag_not_replayable_after_restart(self, tmp_path: Path):
        """反例: 重启不得让已消费的 flag 回到可审批"""
        ledger_path = tmp_path / "request_ledger.json"
        first = _adapter(self_id="10000")
        first.set_request_ledger(RequestApprovalLedger(ledger_path))
        first._bot.set_group_add_request.return_value = {}
        await first._handle_request({"self_id": 10000, "request_type": "group", "sub_type": "add",
                                     "group_id": 456, "user_id": 77, "flag": "g-restart"})
        await first.admin.handle_group_request("456", "g-restart", "add", True)
        assert first._bot.set_group_add_request.await_count == 1

        revived = _adapter(self_id="10000")
        revived.set_request_ledger(RequestApprovalLedger(ledger_path))
        with pytest.raises(AdminActionRejected, match="已被处理"):
            await revived.admin.handle_group_request("456", "g-restart", "add", True)
        revived._bot.set_group_add_request.assert_not_called()

    @pytest.mark.asyncio
    async def test_degraded_ledger_refuses_approval_without_action(self, tmp_path: Path):
        """反例: 账本降级时审批拒绝, 不得凭内存缓存放行动作"""
        ledger_path = tmp_path / "request_ledger.json"
        adapter = _adapter(self_id="10000")
        adapter.set_request_ledger(RequestApprovalLedger(ledger_path))
        adapter._bot.set_group_add_request.return_value = {}
        await adapter._handle_request({"self_id": 10000, "request_type": "group", "sub_type": "add",
                                       "group_id": 456, "user_id": 77, "flag": "g-degrade"})
        ledger_path.write_text("{ 不是 JSON", encoding="utf-8")
        degraded = RequestApprovalLedger(ledger_path)
        assert degraded.degraded is True
        adapter.set_request_ledger(degraded)
        with pytest.raises(AdminActionRejected, match="不可用"):
            await adapter.admin.handle_group_request("456", "g-degrade", "add", True)
        adapter._bot.set_group_add_request.assert_not_called()


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
        await _occupy_flag(adapter, "group", "g-flag", group_id="456", sub_type="add")
        await _occupy_flag(adapter, "friend", "f-flag")
        assert emit.await_count == 2

    @pytest.mark.asyncio
    async def test_foreign_or_missing_account_not_registered(self, monkeypatch: pytest.MonkeyPatch):
        adapter = _adapter(self_id="10")
        monkeypatch.setattr(adapter, "_emit_notice", AsyncMock())
        await adapter._handle_request({"self_id": 999, "request_type": "group", "sub_type": "add",
                                       "group_id": 456, "user_id": 77, "flag": "alien"})
        await adapter._handle_request({"request_type": "friend", "user_id": 88, "flag": "no-self"})
        with pytest.raises(LookupError):
            await _occupy_flag(adapter, "group", "alien", group_id="456", sub_type="add")
        with pytest.raises(LookupError):
            await _occupy_flag(adapter, "friend", "no-self")
