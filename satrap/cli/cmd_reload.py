"""CLI 后端配置热重载命令"""
import argparse

from satrap.cli.common import daemon_client_from_args
from satrap.cli.output import ok


def cmd_reload(args: argparse.Namespace):
    """
    通知后端重载配置

    参数:
    - args: 额外位置参数
    """
    client = daemon_client_from_args(args)
    client.require_alive()
    client.reload_config()
    ok("配置已重载")
