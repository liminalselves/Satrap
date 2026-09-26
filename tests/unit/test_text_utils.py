"""TextSplitter 合并语义基线与长文本切分性能回归

性能反例来自复核记录: 40 万字符的无分隔中文文本在 RAG 文档上传路径上需要 11-23 秒完成切分
(同机实测 list.pop(0) 版本 11.0 秒, 审计环境 22.8 秒), 超过上传接口 15 秒的读超时上限。
原因是合并窗口用 list.pop(0) 逐个从队首弹出, 单个分块边界的成本为 O(窗口²)。
"""
from __future__ import annotations

import time

from satrap.core.utils.text_utils import TextSplitter

# 复核使用的场景: 40 万字符连续中文, 无任何分隔符
NO_SEPARATOR_400K = "中文资料" * 100_000
# 知识库上传使用的分块配置 (chunk_size/chunk_overlap 来自知识库 config)
RAG_CHUNK_SIZE = 100_000
RAG_CHUNK_OVERLAP = 0
# 修复后同机实测 0.12 秒; 改回 list.pop(0) 后 11.0 秒, 故 3 秒预算既能容纳机器差异又能拦住旧实现
SPLIT_SECONDS_BUDGET = 3.0
# 大语料预算: 修复后同机约 0.4 秒, 旧实现约 45 秒
LARGE_CORPUS_SECONDS_BUDGET = 8.0


def test_long_text_without_separator_is_split_within_budget():
    """反例: 无分隔长文本曾因队首逐个弹出退化为二次复杂度, 拖垮上传请求"""
    # 预热一次, 排除首次导入与分配带来的抖动
    TextSplitter(RAG_CHUNK_SIZE, RAG_CHUNK_OVERLAP).split_text("预热")
    started = time.perf_counter()
    chunks = TextSplitter(RAG_CHUNK_SIZE, RAG_CHUNK_OVERLAP).split_text(NO_SEPARATOR_400K)
    spent = time.perf_counter() - started
    assert spent < SPLIT_SECONDS_BUDGET, f"40 万字符无分隔文本切分耗时 {spent:.2f} 秒, 超过 {SPLIT_SECONDS_BUDGET} 秒预算"
    # 性能修复不得改变结果: 4 块各 10 万字符, 拼接后与原文一致
    assert [len(chunk) for chunk in chunks] == [RAG_CHUNK_SIZE] * 4
    assert "".join(chunks) == NO_SEPARATOR_400K


def test_long_text_with_default_chunking_stays_within_budget():
    """默认分块参数下的长文本同样受预算约束 (默认参数原本就快, 防回归)"""
    started = time.perf_counter()
    chunks = TextSplitter(1000, 200).split_text(NO_SEPARATOR_400K)
    spent = time.perf_counter() - started
    assert spent < SPLIT_SECONDS_BUDGET, f"默认参数切分耗时 {spent:.2f} 秒, 超过 {SPLIT_SECONDS_BUDGET} 秒预算"
    assert len(chunks) == 500
    assert all(len(chunk) <= 1000 for chunk in chunks)
    # 重叠语义: 每块开头重复上一块结尾的 200 字符, 去掉重叠后应还原原文
    assert all(chunk[:200] == chunks[index - 1][-200:] for index, chunk in enumerate(chunks) if index)
    rebuilt = chunks[0] + "".join(chunk[200:] for chunk in chunks[1:])
    assert rebuilt == NO_SEPARATOR_400K


def test_chunking_cost_is_bounded_by_document_times_window():
    """预算同时约束文档长度与合并窗口的乘积

    旧实现每个分块边界都把整个窗口逐个从队首弹出, 单次成本为 O(窗口²), 总成本为
    O(文档长度 × 窗口大小): 固定窗口时仍随长度线性增长, 但常数因子等于窗口大小。
    因此这里同时放大文档长度 (120 万字符) 并沿用知识库的大窗口, 实测 deque 实现 0.4 秒,
    改回 list.pop(0) 后约 45 秒。
    """
    corpus = "资" * 1_200_000
    started = time.perf_counter()
    chunks = TextSplitter(RAG_CHUNK_SIZE, RAG_CHUNK_OVERLAP).split_text(corpus)
    spent = time.perf_counter() - started
    assert spent < LARGE_CORPUS_SECONDS_BUDGET, f"120 万字符切分耗时 {spent:.2f} 秒, 超过 {LARGE_CORPUS_SECONDS_BUDGET} 秒预算"
    assert len(chunks) == 12
    assert "".join(chunks) == corpus


def test_no_separator_chunks_carry_overlap():
    """无分隔文本的重叠窗口语义 (合并逻辑的核心契约)"""
    chunks = TextSplitter(5, 1).split_text("abcdefghijklmno")
    assert chunks == ["abcde", "efghi", "ijklm", "mno"]


def test_small_paragraph_baseline_is_unchanged():
    """双换行分段的现状基线: 保留分隔符时按段切分, 关闭保留时分隔符随段合并"""
    text = "第一段内容\n\n第二段内容\n\n第三段内容"
    assert TextSplitter(12, 3).split_text(text) == ["第一段内容", "\n\n第二段内容", "\n\n第三段内容"]
    assert TextSplitter(12, 3, keep_separator=False).split_text(text) == ["第一段内容\n\n第二段内容", "第三段内容"]


def test_single_part_larger_than_chunk_size_is_kept():
    """没有可用分隔符时超长片段被强制保留, 不丢内容"""
    chunks = TextSplitter(4, 0).split_text("abcdefghij")
    assert chunks == ["abcd", "efgh", "ij"]


def test_short_text_is_a_single_chunk():
    assert TextSplitter(100, 20).split_text("整个文本很短") == ["整个文本很短"]


def test_documents_are_split_in_order():
    splitter = TextSplitter(5, 0)
    assert splitter.split_documents(["abcdefg", "1234567"]) == ["abcde", "fg", "12345", "67"]
