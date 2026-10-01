"""便携发行包启动器; 管理本次启动的服务与退出清理"""
from __future__ import annotations

import argparse
import ctypes
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import secrets
import shlex
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / ".satrap"
STATE = DATA / "release-instance.json"
STOP = DATA / "release-stop"
CONTROL_URL = "http://127.0.0.1:19871"
CHAT_URL = "http://127.0.0.1:19872"
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
SERVICE_MODULES = {"satrap.core.backend.control_server", "satrap.display.server", "satrap.main", "satrap"}


def configure_environment() -> None:
    """固定工作目录与 UTF-8, 子进程优先使用便携运行时"""
    os.chdir(ROOT)
    os.environ.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    os.environ["PATH"] = os.pathsep.join([
        str(Path(sys.executable).parent),
        str(Path(sys.executable).parent / "Scripts"),
        os.environ.get("PATH", ""),
    ])
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    DATA.mkdir(parents=True, exist_ok=True)


def request(url: str, method: str = "GET", timeout: float = 3) -> dict:
    """携带当前实例令牌访问本地管理接口"""
    token = os.environ.get("SATRAP_API_TOKEN", "").strip()
    token_path = DATA / "api-token"
    if not token and token_path.is_file():
        token = token_path.read_text(encoding="utf-8").strip()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    req = urllib.request.Request(url, headers=headers, method=method)
    with HTTP.open(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_health(url: str, process: subprocess.Popen | None = None, timeout: float = 60) -> dict:
    """等待 HTTP 服务就绪, 子进程提前退出时立即报错"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise RuntimeError(f"服务提前退出, 退出码 {process.returncode}; 请查看 .satrap/release-logs")
        try:
            return request(url)
        except (OSError, ValueError):
            time.sleep(0.2)
    raise RuntimeError(f"服务启动超时: {url}; 请查看 .satrap/release-logs")


class InstanceLock:
    """通过操作系统锁保证同一发行目录只有一个启动器"""

    def acquire(self) -> bool:
        self.file = (DATA / "release.lock").open("a+b")
        self.file.seek(0)
        if self.file.seek(0, 2) == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            self.file.close()
            return False

    def release(self) -> None:
        self.file.close()


class WindowsProcessJob:
    """关闭启动窗口或启动器异常退出时回收其全部子进程"""

    def __init__(self) -> None:
        self.handle = None
        if os.name != "nt":
            return
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                ("flags", wintypes.DWORD), ("minimum_working_set", ctypes.c_size_t),
                ("maximum_working_set", ctypes.c_size_t), ("active_processes", wintypes.DWORD),
                ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                ("scheduling_class", wintypes.DWORD),
            ]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("basic", BasicLimits), ("io_counters", ctypes.c_uint64 * 6),
                ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t),
            ]

        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.api.CreateJobObjectW.restype = wintypes.HANDLE
        self.api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.api.SetInformationJobObject.restype = wintypes.BOOL
        self.api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.api.OpenProcess.restype = wintypes.HANDLE
        self.api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.api.AssignProcessToJobObject.restype = wintypes.BOOL
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.api.CloseHandle.restype = wintypes.BOOL
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000   # 作业句柄关闭时终止所有所属进程
        if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def assign(self, process: subprocess.Popen) -> None:
        """绑定本次创建的进程, 后续子进程自动继承作业"""
        if self.handle is None:
            return
        handle = self.api.OpenProcess(0x0101, False, process.pid)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not self.api.AssignProcessToJobObject(self.handle, handle):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            self.api.CloseHandle(handle)

    def close(self) -> None:
        """释放作业句柄, 终止任何残留的所属子进程"""
        if self.handle is not None:
            self.api.CloseHandle(self.handle)
            self.handle = None


def write_state(state: dict) -> None:
    """原子替换状态文件, 避免重复启动读取到半写入内容"""
    temporary = STATE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state), encoding="utf-8")
    temporary.replace(STATE)


def busy_ports(ports: list[int]) -> list[int]:
    """使用绑定检查端口是否可以重新监听, 包含尚未释放的端口"""
    busy = []
    for port in ports:
        with socket.socket() as listener:
            if os.name == "nt":
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                listener.bind(("127.0.0.1", port))
            except OSError:
                busy.append(port)
    return busy


def windows_snapshot(namespace: str) -> list[dict]:
    """通过系统 PowerShell 获取快照, 强制输入输出均为 UTF-8"""
    if os.name != "nt":
        return []
    queries = {
        "root/cimv2": "@(Get-CimInstance Win32_Process | ForEach-Object { [PSCustomObject]@{ ProcessId=$_.ProcessId; Name=$_.Name; ExecutablePath=$_.ExecutablePath; CommandLine=$_.CommandLine; CreationDate=$(if ($null -ne $_.CreationDate) { $_.CreationDate.ToUniversalTime().ToString('yyyyMMddHHmmss.ffffff') + '+000' } else { '' }) } })",
        "root/StandardCimv2": "@(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Select-Object LocalPort,OwningProcess)",
    }
    prefix = "$ErrorActionPreference='Stop'; [Console]::InputEncoding=[Text.UTF8Encoding]::new($false); [Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); $OutputEncoding=[Text.UTF8Encoding]::new($false); "
    executable = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    command = prefix + "ConvertTo-Json -Compress -Depth 3 -InputObject " + queries[namespace]
    result = subprocess.run(
        [str(executable), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True, text=True, encoding="utf-8", timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode:
        raise RuntimeError("无法查询进程或端口信息: " + result.stderr.strip())
    return json.loads(result.stdout.strip() or "[]")


def process_snapshot() -> list[dict]:
    """获取用于服务识别和端口诊断的进程信息"""
    return windows_snapshot("root/cimv2")


def listener_snapshot() -> list[dict]:
    """查询 TCP 监听端口及其拥有者 PID"""
    return windows_snapshot("root/StandardCimv2")


def service_module(process: dict) -> str | None:
    """同时校验本目录运行时路径与精确模块名, 排除其他实例和普通 CLI"""
    executable = process.get("ExecutablePath")
    if not executable or os.path.normcase(str(Path(executable).resolve())) != os.path.normcase(str(runtime_executable())):
        return None
    try:
        args = [arg.strip('"') for arg in shlex.split(process.get("CommandLine") or "", posix=False)]
        index = args.index("-m")
        module = args[index + 1]
    except (ValueError, IndexError):
        return None
    if module not in SERVICE_MODULES or (module in {"satrap.main", "satrap"} and "run" not in args[index + 2:]):
        return None
    return module


def runtime_executable() -> Path:
    """返回本目录唯一允许清理的 Python 运行时路径"""
    return (ROOT / "runtime/python.exe").resolve()


def port_description(ports: list[int]) -> str:
    """报告端口占用者名称和 PID, 不显示可能含密钥的命令行"""
    processes = {int(process["ProcessId"]): process for process in process_snapshot()}
    listeners = listener_snapshot()
    descriptions = []
    for port in ports:
        owners = {int(item["OwningProcess"]) for item in listeners if int(item["LocalPort"]) == port}
        detail = ", ".join(f"{processes.get(pid, {}).get('Name', '未知进程')}, PID {pid}" for pid in sorted(owners))
        descriptions.append(f"端口 {port}: {detail or '未查询到监听者, 端口可能被系统保留'}")
    return "; ".join(descriptions)


def check_ports(ports: list[int], timeout: float = 0) -> None:
    """等待端口释放, 超时后报告仍占用端口的进程"""
    deadline = time.monotonic() + timeout
    while True:
        busy = busy_ports(ports)
        if not busy:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError("端口已被占用或停止后仍未释放: " + port_description(busy))
        time.sleep(0.2)


def terminate_service(process: dict) -> None:
    """持有进程句柄并复核路径与创建时间, 防止 PID 复用导致误杀"""
    if not service_module(process):
        raise RuntimeError("拒绝终止身份无法验证的进程")
    from ctypes import wintypes
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenProcess.restype = wintypes.HANDLE
    api.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    api.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    api.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    pid = int(process["ProcessId"])
    handle = api.OpenProcess(0x100001 | 0x1000, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:
            return   # 快照中的进程已经退出
        raise ctypes.WinError(error)
    try:
        exit_code = wintypes.DWORD()
        if not api.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            raise ctypes.WinError(ctypes.get_last_error())
        if exit_code.value != 259:
            return   # 健康接口已经让该服务退出
        image = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(image))
        times = [wintypes.FILETIME() for _ in range(4)]
        if not api.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(size)) or not api.GetProcessTimes(handle, *(ctypes.byref(item) for item in times)):
            raise ctypes.WinError(ctypes.get_last_error())
        created = str(process.get("CreationDate") or "")
        timestamp = datetime.strptime(created[:21], "%Y%m%d%H%M%S.%f").replace(tzinfo=timezone(timedelta(minutes=int(created[21:25]))))
        duration = timestamp.astimezone(timezone.utc) - datetime(1601, 1, 1, tzinfo=timezone.utc)
        expected = (duration.days * 86400 + duration.seconds) * 1000000 + duration.microseconds
        actual = ((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime) // 10
        if os.path.normcase(image.value) != os.path.normcase(str(runtime_executable())) or actual != expected:
            raise RuntimeError(f"进程 PID {pid} 身份已变化, 已取消清理")
        if not api.TerminateProcess(handle, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        if api.WaitForSingleObject(handle, 5000) != 0:
            raise RuntimeError(f"进程 PID {pid} 清理超时")
        print(f"已清理本目录残留服务: {service_module(process)}, PID {pid}", flush=True)
    finally:
        api.CloseHandle(handle)


def recover_services(ports: list[int], control_port: int) -> None:
    """清理本目录遗留服务, 复查端口, 其他目录的实例保留并报错"""
    services = [process for process in process_snapshot() if service_module(process)]
    if services:
        listeners = listener_snapshot()
        control_pids = {int(process["ProcessId"]) for process in services if service_module(process) == "satrap.core.backend.control_server"}
        if any(int(item["LocalPort"]) == control_port and int(item["OwningProcess"]) in control_pids for item in listeners):
            try:
                request(f"http://127.0.0.1:{control_port}/stop", "POST", timeout=15)
            except (OSError, ValueError):
                pass
        for process in sorted(services, key=lambda item: service_module(item) == "satrap.core.backend.control_server"):
            terminate_service(process)
    check_ports(ports, timeout=5 if services else 0)
    for filename in ("control_server.pid", "backend.pid"):
        (DATA / filename).unlink(missing_ok=True)


def stop_ports(state: dict, control_port: int, chat_port: int) -> list[int]:
    """优先使用启动器记录的实际端口, 没有记录时读取本目录配置"""
    recorded = state.get("ports")
    if isinstance(recorded, list) and len(recorded) == 3 and all(isinstance(port, int) and 1 <= port <= 65535 for port in recorded):
        return recorded
    from satrap.core.config.loader import ConfigLoader
    backend_port = 19870
    if any(path.is_file() for path in ConfigLoader.candidate_paths(ROOT)):
        backend_port = ConfigLoader.autodetect().api_port
    return [control_port, chat_port, backend_port]


def open_browser(no_browser: bool) -> None:
    """打开带一次性引导凭据的本地管理面板"""
    if no_browser:
        return
    token = os.environ.get("SATRAP_API_TOKEN", "").strip()
    if not token:
        token = (DATA / "api-token").read_text(encoding="utf-8").strip()
    webbrowser.open(f"{CONTROL_URL}/#token={token}")


def start(no_browser: bool, control_port: int = 19871, chat_port: int = 19872) -> None:
    """启动并监督控制服务, 聊天服务和平台后端"""
    global CONTROL_URL, CHAT_URL
    CONTROL_URL = f"http://127.0.0.1:{control_port}"
    CHAT_URL = f"http://127.0.0.1:{chat_port}"
    os.environ["SATRAP_CHAT_PORT"] = str(chat_port)
    os.environ["SATRAP_CONTROL_PORT"] = str(control_port)
    lock = InstanceLock()
    if not lock.acquire():
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if STATE.is_file():
                state = json.loads(STATE.read_text(encoding="utf-8"))
                if state.get("ready"):
                    CONTROL_URL = state["control_url"]
                    wait_health(CONTROL_URL + "/status", timeout=5)
                    open_browser(no_browser)
                    print(f"Satrap 已在运行: {CONTROL_URL}")
                    return
            time.sleep(0.2)
        raise RuntimeError("已有启动器正在运行, 但服务尚未就绪; 请查看 .satrap/release-logs")

    processes: list[subprocess.Popen] = []
    logs = []
    instance = secrets.token_hex(16)
    cleanup_done = False
    callback = None
    job = None
    ports: list[int] = []
    ports_verified = False

    def cleanup() -> None:
        """先请求后端保存数据, 再回收本次创建的进程"""
        nonlocal cleanup_done
        if cleanup_done:
            return
        cleanup_done = True
        if processes and processes[0].poll() is None:
            try:
                request(CONTROL_URL + "/stop", "POST", timeout=15)
            except (OSError, ValueError):
                pass
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        for log in logs:
            log.close()

    try:
        STOP.unlink(missing_ok=True)
        write_state({"instance": instance, "ready": False})
        from satrap.core.config.loader import ConfigLoader
        config = ConfigLoader.autodetect()
        if config.api_host != "127.0.0.1":
            raise RuntimeError("便携启动器要求 api.host 为 127.0.0.1; 自定义部署请使用 satrap.bat run")
        ports = [control_port, chat_port, config.api_port]
        if len(set(ports)) != 3 or any(port < 1 or port > 65535 for port in ports):
            raise RuntimeError("控制服务, 聊天服务与平台后端必须使用三个不同的有效端口")
        recover_services(ports, control_port)
        ports_verified = True
        write_state({"instance": instance, "ready": False, "ports": ports})
        os.environ.setdefault("SATRAP_ALLOWED_ORIGINS", ",".join(f"http://127.0.0.1:{port}" for port in ports))
        job = WindowsProcessJob()
        if os.name == "nt":
            handler_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)

            def on_console_event(event: int) -> bool:
                if event in (2, 5, 6):
                    cleanup()
                return False

            callback = handler_type(on_console_event)
            ctypes.windll.kernel32.SetConsoleCtrlHandler(callback, True)

        log_dir = DATA / "release-logs"
        log_dir.mkdir(exist_ok=True)
        for name, module, port, health_url in (
            ("control", "satrap.core.backend.control_server", control_port, CONTROL_URL + "/status"),
            ("chat", "satrap.display.server", chat_port, CHAT_URL + "/api/chat/health"),
        ):
            print(f"正在启动 {name} 服务...", flush=True)
            log = (log_dir / f"{name}.log").open("ab")
            logs.append(log)
            process = subprocess.Popen(
                [sys.executable, "-X", "utf8", "-u", "-m", module, "--port", str(port)],
                cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            processes.append(process)
            job.assign(process)
            wait_health(health_url, process)
        print("正在启动平台后端...", flush=True)
        request(CONTROL_URL + "/start", "POST", timeout=30)
        health = wait_health(f"http://127.0.0.1:{config.api_port}/api/health")
        if not health.get("running"):
            raise RuntimeError("平台后端未能就绪; 请查看 .satrap/logs")
        write_state({"instance": instance, "ready": True, "control_url": CONTROL_URL, "ports": ports})
        print(f"Satrap 已启动: {CONTROL_URL}\n按 Ctrl+C 或运行 stop.bat 停止全部服务", flush=True)
        open_browser(no_browser)
        while True:
            if STOP.is_file() and STOP.read_text(encoding="utf-8") == instance:
                break
            for process in processes:
                if process.poll() is not None:
                    raise RuntimeError("服务意外退出; 请查看 .satrap/release-logs")
            time.sleep(0.3)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            cleanup()
        finally:
            if job is not None:
                job.close()
            if callback is not None:
                ctypes.windll.kernel32.SetConsoleCtrlHandler(callback, False)
            try:
                if ports_verified:
                    check_ports(ports, timeout=5)
                STATE.unlink(missing_ok=True)
                STOP.unlink(missing_ok=True)
            finally:
                lock.release()


def stop(control_port: int = 19871, chat_port: int = 19872) -> None:
    """让持有目录锁的启动器停止服务, 不依据 PID 终止未知进程"""
    lock = InstanceLock()
    if lock.acquire():
        try:
            state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.is_file() else {}
            ports = stop_ports(state, control_port, chat_port)
            recover_services(ports, ports[0])
            STATE.unlink(missing_ok=True)
            STOP.unlink(missing_ok=True)
            print("Satrap 已停止, 端口残留检查通过")
        finally:
            lock.release()
        return
    if not STATE.is_file():
        raise RuntimeError("启动器尚未写入实例信息, 请稍后重试")
    state = json.loads(STATE.read_text(encoding="utf-8"))
    ports = stop_ports(state, control_port, chat_port)
    STOP.write_text(state["instance"], encoding="utf-8")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if lock.acquire():
            try:
                recover_services(ports, ports[0])
                print("Satrap 已停止, 端口残留检查通过")
            finally:
                lock.release()
            return
        time.sleep(0.2)
    raise RuntimeError("停止超时, 请查看启动器窗口和日志")


def main() -> int:
    """解析发行包启动或停止命令"""
    parser = argparse.ArgumentParser(description="Satrap 便携发行包启动器")
    parser.add_argument("action", choices=["start", "stop"])
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--control-port", type=int, default=19871)
    parser.add_argument("--chat-port", type=int, default=19872)
    args = parser.parse_args()
    configure_environment()
    try:
        if args.action == "start":
            start(args.no_browser, args.control_port, args.chat_port)
        else:
            stop(args.control_port, args.chat_port)
        return 0
    except Exception as error:
        print(f"启动器错误: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
