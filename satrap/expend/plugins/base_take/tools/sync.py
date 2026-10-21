"""base_take 同步工具入口, 复用文档读取核心"""
from __future__ import annotations

from typing import Any

from satrap.core.file_access.reads import read_error
from satrap.core.utils.TCBuilder import Tool
from satrap.edictum import SimpleSession
from .base import _DocumentCore, _MediaCore


class ReadDocumentTool(_DocumentCore, Tool):
    """读取本地图片和文档文本, 按模型能力分页查看 PDF 图像"""

    tool_name = "read_document"
    description = "读取本地图片和视频 (png/jpg/webp/gif/mp4/webm/mov/mkv/avi) 和 xlsx/docx/pdf/txt/md 等文档; 图片返回视觉内容, 动画和视频支持按时间区间读取; 目录外读取按 user/auto/full-access 审批; PDF 支持分页, auto 随模型能力返回文字或页面图像, text 只读文字, visual 查看图表和扫描页"
    params_dict = {
        "path": ("string", "文档或图片路径 (绝对或相对工作区)"),
        "max_length": ("number", "返回文本最大长度, 默认 131072, 超出截断"),
        "mode": ("string", "auto/text/visual, 默认 auto"),
        "start_page": ("number", "PDF 起始页码, 从 1 开始"),
        "page_count": ("number", "本次读取 1 至 5 页, 默认 5"),
        "start_time": ("number", "动画或视频起始秒数, 默认 0"),
        "end_time": ("number", "动画或视频结束秒数, 不填表示结尾; 长媒体应分段读取"),
    }

    def _bind(self, session: SimpleSession) -> None:
        self._session = session

    def execute(self, path: str, max_length: int = 131072, mode: str = "auto", start_page: int = 1, page_count: int = 5, start_time: float = 0.0, end_time: float | None = None) -> str | dict[str, Any]:
        """
        读取文档文字, 本地图片及可选 PDF 页面图片

        参数:
        - path: 文档或图片路径, 目录外单文件读取按当前审批模式处理
        - max_length: 文本字符上限, 默认 131072
        - mode: auto 随模型能力选择, text 只读文字, visual 返回图片或页面图像
        - start_page: PDF 起始页码, 默认 1
        - page_count: PDF 读取页数, 默认 5, 最多 5
        - start_time: 动画或视频起始秒数, 默认 0
        - end_time: 动画或视频结束秒数, 默认结尾

        返回:
        - 文本或可持久化的媒体结果, 读取失败时返回错误文本
        """
        try:
            options = self._prepare_document(path, max_length, mode, start_page, page_count, start_time, end_time)
            return options.file.read_sync(
                lambda target: self._read_document(target, options),
            )
        except Exception as error:
            return read_error(error)


class ReadMediaTool(_MediaCore, Tool):
    """同步查看本会话已登记媒体的指定时间区间"""

    def _bind(self, session: SimpleSession) -> None:
        """
        绑定实际会话

        参数:
        - session: 工具所属会话
        """
        self._session = session

    def execute(self, media_id: str, start_time: float = 0.0, end_time: float | None = None) -> str | dict[str, Any]:
        """
        读取已登记媒体

        参数:
        - media_id: 当前会话媒体标识
        - start_time: 起始秒数, 默认 0
        - end_time: 结束秒数, 默认结尾

        返回:
        - 工具媒体结果或错误文字
        """
        try:
            return self._read_media(media_id, start_time, end_time)
        except Exception as error:
            return read_error(error)
