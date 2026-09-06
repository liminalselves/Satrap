"""兼容原文档读取入口, 通用解析实现在 core.utils.documents"""
from satrap.core.utils.documents import (
    extract_text as extract_text,
    _mute_pdfminer_fontbbox_warning as _mute_pdfminer_fontbbox_warning,
)
