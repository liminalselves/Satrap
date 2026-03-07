import os
import shutil
import time
import random
import numpy as np

from satrap.core.database import LiteVectorDB, DataBase


def rm_tree(path: str):
    if os.path.exists(path):
        shutil.rmtree(path, ignore_errors=True)


def build_dataset(n: int, dim: int, seed: int = 42):
    rng = np.random.default_rng(seed)
    vectors = rng.random((n, dim), dtype=np.float32)
    vectors = vectors / (np.linalg.norm(vectors, axis=1, keepdims=True) + 1e-12)
    documents = [f"doc_{i}" for i in range(n)]
    metadata = [{"id": i, "group": i % 10} for i in range(n)]
    return documents, vectors.tolist(), metadata


def build_queries(q: int, dim: int, seed: int = 7):
    rng = np.random.default_rng(seed)
    queries = rng.random((q, dim), dtype=np.float32)
    queries = queries / (np.linalg.norm(queries, axis=1, keepdims=True) + 1e-12)
    return queries.tolist()


def bench_db(db_cls, persist_path: str, docs, vecs, metas, queries, collection: str, k: int, threshold: float):
    rm_tree(persist_path)

    t0 = time.perf_counter()
    db = db_cls(persist_path)
    t1 = time.perf_counter()

    db.create_collection(collection)
    t2 = time.perf_counter()

    added = db.add_to_collection(collection, docs, vecs, metas)
    t3 = time.perf_counter()

    # warmup
    _ = db.search(collection, queries[0], k=k, threshold=threshold)

    search_latencies = []
    total_hits = 0
    for qv in queries:
        s0 = time.perf_counter()
        res = db.search(collection, qv, k=k, threshold=threshold)
        s1 = time.perf_counter()
        search_latencies.append((s1 - s0) * 1000.0)
        total_hits += len(res)

    t4 = time.perf_counter()

    return {
        "init_ms": (t1 - t0) * 1000.0,
        "create_ms": (t2 - t1) * 1000.0,
        "add_ms": (t3 - t2) * 1000.0,
        "search_total_ms": (t4 - t3) * 1000.0,
        "search_avg_ms": float(np.mean(search_latencies)),
        "search_p95_ms": float(np.percentile(search_latencies, 95)),
        "search_min_ms": float(np.min(search_latencies)),
        "search_max_ms": float(np.max(search_latencies)),
        "added": added,
        "total_hits": int(total_hits),
    }


def fmt(name: str, r: dict):
    print(f"\n[{name}]")
    print(f"init_ms        : {r['init_ms']:.2f}")
    print(f"create_ms      : {r['create_ms']:.2f}")
    print(f"add_ms         : {r['add_ms']:.2f}")
    print(f"search_total_ms: {r['search_total_ms']:.2f}")
    print(f"search_avg_ms  : {r['search_avg_ms']:.4f}")
    print(f"search_p95_ms  : {r['search_p95_ms']:.4f}")
    print(f"search_min_ms  : {r['search_min_ms']:.4f}")
    print(f"search_max_ms  : {r['search_max_ms']:.4f}")
    print(f"added          : {r['added']}")
    print(f"total_hits     : {r['total_hits']}")


def main():
    n = 5000
    dim = 384
    q = 200
    k = 5
    threshold = 0.0

    docs, vecs, metas = build_dataset(n, dim)
    queries = build_queries(q, dim)

    lite = bench_db(
        LiteVectorDB,
        persist_path="./sandbox/bench_lite",
        docs=docs,
        vecs=vecs,
        metas=metas,
        queries=queries,
        collection="bench",
        k=k,
        threshold=threshold,
    )

    faiss_db = bench_db(
        DataBase,
        persist_path="./sandbox/bench_faiss",
        docs=docs,
        vecs=vecs,
        metas=metas,
        queries=queries,
        collection="bench",
        k=k,
        threshold=threshold,
    )

    fmt("LiteVectorDB", lite)
    fmt("DataBase(faiss+sqlite)", faiss_db)

    print("\n[ratio DataBase / LiteVectorDB]")
    print(f"add_ms ratio       : {faiss_db['add_ms'] / lite['add_ms']:.3f}")
    print(f"search_avg ratio   : {faiss_db['search_avg_ms'] / lite['search_avg_ms']:.3f}")
    print(f"search_p95 ratio   : {faiss_db['search_p95_ms'] / lite['search_p95_ms']:.3f}")


if __name__ == "__main__":
    random.seed(42)
    np.random.seed(42)
    main()
