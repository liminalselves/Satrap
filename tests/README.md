# Satrap 测试

测试按运行成本和外部依赖分类:

- `unit/`: 不访问外部服务的离线单元测试
- `integration/`: 需要模型、向量服务或其他外部服务的集成测试
- `manual/`: 保留原有演示、基准和手工调试脚本, 不由 pytest 自动收集
- `benchmark/`: 可重复运行的性能基准; 原始测量数据位于 `benchmark/results/`, 不由 pytest 自动收集

安装测试依赖:

```powershell
python -m pip install -r requirements-test.txt
```

## 常用命令

```powershell
python -m pytest
python -m pytest tests/unit
python tests/manual/agent_demo.py --help
python -m pytest -m integration --run-integration
python -m pytest --run-integration
```

默认运行不会访问外部服务。需要 API 凭据的测试应通过环境变量或项目本地配置提供凭据, 并显式使用 `--run-integration`

## 后端修复回归

修复测试已按功能并入 `unit/`, 无需单独运行审计目录:

| 模块 | 覆盖内容 |
| --- | --- |
| `test_satrap_coding_security.py` | Shell 逐次审批、计划模式、受保护文件与 UTF-8 执行 |
| `test_storage_restore.py` | 归档损坏、文件发布失败、清理失败与幂等重试 |
| `test_database_recovery.py` | 向量源数据事务、索引故障、跨进程恢复与旧格式迁移 |
| `test_display_stream.py` | 本机 WebSocket 溢出隔离、快照与询问恢复 |
| `test_async_worker.py` | 同步会话取消、线程池背压与 DNS 截止时间 |
| 现有 coding tools / minihttp / display service / display recorder 模块 | 文件分页、参数解码、重试并发与历史批量查询 |

以下 PowerShell 命令强制 UTF-8, 使用自动清理的临时目录运行全部离线单元测试:

```powershell
$testUtf8 = [System.Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = $testUtf8
[Console]::OutputEncoding = $testUtf8
$OutputEncoding = $testUtf8
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONDONTWRITEBYTECODE = '1'
chcp 65001 > $null
@'
import pathlib, subprocess, sys, tempfile
with tempfile.TemporaryDirectory(prefix='satrap-unit-') as folder:
    result = subprocess.run([
        sys.executable, '-B', '-m', 'pytest', 'tests/unit', '-q',
        '-p', 'no:cacheprovider', '-m', 'not requires_api and not integration',
        '--basetemp', str(pathlib.Path(folder) / 'pytest'),
    ])
raise SystemExit(result.returncode)
'@ | python -B -
```

Windows 无创建符号链接权限时会跳过对应测试; 缺少 reportlab 时跳过 PDF 用例。真实 Shell 对照需要 Windows PowerShell, 禁用 pytest 缓存插件会产生既有 `cache_dir` 配置警告。

## 性能与前端重连

沿用上面的 UTF-8 环境, 在仓库根目录执行:

```powershell
python -B tests/benchmark/benchmark_backend.py --output tests/benchmark/results/backend/local.json --repeats 9
```

脚本自动清理临时负载, 拒绝覆盖已有输出。保留的 `baseline.json`、`after.json` 和 `after-stability.json` 为同机原始样本, 包含环境、脚本与源文件指纹; 只测热缓存、受控阻塞负载与 Python 分配峰值, 不代表公网延迟或进程 RSS。验收结论见[后端修复记录](../docs/backend-repair-report.md)。

前端单元测试仍位于 `satrap-ui/src` 的对应模块; 在 `satrap-ui` 目录运行 `npm test`。浏览器回归位于 `satrap-ui/e2e/chat-reconnect.mjs`, 运行 `npm run test:e2e`, 需要已安装 Playwright Chromium。该用例使用本机临时 Vite 服务与受控 HTTP / WebSocket, 不访问用户服务, 结束后关闭浏览器和服务。
