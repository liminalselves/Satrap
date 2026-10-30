# Actions 测试范围

PR 检查以目标分支和当前检出版本的共同祖先为基线, 不以单个提交作为范围, 避免后续提交漏掉同一 PR 先前新增的测试

## 默认检查

- 新增测试文件完整运行; 既有文件按 AST 选择新增或改动的测试函数, 参数化正常展开
- 测试文件的公共导入, 夹具或辅助定义变化时, 回归整个文件
- 生产代码按 `.github/scripts/select_tests.py` 中的业务映射补充必要回归; 公共测试辅助变化时回归直接消费者
- `tests/conftest.py` 变化时, Linux 业务任务运行完整单元测试
- 业务回归只在 Linux / Python 3.13 运行一次, 使用两个 pytest worker
- 后端变化时, 其余八组系统与 Python 版本只执行配置构造, 独立底层组件, 沙箱 worker 和原生 shell 的关键兼容流程; 保留 Windows 与 macOS 的 Python 3.11 / 3.12 组合, 避免遗漏曾出现过的问题
- 仅前端变更时跳过 Python 任务; 前端保持快速单元测试与生产构建, 浏览器只运行对应领域核心回归及新增登记场景, 不因修改夹具就扩展到全部旧场景
- 公共前端组件, API, 样式, 依赖或工作流变化时, 补跑既有核心浏览器流程
- 普通开发文档不触发测试; 用户文档变更运行文档浏览器验收

业务映射用于限定回归范围, 不等同于完整依赖分析. 新增业务目录或间接依赖变化时应同步补充映射. 未匹配到具体领域的后端代码仍执行模型配置与工作流核心回归

计划写入 Actions Summary 和 `ci-test-plan` artifact, 可以直接查看实际选中的测试文件, 节点, 浏览器场景及兼容矩阵

## 手动全量

在 Actions 的 `Run workflow` 中勾选 `full_suite`, 恢复三个系统和三个 Python 版本的完整单元测试, 并执行所有已登记浏览器场景. Linux / Python 3.13 由业务任务负责, 不在兼容矩阵中重复运行

发布打包与迁移后 smoke 检查保持独立, 不受 PR 测试选择影响

## 本地查看计划

```text
python -m unittest discover -s .github/scripts -p "test_*.py" -v
python .github/scripts/select_tests.py --base origin/main --output tests/.pytest_cache/ci-test-plan.json
```

计划读取已提交的 HEAD 与基线差异, 不将工作区临时改动混入 PR 范围
