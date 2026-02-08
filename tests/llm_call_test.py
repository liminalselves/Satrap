from satrap.core.api_call.llm_call import AsyncLLM, LLM
import asyncio, os, sys

# 将项目根目录添加到 sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# 示例: 初始化机器人 (请替换为实际的 key)
bot = LLM(
    api_key="", 
    base_url="https://api.deepseek.com/v1",
    model="deepseek-chat",
    temperature=1.0
)

# 异步版本
async_bot = AsyncLLM(
    api_key="", 
    base_url="https://api.deepseek.com/v1",
    model="deepseek-chat",
    temperature=1.0
)

def main():
    """
    [同步版本] 主运行函数示例
    """
    # 构造符合要求的对话历史 JSON 结构
    dialogue_history = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Hello! Can you write a short poem about code?"},
    ]

    print("=" * 50)
    print("[同步版本] 模型:", bot.get_model())
    print("[同步版本] API Key:", bot.get_api_key())
    print("=" * 50)
    print("正在调用同步 LLM 流式接口...")

    for chunk in bot.stream_chat(messages=dialogue_history):
        print(f"{chunk}", end="", flush=True)
    
    print("\n")

async def async_main():
    """
    [异步版本] 主运行函数示例
    """
    # 构造符合要求的对话历史 JSON 结构
    dialogue_history = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Hello! Can you write a short poem about code?"},
    ]

    print("=" * 50)
    print("[异步版本] 模型:", async_bot.get_model())
    print("[异步版本] API Key:", async_bot.get_api_key())
    print("=" * 50)
    print("正在调用异步 LLM 流式接口...")

    async for chunk in async_bot.stream_chat(messages=dialogue_history):
        print(f"{chunk}", end="", flush=True)
    
    print("\n")

# 运行主循环
if __name__ == "__main__":
    print("\n>>> 测试同步版本 stream_chat <<<\n")
    main()
    
    print("\n>>> 测试异步版本 stream_chat <<<\n")
    asyncio.run(async_main())