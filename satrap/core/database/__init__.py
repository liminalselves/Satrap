from typing import List, Dict, Any
import numpy as np
import msgpack
import sqlite3
import json
import os

from satrap.core.log import logger


class LiteVectorDB:
    """轻量级向量数据库"""
    def __init__(self, persist_path: str = "./lite_vector"):
        """初始化 LiteVectorDB

        参数:
        - persist_path: 数据持久化路径, 默认 "./lite_vector"
        """

        self.persist_path = persist_path
        os.makedirs(persist_path, exist_ok=True)

        self.collections = {}
        self._key_cache = {}   # 缓存集合键值对

        self._load_from_disk()
        # 内存中的索引

    def _precompute_norms(self):
        """预计算所有集合的向量"""
        _vector = {}   # 向量
        _norm = {}     # 归一化向量模长
        self._key_cache.clear()   # 清空缓存
        for name, collection in self.collections.items():
            vectors = collection['vectors']

            if vectors is not None and len(vectors) > 0:
                vectors_np = np.array(vectors, dtype=np.float16)   # 使用 float16 减少内存
                _vector[name] = vectors_np
                _norm[name] = np.linalg.norm(vectors_np, axis=1, keepdims=True)

                self._key_cache[name] = _vector[name] / _norm[name]   # 归一化向量
                logger.info(f"预计算集合 {name} 的向量模长, 共 {len(vectors)} 个向量")

    def _update_norms(self, name: str):
        """更新集合的向量模长"""
        _vector = {}    # 向量
        _norm = {}      # 归一化向量模长
        collection = self.collections[name]
        vectors = collection['vectors']
        if vectors is not None and len(vectors) > 0:
            _vector[name] = np.array(vectors, dtype=np.float16)   # 使用 float16 减少内存
            _norm[name] = np.linalg.norm(_vector[name], axis=1, keepdims=True)
            self._key_cache[name] = _vector[name] / _norm[name]   # 归一化向量
            logger.info(f"更新集合 {name} 的向量模长, 共 {len(vectors)} 个向量")

    def _load_from_disk(self):
        """从磁盘加载数据"""
        index_file = os.path.join(self.persist_path, "index.msgpack")
        if os.path.exists(index_file):  # 使用 msgpack 格式
            try:
                with open(index_file, 'rb') as f:
                    data = msgpack.unpack(f, raw=False)
                    self.collections: dict = self._to_tensor_format(data)   # type: ignore
                logger.info(f"从磁盘加载 {len(self.collections)} 个集合")
                self._precompute_norms()  # 预计算向量模长

            except Exception as e:
                logger.warning(f"从磁盘加载失败: {e}")
                self.collections = {}

        else:
            logger.warning(f"msgpack 文件不存在: {index_file}, 无法加载数据集合")

    def _save_to_disk(self):
        """保存数据到磁盘"""
        index_file_msgpack = os.path.join(self.persist_path, "index.msgpack")

        try:
            with open(index_file_msgpack, 'wb') as f:
                data = self._to_memory_format(self.collections)
                msgpack.pack(data, f)

        except Exception as e:
            logger.error(f"保存数据失败: {e}")

    def _search_with_numpy(
            self,
            name: str,
            query_vector: List[float],
            k: int,
            threshold: float
        ) -> List[Dict]:
        """使用 numpy 进行向量搜索

        参数:
        - name: 集合名称
        - query_vector: 查询向量
        - k: 返回的文档数量
        - threshold: 相似度阈值

        返回:
        - results: 包含文档, 相似度分数和元数据的列表
        """
        query_np = np.array(query_vector, dtype=np.float16)   # 转换为 numpy
        query_norm = query_np / np.linalg.norm(query_np)      # 归一化查询向量

        vectors_norm = self._key_cache[name]              # 使用缓存的归一化向量
        similarities = np.dot(vectors_norm, query_norm)   # 计算余弦相似度

        if k < len(similarities):   # 如果 k 小于向量数量, 使用部分排序
            indices = np.argpartition(similarities, -k)[-k:]                    # 获取最大的 k 个索引
            sorted_indices = indices[np.argsort(similarities[indices])[::-1]]   # 对这些索引排序

        else:   # 否则使用完全排序
            sorted_indices = np.argsort(similarities)[::-1]   # 降序排序

        results = []
        collection = self.collections[name]

        for idx in sorted_indices:   # 遍历排序后的索引
            score = similarities[idx]
            if score >= threshold and len(results) < k:   # 检查阈值和数量
                results.append({
                    'document': collection['documents'][idx],
                    'score': float(score),
                    'metadata': collection['metadata'][idx]
                })

        return results

    def _to_memory_format(self, data: dict) -> dict:
        """将整个数据库转换为可序列化的内存格式"""
        memory_collections = {}

        for name, collection in data.items():
            # 使用 JSON 序列化/反序列化来强制转换所有数据
            collection_json = json.dumps({
                'documents': collection['documents'],
                'vectors': collection.get('vectors', []),
                'metadata': collection.get('metadata', [])
            }, default=str)   # 使用 default=str 处理无法序列化的类型

            memory_collections[name] = json.loads(collection_json)

        return memory_collections

    def _to_tensor_format(self, data: dict) -> dict:
        """将加载的数据库数据转换为带张量的格式"""
        tensor_db = {}
        for name, collection in data.items():
            vectors_data = collection.get('vectors', [])
            processed_vectors = []
            
            for vec in vectors_data:
                if isinstance(vec, str):
                    # 处理字符串格式的向量数据
                    try:
                        # 移除方括号和多余的空格，然后分割
                        vec_str = vec.strip('[]')
                        # 使用正则表达式分割，处理多个空格的情况
                        import re
                        vec_values = re.split(r'\s+', vec_str.strip())
                        vec_array = np.array([float(v) for v in vec_values if v], dtype=np.float16)
                        processed_vectors.append(vec_array)
                    except (ValueError, AttributeError) as e:
                        logger.warning(f"无法解析向量数据: {vec[:50]}..., 错误: {e}")
                        continue
                elif isinstance(vec, (list, tuple)):
                    # 处理列表格式的向量数据
                    processed_vectors.append(np.array(vec, dtype=np.float16))
                elif isinstance(vec, np.ndarray):
                    # 处理已经是numpy数组的数据
                    processed_vectors.append(vec.astype(np.float16))
                else:
                    logger.warning(f"未知的向量数据格式: {type(vec)}")
                    continue
            
            tensor_db[name] = {
                'documents': list(collection.get('documents', [])),
                'vectors': processed_vectors,
                'metadata': list(collection.get('metadata', []))
            }
            
            if processed_vectors:
                logger.info(f"成功加载集合 '{name}': {len(processed_vectors)} 个向量")
            else:
                logger.warning(f"集合 '{name}' 没有有效的向量数据")
                
        return tensor_db

    def create_collection(self, name: str):
        """创建集合"""
        if name not in self.collections:
            self.collections[name] = {
                'documents': [],
                'vectors': [],
                'metadata': []
            }
            self._save_to_disk()
            logger.info(f"创建集合: {name}")

        else:
            logger.info(f"集合 {name} 已存在")

        return True

    def add_to_collection(
            self,
            name: str, 
            documents: List[str],
            vectors: List[List[float]],
            metadata: List[Dict],
        ):
        """添加文档到集合

        参数:
        - name: 集合名称
        - documents: 文档列表
        - vectors: 向量列表
        - metadata: 元数据列表
        """
        if name not in self.collections:   # 集合不存在时创建
            self.create_collection(name)
            logger.info(f"集合 {name} 在加入数据时创建")

        if metadata is None:   # 如果没有元数据, 默认空字典
            metadata = [{}] * len(documents)

        self.collections[name]['documents'].extend(documents)   # 文档
        self.collections[name]['vectors'].extend(vectors)       # 向量
        self.collections[name]['metadata'].extend(metadata)     # 元数据

        self._update_norms(name)   # 更新集合的向量模长
        self._save_to_disk()
        logger.info(f"向集合 {name} 添加 {len(documents)} 个文档")
        return len(documents)  # 返回添加的文档数量

    def search(
        self,
        name: str,
        query_vector: List[float],
        k: int = 4,
        threshold: float = 0.5
    ) -> List[Dict]:
        """搜索相似文档
        
        参数:
        - name: 集合名称
        - query_vector: 查询向量
        - k: 返回的文档数量
        - threshold: 相似度阈值

        返回:
        - results: 包含文档, 相似度分数和元数据的列表
        """
        if name not in self.collections:  # 检查集合是否存在
            return []
        
        collection = self.collections[name]
        if not collection['vectors']:  # 检查是否有向量
            return []
        
        return self._search_with_numpy(name, query_vector, k, threshold)

    def get_collection_names(self):
        """获取所有集合名称"""
        return list(self.collections.keys())

    def delete_collection(self, name: str):
        """删除集合"""
        if name in self.collections:
            del self.collections[name]   # 删除集合
            del self._key_cache[name]   # 删除缓存的归一化向量

            self._save_to_disk()   # 保存更新后的集合
            logger.info(f"删除集合: {name}")
            return True

        logger.info(f"集合 {name} 不存在, 无需删除")
        return True

    def get_collection_stats(self, name: str):
        """获取集合统计"""
        if name in self.collections:
            return {
                'document_count': len(self.collections[name]['documents']),
                'vector_dimension': len(self.collections[name]['vectors'][0]) if self.collections[name]['vectors'] else 0
            }
        return {'document_count': 0, 'vector_dimension': 0}

class DataBase:
    """使用 faiss + SQLite 的向量数据库"""

    def __init__(self, persist_path: str = "./vector"):
        """初始化 DataBase

        参数:
        - persist_path: 数据持久化路径, 默认 "./vector"
        """
        try:
            import faiss as _faiss
        except Exception as e:
            raise ImportError("未检测到 faiss, 请先安装 faiss-cpu 或 faiss-gpu") from e

        self.faiss = _faiss
        self.persist_path = persist_path
        os.makedirs(self.persist_path, exist_ok=True)

        self.sqlite_path = os.path.join(self.persist_path, "metadata.sqlite")
        self.collection_dims: Dict[str, int] = {}
        self.indices: Dict[str, Any] = {}

        self._init_sqlite()
        self._load_from_disk()

    def _connect(self):
        """创建 SQLite 连接"""
        conn = sqlite3.connect(self.sqlite_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_sqlite(self):
        """初始化 SQLite 表"""
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS collections (
                    name TEXT PRIMARY KEY,
                    dim INTEGER NOT NULL DEFAULT 0
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    collection_name TEXT NOT NULL,
                    document TEXT NOT NULL,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY(collection_name) REFERENCES collections(name) ON DELETE CASCADE
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_documents_collection
                ON documents(collection_name)
            """)
            conn.commit()

    def _index_path(self, name: str) -> str:
        """获取集合索引文件路径"""
        safe_name = name.replace("/", "_").replace("\\", "_").replace(":", "_")
        return os.path.join(self.persist_path, f"{safe_name}.faiss")

    def _create_index(self, dim: int):
        """创建 faiss 索引

        说明:
        - 使用 IndexFlatIP + IndexIDMap2
        - 写入前做 L2 归一化, 以支持余弦相似度检索
        """
        return self.faiss.IndexIDMap2(self.faiss.IndexFlatIP(dim))

    def _save_index(self, name: str):
        """保存单个集合索引到磁盘"""
        index = self.indices.get(name)
        if index is None:
            return
        self.faiss.write_index(index, self._index_path(name))

    def _load_from_disk(self):
        """从 SQLite 和磁盘索引加载集合"""
        with self._connect() as conn:
            rows = conn.execute("SELECT name, dim FROM collections").fetchall()

        for row in rows:
            name = str(row["name"])
            dim = int(row["dim"])
            self.collection_dims[name] = dim

            index_file = self._index_path(name)
            if os.path.exists(index_file):
                try:
                    self.indices[name] = self.faiss.read_index(index_file)
                except Exception as e:
                    logger.warning(f"加载集合 {name} 的 faiss 索引失败: {e}")
                    self.indices[name] = self._create_index(dim) if dim > 0 else None
            else:
                self.indices[name] = self._create_index(dim) if dim > 0 else None

    def create_collection(self, name: str):
        """创建集合"""
        if name not in self.collection_dims:
            with self._connect() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO collections(name, dim) VALUES (?, ?)",
                    (name, 0)
                )
                conn.commit()
            self.collection_dims[name] = 0
            self.indices[name] = None
            logger.info(f"创建集合: {name}")
        else:
            logger.info(f"集合 {name} 已存在")

        return True

    def add_to_collection(
        self,
        name: str,
        documents: List[str],
        vectors: List[List[float]],
        metadata: List[Dict],
    ):
        """添加文档到集合

        参数:
        - name: 集合名称
        - documents: 文档列表
        - vectors: 向量列表
        - metadata: 元数据列表
        """
        if name not in self.collection_dims:
            self.create_collection(name)

        if metadata is None:
            metadata = [{}] * len(documents)

        if not (len(documents) == len(vectors) == len(metadata)):
            raise ValueError("documents、vectors、metadata 长度必须一致")

        if not vectors:
            return 0

        vectors_np = np.array(vectors, dtype=np.float32)
        dim = vectors_np.shape[1]

        if self.collection_dims[name] == 0:
            self.collection_dims[name] = dim
            self.indices[name] = self._create_index(dim)
            with self._connect() as conn:
                conn.execute("UPDATE collections SET dim=? WHERE name=?", (dim, name))
                conn.commit()
        elif self.collection_dims[name] != dim:
            raise ValueError(f"向量维度不一致: 期望 {self.collection_dims[name]}, 实际 {dim}")

        self.faiss.normalize_L2(vectors_np)

        ids = []
        with self._connect() as conn:
            for doc, meta in zip(documents, metadata):
                meta_json = json.dumps(meta if meta is not None else {}, ensure_ascii=False, default=str)
                cursor = conn.execute(
                    "INSERT INTO documents(collection_name, document, metadata) VALUES (?, ?, ?)",
                    (name, doc, meta_json)
                )
                ids.append(cursor.lastrowid)
            conn.commit()

        ids_np = np.array(ids, dtype=np.int64)
        self.indices[name].add_with_ids(vectors_np, ids_np)
        self._save_index(name)
        logger.info(f"向集合 {name} 添加 {len(documents)} 个文档")
        return len(documents)

    def search(
        self,
        name: str,
        query_vector: List[float],
        k: int = 4,
        threshold: float = 0.5
    ) -> List[Dict]:
        """搜索相似文档

        参数:
        - name: 集合名称
        - query_vector: 查询向量
        - k: 返回的文档数量
        - threshold: 相似度阈值

        返回:
        - results: 包含 document、score 和 metadata 的列表
        """
        if name not in self.collection_dims:
            return []

        index = self.indices.get(name)
        if index is None or index.ntotal == 0:
            return []

        query_np = np.array([query_vector], dtype=np.float32)
        self.faiss.normalize_L2(query_np)

        top_k = min(max(k, 1), index.ntotal)
        distances, ids = index.search(query_np, top_k)
        scores = distances[0]
        id_list = ids[0]

        results = []
        with self._connect() as conn:
            for score, doc_id in zip(scores, id_list):
                if doc_id < 0:
                    continue
                if float(score) < threshold:
                    continue

                row = conn.execute(
                    "SELECT document, metadata FROM documents WHERE id=? AND collection_name=?",
                    (int(doc_id), name)
                ).fetchone()
                if row is None:
                    continue

                try:
                    meta_obj = json.loads(row["metadata"]) if row["metadata"] else {}
                except Exception:
                    meta_obj = {}

                results.append({
                    "document": row["document"],
                    "score": float(score),
                    "metadata": meta_obj
                })

        return results

    def get_collection_names(self):
        """获取所有集合名称"""
        return list(self.collection_dims.keys())

    def delete_collection(self, name: str):
        """删除集合"""
        with self._connect() as conn:
            conn.execute("DELETE FROM documents WHERE collection_name=?", (name,))
            conn.execute("DELETE FROM collections WHERE name=?", (name,))
            conn.commit()

        self.collection_dims.pop(name, None)
        self.indices.pop(name, None)

        index_file = self._index_path(name)
        if os.path.exists(index_file):
            try:
                os.remove(index_file)
            except Exception as e:
                logger.warning(f"删除集合 {name} 的索引文件失败: {e}")

        logger.info(f"删除集合: {name}")
        return True

    def get_collection_stats(self, name: str):
        """获取集合统计"""
        if name not in self.collection_dims:
            return {"document_count": 0, "vector_dimension": 0}

        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(1) AS cnt FROM documents WHERE collection_name=?",
                (name,)
            ).fetchone()
            document_count = int(row["cnt"]) if row else 0

        return {
            "document_count": document_count,
            "vector_dimension": int(self.collection_dims.get(name, 0))
        }

