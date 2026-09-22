"""
OneBot 群管理与请求审批动作封装

每个动作统一参数校验, 群范围检查, 超时与错误归一;
只负责协议 I/O 和边界收窄, 不决定调用者权限
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, cast
import asyncio
import base64
import binascii


class PlatformAdminError(Exception):
    """管理动作失败的基类, message 为不含平台响应正文的用户可读说明"""


class UnsupportedAdminAction(PlatformAdminError):
    """目标实现缺少对应动作接口"""


class AdminActionRejected(PlatformAdminError):
    """平台明确拒绝执行, 不自动重试"""


class AdminActionUnconfirmed(PlatformAdminError):
    """动作结果未知 (超时或传输异常), 不假定成功也不自动重试"""


ADMIN_TIMEOUT = 10
PTT_TEXT_TIMEOUT = 25
"""fetch_ptt_text 等待秒数, 覆盖 SnowLuma/NapCat 内部 20 秒转写等待"""
RECORD_OUT_FORMATS = frozenset({"mp3", "amr", "wma", "m4a", "spx", "ogg", "wav", "flac"})
"""get_record out_format 允许值, 与 NapCat/SnowLuma 一致"""
"""单个管理动作含等待的最长秒数"""

ADMIN_CAPABILITIES: dict[str, tuple[str, str]] = {
    "get_group_list": ("read", "获取机器人所在群列表"),
    "get_group_info": ("read", "获取群信息"),
    "get_group_member_list": ("read", "获取群成员列表"),
    "get_group_member_info": ("read", "获取群成员信息"),
    "get_group_honor_info": ("read", "获取群荣誉信息"),
    "get_record": ("read", "获取语音并由实现服务端转码 (get_record out_format)"),
    "fetch_ptt_text": ("read", "QQ 原生语音转文字 (fetch_ptt_text)"),
    "recall_message": ("write", "撤回消息 (delete_msg)"),
    "kick_group_member": ("write", "移出群成员"),
    "ban_group_member": ("write", "禁言或解除禁言群成员"),
    "set_group_whole_ban": ("write", "全员禁言开关"),
    "ban_anonymous": ("write", "禁言匿名成员"),
    "set_group_admin": ("write", "设置或取消群管理员"),
    "set_group_anonymous": ("write", "群匿名开关"),
    "set_group_card": ("write", "设置群名片"),
    "set_group_name": ("write", "修改群名"),
    "set_group_special_title": ("write", "设置专属头衔"),
    "leave_group": ("write", "退出或解散群"),
    "handle_friend_request": ("write", "批准或拒绝好友请求"),
    "handle_group_request": ("write", "批准或拒绝加群请求/邀请"),
}
"""OneBot v11 标准管理动作登记表: 名称到读写属性与说明"""


MISSING_ACTION_RETCODES = frozenset({10002, 1404})
"""OneBot 实现未提供该动作时常见的 retcode (go-cqhttp 1404 / Lagrange 10002)"""


def is_missing_action_error(error: BaseException) -> bool:
    """
    判断动作失败是否因为当前实现不提供该接口

    参数:
    - error: 平台客户端抛出的动作失败异常

    返回:
    - bool: retcode 属于缺失动作集合时为 True
    """
    raw_result = getattr(error, "result", None)
    payload = cast(dict[str, Any], raw_result) if isinstance(raw_result, dict) else {}
    return payload.get("retcode") in MISSING_ACTION_RETCODES


def _normalize_decimal(value: Any, label: str) -> str:
    """
    校验并归一化纯数字平台 ID

    参数:
    - value: 外部输入
    - label: 错误文案中的对象名

    返回:
    - str: 纯数字字符串
    """
    text = str(value).strip()
    if not text or not text.isdecimal():
        raise ValueError(f"{label} 必须为纯数字字符串")
    return text


def normalize_group_id(value: Any) -> str:
    """校验并归一化群 ID"""
    return _normalize_decimal(value, "群 ID")


def normalize_user_id(value: Any) -> str:
    """校验并归一化用户 ID"""
    return _normalize_decimal(value, "用户 ID")


def normalize_flag(value: Any) -> str:
    """
    校验请求审批 flag

    参数:
    - value: request 事件上报的 flag

    返回:
    - str: 非空且不含空白字符的 flag
    """
    text = str(value).strip()
    if not text or any(ch.isspace() for ch in text):
        raise ValueError("请求标识 flag 非法")
    return text


class OneBotAdmin:
    """
    OneBot v11 群管理动作集

    通过适配器持有的 aiocqhttp 客户端调用动作接口,
    群动作在执行前检查实例群白名单; ActionFailed 按 retcode 区分
    接口缺失 (10002/1404) 与业务拒绝, 不读取响应正文作为错误细节
    """

    def __init__(self, adapter: Any, action_failures: tuple[type[Exception], ...] = ()) -> None:
        """
        初始化动作集

        参数:
        - adapter: OneBotAdapter 实例
        - action_failures: 视为平台明确拒绝的异常类型 (如 aiocqhttp ActionFailed)
        """
        self._adapter = adapter
        self._action_failures = action_failures

    async def _call(self, action: str, timeout: float = ADMIN_TIMEOUT, **params: Any) -> Any:
        """
        统一执行动作并归一化错误

        参数:
        - action: aiocqhttp 动作方法名
        - timeout: 最长等待秒数
        - params: 动作参数

        返回:
        - Any: 动作返回的数据字段, 无数据返回空字典
        """
        bot = self._adapter.get_client()
        if bot is None:
            raise AdminActionUnconfirmed("平台客户端未连接")
        method = getattr(bot, action, None)
        if not callable(method):
            raise UnsupportedAdminAction(f"当前实现不支持动作 {action}")
        call = cast(Callable[..., Awaitable[Any]], method)
        try:
            return await asyncio.wait_for(call(**params), timeout)
        except asyncio.TimeoutError as error:
            raise AdminActionUnconfirmed(f"动作 {action} 超时, 结果未知") from error
        except PlatformAdminError:
            raise
        except Exception as error:
            if self._action_failures and isinstance(error, self._action_failures):
                if is_missing_action_error(error):
                    raise UnsupportedAdminAction(f"当前实现不支持动作 {action}") from error
                raw_result = getattr(error, "result", None)
                retcode = cast(dict[str, Any], raw_result).get("retcode") if isinstance(raw_result, dict) else None
                raise AdminActionRejected(f"动作 {action} 被平台拒绝 (retcode={retcode})") from error
            raise AdminActionUnconfirmed(f"动作 {action} 结果未知: {type(error).__name__}") from error

    def _check_group(self, group_id: str) -> None:
        """
        校验目标群在当前实例允许范围内

        参数:
        - group_id: 已归一化群 ID
        """
        if not self._adapter.allows_group(group_id):
            raise AdminActionRejected("目标群不在当前实例允许范围内")

    async def get_group_list(self) -> list[dict[str, Any]]:
        """
        获取机器人所在群列表

        返回:
        - list[dict]: 收窄后的群信息 (group_id/group_name/member_count/max_member_count)
        """
        result = await self._call("get_group_list")
        if not isinstance(result, list):
            raise AdminActionUnconfirmed("群列表响应格式不符")
        groups: list[dict[str, Any]] = []
        for item in cast(list[Any], result)[:512]:
            if not isinstance(item, dict):
                continue
            data = cast(dict[str, Any], item)
            groups.append({key: data.get(key) for key in ("group_id", "group_name", "member_count", "max_member_count")})
        return groups

    async def get_group_info(self, group_id: Any) -> dict[str, Any]:
        """
        获取群信息

        参数:
        - group_id: 目标群

        返回:
        - dict: 收窄后的群信息
        """
        gid = normalize_group_id(group_id)
        self._check_group(gid)
        result = await self._call("get_group_info", group_id=int(gid))
        if not isinstance(result, dict):
            raise AdminActionUnconfirmed("群信息响应格式不符")
        data = cast(dict[str, Any], result)
        return {key: data.get(key) for key in ("group_id", "group_name", "member_count", "max_member_count", "group_create_time", "group_level")}

    async def get_group_member_list(self, group_id: Any) -> list[dict[str, Any]]:
        """
        获取群成员列表

        参数:
        - group_id: 目标群

        返回:
        - list[dict]: 收窄后的成员信息, 至多 2048 条
        """
        gid = normalize_group_id(group_id)
        self._check_group(gid)
        result = await self._call("get_group_member_list", group_id=int(gid))
        if not isinstance(result, list):
            raise AdminActionUnconfirmed("成员列表响应格式不符")
        members: list[dict[str, Any]] = []
        for item in cast(list[Any], result)[:2048]:
            if not isinstance(item, dict):
                continue
            data = cast(dict[str, Any], item)
            members.append({key: data.get(key) for key in (
                "user_id", "nickname", "card", "role", "join_time", "last_sent_time",
                "title", "level", "sex", "age", "area", "unfriendly", "card_changeable",
            )})
        return members

    async def get_group_member_info(self, group_id: Any, user_id: Any) -> dict[str, Any]:
        """
        获取群成员信息

        参数:
        - group_id: 目标群
        - user_id: 目标成员

        返回:
        - dict: 收窄后的成员信息
        """
        gid, uid = normalize_group_id(group_id), normalize_user_id(user_id)
        self._check_group(gid)
        result = await self._call("get_group_member_info", group_id=int(gid), user_id=int(uid))
        if not isinstance(result, dict):
            raise AdminActionUnconfirmed("成员信息响应格式不符")
        data = cast(dict[str, Any], result)
        return {key: data.get(key) for key in (
            "group_id", "user_id", "nickname", "card", "role", "join_time",
            "last_sent_time", "title", "level", "sex", "shut_up_timestamp",
        )}

    async def get_record(self, file: str, out_format: str, max_bytes: int) -> bytes:
        """
        请求实现把缓存语音转码为指定格式并返回字节

        参数:
        - file: 上报 record 段的 file 或 url 字段
        - out_format: 目标格式, 见 RECORD_OUT_FORMATS
        - max_bytes: 解码后允许的最大字节数

        返回:
        - bytes: 转码结果; 实现不支持该动作时抛 UnsupportedAdminAction
        """
        if out_format not in RECORD_OUT_FORMATS:
            raise ValueError("out_format 不在支持范围")
        source = str(file).strip()
        if not source:
            raise ValueError("语音标识不能为空")
        result = await self._call("get_record", timeout=ADMIN_TIMEOUT * 3, file=source, out_format=out_format)
        payload = cast(dict[str, Any], result) if isinstance(result, dict) else {}
        encoded = payload.get("base64")
        if not isinstance(encoded, str) or not encoded:
            raise UnsupportedAdminAction("实现未返回转码后的 base64 内容")
        if len(encoded) > max_bytes * 4 // 3 + 4:
            raise AdminActionRejected("转码后语音超过大小上限")
        try:
            data = base64.b64decode(encoded, validate=True)
        except binascii.Error as error:
            raise AdminActionUnconfirmed("转码结果不是合法 base64") from error
        if len(data) > max_bytes:
            raise AdminActionRejected("转码后语音超过大小上限")
        return data

    async def fetch_ptt_text(self, message_id: Any) -> str:
        """
        用实现提供的原生语音转文字获取转写

        参数:
        - message_id: 含语音的平台消息 ID

        返回:
        - str: 转写文本, 空字符串表示实现返回空结果
        """
        text = str(message_id).strip()
        if not text or not text.lstrip("-").isdecimal():
            raise ValueError("消息 ID 必须为整数")
        result = await self._call("fetch_ptt_text", timeout=PTT_TEXT_TIMEOUT, message_id=int(text))
        payload = cast(dict[str, Any], result) if isinstance(result, dict) else {}
        value = payload.get("text", "")
        return value if isinstance(value, str) else ""

    async def get_group_honor_info(self, group_id: Any, honor_type: str = "all") -> dict[str, Any]:
        """
        获取群荣誉信息

        参数:
        - group_id: 目标群
        - honor_type: talkative/performer/legend/strong_newbie/emotion 或 all

        返回:
        - dict: 实现返回的荣誉数据
        """
        gid = normalize_group_id(group_id)
        self._check_group(gid)
        if honor_type not in {"talkative", "performer", "legend", "strong_newbie", "emotion", "all"}:
            raise ValueError("荣誉类型非法")
        result = await self._call("get_group_honor_info", group_id=int(gid), type=honor_type)
        if not isinstance(result, dict):
            raise AdminActionUnconfirmed("荣誉信息响应格式不符")
        return cast(dict[str, Any], result)

    async def recall_message(self, group_id: Any, message_id: Any) -> None:
        """
        撤回一条群消息

        参数:
        - group_id: 消息所在群, 必须在当前实例允许范围内
        - message_id: 平台消息 ID
        """
        self._check_group(normalize_group_id(group_id))
        text = str(message_id).strip()
        if not text or not text.lstrip("-").isdecimal():
            raise ValueError("消息 ID 必须为整数")
        await self._call("delete_msg", message_id=int(text))

    async def kick_group_member(self, group_id: Any, user_id: Any, reject_add_request: bool = False) -> None:
        """
        移出群成员

        参数:
        - group_id: 目标群
        - user_id: 目标成员
        - reject_add_request: 是否拒绝其后续加群请求
        """
        gid, uid = normalize_group_id(group_id), normalize_user_id(user_id)
        self._check_group(gid)
        await self._call("set_group_kick", group_id=int(gid), user_id=int(uid), reject_add_request=bool(reject_add_request))

    async def ban_group_member(self, group_id: Any, user_id: Any, duration: Any = 1800) -> None:
        """
        禁言群成员, 时长 0 表示解除

        参数:
        - group_id: 目标群
        - user_id: 目标成员
        - duration: 禁言秒数, 0 到 2592000
        """
        gid, uid = normalize_group_id(group_id), normalize_user_id(user_id)
        self._check_group(gid)
        seconds = int(duration)
        if isinstance(duration, bool) or not 0 <= seconds <= 2592000:
            raise ValueError("禁言时长必须为 0 到 2592000 秒")
        await self._call("set_group_ban", group_id=int(gid), user_id=int(uid), duration=seconds)

    async def set_group_whole_ban(self, group_id: Any, enable: Any) -> None:
        """
        开启或解除全员禁言

        参数:
        - group_id: 目标群
        - enable: 是否开启
        """
        gid = normalize_group_id(group_id)
        self._check_group(gid)
        if not isinstance(enable, bool):
            raise ValueError("enable 必须为布尔值")
        await self._call("set_group_whole_ban", group_id=int(gid), enable=enable)

    async def ban_anonymous(self, group_id: Any, flag: Any, duration: Any = 1800) -> None:
        """
        禁言匿名成员

        参数:
        - group_id: 目标群
        - flag: 匿名消息上报的 anonymous flag
        - duration: 禁言秒数, 0 到 2592000
        """
        gid = normalize_group_id(group_id)
        self._check_group(gid)
        seconds = int(duration)
        if isinstance(duration, bool) or not 0 <= seconds <= 2592000:
            raise ValueError("禁言时长必须为 0 到 2592000 秒")
        await self._call("set_group_anonymous_ban", group_id=int(gid), flag=normalize_flag(flag), duration=seconds)

    async def set_group_admin(self, group_id: Any, user_id: Any, enable: Any) -> None:
        """
        设置或取消群管理员

        参数:
        - group_id: 目标群
        - user_id: 目标成员
        - enable: 是否设置为管理员
        """
        gid, uid = normalize_group_id(group_id), normalize_user_id(user_id)
        self._check_group(gid)
        if not isinstance(enable, bool):
            raise ValueError("enable 必须为布尔值")
        await self._call("set_group_admin", group_id=int(gid), user_id=int(uid), enable=enable)

    async def set_group_anonymous(self, group_id: Any, enable: Any) -> None:
        """
        开启或关闭群匿名

        参数:
        - group_id: 目标群
        - enable: 是否允许匿名
        """
        gid = normalize_group_id(group_id)
        self._check_group(gid)
        if not isinstance(enable, bool):
            raise ValueError("enable 必须为布尔值")
        await self._call("set_group_anonymous", group_id=int(gid), enable=enable)

    async def set_group_card(self, group_id: Any, user_id: Any, card: Any = "") -> None:
        """
        设置群成员名片

        参数:
        - group_id: 目标群
        - user_id: 目标成员
        - card: 新名片, 空字符串表示删除
        """
        gid, uid = normalize_group_id(group_id), normalize_user_id(user_id)
        self._check_group(gid)
        text = str(card)
        if len(text) > 60:
            raise ValueError("群名片长度不能超过 60 字符")
        await self._call("set_group_card", group_id=int(gid), user_id=int(uid), card=text)

    async def set_group_name(self, group_id: Any, name: Any) -> None:
        """
        修改群名

        参数:
        - group_id: 目标群
        - name: 新群名
        """
        gid = normalize_group_id(group_id)
        self._check_group(gid)
        text = str(name).strip()
        if not text or len(text) > 60:
            raise ValueError("群名必须为 1 到 60 字符")
        await self._call("set_group_name", group_id=int(gid), group_name=text)

    async def set_group_special_title(self, group_id: Any, user_id: Any, title: Any, duration: Any = -1) -> None:
        """
        设置群成员专属头衔

        参数:
        - group_id: 目标群
        - user_id: 目标成员
        - title: 头衔文本, 空字符串表示删除
        - duration: 有效期秒数, -1 表示永久
        """
        gid, uid = normalize_group_id(group_id), normalize_user_id(user_id)
        self._check_group(gid)
        text = str(title)
        if len(text) > 18:
            raise ValueError("专属头衔长度不能超过 18 字符")
        seconds = int(duration)
        if isinstance(duration, bool) or seconds != -1 and not 0 <= seconds <= 2592000:
            raise ValueError("头衔有效期必须为 -1 或 0 到 2592000 秒")
        await self._call("set_group_special_title", group_id=int(gid), user_id=int(uid), special_title=text, duration=seconds)

    async def leave_group(self, group_id: Any, dismiss: Any = False) -> None:
        """
        退出或解散群 (解散仅群主可用)

        参数:
        - group_id: 目标群
        - dismiss: 是否解散
        """
        gid = normalize_group_id(group_id)
        self._check_group(gid)
        if not isinstance(dismiss, bool):
            raise ValueError("dismiss 必须为布尔值")
        await self._call("set_group_leave", group_id=int(gid), is_dismiss=dismiss)

    async def handle_friend_request(self, flag: Any, approve: Any, remark: Any = "") -> None:
        """
        处理好友添加请求

        参数:
        - flag: request 事件上报的标识
        - approve: 是否同意
        - remark: 同意后的好友备注
        """
        if not isinstance(approve, bool):
            raise ValueError("approve 必须为布尔值")
        text = str(remark)
        if len(text) > 60:
            raise ValueError("备注长度不能超过 60 字符")
        await self._call("set_friend_add_request", flag=normalize_flag(flag), approve=approve, remark=text)

    async def handle_group_request(self, group_id: Any, flag: Any, sub_type: Any, approve: Any, reason: Any = "") -> None:
        """
        处理加群请求或邀请

        参数:
        - group_id: 请求所属群, 必须在当前实例允许范围内
        - flag: request 事件上报的标识
        - sub_type: add 或 invite, 必须与事件一致
        - approve: 是否同意
        - reason: 拒绝理由
        """
        self._check_group(normalize_group_id(group_id))
        if sub_type not in {"add", "invite"}:
            raise ValueError("sub_type 必须为 add 或 invite")
        if not isinstance(approve, bool):
            raise ValueError("approve 必须为布尔值")
        text = str(reason)
        if len(text) > 120:
            raise ValueError("理由长度不能超过 120 字符")
        await self._call("set_group_add_request", flag=normalize_flag(flag), sub_type=sub_type, approve=approve, reason=text)
