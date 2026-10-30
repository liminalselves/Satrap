# Responses API 后端支持


## 使用方式

直接使用底层库时不需要 Backend, Control, Chat 服务或模型配置管理器:

```python
from satrap import LLM

llm = LLM(
    api_key="...",
    base_url="https://api.openai.com/v1",
    model="你的模型名称",
    api_type="responses",
)
reply = llm.chat([{"role": "user", "content": "你好"}])
llm.client.close()
```

`AsyncLLM` 接受相同参数, 方法使用 `await`, 流式方法使用 `async for`

`api_type` 仅支持 `chat_completions` 与 `responses`, 默认前者, 初始化时确定; 改协议应创建新实例

模型配置保存相同字段, `build_llm_from_config` 将其传入构造函数; Control 和 Chat 的模型配置 API 均可读写该字段, 旧配置默认使用 Chat Completions

前端模型配置提供请求协议与视频传输方式选择, 也可通过配置 API 或 CLI 的模型配置写入入口设置 `api_type`

## 分层与独立性

- `LLMCall/protocols`: 请求编码, 响应解析和独立流式解码器, 不读配置文件或数据库
- `LLM` / `AsyncLLM`: 选择 SDK 端点, 保留同步与异步生命周期, 网络超时和异常约定
- `LLMCall/observation.py`: 显式注入的可选观察接口及调用用途; 未注入时直接请求 SDK
- `log/debug/hooks.py`: 库组件的可选调试入口; 没有宿主时不加载高级诊断采集与策略
- `ModelWorkflowFramework`, Tool 和 Session 不需要启动后台服务; 持久化路径可显式指定, Workflow 也可仅使用内存

宿主在统计作用域或调试请求作用域内注入观察器, 库组件不反向导入 Backend 或计量采集实现

## 协议行为

- 请求使用 `/v1/responses`, `store=false`, `truncation=disabled`, 由本地管理历史
- `max_tokens` 在发送时映射为 `max_output_tokens`, 包含推理和可见输出预算
- Responses 不默认发送 temperature 或 top_p; 调用方显式设置时按原值发送
- 显式思考强度映射为 `reasoning.effort`; 展示思考优先使用公开摘要, 服务商不生成摘要时 (如 DeepSeek) 使用 reasoning 项的明文 `reasoning_text`, 不解密加密推理资料
- 思考参数分三档: `thinking="default"` (库, Chat, Agent 与 Edictum 的默认) 不发送任何思考参数, 由模型服务决定; `thinking="off"` 明确关闭; 其他值为显式强度
- Responses 不使用 `thinking_fields` 或 `omit_none_thinking_fields`; `off` 发送 `reasoning.effort="none"`, `default` 不发送 `reasoning`, 不支持该强度的模型返回原有错误
- Chat Completions 的 `default` 不发送 extra_body 思考字段; `off` 发送各字段的关闭值, 只应对支持关闭的混合推理模型使用
- `thinking_levels` 可声明 `none` (允许前端显示关闭) 与 `minimal`; 旧配置未保存该列表时前端视为勾选 none 与低中高
- 已移除无供应商使用的 `thinking_level` 请求字段, 旧配置加载时自动剔除
- 自定义函数平铺定义字段; 未声明 strict 的旧工具明确使用 strict=false
- 工具结果通过 call_id 对应 function_call_output, 与输出 Item 的 id 分离
- 普通文本, 图片, 工具返回图片和 JSON 对象输出共用现有调用入口
- `structured_output(format=...)` 保持原有自定义格式提示语义, 不把它误当 JSON Schema
- 暂不接入托管工具, 远端 Conversations, 后台响应任务或 WebSocket

`session_timeout` 和 `network_timeout` 的既有作用范围不变

## Chat 会话温度

`GET /api/chat/conversations/{id}/settings` 返回 `temperature` 和 `request_temperature`, 前者为已保存的覆盖, 后者为下一轮合并后拟发送的值, 不代表供应商实际使用的采样温度

`PATCH` 接受如下三种输入, 使用 Chat 原有鉴权:

| 请求 | 语义 |
| --- | --- |
| `{}` | 保持当前偏好 |
| `{"temperature": null}` | 清除覆盖, 跟随模型配置 |
| `{"temperature": 0.6}` | 保存会话覆盖, 包括合法的 0 |

只接受 0 至 2 的有限数字或 null, 不将字符串或布尔值隐式转为温度

创建和预加载接受同一可空字段, 既有 send 请求中的 temperature 也更新会话偏好; 未传时不修改

接受每轮发送或重试时固定有效温度, 整个工具循环使用同一值; 生成中更新偏好只影响后续轮次, 不重建客户端或会话

优先级为会话覆盖, 模型配置, 协议默认; Responses 最终为 None 时省略参数, Chat Completions 保持原有 0.7 默认

覆盖保存于 conversation_meta.temperature 可空列, 旧库按需升级; 模型切换保留覆盖, Fork 继承当前偏好, 重启与归档恢复保留设置; 未完成任务仍按原配置指纹检查恢复资格

## 原生视频

```yaml
api_type: responses
supports_native_video_input: true
native_video_transport: data_url  # data_url 或 url, 默认 data_url
```

上述参数也可直接传给 LLM / AsyncLLM 构造函数, 不依赖 Backend 或配置管理器; 模型配置 API 读写相同字段

- 原生视频开启时, 内部 video_url 片段编码为 `{"type": "input_video", "video_url": "..."}`
- data_url 使用受控媒体的固定字节, url 使用当前会话媒体引用登记并再次校验的公网来源
- 公网来源按会话保存, 不写入共享原文件的全局元数据; Fork 和归档沿用引用记录, 请求副本中的内部引用标记不会发送到供应商
- url 模式下无公网来源的本地媒体明确失败, 不自动上传或创建媒体服务器; 已登记同内容公网来源的媒体可以复用该来源
- 关闭原生视频但支持图片时继续抽帧, 两种视觉能力均关闭时仍拒绝新媒体
- 工具返回的视频在完整工具结果组之后投影为带来源说明的用户输入, 工具文字和图片仍留在 function_call_output
- 保留现有媒体大小及归属检查, 原媒体仅去重保存一份, 请求和 Debug 不把 Base64 副本写回对话历史
- 不增加 fps 或自动取得 file_id 的流程, video_frame_interval 仍只控制本地抽帧; 服务商拒绝原生输入后不自动改用抽帧重发

公网链接可能变化或到期, 请求错误应如实返回; 只有 Data URL 传输承诺发送本地已冻结的字节

## 历史与恢复

`LLMCallResponse.protocol_payload` 只包含本次输出 Items, 协议标识, 来源摘要和模型身份, 不包含整份请求或认证凭据

助手消息绑定该资料后保存于 chat_history 的可选 protocol_payload JSON 列, 旧表按需增加该列

完整输出项保留顺序, reasoning.encrypted_content, function_call 与 assistant.phase; 清除展示思考不会删除协议资料

消息内容或工具调用被编辑后不再回放旧资料; 来源或模型变化时退回通用文本与工具历史, 不把其他模型的加密资料转发给新模型

该列贯通同步与异步保存, Agent 事务提交, 检查点, Fork, 会话归档及上下文编辑备份恢复

本地 token 估算不将协议资料 JSON 再算成一份可见正文, 使用供应商 usage 校准输入用量

## 流式与诊断

增量只用于展示, 最终工具参数以完整终态为准; response.failed, response.incomplete, error 或未收到终态的断流均不能变成成功工具调用

计量从终态 response.usage 读取, 终态失败仍保存实际返回用量; 取消沿用原观察器的幂等结算与关闭行为

Debug Logger 采集新增正文和工具参数, 终态不重复记录整份流式正文; Responses input 项与 Chat Completions messages 一样使用共享存储和差量浏览

## 验收

- 使用真实 OpenAI SDK 和 HTTP MockTransport 检查实际请求与 SSE 编解码, 不通过伪造 SDK 方法签名验证协议
- 覆盖同步, 异步, 普通, 流式, 工具循环, 图片, JSON 输出和错误返回
- 覆盖加密推理与 phase 回放, 重载, 检查点, Fork, 归档恢复和消息编辑
- 在独立进程禁止导入 Backend, Chat Server 与计量服务, 验证两种协议下 Tool / Workflow / Session / LLMCall 实际运行
- 实际服务商接口需在可用的 Responses 凭据环境另行验收, 本地离线通过不代表第三方接口支持完整规范

2026-10-29 Responses 首版的本地验收记录:

- Windows, Python 3.13.5, OpenAI SDK 2.41.1
- 完整 Python 单元测试: `4045 passed, 8 skipped`, 用时 446.20 秒
- 跳过项: Windows 符号链接权限相关 4 项, POSIX 专用场景 3 项, 显式开启的真实视觉接口测试 1 项
- 变更生产模块 Pyright: 0 errors, 80 warnings; 新增生产模块无诊断
- `git diff --check` 通过, 前端目录无修改
- Linux / macOS 本轮未在本地运行, 真实 Responses 服务商接口尚未验收

关闭思考修正的增量验收: Responses, 兼容协议, 视觉输入与模型配置整合测试 `197 passed, 1 skipped`; 变更生产模块 Pyright 无诊断, 本次未重跑完整测试集

2026-10-29 会话温度与原生视频验收:

- Windows / Python 3.13.5 / OpenAI SDK 2.41.1, 完整 Python 单元测试 `4050 passed, 8 skipped`, 用时 402.61 秒
- 最后的 HTTP 参数类型收紧与初始表结构调整后, 对 Chat 服务, WebSocket, Responses, 视觉, 存储, 模型配置和兼容协议重新验收: `348 passed, 2 skipped`, 用时 87.37 秒
- 覆盖温度在执行中修改仍保留当前请求值, 下一轮和重试采用新值, 清除覆盖, 模型切换与热更新, 保存失败回滚, 重启, Fork 和旧库归档恢复
- 原生视频覆盖同步/异步和普通/流式四条 SDK 请求路径, Data URL, 公网 URL, 工具视频位置, 引用隔离, Fork 来源复制, 本地无公网来源拒绝及投影不重复下载
- 23 个变更及直接关联生产模块 Pyright: 0 errors, 143 warnings; 两个协议适配器及新增或修改代码行无诊断
- 前端目录无修改, `git diff --check` 通过; Linux / macOS 及各供应商真实视频接口本轮未验收

官方参考:

- [Responses 迁移](https://developers.openai.com/api/docs/guides/migrate-to-responses)
- [工具调用](https://developers.openai.com/api/docs/guides/function-calling)
- [推理资料与 phase](https://developers.openai.com/api/docs/guides/reasoning)
- [流式事件](https://developers.openai.com/api/reference/resources/responses/streaming-events)
- [Ark 原生视频类型](https://github.com/volcengine/volcengine-python-sdk/blob/master/volcenginesdkarkruntime/types/responses/response_input_video_param.py)
- [百炼 Responses](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-responses)
- [OpenRouter 视频输入](https://openrouter.ai/docs/guides/overview/multimodal/videos)
