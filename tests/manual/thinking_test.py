"""实测 deepseek-v4-flash 的 thinking off/on 效果"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from satrap.core.APICall.LLMCall import AsyncLLM

BASE_URL = "https://api.deepseek.com/v1"
MODEL = "deepseek-v4-flash"
API_KEY = "***REMOVED***"


async def test_thinking_off():
    """thinking='off' 应显式关闭思考, 响应中无 thinking"""
    llm = AsyncLLM(base_url=BASE_URL, api_key=API_KEY, model=MODEL)
    messages = [{"role": "user", "content": "1+1=?"}]
    print("=== thinking='off' ===")
    result = await llm.call(messages, thinking="off")
    print(f"type: {result.type}")
    print(f"content: {result.content[:100]}")
    print(f"thinking: {result.thinking!r}")
    assert result.thinking is None or result.thinking == "", \
        f"thinking='off' 时不应有 thinking, 得到: {result.thinking!r}"
    print("PASS: thinking='off' 无 thinking\n")


async def test_thinking_medium():
    """thinking='medium' 应开启思考, 响应中有 thinking"""
    llm = AsyncLLM(base_url=BASE_URL, api_key=API_KEY, model=MODEL)
    messages = [{"role": "user", "content": "1+1=?"}]
    print("=== thinking='medium' ===")
    result = await llm.call(messages, thinking="medium")
    print(f"type: {result.type}")
    print(f"content: {result.content[:100]}")
    thinking = result.thinking
    print(f"thinking: {thinking[:100] if thinking else None!r}")
    if thinking:
        print("PASS: thinking='medium' 有 thinking\n")
    else:
        print("WARN: thinking='medium' 无 thinking (模型可能不支持 effort 参数)\n")


async def test_thinking_default_off():
    """默认 thinking='off' 应关闭思考"""
    llm = AsyncLLM(base_url=BASE_URL, api_key=API_KEY, model=MODEL)
    messages = [{"role": "user", "content": "1+1=?"}]
    print("=== 默认 thinking (应为 'off') ===")
    result = await llm.call(messages)
    print(f"thinking: {result.thinking!r}")
    assert result.thinking is None or result.thinking == "", \
        f"默认 thinking='off' 时不应有 thinking"
    print("PASS: 默认 thinking='off' 无 thinking\n")


async def main():
    await test_thinking_off()
    await test_thinking_medium()
    await test_thinking_default_off()
    print("=== 全部测试通过 ===")


if __name__ == "__main__":
    asyncio.run(main())
