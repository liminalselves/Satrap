"""
后端控制服务 - 独立运行, 用于启动/停止/监控后端

这是一个简单的 HTTP 服务, 独立于主后端运行,
提供启动, 停止, 重启后端的功能, 以及配置文件的读写
默认监听 127.0.0.1:19871

特性:
- 单实例锁防止重复启动
- PID 文件管理
- 前端关闭时自动停止后端
"""
from __future__ import annotations

import argparse
import asyncio
import atexit
import ctypes
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any
import urllib.error
import urllib.request

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
# 项目根目录

DATA_DIR = PROJECT_ROOT / ".satrap"
# 数据目录
DATA_DIR.mkdir(parents=True, exist_ok=True)

CONTROL_PID_FILE = DATA_DIR / "control_server.pid"
# PID 文件路径
BACKEND_PID_FILE = DATA_DIR / "backend.pid"

_backend_process: subprocess.Popen | None = None
# 后端进程

CONFIG_PATH = PROJECT_ROOT / "config.yaml"
# 配置文件路径

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, PUT, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}
# CORS 头


def _write_pid_file(pid_file: Path, pid: int) -> None:
    """
    写入 PID 文件

    参数:
    - pid_file: pid文件
    - pid: 进程 ID
    """
    try:
        pid_file.write_text(str(pid), encoding="utf-8")
    except Exception:
        pass


def _read_pid_file(pid_file: Path) -> int | None:
    """
    读取 PID 文件

    参数:
    - pid_file: pid文件

    返回:
    - int | None: 读取 PID 文件
    """
    try:
        if pid_file.exists():
            return int(pid_file.read_text(encoding="utf-8").strip())
    except Exception:
        pass
    return None


def _remove_pid_file(pid_file: Path) -> None:
    """
    删除 PID 文件

    参数:
    - pid_file: pid文件
    """
    try:
        if pid_file.exists():
            pid_file.unlink()
    except Exception:
        pass


def _is_process_running(pid: int) -> bool:
    """
    检查进程是否在运行

    参数:
    - pid: 进程 ID

    返回:
    - bool: 检查结果
    """
    if sys.platform == "win32":
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)   # Windows 进程查询权限标志
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def _check_single_instance() -> bool:
    """
    检查是否已有控制服务实例在运行

    返回 True 表示可以继续启动, False 表示已有实例

    返回:
    - bool: 检查结果
    """
    if not CONTROL_PID_FILE.exists():
        return True
    
    old_pid = _read_pid_file(CONTROL_PID_FILE)
    if old_pid is None:
        return True
    
    if _is_process_running(old_pid):
        try:
            url = "http://127.0.0.1:19871/status"
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return False   # 已有实例在运行
        except Exception:
            pass
        # 检查是否是我们的控制服务
    
    _remove_pid_file(CONTROL_PID_FILE)
    # 旧进程已不存在, 清理 PID 文件
    return True


def _cleanup_backend() -> None:
    """清理后端进程"""
    global _backend_process
    
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:19870/api/shutdown",
            method="POST",
        )
        urllib.request.urlopen(req, timeout=2)
    except Exception:
        pass
    # 尝试通过 API 停止
    
    if _backend_process is not None:
        try:
            _backend_process.terminate()
            _backend_process.wait(timeout=5)
        except Exception:
            try:
                _backend_process.kill()
            except Exception:
                pass
        _backend_process = None
    # 终止我们启动的进程
    
    backend_pid = _read_pid_file(BACKEND_PID_FILE)
    # 通过 PID 文件终止
    if backend_pid and _is_process_running(backend_pid):
        try:
            if sys.platform == "win32":
                os.kill(backend_pid, signal.SIGTERM)
            else:
                os.kill(backend_pid, signal.SIGTERM)
        except Exception:
            pass
    
    _remove_pid_file(BACKEND_PID_FILE)


def _cleanup_control() -> None:
    """清理控制服务"""
    _cleanup_backend()
    _remove_pid_file(CONTROL_PID_FILE)


def _get_backend_cmd(host: str = "127.0.0.1", port: int = 19870) -> list[str]:
    """
    获取后端启动命令

    参数:
    - host: 主机
    - port: 端口

    返回:
    - list[str]: 后端启动命令
    """
    return [
        sys.executable,
        "-m",
        "satrap.main",
        "--api-host", host,
        "--api-port", str(port),
        "run",
    ]


def _check_backend_health(host: str = "127.0.0.1", port: int = 19870) -> dict[str, Any]:
    """
    检查后端健康状态

    参数:
    - host: 主机
    - port: 端口

    返回:
    - dict[str, Any]: 检查结果
    """
    try:
        url = f"http://{host}:{port}/api/health"
        with urllib.request.urlopen(url, timeout=2) as response:
            return json.loads(response.read().decode())
    except urllib.error.URLError:
        return {"running": False}
    except Exception as e:
        return {"running": False, "error": str(e)}


async def _handle_request(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    """
    处理 HTTP 请求

    参数:
    - reader: 流读取器
    - writer: 流写入器
    """
    global _backend_process
    
    try:
        raw_request = await reader.readuntil(b"\r\n\r\n")
        first_line = raw_request.split(b"\r\n")[0].decode()
        parts = first_line.split(" ")
        method = parts[0]
        path = parts[1] if len(parts) > 1 else "/"
        
        if method == "OPTIONS":
            response = "HTTP/1.1 204 No Content\r\n"
            for k, v in CORS_HEADERS.items():
                response += f"{k}: {v}\r\n"
            response += "\r\n"
            writer.write(response.encode())
            await writer.drain()
            return
        # CORS 预检
        
        status = 200
        body: dict[str, Any] = {}
        
        if method == "GET" and path == "/status":
            health = _check_backend_health()
            body = {
                "running": health.get("running", False),
                "managed": _backend_process is not None and _backend_process.poll() is None,
                "health": health,
            }
        
        elif method == "POST" and path == "/start":
            health = _check_backend_health()
            # 检查是否已在运行
            if health.get("running"):
                body = {"ok": True, "message": "后端已在运行中"}
            elif _backend_process is not None and _backend_process.poll() is None:
                body = {"ok": True, "message": "后端正在启动中"}
            else:
                old_backend_pid = _read_pid_file(BACKEND_PID_FILE)
                # 检查是否有其他后端进程
                if old_backend_pid and _is_process_running(old_backend_pid):
                    try:
                        os.kill(old_backend_pid, signal.SIGTERM)
                        await asyncio.sleep(1)
                    except Exception:
                        pass
                    # 尝试终止旧进程
                
                cmd = _get_backend_cmd()
                # 启动后端
                
                startupinfo = None
                creationflags = 0
                if sys.platform == "win32":
                    startupinfo = subprocess.STARTUPINFO()
                    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                    creationflags = subprocess.CREATE_NO_WINDOW
                
                try:
                    _backend_process = subprocess.Popen(
                        cmd,
                        cwd=str(PROJECT_ROOT),
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        startupinfo=startupinfo,
                        creationflags=creationflags,
                    )
                    
                    _write_pid_file(BACKEND_PID_FILE, _backend_process.pid)
                    # 记录后端 PID
                    
                    for _ in range(30):   # 最多等待 15 秒
                        await asyncio.sleep(0.5)
                        health = _check_backend_health()
                        if health.get("running"):
                            body = {"ok": True, "message": "后端已启动"}
                            break
                    else:
                        body = {"ok": True, "message": "后端启动中，请稍候..."}
                    # 等待后端就绪
                        
                except Exception as e:
                    body = {"ok": False, "error": str(e)}
                    status = 500
        
        elif method == "POST" and path == "/stop":
            _cleanup_backend()
            body = {"ok": True, "message": "后端已停止"}
        
        elif method == "POST" and path == "/restart":
            _cleanup_backend()
            await asyncio.sleep(1)
            
            cmd = _get_backend_cmd()
            # 启动
            startupinfo = None
            creationflags = 0
            if sys.platform == "win32":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                creationflags = subprocess.CREATE_NO_WINDOW
            
            try:
                _backend_process = subprocess.Popen(
                    cmd,
                    cwd=str(PROJECT_ROOT),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    startupinfo=startupinfo,
                    creationflags=creationflags,
                )
                _write_pid_file(BACKEND_PID_FILE, _backend_process.pid)
                body = {"ok": True, "message": "后端重启中"}
            except Exception as e:
                body = {"ok": False, "error": str(e)}
                status = 500
        
        elif method == "POST" and path == "/shutdown":
            body = {"ok": True, "message": "控制服务即将停止"}
            
            response_body = json.dumps(body).encode()
            # 发送响应后再停止
            response = f"HTTP/1.1 {status} OK\r\n"
            response += "Content-Type: application/json\r\n"
            response += f"Content-Length: {len(response_body)}\r\n"
            for k, v in CORS_HEADERS.items():
                response += f"{k}: {v}\r\n"
            response += "\r\n"
            
            writer.write(response.encode() + response_body)
            await writer.drain()
            writer.close()
            
            asyncio.get_event_loop().call_later(0.5, lambda: os._exit(0))
            # 延迟停止
            return
        
        elif method == "GET" and path == "/config":
            try:
                if CONFIG_PATH.exists():
                    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                        config_data = yaml.safe_load(f) or {}
                    body = {"ok": True, "config": config_data, "path": str(CONFIG_PATH)}
                else:
                    body = {"ok": True, "config": {}, "path": str(CONFIG_PATH), "exists": False}
            except Exception as e:
                body = {"ok": False, "error": str(e)}
                status = 500
        
        elif method == "PUT" and path == "/config":
            try:
                cl_idx = raw_request.lower().find(b"content-length:")
                # 读取请求体
                if cl_idx >= 0:
                    cl_end = raw_request.find(b"\r\n", cl_idx)
                    cl_line = raw_request[cl_idx:cl_end].decode()
                    cl = int(cl_line.split(":")[1].strip())
                    request_body = await reader.readexactly(cl)
                    config_data = json.loads(request_body.decode())
                    
                    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                        yaml.dump(config_data, f, allow_unicode=True, default_flow_style=False)
                    # 保存配置
                    
                    body = {"ok": True, "message": "配置已保存", "path": str(CONFIG_PATH)}
                else:
                    body = {"ok": False, "error": "Missing request body"}
                    status = 400
            except Exception as e:
                body = {"ok": False, "error": str(e)}
                status = 500
        
        else:
            status = 404
            body = {"error": "not found"}
        # 接口: GET /status - 获取后端状态
        # 接口: POST /start - 启动后端
        # 接口: POST /stop - 停止后端
        # 接口: POST /restart - 重启后端
        # 接口: POST /shutdown - 停止控制服务和后端
        # 接口: GET /config - 读取配置文件
        # 接口: PUT /config - 保存配置文件
        
        response_body = json.dumps(body).encode()
        # 发送响应
        response = f"HTTP/1.1 {status} OK\r\n"
        response += "Content-Type: application/json\r\n"
        response += f"Content-Length: {len(response_body)}\r\n"
        for k, v in CORS_HEADERS.items():
            response += f"{k}: {v}\r\n"
        response += "\r\n"
        
        writer.write(response.encode() + response_body)
        await writer.drain()
        
    except Exception as e:
        error_body = json.dumps({"error": str(e)}).encode()
        # 发送错误响应
        response = f"HTTP/1.1 500 Internal Server Error\r\n"
        response += "Content-Type: application/json\r\n"
        response += f"Content-Length: {len(error_body)}\r\n"
        for k, v in CORS_HEADERS.items():
            response += f"{k}: {v}\r\n"
        response += "\r\n"
        writer.write(response.encode() + error_body)
        await writer.drain()
    finally:
        writer.close()


async def run_server(host: str = "127.0.0.1", port: int = 19871):
    """
    运行控制服务

    参数:
    - host: 主机
    - port: 端口
    """
    # 检查单实例
    if not _check_single_instance():
        print("Control server is already running")
        sys.exit(1)
    
    _write_pid_file(CONTROL_PID_FILE, os.getpid())
    # 写入 PID 文件
    
    atexit.register(_cleanup_control)
    # 注册退出清理
    
    if sys.platform != "win32":
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    # 注册信号处理
    
    server = await asyncio.start_server(_handle_request, host, port)
    print(f"Backend control server running at http://{host}:{port}")
    print("Endpoints:")
    print("  GET  /status   - Get backend status")
    print("  POST /start    - Start backend")
    print("  POST /stop     - Stop backend")
    print("  POST /restart  - Restart backend")
    print("  POST /shutdown - Stop control server and backend")
    print("  GET  /config   - Read config file")
    print("  PUT  /config   - Save config file")
    
    async with server:
        await server.serve_forever()


def main():
    """入口函数"""
    parser = argparse.ArgumentParser(description="Backend Control Server")
    parser.add_argument("--host", default="127.0.0.1", help="Listen host")
    parser.add_argument("--port", type=int, default=19871, help="Listen port")
    args = parser.parse_args()
    
    try:
        asyncio.run(run_server(args.host, args.port))
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        _cleanup_control()


if __name__ == "__main__":
    main()
