"""
display: 面向前端的展示数据层

把对话显示数据 (用户输入 / thinking / 最终输出 / 工具调用状态) 旁路落入独立 db,
供前端聊天页消费; 与 satrap 后端核心 (core 会话/工作流, edictum 简易会话) 解耦,
经 api 层暴露给前端
"""
from satrap.display.plugins import ChatPluginRegistry
from satrap.display.recorder import DisplayRecorder, list_conversations
from satrap.display.service import ChatService

__all__ = ["DisplayRecorder", "list_conversations", "ChatPluginRegistry", "ChatService"]
