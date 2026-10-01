# Edictum 命名会话设置

在管理界面的「会话 → Edictum 会话 → 编辑配置」中, 内置 `simple` 和 `async_simple` 类型提供独立的系统提示词输入框

- 勾选「配置系统提示词」后, 文本保存到 `params.system_prompt`
- 保持勾选并清空文本, 会清空已有系统提示词
- 不勾选表示不配置此字段, 初始化时保留上下文已有系统提示词; 群配置仍可提供提示词覆盖
- 旧 JSON 中的 `system_prompt` 会自动回填到输入框, 其他会话参数继续在 JSON 中编辑
- 保存并应用运行时配置会沿用既有上下文数据库, 不清空用户和助手的对话历史

自定义 Edictum 类型继续使用其原始 JSON 参数, 不强制采用内置类型的提示词约定

## 模型子配置

内置类型提供默认思考强度、温度、`top_p` 和最大输出 token 数控件

- 思考选项与 Chat 共用绑定模型的 `thinking_fields` 和 `thinking_levels`, 默认关闭
- 生成参数留空时继承绑定模型, 填写时只覆盖当前会话; 模型重载仍保留这些覆盖
- `run(thinking=...)` 可逐次覆盖会话默认思考强度, 不修改后续轮次
- 同步、异步、流式和非流式调用使用相同配置
- 旧的顶层 `temperature`、`top_p`、`max_tokens` 会迁移到 `model_params`, 冲突或非法值明确报错

配置示例:

```json
{
  "system_prompt": "你是群聊助手",
  "thinking": "high",
  "model_params": {
    "temperature": 0.2,
    "top_p": 0.9,
    "max_tokens": 2048
  }
}
```
