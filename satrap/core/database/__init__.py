"""
轻量向量库与 SQLite 持久化向量库

DataBase 将原始向量与文档原子提交, 使用可重建的 FAISS 版本缓存,
并提供保留备份的旧格式迁移与缺失向量修复入口
"""
from contextlib import closing, contextmanager
import threading
import tempfile
import msgpack
from pathlib import Path
import sqlite3
from weakref import WeakValueDictionary
import shutil
from typing import List, Dict, Any, BinaryIO, cast
import faiss as _faiss
import numpy as np
import json
import uuid
import os
import re

from satrap.core.log import logger


def _msgpack_unpack(file_obj: BinaryIO) -> dict[str, Any]:
    """
    解包 msgpack 索引文件, 断言顶层为字典

    msgpack 库无类型标注, 边界断言集中在此函数

    参数:
    - file_obj: 二进制文件对象

    返回:
    - dict[str, Any]: 解包出的索引数据
    """
    return cast(dict[str, Any], cast(Any, msgpack).unpack(file_obj, raw=False))


def _msgpack_pack(data: dict[str, Any], file_obj: BinaryIO) -> None:
    """
    将索引数据打包写入 msgpack 文件

    msgpack 库无类型标注, 边界断言集中在此函数

    参数:
    - data: 待写入的索引数据
    - file_obj: 二进制文件对象
    """
    cast(Any, msgpack).pack(data, file_obj)


def _validate_vector_batch(
    documents: List[str],
    vectors: List[List[float]],
    metadata: List[Dict[str, Any]],
) -> int | None:
    """
    在写入前校验文档, 向量和元数据的对应关系

    参数:
    - documents: 文档列表
    - vectors: 向量列表
    - metadata: 元数据列表

    返回:
    - 非空批次的向量维度, 空批次返回 None
    """
    if not (len(documents) == len(vectors) == len(metadata)):
        raise ValueError("documents, vectors, metadata 长度必须一致")
    if not vectors:
        return None
    if any(not vector for vector in vectors):
        raise ValueError("向量不能为空")

    dimensions = {len(vector) for vector in vectors}
    if len(dimensions) != 1:
        raise ValueError("同一批次的向量维度必须一致")
    return dimensions.pop()


class LiteVectorDB:
    """轻量级向量数据库"""
    def __init__(self, persist_path: str = ".satrap/lite_vector"):
        """
        初始化 LiteVectorDB

        参数:
        - persist_path: 数据持久化路径, 默认 ".satrap/lite_vector"
        """

        self.persist_path = persist_path
        os.makedirs(persist_path, exist_ok=True)

        self.collections: dict[str, dict[str, Any]] = {}
        self._key_cache: dict[str, Any] = {}   # 缓存集合键值对

        self._load_from_disk()
        # 内存中的索引

    def _precompute_norms(self):
        """预计算所有集合的向量"""
        _vector: dict[str, Any] = {}   # 向量
        _norm: dict[str, Any] = {}   # 归一化向量模长
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
        """
        更新集合的向量模长

        参数:
        - name: 名称
        """
        _vector: dict[str, Any] = {}   # 向量
        _norm: dict[str, Any] = {}   # 归一化向量模长
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
        if os.path.exists(index_file):   # 使用 msgpack 格式
            try:
                with open(index_file, 'rb') as f:
                    data = _msgpack_unpack(f)
                    self.collections: dict[str, dict[str, Any]] = self._to_tensor_format(data)
                logger.info(f"从磁盘加载 {len(self.collections)} 个集合")
                self._precompute_norms()   # 预计算向量模长

            except Exception as e:
                logger.warning(f"从磁盘加载失败: {e}")
                self.collections: dict[str, dict[str, Any]] = {}

        else:
            logger.warning(f"msgpack 文件不存在: {index_file}, 无法加载数据集合")

    def _save_to_disk(self):
        """保存数据到磁盘"""
        index_file_msgpack = os.path.join(self.persist_path, "index.msgpack")

        try:
            with open(index_file_msgpack, 'wb') as f:
                data = self._to_memory_format(self.collections)
                _msgpack_pack(data, f)

        except Exception as e:
            logger.error(f"保存数据失败: {e}")

    def _search_with_numpy(
            self,
            name: str,
            query_vector: List[float],
            k: int,
            threshold: float
        ) -> List[Dict[str, Any]]:
        """
        使用 numpy 进行向量搜索

        参数:
        - name: 集合名称
        - query_vector: 查询向量
        - k: 返回的文档数量
        - threshold: 相似度阈值

        返回:
        - results: 包含文档, 相似度分数和元数据的列表
        """
        query_np = np.array(query_vector, dtype=np.float16)   # 转换为 numpy
        query_norm = query_np / np.linalg.norm(query_np)   # 归一化查询向量

        vectors_norm = self._key_cache[name]   # 使用缓存的归一化向量
        similarities = np.dot(vectors_norm, query_norm)   # 计算余弦相似度

        if k < len(similarities):   # 如果 k 小于向量数量, 使用部分排序
            indices = np.argpartition(similarities, -k)[-k:]   # 获取最大的 k 个索引
            sorted_indices = indices[np.argsort(similarities[indices])[::-1]]   # 对这些索引排序

        else:   # 否则使用完全排序
            sorted_indices = np.argsort(similarities)[::-1]   # 降序排序

        results: list[dict[str, Any]] = []
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

    def _to_memory_format(self, data: dict[str, Any]) -> dict[str, Any]:
        """
        将整个数据库转换为可序列化的内存格式

        参数:
        - data: 输入数据

        返回:
        - dict[str, Any]: 将整个数据库转换为可序列化的内存格式
        """
        memory_collections: dict[str, Any] = {}

        for name, collection in data.items():
            collection_json = json.dumps({
                'documents': collection['documents'],
                'vectors': collection.get('vectors', []),
                'metadata': collection.get('metadata', [])
            }, default=str)   # 使用 default=str 处理无法序列化的类型
            # 使用 JSON 序列化/反序列化来强制转换所有数据

            memory_collections[name] = json.loads(collection_json)

        return memory_collections

    def _to_tensor_format(self, data: dict[str, Any]) -> dict[str, Any]:
        """
        将加载的数据库数据转换为带张量的格式

        参数:
        - data: 输入数据

        返回:
        - dict[str, Any]: 将加载的数据库数据转换为带张量的格式
        """
        tensor_db: dict[str, Any] = {}
        for name, collection in data.items():
            vectors_data = collection.get('vectors', [])
            processed_vectors: list[Any] = []
            
            for vec in vectors_data:
                if isinstance(vec, str):
                    try:
                        vec_str = vec.strip('[]')
                        # 移除方括号和多余的空格, 然后分割
                        # 使用正则表达式分割, 处理多个空格的情况
                        vec_values = re.split(r'\s+', vec_str.strip())
                        vec_array = np.array([float(v) for v in vec_values if v], dtype=np.float16)
                        processed_vectors.append(vec_array)
                    except (ValueError, AttributeError) as e:
                        logger.warning(f"无法解析向量数据: {vec[:50]}..., 错误: {e}")
                        continue
                    # 处理字符串格式的向量数据
                elif isinstance(vec, (list, tuple)):
                    processed_vectors.append(np.array(vec, dtype=np.float16))
                    # 处理列表格式的向量数据
                elif isinstance(vec, np.ndarray):
                    processed_vectors.append(vec.astype(np.float16))
                    # 处理已经是numpy数组的数据
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
        """
        创建集合

        参数:
        - name: 名称

        返回:
        - 创建集合
        """
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
            metadata: List[Dict[str, Any]] | None,
        ):
        """
        添加文档到集合

        参数:
        - name: 集合名称
        - documents: 文档列表
        - vectors: 向量列表
        - metadata: 元数据列表

        返回:
        - 添加文档到集合
        """
        if metadata is None:   # 如果没有元数据, 默认空字典
            metadata = [{} for _ in range(len(documents))]

        dim = _validate_vector_batch(documents, vectors, metadata)
        # 在创建集合和修改内存数据前完成输入校验

        if dim is not None and name in self.collections:
            existing_vectors = cast(List[List[float]], self.collections[name]['vectors'])
            if existing_vectors and len(existing_vectors[0]) != dim:
                raise ValueError(f"向量维度不一致: 期望 {len(existing_vectors[0])}, 实际 {dim}")

        if name not in self.collections:   # 集合不存在时创建
            self.create_collection(name)
            logger.info(f"集合 {name} 在加入数据时创建")

        self.collections[name]['documents'].extend(documents)   # 文档
        self.collections[name]['vectors'].extend(vectors)   # 向量
        self.collections[name]['metadata'].extend(metadata)   # 元数据

        self._update_norms(name)   # 更新集合的向量模长
        self._save_to_disk()
        logger.info(f"向集合 {name} 添加 {len(documents)} 个文档")
        return len(documents)   # 返回添加的文档数量

    def search(
        self,
        name: str,
        query_vector: List[float],
        k: int = 4,
        threshold: float = 0.5
    ) -> List[Dict[str, Any]]:
        """
        搜索相似文档

        参数:
        - name: 集合名称
        - query_vector: 查询向量
        - k: 返回的文档数量
        - threshold: 相似度阈值

        返回:
        - results: 包含文档, 相似度分数和元数据的列表
        """
        if name not in self.collections:   # 检查集合是否存在
            return cast(List[Dict[str, Any]], [])
        
        collection = self.collections[name]
        if not collection['vectors']:   # 检查是否有向量
            return cast(List[Dict[str, Any]], [])
        
        return self._search_with_numpy(name, query_vector, k, threshold)

    def get_collection_names(self):
        """
        获取所有集合名称

        返回:
        - 所有集合名称
        """
        return list(self.collections.keys())

    def delete_collection(self, name: str):
        """
        删除集合

        参数:
        - name: 名称

        返回:
        - 删除集合
        """
        if name in self.collections:
            del self.collections[name]   # 删除集合
            del self._key_cache[name]   # 删除缓存的归一化向量

            self._save_to_disk()   # 保存更新后的集合
            logger.info(f"删除集合: {name}")
            return True

        logger.info(f"集合 {name} 不存在, 无需删除")
        return True

    def get_collection_stats(self, name: str):
        """
        获取集合统计

        参数:
        - name: 名称

        返回:
        - 集合统计
        """
        if name in self.collections:
            return {
                'document_count': len(self.collections[name]['documents']),
                'vector_dimension': len(self.collections[name]['vectors'][0]) if self.collections[name]['vectors'] else 0
            }
        return {'document_count': 0, 'vector_dimension': 0}

_DATABASE_LOCKS: WeakValueDictionary[str, Any] = WeakValueDictionary()
_DATABASE_LOCKS_GUARD = threading.Lock()


class VectorDataUnavailable(RuntimeError):
    """持久化向量缺失或损坏, 需要显式补齐后才能搜索"""


class DataBase:
    """SQLite 保存文档与原始向量, FAISS 仅作为可重建的版本缓存"""

    def __init__(self, persist_path: str = ".satrap/vector"):
        """
        打开新版向量库, 旧格式要求显式维护迁移

        参数:
        - persist_path: 包含 metadata.sqlite 与派生索引的目录
        """
        self.faiss = _faiss
        self.persist_path = os.path.realpath(persist_path)
        os.makedirs(self.persist_path, exist_ok=True)
        self.sqlite_path = os.path.join(self.persist_path, "metadata.sqlite")
        with _DATABASE_LOCKS_GUARD:
            self._lock = _DATABASE_LOCKS.setdefault(self.sqlite_path, threading.RLock())
        self.collection_dims: Dict[str, int] = {}
        self.indices: Dict[str, Any] = {}
        self._index_versions: dict[str, tuple[str, int]] = {}
        self._init_sqlite()
        self._load_from_disk()

    @contextmanager
    def _connect(self):
        """显式关闭连接, 退出时提交或回滚事务"""
        with closing(sqlite3.connect(self.sqlite_path, timeout=30)) as conn, conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn

    def _init_sqlite(self):
        """在写事务内初始化新版表, 不隐式迁移旧库"""
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "collections" in tables:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(collections)")}
                if "collection_id" not in columns:
                    raise RuntimeError("旧向量库需要维护迁移: 停止所有写入者后调用 DataBase.migrate_legacy(path)")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS collections (name TEXT PRIMARY KEY, dim INTEGER NOT NULL DEFAULT 0, "
                "collection_id TEXT NOT NULL UNIQUE, revision INTEGER NOT NULL DEFAULT 0)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS documents (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "collection_name TEXT NOT NULL, document TEXT NOT NULL, metadata TEXT NOT NULL DEFAULT '{}', "
                "vector BLOB, FOREIGN KEY(collection_name) REFERENCES collections(name) ON DELETE CASCADE)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_documents_collection ON documents(collection_name)")
            conn.execute("PRAGMA user_version=2")

    @staticmethod
    def migrate_legacy(persist_path: str) -> dict[str, Any]:
        """
        停止旧写入者后备份并迁移格式, 不访问外部模型或删除旧索引

        参数:
        - persist_path: 需要维护迁移的旧向量库目录

        返回:
        - 备份路径, 各集合恢复统计和缺失文档 ID; 已迁移时返回状态与缺失 ID
        """
        root = Path(persist_path).resolve()
        database = root / "metadata.sqlite"
        if not database.is_file():
            raise ValueError("旧向量库不存在")
        with closing(sqlite3.connect(str(database), timeout=30)) as conn:
            conn.row_factory = sqlite3.Row
            columns = {row[1] for row in conn.execute("PRAGMA table_info(collections)")}
            if "collection_id" in columns:
                missing = [row[0] for row in conn.execute("SELECT id FROM documents WHERE vector IS NULL ORDER BY id")]
                return {"already_migrated": True, "missing_document_ids": missing}
            backup = root / ("migration-backup-" + uuid.uuid4().hex)
            backup.mkdir()
            with closing(sqlite3.connect(str(backup / "metadata.sqlite"))) as saved:
                conn.backup(saved)
            for file in root.glob("*.faiss"):
                if file.is_file() and not file.is_symlink():
                    shutil.copy2(file, backup / file.name)
            report: dict[str, Any] = {"backup": str(backup), "missing_document_ids": [], "collections": {}}
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("ALTER TABLE collections ADD COLUMN collection_id TEXT")
                conn.execute("ALTER TABLE collections ADD COLUMN revision INTEGER NOT NULL DEFAULT 0")
                conn.execute("ALTER TABLE documents ADD COLUMN vector BLOB")
                for row in conn.execute("SELECT name, dim FROM collections").fetchall():
                    name, dim = str(row["name"]), int(row["dim"])
                    conn.execute("UPDATE collections SET collection_id=? WHERE name=?", (uuid.uuid4().hex, name))
                    ids = [int(item[0]) for item in conn.execute(
                        "SELECT id FROM documents WHERE collection_name=? ORDER BY id", (name,),
                    )]
                    legacy = name.replace("/", "_").replace("\\", "_").replace(":", "_") + ".faiss"
                    source = backup / legacy
                    recovered: set[int] = set()
                    if source.is_file() and dim > 0:
                        try:
                            index = _faiss.read_index(str(source))
                            if not isinstance(index, _faiss.IndexIDMap2):
                                raise ValueError("旧索引缺少文档 ID 映射")
                            stored_ids = _faiss.vector_to_array(index.id_map)
                            if index.d != dim or len(set(map(int, stored_ids))) != len(stored_ids):
                                raise ValueError("旧索引维度或文档 ID 无效")
                            for doc_id in set(ids).intersection(map(int, stored_ids)):
                                vector = np.asarray(index.reconstruct(doc_id), dtype="<f4")
                                if vector.size != dim or not np.isfinite(vector).all():
                                    continue
                                conn.execute("UPDATE documents SET vector=? WHERE id=?", (vector.tobytes(), doc_id))
                                recovered.add(doc_id)
                        except Exception as error:
                            report["collections"][name] = {"index_error": str(error)}
                    missing = sorted(set(ids) - recovered)
                    report["missing_document_ids"].extend(missing)
                    report["collections"].setdefault(name, {}).update(
                        recovered=len(recovered), missing_document_ids=missing,
                    )
                conn.execute("CREATE UNIQUE INDEX idx_collections_identity ON collections(collection_id)")
                conn.execute("PRAGMA user_version=2")
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            (backup / "migration-report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8",
            )
            return report

    def _load_from_disk(self):
        """加载集合维度, 索引在首次查询或写入后按版本恢复"""
        with self._lock, self._connect() as conn:
            self.collection_dims = {str(row["name"]): int(row["dim"]) for row in conn.execute("SELECT name, dim FROM collections")}

    @staticmethod
    def _version(row: sqlite3.Row) -> tuple[str, int]:
        return str(row["collection_id"]), int(row["revision"])

    def _version_path(self, version: tuple[str, int]) -> str:
        return os.path.join(self.persist_path, f"v2-{version[0]}-{version[1]}.faiss")

    def _index_path(self, name: str) -> str:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM collections WHERE name=?", (name,)).fetchone()
            if row is None:
                raise ValueError(f"集合不存在: {name}")
            return self._version_path(self._version(row))

    def _create_index(self, dim: int):
        return self.faiss.IndexIDMap2(self.faiss.IndexFlatIP(dim))

    def _ensure_index(self, conn: sqlite3.Connection, name: str, row: sqlite3.Row):
        """从同一数据库快照加载源向量并核对索引版本"""
        version = self._version(row)
        if self._index_versions.get(name) == version and name in self.indices:
            return self.indices[name]
        dim = int(row["dim"])
        source = conn.execute("SELECT id, vector FROM documents WHERE collection_name=? ORDER BY id", (name,)).fetchall()
        if any(item["vector"] is None or len(item["vector"]) != dim * 4 for item in source):
            raise VectorDataUnavailable(f"集合 {name} 存在缺失或损坏向量, 请检查 missing_vector_ids 并显式补齐")
        ids = np.array([item["id"] for item in source], dtype=np.int64)
        index = None
        filename = self._version_path(version)
        if os.path.isfile(filename) and dim > 0:
            try:
                candidate = self.faiss.read_index(filename)
                if isinstance(candidate, _faiss.IndexIDMap2) and candidate.d == dim and np.array_equal(np.sort(self.faiss.vector_to_array(candidate.id_map)), ids):
                    index = candidate
            except Exception as error:
                logger.warning(f"集合 {name} 的派生索引不可用, 从 SQLite 重建: {error}")
        if index is None and dim > 0:
            index = self._create_index(dim)
            if source:
                vectors = np.vstack([np.frombuffer(item["vector"], dtype="<f4") for item in source]).astype(np.float32)
                if not np.isfinite(vectors).all():
                    raise VectorDataUnavailable(f"集合 {name} 的持久化向量包含非有限值")
                self.faiss.normalize_L2(vectors)
                index.add_with_ids(vectors, ids)
        self.collection_dims[name] = dim
        self.indices[name] = index
        self._index_versions[name] = version
        return index

    def _save_index(self, name: str):
        """原子发布带集合身份与修订号的缓存, 过期构建不会覆盖新版本"""
        index = self.indices.get(name)
        version = self._index_versions.get(name)
        if index is None or version is None:
            return
        descriptor, temporary = tempfile.mkstemp(prefix=".faiss-build-", dir=self.persist_path)
        os.close(descriptor)
        try:
            self.faiss.write_index(index, temporary)
            with open(temporary, "r+b") as stream:
                os.fsync(stream.fileno())
            with self._connect() as conn:
                row = conn.execute("SELECT * FROM collections WHERE name=?", (name,)).fetchone()
                if row is not None and self._version(row) == version:
                    os.replace(temporary, self._version_path(version))
                    for old_cache in Path(self.persist_path).glob(f"v2-{version[0]}-*.faiss"):
                        old_revision = old_cache.stem.rsplit("-", 1)[-1]
                        if old_revision.isdigit() and int(old_revision) < version[1] - 1:
                            try:
                                old_cache.unlink()   # 旧读快照仍可用自身 SQLite 源数据重建
                            except OSError:
                                pass   # 另一进程可能仍打开该派生缓存, 留待后续写入清理
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _refresh_after_commit(self, name: str) -> None:
        """SQL 提交后缓存失败只记录降级, 不诱发调用方重复写入"""
        try:
            with self._connect() as conn:
                conn.execute("BEGIN")
                row = conn.execute("SELECT * FROM collections WHERE name=?", (name,)).fetchone()
                if row is None:
                    return
                self._ensure_index(conn, name, row)
            self._save_index(name)
        except Exception as error:
            logger.warning(f"集合 {name} 已持久化, 派生索引暂不可用: {error}")

    def create_collection(self, name: str):
        """
        幂等创建具有独立持久化身份的集合

        参数:
        - name: 外部集合名称, 不用于构造文件名

        返回:
        - 集合存在或创建成功时返回 True
        """
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO collections(name, collection_id) VALUES (?, ?)", (name, uuid.uuid4().hex),
            )
            row = conn.execute("SELECT dim FROM collections WHERE name=?", (name,)).fetchone()
            self.collection_dims[name] = int(row["dim"])
        return True

    def add_to_collection(self, name: str, documents: List[str], vectors: List[List[float]], metadata: List[Dict[str, Any]] | None):
        """
        原子提交文档和向量, 提交后缓存失败仅记录降级

        参数:
        - name: 目标集合名称
        - documents: 按输入顺序保存的文档
        - vectors: 与文档对应的原始向量, 维度必须一致
        - metadata: 对应元数据, None 表示全部使用空对象

        返回:
        - 已提交文档数量, 事务失败抛出异常且不保留部分写入
        """
        if metadata is None:
            metadata = [{} for _ in documents]
        dim = _validate_vector_batch(documents, vectors, metadata)
        if dim is None:
            self.create_collection(name)
            return 0
        vector_array = np.asarray(vectors, dtype="<f4")
        if not np.isfinite(vector_array).all():
            raise ValueError("向量必须包含有限数值")
        serialized = [json.dumps(meta if meta is not None else {}, ensure_ascii=False, default=str) for meta in metadata]
        with self._lock:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT OR IGNORE INTO collections(name, collection_id) VALUES (?, ?)", (name, uuid.uuid4().hex),
                )
                existing = conn.execute("SELECT * FROM collections WHERE name=?", (name,)).fetchone()
                if existing["dim"] not in (0, dim):
                    raise ValueError(f"向量维度不一致: 期望 {existing['dim']}, 实际 {dim}")
                ids: list[int] = []
                previous_version = self._version(existing)
                for document, meta, vector in zip(documents, serialized, vector_array):
                    cursor = conn.execute(
                        "INSERT INTO documents(collection_name, document, metadata, vector) VALUES (?, ?, ?, ?)",
                        (name, document, meta, vector.tobytes()),
                    )
                    if cursor.lastrowid is None:
                        raise RuntimeError("文档插入未返回 ID")
                    ids.append(cursor.lastrowid)
                conn.execute("UPDATE collections SET dim=?, revision=revision+1 WHERE name=?", (dim, name))
            if self._index_versions.get(name) == previous_version and self.indices.get(name) is not None:
                try:
                    normalized = vector_array.copy()
                    self.faiss.normalize_L2(normalized)
                    self.indices[name].add_with_ids(normalized, np.asarray(ids, dtype=np.int64))
                    self._index_versions[name] = (previous_version[0], previous_version[1] + 1)
                except Exception:
                    self._index_versions.pop(name, None)   # 部分缓存更新失败时从完整 SQL 快照重建
            self._refresh_after_commit(name)
        return len(documents)

    def missing_vector_ids(self, name: str) -> list[int]:
        """
        列出缺失或字节长度错误的持久化向量

        参数:
        - name: 需要检查的集合名称

        返回:
        - 按文档 ID 升序排列的待补齐记录
        """
        with self._lock, self._connect() as conn:
            return [int(row[0]) for row in conn.execute(
                "SELECT d.id FROM documents d JOIN collections c ON c.name=d.collection_name "
                "WHERE c.name=? AND (d.vector IS NULL OR length(d.vector) != c.dim*4) ORDER BY d.id", (name,),
            )]

    def repair_missing_vectors(self, name: str, document_ids: list[int], vectors: list[list[float]]) -> None:
        """
        原子补齐缺失向量, 不覆盖已有完整向量

        参数:
        - name: 目标集合名称
        - document_ids: 待补齐的唯一文档 ID
        - vectors: 调用方使用原模型生成且与 ID 对应的向量
        """
        dim = _validate_vector_batch([""] * len(document_ids), vectors, [{}] * len(document_ids))
        if dim is None:
            return
        if len(set(document_ids)) != len(document_ids):
            raise ValueError("文档 ID 不能重复")
        values = np.asarray(vectors, dtype="<f4")
        if not np.isfinite(values).all():
            raise ValueError("向量必须包含有限数值")
        with self._lock:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT dim FROM collections WHERE name=?", (name,)).fetchone()
                if row is None or row["dim"] != dim:
                    raise ValueError("集合不存在或向量维度不一致")
                for doc_id, vector in zip(document_ids, values):
                    changed = conn.execute(
                        "UPDATE documents SET vector=? WHERE id=? AND collection_name=? "
                        "AND (vector IS NULL OR length(vector) != ?)",
                        (vector.tobytes(), doc_id, name, dim * 4),
                    ).rowcount
                    if changed != 1:
                        raise ValueError(f"文档 {doc_id} 不存在, 不属于集合或已有完整向量")
                conn.execute("UPDATE collections SET revision=revision+1 WHERE name=?", (name,))
            self._refresh_after_commit(name)

    def search(self, name: str, query_vector: List[float], k: int = 4, threshold: float = 0.5) -> List[Dict[str, Any]]:
        """
        在同一数据库快照中进行余弦检索, 源向量不完整时明确报错

        参数:
        - name: 目标集合名称
        - query_vector: 与集合维度一致的查询向量
        - k: 最大候选数量
        - threshold: 最小余弦相似度

        返回:
        - 包含 document, score 和 metadata 的相似度降序结果
        """
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN")
            row = conn.execute("SELECT * FROM collections WHERE name=?", (name,)).fetchone()
            if row is None:
                return []
            index = self._ensure_index(conn, name, row)
            if index is None or index.ntotal == 0:
                return []
            query = np.asarray([query_vector], dtype=np.float32)
            if query.ndim != 2 or query.shape[1] != row["dim"] or not np.isfinite(query).all():
                raise ValueError("查询向量维度不一致或包含非有限值")
            self.faiss.normalize_L2(query)
            distances, ids = index.search(query, min(max(k, 1), index.ntotal))
            results: list[dict[str, Any]] = []
            for score, doc_id in zip(distances[0], ids[0]):
                if doc_id < 0 or float(score) < threshold:
                    continue
                document = conn.execute(
                    "SELECT document, metadata FROM documents WHERE id=? AND collection_name=?", (int(doc_id), name),
                ).fetchone()
                if document is None:
                    raise VectorDataUnavailable("索引与数据库快照不一致")
                results.append({"document": document["document"], "score": float(score), "metadata": json.loads(document["metadata"])})
            return results

    def get_collection_names(self):
        """从数据库获取集合名称, 包括其他实例已经提交的集合"""
        with self._lock, self._connect() as conn:
            return [str(row[0]) for row in conn.execute("SELECT name FROM collections ORDER BY rowid")]

    def delete_documents(self, name: str, document_ids: list[int]) -> int:
        """
        原子删除指定文档并更新集合修订号

        参数:
        - name: 限定删除范围的集合名称
        - document_ids: 需要删除的文档 ID

        返回:
        - 实际删除数量, 缓存失败不撤销已提交删除
        """
        with self._lock:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                deleted = sum(conn.execute(
                    "DELETE FROM documents WHERE collection_name=? AND id=?", (name, document_id),
                ).rowcount for document_id in set(document_ids))
                if deleted:
                    conn.execute("UPDATE collections SET revision=revision+1 WHERE name=?", (name,))
            if deleted:
                self._refresh_after_commit(name)
            return deleted

    def source_documents(self, name: str) -> list[dict[str, Any]]:
        """读取 RAG 来源记录, 原文仅保存在首块元数据中"""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT metadata FROM documents WHERE collection_name=? "
                "AND json_extract(metadata, '$.chunk_index')=0 ORDER BY id", (name,),
            ).fetchall()
            return [json.loads(row["metadata"]) for row in rows]

    def replace_source(
        self, name: str, source_id: str, documents: list[str], vectors: list[list[float]], metadata: list[dict[str, Any]],
    ) -> int:
        """在同一事务中替换来源的全部块, 空批次表示删除来源"""
        dim = _validate_vector_batch(documents, vectors, metadata)
        if any(item.get("source_id") != source_id for item in metadata):
            raise ValueError("来源 ID 与元数据不一致")
        array = np.asarray(vectors, dtype="<f4")
        if not np.isfinite(array).all():
            raise ValueError("向量必须包含有限数值")
        with self._lock:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute("INSERT OR IGNORE INTO collections(name, collection_id) VALUES (?, ?)", (name, uuid.uuid4().hex))
                row = conn.execute("SELECT dim FROM collections WHERE name=?", (name,)).fetchone()
                if dim is not None and row["dim"] not in (0, dim):
                    raise ValueError("向量维度不一致, 请重建知识库")
                conn.execute(
                    "DELETE FROM documents WHERE collection_name=? AND json_extract(metadata, '$.source_id')=?",
                    (name, source_id),
                )
                for document, vector, meta in zip(documents, array, metadata):
                    conn.execute(
                        "INSERT INTO documents(collection_name, document, metadata, vector) VALUES (?, ?, ?, ?)",
                        (name, document, json.dumps(meta, ensure_ascii=False, allow_nan=False), vector.tobytes()),
                    )
                conn.execute("UPDATE collections SET dim=?, revision=revision+1 WHERE name=?", (dim or row["dim"], name))
            self._refresh_after_commit(name)
        return len(documents)

    def delete_collection(self, name: str):
        """
        原子删除源数据, 随后清理此集合身份下的派生缓存

        参数:
        - name: 待删除集合名称

        返回:
        - 删除或集合已经不存在时返回 True; 缓存清理失败记录日志
        """
        with self._lock:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT collection_id FROM collections WHERE name=?", (name,)).fetchone()
                conn.execute("DELETE FROM documents WHERE collection_name=?", (name,))
                conn.execute("DELETE FROM collections WHERE name=?", (name,))
            self.collection_dims.pop(name, None)
            self.indices.pop(name, None)
            self._index_versions.pop(name, None)
            if row is not None:
                for file in Path(self.persist_path).glob(f"v2-{row['collection_id']}-*.faiss"):
                    try:
                        file.unlink()
                    except OSError as error:
                        logger.warning(f"集合 {name} 已删除, 缓存清理失败: {error}")
        return True

    def get_collection_stats(self, name: str):
        """
        查询已提交文档数量和集合维度

        参数:
        - name: 目标集合名称

        返回:
        - document_count 与 vector_dimension, 集合不存在时均为 0
        """
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT c.dim, COUNT(d.id) AS cnt FROM collections c LEFT JOIN documents d "
                "ON d.collection_name=c.name WHERE c.name=? GROUP BY c.name", (name,),
            ).fetchone()
            return {"document_count": int(row["cnt"]) if row else 0, "vector_dimension": int(row["dim"]) if row else 0}
