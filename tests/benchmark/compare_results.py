"""
对比两份插件热路径基准结果, 判定优化收益与语义变化

运行:
    python -B tests/benchmark/compare_results.py tests/benchmark/results/plugin_hotpath/baseline.json tests/benchmark/results/plugin_hotpath/after-batch0.json

规则:
- 中位数变化低于 5% 或两次样本区间重叠时标记为噪声
- 任一场景 result_hash 不一致即视为语义变化, 进程以非零状态退出
- 调用计数, SQL 计数与句柄增量逐项列出差异
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, cast
import argparse
import json
import sys

NOISE_PERCENT = 5.0


def _load(path: Path) -> dict[str, Any]:
    """
    读取结果文件的场景表

    参数:
    - path: 结果 JSON 路径

    返回:
    - 场景名到子场景指标的映射
    """
    body: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return cast("dict[str, dict[str, Any]]", body["scenarios"])


def _verdict(old: dict[str, Any], new: dict[str, Any]) -> str:
    """
    比较耗时中位数并给出结论

    参数:
    - old: 旧耗时汇总
    - new: 新耗时汇总

    返回:
    - 带变化百分比的结论文本
    """
    change = (new["median"] - old["median"]) / old["median"] * 100 if old["median"] else 0.0
    overlap = new["min"] <= old["max"] and old["min"] <= new["max"]
    label = "噪声" if abs(change) < NOISE_PERCENT or overlap else "变快" if change < 0 else "变慢"
    return f"{old['median']:.3f}ms -> {new['median']:.3f}ms ({change:+.1f}%, {label})"


def _diff(name: str, old: Any, new: Any) -> list[str]:
    """
    列出计数类字段的差异

    参数:
    - name: 字段名
    - old: 旧值
    - new: 新值

    返回:
    - 差异描述行
    """
    if isinstance(old, dict) and isinstance(new, dict):
        old_fields = cast("dict[str, Any]", old)
        new_fields = cast("dict[str, Any]", new)
        return [f"    {name}.{key}: {old_fields.get(key)} -> {new_fields.get(key)}"
                for key in sorted(set(old_fields) | set(new_fields)) if old_fields.get(key) != new_fields.get(key)]
    return [f"    {name}: {old} -> {new}"] if old != new else []


def main() -> int:
    """解析参数, 打印对比并返回退出码"""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    args = parser.parse_args()
    old_all, new_all = _load(args.baseline), _load(args.candidate)
    changed: list[str] = []
    for scenario in sorted(set(old_all) | set(new_all)):
        old_group, new_group = old_all.get(scenario, {}), new_all.get(scenario, {})
        for case in sorted(set(old_group) | set(new_group)):
            old, new = old_group.get(case), new_group.get(case)
            print(f"{scenario}/{case}")
            if old is None or new is None:
                print("    仅存在于" + ("候选" if old is None else "基线"))
                continue
            if "elapsed_ms" in old and "elapsed_ms" in new:
                print("    耗时: " + _verdict(old["elapsed_ms"], new["elapsed_ms"]))
            lines: list[str] = []
            for field in ("python_peak_bytes", "handle_delta", "handle_delta_without_gc", "handle_delta_after_gc", "calls", "sql"):
                if field in old or field in new:
                    lines.extend(_diff(field, old.get(field), new.get(field)))
            for line in lines:
                print(line)
            if old.get("result_hash") != new.get("result_hash"):
                changed.append(f"{scenario}/{case}")
                print("    结果哈希不一致: 语义发生变化")
    if changed:
        print("语义变化场景: " + ", ".join(changed))
        return 1
    print("全部场景结果哈希一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
