import sys

from satrap.cli.client import DaemonClient


def cmd_reload(args):
    """通知后端重载配置"""
    client = DaemonClient()
    if not client.is_alive():
        print("错误: 后端未运行")
        sys.exit(1)
    result = client.reload_config()
    if "error" in result:
        print(f"重载失败: {result['error']}")
        sys.exit(1)
    print("配置已重载")
