"""
文本嵌入模型 API 调用封装

提供单条和批量文本的同步与异步向量化接口,
统一处理模型配置, 输入校验与嵌入响应提取
"""

from typing import List, Any, cast
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
            _handle_batch_parse_issue(
                "Embedding 条目的 'index' 不是整数", suppress_error
            )
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

        sorted_data = sorted(
            data, key=lambda x: x.index if hasattr(x, "index") else x.get("index", 0)
        )
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
