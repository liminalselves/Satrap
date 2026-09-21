"""使用本机 SnowLuma 发行包的真实网络模块验收, QQ 侧仅使用测试替身"""
from __future__ import annotations

from pathlib import Path
import tempfile
import argparse
import hashlib
import asyncio
import secrets
import socket
import json
import re

from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.platform.event import MessageChain
from satrap.core.platform import PlatformConfig


DRIVER = r'''
import { createInterface } from 'node:readline';
const input = createInterface({input: process.stdin});
const lines = input[Symbol.asyncIterator]();
const config = JSON.parse((await lines.next()).value);
const {WsClientAdapter, buildDispatchPayload} = await import('./network.mjs');
const ctx = {
  uin: '10000',
  buildLifecycleEvent: (sub_type) => ({time: 1, self_id: 10000, post_type: 'meta_event', meta_event_type: 'lifecycle', sub_type}),
  buildHeartbeatEvent: () => ({time: 1, self_id: 10000, post_type: 'meta_event', meta_event_type: 'heartbeat', status: {online: true, good: true}, interval: 30000}),
  api: {isAcceptingActions: true, async processStreamRequest(text, send) {
    const request = JSON.parse(text);
    if (request.action !== 'send_group_msg' || request.params.group_id !== 20000) throw new Error('unexpected action');
    const body = request.params.message[0].data.text;
    const id = body === 'probe-first' ? 101 : 102;
    await new Promise(resolve => setTimeout(resolve, id === 101 ? 50 : 0));
    await send(JSON.stringify({status:'ok', retcode:0, data:{message_id:id}, echo:request.echo}));
  }},
};
const adapter = new WsClientAdapter('satrap-probe', {enabled:true, url:config.url, accessToken:config.token, role:'Universal', reconnectIntervalMs:1000}, ctx);
const rejected = new WsClientAdapter('satrap-auth-probe', {enabled:true, url:config.url, accessToken:config.token + '-wrong', role:'Universal'}, ctx);
const connected = async () => {
  const end = Date.now() + 10000;
  while (!adapter.connected && Date.now() < end) await new Promise(resolve => setTimeout(resolve, 25));
  if (!adapter.connected) throw new Error('connection timeout');
};
try {
  rejected.open();
  const rejection = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('auth rejection timeout')), 5000);
    rejected.socket.once('error', error => {clearTimeout(timer); resolve(String(error));});
  });
  if (!/401|403/.test(rejection) || rejected.connected) throw new Error('invalid token was not rejected');
  await rejected.close();
  console.log('AUTH_REJECTED');
  adapter.open();
  await connected();
  console.log('READY');
  for await (const line of { [Symbol.asyncIterator]: () => lines }) {
    const command = JSON.parse(line);
    if (command.type === 'close') break;
    if (command.type === 'reconnect') {
      adapter.socket.terminate();
      await new Promise(resolve => setTimeout(resolve, 150));
      await connected();
      console.log('RECONNECTED');
    }
    if (command.type === 'event') adapter.onEvent(command.event, buildDispatchPayload(command.event));
  }
} finally {
  await rejected.close();
  await adapter.close();
  input.close();
}
'''


def extract_network(installation: Path) -> tuple[str, str]:
    """
    从发行包按源文件标记提取网络实现, 不复制到版本库

    参数:
    - installation: 用户自行安装的 SnowLuma 目录

    返回:
    - tuple[str, str]: 临时模块内容和原包 SHA256
    """
    source = (installation / "index.mjs").read_text(encoding="utf-8")
    sections = [
        ("../onebot/src/event-filter.ts", "../onebot/src/network/quick-operation.ts"),
        ("../websocket/src/native.ts", "../onebot/src/network/ws-server-connections.ts"),
        ("../onebot/src/network/ws-client-adapter.ts", "../onebot/src/network/ws-server-adapter.ts"),
    ]
    builtin = {"util", "fs", "path", "events", "net", "os", "child_process", "http", "https", "url", "worker_threads", "crypto", "tls", "module", "zlib"}
    imports = [line for line in source.splitlines() if (match := re.fullmatch(r'import .+ from "([^"]+)";', line))
               and (match[1].removeprefix("node:") in builtin or match[1] == "node:fs/promises")]
    parts: list[str] = []
    for start, end in sections:
        parts.append(source.split(f"//#region {start}\n", 1)[1].split(f"//#region {end}\n", 1)[0])
    prelude = "const createLogger = () => ({child(){return this}, info(){}, warn(){}, error(){}});\nclass StreamTransportClosedError extends Error {}\n"
    return "\n".join(imports) + "\n" + prelude + "\n".join(parts) + "\nexport {WsClientAdapter, buildDispatchPayload};\n", hashlib.sha256(source.encode("utf-8")).hexdigest()


async def probe(installation: Path) -> dict[str, object]:
    """
    验证真实反向 WebSocket 事件, 动作 echo 关联和重连

    参数:
    - installation: 本机发行包目录

    返回:
    - dict[str, object]: 不包含凭据的验收结果
    """
    module, digest = extract_network(installation)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    token = secrets.token_urlsafe(32)
    adapter = OneBotAdapter(PlatformConfig(id="snowluma-probe", type="onebot", settings={
        "host": "127.0.0.1", "port": port, "access_token": token,
    }))
    process = None
    with tempfile.TemporaryDirectory(prefix="satrap-snowluma-") as temporary:
        root = Path(temporary)
        (root / "network.mjs").write_text(module, encoding="utf-8")
        (root / "driver.mjs").write_text(DRIVER, encoding="utf-8")
        try:
            await adapter.start()
            await adapter.wait_ready()
            process = await asyncio.create_subprocess_exec(str(installation / "node.exe"), str(root / "driver.mjs"), cwd=installation,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            assert process.stdin and process.stdout
            writer = process.stdin

            async def command(value: dict[str, object]) -> None:
                """
                向隔离驱动发送控制帧

                参数:
                - value: 不写入日志的控制对象
                """
                writer.write((json.dumps(value) + "\n").encode("utf-8"))
                await writer.drain()

            await command({"url": f"ws://127.0.0.1:{port}/ws/", "token": token})
            if await asyncio.wait_for(process.stdout.readline(), 10) != b"AUTH_REJECTED\n":
                raise RuntimeError("错误 token 拒绝验证失败")
            if await asyncio.wait_for(process.stdout.readline(), 15) != b"READY\n":
                raise RuntimeError("SnowLuma 网络模块启动失败")
            for message_id in (1, 2):
                if message_id == 2:
                    await command({"type": "reconnect"})
                    assert await asyncio.wait_for(process.stdout.readline(), 15) == b"RECONNECTED\n"
                await command({"type": "event", "event": {"time": 1, "self_id": 10000, "post_type": "message",
                    "message_type": "group", "sub_type": "normal", "group_id": 20000, "user_id": 30000,
                    "message_id": message_id, "sender": {"user_id": 30000, "nickname": "probe"},
                    "message": [{"type": "text", "data": {"text": "通信探针"}}]}})
                event = await asyncio.wait_for(adapter._event_queue.get(), 5)
                assert event.call_origin.source_message_id == str(message_id)
                assert event.get_message_str() == "通信探针"
                responses = await asyncio.wait_for(asyncio.gather(*[
                    adapter.send_message(event.session_id, MessageChain.from_text(text))
                    for text in ("probe-first", "probe-second")
                ]), 5)
                assert all(response.status == "success" for response in responses)
                assert [response.message_ids for response in responses] == [("101",), ("102",)]
                adapter._event_queue.task_done()
            await command({"type": "close"})
            assert await asyncio.wait_for(process.wait(), 5) == 0
            return {"version": json.loads((installation / "package.json").read_text(encoding="utf-8"))["version"],
                    "bundle_sha256": digest, "event_roundtrips": 2, "correlated_actions": 4, "reconnect": "passed", "wrong_token": "rejected",
                    "transport": "installed WsClientAdapter and native websocket", "qq": "simulated"}
        finally:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            if process is not None and process.returncode:
                assert process.stderr
                error = (await process.stderr.read()).decode("utf-8", errors="replace").replace(token, "[redacted]")
                print(error[:3000])
            await adapter.terminate()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installation", type=Path, required=True)
    arguments = parser.parse_args()
    print(json.dumps(asyncio.run(probe(arguments.installation.resolve())), ensure_ascii=False, indent=2))
