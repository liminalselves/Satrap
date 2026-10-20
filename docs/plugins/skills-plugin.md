# 通用技能插件

`skills` 插件统一管理外部 Markdown 技能. 在插件管理中打开 `skills → 技能库`, 可以上传 `.md` / `.markdown`, 粘贴正文, 或从 HTTPS Markdown 地址导入. GitHub `blob` 文件链接会转换为原始文件链接

导入后还需在 Chat 或对应平台 Agent 的使用位置启用 `skills` 插件. 插件的 `allowed_skills` 每行填写一个允许的外部技能名称, 留空允许全部已启用技能. 单个技能还能限制 Chat, 平台会话和嵌入会话范围

## Markdown 格式

```markdown
---
name: weekly-report
description: 用户需要将零散工作记录整理成周报时使用
---

# 周报整理

按已完成, 问题和下周计划整理记录, 不编造结果
```

名称和用途简介可以在导入页面补充. 名称限制为 1 至 64 位英文字母, 数字, 点, 下划线和连字符; 简介限制为 1 至 512 字. 正文最大 256 KiB, 最多保存 200 个外部技能. 外部技能以 `user.<名称>` 登记, 避免与内置插件技能混淆

数据存储于项目 `.satrap/skills/managed.json`, 保留 Markdown 原文, 来源, 开关, 适用范围和递增版本. 写入使用跨进程锁及原子替换. 同名导入不会覆盖, 编辑和删除需要当前版本; 保存冲突时前端保留草稿

## 模型加载

安装, 启用和本次任务加载是不同状态:

| 内容 | 模型请求中的位置 | 是否持久改写系统提示词 |
| --- | --- | --- |
| 当前有效技能的名称, 简介和加载规则 | 请求副本的 system 消息 | 否 |
| Agent 调用 `load_skill` 返回的正文 | 对应 tool 消息 | 否 |
| `/skill use` 强制使用的正文 | 本轮用户任务前的资料块 | 否 |

目录与加载器使用同一套有效性检查. 停用技能, 停用所属插件或能力被替代后, 猜测名字也不能加载. 插件无需导出技能给其它插件, 即可向模型提供自己的技能; 插件间只读查询仍使用独立的 `plugin_host.plugins.skills` 依赖规则

模型先根据简介选择技能, 再发出 `load_skill` tool call. 同一任务重复加载会提示沿用前面的说明. 工具循环中的当前轮和本轮资料按固定内容参与上下文预算, 不因历史压缩悄悄丢失. 中断恢复复用已完成加载步骤和已保存请求中的正文, 不以新文件替换旧证据

任务结束后不继续自动激活正文. 加载记录可以作为普通历史保留, 但新请求的系统目录不包含已加载正文

## 命令

```text
/skill list
/skill show user.weekly-report
/skill use user.weekly-report 把这些工作记录整理成周报
/skill use user.weekly-report
/skill import https://example.com/SKILL.md
```

`list` 查询当前会话可用技能, `show` 只查看说明. `use` 带任务时先固定技能资料再执行任务, 不伪造 tool 消息; 不带任务时登记为下一次本人任务使用. 群聊待办绑定真实平台账号, 对话及发送者, 其它成员不会消费该待办

`import` 是系统管理员子命令, 普通用户仍可使用已授权技能. 前端管理接口走已有的后端或控制服务认证. HTTPS 下载复用出站地址绑定, 禁止访问非公网地址, 并限制重定向次数, 响应大小和超时; 不接受网页 HTML 或仓库目录

外部 Markdown 只提供说明, 不安装软件, 不执行 `tools.py`, 不连接 MCP, 不启用关闭的工具, 不改变审批模式. 内置插件可信代码提供的工具和 MCP 仍在插件资源生命周期中管理, 正文加载不会扩权

## 插件开发

原有 `skills/<名称>/skill.md` 和 `skills.py` 的静态 `skills` 列表继续可用, 同时支持标准 `SKILL.md`. 动态来源可声明同步工厂:

```python
def get_skills(session, config, *, plugin_host=None):
    return plugin_host.skills.provide(read_plain_skills)
```

`read_plain_skills` 返回该插件管理的 `list[Skill]`, 不带工具或 MCP 实例. 宿主在任务开始时刷新该来源, 独立技能开关和所属插件身份继续由框架管理. 卸载时撤销来源, 不直接修改其它插件的私有实现

## 一次性迁移

现有用户目录不会被框架默认扫描后自动激活. 旧 Markdown 可以通过插件接管, 命令默认只预览; 缺少简介的文件需在导入界面补充:

```powershell
python scripts/import_legacy_skills.py
python scripts/import_legacy_skills.py --apply
```

清理旧历史中的常驻技能块使用原有离线清洗器的 `--skills` 模式. 默认只预览, 不输出正文:

```powershell
python scripts/clean_request_context.py --skills --database <platform.db>
```

仅移除能与已登记 Markdown 完整核对的系统消息尾部技能块. 普通用户粘贴的技能原文保留; 无法核对的系统块报告为待处理. 同一例程覆盖历史, 物化检查点, 上下文备份, 展示版本上下文及回收归档, 并失效相关摘要

实际清洗前必须停止目标数据库的后端写入. 脚本检查可识别进程和受影响的未完成任务, 应用还需要 `--apply --offline-confirmed --backup-dir <外部备份目录>`. 应用前备份数据库, 复核预览行, 不修改运行中任务的指纹强行恢复. 清洗不进入模型请求路径或日常启动流程
