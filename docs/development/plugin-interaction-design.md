# 插件间交互规范设计

状态: A1/A2/B/C 已实现, 最终验收记录见第 18 节
基线: fix/issue19, e5de0a9
范围: 已授权实施 A1 -> A2 -> B -> C, D 中权限插件拆分已实施, 目录外读取仍为后续方向

## 1. 本版调整

按审阅意见统一采用以下规则:

- YAML 默认不写交互格式版本, 缺省固定使用首版格式, 不随框架升级改变语义
- 不再为依赖起别名, 直接使用对方插件 name 和已注册能力名
- YAML 不逐项填写 API 版本, 兼容性信息由框架维护
- 保留既有 tools / handlers / mcp / skills / commands, 新增第六类 services 专门提供插件调用能力
- 不恢复原稿的 provides, 服务别名或逐项 API 版本; services 直接使用实际注册名称
- 依赖, 替代与排斥均按“插件名 -> 能力类别 -> 能力名”表达
- 默认值不重复写, 常用配置不增加消费者列表和优先级字段
- 撤回未实施的 model_hidden 草案, 用独立 services 类别区分插件专用能力与模型工具
- 服务通过 Service / AsyncService 基类实现, 直接使用 execute 的业务参数与类型注释, 不强制请求/响应 DTO 或重复参数表
- 插件访问宿主能力的对象命名为 PluginHost, 工厂参数使用 plugin_host, 实例属性使用 self._plugin_host; 业务参数按调用单独传递
- 替代若使仍启用插件原本满足的必需依赖新变为不满足, 报冲突并阻止生效, 不自动停用依赖者

框架仍需要类型明确的环境与受控能力引用, 但这些是实现细节, 不转化成开发者要逐项声明的 YAML 协议。

本规范用于正常 Python 插件协作, 不把进程内注册表描述成隔离恶意插件的安全边界。

## 2. 已有能力与当前问题

现有五类能力继续沿用正式键名, services 是本轮新增的第六类:

| 键名 | 对应能力 |
| --- | --- |
| tools | 面向模型的 Tool / AsyncTool 工具, 明确导出时也可供插件复用 |
| handlers | SessionHandler 输入/输出处理器, 包含用户所说的前处理 |
| mcp | 插件注册的命名 MCP 连接 |
| skills | 已注册技能 |
| commands | 同步/异步命令 |
| services | 仅供插件程序调用的业务能力, 不进入模型工具注册表 |

沿用这些名称, 不另建 tool / cmd / service 等近义键。

现有安装回滚, 能力开关, 管理权限, 恢复指纹和模型资源机制继续使用。目前需要解决的是:

- Chat 和平台用 coding_* 会话属性提供目录, 各插件自行读取
- Chat 写死 coding 启用时关闭 code_sandbox, 不符合当前两种执行能力的定位
- 宿主审批直接引用 coding 内部状态, 后续难以供其他插件共用
- 群聊 SDK 同时承担通用沙箱图片读取与群资产授权
- 插件间没有明确的公开能力边界, 依赖和替代规则也未统一

## 3. 开发者 YAML 总体形态

interop 仍使用 exports / requires / replaces / conflicts 四个分组; 新增的是能力类别 services, 不增加第五个交互分组:

```yaml
interop:
  exports:
    services: [transcode_video]
  requires:
    base_take:
      tools: [read_document]
  replaces:
    legacy_media:
      tools:
        inspect_image: read_image
  conflicts:
    legacy_executor: true
```

该示例假设插件既实现 services.transcode_video, 也实现 tools.read_image; 服务只供合法插件调用, 工具仍为模型入口。各名称必须与真实注册结果一致, 示例不表示两个插件不能同时安装; 官方 Shell 对基础沙箱工具的替代见第 12 节。

默认行为:

| 未填写的配置 | 行为 |
| --- | --- |
| interop | 按旧机制安装, 未声明对其他插件开放能力 |
| exports | 没有对其他插件开放的能力 |
| requires | 没有插件依赖 |
| replaces | 不替代其他入口 |
| conflicts | 不排斥其他插件 |
| requires.<插件名>.optional | 默认 false, 即必需依赖 |
| 交互格式版本 | 缺省固定为首版格式, 普通配置不填写 |
| 能力 API 版本 | 使用框架的能力适配协议, 开发者不填写 |

不填写 interop 不改变该插件原本向模型提供工具, 向会话注册处理器或向用户提供命令的行为。exports 控制其他插件的程序化访问。services 无论是否导出都不成为模型工具; tools 继续按既有模型工具规则生效。类别与导出均不取代已有开关和用户权限。

声明校验统一采用以下口径:

- interop 只接受上述四个分组; exports / requires / replaces / conflicts 中的能力类别位置均只接受 tools / handlers / mcp / skills / commands / services 六类键, 未知键作为配置错误拒绝, 不静默忽略拼写错误
- 上述类别限制不排除既有的插件级 true 简写和 requires.<插件名>.optional 等结构字段, 这些字段只在各自规定位置合法
- 不再支持未发布的 model_hidden 配置, 不把 services 隐式转成隐藏工具; 拼写错误或未支持字段明确拒绝
- 类别中的能力名使用非空字符串列表, replaces 使用同类能力名映射; 类型错误或不存在的本插件能力均使安装失败
- requires / replaces / conflicts 不能指向当前插件自身, 自引用是配置错误; 插件内部复用不借用跨插件声明
- exports 本来就声明本插件自己的能力, 不属于上述自引用错误
- 必需 requires 与 conflicts 的声明直接自相矛盾时按配置错误拒绝, 不等安装依赖后才反复切换 blocked / conflict
- 安装和配置变更均先校验, 预览报告具体字段和原因, 实际安装遇到同一错误时按原流程失败回滚

硬矛盾按精确作用范围判断, 不是只要两个分组出现同一个插件名就报错:

| 当前插件的必需依赖 | 当前插件的排斥声明 | 结果 |
| --- | --- | --- |
| x 整个插件 | x 整个插件 | 配置错误, x 不可能既启用又不启用 |
| x.tools.a | x 整个插件 | 配置错误, 使用 a 必须先启用 x |
| x.tools.a | x.tools.a | 配置错误, 同一能力被同时要求可用与不可用 |
| x.tools.a | x.tools.b | 允许, x 开启 a 并关闭 b 即可 |
| x 整个插件 | x.tools.a | 允许, 插件可启用而 a 关闭 |
| 可选 x 或 x.tools.a | x 整个插件或 x.tools.a | 允许, 可选依赖不要求目标必须可用 |

能力级比较使用同一插件, 同一类别和同一能力名; 任一必需能力落入排斥集合即构成硬矛盾。其他间接运行时问题继续通过依赖图和关系预检处理, 不把不同能力或 optional 变体误报为声明层矛盾。

## 4. 对外开放 exports

假设 document_tools 插件具有下面这些真实能力, 可直接声明:

```yaml
interop:
  exports:
    tools: [read_document, read_image]
    services: [transcode_video]
    handlers: [normalize_document_input]
    mcp: [document_files]
    skills: [document_reader]
    commands: [document_status]
```

规则:

1. 名称就是现有注册名, 不重复定义公开别名或导出类
2. 只开放列出的能力, 未列出的一律不对其他插件开放
3. 名称必须存在于插件实际收集结果中, 声明不存在的能力时安装失败
4. 不支持 all / * 等自动开放将来新增能力的写法
5. 开放不等于启用, 用户关闭工具或服务后, 插件间调用也不能继续执行
6. 不返回工具实例, MCP 客户端, 可变配置或私有状态对象
7. 原五类能力继续绑定真实注册结果; 插件专用业务在可选 services.py 中继承 Service / AsyncService, 通过类收集或 get_services 注册

六类能力统一声明, 但不强行采用相同的执行语义:

| 类别 | 对其他插件开放的含义 | 保留的边界 |
| --- | --- | --- |
| tools | 通过受控分派复用已注册的模型工具 | 原参数校验, 启用状态, 来源检查和审批仍执行 |
| services | 通过受控分派调用插件专用业务能力 | 不进入 tools_manager, 不生成模型工具定义, 仍核验来源, 权限与审批 |
| handlers | 查询/依赖已注册处理器, 取得其公开描述与阶段信息 | 仍由宿主处理链调度, 不额外注册副本, 不开放任意手动重跑私有回调 |
| mcp | 访问该连接已经注册并启用的工具适配器 | 不暴露原始连接或凭据, 不访问未注册的远端方法 |
| skills | 查询或读取已注册技能的公开内容 | 不绕过技能启用状态, 不自动修改系统指令优先级 |
| commands | 经命令分派入口执行公开命令 | 保留原生命令/管理权限约束, 模型工具不能伪装成用户命令 |

例如导出 handlers.normalize_document_input, 不表示调用者可以拿到 after_model_reply 函数并在任意时刻反复执行。需要复用可主动调用的转换行为时, 将其发布为 services, 处理器和工具均可复用同一业务函数, 无需为此暴露模型工具。

### 4.1 插件专用能力 services

services 是新增的第六类能力, 用于业务上不适合模型直接操作的程序原语, 例如转码, 文档转换或受控资产处理。它不继承 Tool / AsyncTool, 不注册到 tools_manager, 不生成 function calling 定义。

开发者仍使用简单的实际名称声明:

```yaml
interop:
  exports:
    services: [transcode_video]
```

services 与 tools 分别注册, 不通过一个“模型隐藏”标志在两种身份之间切换:

| 类别与状态 | 模型直接调用 | 已声明依赖的其他插件调用 |
| --- | --- | --- |
| tools 启用且未导出 | 按原模型工具规则允许 | 不允许 |
| tools 启用且已导出 | 按原模型工具规则允许 | 按原权限规则允许 |
| services 启用且已导出 | 无此模型入口 | 按原权限规则允许 |
| services 启用但未导出 | 无此模型入口 | 不允许 |
| 该能力关闭或所属插件停用 | 不允许 | 不允许 |

服务可以保持私有供本插件内部使用, 无需导出, 不默认告警。普通内部 Python 函数没有注册为服务时也不会被发现, 注册服务才进入六类能力管理。

模型即使得知服务名, 工具分派器也只查模型工具注册表, 不回退查找 services。服务名不是工具名, 框架不自动提供一个让模型任意填写插件名和服务名的通用调用工具。

其他插件能否调用仍同时取决于 exports, requires, 启用状态和原始用户/领域权限。exports 不提供消费者白名单; 一个合法高层工具可以转发请求到服务, 专用类别不保证业务行为不会被模型请求间接触发。

### 4.2 services 的代码入口与生命周期

新增独立的 Service / AsyncService 基类, 开发者写法与 Tool / AsyncTool 对齐, 但不继承工具基类, 不携带模型工具定义或 JSON Schema 生成逻辑。通过 satrap.edictum 公开导出, 业务插件在可选 services.py 中定义服务:

```python
"""插件专用文本处理服务"""

from satrap.edictum import Service


class NormalizeTextService(Service):
    """供插件复用的文本规范化服务"""

    service_name = "normalize_text"
    description = "去掉首尾空白并合并连续空白"

    def execute(self, text: str, *, lowercase: bool = False) -> str:
        """
        规范化文本, 可选转为小写

        参数:
        - text: 待处理文本
        - lowercase: 是否转为小写, 默认 False

        返回:
        - 规范化后的文本; 参数类型不符时抛出 TypeError
        """
        if not isinstance(text, str) or not isinstance(lowercase, bool):
            raise TypeError("text 必须是 str, lowercase 必须是 bool")

        result = " ".join(text.split())
        return result.lower() if lowercase else result
```

该示例使用本轮已实现的接口。服务名使用 service_name, 说明使用 description; execute 的签名就是业务参数定义, 不另写 params_dict, 输入/输出 Schema 或强制 Request / Response 类。异步实现继承 AsyncService 并定义 async def execute(...), 其余规则相同。

注册约定:

- 默认收集 services.py 中本模块定义的可无参构造服务类, 不重复收集导入的类或基类
- 需要注入会话配置或宿主接口时, 可提供 get_services(session, config=None, resources=None, *, plugin_host=None), 返回 list[Service | AsyncService] 实例列表
- 无参类收集不自动注入 plugin_host; 服务需要查询宿主环境, 使用宿主产物接口或调用其他插件时, 必须通过 get_services 把 plugin_host 显式传给构造方法, 与工具工厂注入形态相同, 参见第 17.5 节
- 存在 get_services 时只使用工厂结果, 不再同时扫描类, 避免双重构造或重复注册
- 名称只来自实例的 service_name, 不再用一份映射重复声明名称; 同一插件重复注册同名服务时安装失败
- 工厂签名预绑定后只执行一次, 不以捕获内部 TypeError 的方式换一组参数重试
- 不要求额外 provides, 别名, API 版本, 每项独立协议类或业务启动/关闭钩子

传入传出采用最少约束:

- execute 的业务参数和返回值应写明确的 Python 类型注释, 简单结果直接返回 str / bool / int 等, 固定结构可选用 TypedDict
- 调用方仍提交具名参数映射, 分派器用真实 execute 签名检查参数名, 必选项及默认值; 不声称 Python 注释会自动变成运行时类型校验
- 推荐显式具名参数, 不建议 execute 使用 *args / **kwargs; 绑定遵循 Python 签名语义, **kwargs 接收的额外字段由服务自己校验, 不承诺它们会在绑定阶段被拒绝
- 类型范围, 路径合法性及其他业务约束由实现校验, 简单服务无需为此创建单独的数据类或校验器
- 普通输入/输出限定为 JSON 兼容数据, 大文件或媒体使用宿主受控产物引用; 分派边界检查数据范围, 不返回 Session, 私有引擎, MCP 客户端或可变配置等实现对象
- 数据类只在确有需要时使用, 由业务实现转换为普通数据, 不新增自动序列化或任意对象透传协议
- 异常由既有插件调用边界记录和隔离, 保留原有失败包装, 不要求每个服务另造成功/错误信封

基类只承担名称, 说明及已有能力管理需要的启用/可用状态, 两个公开基类分别规定同步 execute 和异步 execute。服务引用 CapabilityReference 只暴露受控调用, 不把原始服务实例或 execute 方法交给消费者; 插件之间不得以直接调用实例代替分派。

生命周期沿用现有流程:

- 安装器把服务记录到 Plugin.services 及专用注册表, 增加 services 独立开关和权限映射, 不修改 tools_manager 以伪装服务
- 同步会话收集 Service, 异步会话收集 AsyncService; 提供者可以明确编写对应适配器, 框架不猜测或偷偷桥接
- 与现有五类能力同样受插件聚合开关, 依赖, 替代及权限约束; 服务管理权限复用既有授权规则, 不新增另一套管理员名单
- services.py 可省略; 有导出声明却没有真实实现时安装失败
- 构造, 注册或激活失败走原安装回滚, 资源清理复用 cleanup(session) 与已有资源生命周期
- 安装代次改变后旧服务引用失效, 不因同名服务重装而自动换绑

服务注册标识使用会话作用域, 插件名与服务名三者, 不与模型工具共享全局名称表。不同插件可各自注册同名服务。工具仍遵守原工具注册命名规则, 替代不自动重定向或放宽所属类别的注册约束。

同一插件可以同时注册 tools.foo 与 services.foo, 类别和调用入口已经区分目标, 不因跨类别同名报错或告警。同一插件同一类别内的重名仍按该类别原有规则处理。

需要宿主资料的实现通过工厂注入的 plugin_host 获取当前受控调用作用域, 在等待后和真实副作用前重新核验。服务注册本身不授予文件, 网络或消息发送权限; 通用分派核验来源与能力授权, 实现涉及的领域操作继续使用相应宿主接口和审批机制。

类型明确不等于跨插件动态查找自动拥有具体 Python 函数签名。服务实现内部保持精确类型, 公共分派入口使用明确的数据联合类型和受控引用, 动态绑定集中在调用边界, 不把 Any 状态对象传遍业务代码。

## 5. 依赖 requires

### 5.1 依赖具体能力

```yaml
interop:
  requires:
    document_tools:
      tools: [read_document]
      skills: [document_reader]
```

document_tools 必须等于提供者的 name。tools / skills 中填写对方的实际能力名称, 不再出现 formatter 之类额外别名。

含义: 当前插件要求这些能力已经存在, 对外开放且对插件调用实际可用。两项均满足后才能激活当前插件, 不自动安装或启用提供者。services 本就不对模型开放, 不因此视为不可用; 用户关闭服务或提供者停用则影响依赖。

为简化语法, 必需依赖默认作用于当前整个插件, 不再要求消费者列表。这个选择意味着缺少依赖时整个依赖插件不可激活。若只是个别工具的可选增强, 使用下节的 optional, 由实际调用工具报告不可用或采用明确降级。

### 5.2 可选依赖

```yaml
interop:
  requires:
    document_tools:
      optional: true
      tools: [read_image]
```

optional 唯一的默认值是 false, 不需要再写 required: true。

提供者或指定能力不可用时, 当前插件仍可启用。实际使用时通过可选查找确认是否可用; 没有替代方案的单次操作返回依赖不可用, 不假装完成。optional 适用于该插件名下本条依赖的全部能力; 首版不增加逐项不同的条件表达式。

不带能力列表也合法:

```yaml
interop:
  requires:
    document_tools:
      optional: true
```

该写法只声明可选的插件存在性/启用状态检查, 不授予任何具体能力访问权。可通过第 10 节的 plugin_host.plugins.find 查询, 不要求调用方为了正常的可选缺失写 try/except。缺少能力类别时, optional 默认为 false 的空映射与插件整体必需依赖等价。

### 5.3 仅要求插件存在并启用

```yaml
interop:
  requires:
    document_tools: true
```

这是插件整体依赖的简写, 但不开放对方任何未声明的能力。若要实际调用某个工具, 仍须使用上面的具体能力声明且对方必须导出它。

不是必须为了表达插件存在性而构造一个空服务。false 不是“反向依赖”, 不需要依赖时删除本条即可。

### 5.4 执行规则

- 私有或未声明依赖的能力不能被查找和调用
- 必需依赖在安装/启用前预校验; 缺失时保留目标配置并显示 blocked, 独立安装则失败回滚
- 用户明确关闭或卸载提供者后, 依赖者的实际状态随之不可用; 替代导致的必需依赖失效则按第 6 节提前报冲突, 不默认放行
- 提供者恢复后按用户原始开关恢复, 不自动修改配置
- 可选依赖不屏蔽整个插件, 相关调用仍进行完整检查
- 必需依赖存在循环时预览报错, 不靠安装顺序碰运气
- 不自动下载依赖, 不从对方模块导入私有实现

依赖还必须适配当前会话的同步/异步形态。同步会话仅找到异步能力, 且提供者没有明确的同步适配器时, 视为该能力对当前会话不可用。必需依赖阻止激活, 可选查找返回 None。异步会话需要的形态也按同一规则核验; 不偷偷使用 asyncio.run, run_until_complete 或直接阻塞事件循环。提供者明确编写且保留来源和权限的既有桥接适配器可以使用。

形态检查针对实际执行能力, 不要求纯元数据查询凭空提供同步/异步两个版本。处理器仍可采用宿主已有的明确调度适配, 技能公开资料查询不因此变成可执行协程。

## 6. 替代 replaces

```yaml
interop:
  replaces:
    base_take:
      tools:
        read_document: read_media
```

含义:

- base_take: 目标插件名
- tools: 目标及替代入口的同一能力类别
- read_document: 对方要被替代的能力名
- read_media: 当前插件实际提供的能力名

不再使用 plugin / entry / with 三个字段逐项描述同一引用, 也不为关系设置别名。

行为:

1. 替代者实际可用且必需依赖保护检查通过时, 目标增加 replaced_by 不可用原因
2. 替代者关闭, 卸载或缺少必需依赖时, 目标按原用户开关重新计算状态
3. 目标未安装时规则未命中, 不阻止替代者独立使用
4. 目标已安装但指定名称不存在时在安装/启用/变更预检报配置错误, 不提交该关系; 实际安装遇到同一错误失败回滚
5. 同一目标不能存在两个生效替代者, 不引入 priority 或“最后加载者覆盖”
6. 原能力的权限不自动转移到替代者, 也不在替代者拒绝或报错后自动降级到原能力
7. 不把旧调用名重定向到新能力, 不以替代声明放宽同名注册冲突
8. 同类匹配适用于六类能力, 不提供跨类替代, 不支持一条规则替代整个插件
9. 替代使仍要求启用的其他插件原本满足的必需依赖新变为不满足时, 报 replacement_breaks_dependency 并拒绝这次替代, 不自动关闭依赖者
10. tools 只能替代 tools, services 只能替代 services, 不跨类别转移模型可见性或调用语义; 模型工具替代关系不能借此发布服务

替代只使旧目标能力不可用, 不保证两个实现语义等价, 不将依赖声明或旧调用名自动迁移到替代者。判断新工具是否兼容由依赖者的作者负责。

例: report 必需依赖 base_take.read_document, media_reader 想用自己的 read_media 替代该工具。若目标配置仍保留 report 启用, 应提示“无法替代 base_take.read_document: report 必需依赖该工具”, 保留原运行状态。不能先禁用 read_document, 再顺带把 report 变成 blocked。

保护检查覆盖本次变更后的完整目标配置, 不仅查看替代者自身依赖, 但采用增量语义: 按每条必需依赖比较施加本次拟生效替代影响之前与之后的可用性, 只拦截满足 -> 不满足。两边采用相同的目标用户开关, 环境与依赖声明, 不简单以变更前的旧运行状态作为唯一基准。

如果目标工具本来就被用户关闭, 依赖已经不满足, 替代没有新破坏该依赖, 不以这项既有 blocked 状态拒绝替代。反过来, 同一变更准备恢复目标工具却又将其替代时, 应按最终目标开关检查, 不能因旧运行状态中工具是关闭的而漏判。插件因另一项依赖已 blocked, 也不意味着其余原本满足的必需依赖可以任意被新破坏。

用户同一变更中明确关闭依赖者, 或依赖者作者已更新声明及实现后, 可重新检查并应用; 框架不替用户改配置或替作者改依赖。仅有可选依赖时不阻止替代, 相关调用按既有可选依赖处理。

此保护针对替代引入的依赖破坏, 不隐式阻止用户主动停用或卸载提供者。新安装或重新启用的依赖者, 必需依赖必须在当前替代关系下满足; 若被已生效替代阻断, 报 replacement_breaks_dependency 并拒绝该次安装/启用, 不返回启用成功再连带显示 blocked。这与已有依赖者的增量保护分别处理, 不能拿该插件原先未启用来跳过激活校验。

目标插件之后升级或重装并删除被引用的能力名时, 在该次变更预检重新校验, 拒绝提交不兼容关系并保留可用的旧运行状态, 不猜测新名称或追改已完成的历史。若启动时已无旧实现可保留, 明确报告相关关系不可用, 不假装已完成替代。

处理器替代意味着之后的宿主处理链只调度替代者, 不补跑历史输入; 保持替代者原 priority, 不复制目标的顺序。技能替代只影响之后的有效技能, 不改写已提交历史。MCP 替代按命名连接及其适配器集合处理, 不移交原客户端实例。

## 7. 排斥 conflicts

### 7.1 排斥整个插件

```yaml
interop:
  conflicts:
    legacy_executor: true
```

当前插件和 legacy_executor 不能同时启用。任意一方声明即可, 不要求对方也声明一次。

### 7.2 排斥对方的指定能力

```yaml
interop:
  conflicts:
    legacy_executor:
      tools: [shell]
      commands: [execute]
```

当前插件启用时, 对方 shell 工具或 execute 命令任一个实际可用就构成冲突, 不排斥对方其他能力。同样可以使用 handlers / mcp / skills / services。

两个口径有意不同: 插件级规则限制双方聚合开关同时为开启, 不因其中某个能力失效就自动解除; 能力级规则仅在指定目标能力结构上实际可用时命中。目标能力被用户关闭或缺少依赖时不命中能力级规则, services 不对模型开放也不等于关闭, 仍按服务有效状态计入实际可用。两种规则均不依据当前发话者权限切换结果。

首版声明方按当前插件整体处理, 不引入一套左侧入口 -> 右侧入口的条件表达式。需要细到当前插件某个能力才冲突的场景另行讨论, 不用容易误解的默认行为代替。

冲突时报告双方, 拒绝激活该冲突配置并保留上一份可运行状态。不会自动关闭其他插件, 也不提供 overwrite / force 等跳过选择的开关。

同一插件同时出现在 replaces 和 conflicts 不必然构成硬矛盾, 不默认告警。若排斥的是整个目标插件, 双方不能同时启用, 替代规则通常没有实际命中场景, 可以保留声明但不能期待它实现透明升级。若只是替代 x.a 并排斥 x.b, 仍可能有合理的共存状态, 不按同名插件简单拒绝。

## 8. 解析顺序与有效性

框架按下面顺序处理:

1. 解析用户期望的插件与能力开关, 校验环境适用性
2. 校验依赖和对外开放声明, 得到候选状态
3. 在预览状态中解析替代关系及其完整影响, 暂不改变运行中的能力
4. 校验最终目标依赖图; 替代新破坏原本满足的必需依赖时报冲突, 新安装/重新启用依赖者则另行核验当前替代关系下的全部必需依赖
5. 检查生效能力之间的排斥关系
6. 安装/启用提交时发布新状态, 执行时核验用户与业务权限

必须合并检查依赖和替代的循环, 例如 A 需要 B.read_document, 同时 A 又替代 B.read_document, 应直接报不可满足关系, 不反复启停。

用户配置开关不变。先计算不随当前发话者改变的基础可用性:

```text
插件已启用
AND 自身能力开关已启用
AND 环境适用
AND 必需依赖满足
AND 未被有效替代
```

结构可用性不根据某个发话者的权限变化而选择替代者。权限不足时拒绝本次操作, 不切换工具逃过拒绝。实际入口的旧引用也必须进行同样检查。

模型只从原 tools/MCP 工具通道选择入口, 不能直接选择 services。插件调用六类公开能力都要求目标已导出且消费者声明依赖, 并额外核验原始用户和领域权限。依赖与替代检查使用结构可用性, 不因调用者不同而切换结果, 也不把 services 缺少模型入口误当作服务禁用。

## 9. 宿主环境和产物, 不伪装成业务插件

工作区, 上传目录, 沙箱目录属于宿主环境, 不要求每个插件写 requires.host_*。

框架注入只读 PluginHost:

| 接口 | 内容 |
| --- | --- |
| plugin_host.environment_snapshot() | 当前作用域, 项目, 目录与环境 revision 的冻结快照 |
| plugin_host.plugins | 仅可访问已声明依赖且对方已导出的能力 |
| plugin_host.artifacts | 宿主的有界产物读取接口, 首版覆盖沙箱图片 |

上述入口均已接入; 具体调用形态见第 10 节和第 17 节。

环境快照使用明确类型, 不公开可变 Session 或引擎。当前发话者和轮次仍由已有 CallOrigin / 工作流绑定, 不存入长期环境快照。

插件每次操作读取快照。项目改绑递增 revision, 等待中的路径敏感操作在实际使用前复核, 不自动把旧操作转到新目录。目录资料不自动授予读写或发送权限。

Chat 和平台宿主通过 bind_environment 绑定 EnvironmentSnapshot, 官方消费者直接使用统一环境; 旧 coding_* 路径属性及过渡适配均已移除。未声明 interop 的独立工厂调用也读取同一绑定, 不维护另一份路径状态。

产物首版沿用当前沙箱相对图片路径, 有界读取并冻结字节和来源。群聊随后使用现有 AssetStore 登记本群/本轮 asset_id。读取不等于发送, 不新增通用资产数据库或自动发送。

## 10. 程序化访问与权限边界

工具调用示意, 参数类型和返回包装以现有工具协议为准:

```python
result = await plugin_host.plugins.tools.call_async(
    "base_take", "read_document", {"path": "images/plot.png"}
)
```

插件专用业务通过不同的类别入口调用, 不经过模型工具管理器:

```python
result = await plugin_host.plugins.services.call_async(
    "media_backend", "transcode_video", {"input_path": "source.mp4"}
)
```

这只是框架 API, 不自动创建一个允许模型任意选择插件和能力的工具。

分派前必须同时满足:

- 消费方实际入口仍有效, 且它所属插件声明了目标依赖
- 提供方 exports 明确开放目标, 目标当前有效
- 当前会话实例与安装代次一致
- 提供方原参数校验, 管理权限与操作审批通过
- 原始用户, 平台账号, 群, 轮次及工作流来源保持不变
- 调用通道由框架绑定; 模型工具分派器不解析 services 名称, 合法插件服务调用必须通过依赖, 导出, 来源及原权限检查

工具与 MCP 调用走原执行路径及工具媒体结果处理, 不直接绕过执行框架调用 execute 裸方法。services 经专用分派器执行并记录当前操作的子调用, 不制造 assistant/tool 消息; 返回业务数据或宿主受控产物, 不返回原始实现对象。跨插件调用作为当前操作的子调用记录, 消费者最终对模型返回一次结果, 不制造重复的 assistant/tool 回复或提前提交历史。

命令从插件调用时仍保留真实来源。不能把模型提供的命令文本送入“可信用户原生命令”通道, 从而修改审批模式或获得管理权限。handlers 与 skills 按第 4 节的专属语义处理, 不套用任意函数调用。

对内使用有类型的宿主接口, 能力引用, 分派参数和结果容器。工具业务参数来自既有 JSON Schema, 不承诺给第三方任意工具自动生成 Python 静态函数签名; 动态字段校验集中在调用边界, 不让 Any 状态对象传遍业务代码。

### 10.1 可选查找与查询接口

统一使用 find 表达正常的可选缺失, 不要求业务代码通过异常判断有没有依赖。拟议接口如下:

| 接口 | 有效目标的返回值 | 不可用时 |
| --- | --- | --- |
| plugin_host.plugins.find(plugin_name) | 只读 PluginInfo, 包含公开名称, 版本和当前状态 | None |
| plugin_host.plugins.tools.find(plugin_name, tool_name) | 受控 CapabilityReference | None |
| plugin_host.plugins.services.find(plugin_name, service_name) | 受控 CapabilityReference, 不返回原始服务实例 | None |
| plugin_host.plugins.commands.find(plugin_name, command_name) | 受控 CommandReference | None |
| plugin_host.plugins.mcp.find(plugin_name, connection_name) | 受控 MCPReference | None |
| plugin_host.plugins.handlers.find(plugin_name, handler_name) | 只读 HandlerInfo, 包含名称, 公开描述, 处理阶段与顺序信息 | None |
| plugin_host.plugins.skills.find(plugin_name, skill_name) | 只读 SkillInfo, 包含标识, 公开描述与技能内容 | None |

所有接口都先检查当前调用插件声明过该依赖。未声明时抛 capability_not_declared, 不将配置错误伪装成正常缺失。对已声明的目标, 不存在, 未开放, 关闭, 被替代或没有当前会话适配形态时返回 None, 不暴露私有对象。声明了 x 的具体能力依赖也允许 plugin_host.plugins.find("x") 查询 PluginInfo, 但不能顺带访问 x 的其他未声明能力。仅声明插件整体依赖时, 只允许第一行的插件资料查询, 不自动允许其他行的具体能力查询。

find 是无业务副作用的查找。取得引用不授予执行权, 不承诺稍后的调用一定成功, 仍需在执行时重新核验状态和权限。内部或宿主自用能力不通过伪造跨插件依赖调用这些接口。

工具与服务共用 CapabilityReference, 均提供 call_sync(arguments) / await call_async(arguments), 各自只经所属注册表执行, CommandReference 提供对应 run_sync(args) / await run_async(args)。只允许当前会话中实际提供的形态。MCPReference 只提供其已注册工具适配器的受控查找与调用, 不返回客户端; HandlerInfo 和 SkillInfo 不提供 execute 或私有回调。

第 10 节的 tools.call_async / services.call_async 是各类别直接发起调用的便捷入口, 不把找不到目标当成成功返回 None。目标被替代等原因不可用时, 按第 13 节返回明确错误。直接调用和先 find 后调用均不能绕过所属类别的分派检查。

### 10.2 宿主接口注入与执行期作用域

PluginHost 与 Service / AsyncService 一并通过 satrap.edictum 公开导出。统一支持注入的工厂为 get_tools, get_services, build_commands, build_handlers 和 MCP 的 build_clients。业务插件需要宿主接口时, 由对应工厂接收可选的仅关键字参数 plugin_host, 再显式传给本插件实例的构造方法。无此需求的插件保持原写法, 不强制给每个类新增宿主接口参数。技能继续沿用已有声明与加载方式, 不为注入新增技能工厂。

plugin_host 表示插件访问宿主能力的接口, 类型为 PluginHost, 由宿主创建并经函数参数传入插件, 提供环境查询, 受控插件访问和产物接口。业务参数仍通过每次调用的参数映射单独传递, 不装入 plugin_host。实例保存 self._plugin_host = plugin_host 后即可在自己的方法中使用这些入口, 不需要自己查找宿主实例或导入其他插件实现。

- plugin_host 是新增工厂协议的仅关键字保留参数, 由显式声明选择注入; 既有 host 参数不用于该接口注入, 不因只有 **kwargs 就传入 plugin_host
- 加载器只在真实签名显式声明仅关键字 plugin_host 时注入, 类型注释用于静态检查而不作为注入识别机制; 按各工厂原有参数规则预绑定, 选定一次合法调用后只执行一次工厂
- 是否声明 interop 不影响 plugin_host 注入; 未声明 interop 的插件也可按签名接收 plugin_host, 使用 environment_snapshot() 和 artifacts 等宿主接口, 具体操作仍遵守原权限规则
- plugin_host.plugins 的跨插件访问仍须有对应 requires 声明, 获得 plugin_host 不自动授予依赖访问权
- 旧工厂不声明 plugin_host 时不额外传入, 旧工具也不要求调用新增构造方法; 不修改既有参数的含义
- 安装期 plugin_host 绑定当前会话, 当前插件身份与安装代次, 可以保存到实例; 不把安装期某个用户或某轮 CallOrigin 永久存入其中
- 插件身份与安装代次仅供框架内部校验, 首版不公开 plugin_name 或安装代次属性; 公开入口为第 9 节的环境快照, plugins 和 artifacts, 实现时明确其类型与方法签名
- 每次调用通过既有执行作用域取得当前真实用户, 群, 平台账号和工作流来源; 并发请求使用各自作用域, 不写共享的 current_user 等可变字段
- A 调用 B 服务, B 再调用 C 时, B 使用绑定自身插件的 plugin_host 访问 C, 按 B 的 requires 声明检查依赖; 调用链记录 A -> B -> C, 原始用户和审批来源始终保持不变
- 新增服务身份不扩大原始入口已有授权, 也不能把模型工具来源变成可信原生命令来源
- 没有有效执行作用域时不能靠缓存 plugin_host 发起跨插件业务调用; 后台任务需由已有宿主任务入口建立有效作用域, 首版不新增后台调度机制
- plugin_host 随所属插件失效, 提供者引用随提供者安装代次失效; 重装后必须重新创建消费实例或重新取得引用, 不重用旧对象

模型工具, 原生命令和处理器入口继续由宿主建立原有执行作用域。安装期只构造实例并校验依赖, 不提前调用其他插件的业务服务。跨插件调用返回后恢复外层调用作用域, 异常或取消也不能遗留提供者身份。

### 10.3 服务分派的具体执行顺序

capabilities.py 提供服务专用分派入口, call_sync / call_async 与服务引用共用同一套目标解析和校验, 不各写一份权限规则:

1. 从调用方绑定的 PluginHost 和当前执行作用域确定消费者, 核验消费者会话与安装代次
2. 检查消费者 requires 是否声明目标服务, 再核验提供者 exports 及目标的启用, 替代, 会话形态与安装代次
3. 保持原始用户和入口权限, 核验目标能力授权; 涉及文件, 网络和群消息等副作用时仍使用相应领域接口审批
4. 检查具名参数映射属于普通数据或受控产物引用, 对真实 execute 签名执行 bind 并应用默认值; 缺少必选参数及签名不接受的额外参数在执行前失败, **kwargs 接受的字段继续由服务校验
5. 建立提供者的子调用作用域, 同步只调用 Service.execute, 异步只 await AsyncService.execute, 不隐式桥接或因 TypeError 重试
6. 在统一退出边界检查返回数据范围, 记录子调用结果或异常并恢复外层作用域; 不提交额外 assistant/tool 历史消息

状态检查不要求长时间持有注册表锁。等待权限审批后重新核验有效性, 实现将要产生真实副作用时仍需通过领域接口复核。内部绑定失败和服务实现抛出的 TypeError 必须区分发生阶段, 两者均不能触发第二次执行。

SDK 成功时直接返回业务数据, 失败时抛出异常交给既有工具/命令执行边界包装。交互状态错误使用 PluginInteractionError; 参数绑定失败和业务异常沿用现有异常边界, 不增加一套服务成功/失败信封。可选 find 返回 None 仅表示正常缺失, 不用于吞掉已经开始执行后的业务异常。

按字符串名称调用时, SDK 返回明确的普通数据/产物联合类型, 不承诺自动推导指定服务的精确返回类型。消费方在边界用 isinstance 或类型守卫收窄; 固定结构使用 TypedDict 配合校验辅助函数。重复调用较多时将其集中到本插件自己的带类型辅助函数, 不强制为每个服务新建协议文件或 DTO。

## 11. 安装, 热更新与恢复

继续扩展现有安装器和协调流程, 不另建服务安装体系:

1. 校验 interop 引用与关系
2. 通过原有工厂构造五类能力; services.py 按第 4.2 节收集 Service / AsyncService 类, 或使用 get_services 返回的实例
3. 对照真实注册结果绑定 exports, 任一错误走现有回滚
4. 核验依赖, 服务注册与替代保护等关系后激活, 不向并发调用暴露半成品
5. 禁用/卸载时先使公开能力引用失效, 再按既有机制清理

旧工厂的 session / config / resources 参数保持原含义。需要宿主接口的工厂增加可选 plugin_host 关键字参数, 签名预绑定后执行, 不以捕获内部 TypeError 的方式重跑工厂。PluginResources 继续只管理模型依赖与客户端。

公开能力引用绑定会话及提供者安装代次。重新启用或重装后旧引用仍失效, 下一次操作重新取得。引用不因名称相同而透明重绑定。

等待审批后及真实副作用前核验有效性。已经执行的外部副作用不能由注册表自动撤销, 也不能假设任意同步回调都能被强制终止。取消与资源关闭沿用现有执行器能力, 不持锁等待插件或模型。

框架内部维护交互格式与能力适配协议版本, 作者 YAML 默认隐藏。省略格式版本始终表示首版格式, 不随软件升级指向新格式; 将来出现不兼容语法时再提供显式迁移入口, 不靠隐藏版本猜测历史语义。

跨进程恢复使用稳定资料:

- 规范化的 exports / requires / replaces / conflicts, 含 services 的声明与调用签名
- 实际提供者原有插件版本, 代码与有效配置指纹
- 对应工具定义, 命令定义及其他能力结构摘要
- 宿主作用域和真实目录绑定

安装代次只用于进程内撤销, 不当作稳定恢复指纹。接口变化不匹配时明确拒绝旧任务恢复, 不复用旧批准或重放已完成副作用。

服务声明, 实现和调用签名变化纳入恢复核验。services 不得因恢复旧任务而被当作模型工具执行; 从 tools 改成 services 是调用契约变化, 不重定向旧调用, 已完成步骤仍不重放。

不再要求作者填写单独 api 版本, 但框架不会因此声称不同插件发布版本一定兼容。参数仍需按当前真实定义校验, 破坏性行为变化仍由插件作者管理自身发布版本与说明。

## 12. 现有交互的落地结果

| 交互 | 采用方式 |
| --- | --- |
| code_sandbox 与 coding 文件操作 | 共用宿主环境, 无需互相 requires |
| code_sandbox 与 Shell | satrap_coding 通过 interop.replaces 声明 shell 替代 base_take.code_sandbox, 自动压制原入口并保留用户开关 |
| coding 或其他插件调用 base_take 工具 | base_take 导出目标工具, 调用方声明依赖, 受控分派 |
| group_chat 导入沙箱图片 | 使用宿主产物接口后走现有群资产登记, 不依赖文件由哪个插件创建 |
| base_take 目录外读取 | 本轮不放开, 后续通过统一宿主审批接口处理 |
| 权限插件拆分 | 后续提供管理命令, 通用授权由宿主接口维护, 不暴露 PermissionEngine 私有对象 |

full-access 不扩大 code_sandbox worker 的联网, 导入库或文件范围。两个工具共享目录也不表示 Shell 变成受限 worker, 或 worker 获得 Shell 权限。

## 13. 代码位置与前端展示

公共包位于 satrap/core/plugin_interop/, 不在 core 根目录堆零散文件:

| 模块 | 职责 |
| --- | --- |
| contracts.py | 六类能力声明, 冻结元数据, 普通业务数据范围, 图片引用及交互错误类型 |
| service.py | Service / AsyncService 基类, 服务元数据及同步/异步执行约定 |
| environment.py | 宿主环境绑定和快照版本 |
| capabilities.py | PluginHost, 六类受控访问与 exports 绑定, 依赖/关系预检, 工具/服务分派与失效 |
| artifacts.py | 通用沙箱图片有界读取及固定内容 |

框架另新增该公共包的 __init__.py 作为导出入口, 上表加导出文件共六个框架 Python 文件; satrap.edictum 同步公开导出 PluginHost / Service / AsyncService。services.py 是需要提供服务的业务插件自己的可选代码文件, 不是每个插件必建, 也不额外新增根级框架模块。

加载器继续在 Edictum 原有模块接入, 框架公共包不导入 satrap/expend/plugins 中的业务实现。沙箱 worker 等逻辑继续留在 core/utils/sandbox。

插件管理界面展示:

- 对外开放的六类能力
- services 单独展示为插件专用能力, 不混进模型工具列表
- 按插件名列出的必需/可选依赖
- 替代与排斥关系及命中入口
- 用户期望开关与实际不可用原因, 两者分开
- 配置变更对依赖者的影响
- 替代新破坏原本满足的必需依赖时列出目标能力与依赖者, 阻止变更而非默认连带停用; 原本已失效的依赖不作为替代新增冲突

不额外展示隐藏版本字段或要求用户选择内部适配协议。

SDK 使用带稳定 code 的 PluginInteractionError, 由现有 HTTP/工具边界包装。保留以下九个原因, 不为每个细小不可用状态新增 code:

| code | 适用边界 |
| --- | --- |
| dependency_unavailable | 已声明依赖不存在, 关闭, 被替代或不适配当前会话; 直接调用被替代能力使用此 code, 附带 replaced_by 原因及目标信息 |
| capability_private | 直接请求调用未对外开放的能力, 不能因已知名称而执行; 可选 find 对已声明但未开放目标只返回 None |
| capability_not_declared | 当前消费者没有声明对应插件或具体能力依赖 |
| capability_stale | 已取得引用的安装代次或绑定作用域失效, 不因后来出现同名能力而自动重绑定 |
| permission_denied | 当前用户, 原始入口或领域操作权限不满足 |
| replacement_conflict | 同一目标存在两个或更多生效替代者 |
| replacement_breaks_dependency | 替代新破坏原本满足的必需依赖, 或安装/重新启用的依赖者被现有替代阻断 |
| plugin_conflict | 命中了显式 conflicts 关系 |
| interaction_cycle | 必需依赖或替代关系构成循环, 不归入 replacement_conflict |

错误判断先核验声明与访问边界, 再判断目标状态。已声明且对外开放但被替代的目标, 按名称新发起调用时报告 dependency_unavailable; 如果持有的引用同时已因安装代次或作用域变化失效, 则报告 capability_stale。原因详情说明关闭, 被替代或调用形态不支持等具体情况, 不将错误码当成语义等价或自动回退的依据。

YAML 未知键, 本插件能力名称不存在, 自引用, 必需依赖与排斥的硬矛盾及关系名称拼写错误等预检失败沿用现有配置错误边界, 不硬塞进上述运行时 code。日志记录定位信息, 非预期异常保留堆栈, 不记录密钥, 完整文件或对话正文。

## 14. 迁移批次与验收

A1. 六类能力与受控访问

- 增加声明解析, 真实注册结果校验和只读 PluginHost; 同步扩展 CAPABILITY_KINDS, Plugin, 目录/规格及能力管理接口的 services 类别
- 完成 exports / requires, 同步/异步分派, find / call API 及原始调用来源传递
- 统一接入 get_tools / get_services / build_commands / build_handlers / build_clients 的旧签名适配与可选 plugin_host 注入, 保持各工厂原有返回及同步/异步语义; 分别绑定调用者身份, 当前执行来源及提供者子调用作用域, 不缓存安装期用户身份
- 同批完成 services 类别, Service / AsyncService 基类, 类收集/可选工厂和受控调用; services 从注册源头与模型工具分开, 不采用隐藏工具补丁
- 原五类能力保持执行语义, 新 services 只提供插件调用; 所有类别均不返回原始实例
- 接入安装回滚, 启停撤销, 恢复指纹, 原入口权限与最小管理状态展示
- 独立验收服务可被合法插件调用, 不进入模型工具清单且不能被模型猜名调用, 可选缺失与形态不匹配处理一致

A2. 替代, 排斥与关系保护

- 实现 replaces / conflicts, 必需依赖与排斥的静态硬矛盾检查
- 实现增量替代保护, 新安装/重新启用校验, 循环和多替代者冲突检查
- 完成关系预览与影响说明, 所有关系只合成实际可用性, 不改用户保存的开关
- 补齐目标改名, 更新失败回滚及工具与服务不能跨类别替代等场景
- A1 若作为独立中间版本运行, 非空 replaces / conflicts 必须明确报尚未支持, 不得默默忽略; 完成 A2 后才开放完整关系配置

B. 统一宿主环境, 迁移 base_take / coding / group_chat 的目录来源, 将 coding 自动停用 code_sandbox 的行为从宿主硬编码迁入 replaces 声明

C. 抽取沙箱图片通用读取, 验证绘图 -> 导入 -> 群回复, 保留原资产授权

D. 单独设计并实施权限插件拆分及 base_take 目录外读取审批, 不在此前批次提前放开

本轮实际实现范围为 A1 -> A2 -> B -> C, 每批完成后形成可独立审阅的变更和验证记录。D 仅作为后续方向, 不混入本轮完成条件。当前已开始运行代码修改, 各批次验收状态见第 18 节。

集中业务验收:

- 两个插件只声明名称和能力就能完成公开工具调用, 未导出/未声明/已关闭的能力均不能绕过
- 已启用且导出的服务可被合法插件调用, 不出现在模型或动态 MCP 工具清单, 模型猜名和恢复旧调用也不能进入服务注册表
- 服务类或工厂只收集一次, service_name 与 exports 一致; 同步/异步服务形态, 参数绑定和普通数据/产物返回边界正确, 内部 TypeError 不触发重试
- 私有服务未导出时不默认告警; 服务关闭后所有跨插件调用拒绝
- 合法高层工具可调用服务, 仍保留原用户权限与审批, 不把专用类别当作阻止所有间接调用的保证
- 必需依赖失败阻止激活, 可选依赖失败不影响无关功能
- 必需依赖与排斥同一插件整体或同一能力时静态拒绝, 不同能力及 optional 变体允许
- 替代者启停后原开关不变, 多替代者和循环关系明确报错
- 替代新破坏原本满足的必需依赖时拒绝变更, 原目标已关闭的既有失效不拦截替代; 恢复目标与替代同批发生时仍检查新破坏
- 新安装或重新启用依赖者遇到已生效替代时明确拒绝, 不先启用再显示 blocked; 可选依赖按显式降级处理
- 排斥只报告冲突并拒绝目标配置, 不自动关闭其他插件
- tools / services 跨类别替代拒绝; 同时替代并排斥整个目标插件不误报硬矛盾, 能力级合理组合仍可共存
- 原五类能力保持执行语义, 新 services 不成为模型工具, 处理器不重复调度, 命令不变成管理旁路
- MCP 经现有工具适配器执行, 不泄露客户端和凭据
- 可选插件整体查询与各类 find 行为一致, 未声明调用报错, 已声明但不可用返回 None; 同步/异步形态不匹配时不偷偷启动桥接
- 能力级依赖允许查询目标 PluginInfo, 不借查询扩展对其他能力的访问
- 目标更新删名, 自引用及未知 exports 类别在预检被拒绝; 运行时错误能区分多替代者, 循环和依赖破坏
- 安装失败不留公开引用, 工具, 连接或其他资源
- 项目改绑, 插件重装和会话恢复不能复活旧权限与旧引用
- 同步/异步, Chat/平台使用相同依赖及有效性语义
- 使用第 17 节的双插件链路验收类收集, 工厂注入和真实受控分派, 同步/异步分别验证, 不通过直接调用服务实例替代集成检查
- 同会话两个用户并发调用及 A -> B -> C 嵌套调用保留原用户来源, 正常退出, 异常和取消均恢复外层作用域; 无执行作用域及旧消费方 plugin_host 均拒绝调用
- 服务缺少必选参数及签名不接受的额外参数在执行前失败, **kwargs 按 Python 语义接收额外字段, 方法内部 TypeError 只执行一次; 非法返回对象不透传, 动态结果在消费边界可收窄到具体类型
- 无 interop 的工厂仍可获得 plugin_host 并在原权限范围内使用宿主环境/产物接口, 未声明依赖时跨插件访问拒绝; 工厂注入的服务可继续调用依赖服务, 无参类收集不隐式注入 plugin_host
- 五个工厂入口按显式仅关键字 plugin_host 注入, 既有 host 参数及只接收 **kwargs 的工厂不因此获得该对象; 类型注释不参与注入识别, 插件身份与安装代次不成为公开属性
- 同一插件 tools.foo 与 services.foo 可同时注册并分别调用, 不报跨类别同名告警, 同类别重名仍拒绝

优先扩展现有集中流程测试, 不为每个元数据字段建立独立小测试模块。Pyright basic 保持无错误和警告。

## 15. 不包含

- provides 声明, 每项 API 版本, 依赖别名及消费者列表; services 类别及其可选代码入口则包含在本轮
- 事件总线, 任意 RPC, 跨会话发现和自动安装依赖
- 同名能力偷偷覆盖, 旧调用重定向或按加载先后决定替代者
- 原始插件实例, MCP 客户端或权限引擎对外暴露
- 通用资产数据库, 文件监控, 自动图片发送
- 本轮直接拆权限插件或放开目录外图片读取

## 16. 本轮完成后的 YAML 示例

下面是本轮 A1/A2/B/C 支持的声明。业务插件需要实际的 Python 工具/服务实现, YAML 不自动生成代码。

### 16.1 提供者 media_backend

该插件在 services.py 中通过 Service / AsyncService 实现并注册 transcode_video, 不把它注册为 Tool。服务只供合法插件调用:

```yaml
name: media_backend
version: 1.0.0
description: 媒体处理基础能力

interop:
  exports:
    services: [transcode_video]
```

- transcode_video 不进入模型工具列表, 不需要额外隐藏字段
- exports.services 允许声明了相应依赖且通过权限检查的插件调用
- 若服务开关关闭或 media_backend 停用, 调用和必需依赖均不再满足
- 插件私有函数不自动成为服务, 其他插件也不能直接取得业务函数对象

### 16.2 消费者 video_workflow

该插件通过 tools.py 提供面向模型的 make_video, 内部按声明调用转码服务。保留对 base_take 的可选文档读取和独立目标上的替代/排斥示例:

```yaml
name: video_workflow
version: 1.0.0
description: 面向用户的视频制作工作流

interop:
  exports:
    tools: [make_video]
    commands: [video_status]

  requires:
    media_backend:
      services: [transcode_video]
    base_take:
      optional: true
      tools: [read_document]

  replaces:
    legacy_video:
      tools:
        create_video: make_video

  conflicts:
    unsafe_executor: true
```

预期行为:

1. media_backend.transcode_video 作为 services 能力满足必需依赖, 不需要也不会拥有模型工具入口
2. 模型选择 make_video, 其代码调用 plugin_host.plugins.services.call_async 或取得服务引用后调用, 保留原始用户和审批约束
3. base_take 未安装或 read_document 未开放/关闭时, 通过 find 返回 None 后由业务代码降级, 不阻止工作流启用
4. 依赖服务允许查询 media_backend 的 PluginInfo, 但不允许顺带访问该插件其他未声明能力
5. legacy_video 未安装时替代规则未命中, 已安装时执行增量必需依赖保护, 不把服务替代成模型工具
6. 若其他启用插件原本必需依赖 legacy_video.create_video, 本次替代新破坏该依赖时拒绝变更
7. unsafe_executor 同时启用时报冲突, 不自动关闭它
8. video_workflow 退出后原目标按用户开关恢复, 不重放或重定向历史调用

本例不写 schema_version / api / 依赖别名 / priority, 也不恢复 provides 或 model_hidden。新增的只是明确的第六类 services, 公开与依赖仍沿用原有简洁写法。

## 17. 双插件交互代码示例

以下是本轮支持的最小完整同步链路, 用于说明开发者需要写什么。Service / AsyncService / PluginHost 已实现; 示例仍为开发文档, 未向内置插件目录新增这些文本业务插件。

### 17.1 文件与依赖声明

两个业务插件各自维护实现和 meta.yaml, 不互相导入业务模块:

```text
text_backend/
    meta.yaml
    services.py

text_workflow/
    meta.yaml
    tools.py
```

提供方 text_backend/meta.yaml:

```yaml
name: text_backend   # 服务提供者的真实插件名
version: 1.0.0   # 插件自身发布版本
description: 插件专用文本处理能力   # 插件管理界面中的说明

interop:   # 声明插件间交互关系
  exports:   # 允许其他插件访问的能力
    services: [normalize_text]   # 对外开放真实注册的服务名
```

提供方 services.py 使用第 4.2 节完整的 NormalizeTextService 实现, 由加载器收集可无参构造的 Service 子类, 不需要额外 get_services。text 与可选 lowercase 直接来自 execute 签名; 不注册模型工具。

消费方 text_workflow/meta.yaml:

```yaml
name: text_workflow   # 服务消费者的真实插件名
version: 1.0.0   # 插件自身发布版本
description: 面向模型的文本整理工具   # 插件管理界面中的说明

interop:   # 声明插件间交互关系
  requires:   # 激活当前插件所需的依赖
    text_backend:   # 指向服务提供者的真实插件名
      services: [normalize_text]   # 必需依赖该公开服务
```

消费方不需要 exports.tools 才能让模型调用自身工具。模型可见性继续由原工具注册规则决定; 只有希望其他插件也能程序化调用 clean_text 时, 才额外导出 tools。

### 17.2 消费方工具与宿主接口注入

text_workflow/tools.py:

```python
"""通过公开服务整理用户文本"""

from satrap.core.utils.TCBuilder import Tool
from satrap.edictum import PluginHost, SimpleSession


class CleanTextTool(Tool):
    """向模型提供文本整理能力"""

    tool_name = "clean_text"
    description = "整理文本中的多余空白"
    params_dict = {
        "text": ("string", "待整理的文本"),
    }

    def __init__(self, plugin_host: PluginHost) -> None:
        """
        保存宿主注入的插件调用入口

        参数:
        - plugin_host: 当前插件的受控宿主接口
        """
        super().__init__()
        self._plugin_host = plugin_host

    def execute(self, text: str) -> str:
        """
        调用依赖服务整理文本

        参数:
        - text: 待整理的文本

        返回:
        - 整理后的文本; 服务不可用或返回类型不符时抛出异常
        """
        result = self._plugin_host.plugins.services.call_sync(
            "text_backend",
            "normalize_text",
            {"text": text},
        )   # 当前用户来源与权限由分派器在执行时取得

        if not isinstance(result, str):
            raise TypeError("normalize_text 应返回 str")

        return result


def get_tools(
    session: SimpleSession,
    *,
    plugin_host: PluginHost | None = None,
) -> list[Tool]:
    """
    创建当前会话的文本工具

    参数:
    - session: 当前同步会话, 本例不直接读取
    - plugin_host: 宿主注入的访问接口, 缺失时拒绝构造

    返回:
    - 绑定受控调用入口的工具列表
    """
    if plugin_host is None:
        raise RuntimeError("缺少插件宿主接口")

    return [CleanTextTool(plugin_host)]
```

此处的 params_dict 属于既有模型工具, 服务自身不需要一份重复参数表。get_tools 展示只接收所需 session 和 plugin_host 的最小工厂签名; 加载器按第 10.2 节适配, 不强制写出未使用的 config / resources。

模型执行 clean_text 后, 只产生一条对模型的工具结果。normalize_text 属于该次执行的内部子调用, 其执行结果直接回到 CleanTextTool.execute, 不另外插入消息历史。服务业务错误直接向上抛出, 由统一调用边界记录和包装, 本例不逐层捕获并重复记录。

### 17.3 异步和可选调用

异步链路使用同一 YAML 名称, 但提供方需实际提供 AsyncService, 消费方使用 AsyncTool 及对应异步会话工厂。保留业务签名, 将服务与工具的 execute 改为 async def, 消费方调用如下:

```python
result = await self._plugin_host.plugins.services.call_async(
    "text_backend",
    "normalize_text",
    {"text": text},
)
```

这是异步方法体中的调用片段, 不是同步示例可以不修改类定义就直接执行的替换。纯 CPU 文本计算不要求为示例包装线程; 文件或网络等阻塞实现仍按现有异步执行规范处理, 不直接阻塞事件循环。

若文本整理只是可选增强, 在 requires.text_backend 下加入 optional: true, 调用代码可先查找:

```python
service = self._plugin_host.plugins.services.find(
    "text_backend",
    "normalize_text",
)
if service is None:
    return text   # 可选增强不可用时保留原文

result = service.call_sync({"text": text})
if not isinstance(result, str):
    raise TypeError("normalize_text 应返回 str")
return result
```

这是同步 execute 方法体片段。find 成功之后目标仍可能被关闭或重装, call_sync 会重新检查; 不把后续业务异常当成“没有可选服务”而自动吞掉。异步引用调用使用 await service.call_async(...)。

### 17.4 现有插件的接入方式

- 插件复用另一插件的既有模型工具时, 仍通过 plugin_host.plugins.tools.call_sync / call_async, 不要求把所有旧工具改成服务
- 新增只供程序复用的原语时使用 Service / AsyncService, 模型工具和处理器可共享本插件内部业务函数, 不必在本插件内部绕行跨插件分派
- code_sandbox 与 coding 首先读取共同的宿主环境接口, 不为共享目录相互声明服务依赖, 不改变受限 worker 的权限
- group_chat 读取沙箱产物时通过宿主产物接口固定内容, 再走已有 AssetStore 登记与群回复权限, 不直接导入沙箱插件私有实现
- replaces / conflicts 在安装和目标配置变更时处理, 业务代码无需主动禁用其他插件, 也不能自行将旧服务名重定向到替代者

实施时在既有集中流程测试中构造这些最小插件, 由真实加载器安装并通过受控入口执行。本文示例语法验证不能替代 A1/A2 的运行时集成验收。

### 17.5 服务通过工厂获取 plugin_host

服务需要继续调用其他插件时, 与第 17.2 节工具采用相同的构造函数注入。下面可作为 text_workflow/services.py, 沿用第 17.1 节对 text_backend.normalize_text 的依赖, 不必新增注入字段:

```python
"""通过工厂注入宿主接口的文本服务"""

from satrap.edictum import PluginHost, SimpleSession, Service


class PrepareTextService(Service):
    """复用依赖插件的文本规范化服务"""

    service_name = "prepare_text"
    description = "通过依赖服务规范化文本"

    def __init__(self, plugin_host: PluginHost) -> None:
        """
        保存宿主访问入口

        参数:
        - plugin_host: 当前插件的受控宿主接口
        """
        super().__init__()
        self._plugin_host = plugin_host

    def execute(self, text: str) -> str:
        """
        调用依赖服务准备文本

        参数:
        - text: 待处理文本

        返回:
        - 规范化后的文本; 依赖不可用或结果类型不符时抛出异常
        """
        result = self._plugin_host.plugins.services.call_sync(
            "text_backend", "normalize_text", {"text": text},
        )
        if not isinstance(result, str):
            raise TypeError("normalize_text 应返回 str")
        return result


def get_services(
    session: SimpleSession,
    *,
    plugin_host: PluginHost | None = None,
) -> list[Service]:
    """
    构造需要宿主访问入口的服务

    参数:
    - session: 当前同步会话, 本例不直接读取
    - plugin_host: 宿主注入的访问接口, 缺失时拒绝构造

    返回:
    - 已保存宿主接口的服务实例列表
    """
    if plugin_host is None:
        raise RuntimeError("缺少插件宿主接口")

    return [PrepareTextService(plugin_host)]
```

关键是 get_services 接收宿主传入的对象, 再将同一对象交给服务构造方法保存; requires 只声明可访问的依赖, 不自动给实例设置属性。由于存在工厂, 加载器不再无参扫描构造该模块中的服务类。prepare_text 是否对其他插件开放仍由 exports.services 单独决定。


## 18. 实施与验收记录

2026-10-17 开始实施, A1 -> A2 -> B -> C 已落地; 2026-10-18 按后端, 前端和文档分批提交。D 保留为后续方向, 本轮没有拆权限插件或开放 base_take 目录外读取。

| 批次 | 实际落地结果 | 主要证据 |
| --- | --- | --- |
| A1 | 独立 Service / AsyncService; 六类 exports / requires; 五工厂显式仅关键字 plugin_host 注入; 服务权限与原工具/命令分派共用 | plugin_interop/ 六个模块, Edictum 安装器, plugin_permissions.py, test_plugin_interaction_flows.py |
| A2 | 完整目标关系预检, 依赖/替代合并图, 多替代者和排斥拒绝, 增量依赖保护, 原开关保留, 批次失败/取消回滚 | capabilities.py, plugin_runtime.py, 真实安装/更新/开关与资源回滚场景 |
| B | Chat 与平台统一绑定 EnvironmentSnapshot; 官方基础, 编程, RAG 和群插件完成迁移; 旧 coding_* 路径属性已移除, 自动停用行为改由官方 replaces 声明实现 | display/service.py, SessionManager.py, 官方插件调用入口, 项目/审批/官方替代与恢复场景 |
| C | 宿主产物接口有界读取图片并冻结来源; group_chat 使用该接口后复用原 AssetStore 登记和发送授权; 群 SDK 仅保留领域登记职责 | artifacts.py, group_chat/tools.py, group_chat/sdk.py, 真实绘图 -> 导入 -> 群回复链路 |

集中场景与逐项证据:

| 验收范围 | 已验证内容 |
| --- | --- |
| 声明与工厂 | 四组声明严格解析六类名称, 自引用/硬矛盾拒绝; 真实注册结果核验; 旧 host 和只有 **kwargs 的工厂不被注入; 工厂实现只执行一次 |
| 模型与服务分离 | 同名 tools.echo / services.echo 分别调用; 服务不进入模型定义, 猜工具名无法命中服务; 类收集与工厂不重复构造 |
| 调用数据 | 必选/额外参数绑定, 默认值与 **kwargs 的 Python 语义; 内部 TypeError 不重试; 容器复制, 非法对象拒绝; 图片引用退出执行后失效 |
| 来源与权限 | 同会话双用户并发, A -> B -> C 嵌套调用保留原用户; 服务复用已有管理权限; 公开命令不能借插件调用取得原生命令信任 |
| 六类访问 | 工具和服务使用真实分派器; MCP 使用真实适配器与离线传输并保留用户; handler/skill 返回只读资料, 查询不执行回调或修改技能 |
| 可选与失效 | 已声明可选缺失返回 None, 未声明拒绝; 关闭能力或插件后拒绝调用; 重新启用/重装不复活旧引用; 等待中停用再启用仍拒绝旧结果, 取消后作用域恢复 |
| 关系变更 | 既有有效必需依赖受保护, 已 blocked 不冒充新增破坏; 最终目标中恢复能力仍检查替代影响; 新安装/重新启用依赖者拒绝不兼容目标; 多替代者和循环错误可区分 |
| 更新与取消 | 只读预览不执行插件代码; 提供者优先于消费者安装; 目标版本删名后回滚整批配置; 中途取消关闭等待连接并回收本批新安装插件 |
| 技能资源 | 替代移除原指令与自带工具; 替代者退出只恢复用户仍启用的技能; 资源激活失败恢复原开关和有效指令; 不修改历史用户消息 |
| 环境与沙箱 | 改绑切换读取目录并递增 revision; 写入审批, 排队读取和 worker 启动/运行期间复核原环境; 插件可以同时安装, Shell 可用时压制基础沙箱工具; full-access 不扩大 worker 库范围 |
| 恢复 | 关系声明, 服务实际签名, 实现与配置和稳定目录进入恢复指纹; 改变服务签名后真实 resume_run 拒绝旧任务; 安装代次和环境 revision 不用作持久化身份 |
| 群资产 | 正常图片与错误路径/格式/大小覆盖; 读取前后复核权限与环境; 本群/本轮资产授权, 路由与主工作流约束保留; 无自动发送或通用资产数据库 |
| 管理展示 | 六类能力及交互关系展示; Chat 能力开关采用 requested_enabled; 运行实例单列实际可用原因与 replaced_by; 浏览器场景验证服务开关与替代原因 |
| 文档示例 | 第 4.2 / 17.1 / 17.2 / 17.5 节代码在临时目录真实安装与分派通过; 按第 17.3 节转换为异步形态后同样通过, 未新增示例业务插件 |

最终检查记录:

- 前序完整后端集中业务回归: 25 个业务模块, 1230 项通过, 2 项跳过 (本机无文件符号链接权限; 真实外部视觉 API 集成默认不运行)
- 最终移除未再使用的技能装配私有钩子后, 插件安装与交互集中回归 80 项通过; 旧路径属性, 宿主硬编码互斥及这些废弃钩子在生产代码中已无引用
- 本轮全部生产改动及新公共包 Pyright: 0 errors, 0 warnings
- 前端 npm run build: 通过
- 前端 npm test: 18 个文件, 300 项通过
- 前端 npm run test:e2e:plugins: 通过, 已纳入六类开关, 依赖/替代关系及实际替代原因显示
- 独立三进程插件应用集成: 1 项通过, 使用隔离配置与离线模型, 不连接用户平台
- git diff --check: 通过

边界说明: 本机验证环境为 Windows; 本轮没有宣称完成 Linux/macOS 的实机验收。MCP 测试使用真实适配器配合离线传输, 群聊测试使用宿主测试适配器, 没有向真实群发送消息。运行中的外部副作用仍按原执行器能力处理, 注册表不承诺撤销已完成的操作。

### 18.1 2026-10-18 行为修正

按用户确认保留 coding 启用后自动停用基础沙箱工具的行为, 撤回此前让 Shell 与 code_sandbox 同时生效的处理。自动停用使用官方插件声明, 不恢复 ChatService 的硬编码:

```yaml
interop:
  replaces:
    base_take:
      tools:
        code_sandbox: shell
```

- satrap_coding.shell 结构可用时, base_take.code_sandbox 实际不可用, 显示 replaced_by 原因; 用户保存的独立开关不被改写
- coding 停用/卸载或 shell 独立关闭后, code_sandbox 按用户原开关恢复; 用户已关闭 code_sandbox 时不会自动打开
- 两插件可以同时安装, 任一安装顺序采用相同替代规则; coding 单独安装不要求 base_take 存在
- 共享宿主目录, worker 边界和各自原审批规则保持原定义
- 本次交互回归 17 项通过, 插件安装/配置/目录及编程配置回归 264 项通过; 补充卸载与重新安装断言后, 官方替代场景 4 项通过; git diff --check 通过

## 19. 权限插件拆分

2026-10-18 实施 D 中的权限管理拆分, 不包含 base_take 目录外读取。

- /approve 与审批模型配置迁至官方 permissions 插件; /goal, /plan, 文件与 Shell 保留在 coding
- 审批契约, 策略, 审查协调和持久状态由 core/approval/ 统一管理, coding 卸载不重置会话模式
- PluginHost 新增 approvals.snapshot/set_mode/request_sync/request_async; 模式修改只允许具备有效原生作用域的用户管理命令, 插件服务或模型转发不具备修改权限
- 其他插件请求实际操作审批不需要依赖 coding 或导入其私有实现, 请求完成后仍须执行领域边界与实际副作用前复核
- Chat 使用 /api/chat/approval 和 session.approval_provider, 前端入口为“执行审批模式”, 不再依赖 coding 状态
- 旧命令开关与审批模型按全局, 命名配置及会话覆盖迁移; 新插件显式配置优先, coding 配置版本升级为 1 后不重复补回权限插件
- 旧管理员 coding 范围的允许与否决同时复制到 permissions; 文档的 approval_plugin_migration: 1 记录迁移完成, 后续撤销不会重新被推导
- 原审批模式与日志只在目标缺失时迁移并保留原文件, 旧无版本规则继续保守迁移为 user
- full-access 不改变受限 code_sandbox worker, coding 对基础沙箱的声明替代保持原样

### 19.1 拆分验收

- 后端集中回归: 15 个业务模块, 679 项通过, 1 项因本机无 Windows 文件符号链接权限跳过
- 平台命令与权限迁移专项: 71 项通过, 前置门分别按 permissions 和 satrap_coding 核验 approve 与 plan
- 独立冷启动场景覆盖权限插件单独使用, coding 装卸保留模式, 安装失败不改变审批模型, 等待期间配置变化撤销旧请求
- 旧配置通过真实插件协调器安装, 保留自定义数据根, auto 模式, 专用模型和已关闭命令; 管理员允许与否决经实际授权服务复核
- 前端 18 个测试文件共 300 项通过, 生产构建通过; chat-reconnect 与 plugins 两个浏览器场景通过
- 变动生产 Python 的 Pyright 检查与 git diff --check 通过; 无真实模型调用或群消息发送
