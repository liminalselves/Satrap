"""
实测 deepseek-v4-flash 的 thinking off/on 效果

API 配置从 .toolkit/apikey.txt 读取 (该目录已 gitignore, 不硬编码密钥)
格式: 每块为若干 `key: value` 行, 空行分隔; 取 base url 含 deepseek.com 的块
"""
import asyncio
from pathlib import Path
from typing import Any
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from satrap.core.APICall.LLMCall import AsyncLLM
from satrap.core.type import LLMCallResponse

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TOOLKIT_PATH = PROJECT_ROOT / ".toolkit" / "apikey.txt"

BASE_URL = "https://api.deepseek.com/v1"
MODEL = "deepseek-v4-flash"


def _load_deepseek_config() -> dict[str, str]:
    """
    从 .toolkit/apikey.txt 解析 deepseek 配置块 (api key / base url / model)

    返回:
    - dict[str, str]: 从 .toolkit/apikey.txt 解析 deepseek 配置块 (api key / base url / model)
    """
    if not TOOLKIT_PATH.exists():
        return {}
    text = TOOLKIT_PATH.read_text(encoding="utf-8")
    for block in text.strip().split("\n\n"):
        cfg: dict[str, str] = {}
        for line in block.splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            cfg[key.strip().lower()] = value.strip()
        if "deepseek.com" in cfg.get("base url", ""):
            return cfg.copy()
    return {}


def _build_llm() -> AsyncLLM:
    """
    构造真实 DeepSeek LLM; 配置缺失时明确报错退出

    返回:
    - AsyncLLM: 构造真实 DeepSeek LLM; 配置缺失时明确报错退出
    """
    cfg = _load_deepseek_config()
    api_key = cfg.get("api key", "")
    if not api_key:
        print(f"错误: {TOOLKIT_PATH} 中没有可用的 deepseek 配置 (api key 缺失)")
        sys.exit(1)
    return AsyncLLM(
        base_url=cfg.get("base url") or BASE_URL,
        api_key=api_key,
        model=cfg.get("model") or MODEL,
        max_tokens=256,
        thinking_fields=["thinking.type", "reasoning_effort"],
        omit_none_thinking_fields=True,
    )


async def _stream_response(
    llm: AsyncLLM,
    messages: list[dict[str, Any]],
    thinking: str = "off",
) -> LLMCallResponse:
    """
    通过 Chat 实际使用的流式路径获取最终响应

    参数:
    - llm: 异步 LLM
    - messages: 对话消息
    - thinking: 思考强度

    返回:
    - LLMCallResponse: 最终响应
    """
    result: LLMCallResponse | None = None
    async for event in llm.stream_call(messages, thinking=thinking):
        if event.kind == "done" and isinstance(event.response, LLMCallResponse):
            result = event.response
    if result is None:
        raise RuntimeError("流式调用未返回最终响应")
    return result


async def test_thinking_off():
    """thinking='off' 应显式关闭思考, 响应中无 thinking"""
    llm = _build_llm()
    messages: list[dict[str, Any]] = [{"role": "user", "content": "1+1=?"}]
    print("=== thinking='off' ===")
    result = await _stream_response(llm, messages, thinking="off")
    print(f"type: {result.type}")
    print(f"content: {result.content[:100]}")
    print(f"thinking: {result.thinking!r}")
    assert result.thinking is None or result.thinking == "", \
        f"thinking='off' 时不应有 thinking, 得到: {result.thinking!r}"
    print("PASS: thinking='off' 无 thinking\n")


async def test_thinking_enabled(level: str):
    """
    指定思考强度时应开启思考并返回 thinking

    参数:
    - level: 思考强度
    """
    llm = _build_llm()
    messages: list[dict[str, Any]] = [{"role": "user", "content": "1+1=?"}]
    print(f"=== thinking='{level}' ===")
    result = await _stream_response(llm, messages, thinking=level)
    print(f"type: {result.type}")
    print(f"content: {result.content[:100]}")
    thinking = result.thinking
    print(f"thinking: {thinking[:100] if thinking else None!r}")
    assert thinking, f"thinking='{level}' 时应返回 thinking"
    print(f"PASS: thinking='{level}' 有 thinking\n")


async def test_thinking_default_off():
    """默认 thinking='off' 应关闭思考"""
    llm = _build_llm()
    messages: list[dict[str, Any]] = [{"role": "user", "content": "1+1=?"}]
    print("=== 默认 thinking (应为 'off') ===")
    result = await _stream_response(llm, messages)
    print(f"thinking: {result.thinking!r}")
    assert result.thinking is None or result.thinking == "", \
        f"默认 thinking='off' 时不应有 thinking"
    print("PASS: 默认 thinking='off' 无 thinking\n")


async def main():
    await test_thinking_off()
    await test_thinking_enabled("low")
    await test_thinking_enabled("high")
    await test_thinking_default_off()
    print("=== 全部测试通过 ===")


if __name__ == "__main__":
    asyncio.run(main())
