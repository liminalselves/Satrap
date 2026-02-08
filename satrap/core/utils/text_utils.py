from typing import List, Optional
import re

class TextSplitter:
    def __init__(self,
        chunk_size: int = 1000,
        chunk_overlap: int = 200,
        separators: Optional[List[str]] = None,
        keep_separator: bool = True,
    ):
        """
        初始化递归字符文本分割器
        模拟 LangChain 的 RecursiveCharacterTextSplitter 行为

        参数:
        - chunk_size: 每个分块的最大字符数
        - chunk_overlap: 分块之间的重叠字符数
        - separators: 用于分割文本的分隔符列表, 按优先级排序; 默认为 ["\\n\\n", "\\n", ""]
        - keep_separator: 是否在分割后的文本中保留分隔符
        """
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        if separators is None:
            self.separators = ["\n\n", "\n", ""]
        else:
            self.separators = separators
        self.keep_separator = keep_separator

    def split_text(self, text: str) -> List[str]:
        """
        文本分割

        参数:
        - text: 需要分割的原始长文本

        返回:
        - 分割后的字符串列表
        """
        return self._split_text(text, self.separators)

    def _split_text(self, text: str, separators: List[str]) -> List[str]:
        """
        内部递归分割逻辑

        参数:
        - text: 当前需要处理的文本片段
        - separators: 当前可用的分隔符列表

        返回:
        - 分割后的文本块列表
        """
        # Step.1 确定当前使用的分隔符
        final_chunks = []
        separator = separators[-1] if separators else ""
        new_separators = []

        for i, sep in enumerate(separators):
            if sep == "":
                separator = ""
                break
            if sep in text:
                separator = sep
                new_separators = separators[i + 1:]
                break
            # 找到文本中存在的最高优先级分隔符, 并确定剩余可用的分隔符

        # Step.2 根据分隔符执行分割
        if separator:
            if self.keep_separator:
                # 使用正则分割并保留分隔符, 这里的逻辑模拟 split 但保留 sep
                _splits = re.split(f"({re.escape(separator)})", text)
                splits = [f"{_splits[i]}{_splits[i+1]}" for i in range(1, len(_splits), 2)]
                if len(_splits) % 2 == 1:
                    splits = [_splits[0]] + splits
                # 将分隔符拼接到分割后的前一段文本末尾 (也可根据需求拼接到下一段开头)
            else:
                splits = text.split(separator)
                # 直接分割, 不保留分隔符
        else:
            splits = list(text)
            # 如果没有找到分隔符或分隔符为空字符串, 则按字符逐个分割

        # Step.3 递归处理过大的片段并合并
        good_splits = []
        for s in splits:
            if len(s) < self.chunk_size:
                good_splits.append(s)
            else:
                # 如果当前片段仍然过大, 使用剩余的分隔符继续递归分割
                if new_separators:
                    good_splits.extend(self._split_text(s, new_separators))
                else:
                    good_splits.append(s)
                    # 如果没有剩余分隔符了, 只能强制保留 (这种情况极少, 除非 chunk_size 小于单个字符)

        # Step.4 合并细碎片段为最终的 Chunk
        return self._merge_splits(good_splits, separator)

    def _merge_splits(self, splits: List[str], separator: str) -> List[str]:
        """
        将细小的文本片段合并成不超过 chunk_size 的块, 并处理重叠

        参数:
        - splits: 已经分割好的小文本片段列表
        - separator: 连接这些片段时使用的分隔符

        返回:
        - 合并完成的最终文本块列表
        """
        docs = []
        current_doc: List[str] = []
        total_len = 0
        separator_len = len(separator)

        for d in splits:
            _len = len(d)
            
            # Step.5 判断是否需要截断当前块
            if total_len + _len + (separator_len if current_doc else 0) > self.chunk_size:
                if total_len > self.chunk_size:
                    # 如果单个片段本身就超长 (且无法再分), 也要强制输出
                    pass

                if current_doc:
                    doc = separator.join(current_doc)
                    if doc:
                        docs.append(doc)
                    # 将当前累积的片段合并为一个 Chunk 并保存

                    # Step.6 处理 Overlap (重叠逻辑)
                    while total_len > self.chunk_overlap or (total_len + _len + separator_len > self.chunk_size and total_len > 0):
                        # 移除最早加入的片段, 直到满足重叠大小或腾出足够空间
                        pop_len = len(current_doc[0])
                        total_len -= pop_len
                        if len(current_doc) > 1:
                            total_len -= separator_len
                        current_doc.pop(0)
                        if not current_doc:
                            break
                    # 这里通过弹出队首元素来维持窗口在 chunk_overlap 范围内

            current_doc.append(d)
            total_len += _len + (separator_len if len(current_doc) > 1 else 0)
            # 将当前片段加入缓存并更新总长度

        # Step.7 处理剩余的片段
        if current_doc:
            doc = separator.join(current_doc)
            if doc:
                docs.append(doc)

        return docs
    
if __name__ == "__main__":
    splitter = TextSplitter(chunk_size=10, chunk_overlap=5)
    text_path = "README.md"
    with open(text_path, "r", encoding="utf-8") as f:
        text = f.read()
    chunks = splitter.split_text(text)
    print(chunks)
