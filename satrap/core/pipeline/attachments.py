"""
语音转写与文件正文提取的内容补全

在唤醒与限流之后, 按事件级预算获取顶层 Record/File 组件的远端内容, 语音经 AsyncASR 转写,
文件经受限文档提取器读取正文; 结果冻结进投影, 临时文件随事件清理, 失败时保留可识别的降级标记,
不把未知二进制送入文本模型
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any, Callable, cast
from urllib.parse import urlsplit
import asyncio
import os
import tempfile
import unicodedata

from satrap.core.utils.outbound import UnsafeOutboundURLError, normalize_hostname, safe_async_get
from satrap.core.pipeline.audio_convert import AudioTooLong, convert_to_wav, probe_audio, probe_duration
from satrap.core.platform.onebot.admin import PlatformAdminError, UnsupportedAdminAction
from satrap.core.utils.async_worker import BoundedAsyncWorker
from satrap.core.APICall.ASRCall import AsyncASR, build_asr_from_config
from satrap.core.components import PlatformComponentType, Record
from satrap.core.utils.documents import SUPPORTED_EXTENSIONS, extract_text
from satrap.core.platform.event import MessageEvent
from satrap.core.platform import PlatformAdapter
from satrap.core.type import ASRConfig, safe_getattr, safe_getattr_str

from satrap.core.log import logger

AUDIO_MAX_BYTES = 16 * 1024 * 1024
"""单条语音允许下载的最大字节数"""
FILE_MAX_BYTES = 32 * 1024 * 1024
"""单个文件允许下载的最大字节数"""
FILE_TEXT_LIMIT = 20000
"""单个文件正文进入模型输入的最大字符数"""
TRANSCRIPT_LIMIT = 4000
"""单条语音转写进入模型输入的最大字符数"""
ATTACHMENT_LIMIT = 4
"""每事件处理的语音与文件总数上限"""
FETCH_TIMEOUT = 20.0
"""单个附件下载超时秒数"""
ASR_TIMEOUT = 60.0
"""单次转写总超时秒数"""
ATTACHMENT_TOTAL_TIMEOUT = 90.0
"""每事件附件处理总预算秒数, 超出后其余附件标记 attachment_budget_exceeded 不再获取"""
AUDIO_MAX_SECONDS = 300.0
"""本地转码接受的最长语音秒数"""
VOICE_TRANSCRIBE_MODES = ("off", "asr", "platform", "asr_then_platform")
"""voice_transcribe 允许值: 关闭, 仅 ASR, 仅平台原生转写, ASR 失败后回退平台"""
EXTRACT_WORKERS = BoundedAsyncWorker("satrap-attachments", workers=2, capacity=8)
"""文档提取线程池, 不阻塞事件循环"""

AsrResolver = Callable[[str], ASRConfig | None]


def _safe_display(value: str) -> str:
    """
    消毒进入日志与投影标记的展示名

    平台上报的文件名可含换行/控制字符/ANSI 转义, 统一替换为空格后折叠空白并截断 80 字符

    参数:
    - value: 原始展示名

    返回:
    - str: 单行消毒结果, 可能为空串由调用方回退
    """
    cleaned = "".join(" " if unicodedata.category(ch) == "Cc" else ch for ch in value)
    return " ".join(cleaned.split())[:80]


@dataclass(frozen=True)
class AttachmentResult:
    """单个附件的补全结果"""

    kind: str
    """record 或 file"""
    name: str
    status: str
    """resolved, disabled, unsupported, too_large, failed 或 skipped"""
    text: str = ""
    reason: str = ""


def _suffix_of(name: str, url: str) -> str:
    """
    从文件名或 URL 路径推断扩展名

    参数:
    - name: 文件名
    - url: 远端地址

    返回:
    - str: 小写扩展名, 无法判断为空
    """
    for candidate in (name, url.split("?", 1)[0].split("#", 1)[0]):
        suffix = Path(candidate).suffix.lower()
        if suffix and len(suffix) <= 8 and suffix[1:].isalnum():
            return suffix
    return ""


async def _download(
    url: str, limit: int, trusted_hosts: tuple[str, ...], verify_tls: bool = True, allow_plaintext: bool = False,
) -> bytes:
    """
    经出站防护下载远端附件, 只接受 http/https

    参数:
    - url: 平台上报的下载地址
    - limit: 最大字节数
    - trusted_hosts: 允许访问私网的显式主机名
    - verify_tls: 是否校验证书; 用户开启 media_insecure_tls 时为 False, 且仅对 trusted_hosts 登记的主机生效, 公网下载始终校验
    - allow_plaintext: 用户开启 media_plaintext_http 时为 True, 放行公网明文 http; 登记主机不受此限

    返回:
    - bytes: 响应正文, 超限或不安全地址时抛出异常
    """
    if not url.lower().startswith(("http://", "https://")):
        raise UnsafeOutboundURLError("附件地址不是 http/https URL")
    host = normalize_hostname(urlsplit(url).hostname or "")
    trusted = {normalize_hostname(item) for item in trusted_hosts}
    if url.lower().startswith("http://") and host not in trusted:
        if not allow_plaintext:
            raise UnsafeOutboundURLError("公网附件地址必须使用 https, 或显式开启 media_plaintext_http")
        logger.debug(f"[attachments] 公网明文 http 下载已由用户开启: host={host}")
    effective_verify = verify_tls or host not in trusted
    response = await safe_async_get(
        url, timeout=FETCH_TIMEOUT, max_response_bytes=limit, trusted_hosts=trusted_hosts,
        ssl_verify=effective_verify, restrict_redirects_to_origin=True,
    )
    response.raise_for_status()
    return response.content


async def _transcribe(data: bytes, filename: str, config: ASRConfig) -> str:
    """
    调用异步 ASR 转写字节音频

    参数:
    - data: 音频字节
    - filename: 含扩展名的文件名
    - config: 已保存的 ASR 配置

    返回:
    - str: 转写文本, 空字符串表示服务返回空结果
    """
    client = cast(AsyncASR, build_asr_from_config(config, async_=True))
    client.suppress_error = False
    try:
        result = await asyncio.wait_for(client.transcribe(data, filename=filename), timeout=ASR_TIMEOUT)
    finally:
        try:
            await client.client.close()
        except Exception as error:
            logger.debug(f"[attachments] ASR 客户端关闭失败: {type(error).__name__}")
            # 关闭异常不覆盖转写结果或原始异常
    return (result.text if result is not None else "") or ""


class VoiceUnsupported(Exception):
    """语音格式无法转成 ASR 可接受的输入, reason 进入降级标记"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _check_duration(data: bytes, codec: str) -> None:
    """免转码语音的时长预算检查, 可探测且超时抛 VoiceUnsupported"""
    duration = probe_duration(data, codec)
    if duration is not None and duration > AUDIO_MAX_SECONDS:
        raise VoiceUnsupported("audio_too_long")


def _admin_action(event: MessageEvent, name: str) -> Callable[..., Any] | None:
    """
    取适配器 admin 上的可调用动作, 适配器或实现不提供时返回 None

    参数:
    - event: 当前事件
    - name: 动作名
    """
    admin = safe_getattr(event.adapter, "admin") if isinstance(event.adapter, PlatformAdapter) else None
    action = safe_getattr(admin, name) if admin is not None else None
    return action if callable(action) else None


async def _fetch_voice(
    event: MessageEvent, url: str, suffix: str, trusted: tuple[str, ...], verify_tls: bool, allow_plaintext: bool,
) -> tuple[bytes, str]:
    """
    三级获取 ASR 可接受的语音字节

    1. 实现服务端转码 (OneBot get_record out_format=wav), 覆盖 QQ SILK 语音
    2. 直接下载并按魔数探测, 已是 ASR 接受格式时原样使用
    3. 本地 PyAV 转码 ffmpeg 可解的编码 (amr 等), 面向不提供 get_record 的实现

    参数:
    - event: 当前事件, 提供适配器
    - url: 上报的 file 或 url 字段
    - suffix: 从名称或地址推断的扩展名
    - trusted / verify_tls / allow_plaintext: 出站下载参数

    返回:
    - tuple[bytes, str]: 音频字节与送入 ASR 的文件名; 无法处理时抛 VoiceUnsupported
    """
    get_record = _admin_action(event, "get_record")
    if get_record is not None:
        try:
            data = cast(bytes, await get_record(url, "wav", AUDIO_MAX_BYTES))
            if probe_audio(data, ".wav").codec == "wav":
                _check_duration(data, "wav")
                return data, "voice.wav"
            logger.warning("[attachments] get_record 返回内容不是 wav, 回退直接下载")
        except UnsupportedAdminAction:
            logger.debug("[attachments] get_record 不受当前实现支持, 回退直接下载")
        except PlatformAdminError as error:
            logger.warning(f"[attachments] get_record 失败, 回退直接下载: {type(error).__name__}")
    data = await _download(url, AUDIO_MAX_BYTES, trusted, verify_tls, allow_plaintext)
    probe = probe_audio(data, suffix)
    if probe.accepted:
        _check_duration(data, probe.codec)
        return data, f"voice.{probe.codec}"
    if not probe.convertible:
        raise VoiceUnsupported(probe.reason or f"audio_format{suffix or '_unknown'}")
    try:
        converted = await EXTRACT_WORKERS.run(convert_to_wav, data, max_seconds=AUDIO_MAX_SECONDS)
    except AudioTooLong as error:
        raise VoiceUnsupported("audio_too_long") from error
    return converted, "voice.wav"


async def _transcribe_platform(event: MessageEvent) -> str:
    """
    经实现的原生语音转文字 (OneBot fetch_ptt_text) 获取转写

    参数:
    - event: 当前事件, 需要源消息 ID

    返回:
    - str: 转写文本; 实现不支持时抛 VoiceUnsupported
    """
    message_id = event.call_origin.source_message_id
    fetch = _admin_action(event, "fetch_ptt_text")
    if fetch is None or not message_id:
        raise VoiceUnsupported("platform_transcribe_unavailable")
    try:
        return cast(str, await fetch(message_id))
    except UnsupportedAdminAction as error:
        raise VoiceUnsupported("platform_transcribe_unavailable") from error


async def _resolve_record(
    event: MessageEvent, url: str, suffix: str, mode: str, config: ASRConfig | None,
    trusted: tuple[str, ...], verify_tls: bool, allow_plaintext: bool,
) -> tuple[str, str, str]:
    """
    按 voice_transcribe 模式获取单条语音的转写

    参数:
    - mode: asr, platform 或 asr_then_platform
    - config: 已校验的 ASR 配置, platform 模式可为 None

    返回:
    - tuple[str, str, str]: (status, text, reason)
    """
    asr_reason = ""
    if mode != "platform" and config is not None:
        try:
            data, filename = await _fetch_voice(event, url, suffix, trusted, verify_tls, allow_plaintext)
            return "resolved", await _transcribe(data, filename, config), ""
        except VoiceUnsupported as error:
            asr_reason = error.reason
            if mode == "asr":
                return "unsupported", "", asr_reason
        except Exception as error:
            asr_reason = type(error).__name__
            logger.warning(f"[attachments] 语音转写失败 session={event.session_id} asr={config.name}: {asr_reason}")
            if mode == "asr":
                return "failed", "", asr_reason
    try:
        return "resolved", await _transcribe_platform(event), ""
    except VoiceUnsupported as error:
        return "unsupported", "", asr_reason or error.reason
    except Exception as error:
        logger.warning(f"[attachments] 平台语音转写失败 session={event.session_id}: {type(error).__name__}")
        return "failed", "", asr_reason or type(error).__name__


def _write_and_extract(data: bytes, suffix: str) -> tuple[str, str]:
    """
    线程内写入临时文件并提取正文 (由 EXTRACT_WORKERS 调用, 不占用事件循环)

    参数:
    - data: 文件字节
    - suffix: 扩展名

    返回:
    - tuple[str, str]: (临时文件路径, 提取正文); 失败时临时文件已在内部清理, 不向事件登记
    """
    handle = tempfile.NamedTemporaryFile(prefix="satrap-attach-", suffix=suffix, delete=False)
    path = handle.name
    try:
        try:
            handle.write(data)
        finally:
            handle.close()
        return path, extract_text(path, max_length=FILE_TEXT_LIMIT, max_file_size=FILE_MAX_BYTES)
    except BaseException:
        try:
            os.unlink(path)
        except OSError as error:
            logger.debug(f"[attachments] 临时文件清理失败 path={path}: {type(error).__name__}")
        raise


async def _extract_file(data: bytes, suffix: str, event: MessageEvent) -> str:
    """
    在线程中写入临时文件并提取正文, 成功后临时文件登记到事件

    参数:
    - data: 文件字节
    - suffix: 扩展名
    - event: 当前事件, 负责结束时清理

    返回:
    - str: 提取正文, 已按 FILE_TEXT_LIMIT 截断
    """
    path, text = await EXTRACT_WORKERS.run(_write_and_extract, data, suffix)
    # 提取成功后登记, 由事件统一清理; 失败路径已在 _write_and_extract 内部清理
    event.track_temporary_local_file(path)
    return text


async def resolve_attachments(event: MessageEvent, asr_resolver: AsrResolver | None) -> tuple[AttachmentResult, ...]:
    """
    处理顶层 Record 与 File 组件, 每事件至多 ATTACHMENT_LIMIT 个

    全程受 ATTACHMENT_TOTAL_TIMEOUT 总预算约束, 预算耗尽后其余附件直接标记失败, 不再发起下载或转写

    参数:
    - event: 已唤醒且已通过限流的事件
    - asr_resolver: 按名称返回 ASR 配置, 后端未提供时语音标记为 disabled

    返回:
    - tuple[AttachmentResult, ...]: 按组件顺序排列的结果, 已冻结的 Record.text 优先复用
    """
    settings = event.policy_settings
    trusted = tuple(str(item) for item in cast(list[object], settings.get("media_trusted_hosts", []) or []))
    verify_tls = settings.get("media_insecure_tls", False) is not True
    allow_plaintext = settings.get("media_plaintext_http", False) is True
    asr_name = str(settings.get("asr_model", "") or "")
    voice_mode = str(settings.get("voice_transcribe", "asr") or "asr")
    extract_enabled = settings.get("attachment_extract", True) is not False
    deadline = monotonic() + ATTACHMENT_TOTAL_TIMEOUT
    results: list[AttachmentResult] = []
    handled = 0
    for comp in event.get_messages():
        if comp.type not in {PlatformComponentType.Record, PlatformComponentType.File}:
            continue
        kind = "record" if comp.type == PlatformComponentType.Record else "file"
        name = safe_getattr_str(comp, "name") or ""
        url = safe_getattr_str(comp, "url") or safe_getattr_str(comp, "file") or safe_getattr_str(comp, "file_") or ""
        display = _safe_display(name or Path(url.split("?", 1)[0]).name) or kind
        if handled >= ATTACHMENT_LIMIT:
            results.append(AttachmentResult(kind, display, "skipped", reason="attachment_limit"))
            continue
        if monotonic() >= deadline:
            results.append(AttachmentResult(kind, display, "failed", reason="attachment_budget_exceeded"))
            continue
        handled += 1
        if kind == "record":
            existing = safe_getattr_str(comp, "text")
            if existing:
                results.append(AttachmentResult(kind, display, "resolved", existing[:TRANSCRIPT_LIMIT]))
                continue
            if voice_mode == "off":
                results.append(AttachmentResult(kind, display, "disabled", reason="voice_transcribe_off"))
                continue
            config: ASRConfig | None = None
            if voice_mode != "platform":
                if not asr_name or asr_resolver is None:
                    results.append(AttachmentResult(kind, display, "disabled", reason="asr_not_configured"))
                    continue
                config = asr_resolver(asr_name)
                if config is None or not config.model or not config.api_key:
                    results.append(AttachmentResult(kind, display, "disabled", reason="asr_config_missing"))
                    continue
                if not url:
                    results.append(AttachmentResult(kind, display, "failed", reason="no_remote_url"))
                    continue
            status, text, reason = await _resolve_record(event, url, _suffix_of(name, url), voice_mode, config, trusted, verify_tls, allow_plaintext)
            if status == "resolved" and isinstance(comp, Record):
                comp.text = text[:TRANSCRIPT_LIMIT]
            # 转写结果冻结在组件上, 同一事件的后续消费者不重复调用 ASR
            results.append(AttachmentResult(kind, display, status, text[:TRANSCRIPT_LIMIT], reason))
            continue
        if not extract_enabled:
            results.append(AttachmentResult(kind, display, "disabled", reason="attachment_extract_off"))
            continue
        suffix = _suffix_of(name, url)
        if suffix not in SUPPORTED_EXTENSIONS:
            results.append(AttachmentResult(kind, display, "unsupported", reason=f"file_type{suffix or '_unknown'}"))
            continue
        if not url:
            results.append(AttachmentResult(kind, display, "failed", reason="no_remote_url"))
            continue
        try:
            data = await _download(url, FILE_MAX_BYTES, trusted, verify_tls, allow_plaintext)
            text = await _extract_file(data, suffix, event)
        except Exception as error:
            results.append(AttachmentResult(kind, display, "failed", reason=type(error).__name__))
            logger.warning(f"[attachments] 文件提取失败 session={event.session_id} file={display}: {type(error).__name__}")
            continue
        results.append(AttachmentResult(kind, display, "resolved", text[:FILE_TEXT_LIMIT]))
    return tuple(results)


_UNSUPPORTED_HINTS = {
    "silk_needs_platform_transcode": "QQ SILK 语音需要实现提供 get_record 转码或平台原生转写",
    "av_missing": "该语音格式需要本地转码, 未安装 av 包",
    "audio_too_long": "语音过长, 已跳过转写",
    "platform_transcribe_unavailable": "当前实现不提供原生语音转写",
}
"""unsupported 状态的原因说明, 未列出的原因使用通用文案"""

_FAILED_HINTS = {
    "attachment_budget_exceeded": "附件处理超时, 已跳过",
}
"""failed 状态的原因说明, 未列出的原因使用通用文案"""


def render_attachments(results: tuple[AttachmentResult, ...]) -> str:
    """
    把附件结果渲染为标记块, 作为用户提供的资料而非指令

    参数:
    - results: resolve_attachments 的输出

    返回:
    - str: 空结果返回空字符串
    """
    blocks: list[str] = []
    for item in results:
        label = "语音" if item.kind == "record" else f"文件 {item.name}"
        if item.status == "resolved":
            body = item.text.strip() or "(内容为空)"
            blocks.append(f"[{label} 转写内容: {body}]" if item.kind == "record" else f"[{label} 内容:\n{body}]")
        elif item.status == "disabled":
            blocks.append(f"[{label}: 未启用{'转写' if item.kind == 'record' else '正文提取'}]")
        elif item.status == "unsupported":
            blocks.append(f"[{label}: {_UNSUPPORTED_HINTS.get(item.reason, '不支持的格式')}]")
        elif item.status == "skipped":
            blocks.append(f"[{label}: 超出附件处理数量]")
        else:
            blocks.append(f"[{label}: {_FAILED_HINTS.get(item.reason, '获取或处理失败')}]")
    return "\n".join(blocks)


def asr_resolver_from_manager(manager: Any) -> AsrResolver | None:
    """
    由 ModelConfigManager 构造名称到 ASRConfig 的解析器

    参数:
    - manager: 后端模型配置管理器, None 时返回 None

    返回:
    - AsrResolver | None: 缺失配置返回 None 的解析函数
    """
    if manager is None:
        return None
    has_config = safe_getattr(manager, "has_config")
    get_config = safe_getattr(manager, "get_asr_config")
    if not callable(has_config) or not callable(get_config):
        return None

    def resolve(name: str) -> ASRConfig | None:
        if not has_config("asr", name):
            return None
        config = get_config(name)
        return config if isinstance(config, ASRConfig) else None
    return resolve
