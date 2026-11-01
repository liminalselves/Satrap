# ImageGen / AsyncImageGen

图片调用层支持 OpenAI Images, Seedream 和 NovelAI 的请求格式, 同步与异步类使用同一套参数和结果类型

```python
from satrap import AsyncImageGen, ImageGenParams

async with AsyncImageGen(
    api_key="读取安全配置中的密钥",
    base_url="https://api.openai.com/v1/images/generations",
    edit_base_url="https://api.openai.com/v1/images/edits",
    api_type="openai_images",
    model="填写服务商当前支持的模型 ID",
    default_params=ImageGenParams(size=(1024, 1024)),
) as client:
    result = await client.generate("窗边的一只猫")
    if result is not None:
        image = result.images[0]
        # 由业务层保存 image.data, 对话历史只保留文件引用
```

## 初始化参数

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `api_key` | 必填 | 厂商密钥, NovelAI 使用 Persistent API Token |
| `base_url` | 必填 | 完整生成请求地址, 不拼接, 裁剪或自动推导路径 |
| `api_type` | 必填 | `openai_images`, `seedream`, `novelai`, 仅选择入参构造方式 |
| `model` | 必填 | 默认模型 ID |
| `edit_base_url` | `None` | 完整编辑地址, 省略时原样使用 `base_url` |
| `timeout` | `180` | 单次 HTTP 网络读写超时秒数, 不是整个调用的总期限 |
| `default_params` | `None` | 公共和厂商专有参数的统一默认值 |
| `suppress_error` | `True` | 网络或上游错误返回 `None`, 关闭时抛出脱敏的 `ImageGenError` |
| `lock_api_key` | `True` | 隐藏公开属性中的密钥, 不构成对 Python 进程内部的密钥隔离 |
| `allow_insecure_base_url` | `False` | 显式允许非回环 HTTP 地址, 与现有 APICall 一致 |

生成 POST 不自动重试, 不自动跟随重定向, 不自动拆分成多个生成请求, 不猜测 inpainting 模型名

## 调用参数

```python
result = client.generate(prompt, params=None)
result = client.edit(prompt, images, mask=None, params=None)
```

- `prompt`: 非空正向提示词或编辑指令
- `images`: 编辑使用的非空底图列表, 每张图片显式使用 `ImageInput`
- `mask`: 可选遮罩, 尺寸与第一张底图一致, 统一采用白色编辑, 黑色保留的语义
- `params`: 本次参数, 覆盖实例默认值

异步版本对应 `await client.generate(...)` 和 `await client.edit(...)`, 使用 `async with` 或 `await client.aclose()` 释放连接, 同步版本使用 `with` 或 `client.close()`

```python
from satrap import ImageInput

local = ImageInput.from_path("authorized-input.png")
memory = ImageInput.from_bytes(png_bytes, mime_type="image/png")
remote = ImageInput.from_url("https://example.com/input.png")
```

路径和 URL 均由调用方授权, 库不将模型生成的任意字符串当作路径或 URL, 本地文件读取和异步图片转换在线程中执行

## 参数合并

`ImageGenParams` 包含 `model`, `size`, `n`, `negative_prompt`, `seed`, `output_format`, `native`

- 字段默认 `None`, 表示继承实例配置
- 本次非 `None` 字段覆盖默认值, `0` 和 `False` 不会被误判为缺失
- 空字符串清除默认负向提示词, 空列表清除默认角色或参考图
- 列表整体替换, 不自动追加
- `native` 为上游原生 JSON 对象, 默认值和本次值递归合并, 本次值优先
- 类型化参数构造完请求后补充 `native`, 与已构造字段冲突时抛出 `ValueError`
- 不修改传入的参数对象或字典

`size` 支持像素元组, 或协议认可的字符串, 如 Seedream 的 `2K`; `output_format` 表示图片编码, 不表示 URL, Base64 或 ZIP 传输方式

OpenAI Images 不接受公共 `seed` 和 `negative_prompt`; Seedream 不将 `n` 转换为组图参数, 组图需显式使用 `native` 并自行确认模型支持; NovelAI 输出支持 `png` 或 `webp`

自定义兼容服务的扩展字段通过 `native` 显式发送, 例如:

```python
ImageGenParams(native={"quality": "high", "background": "transparent"})
NovelAIParams(native={"parameters": {"future_parameter": True}})
```

## NovelAI 参数与参考图

`NovelAIParams` 继承公共参数, 另外包含:

- 采样: `sampler`, `steps`, `scale`, `cfg_rescale`, `noise_schedule`
- 图生图: `strength`, `noise`, `extra_noise_seed`, `color_correct`, `add_original_image`
- 其他: `sm`, `sm_dyn`, `dynamic_thresholding`, `skip_cfg_above_sigma`, `params_version`
- 角色: `characters`, `use_coords`, `use_order`
- 参考: `precise_references`, `vibes`

NovelAI 请求在未配置时采用 `832x1216`, 单图, 28 步, `scale=5`, `k_euler_ancestral`, 参数版本 3, 随机 32 位 seed; 调用者可用类型化字段覆盖

```python
from satrap import CharacterPrompt, NovelAIParams, PreciseReference

params = NovelAIParams(
    steps=28,
    seed=123456,
    characters=[
        CharacterPrompt(
            prompt="woman, blue hair",
            negative_prompt="hat",
            centers=[(0.25, 0.5)],
        ),
    ],
    use_coords=True,
    precise_references=[
        PreciseReference(image=local, kind="character", strength=0.8, fidelity=0.7),
        PreciseReference(image=memory, kind="style", strength=0.5),
    ],
)
result = client.generate("2women, park, daytime", params=params)
```

参考类型为 `character`, `style`, `character_style`; 输入在内存中等比缩放并黑边填充到参考画布, 不覆盖原文件, 也不把图片写入参数配置

精准参考当前限 NovelAI 4.5, 不可与 Vibe 同时使用; 多张角色参考是混合参考, 并不自动与多个角色提示词一一绑定

普通图生图调用 `edit()`; 带遮罩时需要通过 `params.model` 显式选择正确的 inpainting 模型, 地址仍使用完整的 `edit_base_url` 或 `base_url`

## 显式 Vibe 编码

Vibe 编码和生成均可能产生费用, `generate()` 不隐式请求编码

```python
from satrap import VibeReference

encoding = client.encode_vibe(
    local,
    base_url="https://image.novelai.net/ai/encode-vibe",
    information_extracted=0.7,
)
if encoding is not None:
    result = client.generate(
        "a peaceful landscape",
        params=NovelAIParams(vibes=[VibeReference(encoding=encoding, strength=0.5)]),
    )
```

异步类也提供 `await client.encode_vibe(...)`; 编码对象保存字节, 模型和信息提取值, 可交给业务层缓存复用, 库不新建编码缓存数据库

## 结果和错误

所有入参协议共用响应解析, 支持常见的 `data[].b64_json`, `data[].url`, `images[].image`, `images[].url`, 图片二进制, ZIP 和 SSE 完成事件

`ImageGenResponse` 包含:

- `images`: 有序图片列表, 每项包含 `data`, `mime_type`, `width`, `height`, 可选 `seed`, `revised_prompt`
- `model`: 上游返回的模型或本次实际请求模型
- `request_id`: 可选上游跟踪 ID
- `usage`: 可选原始用量对象, 不虚构缺失 token 或价格
- `text`: 上游可选文本

返回 URL 会立即下载成字节, 下载不携带生成 API 密钥; ZIP 仅在内存中读取, 不按上游文件名写入文件系统; SSE 忽略中间预览, 只收集完成事件, 当前不提供逐帧迭代接口

空结果, 错误正文和异步任务回执均不视为生成成功, 也不会自动重新提交或轮询任务; 错误日志不包含提示词, URL, 密钥和上游响应正文

库只负责调用和图片解析, 不保存文件, 不新增数据库表, 不把 Base64 写入会话历史, 不自动发送消息; 模型管理页面和绘图工具可在此调用层上接入

## 上游参考

- [OpenAI Images](https://developers.openai.com/api/docs/guides/image-generation)
- [Seedream 图片 API](https://console.volcengine.com/ark/region%3Acn-beijing/docs/image-generation-api)
- [NovelAI API 定义](https://image.novelai.net/docs/doc.json)
- [NovelAI 精准参考](https://docs.novelai.net/en/image/precisereference/)
- [NovelAI Vibe Transfer](https://docs.novelai.net/en/image/vibetransfer/)
