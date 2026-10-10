---
name: message_forward
description: 在群聊和私聊中按真实消息 ID 转发原文, 区分原消息与机器人创建的文字合集
---

转发原文使用 message_forward_send, 只填写原消息 ID, 不复述后填入 compose
merge 合并多条消息, existing_forward 原样转发一个已有合并转发卡片
read 的结果是可能截断的预览, 不用于重新构造原文
compose 创建机器人写的新文字合集, 不能说成来自其他成员的原文
跨对话可直接使用模型工具, 来源和目标明确时执行; 目标不明确时先询问, /forward 是可选快捷命令
后端检查真实发送者的 read, write 和 cross 权限; 工具存在不代表本次获授权, 权限拒绝时如实说明
success 表示平台返回消息 ID, failed 不是成功, unknown 时不要重复发送
