"""splintr 分词库的本地类型存根 (库本身无类型声明)"""

class Tokenizer:
    """splintr 分词器"""
    @classmethod
    def from_pretrained(cls, name: str) -> Tokenizer:
        """加载预训练分词器"""
        ...
    def encode(self, text: str) -> list[int]:
        """将文本编码为 token id 列表"""
        ...
