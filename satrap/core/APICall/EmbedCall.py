"""
文本嵌入模型 API 调用封装

提供单条和批量文本的同步与异步向量化接口,
统一处理模型配置, 输入校验与嵌入响应提取
"""
from typing import List, Dict, Any, Optional, Union, Literal, cast, overload
from openai import OpenAI, AsyncOpenAI, APIError

from satrap.core.utils import normalize_openai_base_url
from satrap.core.type import safe_getattr

from satrap.core.log import logger


def _response_field(value: Any, name: str) -> Any:
    """读取对象或字典形式的响应字段"""
    if isinstance(value, dict):
        return cast(dict[str, Any], value).get(name)
    return safe_getattr(value, name)


def _handle_batch_parse_issue(message: str, suppress_error: bool) -> None:
    """
    处理批次响应中的单项格式问题

    参数:
    - message: 错误说明
    - suppress_error: 是否记录警告并继续处理其他响应项
    """
    if suppress_error:
        logger.warning(message)
        return
    raise ValueError(message)


def _parse_embedding_batch_response(
    api_response: Any,
    expected_count: int,
    suppress_error: bool,
) -> List[List[float]]:
    """
    按响应 index 解析单个批次并保留失败项的位置

    参数:
    - api_response: API 返回的 Embedding 响应对象
    - expected_count: 当前批次的输入数量
    - suppress_error: 是否使用空向量占位无效响应项

    返回:
    - 与当前批次输入等长的二维向量列表, 无效位置使用 [] 占位
    """
    results: List[List[float]] = [[] for _ in range(expected_count)]
    if api_response is None:
        _handle_batch_parse_issue("Embedding 接口响应为空", suppress_error)
        return results

    data = _response_field(api_response, "data")
    if not data:
        _handle_batch_parse_issue("Embedding 响应中 'data' 为空", suppress_error)
        return results

    seen_indices: set[int] = set()
    vector_dimension: int | None = None

    for item in data:
        index = _response_field(item, "index")
        if not isinstance(index, int) or isinstance(index, bool):
            _handle_batch_parse_issue("Embedding 条目的 'index' 不是整数", suppress_error)
            continue
        if index < 0 or index >= expected_count:
            _handle_batch_parse_issue(
                f"Embedding 条目的 'index' 越界: {index}",
                suppress_error,
            )
            continue
        if index in seen_indices:
            results[index] = []
            _handle_batch_parse_issue(
                f"Embedding 响应中存在重复 index: {index}",
                suppress_error,
            )
            continue

        seen_indices.add(index)
        embedding = _response_field(item, "embedding")
        if not isinstance(embedding, list) or not embedding:
            _handle_batch_parse_issue(
                f"Embedding 条目 {index} 的向量为空或格式无效",
                suppress_error,
            )
            continue

        try:
            vector = [float(value) for value in cast(list[Any], embedding)]
        except (TypeError, ValueError):
            _handle_batch_parse_issue(
                f"Embedding 条目 {index} 包含非数值向量元素",
                suppress_error,
            )
            continue

        if vector_dimension is None:
            vector_dimension = len(vector)
        elif len(vector) != vector_dimension:
            _handle_batch_parse_issue(
                f"Embedding 条目 {index} 的向量维度不一致",
                suppress_error,
            )
            continue

        results[index] = vector

    missing_count = sum(not vector for vector in results)
    if missing_count:
        _handle_batch_parse_issue(
            f"Embedding 批次缺少 {missing_count}/{expected_count} 个有效向量",
            suppress_error,
        )

    return results


def _align_embedding_dimensions(
    embeddings: List[List[float]],
    expected_dimension: int | None,
    suppress_error: bool,
) -> int | None:
    """
    校验跨批次向量维度并将异常项改为空向量

    参数:
    - embeddings: 当前批次按输入位置排列的向量
    - expected_dimension: 先前批次确定的向量维度
    - suppress_error: 是否使用空向量占位维度异常项

    返回:
    - 当前调用已确定的向量维度
    """
    for index, vector in enumerate(embeddings):
        if not vector:
            continue
        if expected_dimension is None:
            expected_dimension = len(vector)
            continue
        if len(vector) != expected_dimension:
            embeddings[index] = []
            _handle_batch_parse_issue(
                f"Embedding 条目 {index} 的向量维度与先前批次不一致",
                suppress_error,
            )
    return expected_dimension


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
    - 二维浮点数列表, 每个元素的顺序与输入文本顺序一致; 如果出错则返回 []
    """
    if api_response is None:
        msg = "Embedding 接口响应为空"
        if suppress_error:
            logger.warning(msg)
            return []
        raise ValueError(msg)

    try:
        data = safe_getattr(api_response, "data")
        # 提取 data 字段(对象属性或字典)
        if data is None and hasattr(api_response, "get"):
            data = api_response.get("data")

        if not data:
            logger.warning("Embedding 响应中 'data' 为空")
            return []

        sorted_data = sorted(data, key=lambda x: x.index if hasattr(x, "index") else x.get("index", 0))
        # 按索引排序以保证顺序与输入一致

        embeddings: list[Any] = []
        for item in sorted_data:
            embedding = safe_getattr(item, "embedding")
            if embedding is None and isinstance(item, dict):
                embedding = cast(dict[str, Any], item).get("embedding")
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
        allow_insecure_base_url: bool = False,
        max_batch_size: int = 100,
        timeout: int = 60,
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
        - allow_insecure_base_url: 是否显式允许非回环 HTTP API 地址
        - max_batch_size: 单次 API 调用最大处理的文本数量, 默认 100
        - timeout: 请求超时时间(秒), 默认 60
        """
        self.api_key = api_key if lock_api_key else "api key locked"
        self.base_url = normalize_openai_base_url(
            base_url,
            allow_insecure=allow_insecure_base_url,
        )
        self.model = model
        self.dimensions = dimensions
        self.encoding_format = encoding_format
        self.suppress_error = suppress_error
        self.return_false = return_false
        if max_batch_size <= 0:
            raise ValueError("max_batch_size 必须大于 0")
        self.max_batch_size = max_batch_size

        self.client = OpenAI(api_key=api_key, base_url=self.base_url, timeout=timeout)

    @overload
    def embed(
        self,
        texts: str,
        model: Optional[str] = None,
        dimensions: Optional[int] = None,
        encoding_format: Optional[str] = None,
    ) -> List[float] | Literal[False]: ...
    @overload
    def embed(
        self,
        texts: List[str],
        model: Optional[str] = None,
        dimensions: Optional[int] = None,
        encoding_format: Optional[str] = None,
    ) -> List[List[float]] | Literal[False]: ...
    def embed(
        self,
        texts: Union[str, List[str]],
        model: Optional[str] = None,
        dimensions: Optional[int] = None,
        encoding_format: Optional[str] = None,
    ) -> List[float] | List[List[float]] | Literal[False]:
        """
        同步生成文本嵌入向量

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
        expected_dimension: int | None = None
        total_count = len(text_list)
        batch_size = self.max_batch_size

        for i in range(0, total_count, batch_size):   # 每次处理 batch_size 个文本
            batch_texts = text_list[i:i + batch_size]
            batch_num = i // batch_size + 1
            total_batches = (total_count + batch_size - 1) // batch_size

            request_kwargs: dict[str, Any] = {
                "model": target_model,
                "input": batch_texts,
                "encoding_format": target_encoding,
            }
            # 构造请求参数
            if target_dimensions is not None:
                request_kwargs["dimensions"] = target_dimensions

            try:
                response = self.client.embeddings.create(**request_kwargs)
                # 调用 API

                embeddings = _parse_embedding_batch_response(
                    response,
                    len(batch_texts),
                    self.suppress_error,
                )
                # 按输入位置解析响应并为空结果保留占位
                expected_dimension = _align_embedding_dimensions(
                    embeddings,
                    expected_dimension,
                    self.suppress_error,
                )

                if any(not vector for vector in embeddings):
                    logger.warning(f"[Embedding] 第 {batch_num}/{total_batches} 批次包含空结果")
                    if self.return_false:
                        return False

                all_embeddings.extend(embeddings)
                logger.debug(f"[Embedding] 第 {batch_num}/{total_batches} 批次处理完成，共 {len(embeddings)} 条")

            except APIError as e:
                if not self.suppress_error:
                    raise
                logger.error(f"[Embedding] 第 {batch_num}/{total_batches} 批次 API 错误: {e}")
                if self.return_false:
                    return False
                all_embeddings.extend([[] for _ in batch_texts])
                continue

            except Exception as e:
                if not self.suppress_error:
                    raise
                logger.error(f"[Embedding] 第 {batch_num}/{total_batches} 批次调用过程发生未知异常: {e}")
                if self.return_false:
                    return False
                all_embeddings.extend([[] for _ in batch_texts])

        # Step.4 根据输入格式返回
        return all_embeddings[0] if is_single else all_embeddings

    def _empty_return(self, is_single: bool) -> List[float] | List[List[float]] | Literal[False]:
        """
        根据 return_false 配置返回适当的空值

        参数:
        - is_single: 输入是否为单个值

        返回:
        - List[float] | List[List[float]] | Literal[False]: 根据 return_false 配置返回适当的空值
        """
        if self.return_false:
            return False
        return []

    def get_model(self) -> str:
        """
        获取当前 Embed 实例使用的模型名称

        返回:
        - str: 当前 Embed 实例使用的模型名称
        """
        return self.model

    def get_api_key(self) -> str:
        """
        获取当前 Embed 实例的 API Key

        返回:
        - str: 当前 Embed 实例的 API Key
        """
        return self.api_key
    
    def get_base_url(self) -> Optional[str]:
        """
        获取当前 Embed 实例的 Base URL;
        如果未设置则返回 None

        返回:
        - Optional[str]: 当前 Embed 实例的 Base URL
        """
        return self.base_url

    def check_embedding(self):
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
        allow_insecure_base_url: bool = False,
        max_batch_size: int = 100,
        timeout: int = 60,
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
        - allow_insecure_base_url: 是否显式允许非回环 HTTP API 地址
        - max_batch_size: 单次 API 调用最大处理的文本数量, 默认 100
        - timeout: 请求超时时间(秒), 默认 60
        """
        self.api_key = api_key if lock_api_key else "api key locked"
        self.base_url = normalize_openai_base_url(
            base_url,
            allow_insecure=allow_insecure_base_url,
        )
        self.model = model
        self.dimensions = dimensions
        self.encoding_format = encoding_format
        self.suppress_error = suppress_error
        self.return_false = return_false
        if max_batch_size <= 0:
            raise ValueError("max_batch_size 必须大于 0")
        self.max_batch_size = max_batch_size
        self.timeout = timeout

        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=self.base_url,
            timeout=timeout,
        )   # 初始化异步 OpenAI 客户端

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
        expected_dimension: int | None = None
        total_count = len(text_list)
        batch_size = self.max_batch_size

        for i in range(0, total_count, batch_size):   # 每次处理 batch_size 个文本
            batch_texts = text_list[i:i + batch_size]
            batch_num = i // batch_size + 1
            total_batches = (total_count + batch_size - 1) // batch_size

            request_kwargs: dict[str, Any] = {
                "model": target_model,
                "input": batch_texts,
                "encoding_format": target_encoding,
            }
            # 构造请求参数
            if target_dimensions is not None:
                request_kwargs["dimensions"] = target_dimensions

            try:
                # Step.4 异步调用 API
                response = await self.client.embeddings.create(**request_kwargs)

                # Step.5 按输入位置解析响应
                embeddings = _parse_embedding_batch_response(
                    response,
                    len(batch_texts),
                    self.suppress_error,
                )
                expected_dimension = _align_embedding_dimensions(
                    embeddings,
                    expected_dimension,
                    self.suppress_error,
                )

                if any(not vector for vector in embeddings):
                    logger.warning(f"[AsyncEmbedding] 第 {batch_num}/{total_batches} 批次包含空结果")
                    if self.return_false:
                        return False

                all_embeddings.extend(embeddings)
                logger.debug(f"[AsyncEmbedding] 第 {batch_num}/{total_batches} 批次处理完成，共 {len(embeddings)} 条")

            except APIError as e:
                if not self.suppress_error:
                    raise
                logger.error(f"[AsyncEmbedding] 第 {batch_num}/{total_batches} 批次 API 错误: {e}")
                if self.return_false:
                    return False
                all_embeddings.extend([[] for _ in batch_texts])
                continue
            except Exception as e:
                if not self.suppress_error:
                    raise
                logger.error(f"[AsyncEmbedding] 第 {batch_num}/{total_batches} 批次调用过程发生未知异常: {e}")
                if self.return_false:
                    return False
                all_embeddings.extend([[] for _ in batch_texts])

        # Step.6 根据输入格式返回
        return all_embeddings[0] if is_single else all_embeddings

    def _empty_return(self, is_single: bool) -> List[float] | List[List[float]] | Literal[False]:
        """
        根据 return_false 配置返回适当的空值

        参数:
        - is_single: 输入是否为单个值

        返回:
        - List[float] | List[List[float]] | Literal[False]: 根据 return_false 配置返回适当的空值
        """
        if self.return_false:
            return False
        return []

    def get_model(self) -> str:
        """
        获取当前 AsyncEmbedding 实例使用的模型名称

        返回:
        - str: 当前 AsyncEmbedding 实例使用的模型名称
        """
        return self.model

    def get_api_key(self) -> str:
        """
        获取当前 AsyncEmbedding 实例的 API Key

        返回:
        - str: 当前 AsyncEmbedding 实例的 API Key
        """
        return self.api_key
    
    def get_base_url(self) -> Optional[str]:
        """
        获取当前 AsyncEmbedding 实例的 Base URL;
        如果未设置则返回 None

        返回:
        - Optional[str]: 当前 AsyncEmbedding 实例的 Base URL
        """
        return self.base_url

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
