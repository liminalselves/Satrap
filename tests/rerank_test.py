from satrap.core.APICall.ReRankCall import ReRank

reranker = ReRank(
    api_key="",
    base_url="https://api.siliconflow.cn/v1",
    model="BAAI/bge-reranker-v2-m3",
    top_k=5,
    min_score=0.0,
)

results = reranker.call(
        query="什么是量子计算？",
        documents=[
            "量子计算是一种基于量子力学原理的计算方式。",
            "今天的天气真好。",
            "量子比特可以同时处于 0 和 1 的叠加态。"
        ],
        top_k=2
)

for r in results:
    print(f"[{r['score']:.3f}] {r['text']}")
