# 图像, 视频与 PDF 页面输入

## 模型能力配置

LLM 配置中的 `supports_visual_input` 对应界面上的“图像与视频输入”选项, 默认 `False`
旧配置没有该字段时继续作为文本模型使用; 开启后表示该模型配置允许接收图像和原生视频

```python
config = LLMConfig(
    model="your-visual-model",
    supports_visual_input=True,
)
```

此开关声明能力, 不根据模型名称推测能力, 也不会使供应商原本不支持的视频接口变得可用
图像采用 `image_url`, 视频采用 `video_url` 内容片段; 视频需要供应商实现相应兼容协议,
例如 [阿里云百炼兼容接口的视频输入](https://www.alibabacloud.com/help/zh/model-studio/qwen-api-via-openai-chat-completions)
视频不会自动抽帧, 供应商不支持时会报告模型调用错误

## 会话输入

```python
session.run("说明这张图", img_urls=["diagram.png"])
session.run("总结这段视频", video_urls=["demo.mp4"])

await async_session.run("说明这张图", img_urls=["diagram.png"])
await async_session.run("总结这段视频", video_urls=["demo.mp4"])
```

媒体来源可为本地路径, HTTP 地址或 Base64 Data URL
新媒体要求当前模型启用视觉能力; 本地和远程来源在执行前固定为 Data URL,
随后作为用户消息内容保存, 后续工具循环和会话历史继续保留媒体
可恢复执行重放已保存的媒体内容, 不重新读取原文件或下载原链接

切换到文本模型后, 历史媒体仅在请求副本中转成文字占位, 原始历史不被删除
直接调用 LLM 时携带媒体仍要求显式开启能力

单次输入最多 16 个媒体项, 原始媒体总量最多 32 MiB
Chat 上传单文件仍限制为 10 MiB, 媒体附件须位于当前会话的私有上传目录
模型服务本身可能有更小的大小, 时长和分辨率限制
视频预算暂按八张图片的固定成本预留, 不是基于视频时长的精确估算,
实际成本由模型返回的 usage 校准, 长视频仍可能触及服务端上下文限制
Chat 的图片和视频附件可按需预览, 查看历史附件不要求当前模型启用视觉能力

## base_take 的 PDF 读取

`read_document` 保留原有路径和文本长度参数, 增加以下可选参数:

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `mode` | `auto` | `auto` 随模型能力选择, `text` 只读文字, `visual` 返回页面图片和文字 |
| `start_page` | `1` | 起始页码, 从 1 开始 |
| `page_count` | `5` | 本次读取 1 至 5 页 |

```python
read_document(path="报告.pdf", mode="auto", start_page=3, page_count=2)
```

PDF 结果包含总页数, 本次页码范围和下一页位置
没有文本层的页面会明确提示; 视觉模式仍返回页面图片供模型读取
页面图片长边最多 1600 像素, 每次生成的图片总量最多 16 MiB
视觉读取依赖 `pypdfium2`, 同步和异步工具共用同一个处理核心

工具图片在历史中属于工具回复, 不增加用户对话轮次
发送模型请求时, 适配层在同组工具回复全部结束后追加关联的媒体内容,
避免将 Base64 当作普通工具 JSON 文本发送
恢复执行时保留已经完成的工具结果, 不重复读取 PDF

非 PDF 文档继续提取文本, `visual` 模式会明确提示暂不支持
RAG 仍使用原有文本提取接口, 不自动将索引文档渲染为图片
