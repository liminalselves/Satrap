"""统一调度轻量 JS 子进程, 保持同步异步的超时与真实进程回收语义"""
from __future__ import annotations

from importlib.metadata import version, PackageNotFoundError
from packaging.specifiers import SpecifierSet
from packaging.version import Version
from pathlib import Path
import subprocess
import threading
import traceback
import asyncio
import time
import sys
import os

from satrap.core.utils.async_worker import BoundedAsyncWorker, WorkerBusyError
from satrap.core.utils.proc_env import sanitized_child_env
from .challenge import ScriptChallenge, validate_cookie, MAX_REPLY_BYTES
from .utils import Deadline, WebRequestError

from satrap.core.log import logger


_SLOTS = threading.BoundedSemaphore(1)
_ASYNC = BoundedAsyncWorker("satrap-web-js", workers=1, capacity=1)


def _command() -> list[str]:
    """
    固定当前解释器和隔离启动选项, 不导入 V8 到宿主

    返回:
    - 可信 worker 的参数列表; 未安装合适版本时明确报错
    """
    try:
        installed = Version(version("mini-racer"))
        if installed not in SpecifierSet(">=0.14.1,<0.15"):
            raise PackageNotFoundError("mini-racer")
    except PackageNotFoundError as error:
        raise WebRequestError("js_runtime_unavailable", "请为当前 Python 安装 Satrap 的 web-js 可选依赖", status=200) from error
    return [sys.executable, "-I", "-B", "-u", "-X", "utf8", str(Path(__file__).with_name("js_worker.py"))]


def run(challenge: ScriptChallenge, deadline: Deadline, cancelled: threading.Event | None = None) -> tuple[str, str]:
    """
    运行一个轻量验证任务, 返回前回收实际进程

    参数:
    - challenge: 已识别的有界验证模板
    - deadline: HTTP 与脚本共享的总时限
    - cancelled: 异步入口的取消通知, 默认 None

    返回:
    - 经父进程校验的 Cookie 名和值; 超时和取消先终止进程再抛错
    """
    command = _command()
    payload = challenge.payload()
    if not _SLOTS.acquire(blocking=False):
        raise WebRequestError("busy", "轻量验证执行器繁忙", status=200)
    process: subprocess.Popen[bytes] | None = None
    sender: threading.Thread | None = None
    try:
        budget = min(5.0, deadline.remaining())
        end = time.monotonic() + budget
        env = {key: value for key, value in sanitized_child_env().items()
               if not key.upper().startswith(("PYTHON", "DYLD_", "LD_"))}
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   env=env, close_fds=True,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        stdin = process.stdin
        assert stdin is not None
        process.stdin = None

        def send() -> None:
            """独立发送并关闭输入, 防止 Windows 管道写入阻塞外层超时和取消检查"""
            try:
                with stdin:
                    stdin.write(payload)
            except OSError:
                logger.debug("[网页脚本] 请求管道已关闭, 由进程状态和结果协议判定")

        sender = threading.Thread(target=send, name="satrap-web-js-input", daemon=True)
        sender.start()
        while True:
            if cancelled is not None and cancelled.is_set():
                raise WebRequestError("js_execution_timeout", "轻量验证已取消", status=200)
            remaining = end - time.monotonic()
            if remaining <= 0:
                raise WebRequestError("js_execution_timeout", "轻量验证进程超过本次时间预算", status=200)
            try:
                output, diagnostics = process.communicate(timeout=min(0.05, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
        if process.returncode or len(output) > MAX_REPLY_BYTES:
            logger.warning(f"[网页脚本] worker 失败, code={process.returncode}")
            raise WebRequestError("js_execution_failed", "轻量验证 worker 异常退出或结果超限", status=200)
        if diagnostics:
            logger.error(f"[网页脚本] worker 诊断: {diagnostics[-2048:].decode('utf-8', errors='replace')}")
        deadline.remaining()
        return validate_cookie(output, challenge)
    except OSError as error:
        logger.error(f"[网页脚本] 无法启动或通信 worker\n{traceback.format_exc()}")
        raise WebRequestError("js_execution_failed", "无法运行轻量验证子进程", status=200) from error
    finally:
        try:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.communicate()
            if sender is not None:
                sender.join()
        finally:
            _SLOTS.release()


async def run_async(challenge: ScriptChallenge, deadline: Deadline) -> tuple[str, str]:
    """
    使用已有有界线程调度器, 取消后通知并等待子进程完成清理

    参数:
    - challenge: 已识别的有界验证模板
    - deadline: 当前工具的总时限

    返回:
    - 校验后的 Cookie; 取消会在回收进程后重新抛出
    """
    cancelled = threading.Event()
    task = asyncio.create_task(_ASYNC.run(run, challenge, deadline, cancelled))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        cancelled.set()
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()
        raise
    except WorkerBusyError as error:
        raise WebRequestError("busy", "轻量验证执行器繁忙", status=200) from error
