"""本地 HTTP 验收: 真正结束后端进程, 重启后恢复同一任务"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

import pytest


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def until(predicate, timeout=40):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(.05)
    raise AssertionError("等待验收状态超时")


@pytest.mark.parametrize("scenario", ["manual", "read", "model"])
def test_http_chat_survives_process_kill(tmp_path, scenario):
    shutil.copytree(FIXTURES / "recovery_probe", tmp_path / "plugins" / "recovery_probe")
    token = uuid4().hex
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "SATRAP_API_TOKEN": token,
           "SATRAP_DATA_ROOT": str(tmp_path / "data"), "RECOVERY_PROBE_ROOT": str(tmp_path),
           "RECOVERY_PROBE_SCENARIO": scenario}
    processes = []
    files = []

    def start():
        ready = tmp_path / f"ready-{len(processes)}.json"
        log = (tmp_path / f"worker-{len(processes)}.log").open("w", encoding="utf-8")
        files.append(log)
        process = subprocess.Popen([sys.executable, str(FIXTURES / "chat_recovery_worker.py"), str(ready)],
                                   cwd=tmp_path, env=env, stdout=log, stderr=subprocess.STDOUT)
        processes.append(process)

        def ready_or_error():
            assert process.poll() is None, (tmp_path / f"worker-{len(processes) - 1}.log").read_text(encoding="utf-8")
            if ready.exists():
                try:
                    return json.loads(ready.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    return None

        return process, until(ready_or_error)["port"]

    def request(path, payload=None, expected=200):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        req = Request(f"http://127.0.0.1:{port}/api/chat/{path}", data=data,
                      headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        try:
            with urlopen(req, timeout=10) as response:
                status, body = response.status, json.load(response)
        except HTTPError as error:
            status, body = error.code, json.load(error)
        assert status == expected, body
        return body

    def events():
        return [json.loads(line) for line in (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()]

    def counts():
        rows = events()
        return (sum(row.get("tool") == "read" and row.get("index") == 1 for row in rows),
                sum(row.get("tool") == "write" for row in rows),
                sum(row.get("tool") == "read" and row.get("index") == 2 for row in rows),
                sum("model" in row for row in rows))

    try:
        process, port = start()
        cid = request("conversations", {})["conversation_id"]
        request("send", {"conversation": cid, "text": "依次读取, 写入, 再读取资料"})
        until(lambda: (tmp_path / "blocked").exists())
        runs = request(f"runs?conversation={cid}")["runs"]
        run_id = runs[0]["id"]
        assert runs[0]["status"] == "running"
        before = counts()
        process.kill()  # 模拟没有机会执行 finally 的进程终止
        process.wait(timeout=10)
        (tmp_path / "released").touch()
        _, port = start()
        assert request(f"runs?conversation={cid}")["runs"][0]["id"] == run_id
        action = {"conversation": cid, "run_id": run_id, "action": "resume"}
        request("runs/action", action)

        def state_is(status):
            rows = request(f"runs?conversation={cid}")["runs"]
            return rows and rows[0]["status"] == status

        if scenario == "manual":
            until(lambda: state_is("needs_attention"))
            assert counts() == before
            request("runs/action", {**action, "action": "authorize_retry", "step_id": "tool:0:1"})
            assert counts() == before
            request("runs/action", action)
        until(lambda: state_is("completed"))

        def display_ready():
            turns = request(f"turns?conversation={cid}")["turns"]
            return turns if turns and turns[0]["answer"] == "验收完成" else None

        def idle():
            return all(not item["generating"] for item in request("history")["items"] if item["conversation_id"] == cid)

        def variant_ready():
            current = display_ready()
            return current is not None and current[0]["variant_count"] == 2

        turns = until(display_ready)
        assert len(turns) == 1 and turns[0]["variant_count"] == 1
        expected = {"manual": (1, 2, 1, 3), "read": (1, 1, 2, 3), "model": (1, 1, 1, 4)}[scenario]
        assert counts() == expected
        if scenario == "model":
            attempts = [row["messages"] for row in events() if row.get("model") == 2]
            assert attempts[0] == attempts[1]
            assert "尚未完成" not in turns[0]["answer"]
        request("runs/action", action)
        until(display_ready)
        assert counts() == expected

        until(idle)
        request("retry", {"conversation": cid})
        until(lambda: len(request(f"runs?conversation={cid}")["runs"]) == 2 and state_is("completed"))
        until(variant_ready)
        assert counts() == tuple(a + b for a, b in zip(expected, (1, 1, 1, 3)))
        until(idle)
        fork = request("fork", {"conversation": cid, "turn_index": 1})["conversation_id"]
        assert request(f"runs?conversation={fork}")["runs"] == []
        request("runs/action", {**action, "conversation": fork}, expected=409)
        assert len(request(f"turns?conversation={fork}")["turns"]) == 1
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
        for file in files:
            file.close()
