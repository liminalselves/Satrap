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

## 命名图片模型与管理页面

模型管理页面的「图片模型」标签使用 `img` 类型, 可以新增, 编辑, 重命名和删除配置, 并使用后端保存的密钥显式测试生成

常用默认参数使用表单控件, 不需要手写 JSON:

- 分辨率: 使用模型默认值, 常用尺寸预设, 自定义宽高像素, 或协议支持的分辨率档位
- 生成数量, 随机或固定种子, 输出格式, 按协议显示可用字段
- NovelAI: 采样步数, 引导强度, 可自定义的采样器, 负向提示词
- NAI 高级区: CFG 重缩放, 噪声调度, SMEA 等三态开关, 图生图修改强度和噪声, 颜色校正
- 多角色区: 添加和移除角色, 编辑角色正负提示词与多个归一化位置

空输入沿用默认行为, 开关可以区分默认, 开启和关闭; 固定种子 0 与关闭值 false 会保留; 随机模式省略 seed, 不发送 -1

表单仍保存为同一个 `default_params` 对象, 不新增平行配置字段; 旧尺寸字符串未编辑时保留原格式; 未提供控件的参数放在折叠的扩展 JSON 中, 未识别字段不自动删除; 表单与扩展 JSON 重复配置同一字段时阻止保存

切换协议保留尚未保存的输入, 对不兼容参数明确提示, 由用户清除或切回原协议; 「清空绘图默认值」清除本次草稿中的全部默认参数, 保存时提交 null

配置沿用模型配置 JSON 文件的命名集合:

```json
{
  "img": {
    "painter": {
      "name": "painter",
      "api_type": "novelai",
      "model": "nai-diffusion-4-5-full",
      "base_url": "https://image.novelai.net/ai/generate-image",
      "edit_base_url": null,
      "api_key": "填写 Persistent API Token",
      "timeout": 180,
      "default_params": {
        "size": [832, 1216],
        "steps": 28,
        "scale": 5,
        "native": {}
      }
    }
  }
}
```

`ImgConfig` 在创建和重载时验证参数; `build_imagegen_from_config(config, async_=True)` 将尺寸数组和角色 JSON 转为调用层类型, 不发送网络请求; 清空默认参数提交 `null`, 修改时不提交空密钥即可保留旧密钥

- 冷管理: `GET /config/models?type=img`, `POST/PATCH/DELETE /config/models/img/{name}`
- 热管理: `GET /api/config/models?type=img`, `POST/PATCH/DELETE /api/config/models/img/{name}`
- 配置快照: `GET /config/snapshot?resource=models&type=img`
- 查看密钥: `POST /config/models/img/{name}/api-key`, 沿用控制服务鉴权
- 真实测试: `POST /config/models/img/{name}/test`, 请求体仅含 `prompt`

测试接口强制单图, 预览图片合计最多 12 MiB, 仅在响应中临时返回 Data URL; 不写模型配置, 会话历史或图片数据库, 不自动重试; 模型配置中拒绝保存底图, 遮罩和参考图片字节

CLI 支持 `satrap model list img` 及现有 `show`, `set`, `remove` 操作; 插件可声明图片模型引用:

```yaml
config_schema:
  painter:
    type: img
    label: 绘图模型
```

插件通过 `PluginResources` 取得匹配同步或异步的图片客户端; 配置快照保留安装时的参数; 删除或重命名前检查插件全局配置, 命名 Agent 配置, 会话覆盖和群插件配置中的引用, 扫描不完整时拒绝变更

## image_generation 插件

内置插件目录为 `satrap/expend/plugins/image_generation`, 启用时选择一个已经保存的 `img` 模型配置

插件提供两个工具:

- `generate_image`: 请求生成一次, 保存图片, 再交付当前会话
- `send_generated_image`: 按当前会话的 `file_id` 重发已经保存的图片, 不调用图片模型

插件配置示例:

```yaml
img_model: painter
accept_natural_language: true
accept_keywords: true
prompt_guidance: "推荐英文关键词, 使用逗号分隔; 可以混合自然语言描述"
size_mode: choices
size_choices: |
  832x1216
  1216x832
  1024x1024
size_default: "832x1216"
count_mode: range
count_min: 1
count_max: 4
count_default: 1
allow_negative_prompt: true
```

两个提示词开关互相独立, 至少开启一个; 它们和 `prompt_guidance` 共同生成 `prompt` 的参数描述, 不额外要求模型选择格式, 不强制翻译或自动判断标签写法

尺寸策略:

| 模式 | 配置 | 模型侧参数 |
| --- | --- | --- |
| `fixed` | `size_fixed` | 不暴露尺寸参数 |
| `choices` | 多行 `size_choices`, `size_default` | `size`, 限制在候选列表内 |
| `range` | `width_min/max`, `height_min/max`, `size_step`, `max_pixels`, `size_default` | `width`, `height`, 两者同时填写或同时省略 |

范围模式的默认尺寸使用 `宽x高`, 宽高须在范围内并为 `size_step` 的整数倍, 宽高乘积不能超过 `max_pixels`; 像素尺寸也受调用层的 4000 万像素硬上限约束

固定和候选模式可使用所选协议接受的原生尺寸值, 如 Seedream 的 `2K`; 原生档位对应的实际像素数由上游模型决定, `max_pixels` 只能约束显式像素尺寸

数量策略使用 `count_mode: fixed | range`; 固定模式读取 `count_fixed`, 不暴露 `n`; 范围模式使用 `count_min`, `count_max`, `count_default`, 模型省略 `n` 时由插件补入默认值

模型默认参数首先生效, 然后由插件策略及允许的本次参数覆盖尺寸与数量; 其它参数继续继承, 不修改保存的模型配置; 不开放任意 `native` 给模型填写

`allow_negative_prompt` 仅在 NovelAI 协议中可开启; 省略负向提示词继承模型默认值, 空字符串表示本次清空; OpenAI Images 与 Seedream 不支持公共负向提示词时, 安装阶段直接拒绝开启

安装阶段校验默认值, 候选尺寸, 协议能力和原生字段冲突, 不发送 HTTP 请求; 动态 schema 与运行时校验使用同一策略, 固定参数不能通过额外字段覆盖, 越界不会静默截断

当前 Seedream 公共调用只支持 `n=1`, 插件禁止配置多张数量, 并将继承的组图开关覆盖为关闭; 上游返回图片少于请求数量时报告实际数量, 不额外补发生成请求

图片通过现有会话文件产物机制保存和交付, Chat 登记附件, 平台使用各适配器的文件上传或发布能力; 不指定任意发送目标, 不新增数据库表, 不把 Base64 放入工具结果或历史

每张图片在当前会话的 `artifacts` 目录保留一份, `.image_generation` 子目录仅保存文件摘要, 尺寸和种子等轻量索引; 临时复制文件在保存后删除; 沿用单文件 32 MiB, 每会话 128 项及 512 MiB 的产物上限

结果分别提供 `generation_status`, `delivery_status`, `requested_count`, `actual_count` 和每张图片的 `file` 引用及交付回执; 生成成功但交付失败时保留文件, 使用 `send_generated_image({"file_id": "sf_..."})` 重发, 不重新生成和计费; 回执为 `unknown` 时不自动重发

同步和异步会话均受支持; 异步文件操作在线程执行, 取消不触发请求重试; 计划模式或失效安装不执行生成或交付

此插件当前提供文生图, 编辑底图和蒙版继续使用调用层的 `edit` 接口, 尚未作为插件工具开放

## 上游参考

- [OpenAI Images](https://developers.openai.com/api/docs/guides/image-generation)
- [Seedream 图片 API](https://console.volcengine.com/ark/region%3Acn-beijing/docs/image-generation-api)
- [NovelAI API 定义](https://image.novelai.net/docs/doc.json)
- [NovelAI 精准参考](https://docs.novelai.net/en/image/precisereference/)
- [NovelAI Vibe Transfer](https://docs.novelai.net/en/image/vibetransfer/)
