#!/usr/bin/env python3
"""由策略字段契约生成前端契约 JSON

`satrap/core/config/platform_policy.POLICY_FIELD_CONTRACT` 是唯一事实来源,
本脚本把它序列化为 `satrap-ui/src/generated/wake-policy-contract.json`;
只读契约表, 不启动后端服务, 也不依赖控制端在线

用法:
- python scripts/sync_wake_policy_contract.py           写入文件
- python scripts/sync_wake_policy_contract.py --check   只比对, 漂移时退出码 1
"""
from __future__ import annotations

from pathlib import Path
from collections.abc import Mapping
from typing import cast
import argparse
import json
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from satrap.core.config.platform_policy import POLICY_FIELD_CONTRACT, PolicyField  # noqa: E402

TARGET = ROOT / "satrap-ui" / "src" / "generated" / "wake-policy-contract.json"
CONTRACT_VERSION = 1
"""契约 JSON 结构版本; 字段增删或语义变化时递增"""

SERIALIZED_KEYS = (
    "kind", "scope", "hot_reload", "display_in_preview", "off_value",
    "min", "max", "max_exclusive", "integer", "max_length", "max_items",
    "enum", "default", "nullable",
)
"""进入前端契约的字段属性; message 只用于后端错误文案, 不参与序列化"""


def build_payload() -> dict[str, object]:
    """
    按字段名排序构造契约载荷

    返回:
    - dict[str, object]: 含 version 与 fields 的可 JSON 序列化对象
    """
    fields: list[dict[str, object]] = []
    for key in sorted(POLICY_FIELD_CONTRACT):
        field: PolicyField = POLICY_FIELD_CONTRACT[key]
        declared = cast("Mapping[str, object]", field)
        entry: dict[str, object] = {"key": key}
        for name in SERIALIZED_KEYS:
            if name in declared:
                value = declared[name]
                entry[name] = list(value) if isinstance(value, tuple) else value
        fields.append(entry)
    return {"version": CONTRACT_VERSION, "fields": fields}


def render(payload: dict[str, object]) -> bytes:
    """
    序列化为固定格式的字节: UTF-8, LF, 键排序, 两空格缩进, 末尾换行

    参数:
    - payload: 契约载荷

    返回:
    - bytes: 目标文件内容
    """
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    return (text + "\n").encode("utf-8")


def main(argv: list[str] | None = None) -> int:
    """
    生成或校验前端契约 JSON

    参数:
    - argv: 命令行参数, 缺省读取 sys.argv

    返回:
    - int: 0 表示一致或已写入; --check 模式下漂移返回 1
    """
    parser = argparse.ArgumentParser(description="同步策略字段契约 JSON")
    parser.add_argument("--check", action="store_true", help="只比对库内文件, 不写入")
    args = parser.parse_args(argv)
    expected = render(build_payload())
    if args.check:
        actual = TARGET.read_bytes() if TARGET.is_file() else b""
        if actual == expected:
            print(f"契约一致: {TARGET.relative_to(ROOT)}")
            return 0
        print(f"契约漂移: 请运行 python {Path(__file__).name} 重新生成", file=sys.stderr)
        return 1
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_bytes(expected)
    print(f"已写入 {TARGET.relative_to(ROOT)} ({len(expected)} 字节)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
