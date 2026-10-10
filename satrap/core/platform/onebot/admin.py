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
import json
import re
import unicodedata

from satrap.core.platform.onebot.request_registry import flag_digest
from satrap.core.log import logger

from satrap.core.config.group_action_origin import current_group_action_preflight, bind_group_action_preflight, bind_group_request_occupancy


class PlatformAdminError(Exception):
    """管理动作失败的基类, message 为不含平台响应正文的用户可读说明"""


class UnsupportedAdminAction(PlatformAdminError):
    """目标实现缺少对应动作接口"""


class AdminActionRejected(PlatformAdminError):
    """平台明确拒绝执行, 不自动重试"""

    def __init__(self, message: str, *, retcode: int | None = None) -> None:
        """
        保留平台错误码, 不携带平台响应正文

        参数:
        - message: 宿主生成的错误说明
        - retcode: 平台返回的整数错误码, 缺少或格式无效时为 None
        """
        super().__init__(message)
        self.retcode = retcode if type(retcode) is int and -(2 ** 31) <= retcode < 2 ** 31 else None


class AdminActionUnconfirmed(PlatformAdminError):
    """动作结果未知 (超时或传输异常), 不假定成功也不自动重试"""


ADMIN_TIMEOUT = 10
PTT_TEXT_TIMEOUT = 25
"""fetch_ptt_text 等待秒数, 覆盖 SnowLuma/NapCat 内部 20 秒转写等待"""
RECORD_OUT_FORMATS = frozenset({"mp3", "amr", "wma", "m4a", "spx", "ogg", "wav", "flac"})
"""get_record out_format 允许值, 与 NapCat/SnowLuma 一致"""
GROUP_DIRECTORY_LIMIT = 10000
"""一次群目录同步允许确认的最多条目数"""
GROUP_DIRECTORY_BYTES = 4 * 1024 * 1024
"""一次群目录响应允许确认的最大 UTF-8 JSON 字节数"""
"""单个管理动作含等待的最长秒数"""

WRITE_ACTIONS = frozenset({
    "delete_msg", "set_group_kick", "set_group_ban", "set_group_whole_ban", "set_group_anonymous_ban", "set_group_admin",
    "set_group_anonymous", "set_group_card", "set_group_name", "set_group_special_title", "set_group_leave",
    "set_friend_add_request", "set_group_add_request", "delete_friend",
})
"""会改变平台状态的 OneBot 动作名, 执行成功记审计日志"""

APPROVAL_FLAG_KINDS: dict[str, str] = {
    "set_friend_add_request": "friend",
    "set_group_add_request": "group",
}
"""审批动作到账本 flag 域的映射, 供审计日志用同域摘要替代原始标识"""

ADMIN_CAPABILITIES: dict[str, tuple[str, str]] = {
    "get_friend_list": ("read", "获取好友列表"),
    "delete_friend": ("write", "删除好友"),
    "get_group_list": ("read", "获取机器人所在群列表"),
    "get_group_info": ("read", "获取群信息"),
    "get_group_member_list": ("read", "获取群成员列表"),
    "get_group_member_info": ("read", "获取群成员信息"),
    "get_group_honor_info": ("read", "获取群荣誉信息"),
    "get_record": ("read", "获取语音并由实现服务端转码 (get_record out_format)"),
    "get_image": ("read", "获取图片信息并刷新已过期的上报地址 (get_image)"),
    "fetch_ptt_text": ("read", "QQ 原生语音转文字 (fetch_ptt_text)"),
    "recall_message": ("write", "撤回消息 (delete_msg)"),
    "kick_group_member": ("write", "移出群成员"),
    "ban_group_member": ("write", "禁言或解除禁言群成员"),
    "set_group_whole_ban": ("write", "全员禁言开关"),
    "ban_anonymous": ("write", "禁言匿名成员"),
    "set_group_admin": ("write", "设置或取消群管理员"),
    "set_group_anonymous": ("write", "群匿名开关"),
    "set_group_card": ("write", "设置本群昵称"),
    "set_group_name": ("write", "修改群名"),
    "set_group_special_title": ("write", "设置专属头衔"),
    "leave_group": ("write", "退出或解散群"),
    "handle_friend_request": ("write", "批准或拒绝好友请求"),
    "handle_group_request": ("write", "批准或拒绝加群请求/邀请"),
    "get_message": ("read", "回源读取群消息 (get_msg)"),
    "upload_file": ("write", "上传群/私聊文件 (upload_group_file/upload_private_file)"),
}
"""OneBot v11 标准管理动作登记表: 名称到读写属性与说明"""

_CAPABILITY_ACTIONS: dict[str, tuple[str, ...]] = {
    "get_friend_list": ("get_friend_list",),
    "delete_friend": ("delete_friend",),
    "get_group_list": ("get_group_list",),
    "get_group_info": ("get_group_info",),
    "get_group_member_list": ("get_group_member_list",),
    "get_group_member_info": ("get_group_member_info",),
    "get_group_honor_info": ("get_group_honor_info",),
    "get_record": ("get_record",),
    "get_image": ("get_image",),
    "fetch_ptt_text": ("fetch_ptt_text",),
    "recall_message": ("delete_msg",),
    "kick_group_member": ("set_group_kick",),
    "ban_group_member": ("set_group_ban",),
    "set_group_whole_ban": ("set_group_whole_ban",),
    "ban_anonymous": ("set_group_anonymous_ban",),
    "set_group_admin": ("set_group_admin",),
    "set_group_anonymous": ("set_group_anonymous",),
    "set_group_card": ("set_group_card",),
    "set_group_name": ("set_group_name",),
    "set_group_special_title": ("set_group_special_title",),
    "leave_group": ("set_group_leave",),
    "handle_friend_request": ("set_friend_add_request",),
    "handle_group_request": ("set_group_add_request",),
    "get_message": ("get_msg",),
    "upload_file": ("upload_group_file", "upload_private_file"),
}
"""能力登记名到底层协议动作名的映射, 供按连接代次的被动能力学习查询"""


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


def _normalize_message_id(value: Any, message: str = "消息 ID 必须为整数") -> str:
    """
    校验并归一化平台消息 ID

    参数:
    - value: 外部输入
    - message: 错误文案

    返回:
    - str: 允许负号的十进制字符串 (群聊消息 ID 可为负数)
    """
    text = str(value).strip()
    if not text or not text.lstrip("-").isdecimal():
        raise ValueError(message)
    return text




def _normalize_duration(value: Any, message: str, *, allow_minus_one: bool = False) -> int:
    """
    校验并归一化秒数时长, 类型校验先于 int() 转换

    参数:
    - value: 外部输入
    - message: 错误文案
    - allow_minus_one: 是否接受 -1 表示永久

    返回:
    - int: 合法秒数
    """
    if isinstance(value, bool):
        raise ValueError(message)
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        raise ValueError(message) from None
    if allow_minus_one and seconds == -1:
        return seconds
    if not 0 <= seconds <= 2592000:
        raise ValueError(message)
    return seconds


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
            self._adapter.note_action_outcome(action, False)
            raise UnsupportedAdminAction(f"当前实现不支持动作 {action}")
        call = cast(Callable[..., Awaitable[Any]], method)
        try:
            async def guarded_call() -> Any:
                """在平台写调用的同一协程中完成最后一次权限复核"""

                preflight = current_group_action_preflight()
                if preflight is not None:
                    preflight()
                return await call(**params)

            result = await asyncio.wait_for(guarded_call(), timeout)
        except asyncio.TimeoutError as error:
            logger.warning(f"[OneBotAdmin] {action} 超时 ({timeout}s), 结果未知")
            raise AdminActionUnconfirmed(f"动作 {action} 超时, 结果未知") from error
        except PlatformAdminError:
            raise
        except PermissionError:
            raise
        except Exception as error:
            if self._action_failures and isinstance(error, self._action_failures):
                if is_missing_action_error(error):
                    self._adapter.note_action_outcome(action, False)
                    logger.debug(f"[OneBotAdmin] 当前实现不支持动作 {action}")
                    raise UnsupportedAdminAction(f"当前实现不支持动作 {action}") from error
                raw_result = getattr(error, "result", None)
                raw_retcode = raw_result.get("retcode") if isinstance(raw_result, dict) else None
                retcode = raw_retcode if type(raw_retcode) is int and -(2 ** 31) <= raw_retcode < 2 ** 31 else None
                logger.warning(f"[OneBotAdmin] {action} 被平台拒绝 retcode={retcode}")
                raise AdminActionRejected(f"动作 {action} 被平台拒绝 (retcode={retcode})", retcode=retcode) from error
            logger.warning(f"[OneBotAdmin] {action} 结果未知: {type(error).__name__}")
            raise AdminActionUnconfirmed(f"动作 {action} 结果未知: {type(error).__name__}") from error
        self._adapter.note_action_outcome(action, True)
        if action in WRITE_ACTIONS:
            targets = {key: value for key, value in params.items() if key in {"group_id", "user_id", "message_id"}}
            approval_kind = APPROVAL_FLAG_KINDS.get(action)
            if approval_kind is not None and "flag" in params:
                # 原始 flag 可重放审批, 不落日志; 只记与账本同域的摘要便于对照条目
                targets["flag_digest"] = flag_digest(approval_kind, self._adapter.bot_self_id, str(params["flag"]))[:8]
            logger.info(
                f"[OneBotAdmin] 写动作已执行 {action} adapter={self._adapter.config.id} "
                f"self_id={self._adapter.bot_self_id} {targets}"
            )
        return result

    def _check_group(self, group_id: str) -> None:
        """
        校验目标群在当前实例允许范围内

        参数:
        - group_id: 已归一化群 ID
        """
        if not self._adapter.allows_management_target(group_id):
            if self._adapter._group_access_store is None:
                raise AdminActionRejected("目标群不在当前实例允许范围内")
            raise AdminActionRejected("目标群成员关系未确认或已离开")

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

    async def fetch_group_directory(self) -> dict[str, Any]:
        """
        获取带完整性证据的群目录, 截断或坏条目不能用于退群判定

        返回:
        - items 为可信群条目; complete 仅在全部条目合法且未超限时为 True
        """
        result = await self._call("get_group_list")
        if not isinstance(result, list):
            raise AdminActionUnconfirmed("群目录响应格式不符")
        size = len(json.dumps(result, ensure_ascii=False, default=str).encode("utf-8"))
        over_limit = len(result) > GROUP_DIRECTORY_LIMIT or size > GROUP_DIRECTORY_BYTES
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        invalid = False
        for raw in result[:GROUP_DIRECTORY_LIMIT]:
            if not isinstance(raw, dict):
                invalid = True
                continue
            data = cast(dict[str, Any], raw)
            group_id = str(data.get("group_id") or "")
            if not re.fullmatch(r"[1-9][0-9]*", group_id) or group_id in seen:
                invalid = True
                continue
            name = data.get("group_name")
            if name is not None and (not isinstance(name, str) or len(name) > 200):
                invalid = True
                continue
            counts: dict[str, int | None] = {}
            for key in ("member_count", "max_member_count"):
                count = data.get(key)
                if count is None:
                    counts[key] = None
                elif isinstance(count, int) and not isinstance(count, bool) and count >= 0:
                    counts[key] = count
                else:
                    invalid = True
                    counts[key] = None
            seen.add(group_id)
            items.append({"group_id": group_id, "group_name": name, **counts})
        truncated = over_limit or invalid
        reason = "limit_exceeded" if over_limit else "invalid_entries" if invalid else None
        return {
            "items": items, "complete": not truncated, "truncated": truncated,
            "reason": reason, "raw_count": len(result),
        }

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

    async def get_image(self, file: str) -> dict[str, Any]:
        """
        回源读取实现缓存的图片信息, 用于刷新已过期的上报地址

        参数:
        - file: 上报 image 段的 file 或 url 字段

        返回:
        - dict[str, Any]: file, url 与 file_name; url 是重新解析后的可下载地址;
          实现不支持该动作时抛 UnsupportedAdminAction, 图片不在实现缓存时抛 AdminActionRejected
        """
        source = str(file).strip()
        if not source:
            raise ValueError("图片标识不能为空")
        if len(source) > 512 or any(unicodedata.category(ch) == "Cc" for ch in source):
            # 合法值是实现自定义的文件 ID 或 URL, 不做路径语义假设, 只挡控制字符与异常长度
            raise ValueError("图片标识超长或含控制字符")
        result = await self._call("get_image", timeout=ADMIN_TIMEOUT, file=source)
        payload = cast(dict[str, Any], result) if isinstance(result, dict) else {}
        url = str(payload.get("url") or "")
        if not url:
            # 实现返回空地址说明图片已不在其缓存中, 无法恢复
            raise AdminActionRejected("实现未返回可下载的图片地址")
        return {
            "file": str(payload.get("file") or source),
            "url": url,
            "file_name": str(payload.get("file_name") or ""),
        }

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
        if len(source) > 512 or any(unicodedata.category(ch) == "Cc" for ch in source):
            # 合法值是实现自定义的文件 ID 或 URL, 不做路径语义假设, 只挡控制字符与异常长度
            raise ValueError("语音标识超长或含控制字符")
        result = await self._call("get_record", timeout=ADMIN_TIMEOUT * 3, file=source, out_format=out_format)
        payload = cast(dict[str, Any], result) if isinstance(result, dict) else {}
        encoded = payload.get("base64")
        if not isinstance(encoded, str) or not encoded:
            raise UnsupportedAdminAction("实现未返回转码后的 base64 内容")
        if len(encoded) > max_bytes * 4 // 3 + 4:
            raise AdminActionRejected("转码后语音超过大小上限")
        try:
            # 大体积解码是 CPU 密集操作, 移出事件循环
            data = await asyncio.to_thread(base64.b64decode, encoded, validate=True)
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

    async def _verify_group_message(self, gid: str, message_id: Any, *, rejection: str) -> tuple[int, dict[str, Any]]:
        """
        回源并确认消息实际属于目标群

        参数:
        - gid: 已归一化且已通过群范围检查的目标群
        - message_id: 平台消息 ID
        - rejection: 归属无法确认时的拒绝文案

        返回:
        - tuple[int, dict]: 整数消息 ID 与 get_msg 响应; 无法确认归属时抛 AdminActionRejected
        """
        text = _normalize_message_id(message_id)
        info = await self._call("get_msg", message_id=int(text))
        payload = cast(dict[str, Any], info) if isinstance(info, dict) else {}
        raw_group = payload.get("group_id")
        actual_group = str(raw_group).strip() if isinstance(raw_group, (int, str)) and not isinstance(raw_group, bool) else ""
        if payload.get("message_type") != "group" or actual_group != gid:
            raise AdminActionRejected(rejection)
        return int(text), payload

    async def get_message(self, group_id: Any, message_id: Any) -> dict[str, Any]:
        """
        回源读取一条群消息, 收窄字段后返回

        参数:
        - group_id: 消息所在群, 必须在当前实例允许范围内
        - message_id: 平台消息 ID

        返回:
        - dict: 消息 ID, 时间, 发送者与原文; 归属无法确认或超界一律拒绝, 防借读工具跨群读消息
        """
        gid = normalize_group_id(group_id)
        self._check_group(gid)
        _, payload = await self._verify_group_message(gid, message_id, rejection="无法确认消息属于目标群, 已拒绝读取")
        self._check_group(gid)
        if len(json.dumps(payload, ensure_ascii=False, default=str)) > 65536:
            raise AdminActionUnconfirmed("消息回源响应超过大小上限")
        sender = payload.get("sender")
        sender_data = cast(dict[str, Any], sender) if isinstance(sender, dict) else {}
        return {
            "message_id": payload.get("message_id"),
            "time": payload.get("time"),
            "message_type": payload.get("message_type"),
            "group_id": payload.get("group_id"),
            "sender": {key: sender_data.get(key) for key in ("user_id", "nickname", "card", "role")},
            "message": payload.get("message"),
        }




    async def recall_message(self, group_id: Any, message_id: Any) -> None:
        """
        撤回一条群消息

        参数:
        - group_id: 消息所在群, 必须在当前实例允许范围内
        - message_id: 平台消息 ID

        执行前经 get_msg 回源确认消息实际属于目标群, 无法确认一律拒绝;
        回源通过后复查群范围, 防止回源等待期间配置已被修改
        """
        gid = normalize_group_id(group_id)
        self._check_group(gid)
        message_int, _ = await self._verify_group_message(gid, message_id, rejection="无法确认消息属于目标群, 已拒绝撤回")
        self._check_group(gid)
        await self._call("delete_msg", message_id=message_int)

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
        seconds = _normalize_duration(duration, "禁言时长必须为 0 到 2592000 秒")
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
        seconds = _normalize_duration(duration, "禁言时长必须为 0 到 2592000 秒")
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
        设置成员在本群的昵称

        参数:
        - group_id: 目标群
        - user_id: 目标成员
        - card: 新的本群昵称, 空字符串表示删除
        """
        gid, uid = normalize_group_id(group_id), normalize_user_id(user_id)
        self._check_group(gid)
        text = str(card)
        if len(text) > 60:
            raise ValueError("本群昵称长度不能超过 60 字符")
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
        seconds = _normalize_duration(duration, "头衔有效期必须为 -1 或 0 到 2592000 秒", allow_minus_one=True)
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

    async def _settle_request_flag(self, kind: str, flag: str, self_id: str, state: str,
                                   decision: str | None = None, *, identity_digest: str | None = None) -> None:
        """
        回写审批 flag 终态, 落盘失败只告警

        参数:
        - kind: group 或 friend
        - flag: 已占用的请求标识
        - self_id: 已绑定机器人账号
        - state: completed 或 unknown
        - decision: 成功发送的审批决定, 失败时为 None
        - identity_digest: 实际占用的申请身份, None 沿用旧版凭据身份

        写失败时标识保持 executing: 不可重放优先于终态精确
        """
        try:
            await self._adapter.request_flags.settle(kind, flag, self_id=self_id, state=state, decision=decision,
                                                     identity_digest=identity_digest)
        except Exception as error:
            logger.error(f"[OneBotAdmin] 审批终态写入失败 kind={kind} state={state}: {type(error).__name__}: {error}")

    async def _execute_request_decision(
        self, kind: str, flag: str, self_id: str, action: str, params: dict[str, Any], *, identity_digest: str | None = None,
    ) -> None:
        """
        执行审批动作并按异常分类结算 flag 终态

        参数:
        - kind: group 或 friend, 决定账本域
        - flag: 已占用的请求标识
        - self_id: 已绑定机器人账号
        - action: 审批动作名
        - params: 动作参数, 经 _call 原样交给平台实现
        - identity_digest: 固定的一次好友申请身份, 不作为平台参数发送

        超时或取消记 unknown, 平台给出明确结果记 completed; 终态写失败时标识保持
        executing, 不可重放优先于终态精确
        """
        try:
            if kind == "friend" and identity_digest is not None:
                previous = current_group_action_preflight()

                def verify_request() -> None:
                    """保留宿主权限复核, 并拒绝排队期间被新申请替代的旧申请"""
                    if previous is not None:
                        previous()
                    self._adapter.request_flags.ledger.verify_occupied_friend(
                        self._adapter.config.id, self_id, flag, identity_digest,
                    )

                with bind_group_action_preflight(verify_request):
                    await self._call(action, **params)
            else:
                await self._call(action, **params)
        except AdminActionUnconfirmed:
            await self._settle_request_flag(kind, flag, self_id, "unknown", identity_digest=identity_digest)
            raise
        except Exception:
            await self._settle_request_flag(kind, flag, self_id, "completed", identity_digest=identity_digest)
            raise
        except BaseException:
            # 取消等 BaseException 路径保守结束为 unknown, 不遗漏
            await self._settle_request_flag(kind, flag, self_id, "unknown", identity_digest=identity_digest)
            raise
        await self._settle_request_flag(kind, flag, self_id, "completed", "accepted" if params.get("approve") else "rejected",
                                        identity_digest=identity_digest)

    async def handle_friend_request(self, flag: Any, approve: Any, remark: Any = "", *, allow_archived: bool = False,
                                    identity_digest: str | None = None) -> None:
        """
        处理好友添加请求

        参数:
        - flag: request 事件上报的标识, 必须已在好友请求账本中且未被占用
        - approve: 是否同意
        - remark: 同意后的好友备注
        - allow_archived: 是否已取得归档申请的人工确认
        - identity_digest: 宿主收件箱固定的一次申请身份, None 仅处理旧版申请

        flag 校验与持久占用在首次网络等待前完成; 动作超时, 取消或传输异常记 unknown,
        不自动重试, 同一次申请不可重放
        """
        if not isinstance(approve, bool):
            raise ValueError("approve 必须为布尔值")
        text = str(remark)
        if len(text) > 60:
            raise ValueError("备注长度不能超过 60 字符")
        normalized = normalize_flag(flag)
        registry = self._adapter.request_flags
        self_id = self._adapter.bot_self_id
        try:
            await registry.occupy("friend", normalized, self_id=self_id, allow_archived=allow_archived, identity_digest=identity_digest)
        except LookupError as error:
            raise AdminActionRejected(str(error)) from None
        await self._execute_request_decision(
            "friend", normalized, self_id,
            "set_friend_add_request", {"flag": normalized, "approve": approve, "remark": text},
            identity_digest=identity_digest,
        )

    async def handle_group_request(self, group_id: Any, flag: Any, sub_type: Any, approve: Any, reason: Any = "", *,
                                   request_id: str | None = None, expected_revision: int | None = None, allow_archived: bool = False) -> None:
        """
        处理加群请求或邀请

        参数:
        - group_id: 请求所属群, 必须在当前实例允许范围内且与登记一致
        - flag: request 事件上报的标识, 必须已在群请求账本中且未被占用
        - sub_type: add 或 invite, 必须与登记事件一致
        - approve: 是否同意
        - reason: 拒绝理由

        flag 校验与持久占用在首次网络等待前完成; 动作超时, 取消或传输异常记 unknown,
        不自动重试, 同一 flag 不可重放
        """
        gid = normalize_group_id(group_id)
        if sub_type not in {"add", "invite"}:
            raise ValueError("sub_type 必须为 add 或 invite")
        if not isinstance(approve, bool):
            raise ValueError("approve 必须为布尔值")
        text = str(reason)
        if len(text) > 120:
            raise ValueError("理由长度不能超过 120 字符")
        normalized = normalize_flag(flag)
        registry = self._adapter.request_flags
        self_id = self._adapter.bot_self_id
        if request_id is not None:
            row = await registry.resolve_request("group", request_id, self_id=self_id,
                                                 expected_revision=expected_revision, allow_archived=allow_archived)
            if row["flag"] != normalized or row["group_id"] != gid or row["sub_type"] != sub_type:
                raise AdminActionRejected("群请求与固定申请身份不符")
        try:
            occupied = await registry.occupy(
                "group", normalized, self_id=self_id, group_id=gid, sub_type=cast(str, sub_type),
                allow_archived=allow_archived,
            )
        except LookupError as error:
            raise AdminActionRejected(str(error)) from None
        if occupied.group_id != gid or occupied.sub_type != sub_type or occupied.state != "executing":
            raise AdminActionRejected("群请求占用凭据与当前动作不符")

        with bind_group_request_occupancy(
            self._adapter.config.id, self_id, gid, cast(str, sub_type), flag_digest("group", self_id, normalized),
        ):
            await self._execute_request_decision(
                "group", normalized, self_id,
                "set_group_add_request", {"flag": normalized, "sub_type": sub_type, "approve": approve, "reason": text},
            )
