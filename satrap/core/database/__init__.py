from typing import List, Dict, Any
import numpy as np
import traceback
import aiofiles
import asyncio
import msgpack
import json
import os

from satrap import logger


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