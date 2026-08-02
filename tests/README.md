# Satrap 测试

测试按运行成本和外部依赖分为三层:

- `unit/`: 不访问外部服务的离线单元测试
- `integration/`: 需要模型、向量服务或其他外部服务的集成测试
- `manual/`: 保留原有演示、基准和手工调试脚本, 不由 pytest 自动收集

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
