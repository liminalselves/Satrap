# Satrap

一个简洁的 Python 工具库，提供日志记录和大语言模型 (LLM) API 调用封装。

> ⚠️ **注意**: 这是一个早期开发阶段的项目，API 可能会发生变化。

## 功能特性

- **日志系统** - 彩色控制台输出 + 文件持久化
- **LLM 调用封装** - 同步/异步调用 OpenAI 兼容 API
- **流式输出** - 支持流式响应，实时获取生成内容

## 项目结构

``` bash
satrap/
├── __init__.py             # 包入口
├── api/                    # API 相关 (开发中)
├── core/
│   ├── log.py              # 日志系统
│   ├── api_call/
│   │   ├── LLMCall.py      # LLM API 调用封装
│   │   └── EmbedCall.py    # Embed API 调用封装
│   ├── database/           # 数据库模块 (开发中)
│   ├── utils/              # 工具函数 (开发中)
│   │   ├── TCBuilder.py    # tools call 构建
│   │   └── text_utils.py   # 文本处理
└── satrapdata/             # 数据存储目录
    └── logs/               # 日志文件
```

## 兼容性

LLM 模块兼容所有 OpenAI API 格式的服务

## 开发计划

- [ ] 数据库操作封装
- [ ] 更多工具函数
- [ ] 向量数据库支持
- [ ] RAG 功能集成

## 许可证

GPL-v3.0
