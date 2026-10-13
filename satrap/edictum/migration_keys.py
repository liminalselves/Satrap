"""插件迁移持久标记的共享常量, 不依赖配置管理器或迁移执行器"""

GLOBAL_RECEIPT = "__capability_migration"
"""好友能力迁移标记, 读取与写入必须使用同一键"""
