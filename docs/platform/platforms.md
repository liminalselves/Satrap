# 平台接入

Satrap 用统一的 `PlatformAdapter` 把不同聊天平台接入后端。平台消息会被转换成统一事件, 交给 `PipelineScheduler`, 再路由到对应 Session。

## 平台配置结构

```yaml
platforms:
  - id: misskey1
    type: misskey
    session_type: assistant
    settings:
      base_url: https://misskey.example.com
      api_token: ${MISSKEY_API_TOKEN}
      chat_enabled: true
      room_enabled: false
```

字段说明:

| 字段 | 说明 |
| --- | --- |
| `id` | 适配器实例唯一 ID |
| `type` | 适配器类型, 如 `misskey`, `onebot`, `aiocqhttp` |
| `session_type` | 入站消息使用的会话类配置名称; 未设置时优先使用与适配器类型同名的会话类, 再回退到全局 `default_session_type` |
| `settings` | 适配器专属配置 |

同一种平台可以配置多个实例, 只要 `id` 唯一即可。

## Misskey

```yaml
platforms:
  - id: misskey1
    type: misskey
    session_type: assistant
    settings:
      base_url: https://misskey.example.com
      api_token: ${MISSKEY_API_TOKEN}
      chat_enabled: true
      room_enabled: false
```

常用 settings:

| 字段 | 说明 |
| --- | --- |
| `base_url` | Misskey 实例地址 |
| `api_token` | API token |
| `chat_enabled` | 是否启用 chat 消息 |
| `room_enabled` | 是否启用 room 消息 |

Misskey 适配器会处理 note, chat, room 等会话来源, 并尽量把文件和图片转换为统一消息组件。

Misskey streaming 协议要求把 token 放在 WebSocket URL 的 `i` 查询参数中。Satrap 会对 token 做 URL 编码, 并向底层 WebSocket 客户端注入脱敏日志器, 避免连接调试日志记录明文或编码后的 token; 反向代理、抓包工具和外部观测系统仍应避免采集完整查询字符串。

## OneBot / aiocqhttp

```yaml
platforms:
  - id: qq_bot
    type: onebot
    session_type: assistant
    settings:
      host: 127.0.0.1
      port: 6700
      access_token: ${ONEBOT_ACCESS_TOKEN}
      enable_private: true
      enable_group: true
```

`type` 可以使用 `onebot` 或 `aiocqhttp`。

常用 settings:

| 字段 | 说明 |
| --- | --- |
| `host` | 反向 WebSocket 监听地址 |
| `port` | 反向 WebSocket 监听端口 |
| `access_token` | OneBot access token |
| `enable_private` | 是否处理私聊 |
| `enable_group` | 是否处理群聊 |

## 多平台路由

后端会根据平台实例, `session_type` 和用户来源构建会话 ID。不同平台实例上的同一用户会进入不同上下文, 避免消息串线。平台管理页可直接从已注册且启用的会话类中选择 `session_type`。

手动创建 Session 时可以通过 `adapter_id` 写入会话初始化参数, 供会话类或插件使用; 它不会改变平台入站路由, 入站会话类由平台的 `session_type` 决定:

```bash
satrap session create assistant --id demo --adapter-id misskey1
```

## 平台管理命令

```bash
satrap platform list
satrap platform show misskey1
satrap platform add misskey1 --type misskey --session-type assistant --set base_url=https://misskey.example.com api_token=${MISSKEY_API_TOKEN}
satrap platform update misskey1 --type misskey --session-type assistant --set chat_enabled=true
satrap platform remove misskey1
```

## 自定义适配器

继承 `PlatformAdapter`, 实现 `meta()`, `run()` 和发送方法, 再用装饰器注册:

```python
from satrap.core.platform import PlatformAdapter, register_platform_adapter


@register_platform_adapter("my_platform")
class MyPlatformAdapter(PlatformAdapter):
    async def run(self) -> None:
        ...

    def meta(self):
        ...

    async def send_text(self, session_id: str, text: str):
        ...
```

接收到平台消息时, 适配器应构造统一事件并提交到事件队列。发送消息时, 优先实现 `send_message()`, 最低也要实现 `send_text()`。
