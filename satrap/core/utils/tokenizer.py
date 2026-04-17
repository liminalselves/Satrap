from splintr import Tokenizer

tokenizer = Tokenizer.from_pretrained("deepseek_v3")

def tokenizer_estimate(text: str) -> int:
    """使用分词器估计文本的 token 数量"""
    return len(tokenizer.encode(text))

def experience_estimate(text: str) -> int:
    """使用经验法则估计文本的 token 数量

    规则:
    - 中文字符每个约 0.7 个 token
    - 英文字母、数字、标点等: 每 4 个字符约 1 个 token(即 0.25 token/字符)
    """
    import math
    
    chinese_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    other_chars = len(text) - chinese_chars
    estimate = chinese_chars * 0.7 + other_chars * 0.25
    return max(1, math.ceil(estimate))
if __name__ == "__main__":
    print(tokenizer_estimate("Hello, world!"))
    print(experience_estimate("Hello, world!"))
    print(tokenizer_estimate("你好，世界！"))
    print(experience_estimate("你好，世界！"))
