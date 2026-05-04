import sys

from satrap.cli.client import DaemonClient
from satrap.core.platform import PlatformAdapterManager, registry as global_registry


def cmd_platform_list(args):
    """列出平台适配器"""
    client = DaemonClient()
    if client.is_alive():
        health = client.health()
        adapters = health.get("adapters", {})
        print("已注册适配器类型:")
        for t in global_registry.list_types():
            print(f"  {t}")
        print()
        if adapters:
            header = ["ID", "状态", "已启动"]
            rows = []
            for aid, info in adapters.items():
                rows.append([aid, info.get("status", "?"), str(info.get("started", False))])
            print(_fmt_table(rows, header))
        else:
            print("当前无运行中的适配器实例")
    else:
        mgr = PlatformAdapterManager(registry=global_registry)
        types = global_registry.list_types()
        print("已注册适配器类型:")
        for t in types:
            print(f"  {t}")
        print()
        instances = mgr.list_adapters()
        if instances:
            header = ["ID", "类型", "状态"]
            rows = [[a, "?", "未运行"] for a in instances]
            print(_fmt_table(rows, header))
        else:
            print("当前无运行中的适配器实例")


def _fmt_table(rows: list[list[str]], header: list[str] | None = None) -> str:
    if not rows:
        return "(空)"
    col_widths = []
    all_rows = ([header] if header else []) + rows
    for col_idx in range(len(all_rows[0])):
        col_widths.append(max(len(str(r[col_idx])) for r in all_rows))
    lines = []
    if header:
        hdr = " | ".join(str(h).ljust(w) for h, w in zip(header, col_widths))
        lines.append(hdr)
        lines.append("-+-".join("-" * w for w in col_widths))
    for row in rows:
        lines.append(" | ".join(str(c).ljust(w) for c, w in zip(row, col_widths)))
    return "\n".join(lines)


def dispatch(args):
    if args.action == "list":
        cmd_platform_list(args)
    else:
        print(f"未知操作: {args.action}")
        sys.exit(1)
