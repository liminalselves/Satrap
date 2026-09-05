"""
checkpoint CLI 分发兜底测试

覆盖:
- 业务错误 (检查点不存在) 以退出码 1 结束且不抛裸 traceback
- 未知操作以退出码 2 结束
- 成功路径正常执行
"""
from argparse import Namespace
from pathlib import Path
import pytest
from typing import Any

from satrap.cli.cmd_checkpoint import dispatch
from satrap.core.utils.context import ContextManager
from satrap.core.storage import StorageLayout


def _args(data_root: str, **overrides: Any) -> Namespace:
    base: dict[str, Any] = dict(
        action="list", conversation_id="conv-cli", checkpoint_id="",
        branch_name="", checkpoint="", name="", description="",
        platform_id="local", data_root=data_root,
    )
    base.update(overrides)
    return Namespace(**base)


def _seed_conv(db: str) -> str:
    """
    准备带一个检查点的对话, 返回检查点 ID

    参数:
    - db: 数据库实例

    返回:
    - str: 检查点 ID
    """
    ctx = ContextManager("conv-cli", db_path=db, enable_checkpoint=True)
    try:
        ctx.add_user_message("一")
        return ctx.create_checkpoint(name="起点").checkpoint_id
    finally:
        ctx.close()


def test_dispatch_business_error_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """
    业务错误 (检查点不存在) 以退出码 1 结束, 输出友好信息

    参数:
    - tmp_path: tmp路径
    - capsys: pytest 输出捕获夹具
    """
    data_root = str(tmp_path / "data")
    db = str(StorageLayout(data_root).platform_db("local"))
    _seed_conv(db)

    args = _args(data_root, action="rollback", checkpoint_id="not-exist")
    with pytest.raises(SystemExit) as ei:
        dispatch(args)
    assert ei.value.code == 1
    assert "错误:" in capsys.readouterr().out


def test_dispatch_unknown_action_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """
    未知操作以退出码 2 结束

    参数:
    - tmp_path: tmp路径
    - capsys: pytest 输出捕获夹具
    """
    args = _args(str(tmp_path / "data"), action="nope")
    with pytest.raises(SystemExit) as ei:
        dispatch(args)
    assert ei.value.code == 2
    assert "未知操作" in capsys.readouterr().out


def test_dispatch_success_paths(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """
    create / list / lineage 成功路径不抛异常

    参数:
    - tmp_path: tmp路径
    - capsys: pytest 输出捕获夹具
    """
    data_root = str(tmp_path / "data")
    db = str(StorageLayout(data_root).platform_db("local"))
    cp_id = _seed_conv(db)

    dispatch(_args(data_root, action="list"))
    dispatch(_args(data_root, action="create", name="新检查点"))
    dispatch(_args(data_root, action="lineage", checkpoint_id=cp_id))
    capsys.readouterr()   # 吞掉输出
