from openai import AsyncOpenAI, APIError
from typing import List, Any, Optional, Union, Literal, overload
from satrap.core.log import logger

from .base import _EmbeddingBase


class AsyncEmbedding(_EmbeddingBase[AsyncOpenAI]):

    @overload
    async def embed(
        self,
        texts: str,
        model: Optional[str] = None,
        dimensions: Optional[int] = None,
        encoding_format: Optional[str] = None,
    ) -> List[float] | Literal[False]: ...
    @overload
    async def embed(
        self,
        texts: List[str],
        model: Optional[str] = None,
        dimensions: Optional[int] = None,
        encoding_format: Optional[str] = None,
    ) -> List[List[float]] | Literal[False]: ...
    async def embed(
        self,
        texts: Union[str, List[str]],
        model: Optional[str] = None,
        dimensions: Optional[int] = None,
        encoding_format: Optional[str] = None,
    ) -> List[float] | List[List[float]] | Literal[False]:
        """
        异步生成文本嵌入向量

        参数:
        - texts: 单个文本字符串, 或字符串列表
        - model: 可选, 覆盖默认嵌入模型
        - dimensions: 可选, 覆盖默认维度
        - encoding_format: 可选, 覆盖默认编码格式

        返回:
        - 如果输入为单个字符串, 返回一维向量列表 [float];
        - 如果输入为字符串列表, 返回与输入等长的二维向量列表, 失败项使用 [] 占位;
        - 如果出错且 return_false=True, 返回 False;
        - 如果单条输入出错且 return_false=False, 返回 []
        """
        # Step.1 参数合并
        parameters = self._request_parameters(model, dimensions, encoding_format)

        # Step.2 输入标准化: 确保为列表
        is_single = isinstance(texts, str)
        text_list = [texts] if is_single else texts

        if not text_list:
            logger.warning("输入 texts 为空")
            return self._empty_return(is_single)

        # Step.3 分批处理
        all_embeddings: List[List[float]] = []
        expected_dimension: int | None = None
        total_count = len(text_list)
        batch_size = self.max_batch_size

        for i in range(0, total_count, batch_size):  # 每次处理 batch_size 个文本
            batch_texts = text_list[i : i + batch_size]
            batch_num = i // batch_size + 1
            total_batches = (total_count + batch_size - 1) // batch_size

            request_kwargs = {**parameters, "input": batch_texts}

            try:
                # Step.4 异步调用 API
                response = await self.client.embeddings.create(**request_kwargs)

                # Step.5 按输入位置解析响应
                embeddings, expected_dimension = self._parse_batch(
                    response, len(batch_texts), expected_dimension
                )

                if any(not vector for vector in embeddings):
                    logger.warning(
                        f"[AsyncEmbedding] 第 {batch_num}/{total_batches} 批次包含空结果"
                    )
                    if self.return_false:
                        return False

                all_embeddings.extend(embeddings)
                logger.debug(
                    f"[AsyncEmbedding] 第 {batch_num}/{total_batches} 批次处理完成，共 {len(embeddings)} 条"
                )

            except APIError as e:
                if not self.suppress_error:
                    raise
                logger.error(
                    f"[AsyncEmbedding] 第 {batch_num}/{total_batches} 批次 API 错误: {e}"
                )
                if self.return_false:
                    return False
                all_embeddings.extend([[] for _ in batch_texts])
                continue
            except Exception as e:
                if not self.suppress_error:
                    raise
                logger.error(
                    f"[AsyncEmbedding] 第 {batch_num}/{total_batches} 批次调用过程发生未知异常: {e}"
                )
                if self.return_false:
                    return False
                all_embeddings.extend([[] for _ in batch_texts])

        # Step.6 根据输入格式返回
        return all_embeddings[0] if is_single else all_embeddings

    async def check_embedding(self):
        """
        检查嵌入模型是否可用
        返回:
        - 嵌入模型的维度; 如果检查失败则返回 None
        """
        request_kwargs: dict[str, Any] = {
            "model": self.model,
            "input": ["测试文本"],
            "encoding_format": self.encoding_format,
            "dimensions": self.dimensions,
        }
        try:
            response = await self.client.embeddings.create(**request_kwargs)
            dim = len(response.data[0].embedding)
            return dim

        except Exception as e:
            logger.error(f"检查嵌入模型失败: {e}")
            return None

    def _create_client(self, api_key: str, timeout: int) -> AsyncOpenAI:
        self.timeout = timeout
        return AsyncOpenAI(api_key=api_key, base_url=self.base_url, timeout=timeout)
