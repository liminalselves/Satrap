from satrap.core.database import LiteVectorDB
from satrap.core.APICall.EmbedCall import Embedding, AsyncEmbedding
from satrap.core.utils.text_utils import TextSplitter
import asyncio, aiofiles, os, traceback

from satrap.core.log import logger

class LiteVectorRAG:
    """基于 LiteVector 的 RAG 实现"""
    def __init__(
        self,
        base_url: str,
        api_key: str,
        embed_model: str,
        persist_directory: str = "./lite_vector_rag",
        default_vectorstore_name: str = "default",
        k_default: int = 4,
        chunk_size: int = 128,
        chunk_overlap: int = 32,
        threshold: float = 0.5,
        batch_size: int = 32,
    ):
        """
        初始化 LiteVectorRAG 系统

        参数:
        - base_url: API 基础 URL
        - api_key: API 密钥
        - embed_model: 嵌入模型名称
        - persist_directory: 持久化目录
        - default_vectorstore_name: 默认向量存储名称
        - k_default: 默认返回文档数量
        - chunk_size: 文本分块大小
        - chunk_overlap: 文本分块重叠大小
        - threshold: 相似度阈值
        - batch_size: 批量处理大小
        """
        self.persist_directory = persist_directory
        self.default_vectorstore_name = default_vectorstore_name
        self.k_default = k_default
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.threshold = threshold
        self.batch_size = batch_size

        self.embeddings = Embedding(
            model=embed_model,
            api_key=api_key,   # type: ignore
            base_url=base_url,
        )   # 初始化嵌入模型

        self.vector_db = LiteVectorDB(persist_directory)
        self.vector_db.create_collection(default_vectorstore_name)
        # 初始化向量数据库

        logger.info("LiteVector RAG 系统初始化完成")

    async def simple_query(
        self,
        query: str,
        k: int | None = None,
        threshold: float | None = None
    ) -> list[str] | None:
        """简单查询
        
        参数:
        - query: 查询字符串
        - k: 返回文档数量
        - threshold: 相似度阈值

        返回:
        - list[str] | None: 文档内容列表或 None
        """
        if k is None:
            k = self.k_default
        if threshold is None:
            threshold = self.threshold

        try:
            query_vector = await asyncio.to_thread(
                self.embeddings.embed, query
            )   # 获取查询向量

            if not query_vector:
                logger.error("查询向量为空")
                return None

            results = await asyncio.to_thread(
                self.vector_db.search,
                self.default_vectorstore_name,
                query_vector,   # type: ignore
                k,
                threshold,
            )   # 搜索相似文档

            text_contents = [result['document'] for result in results]
            logger.info(f"查询成功: '{query}' -> 返回 {len(text_contents)} 个文档")
            # 提取文本内容

            return text_contents
            
        except Exception as e:
            logger.error(f"查询失败: {e}")
            return None

    async def add_documents(
        self,
        documents: list[str],
        chunk_size: int | None = None,
        chunk_overlap: int | None = None,
        batch_size: int | None = None,
        collection_name: str | None = None,
    ) -> bool:
        """添加文档
        
        参数:
        - documents: 文档列表
        - chunk_size: 文本分块大小
        - chunk_overlap: 文本分块重叠大小
        - batch_size: 批量处理大小
        - collection_name: 集合名称
        """
        if chunk_size is None:
            chunk_size = self.chunk_size
        if chunk_overlap is None:
            chunk_overlap = self.chunk_overlap
        if batch_size is None:
            batch_size = self.batch_size

        if collection_name is None:
            collection_name = self.default_vectorstore_name

        try:
            text_splitter = TextSplitter(
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap
            )   # 文本分割器

            splits = await asyncio.to_thread(
                text_splitter.split_documents, documents
            )   # 文本分割

            for i in range(0, len(splits), batch_size):
                batch = splits[i:i + batch_size]
                batch_texts = [doc for doc in batch]
                # 批量生成向量并添加

                batch_vectors = await asyncio.to_thread(
                    self.embeddings.embed, batch_texts
                )   # 批量生成向量

                if not batch_vectors:
                    logger.error(f"批次 {i//batch_size + 1} 生成空向量")
                    continue

                await asyncio.to_thread(
                    self.vector_db.add_to_collection,
                    collection_name,
                    batch,
                    batch_vectors,   # type: ignore
                    [{} for _ in batch]   # 空元数据
                )   # 添加到向量数据库

                logger.debug(f"添加批次 {i//batch_size + 1}: {len(batch)} 个文档")

            logger.info(f"成功添加 {len(splits)} 个文档块")
            return True

        except Exception as e:
            logger.error(f"添加文档失败: {e}")
            return False

    async def get_collection_names(self) -> list[str]:
        """获取所有集合名称"""
        try:
            collections = await asyncio.to_thread(
                self.vector_db.get_collection_names
            )
            logger.debug(f"获取到 {len(collections)} 个集合: {collections}")
            return collections
        except Exception as e:
            logger.error(f"获取集合名称失败: {e}")
            return []

    async def get_vectorstore_overview(self) -> dict:
        """获取向量库概览"""
        try:
            collections = await self.get_collection_names()

            overview = {
                "directory": self.persist_directory,
                "total_collections": len(collections),
                "total_documents": 0,
                "collections": []
            }

            for collection_name in collections:
                stats = await asyncio.to_thread(
                    self.vector_db.get_collection_stats, collection_name
                )
                overview["total_documents"] += stats["document_count"]
                
                collection_info = {
                    "向量库名称": collection_name,
                    "文档数量": stats["document_count"],
                    "状态": "有文档" if stats["document_count"] > 0 else "空"
                }
                overview["collections"].append(collection_info)

            logger.debug(f"获取概览: {overview}")
            return overview

        except Exception as e:
            logger.error(f"获取概览失败: {e}")
            return {
                "error": f"获取概览失败: {str(e)}",
                "directory": self.persist_directory,
                "total_collections": 0,
                "total_documents": 0,
                "collections": []
            }
        
    async def create_collection(self, collection_name: str) -> bool:
        """
        创建一个新的向量集合

        参数:
        - collection_name: 集合名称

        返回:
        - bool: 是否成功创建
        """
        try:
            self.vector_db.create_collection(collection_name)
            logger.info(f"成功创建集合 '{collection_name}'")
            return True

        except Exception as e:
            logger.error(f"创建集合 '{collection_name}' 失败: {e}")
            return False
        
    async def delete_collection(self, collection_name: str) -> bool:
        """
        删除一个向量集合

        参数:
        - collection_name: 集合名称

        返回:
        - bool: 是否成功删除
        """
        try:
            self.vector_db.delete_collection(collection_name)
            logger.info(f"成功删除集合 '{collection_name}'")
            return True

        except Exception as e:
            logger.error(f"删除集合 '{collection_name}' 失败: {e}")
            return False

    async def add_text_file(
        self,
        file_path: str,
        collection_name: str | None = None,
        chunk_size: int | None = None,
        chunk_overlap: int | None = None,
        batch_size: int | None = None
    ) -> bool:
        """
        从文本文件读取内容并添加到向量库
        
        参数:
        - file_path: 文本文件路径
        - collection_name: 目标集合名称，如果为 None 则使用默认集合
        - chunk_size: 分块大小
        - chunk_overlap: 分块重叠大小
        - batch_size: 批量处理大小
        
        返回:
        - bool: 是否成功添加
        """
        if collection_name is None:
            collection_name = self.default_vectorstore_name
        
        if chunk_size is None:
            chunk_size = self.chunk_size
        if chunk_overlap is None:
            chunk_overlap = self.chunk_overlap
        if batch_size is None:
            batch_size = self.batch_size
        
        try:
            if not os.path.exists(file_path):
                logger.error(f"文件不存在: {file_path}")
                return False
            # 检查文件是否存在

            logger.info(f"开始读取文件: {file_path}")

            async with aiofiles.open(file_path, 'r', encoding='utf-8') as file:
                content = await file.read()   # 异步读取文件内容

            if not content.strip():
                logger.warning(f"文件内容为空: {file_path}")
                return True  # 空文件不算错误

            logger.info(f"成功读取文件，内容长度: {len(content)} 字符")

            documents = [content]   # 将内容分割成文档
            text_splitter = TextSplitter(
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                separators=["\n"]
            )   # 文本分割器

            splits = await asyncio.to_thread(
                text_splitter.split_documents, documents
            )   # 文本分割

            if not splits:
                logger.warning(f"文件分割后没有内容块: {file_path}")
                return True

            logger.info(f"文件分割为 {len(splits)} 个内容块")

            added_count = 0
            for i in range(0, len(splits), batch_size):
                batch = splits[i:i + batch_size]
                batch_texts = [doc for doc in batch]
                # 批量生成向量并添加                

                batch_vectors = await asyncio.to_thread(
                    self.embeddings.embed, batch_texts
                )   # 批量生成向量

                await asyncio.to_thread(
                    self.vector_db.add_to_collection,
                    collection_name,
                    batch_texts,
                    batch_vectors,   # type: ignore
                    [{} for _ in batch]   # 空元数据
                )   # 添加到向量数据库
                
                added_count += len(batch)
                logger.debug(f"添加批次 {i//batch_size + 1}: {len(batch)} 个文档块")
            
            logger.info(f"成功从文件 {file_path} 添加 {added_count} 个文档块到集合 '{collection_name}'")
            return True
            
        except UnicodeDecodeError as e:
            logger.error(f"文件编码错误: {file_path} - {e}")
            return False
        except Exception as e:
            error_detail = traceback.format_exc()
            logger.error(f"从文件 {file_path} 添加内容失败: {e}")
            logger.error(f"添加文档失败: {e}\n详细错误信息:\n{error_detail}")
            return False

    async def swift(
            self,
            collection_name: str | None = None,
            add_documents: list[str] | None = None,
            query: str | None = None,
            batch_size: int | None = None,
            chunk_size: int | None = None,
            chunk_overlap: int | None = None,
            K: int | None = None,
            threshold: float | None = None,
        ):
            """
            执行快速 RAG 流程
            
            参数:
            - collection_name: 集合名称
            - add_documents: 要添加的文档列表
            - query: 查询字符串
            - batch_size: 批量处理大小
            - chunk_size: 分块大小
            - chunk_overlap: 分块重叠大小
            - K: 检索文档数量
            - threshold: 相似度阈值
            
            返回:
            - list[str]: 搜索到的文档列表
            - list[float]: 相似度列表
            """
            if collection_name is None:
                collection_name = self.default_vectorstore_name
                
            if batch_size is None:
                batch_size = self.batch_size
            if chunk_size is None:
                chunk_size = self.chunk_size
            if chunk_overlap is None:
                chunk_overlap = self.chunk_overlap
            if K is None:
                K = self.k_default
            if threshold is None:
                threshold = self.threshold

            try:
                # step 1: 如果提供了文档, 先添加文档
                if add_documents is not None and len(add_documents) > 0:
                    logger.info(f"开始添加 {len(add_documents)} 个文档到集合 '{collection_name}'")

                    text_splitter = TextSplitter(
                        chunk_size=chunk_size,
                        chunk_overlap=chunk_overlap
                    )   # 创建文本分割器

                    splits = await asyncio.to_thread(
                        text_splitter.split_documents, add_documents
                    )   # 文本分割
                    

                    for i in range(0, len(splits), batch_size):   # 批量处理文档
                        batch = splits[i:i + batch_size]
                        batch_texts = [doc for doc in batch]

                        batch_vectors = await asyncio.to_thread(
                            self.embeddings.embed, batch_texts
                        )   # 批量生成向量

                        await asyncio.to_thread(
                            self.vector_db.add_to_collection,
                            collection_name,
                            batch_texts,
                            batch_vectors,   # type: ignore
                            [{} for _ in batch]   # 空元数据
                        )   # 添加到向量数据库
                        
                        logger.debug(f"快速流程 - 添加批次 {i//batch_size + 1}: {len(batch)} 个文档块")
                    
                    logger.info(f"快速流程 - 成功添加 {len(splits)} 个文档块")
                
                # step 2: 如果提供了查询, 执行搜索
                documents_list = []
                scores_list = []

                if query is not None and query.strip():
                    logger.info(f"快速流程 - 执行查询: '{query}'")

                    query_vector = await asyncio.to_thread(
                        self.embeddings.embed, query.strip()
                    )   # 生成查询向量

                    results = await asyncio.to_thread(
                        self.vector_db.search,
                        collection_name,
                        query_vector,   # type: ignore
                        K,
                        threshold,
                    )   # 搜索相似文档

                    documents_list = [result['document'] for result in results]
                    scores_list = [result['score'] for result in results]
                    # 提取文档和分数

                    logger.info(f"快速流程 - 查询完成，找到 {len(documents_list)} 个相关文档")

                return documents_list, scores_list

            except Exception as e:
                logger.error(f"快速 RAG 流程执行失败: {e}")
                return [], []