# test_search.py
import json
import asyncio
from satrap.expend.search import AsyncSearchTool

async def test():
    tool = AsyncSearchTool(timeout=10)
    result_json = await tool.execute(query="deepsleep v4", max_results=10)
    results = json.loads(result_json)
    if "error" in results:
        print(f"搜索失败: {results['error']}")
    else:
        print(f"成功获取 {len(results)} 条结果:\n")
        for idx, item in enumerate(results, 1):
            print(f"{idx}. {item['title']}")
            print(f"   URL: {item['url']}")
            print(f"   摘要: {item['snippet'][:200]}...\n")

if __name__ == "__main__":
    asyncio.run(test())
