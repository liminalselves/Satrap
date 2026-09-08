"""
文本重排序模型 API 调用封装

提供查询与候选文档的同步和异步相关性排序,
统一处理模型配置, 返回数量与排序结果解析
"""

from typing import List, Dict, Any, Optional, cast
from typing import Protocol
import math
from satrap.core.log import logger


class _JSONResponse(Protocol):
    """声明同步重排请求实际依赖的最小响应接口"""

    status_code: int

    def json(self) -> object:
        """返回解码后的 JSON 数据"""
        ...


class _RequestsClient(Protocol):
    """声明同步重排请求实际依赖的最小客户端接口"""

    def post(self, url: str, **kwargs: object) -> _JSONResponse:
        """
        发送 POST 请求

        参数:
        - url: 请求地址
        - kwargs: 传递给请求客户端的动态参数
        """
        ...


def parse_rerank_result(
    api_response: Optional[Dict[str, Any]],
    min_score: float = 0.0,
    suppress_error: bool = True,
) -> List[Dict[str, Any]]:
    """
    解析 Rerank API 的输出

    参数:
    - api_response: API 返回的原始 JSON 字典
    - min_score: 最小相关性分数阈值
    - suppress_error: 如果为 True (默认), API 报错时返回空列表而不是抛出异常

    返回:
    - 包含 'text', 'score', 'index' 的字典列表; 如果出错或无结果, 返回空列表
    """

    # Step.1 基础类型检查 (防止 api_response 本身是 None)
    if not api_response or not isinstance(api_response, dict):
        msg = "重排接口响应为空或不是字典格式"
        if suppress_error:
            logger.warning(msg)
            return []

        raise ValueError(msg)

    # Step.2 状态码检查
    status_code = api_response.get("status_code")
    if status_code is not None and status_code != 200:
        error_msg = api_response.get("message", "Unknown Error")
        log_msg = f"重排接口返回错误状态码: {status_code}, 错误信息: {error_msg}"
        if suppress_error:
            logger.error(log_msg)
            return []
        else:
            raise ValueError(log_msg)

    # Step.3 获取 output
    results: list[Any] | None = None
    if "results" in api_response and isinstance(api_response["results"], list):
        results = cast(list[Any], api_response["results"])
    elif "output" in api_response and isinstance(api_response["output"], dict):
        results = cast(dict[str, Any], api_response["output"]).get("results")
    else:
        results = cast(list[Any] | None, api_response.get("results"))
    # 兼容保证

    if not isinstance(results, list):
        if not suppress_error:
            raise ValueError("重排接口响应中缺少有效的 results 数组")
        logger.warning("重排接口响应中未找到有效的 'results' 列表")
        return []

    # Step.4 提取核心数据
    parsed_data: list[dict[str, Any]] = []
    for item in cast(list[Any], results):
        try:
            if not isinstance(item, dict):
                raise ValueError("重排结果项必须是对象")
            item = cast(dict[str, Any], item)
            score = item.get("relevance_score", 0.0)
            if not suppress_error and (
                "relevance_score" not in item
                or isinstance(score, bool)
                or not isinstance(score, (int, float))
                or not math.isfinite(score)
            ):
                raise ValueError("重排结果分数无效")
            position = item.get("index")
            if not suppress_error and (
                isinstance(position, bool)
                or not isinstance(position, int)
                or position < 0
            ):
                raise ValueError("重排结果索引无效")
            if score < min_score:
                continue

            doc = item.get("document")
            if isinstance(doc, dict):
                doc = cast(dict[str, Any], doc)
                text = doc.get("text", "")
            else:
                text = item.get("text", "")
            # 提取 document.text

            parsed_data.append(
                {"text": text, "score": score, "original_index": item.get("index")}
            )

        except Exception as e:
            if not suppress_error:
                raise ValueError("重排结果格式无效") from e
            logger.warning(f"重排接口结果中跳过格式错误项: {e}")
            continue

    # Step.5 确保按分数降序排序
    parsed_data.sort(key=lambda x: x["score"], reverse=True)
    return parsed_data
