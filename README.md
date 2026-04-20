# Satrap

一个简洁的 Python 工具库，提供日志记录和大语言模型 (LLM) API 调用封装。

> ⚠️ **注意**: 这是一个早期开发阶段的项目，API 可能会发生变化。

## 功能特性

- **日志系统** - 彩色控制台输出 + 文件持久化
- **LLM 调用封装** - 同步/异步调用 OpenAI 兼容 API
- **流式输出** - 支持流式响应，实时获取生成内容

## 项目结构

``` bash
Satrap/
├── .gitignore
├── LICENSE
├── README.md
├── requirements.txt
├── satrap/
│   ├── __init__.py
│   ├── api/
│   │   └── __init__.py
│   ├── core/
│   │   ├── __init__.py
│   │   ├── log.py                     # 日志系统
│   │   ├── type.py                    # 类型定义模块
│   │   ├── APICall/
│   │   │   ├── __init__.py
│   │   │   ├── EmbedCall.py           # 嵌入调用封装
│   │   │   ├── LLMCall.py             # LLM 调用封装
│   │   │   └── ReRankCall.py          # 🔥 新增: 重排序接口
│   │   ├── database/                  # 数据库操作封装
│   │   │   └── __init__.py
│   │   ├── framework/                 # 框架模块
│   │   │   ├── __init__.py
│   │   │   ├── BackGroundManager.py   # 🔥 背景任务管理
│   │   │   ├── Base.py                # 工作流 & 会话
│   │   │   ├── SessionManager.py      # 🔥 最近重构
│   │   │   └── command.py             # 命令工具
│   │   └── utils/
│   │       ├── __init__.py
│   │       ├── TCBuilder.py           # 工具管理
│   │       ├── context.py             # 🔥 上下文估算功能
│   │       ├── sandbox.py             # 沙箱工具
│   │       ├── text_utils.py          # 文本工具
│   │       └── tokenizer.py           # 🔥 新增: token 估算
│   └── expend/                        # 🔥 扩展模块
│       ├── __init__.py
│       ├── mem0.py                    # 记忆管理集成
│       ├── rag.py                     # RAG 检索增强
│       ├── sandbox_tools.py           # 沙箱工具扩展
│       └── search.py                  # 网络搜索工具
└── tests/
    ├── agent_test.py
    ├── benchmark_vector_db.py
    ├── chat_demo.py
    ├── coding_agent_demo.py
    ├── control_panel.py
    ├── ctmger_text.py
    ├── embed_test.py
    ├── llm_call_test.py
    ├── log_test.py
    ├── mem0_test.py
    ├── multisession_test.py
    ├── rag_test.py
    ├── rerank_test.py
    ├── sandbox_test.py
    ├── search_test.py
    ├── session_test.py
    ├── tools_test.py
    └── wf_test.py
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
