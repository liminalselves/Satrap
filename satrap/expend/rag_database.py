from satrap.core.database import DataBase
from satrap.expend.rag import LiteVectorRAG


class DataBaseRAG(LiteVectorRAG):
    """基于 DataBase(faiss + sqlite) 的 RAG 实现"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        embed_model: str,
        persist_directory: str = "./vector_rag",
        default_vectorstore_name: str = "default",
        k_default: int = 4,
        chunk_size: int = 128,
        chunk_overlap: int = 32,
        threshold: float = 0.5,
        batch_size: int = 32,
    ):
        super().__init__(
            base_url=base_url,
            api_key=api_key,
            embed_model=embed_model,
            persist_directory=persist_directory,
            default_vectorstore_name=default_vectorstore_name,
            k_default=k_default,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            threshold=threshold,
            batch_size=batch_size,
        )

        self.vector_db = DataBase(persist_directory)
        self.vector_db.create_collection(default_vectorstore_name)
