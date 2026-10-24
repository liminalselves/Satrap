---
name: plan
description: "计划模式: 只读分析和规划, 禁止修改文件和运行本机命令, 使用 /plan 切换"
---

# 计划模式

## 行为
- 只分析和规划，不写文件、不执行命令，也不改变环境
- 计划写清目标拆解、涉及文件、实施步骤和需要验证的风险
- 用户通过 /plan off 退出计划模式后，再按当前审批模式执行
- 只读工具（read_file/list_dir/glob_files/grep_files/search）仍可正常使用

## 与 /plan 命令搭配
- /plan on：进入计划模式
- /plan off：退出计划模式
