"""
宿主统一执行审批

为插件与 Chat 共用不可变审批契约, 会话策略和审查协调;
权限管理插件只负责配置与用户命令, 不持有执行引擎
"""
from satrap.core.approval.contracts import APPROVAL_MODES, ApprovalMode, ApprovalRequest, ApprovalResult
