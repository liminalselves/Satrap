"""验证发行包独立导入, 原生依赖, 插件资源与完整启动停止流程"""
from __future__ import annotations

import argparse
import importlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def check_imports() -> None:
    """确认所有依赖来自便携目录并执行关键原生功能"""
    for name in ("satrap", "mcp", "splintr", "faiss", "av", "openpyxl", "docx", "pdfplumber", "pypdfium2", "pip", "aiocqhttp", "win32api", "pythoncom"):
        module = importlib.import_module(name)
        if not Path(module.__file__).resolve().is_relative_to(ROOT):
            raise AssertionError(f"依赖从发行包之外加载: {name}")
    import faiss
    import numpy as np
    from satrap.core.utils.tokenizer import tokenizer_estimate
    from satrap.core.utils.paths import get_project_root
    from satrap.display.plugins import ChatPluginRegistry

    assert get_project_root() == ROOT
    assert tokenizer_estimate("你好, Satrap") > 0
    index = faiss.IndexFlatL2(2)
    index.add(np.array([[1, 2]], dtype="float32"))
    assert index.ntotal == 1
    plugins = ChatPluginRegistry().scan()
    assert len(plugins) >= 5, plugins
    assert (ROOT / "satrap/expend/plugins/satrap_coding/skills/goal/skill.md").is_file()
    assert (ROOT / "satrap/expend/skills/web-research/meta.yaml").is_file()
    assert (ROOT / "satrap-ui/dist/index.html").is_file()
    subprocess.run([sys.executable, "-X", "utf8", "-m", "satrap", "--help"], cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
    subprocess.run([sys.executable, "-X", "utf8", "-m", "pip", "check"], cwd=ROOT, check=True)
    print("运行时, 原生依赖, 插件资源和 CLI 验证通过", flush=True)


def free_ports() -> list[int]:
    """预留三个不同空闲端口, 验证结束前释放监听器"""
    sockets = []
    try:
        for _ in range(3):
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            sockets.append(listener)
        return [listener.getsockname()[1] for listener in sockets]
    finally:
        for listener in sockets:
            listener.close()


def get(url: str, json_body: bool = True):
    """使用当前发行目录的令牌访问测试服务"""
    token = (ROOT / ".satrap/credentials/api-token").read_text(encoding="utf-8").strip()
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with HTTP.open(request, timeout=3) as response:
        body = response.read()
    return json.loads(body.decode("utf-8")) if json_body else body


def spawn_orphan(module: str, args: list[str], url: str) -> subprocess.Popen:
    """创建不受启动器管理的旧服务, 用于验证残留清理"""
    log_path = ROOT / ".satrap" / (module.rsplit(".", 1)[-1] + "-orphan.log")
    with log_path.open("ab") as log:
        process = subprocess.Popen(
            [sys.executable, "-X", "utf8", "-u", "-m", module, *args],
            cwd=ROOT, stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW,
        )
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError(log_path.read_text(encoding="utf-8"))
            try:
                get(url)
                return process
            except (OSError, ValueError):
                time.sleep(0.2)
        raise AssertionError("遗留服务启动超时: " + log_path.read_text(encoding="utf-8"))
    except BaseException:
        process.terminate()
        process.wait(timeout=5)
        raise


def check_lifecycle() -> None:
    """验证真实启动, 静态页面, 鉴权, 重复启动及停止后端"""
    backend_port, control_port, chat_port = free_ports()
    config = ROOT / ".satrap/config.yaml"
    if config.exists():
        raise RuntimeError("完整冒烟验证只能在新解压的发行目录执行, 不覆盖已有配置")
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps({"api": {"host": "127.0.0.1", "port": backend_port}}), encoding="utf-8")
    launcher = ROOT / "release/launcher.py"
    command = [sys.executable, "-X", "utf8", str(launcher), "start", "--no-browser", "--control-port", str(control_port), "--chat-port", str(chat_port)]
    with socket.socket() as foreign:
        foreign.bind(("127.0.0.1", chat_port))
        foreign.listen()
        refused = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=45)
        assert refused.returncode == 1, refused.stdout + refused.stderr
        assert str(chat_port) in refused.stderr and f"PID {os.getpid()}" in refused.stderr, refused.stderr
        with socket.socket() as probe:
            assert probe.connect_ex(("127.0.0.1", chat_port)) == 0
    print("其他进程占用端口时拒绝启动并报告 PID, 原进程保留", flush=True)
    orphans = []
    try:
        orphans.append(spawn_orphan("satrap.core.backend.control_server", ["--port", str(control_port)], f"http://127.0.0.1:{control_port}/status"))
        orphans.append(spawn_orphan("satrap.display.server", ["--port", str(chat_port)], f"http://127.0.0.1:{chat_port}/api/chat/health"))
        orphans.append(spawn_orphan("satrap.main", ["--api-port", str(backend_port), "run"], f"http://127.0.0.1:{backend_port}/api/health"))
    except BaseException:
        for orphan in orphans:
            if orphan.poll() is None:
                orphan.terminate()
                orphan.wait(timeout=5)
        raise
    output = ROOT / ".satrap/smoke-launcher.log"
    with output.open("wb") as log:
        process = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 90
            state_path = ROOT / ".satrap/release-instance.json"
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise AssertionError(output.read_text(encoding="utf-8"))
                if state_path.exists() and json.loads(state_path.read_text(encoding="utf-8")).get("ready"):
                    break
                time.sleep(0.3)
            else:
                raise AssertionError("启动超时: " + output.read_text(encoding="utf-8"))
            for orphan in orphans:
                orphan.wait(timeout=5)
            print("启动前旧控制服务, 聊天服务和平台后端残留清理通过", flush=True)
            base = f"http://127.0.0.1:{control_port}"
            assert get(base + "/status")["running"]
            assert get(f"http://127.0.0.1:{chat_port}/api/chat/health")["ok"]
            ui_config = get(base + "/ui-config.json")
            assert ui_config["chat_api"] == f"http://127.0.0.1:{chat_port}", ui_config
            assert ui_config["backend_api"] == f"http://127.0.0.1:{backend_port}", ui_config
            assert ui_config["control_api"] == base, ui_config
            html = get(base + "/", False)
            assert b'<div id="root">' in html
            assert get(base + "/chat", False) == html
            assert get(base + "/config")["exists"]
            assert "default" in get(base + "/config/models")
            subprocess.run(command, cwd=ROOT, timeout=10, check=True)
            print("WebUI, 服务地址, 管理接口和重复启动验证通过", flush=True)
        finally:
            try:
                if process.poll() is None:
                    subprocess.run([sys.executable, "-X", "utf8", str(launcher), "stop"], cwd=ROOT, timeout=60, check=True)
                process.wait(timeout=15)
            finally:
                for orphan in orphans:
                    if orphan.poll() is None:
                        orphan.terminate()
                        orphan.wait(timeout=5)
    assert process.returncode == 0, output.read_text(encoding="utf-8")
    for port in (backend_port, control_port, chat_port):
        with socket.socket() as probe:
            assert probe.connect_ex(("127.0.0.1", port)) != 0, f"服务进程残留: {port}"
    print("服务停止与端口释放验证通过", flush=True)
    orphan = spawn_orphan("satrap.display.server", ["--port", str(chat_port)], f"http://127.0.0.1:{chat_port}/api/chat/health")
    try:
        subprocess.run([
            sys.executable, "-X", "utf8", str(launcher), "stop", "--control-port", str(control_port), "--chat-port", str(chat_port),
        ], cwd=ROOT, timeout=60, check=True)
        orphan.wait(timeout=5)
        with socket.socket() as probe:
            assert probe.connect_ex(("127.0.0.1", chat_port)) != 0
    finally:
        if orphan.poll() is None:
            orphan.terminate()
            orphan.wait(timeout=5)
    print("没有活动启动器时, stop 清理孤立服务并确认端口释放", flush=True)


def main() -> None:
    """清除宿主 Satrap 环境变量, 在独立发行目录执行冒烟验证"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--imports-only", action="store_true")
    args = parser.parse_args()
    for key in list(os.environ):
        if key.startswith("SATRAP_"):
            del os.environ[key]
    os.environ.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    os.chdir(ROOT)
    check_imports()
    if not args.imports_only:
        check_lifecycle()


if __name__ == "__main__":
    main()
