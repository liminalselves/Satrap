"""后端控制服务 - 独立运行，用于启动/停止/监控后端

这是一个简单的 HTTP 服务，独立于主后端运行，
提供启动、停止、重启后端的功能，以及配置文件的读写。
默认监听 127.0.0.1:19871
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

# 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# 后端进程
_backend_process: subprocess.Popen | None = None

# 配置文件路径
CONFIG_PATH = PROJECT_ROOT / "config.yaml"

# CORS 头
CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, PUT, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}


def _get_backend_cmd(host: str = "127.0.0.1", port: int = 19870) -> list[str]:
    """获取后端启动命令"""
    return [
        sys.executable,
        "-m",
        "satrap.main",
        "--api-host", host,
        "--api-port", str(port),
        "run",
    ]


def _check_backend_health(host: str = "127.0.0.1", port: int = 19870) -> dict[str, Any]:
    """检查后端健康状态"""
    import urllib.request
    import urllib.error
    
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
    """处理 HTTP 请求"""
    global _backend_process
    
    try:
        raw_request = await reader.readuntil(b"\r\n\r\n")
        first_line = raw_request.split(b"\r\n")[0].decode()
        parts = first_line.split(" ")
        method = parts[0]
        path = parts[1] if len(parts) > 1 else "/"
        
        # CORS 预检
        if method == "OPTIONS":
            response = "HTTP/1.1 204 No Content\r\n"
            for k, v in CORS_HEADERS.items():
                response += f"{k}: {v}\r\n"
            response += "\r\n"
            writer.write(response.encode())
            await writer.drain()
            return
        
        status = 200
        body: dict[str, Any] = {}
        
        # GET /status - 获取后端状态
        if method == "GET" and path == "/status":
            health = _check_backend_health()
            body = {
                "running": health.get("running", False),
                "managed": _backend_process is not None and _backend_process.poll() is None,
                "health": health,
            }
        
        # POST /start - 启动后端
        elif method == "POST" and path == "/start":
            # 检查是否已在运行
            health = _check_backend_health()
            if health.get("running"):
                body = {"ok": True, "message": "后端已在运行中"}
            elif _backend_process is not None and _backend_process.poll() is None:
                body = {"ok": True, "message": "后端正在启动中"}
            else:
                # 启动后端
                cmd = _get_backend_cmd()
                
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
                    
                    # 等待后端就绪
                    for _ in range(30):  # 最多等待 15 秒
                        await asyncio.sleep(0.5)
                        health = _check_backend_health()
                        if health.get("running"):
                            body = {"ok": True, "message": "后端已启动"}
                            break
                    else:
                        body = {"ok": True, "message": "后端启动中，请稍候..."}
                        
                except Exception as e:
                    body = {"ok": False, "error": str(e)}
                    status = 500
        
        # POST /stop - 停止后端
        elif method == "POST" and path == "/stop":
            # 先尝试通过 API 停止
            import urllib.request
            
            try:
                req = urllib.request.Request(
                    "http://127.0.0.1:19870/api/shutdown",
                    method="POST",
                )
                urllib.request.urlopen(req, timeout=2)
            except Exception:
                pass
            
            # 如果是我们启动的进程，也终止它
            if _backend_process is not None:
                try:
                    _backend_process.terminate()
                    _backend_process.wait(timeout=5)
                except Exception:
                    _backend_process.kill()
                _backend_process = None
            
            body = {"ok": True, "message": "后端已停止"}
        
        # POST /restart - 重启后端
        elif method == "POST" and path == "/restart":
            # 停止
            if _backend_process is not None:
                try:
                    _backend_process.terminate()
                    _backend_process.wait(timeout=5)
                except Exception:
                    _backend_process.kill()
                _backend_process = None
            
            await asyncio.sleep(1)
            
            # 启动
            cmd = _get_backend_cmd()
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
                body = {"ok": True, "message": "后端重启中"}
            except Exception as e:
                body = {"ok": False, "error": str(e)}
                status = 500
        
        # GET /config - 读取配置文件
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
        
        # PUT /config - 保存配置文件
        elif method == "PUT" and path == "/config":
            try:
                # 读取请求体
                cl_idx = raw_request.lower().find(b"content-length:")
                if cl_idx >= 0:
                    cl_end = raw_request.find(b"\r\n", cl_idx)
                    cl_line = raw_request[cl_idx:cl_end].decode()
                    cl = int(cl_line.split(":")[1].strip())
                    request_body = await reader.readexactly(cl)
                    config_data = json.loads(request_body.decode())
                    
                    # 保存配置
                    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                        yaml.dump(config_data, f, allow_unicode=True, default_flow_style=False)
                    
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
        
        # 发送响应
        response_body = json.dumps(body).encode()
        response = f"HTTP/1.1 {status} OK\r\n"
        response += "Content-Type: application/json\r\n"
        response += f"Content-Length: {len(response_body)}\r\n"
        for k, v in CORS_HEADERS.items():
            response += f"{k}: {v}\r\n"
        response += "\r\n"
        
        writer.write(response.encode() + response_body)
        await writer.drain()
        
    except Exception as e:
        # 发送错误响应
        error_body = json.dumps({"error": str(e)}).encode()
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
    """运行控制服务"""
    server = await asyncio.start_server(_handle_request, host, port)
    print(f"Backend control server running at http://{host}:{port}")
    print("Endpoints:")
    print("  GET  /status  - Get backend status")
    print("  POST /start   - Start backend")
    print("  POST /stop    - Stop backend")
    print("  POST /restart - Restart backend")
    print("  GET  /config  - Read config file")
    print("  PUT  /config  - Save config file")
    
    async with server:
        await server.serve_forever()


def main():
    """入口函数"""
    import argparse
    
    parser = argparse.ArgumentParser(description="Backend Control Server")
    parser.add_argument("--host", default="127.0.0.1", help="Listen host")
    parser.add_argument("--port", type=int, default=19871, help="Listen port")
    args = parser.parse_args()
    
    try:
        asyncio.run(run_server(args.host, args.port))
    except KeyboardInterrupt:
        print("\nShutting down...")


if __name__ == "__main__":
    main()
