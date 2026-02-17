from typing import List, Dict, Any, Optional
from openai import OpenAI, AsyncOpenAI, APIError
from openai.types.chat.chat_completion import ChatCompletion
from satrap.core.type import LLMCallResponse
import base64
import json
import os

from satrap import logger


def _is_local_file_path(path: str) -> bool:
    """判断字符串是否为本地文件路径
    
    参数:
    - path: 待判断的字符串
    
    返回:
    - 如果是本地文件路径返回 True, 否则返回 False
    """
    if path.startswith("http://") or path.startswith("https://"):
        return False
    # 判断是否为 URL (以 http:// 或 https:// 开头)

    if path.startswith("data:"):
        return False
    # 判断是否为 data URL (base64 编码的图片)

    return True   # 其他情况视为本地文件路径


def _encode_image_to_base64(image_path: str) -> str:
    """将本地图片文件编码为 base64 格式的 data URL
    
    参数:
    - image_path: 本地图片文件路径
    
    返回:
    - base64 编码的 data URL 字符串
    
    异常:
    - FileNotFoundError: 文件不存在
    - ValueError: 不支持的图片格式
    """
    if not os.path.exists(image_path):
        raise FileNotFoundError(f"图片文件不存在: {image_path}")

    # 获取文件扩展名并确定 MIME 类型
    ext = os.path.splitext(image_path)[1].lower()
    mime_types = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
    }
    
    if ext not in mime_types:
        raise ValueError(f"不支持的图片格式: {ext}")

    mime_type = mime_types[ext]

    # 读取文件并编码为 base64
    with open(image_path, "rb") as f:
        image_data = f.read()

    base64_data = base64.b64encode(image_data).decode("utf-8")
    return f"data:{mime_type};base64,{base64_data}"


def _process_image_urls(img_urls: Optional[List[str]]) -> List[Dict[str, Any]]:
    """处理图片 URL 列表, 将本地文件转换为 base64 格式

    参数:
    - img_urls: 图片 URL 列表, 可以是本地文件路径或远程 URL

    返回:
    - OpenAI API 格式的图片内容列表
    """
    if not img_urls:
        return []

    processed_images = []
    for img_url in img_urls:
        if _is_local_file_path(img_url):
            # 本地文件, 转换为 base64
            try:
                data_url = _encode_image_to_base64(img_url)
                processed_images.append({
                    "type": "image_url",
                    "image_url": {"url": data_url}
                })
            except (FileNotFoundError, ValueError) as e:
                logger.warning(f"[图像处理] 跳过无效图片: {e}")
        else:
            # 远程 URL 或 data URL, 直接使用
            processed_images.append({
                "type": "image_url",
                "image_url": {"url": img_url}
            })

    return processed_images


def _build_content_with_images(
    text_content: str | List[Dict[str, Any]],
    img_urls: Optional[List[str]]
) -> List[Dict[str, Any]] | str:
    """构建包含文本和图片的内容
    
    参数:
    - text_content: 文本内容或已有的多模态内容列表
    - img_urls: 图片 URL 列表
    
    返回:
    - 如果有图片, 返回多模态内容列表; 否则返回原始内容
    """
    if not img_urls:
        return text_content
    
    # 如果内容已经是列表格式, 追加图片内容
    if isinstance(text_content, list):
        content = text_content.copy()
        content.extend(_process_image_urls(img_urls))
        return content
    
    # 如果是纯文本, 构建新的多模态内容列表
    content: List[Dict[str, Any]] = [{"type": "text", "text": text_content}]
    content.extend(_process_image_urls(img_urls))
    return content


def parse_chat_response(
    api_response: ChatCompletion | Dict[str, Any],
    suppress_error: bool = True,
) -> str:
    """
    解析 LLM API 对话响应, 提取模型的回复文本

    参数:
    - api_response: API 返回的 ChatCompletion 对象或字典
    - suppress_error: 如果为 True (默认), 解析失败时返回空字符串而不是抛出异常

    返回:
    - 模型的回复文本字符串; 如果出错或无内容, 返回空字符串
    """

    # Step.1 基础有效性检查 (确保响应不为空)
    if api_response is None:
        msg = "LLM 接口响应为空"
        if suppress_error:
            logger.warning(f"[响应处理] {msg}")
            return ""
        raise ValueError(msg)

    try:
        # Step.2 尝试提取 choices (兼容对象属性访问和字典访问)
        choices = getattr(api_response, "choices", None)
        if choices is None and isinstance(api_response, dict):
            choices = api_response.get("choices")
        # 尝试通过属性或字典键获取 choices 列表行

        if not choices or len(choices) == 0:
            logger.warning("[响应处理] LLM 接口响应中 'choices' 列表为空")
            return ""

        # Step.3 提取第一条回复的消息内容
        first_choice = choices[0]

        # 处理 Pydantic 对象或字典格式
        if hasattr(first_choice, "message"):
            content = first_choice.message.content
        elif isinstance(first_choice, dict):
            content = first_choice.get("message", {}).get("content")
        else:
            content = ""
        # 根据返回的数据类型提取 content 字段

        if content is None:
            return ""

        return content.strip()

    # Step.4 异常处理
    except Exception as e:
        logger.error(f"[响应处理] 解析 LLM 响应时发生错误: {e}")
        if suppress_error:
            return ""
        raise e
    
def parse_call_response(
    api_response: ChatCompletion | Dict[str, Any],
    suppress_error: bool = True,
) -> LLMCallResponse:
    """解析 LLM API 调用响应, 判断是否包含函数调用

    参数:
    - api_response: API 返回的 ChatCompletion 对象或字典
    - suppress_error: 如果为 True (默认), 解析失败时返回空字符串而不是抛出异常

    返回:
    - 包含响应类型 (message 或 tools_call), 文本回答与函数调用参数列表 (每个元素包含 name, id, arguments)
    """
    # Step.1 基础有效性检查 (确保响应不为空)
    if api_response is None:
        msg = "LLM 接口响应为空"
        if suppress_error:
            logger.warning(f"[响应处理] {msg}")
            return LLMCallResponse(role="message", content="")
        raise ValueError(msg)

    try:
        # Step.2 尝试提取 choices (兼容对象属性访问和字典访问)
        choices = getattr(api_response, "choices", None)
        if choices is None and isinstance(api_response, dict):
            choices = api_response.get("choices")

        if not choices or len(choices) == 0:
            logger.warning("LLM 接口响应中 'choices' 列表为空")
            return LLMCallResponse(type="message", content="")

        # Step.3 提取第一条回复的消息对象
        first_choice = choices[0]
        message = getattr(first_choice, "message", None)
        if message is None and isinstance(first_choice, dict):
            message = first_choice.get("message")
        
        if message is None:
            return LLMCallResponse(type="message", content="")

        content = getattr(message, "content", None)
        if content is None and isinstance(message, dict):
            content = message.get("content")
        text_content = content.strip() if content else ""
        # 提取文本内容

        # Step.4 检查是否存在工具调用 (tool_calls)
        tool_calls = getattr(message, "tool_calls", None)
        if tool_calls is None and isinstance(message, dict):
            tool_calls = message.get("tool_calls")

        if tool_calls and len(tool_calls) > 0:
            tool_calls_list: list[dict[str, Any]] = []
            
            # 遍历所有工具调用
            for tool_call in tool_calls:
                # 提取工具调用 id
                call_id = getattr(tool_call, "id", None)
                if call_id is None and isinstance(tool_call, dict):
                    call_id = tool_call.get("id", "")
                else:
                    call_id = call_id or ""
                
                function_data = getattr(tool_call, "function", None)
                if function_data is None and isinstance(tool_call, dict):
                    function_data = tool_call.get("function")
                # 提取 function 对象 (兼容对象和字典)

                if function_data:
                    func_name = getattr(function_data, "name", "")
                    if isinstance(function_data, dict):
                        func_name = function_data.get("name", "")
                        # 提取函数名

                    args_str = getattr(function_data, "arguments", "{}")
                    if isinstance(function_data, dict):
                        args_str = function_data.get("arguments", "{}")
                        # 提取参数字符串并解析  
    
                    try:   # 确保参数是字符串后再进行 JSON 解析
                        if isinstance(args_str, str):
                            args_dict = json.loads(args_str)
                        elif isinstance(args_str, dict):
                            args_dict = args_str
                        else:
                            args_dict = {}

                    except json.JSONDecodeError:
                        logger.error(f"[响应处理] 工具调用参数 JSON 解析失败: {args_str}")
                        args_dict = {}

                    call_info = {"name": func_name, "id": call_id, "arguments": args_dict}
                    tool_calls_list.append(call_info)
                    # 封装单个工具调用信息并添加到列表

            if tool_calls_list:
                return LLMCallResponse(type="tools_call", content=text_content, tool_calls=tool_calls_list)

        # Step.5 默认返回普通消息类型
        return LLMCallResponse(type="message", content=text_content)

    # Step.6 异常处理
    except Exception as e:
        logger.error(f"[响应处理] 解析 LLM 响应时发生错误: {e}")
        if suppress_error:
            return LLMCallResponse(type="message", content="")
        raise e


class LLM:
    def __init__(
        self,
        api_key: str,
        base_url: Optional[str] = None,
        model: str = "put-your-model-name-here",
        temperature: float = 0.7,
        top_p: float = 0.95,
        max_tokens: int = 1000,
        suppress_error: bool = True,
        return_false: bool = False,
        lock_api_key: bool = True,
    ):
        """
        [同步版本] LLM API 调用封装

        参数:
        - api_key: API 密钥
        - base_url: API 地址
        - model: 使用的模型名称
        - temperature: 生成文本的随机性 (0.0 - 2.0), 默认 0.7
        - top_p: 控制生成文本的多样性 (0.0 - 1.0), 默认 0.95
        - max_tokens: 最大生成 token 数, 默认 1000
        - suppress_error: 是否抑制异常, 默认 True
        - return_false: 启用时发生错误返回 false 而非空字符串
        - lock_api_key: 是否锁定 API Key 的获取以防止泄露, 默认 True
        """
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.api_key = api_key if lock_api_key else "api key locked"
        self.model = model
        self.base_url = base_url
        self.temperature = temperature
        self.top_p = top_p
        self.max_tokens = max_tokens
        self.suppress_error = suppress_error
        self.return_false = return_false

    def chat(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        thinking: bool = False,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str | bool:
        """
        同步发送对话请求

        参数:
        - messages: 消息列表, 格式 [{"role": "user", "content": "..."}]
        - model: 可选参数, 用于覆盖默认模型
        - thinking: 是否启用思考
        - temperature: 可选参数, 用于覆盖默认温度
        - top_p: 可选参数, 用于覆盖默认 top_p
        - max_tokens: 可选参数, 用于覆盖默认最大 token 数

        返回:
        - 模型回复的字符串内容; 如果出错, 根据配置返回空字符串或 False
        """
        target_model = model if model else self.model
        use_temp = temperature if temperature is not None else self.temperature
        use_top_p = top_p if top_p is not None else self.top_p
        use_max_tokens = max_tokens if max_tokens is not None else self.max_tokens

        if not messages:
            logger.warning("对话输入 messages 为空")
            return "" if not self.return_false else False

        try:
            # Step.1 同步调用 API
            response = self.client.chat.completions.create(
                model=target_model,
                messages=messages,   # type: ignore
                temperature=use_temp,
                top_p=use_top_p,
                max_tokens=use_max_tokens,
                extra_body={"thinking": {"type": "enabled"}} if thinking else None,
            )   # 发起网络请求

            # Step.2 解析结果
            return parse_chat_response(response, self.suppress_error)

        except APIError as e:
            if not self.suppress_error:
                raise e
            logger.error(f"[LLM] LLM API 错误: {e}")
            return "" if not self.return_false else False
        except Exception as e:
            if not self.suppress_error:
                raise e
            logger.error(f"[LLM] 调用过程发生未知异常: {e}")
            return "" if not self.return_false else False

    def stream_chat(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        thinking: bool = False,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ):
        """
        同步流式发送对话请求 (生成器)

        参数:
        - messages: 消息列表, 格式 [{"role": "user", "content": "..."}]
        - model: 可选参数, 用于覆盖默认模型
        - thinking: 是否启用思考
        - temperature: 可选参数, 用于覆盖默认温度
        - top_p: 可选参数, 用于覆盖默认 top_p
        - max_tokens: 可选参数, 用于覆盖默认最大 token 数

        返回:
        - 生成器, 每次 yield 一个文本片段; 如果出错, 根据配置返回空字符串或 False
        """
        target_model = model if model else self.model
        use_temp = temperature if temperature is not None else self.temperature
        use_top_p = top_p if top_p is not None else self.top_p
        use_max_tokens = max_tokens if max_tokens is not None else self.max_tokens

        if not messages:
            logger.warning("对话输入 messages 为空")
            yield "" if not self.return_false else False
            return

        try:
            # Step.1 同步流式调用 API
            stream = self.client.chat.completions.create(
                model=target_model,
                messages=messages,   # type: ignore
                temperature=use_temp,
                top_p=use_top_p,
                max_tokens=use_max_tokens,
                extra_body={"thinking": {"type": "enabled"}} if thinking else None,
                stream=True,
            )   # 发起流式网络请求

            # Step.2 逐步 yield 内容
            for chunk in stream:
                if chunk.choices and len(chunk.choices) > 0:
                    delta = chunk.choices[0].delta
                    content = getattr(delta, "content", None)
                    if content:
                        yield content

        except APIError as e:
            if not self.suppress_error:
                raise e
            logger.error(f"[LLM] LLM API 错误: {e}")
            yield "" if not self.return_false else False
        except Exception as e:
            if not self.suppress_error:
                raise e
            logger.error(f"[LLM] 调用过程发生未知异常: {e}")
            yield "" if not self.return_false else False

    def structured_output(self,
            messages: List[Dict[str, str]],
            model: Optional[str] = None,
            temperature: Optional[float] = None,
            top_p: Optional[float] = None,
            max_tokens: Optional[int] = None,
            format: Optional[dict] = None,
        ) -> str | bool:
            """
            同步调用 LLM 并要求结构化输出

            参数:
            - messages: 消息列表, 格式 [{"role": "user", "content": "..."}]
            - model: 可选参数, 用于覆盖默认模型
            - temperature: 可选参数, 用于覆盖默认温度
            - top_p: 可选参数, 用于覆盖默认 top_p
            - max_tokens: 可选参数, 用于覆盖默认最大 token 数
            - format: 用于指定输出格式的字典提示, 示例如下:

            {
                "a": "str | 对参数的描述",
                "x": "int",
                "y": "float",
                "z": "bool",
                "m": {
                    "k": "str",
                    "v": "int"
                },   # object 类型
                "n": "enum"
            }

            返回:
            - 模型回复的字符串内容; 如果出错, 根据配置返回空字符串或 False
            """
            import json

            target_model = model if model else self.model
            use_temp = temperature if temperature is not None else self.temperature
            use_top_p = top_p if top_p is not None else self.top_p
            use_max_tokens = max_tokens if max_tokens is not None else self.max_tokens

            if not messages:
                logger.warning("对话输入 messages 为空")
                return "" if not self.return_false else False

            # Step.1 处理格式化提示词
            processed_messages = [m.copy() for m in messages]
            # 创建消息列表的深拷贝以避免修改原始数据

            if format:
                format_str = json.dumps(format, ensure_ascii=False, indent=2)
                system_instruction = f"\n请严格按照以下 JSON 格式输出结果, 不要包含 Markdown 标记或其他多余文本:\n{format_str}"
                
                if processed_messages and processed_messages[0].get("role") == "system":
                    processed_messages[0]["content"] += f"\n\n{system_instruction}"
                    # 如果已有 system prompt, 则追加格式要求
                else:
                    processed_messages.insert(0, {"role": "system", "content": system_instruction})
                    # 如果没有 system prompt, 则插入一条新的

            try:
                # Step.2 同步调用 API
                response = self.client.chat.completions.create(
                    model=target_model,
                    messages=processed_messages,   # type: ignore
                    temperature=use_temp,
                    top_p=use_top_p,
                    max_tokens=use_max_tokens,
                    response_format={"type": "json_object"},
                )   # 使用注入了格式提示的消息列表发起请求

                # Step.3 解析结果
                return parse_chat_response(response, self.suppress_error)

            except APIError as e:
                if not self.suppress_error:
                    raise e
                logger.error(f"[LLM] LLM API 错误: {e}")
                return "" if not self.return_false else False
            except Exception as e:
                if not self.suppress_error:
                    raise e
                logger.error(f"[LLM] 调用过程发生未知异常: {e}")
                return "" if not self.return_false else False

    def call(
        self,
        messages: List[Dict[str, str | list]],
        model: Optional[str] = None,
        thinking: bool = False,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        max_tokens: Optional[int] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: str = "auto",
        img_urls: Optional[List[str]] = None,
    ) -> LLMCallResponse | bool:
        """同步调用 LLM 并返回响应

        参数:
        - messages: 消息列表, 格式 [{"role": "user", "content": "..."}]
        - model: 可选参数, 用于覆盖默认模型
        - thinking: 是否要求模型进行思考, 默认为 False
        - temperature: 可选参数, 用于覆盖默认温度
        - top_p: 可选参数, 用于覆盖默认 top_p
        - max_tokens: 可选参数, 用于覆盖默认最大 token 数
        - tools: 可选参数, 工具定义列表, 用于 Function Calling
        - tool_choice: 工具选择策略, 可选 "auto", "none", 或 {"type": "function", "function": {"name": "工具名"}}
        - img_urls: 可选参数, 图片 URL 列表, 支持本地文件路径和远程 URL

        返回:
        - 包含响应类型 (message 或 tools_call), 文本回答与函数调用参数 (字典格式); 如果出错, 根据配置返回空字符串或 False
        """
        target_model = model if model else self.model
        use_temp = temperature if temperature is not None else self.temperature
        use_top_p = top_p if top_p is not None else self.top_p
        use_max_tokens = max_tokens if max_tokens is not None else self.max_tokens

        if not messages:
            logger.warning("对话输入 messages 为空")
            return LLMCallResponse(type="message", content="") if not self.return_false else False

        # Step.1 处理图片 URL, 构建多模态消息
        processed_messages = [m.copy() for m in messages]
        if img_urls:
            # 找到最后一条用户消息并添加图片
            for i in range(len(processed_messages) - 1, -1, -1):
                if processed_messages[i].get("role") == "user":
                    original_content = processed_messages[i].get("content", "")
                    processed_messages[i]["content"] = _build_content_with_images(
                        original_content, img_urls
                    )
                    break

        try:
            # Step.2 同步调用 API
            if tools is not None:
                response = self.client.chat.completions.create(
                    model=target_model,
                    messages=processed_messages if img_urls else messages,   # type: ignore
                    temperature=use_temp,
                    top_p=use_top_p,
                    max_tokens=use_max_tokens,
                    extra_body={"thinking": {"type": "enabled"}} if thinking else None,
                    tools=tools,               # type: ignore
                    tool_choice=tool_choice,   # type: ignore
                )   # 发起网络请求

            else:
                response = self.client.chat.completions.create(
                    model=target_model,
                    messages=processed_messages if img_urls else messages,   # type: ignore
                    temperature=use_temp,
                    top_p=use_top_p,
                    max_tokens=use_max_tokens,
                    extra_body={"thinking": {"type": "enabled"}} if thinking else None,
                )   # 发起网络请求

            # Step.3 解析结果
            return parse_call_response(response, self.suppress_error)

        except APIError as e:
            if not self.suppress_error:
                raise e
            logger.error(f"[LLM] LLM API 错误: {e}")
            return LLMCallResponse(type="message", content="") if not self.return_false else False
        except Exception as e:
            if not self.suppress_error:
                raise e
            logger.error(f"[LLM] 调用过程发生未知异常: {e}")
            return LLMCallResponse(type="message", content="") if not self.return_false else False

    def get_model(self) -> str:
        """获取当前 LLM 实例使用的模型名称"""
        return self.model

    def get_api_key(self) -> str:
        """获取当前 LLM 实例的 API Key"""
        return self.api_key

    def get_base_url(self) -> Optional[str]:
        """获取当前 LLM 实例的 Base URL;
        如果未设置则返回 None"""
        return self.base_url
    
    def set_parameters(
        self,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ):
        """更新 LLM 实例的默认参数设置

        参数:
        - model: 新的模型名称
        - temperature: 新的温度参数
        - top_p: 新的 top_p 参数
        - max_tokens: 新的最大 token 数"""
        if model is not None:
            self.model = model
        if temperature is not None:
            self.temperature = temperature
        if top_p is not None:
            self.top_p = top_p
        if max_tokens is not None:
            self.max_tokens = max_tokens


class AsyncLLM:
    def __init__(
        self,
        api_key: str,
        base_url: Optional[str] = None,
        model: str = "put-your-model-name-here",
        temperature: float = 0.7,
        top_p: float = 0.95,
        max_tokens: int = 1000,
        suppress_error: bool = True,
        return_false: bool = False,
        lock_api_key: bool = True,
    ):
        """
        [异步版本] LLM API 调用封装

        参数:
        - api_key: API 密钥
        - base_url: API 地址
        - model: 模型名称
        - temperature: 生成文本的随机性 (0.0 - 2.0), 默认 0.7
        - top_p: 控制生成文本的多样性 (0.0 - 1.0), 默认 0.95
        - max_tokens: 最大生成 token 数, 默认 1000
        - suppress_error: 是否抑制 API 调用中的异常, 默认 True
        - return_false: 启用时发生错误返回 false 而非空字符串
        - lock_api_key: 是否锁定 API Key 的获取以防止泄露, 默认 True
        """
        self.api_key = api_key if lock_api_key else "api key locked"
        self.model = model
        self.base_url = base_url
        self.temperature = temperature
        self.top_p = top_p
        self.max_tokens = max_tokens
        self.suppress_error = suppress_error
        self.return_false = return_false

        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url
        )  # 初始化异步 OpenAI 客户端

    async def chat(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        thinking: bool = False,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str | bool:
        """
        调用 LLM 进行对话生成

        参数:
        - messages: 对话历史列表, 格式如 [{"role": "user", "content": "..."}]
        - model: 临时覆盖初始化时的模型名称
        - thinking: 是否启用思考
        - temperature: 覆盖初始化时的温度参数
        - top_p: 覆盖初始化时的 top_p 参数
        - max_tokens: 覆盖初始化时的最大生成 token 数

        返回:
        - 助手回复的文本内容; 如果出错且 return_false 为 True, 则返回 False
        """
        # Step.1 准备调用参数 (使用默认值或覆盖值)
        use_model = model if model else self.model
        use_temp = temperature if temperature is not None else self.temperature
        use_top_p = top_p if top_p is not None else self.top_p
        use_max_tokens = max_tokens if max_tokens is not None else self.max_tokens

        if not messages:
            logger.warning("对话输入 messages 为空")
            return "" if not self.return_false else False

        try:
            # Step.2 异步调用 OpenAI 接口
            response = await self.client.chat.completions.create(
                model=use_model,
                messages=messages,   # type: ignore
                temperature=use_temp,
                top_p=use_top_p,
                max_tokens=use_max_tokens,
                extra_body={"thinking": {"type": "enabled"}} if thinking else None,
            )   # 发起网络请求并等待结果

            # Step.3 解析并返回结果
            return parse_chat_response(
                api_response=response,
                suppress_error=self.suppress_error
            )

        except APIError as e:
            # Step.4 API 层面错误的特定处理
            err_msg = f"[AsyncLLM] LLM API 返回错误: {e}"
            if not self.suppress_error:
                raise e
            logger.error(err_msg)
            return ""

        except Exception as e:
            # Step.5 其他未知异常处理 (如网络连接失败)
            err_msg = f"[AsyncLLM] 调用过程发生未知异常: {e}"
            if not self.suppress_error:
                raise e
            logger.error(err_msg)
            return "" if not self.return_false else False

    async def stream_chat(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        thinking: bool = False,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ):
        """
        异步流式发送对话请求 (异步生成器)

        参数:
        - messages: 消息列表, 格式 [{"role": "user", "content": "..."}]
        - model: 可选参数, 用于覆盖默认模型
        - thinking: 是否启用思考
        - temperature: 可选参数, 用于覆盖默认温度
        - top_p: 可选参数, 用于覆盖默认 top_p
        - max_tokens: 可选参数, 用于覆盖默认最大 token 数

        返回:
        - 异步生成器, 每次 yield 一个文本片段; 如果出错, 根据配置返回空字符串或 False
        """
        target_model = model if model else self.model
        use_temp = temperature if temperature is not None else self.temperature
        use_top_p = top_p if top_p is not None else self.top_p
        use_max_tokens = max_tokens if max_tokens is not None else self.max_tokens

        if not messages:
            logger.warning("对话输入 messages 为空")
            yield "" if not self.return_false else False
            return

        try:
            # Step.1 异步流式调用 API
            stream = await self.client.chat.completions.create(
                model=target_model,
                messages=messages,   # type: ignore
                temperature=use_temp,
                top_p=use_top_p,
                max_tokens=use_max_tokens,
                extra_body={"thinking": {"type": "enabled"}} if thinking else None,
                stream=True,
            )   # 发起异步流式网络请求

            # Step.2 逐步 yield 内容
            async for chunk in stream:
                if chunk.choices and len(chunk.choices) > 0:
                    delta = chunk.choices[0].delta
                    content = getattr(delta, "content", None)
                    if content:
                        yield content

        except APIError as e:
            if not self.suppress_error:
                raise e
            logger.error(f"[AsyncLLM] LLM API 错误: {e}")
            yield "" if not self.return_false else False
        except Exception as e:
            if not self.suppress_error:
                raise e
            logger.error(f"[AsyncLLM] 调用过程发生未知异常: {e}")
            yield "" if not self.return_false else False

    async def structured_output(
            self,
            messages: List[Dict[str, str]],
            model: Optional[str] = None,
            temperature: Optional[float] = None,
            top_p: Optional[float] = None,
            max_tokens: Optional[int] = None,
            format: Optional[dict] = None,
        ) -> str | bool:
            """
            异步调用 LLM 并要求结构化输出

            参数:
            - messages: 消息列表, 格式 [{"role": "user", "content": "..."}]
            - model: 可选参数, 用于覆盖默认模型
            - temperature: 可选参数, 用于覆盖默认温度
            - top_p: 可选参数, 用于覆盖默认 top_p
            - max_tokens: 可选参数, 用于覆盖默认最大 token 数
            - format: 用于指定输出格式的字典提示, 示例如下:

            {
                "a": "str | 对参数的描述",
                "x": "int",
                "y": "float",
                "z": "bool",
                "m": {
                    "k": "str",
                    "v": "int"
                },   # object 类型
                "n": "enum"
            }

            返回:
            - 模型回复的字符串内容; 如果出错, 根据配置返回空字符串或 False
            """
            import json

            target_model = model if model else self.model
            use_temp = temperature if temperature is not None else self.temperature
            use_top_p = top_p if top_p is not None else self.top_p
            use_max_tokens = max_tokens if max_tokens is not None else self.max_tokens

            if not messages:
                logger.warning("对话输入 messages 为空")
                return "" if not self.return_false else False

            # Step.1 处理格式化提示词
            processed_messages = [m.copy() for m in messages]
            # 创建消息列表的深拷贝以避免修改原始数据

            if format:
                format_str = json.dumps(format, ensure_ascii=False, indent=2)
                system_instruction = f"\n请严格按照以下 JSON 格式输出结果, 不要包含 Markdown 标记或其他多余文本:\n{format_str}"
                
                if processed_messages and processed_messages[0].get("role") == "system":
                    processed_messages[0]["content"] += f"\n\n{system_instruction}"
                    # 如果已有 system prompt, 则追加格式要求
                else:
                    processed_messages.insert(0, {"role": "system", "content": system_instruction})
                    # 如果没有 system prompt, 则插入一条新的

            try:
                # Step.2 异步调用 API
                response = await self.client.chat.completions.create(
                    model=target_model,
                    messages=processed_messages,   # type: ignore
                    temperature=use_temp,
                    top_p=use_top_p,
                    max_tokens=use_max_tokens,
                    response_format={"type": "json_object"},
                )   # 使用注入了格式提示的消息列表发起请求

                # Step.3 解析结果
                return parse_chat_response(response, self.suppress_error)

            except APIError as e:
                if not self.suppress_error:
                    raise e
                logger.error(f"[AsyncLLM] LLM API 错误: {e}")
                return "" if not self.return_false else False
            except Exception as e:
                if not self.suppress_error:
                    raise e
                logger.error(f"[AsyncLLM] 调用过程发生未知异常: {e}")
                return "" if not self.return_false else False

    async def call(
        self,
        messages: List[Dict[str, str | List[Dict[str, Any]]]],
        model: Optional[str] = None,
        thinking: bool = False,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        max_tokens: Optional[int] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: str = "auto",
        img_urls: Optional[List[str]] = None,
    ) -> LLMCallResponse | bool:
        """异步调用 LLM 并返回响应
        
        参数:
        - messages: 消息列表, 格式 [{"role": "user", "content": "..."}]
        - model: 可选参数, 用于覆盖默认模型
        - thinking: 是否要求模型进行思考, 默认为 False
        - temperature: 可选参数, 用于覆盖默认温度
        - top_p: 可选参数, 用于覆盖默认 top_p
        - max_tokens: 可选参数, 用于覆盖默认最大 token 数
        - tools: 可选参数, 工具定义列表, 用于 Function Calling
        - tool_choice: 工具选择策略, 可选 "auto", "none", 或 {"type": "function", "function": {"name": "工具名"}}
        - img_urls: 可选参数, 图片 URL 列表, 支持本地文件路径和远程 URL

        返回:
        - 包含响应类型 (message 或 tools_call), 文本回答与函数调用参数 (字典格式); 如果出错, 根据配置返回空字符串或 False
        """
        target_model = model if model else self.model
        use_temp = temperature if temperature is not None else self.temperature
        use_top_p = top_p if top_p is not None else self.top_p
        use_max_tokens = max_tokens if max_tokens is not None else self.max_tokens

        if not messages:
            logger.warning("对话输入 messages 为空")
            return LLMCallResponse(type="message", content="") if not self.return_false else False

        # Step.1 处理图片 URL, 构建多模态消息
        processed_messages = [m.copy() for m in messages]
        if img_urls:
            # 找到最后一条用户消息并添加图片
            for i in range(len(processed_messages) - 1, -1, -1):
                if processed_messages[i].get("role") == "user":
                    original_content = processed_messages[i].get("content", "")
                    processed_messages[i]["content"] = _build_content_with_images(
                        original_content, img_urls
                    )
                    break

        try:
            # Step.2 异步调用 API
            if tools is not None:
                response = await self.client.chat.completions.create(
                    model=target_model,
                    messages=processed_messages if img_urls else messages,   # type: ignore
                    temperature=use_temp,
                    top_p=use_top_p,
                    max_tokens=use_max_tokens,
                    extra_body={"thinking": {"type": "enabled"}} if thinking else None,
                    tools=tools,               # type: ignore
                    tool_choice=tool_choice,   # type: ignore
                )   # 发起异步网络请求

            else:
                response = await self.client.chat.completions.create(
                    model=target_model,
                    messages=processed_messages if img_urls else messages,   # type: ignore
                    temperature=use_temp,
                    top_p=use_top_p,
                    max_tokens=use_max_tokens,
                    extra_body={"thinking": {"type": "enabled"}} if thinking else None,
                )   # 发起异步网络请求

            # Step.3 解析结果
            return parse_call_response(response, self.suppress_error)

        except APIError as e:
            if not self.suppress_error:
                raise e
            logger.error(f"[AsyncLLM] LLM API 错误: {e}")
            return LLMCallResponse(type="message", content="") if not self.return_false else False
        except Exception as e:
            if not self.suppress_error:
                raise e
            logger.error(f"[AsyncLLM] 调用过程发生未知异常: {e}")
            return LLMCallResponse(type="message", content="") if not self.return_false else False

    def get_model(self) -> str:
        """获取当前 AsyncLLM 实例使用的模型名称"""
        return self.model

    def get_api_key(self) -> str:
        """获取当前 AsyncLLM 实例的 API Key"""
        return self.api_key
    
    def get_base_url(self) -> Optional[str]:
        """获取当前 AsyncLLM 实例的 Base URL;
        如果未设置则返回 None"""
        return self.base_url
    
    def set_parameters(
        self,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ):
        """更新 AsyncLLM 实例的默认参数设置

        参数:
        - model: 新的模型名称
        - temperature: 新的温度参数
        - top_p: 新的 top_p 参数
        - max_tokens: 新的最大 token 数"""
        if model is not None:
            self.model = model
        if temperature is not None:
            self.temperature = temperature
        if top_p is not None:
            self.top_p = top_p
        if max_tokens is not None:
            self.max_tokens = max_tokens
