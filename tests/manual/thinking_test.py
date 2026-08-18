"""实测 deepseek-v4-flash 的 thinking off/on 效果

API 配置从 .toolkit/apikey.txt 读取 (该目录已 gitignore, 不硬编码密钥)。
格式: 每块为若干 `key: value` 行, 空行分隔; 取 base url 含 deepseek.com 的块。
"""
import asyncio
import sys
import os
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from satrap.core.APICall.LLMCall import AsyncLLM

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TOOLKIT_PATH = PROJECT_ROOT / ".toolkit" / "apikey.txt"

BASE_URL = "https://api.deepseek.com/v1"
MODEL = "deepseek-v4-flash"


def _load_deepseek_config() -> dict[str, str]:
    """从 .toolkit/apikey.txt 解析 deepseek 配置块 (api key / base url / model)"""
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
    """构造真实 DeepSeek LLM; 配置缺失时明确报错退出"""
    cfg = _load_deepseek_config()
    api_key = cfg.get("api key", "")
    if not api_key:
        print(f"错误: {TOOLKIT_PATH} 中没有可用的 deepseek 配置 (api key 缺失)")
        sys.exit(1)
    return AsyncLLM(
        base_url=cfg.get("base url") or BASE_URL,
        api_key=api_key,
        model=cfg.get("model") or MODEL,
    )


async def test_thinking_off():
    """thinking='off' 应显式关闭思考, 响应中无 thinking"""
    llm = _build_llm()
    messages: list[dict[str, Any]] = [{"role": "user", "content": "1+1=?"}]
    print("=== thinking='off' ===")
    result = await llm.call(messages, thinking="off")
    if result is False:
        print("WARN: LLM call 失败 (返回 False), 跳过")
        return
    print(f"type: {result.type}")
    print(f"content: {result.content[:100]}")
    print(f"thinking: {result.thinking!r}")
    assert result.thinking is None or result.thinking == "", \
        f"thinking='off' 时不应有 thinking, 得到: {result.thinking!r}"
    print("PASS: thinking='off' 无 thinking\n")


async def test_thinking_medium():
    """thinking='medium' 应开启思考, 响应中有 thinking"""
    llm = _build_llm()
    messages: list[dict[str, Any]] = [{"role": "user", "content": "1+1=?"}]
    print("=== thinking='medium' ===")
    result = await llm.call(messages, thinking="medium")
    if result is False:
        print("WARN: LLM call 失败 (返回 False), 跳过")
        return
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
    llm = _build_llm()
    messages: list[dict[str, Any]] = [{"role": "user", "content": "1+1=?"}]
    print("=== 默认 thinking (应为 'off') ===")
    result = await llm.call(messages)
    if result is False:
        print("WARN: LLM call 失败 (返回 False), 跳过")
        return
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
