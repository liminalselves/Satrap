# Satrap 总体开发规范

本文档用于统一 Satrap 项目的开发约定, 并作为代码评审与后续维护的共同依据

本规范将随项目逐步补充, 当前包含注释与静态类型检查规范

## 1. 注释规范

本节根据 `satrap/core/APICall/EmbedCall/` 的手写注释风格整理

### 1.1 基本原则

- 使用中文编写注释, API Key, Embedding, OpenAI 等专有名词和代码标识符保留英文
- 使用英文标点, 包括 `,`, `;`, `:`, `()`, 禁止混用中文标点
- `,` / `;` / `:` 后面必须接一个空格, 相当于使用半角符号实现全角符号的分隔效果, `()` 前后的空格不作要求
- URL, 端口、路径、格式模板和代码片段中的标点属于语法组成部分, 不受上述空格规则限制
- 注释行末不添加句号
- 注释应解释代码的目的、约束或非显然行为, 不应机械复述代码本身
- 修改代码行为时必须同步检查相关注释, 避免注释与实现不一致
- 同一函数或同一组同步、异步实现应使用一致的术语和注释结构

### 1.2 Docstring

公开函数、公开方法、构造方法以及存在非显然行为的私有方法应编写 Docstring

仅包含一句功能摘要, 且不需要记录参数、返回值或其他说明时, Docstring 必须使用单行格式:

```python
def reset(self) -> None:
    """重置内部状态"""
    self._items.clear()
```

函数存在业务参数或返回值时, 必须改用多行 Docstring, 并按照“功能摘要、参数、返回”的顺序记录对应说明:

```python
def parse_embedding_response(
    api_response: Any,
    suppress_error: bool = True,
) -> list[list[float]]:
    """
    解析 Embedding API 的响应对象

    参数:
    - api_response: API 返回的 Embedding 响应对象
    - suppress_error: 如果为 True, 解析失败时返回空列表而不是抛出异常, 默认 True

    返回:
    - 二维浮点数列表, 元素顺序与输入文本顺序一致; 如果出错则返回 []
    """
```

Docstring 应遵守以下规则:

- 第一行直接概括功能, 不使用“这个函数用于”等冗余表述
- `参数:` 与 `返回:` 标题单独成行
- 参数使用 `- 参数名: 说明` 格式, 顺序与函数签名一致
- 参数说明应写明可选性、默认值、覆盖关系或特殊行为
- 返回说明应覆盖不同输入形态、配置分支和错误分支, 不能只描述正常结果
- 同步与异步实现容易混淆时, 在摘要中明确标注“同步”或“异步”
- 除 `self` 和 `cls` 外存在参数时, 必须编写 `参数:`; 参数顺序必须与函数签名一致
- 函数存在业务返回值时, 必须编写 `返回:`; 返回 `None` 的过程型函数不要求添加无意义的返回说明
- 只有一句摘要且没有业务参数和返回值时, 使用 `"""摘要"""` 单行格式

### 1.3 流程分段注释

包含多个处理阶段的函数应使用编号注释划分流程, 使主路径可以被快速浏览:

```python
# Step.1 参数合并
target_model = model or self.model

# Step.2 输入标准化: 确保为列表
is_single = isinstance(texts, str)
text_list = [texts] if is_single else texts

# Step.3 分批处理
for i in range(0, total_count, batch_size):
    ...

# Step.4 根据输入格式返回
return all_embeddings[0] if is_single else all_embeddings
```

流程分段注释应遵守以下规则:

- 编号按照实际执行顺序递增
- 每个步骤概括一个完整处理阶段, 不为每一行代码单独编号
- 步骤名称优先描述目的, 例如“输入标准化”“解析响应”“根据输入格式返回”
- 同一函数内统一使用 `Step.N` 格式
- 代码调整导致执行顺序变化时, 必须同步更新编号

### 1.4 代码行后注释

注释独占一行时, 应写在对应代码行或代码块之后, 不写在代码之前:

```python
request_kwargs: dict[str, Any] = {
    "model": target_model,
    "input": batch_texts,
    "encoding_format": target_encoding,
}   # 构造 Embedding API 请求参数

embeddings = parse_embedding_response(response, self.suppress_error)
# 解析响应并保持结果顺序与输入顺序一致
```

代码行后注释应遵守以下规则:

- 注释紧跟在所描述的代码行或代码块之后
- 注释应概括对应代码的目的、约束或结果, 不应逐行翻译实现
- 一条注释只描述紧邻其上方的代码, 避免归属不清
- 流程分段使用的 `Step.N` 注释仍放在对应处理阶段之前

### 1.5 行内注释

描述单行代码作用时, 注释应写在同一代码行, 代码与注释之间固定保留三个空格:

```python
for i in range(0, total_count, batch_size):   # 每次处理 batch_size 个文本
    ...

self.client = AsyncOpenAI(...)   # 初始化异步 OpenAI 客户端
```

如果行内注释过长, 应将注释放在代码行的下一行:

```python
sorted_data = sorted(data, key=get_index)
# 按响应索引排序, 保证返回结果与输入文本的顺序一致
```

行内注释应只用于解释当前代码行, 不应承载整段流程说明

### 1.6 应优先注释的内容

以下内容通常无法仅通过代码表面快速判断, 应优先补充注释:

- 输入标准化与输出形态转换
- 批处理、排序以及顺序保证
- 默认配置与调用参数之间的覆盖关系
- 对象属性、字典字段等兼容读取逻辑
- 异常抑制、空值回退和特殊返回值
- 同步与异步实现之间的重要差异
- 外部 API 调用前后的数据转换
- 为安全、兼容性或性能采取的特殊处理

示例:

```python
data = safe_getattr(api_response, "data")
# 提取 data 字段, 同时兼容对象属性和字典结构

sorted_data = sorted(data, key=get_index)
# 按索引排序, 保证结果顺序与输入一致
```

### 1.7 应避免的注释

避免以下类型的注释:

- 直接复述赋值、判断、循环或函数调用
- 仅说明“进入 if”“开始循环”“返回结果”等显而易见动作
- 与类型标注、变量名完全重复且不增加语义的信息
- 已经过期或与当前实现不一致的描述
- 大段记录修改历史、临时调试过程或个人对话
- 使用含义模糊的描述, 例如“特殊处理”“临时修复”而不解释原因

错误示例:

```python
model = self.model   # 设置 model
return result        # 返回 result
```

推荐写法:

```python
target_model = model or self.model   # 调用参数优先, 未提供时使用实例默认模型
return []   # 抑制解析异常时保持既有空列表返回契约
```

### 1.8 提交前检查

提交包含注释的代码前, 应确认:

- 注释使用中文, 专有名词和代码标识符除外
- 注释只使用英文标点, 行末没有句号
- Docstring 的参数顺序与函数签名一致
- 纯摘要 Docstring 使用单行格式; 包含参数、返回值或扩展说明时使用多行格式
- 存在业务参数或返回值时已分别编写 `参数:` 与 `返回:`
- Docstring 覆盖关键返回分支和错误行为
- 编号步骤连续且与实际执行顺序一致
- 行内注释与代码之间固定保留三个空格
- 单行说明使用行内注释, 过长时放到代码行下一行
- 注释解释了目的、约束或原因, 而不是复述代码
- 同步与异步版本的相同行为使用一致表述

### 1.9 模块头文档

除测试文件外, 每个 Python 文件都必须在文件头编写模块级 Docstring, 包括仅用于导出的 `__init__.py`

模块头文档的位置应遵守以下顺序:

1. shebang, 例如 `#!/usr/bin/env python3`
2. 编码声明, 如果存在
3. 模块级 Docstring
4. `from __future__ import ...`
5. 其他 import 与模块代码

职责单一且用途明确的模块, 只需使用单行摘要:

```python
"""Satrap 统一日志配置与输出接口"""
```

核心模块和常用模块必须使用多行头文档, 参考 `satrap/api/checkpoint.py` 的结构, 简要说明模块用途、主要职责和关键协作关系:

```python
"""
会话实例生命周期管理器

按会话类型创建并缓存同步或异步会话, 管理容量与空闲回收,
同时负责会话元数据持久化, 恢复和默认模型配置注入
"""
```

以下模块通常视为核心模块或常用模块:

- 程序入口与后端编排模块
- 模型 API 调用与配置加载模块
- 工作流, 会话, 用户和上下文管理模块
- 工具调用与插件系统模块
- 消息组件, 平台事件和请求调度模块
- 被多个业务模块直接依赖的公共基础模块

模块头文档应遵守以下规则:

- 第一行直接概括模块用途
- 扩展说明保持简洁, 通常控制在两到五行, 不罗列所有类和函数
- 优先说明模块负责什么, 与哪些核心组件协作, 以及重要的边界或约束
- 单句摘要使用单行格式; 包含职责、协作关系或其他扩展说明时必须使用多行格式
- 模块头文档不编写 `参数:` 或 `返回:` 章节
- 使用中文和英文标点, 行末不添加句号
- 文件职责发生变化时必须同步更新模块头文档
- `tests/` 下的测试文件不强制编写模块头文档

提交前还应确认:

- 所有非测试 Python 文件都能通过 `ast.get_docstring()` 取得模块头文档
- 模块头文档位于 import 之前, 没有被误写成普通字符串常量
- 核心模块和常用模块不是只有含义空泛的一句摘要

### 1.10 导入规范

导入语句应按照以下分组顺序编写, 各组之间保留一个空行, 同一组内不添加空行:

1. 外部库, 包括 Python 标准库和第三方库
2. 内部库, 即 `satrap` 项目内模块
3. Satrap logger, 固定使用 `from satrap.core.log import logger`, 并与其他内部库导入分组

外部库和内部库组内均以导入路径为排序键, 按路径字符数从多到少逐条排列

多个名称较短的模块允许在同一条 `import` 语句中使用逗号导入, 同一行内同样按模块名字符数从多到少排列:

```python
import json, re
```

推荐结构如下, 具体写法可参考 `satrap/core/APICall/LLMCall/responses.py`:

```python
from openai.types.chat.chat_completion import ChatCompletion
from typing import AsyncIterator, Iterator, Optional, Any
import json, re

from satrap.core.utils.vision import normalize_chat_messages
from satrap.core.type import LLMCallResponse

from satrap.core.log import logger
```

## 2. 静态类型检查规范

本节为推荐性规范

### 2.1 检查目标

- Pyright 或 Pylance 使用 `basic` 类型检查模式时, 应保持无错误、无警告
- 其他静态类型检查器应达到等价检查效果
- 编辑器中的关键变量、属性和返回值应能显示明确类型
- Pylance 不应因为类型无法解析而将成员显示为灰色未知项

本文所称“灰色未知项”, 是指变量类型为 `Any` 或 `Unknown` 时, Pylance 无法确认其成员结构, 因而无法对属性访问提供可靠类型提示和检查的情况

例如以下写法会使 `url` 缺少可检查的类型信息:

```python
llm: Any
llm.url
```

### 2.2 避免 Any 和 Unknown

- 尽量避免使用 `Any`, 优先声明具体类型
- 数据结构固定时, 优先使用类、数据类、`TypedDict` 或其他明确类型
- 只依赖少量属性时, 可以使用 `Protocol` 描述所需接口
- 外部库或动态数据无法直接推断时, 应在边界处使用类型守卫、`cast`、重载或项目提供的 `safe_getattr` 系列函数收窄类型
- 不应让 `Any` 或 `Unknown` 从外部接口向核心业务逻辑扩散
- 确实无法避免动态类型时, 应将其限制在尽可能小的范围内, 并说明无法静态确定类型的原因

推荐写法:

```python
class LLMConfig(Protocol):
    url: str


def get_url(llm: LLMConfig) -> str:
    """
    获取 LLM 服务地址
    """
    return llm.url
```

### 2.3 检查标准

提交前建议确认:

- Pyright 或 Pylance 在 `basic` 模式下没有错误与警告
- 编辑器悬停关键变量时能看到具体类型, 而不是 `Any` 或 `Unknown`
- 访问对象属性时能获得补全、定义跳转和类型检查
- 新增的 `Any` 有明确且不可避免的动态边界原因
- 类型收窄发生在数据进入业务逻辑的位置, 而不是分散在各个调用点
