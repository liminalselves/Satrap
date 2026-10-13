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

## 后端回归模块

测试已按业务域整合, 覆盖内容对应到 `unit/` 下的模块:

| 模块 | 覆盖内容 |
| --- | --- |
| `test_coding_security_flows.py` | Shell 逐次审批、计划模式、受保护文件与 UTF-8 执行 |
| `test_storage_flows.py` | 归档损坏、文件发布失败、清理失败与幂等重试, 向量源数据事务与跨进程恢复 |
| `test_recovery_flows.py` | 状态存储、运行记录、可恢复引擎与恢复策略 |
| `test_chat_stream_flows.py` | 本机 WebSocket 溢出隔离、快照与询问恢复 |
| `test_plugin_runtime_flows.py` | 插件循环桥接、同步会话取消与线程池背压 |
| `test_minihttp.py` / `test_display_service.py` | 文件分页、参数解码、重试并发与历史批量查询 |

`tests/support/` 存放跨模块复用的隔离夹具与构造器, 不包含测试用例

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
python -B tests/benchmark/benchmark_platform_ingress.py --output tests/benchmark/results/platform/local.json --repeats 3   # 平台入站/自动参与窗口/出站拆分/配置重载/手动唤醒幂等
```

脚本自动清理临时负载, 拒绝覆盖已有输出。保留的 `baseline.json`、`after.json` 和 `after-stability.json` 为同机原始样本, 包含环境、脚本与源文件指纹; 只测热缓存、受控阻塞负载与 Python 分配峰值, 不代表公网延迟或进程 RSS。基准口径与测量结论见 [docs/development/history.md](../docs/development/history.md)。

前端单元测试按域组织在 `satrap-ui/src` 下 (api 客户端, stores 与 `src/scenarios/` 的集中场景模块); 在 `satrap-ui` 目录运行 `npm test`。浏览器回归为 `satrap-ui/e2e/` 下的 14 个场景模块, 总入口 `npm run test:e2e:all` 复用同一个 Chromium 与 Vite 服务跑完全部场景, 需要已安装 Playwright Chromium。用例使用本机临时 Vite 服务与受控 HTTP / WebSocket, 不访问用户服务, 结束后关闭浏览器和服务。
