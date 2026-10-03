"""OneBot 机器人自身昵称和本群名片的回源及来源边界"""
from unittest.mock import AsyncMock
import asyncio

import pytest

from satrap.core.platform import PlatformConfig
from satrap.core.platform.identity import BotIdentity
from satrap.core.platform.onebot.adapter import OneBotAdapter


@pytest.fixture
def adapter() -> OneBotAdapter:
    """
    装配固定账号及只读动作替身

    返回:
    - OneBotAdapter: 账号 10 的适配器
    """
    result = OneBotAdapter(PlatformConfig(id="bot", type="onebot", settings={"self_id": "10"}))
    result._bot = AsyncMock()
    result._bot.get_login_info.return_value = {"user_id": 10, "nickname": "机器人昵称"}
    result._bot.get_group_member_info.return_value = {"user_id": 10, "group_id": 20, "nickname": "机器人昵称", "card": "群中名字"}
    return result


@pytest.mark.asyncio
async def test_self_nickname_and_card_are_verified_and_cached(adapter: OneBotAdapter):
    first, second = await asyncio.gather(adapter.resolve_self_identity("10", "20"), adapter.resolve_self_identity("10", "20"))
    assert first == second == BotIdentity("10", "机器人昵称", "20", "群中名字")
    adapter._bot.get_login_info.assert_awaited_once_with(self_id="10")
    adapter._bot.get_group_member_info.assert_awaited_once_with(self_id="10", group_id=20, user_id=10, no_cache=True)
    assert await adapter.resolve_self_identity("10") == BotIdentity("10", "机器人昵称")


@pytest.mark.asyncio
async def test_cards_are_group_specific_but_login_info_is_shared(adapter: OneBotAdapter):
    assert (await adapter.resolve_self_identity("10", "20")) == BotIdentity("10", "机器人昵称", "20", "群中名字")
    adapter._bot.get_group_member_info.return_value = {"user_id": 10, "group_id": 21, "card": "另一个群名片"}
    assert (await adapter.resolve_self_identity("10", "21")) == BotIdentity("10", "机器人昵称", "21", "另一个群名片")
    assert (await adapter.resolve_self_identity("10", "20")) == BotIdentity("10", "机器人昵称", "20", "群中名字")
    adapter._bot.get_login_info.assert_awaited_once()
    assert adapter._bot.get_group_member_info.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"user_id": 11, "nickname": "别人的名字"},
    {"nickname": "无法确认所属账号"},
    {"user_id": 10, "nickname": 123},
    {"user_id": 10, "nickname": " "},
    [],
])
async def test_invalid_account_info_cannot_be_used_as_bot_name(adapter: OneBotAdapter, payload: object):
    adapter._bot.get_login_info.return_value = payload
    assert await adapter.resolve_self_identity("10") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"user_id": 11, "group_id": 20, "card": "他人的名片"},
    {"user_id": 10, "group_id": 21, "card": "其他群的名片"},
    {"card": "缺少身份"},
    [],
])
async def test_invalid_group_info_falls_back_to_verified_account(adapter: OneBotAdapter, payload: object):
    adapter._bot.get_group_member_info.return_value = payload
    assert await adapter.resolve_self_identity("10", "20") == BotIdentity("10", "机器人昵称", "20")


@pytest.mark.asyncio
async def test_failed_login_can_fall_back_to_verified_group_nickname(adapter: OneBotAdapter):
    adapter._bot.get_login_info.side_effect = RuntimeError("unavailable")
    assert await adapter.resolve_self_identity("10", "20") == BotIdentity("10", "机器人昵称", "20", "群中名字")


@pytest.mark.asyncio
async def test_failed_queries_are_cached_and_refresh_after_ttl(adapter: OneBotAdapter, monkeypatch: pytest.MonkeyPatch):
    from satrap.core.platform.onebot import self_identity as module

    moment = [0.0]
    monkeypatch.setattr(module, "monotonic", lambda: moment[0])
    adapter._bot.get_login_info.return_value = {}
    adapter._bot.get_group_member_info.return_value = {}
    assert await adapter.resolve_self_identity("10", "20") is None
    assert await adapter.resolve_self_identity("10", "20") is None
    adapter._bot.get_login_info.assert_awaited_once()
    adapter._bot.get_group_member_info.assert_awaited_once()
    moment[0] = module.IDENTITY_TTL + 1
    adapter._bot.get_login_info.return_value = {"user_id": 10, "nickname": "改名后"}
    adapter._bot.get_group_member_info.return_value = {"user_id": 10, "group_id": 20, "card": "新名片"}
    assert await adapter.resolve_self_identity("10", "20") == BotIdentity("10", "改名后", "20", "新名片")


@pytest.mark.asyncio
async def test_reconnect_invalidates_identity_cache(adapter: OneBotAdapter):
    assert await adapter.resolve_self_identity("10") == BotIdentity("10", "机器人昵称")
    adapter._bot.get_login_info.return_value = {"user_id": 10, "nickname": "重连后名字"}
    await adapter._handle_meta({"self_id": 10, "meta_event_type": "lifecycle", "sub_type": "connect"})
    assert await adapter.resolve_self_identity("10") == BotIdentity("10", "重连后名字")
    assert adapter._bot.get_login_info.await_count == 2


@pytest.mark.asyncio
async def test_changed_client_during_lookup_discards_old_result(adapter: OneBotAdapter):
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed(**_kwargs: object) -> dict[str, object]:
        """等待新连接接替旧连接后返回旧资料"""
        entered.set()
        await release.wait()
        return {"user_id": 10, "nickname": "旧连接名字"}

    adapter._bot.get_login_info.side_effect = delayed
    task = asyncio.create_task(adapter.resolve_self_identity("10"))
    await entered.wait()
    adapter._bot = AsyncMock()
    adapter._bot.get_login_info.return_value = {"user_id": 10, "nickname": "新连接名字"}
    release.set()
    assert await task is None
    assert await adapter.resolve_self_identity("10") == BotIdentity("10", "新连接名字")


@pytest.mark.asyncio
async def test_timeout_keeps_available_nickname_and_does_not_repeat_query(adapter: OneBotAdapter, monkeypatch: pytest.MonkeyPatch):
    from satrap.core.platform.onebot import self_identity as module

    monkeypatch.setattr(module, "IDENTITY_ACTION_TIMEOUT", 0.01)

    async def blocked(**_kwargs: object) -> dict[str, object]:
        """模拟成员资料接口无响应"""
        await asyncio.Event().wait()
        return {}

    adapter._bot.get_group_member_info.side_effect = blocked
    assert await asyncio.wait_for(adapter.resolve_self_identity("10", "20"), 0.5) == BotIdentity("10", "机器人昵称", "20")
    assert await adapter.resolve_self_identity("10", "20") == BotIdentity("10", "机器人昵称", "20")
    adapter._bot.get_group_member_info.assert_awaited_once()


@pytest.mark.asyncio
async def test_other_account_or_disallowed_group_never_reads_identity(adapter: OneBotAdapter):
    adapter.config.settings["group_whitelist"] = ["20"]
    assert await adapter.resolve_self_identity("11", "20") is None
    assert await adapter.resolve_self_identity("10", "21") is None
    adapter._bot.get_login_info.assert_not_awaited()
    adapter._bot.get_group_member_info.assert_not_awaited()


@pytest.mark.asyncio
async def test_identity_caches_do_not_cross_adapter_instances(adapter: OneBotAdapter):
    other = OneBotAdapter(PlatformConfig(id="second", type="onebot", settings={"self_id": "10"}))
    other._bot = AsyncMock()
    other._bot.get_login_info.return_value = {"user_id": 10, "nickname": "另一个实例"}
    assert await adapter.resolve_self_identity("10") == BotIdentity("10", "机器人昵称")
    assert await other.resolve_self_identity("10") == BotIdentity("10", "另一个实例")
