# 对话分类目录与子代理存储

对话记录按平台类型和实例筛选, 支持跨平台查看. Agent, Provider, 会话范围, 用户, 群或目标, 项目, 模型等选项来自已有数据库记录. 列表按实际记录的最近活动排序, 无法确认的来源和时间显示未知

模型上下文和展示历史仍分别编辑, 修改一个数据层不会同步覆盖另一个数据层. 跨平台选择使用平台实例与对话 ID 共同定位, 同名对话不会串到其它实例

## 元数据与兼容

- `context_catalog` 随持久化消息写入和会话初始化登记精确上下文归属, 包括共享上下文, 主工作流上下文, 工作流名称和时间
- 查询只读合并 `session_configs`, `context_sessions`, `user_info`, `conversation_meta`, `projects` 与展示历史, 不激活会话或迁移数据库
- 精确归属优先于旧命名前缀, 旧命名推断会标注出来
- `scoped:v1` 路由使用原始路由键恢复身份, 不猜测哈希 ID. 旧版按用户共享的记录标注为旧版用户共享, 不推断成私聊或某个群
- 平台目录合并当前配置与存储清单. 新平台类型由适配器注册表识别, 已移除类型保持实例目录且标明未知类型
- `StorageLayout.ensure_platform(..., platform_type=...)` 保存类型, 后续未传入类型的绑定保留已有值

## 新平台扩展

适配器可以设置 `display_name` 和 `conversation_catalog_fields`, 并覆写类方法 `conversation_catalog_metadata(connection, route)`

```python
class ExampleAdapter(PlatformAdapter):
    adapter_type = "example"
    display_name = "示例平台"
    conversation_catalog_fields = {"channel": "频道"}

    @classmethod
    def conversation_catalog_metadata(cls, connection, route):
        """
        从已有本地元数据读取频道标签

        参数:
        - connection: 平台数据库只读连接
        - route: 包含会话和原始路由键的通用字段

        返回:
        - 分类字段到展示标签的映射
        """
        return {"channel": "示例频道"}
```

该接口不创建适配器, 不请求平台网络, 不写入数据库. 返回的扩展字段自动进入筛选和标签, 前端无需增加平台名单. OneBot 的群目录查询位于 OneBot 适配器自身, 通用目录服务不包含平台类型分支

## 纯内存子代理

`ContextManager` 和 `AsyncContextManager` 的 `persistent=False` 跳过数据库初始化, 加载, 消息保存, 摘要和用量缓存保存. 这与 `keep_in_memory=True` 的延迟保存语义不同

工作流的 `persist_context=False` 传递此策略. 通用子代理和 Coding 子代理均使用纯内存上下文, 异步子任务使用批次唯一 ID. 纯内存上下文不能启用持久化检查点或执行恢复, 也不加入会话检查点聚合

测试收集前设置临时 `SATRAP_DATA_ROOT`, 子进程继承相同目录. 默认存储布局与控制服务的默认数据目录读取该变量; 显式传入的测试数据库路径仍优先

## 旧记录清理与恢复

旧子代理记录默认隐藏, 可通过记录类别筛选查看. 清理仅接受单条系统消息, 默认子代理提示词精确匹配, 无工具与思考记录, 无正式会话或展示引用, 无已运行任务摘要或用量的候选. 自定义提示词或其它含糊记录保留供人工判断

先预览并保存完整清单, 再用该清单清理:

```text
python -m satrap.core.storage.legacy_context_cleanup preview --platform-id local --preview-file preview.json
python -m satrap.core.storage.legacy_context_cleanup apply --platform-id local --preview-file preview.json
```

可用 `--data-root` 指定数据根目录, `--platform-id` 接受任意实例标识. 写入时按精确 ID 锁定, 在数据库事务内重新检查引用及内容版本, 创建并校验备份后才删除. 备份失败或候选变化会终止操作, 不使用前缀批量删除

默认备份位于对应平台的 `trash/legacy-contexts/`. 恢复使用完整备份:

```text
python -m satrap.core.storage.legacy_context_cleanup restore --platform-id local --backup 完整备份路径.json
```

恢复检查数据库身份, 校验和, 字段和现有占用, 同一事务内恢复消息及相关状态. CLI 入口捕获并记录失败, 返回非零退出状态
