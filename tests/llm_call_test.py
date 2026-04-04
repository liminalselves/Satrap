from satrap.core.APICall.LLMCall import LLM, AsyncLLM
import asyncio, os, sys

# 将项目根目录添加到 sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# 示例: 初始化机器人 (请替换为实际的 key)
bot = LLM(
    api_key="", 
    base_url="https://api.deepseek.com/v1",
    model="deepseek-reasoner",
    temperature=1.0
)


def main():
    system_prompt = "You are a helpful assistant."
    user_prompt = "你好"
    message = [
    {"role": "system", "content": system_prompt},
    {"role": "user", "content": user_prompt}
    ]
    response = bot.call(messages=message)   # type: ignore
    print("同步调用结果:", response)

# 运行主循环
if __name__ == "__main__":

 main()