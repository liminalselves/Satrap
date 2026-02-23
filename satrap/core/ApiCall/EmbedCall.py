from typing import List, Dict, Any, Optional, Union
from openai import OpenAI, AsyncOpenAI, APIError
from satrap.core.log import logger

def parse_embedding_response(
    api_response: Any,
    suppress_error: bool = True,
) -> List[List[float]]:
    """
    解析 Embedding API 的响应对象

    参数:
    - api_response: API 返回的 Embedding 响应对象
    - suppress_error: 如果为 True (默认), 解析失败时返回空列表而不是抛出异常

    返回:
    - 二维浮点数列表, 每个元素的顺序与输入文本顺序一致；如果出错则返回 []
    """
    if api_response is None:
        msg = "Embedding 接口响应为空"
        if suppress_error:
            logger.warning(msg)
            return []
        raise ValueError(msg)

    try:
        # 提取 data 字段（对象属性或字典）
        data = getattr(api_response, "data", None)
        if data is None and hasattr(api_response, "get"):
            data = api_response.get("data")

        if not data:
            logger.warning("Embedding 响应中 'data' 为空")
            return []

        # 按索引排序以保证顺序与输入一致
        sorted_data = sorted(data, key=lambda x: x.index if hasattr(x, "index") else x.get("index", 0))

        embeddings = []
        for item in sorted_data:
            embedding = getattr(item, "embedding", None)
            if embedding is None and isinstance(item, dict):
                embedding = item.get("embedding")
            if embedding is not None:
                embeddings.append(embedding)
            else:
                logger.warning("Embedding 条目中缺少 'embedding' 字段")

        return embeddings

    except Exception as e:
        logger.error(f"解析 Embedding 响应时发生错误: {e}")
        if suppress_error:
            return []
        raise e


class Embedding:
    def __init__(
        self,
        api_key: str,
        base_url: Optional[str] = None,
        model: str = "put-your-model-name-here",
        dimensions: Optional[int] = None,
        encoding_format: str = "float",
        suppress_error: bool = True,
        return_false: bool = False,
        lock_api_key: bool = True,
        max_batch_size: int = 100,
    ):
        """
        [同步版本] OpenAI Embedding API 调用封装

        参数:
        - api_key: API 密钥
        - base_url: API 地址
        - model: 使用的嵌入模型名称
        - dimensions: 可选, 输出向量的维度
        - encoding_format: 返回编码格式, 默认为 "float", 可选 "base64"
        - suppress_error: 是否抑制异常, 默认 True
        - return_false: 启用时发生错误返回 False 而非空列表
        - lock_api_key: 是否锁定 API Key 的获取以防止泄露, 默认 True
        - max_batch_size: 单次 API 调用最大处理的文本数量, 默认 100
        """
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.api_key = api_key if lock_api_key else "api key locked"
        self.base_url = base_url
        self.model = model
        self.dimensions = dimensions
        self.encoding_format = encoding_format
        self.suppress_error = suppress_error
        self.return_false = return_false
        self.max_batch_size = max_batch_size

    def embed(
        self,
        texts: Union[str, List[str]],
        model: Optional[str] = None,
        dimensions: Optional[int] = None,
        encoding_format: Optional[str] = None,
    ) -> Union[List[float], List[List[float]], bool]:
        """
        同步生成文本嵌入向量

        参数:
        - texts: 单个文本字符串, 或字符串列表
        - model: 可选, 覆盖默认嵌入模型
        - dimensions: 可选, 覆盖默认维度
        - encoding_format: 可选, 覆盖默认编码格式

        返回:
        - 如果输入为单个字符串, 返回一维向量列表 [float];
        - 如果输入为字符串列表, 返回二维向量列表 List[List[float]], 顺序与输入一致;
        - 如果出错且 return_false=True, 返回 False;
        - 如果出错且 return_false=False, 返回空列表或空二维列表
        """
        # 1. 参数合并
        target_model = model or self.model
        target_dimensions = dimensions if dimensions is not None else self.dimensions
        target_encoding = encoding_format or self.encoding_format

        # 2. 输入标准化: 确保为列表
        is_single = isinstance(texts, str)
        text_list = [texts] if is_single else texts

        if not text_list:
            logger.warning("输入 texts 为空")
            return self._empty_return(is_single)

        # 3. 分批处理
        all_embeddings: List[List[float]] = []
        total_count = len(text_list)
        batch_size = self.max_batch_size

        for i in range(0, total_count, batch_size):   # 每次处理 batch_size 个文本
            batch_texts = text_list[i:i + batch_size]
            batch_num = i // batch_size + 1
            total_batches = (total_count + batch_size - 1) // batch_size

            # 构造请求参数
            request_kwargs = {
                "model": target_model,
                "input": batch_texts,
                "encoding_format": target_encoding,
            }
            if target_dimensions is not None:
                request_kwargs["dimensions"] = target_dimensions

            try:
                # 调用 API
                response = self.client.embeddings.create(**request_kwargs)

                # 解析响应
                embeddings = parse_embedding_response(response, self.suppress_error)

                if not embeddings:
                    logger.warning(f"[Embedding] 第 {batch_num}/{total_batches} 批次返回空结果")
                    return self._empty_return(is_single)

                all_embeddings.extend(embeddings)
                logger.debug(f"[Embedding] 第 {batch_num}/{total_batches} 批次处理完成，共 {len(embeddings)} 条")

            except APIError as e:
                if not self.suppress_error:
                    raise e
                logger.error(f"[Embedding] 第 {batch_num}/{total_batches} 批次 API 错误: {e}")
                return self._empty_return(is_single)
            except Exception as e:
                if not self.suppress_error:
                    raise e
                logger.error(f"[Embedding] 第 {batch_num}/{total_batches} 批次调用过程发生未知异常: {e}")
                return self._empty_return(is_single)

        # 4. 根据输入格式返回
        return all_embeddings[0] if is_single else all_embeddings

    def _empty_return(self, is_single: bool) -> Union[List[float], List[List[float]], bool]:
        """
        根据 return_false 配置返回适当的空值
        """
        if self.return_false:
            return False
        return [] if is_single else [[]]

    def get_model(self) -> str:
        """获取当前 Embed 实例使用的模型名称"""
        return self.model

    def get_api_key(self) -> str:
        """获取当前 Embed 实例的 API Key"""
        return self.api_key
    
    def get_base_url(self) -> Optional[str]:
        """获取当前 Embed 实例的 Base URL;
        如果未设置则返回 None"""
        return self.base_url

    def check_embedding(self):
        """
        检查嵌入模型是否可用
        - return: 嵌入模型的维度; 如果检查失败则返回 None
        """
        request_kwargs = {
            "model": self.model,
            "input": ["测试文本"],
            "encoding_format": self.encoding_format,
            "dimensions": self.dimensions,
        }
        try:
            response = self.client.embeddings.create(**request_kwargs)
            dim = len(response.data[0].embedding)
            return dim

        except Exception as e:
            logger.error(f"检查嵌入模型失败: {e}")
            return None


class AsyncEmbedding:
    def __init__(
        self,
        api_key: str,
        base_url: Optional[str] = None,
        model: str = "put-your-model-name-here",
        dimensions: Optional[int] = None,
        encoding_format: str = "float",
        suppress_error: bool = True,
        return_false: bool = False,
        lock_api_key: bool = True,
        max_batch_size: int = 100,
    ):
        """
        [异步版本] OpenAI Embedding API 调用封装

        参数:
        - api_key: API 密钥
        - base_url: API 地址
        - model: 使用的嵌入模型名称
        - dimensions: 可选, 输出向量的维度
        - encoding_format: 返回编码格式, 默认为 "float", 可选 "base64"
        - suppress_error: 是否抑制异常, 默认 True
        - return_false: 启用时发生错误返回 False 而非空列表
        - lock_api_key: 是否锁定 API Key 的获取以防止泄露, 默认 True
        - max_batch_size: 单次 API 调用最大处理的文本数量, 默认 100
        """
        self.api_key = api_key if lock_api_key else "api key locked"
        self.base_url = base_url
        self.model = model
        self.dimensions = dimensions
        self.encoding_format = encoding_format
        self.suppress_error = suppress_error
        self.return_false = return_false
        self.max_batch_size = max_batch_size

        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url
        )  # 初始化异步 OpenAI 客户端

    async def embed(
        self,
        texts: Union[str, List[str]],
        model: Optional[str] = None,
        dimensions: Optional[int] = None,
        encoding_format: Optional[str] = None,
    ) -> Union[List[float], List[List[float]], bool]:
        """
        异步生成文本嵌入向量

        参数:
        - texts: 单个文本字符串, 或字符串列表
        - model: 可选, 覆盖默认嵌入模型
        - dimensions: 可选, 覆盖默认维度
        - encoding_format: 可选, 覆盖默认编码格式

        返回:
        - 如果输入为单个字符串, 返回一维向量列表 [float];
        - 如果输入为字符串列表, 返回二维向量列表 List[List[float]], 顺序与输入一致;
        - 如果出错且 return_false=True, 返回 False;
        - 如果出错且 return_false=False, 返回空列表或空二维列表
        """
        # Step.1 参数合并
        target_model = model or self.model
        target_dimensions = dimensions if dimensions is not None else self.dimensions
        target_encoding = encoding_format or self.encoding_format

        # Step.2 输入标准化: 确保为列表
        is_single = isinstance(texts, str)
        text_list = [texts] if is_single else texts

        if not text_list:
            logger.warning("输入 texts 为空")
            return self._empty_return(is_single)

        # Step.3 分批处理
        all_embeddings: List[List[float]] = []
        total_count = len(text_list)
        batch_size = self.max_batch_size

        for i in range(0, total_count, batch_size):   # 每次处理 batch_size 个文本
            batch_texts = text_list[i:i + batch_size]
            batch_num = i // batch_size + 1
            total_batches = (total_count + batch_size - 1) // batch_size

            # 构造请求参数
            request_kwargs = {
                "model": target_model,
                "input": batch_texts,
                "encoding_format": target_encoding,
            }
            if target_dimensions is not None:
                request_kwargs["dimensions"] = target_dimensions

            try:
                # Step.4 异步调用 API
                response = await self.client.embeddings.create(**request_kwargs)

                # Step.5 解析响应
                embeddings = parse_embedding_response(response, self.suppress_error)

                if not embeddings:
                    logger.warning(f"[AsyncEmbedding] 第 {batch_num}/{total_batches} 批次返回空结果")
                    return self._empty_return(is_single)

                all_embeddings.extend(embeddings)
                logger.debug(f"[AsyncEmbedding] 第 {batch_num}/{total_batches} 批次处理完成，共 {len(embeddings)} 条")

            except APIError as e:
                if not self.suppress_error:
                    raise e
                logger.error(f"[AsyncEmbedding] 第 {batch_num}/{total_batches} 批次 API 错误: {e}")
                return self._empty_return(is_single)
            except Exception as e:
                if not self.suppress_error:
                    raise e
                logger.error(f"[AsyncEmbedding] 第 {batch_num}/{total_batches} 批次调用过程发生未知异常: {e}")
                return self._empty_return(is_single)

        # Step.6 根据输入格式返回
        return all_embeddings[0] if is_single else all_embeddings

    def _empty_return(self, is_single: bool) -> Union[List[float], List[List[float]], bool]:
        """
        根据 return_false 配置返回适当的空值
        """
        if self.return_false:
            return False
        return [] if is_single else [[]]

    def get_model(self) -> str:
        """获取当前 AsyncEmbedding 实例使用的模型名称"""
        return self.model

    def get_api_key(self) -> str:
        """获取当前 AsyncEmbedding 实例的 API Key"""
        return self.api_key
    
    def get_base_url(self) -> Optional[str]:
        """获取当前 AsyncEmbedding 实例的 Base URL;
        如果未设置则返回 None"""
        return self.base_url

    async def check_embedding(self):
        """
        检查嵌入模型是否可用
        - return: 嵌入模型的维度; 如果检查失败则返回 None
        """
        request_kwargs = {
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
