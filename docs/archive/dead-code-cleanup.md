# 死代码清理记录

本轮依据历史审计后的三批方案清理内部残留, 基线提交为 `cfd519f`
不以仓库内无调用为由删除公开 SDK 能力, 不改变传统手写 Session 的平台接入方式

## 修改范围

| 批次 | 已处理项 | 保留的行为 |
|---|---|---|
| 内部残留 | pipeline 的 adapter_ids 字段, setter 和调用方; 双版 param_split_len | 预处理器注册和执行, 来源平台路由, 命令参数解析 |
| 内部残留 | 两个 injector 的 invalidate 和 state 存槽 | 闭包持有注入器, 按内容比较更新缓存 |
| 内部残留 | 私有思考解析函数的 full_response 参数 | 从 message 提取各供应商思考字段 |
| 内部残留 | 五个未接入内置工具的 sandbox_* 权限条目 | file_write, file_delete, shell 的计划模式限制, 现有沙箱授权流程 |
| 内部残留 | git 后置不可达分支, npm 重复特判, 集合重复成员 | HIGH 分支中的 git 细分, 环境修改规则表 |
| 导入与前端 | LLM 双版错配迭代器导入, plugin.py 未用工具基类导入, handlers 未用 Any | 公开工具和插件入口 |
| 导入与前端 | docread 的私有警告过滤函数再导出 | 公共 documents 实现, 测试迁往实际定义 |
| 导入与前端 | useMousePosition 文件, truncate, sessionApi.get/updateParams, userApi.getSessions, ApiResponse/LogEntry | 当前页面使用的 API 和格式化工具 |
| 导入与前端 | modelLoading/sessionLoading, backend 独立 error 和 setControlStatus, useTheme.isDark/isLight | health.error, 后端加载状态和主题切换 |
| 替换后去重 | 检查点页面改读 list 返回的 branches, 删除客户端 listBranches | 分支展示, 平台作用域, 后端公开分支接口 |
| 替换后去重 | Video.fromBase64 重复覆盖 | 基类构造视频实例并透传 cover 等参数 |
| 替换后去重 | _protection_reason_full, coding tools/utils.py 转发层 | 调用方直连实际模块, 包级配置兜底和内部回读入口 |
| 替换后去重 | CLI restart 丢弃返回值的配置加载 | cmd_run 实际使用配置的加载路径 |

## 兼容边界

MessageEventResult.result_type 暂缓修改: 原实现允许直接赋值 result_type 而不改变 _stop
若改成派生属性并让 setter 同步 _stop, 会改变现有公开赋值行为; 若不提供 setter, 则丢失赋值能力
因此本轮保留两个字段, 未把它们合并为单一状态

PlatformEvent 回调链, pipeline 预处理器, 管理器公开接口, on_session_switched,
Webhook 占位和公开组件构造入口均未删除

## 验证

- 后端全量: `python -m pytest -q --disable-warnings --tb=short`, 1454 通过, 19 跳过
- 前端: `npm test`, 17 个文件共 66 项通过; `npm run build` 通过, 包含 TypeScript 检查
- 浏览器: `node e2e/checkpoint-branches.mjs` 通过, 验证真实页面分支展示及无额外分支请求
- 命令分类: 对照基线提交运行 1277 组输入, 分类结果完全相同
- 注入回归: 通过真实 session.run 验证记忆注入, 新内容刷新及清空后透传; 目标注入沿用完整回归
- 视频构造: 验证 Base64 内容, 视频类型和 cover 参数
- Python basic 类型检查: satrap 和 tests 共 370 个文件; 63 项诊断与独立基线副本逐项一致, 无新增诊断, 无警告
- 删除符号引用复查, Python 语法和模块头文档检查, git diff --check 均通过

类型检查不等于全库零错误; 基线已有的 63 项诊断不属于此次死代码清理范围
后端跳过项包含需显式开启的集成测试, 缺少 reportlab 的测试及当前 Windows 无符号链接权限的测试
