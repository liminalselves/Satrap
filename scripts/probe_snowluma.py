"""使用本机 SnowLuma 发行包的真实网络模块验收, QQ 侧仅使用测试替身"""
from __future__ import annotations

from pathlib import Path
import tempfile
import argparse
import hashlib
import asyncio
import secrets
import urllib.parse
import socket
import json
import re

from satrap.core.platform.onebot.adapter import OneBotAdapter
from satrap.core.platform.event import MessageChain
from satrap.core.components import File, Image, Plain
from satrap.core.platform import PlatformConfig
from satrap.core.config.group_directory import GroupDirectoryStore


DRIVER = r'''
import { createInterface } from 'node:readline';
const input = createInterface({input: process.stdin});
const lines = input[Symbol.asyncIterator]();
const config = JSON.parse((await lines.next()).value);
const {WsClientAdapter, buildDispatchPayload} = await import('./network.mjs');
const actions = [];
const ctx = {
  uin: '10000',
  buildLifecycleEvent: (sub_type) => ({time: 1, self_id: 10000, post_type: 'meta_event', meta_event_type: 'lifecycle', sub_type}),
  buildHeartbeatEvent: () => ({time: 1, self_id: 10000, post_type: 'meta_event', meta_event_type: 'heartbeat', status: {online: true, good: true}, interval: 30000}),
  api: {isAcceptingActions: true, async processStreamRequest(text, send) {
    const request = JSON.parse(text);
    const params = request.params || {};
    if (params.group_id !== undefined && ![20000, 20001].includes(params.group_id)) throw new Error('unexpected group');
    if (request.action === 'send_group_msg') {
      const first = params.message[0];
      if (first.type === 'file') {
        actions.push('file_segment:' + first.data.name);
        await send(JSON.stringify({status:'ok', retcode:0, data:{message_id:301}, echo:request.echo}));
        return;
      }
      const body = first.data.text;
      actions.push('text:' + body);
      const id = body === 'probe-first' ? 101 : 102;
      await new Promise(resolve => setTimeout(resolve, id === 101 ? 50 : 0));
      await send(JSON.stringify({status:'ok', retcode:0, data:{message_id:id}, echo:request.echo}));
      return;
    }
    if (request.action === 'upload_group_file') {
      actions.push('upload:' + params.name);
      if (params.name === 'probe-reject.bin') {
        await send(JSON.stringify({status:'failed', retcode:1200, wording:'rejected', echo:request.echo}));
        return;
      }
      if (params.name === 'probe-missing.bin') {
        await send(JSON.stringify({status:'failed', retcode:10002, wording:'unsupported', echo:request.echo}));
        return;
      }
      if (params.name === 'probe-drop.bin') {
        setTimeout(() => adapter.socket.terminate(), 10);
        return;
      }
      await send(JSON.stringify({status:'ok', retcode:0, data:{file_id:'F-' + params.name}, echo:request.echo}));
      return;
    }
    throw new Error('unexpected action ' + request.action);
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
    if (command.type === 'dump_actions') {
      console.log('ACTIONS ' + JSON.stringify(actions.splice(0, actions.length)));
      continue;
    }
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
        "group_management_version": 1,
    }))
    process = None
    with tempfile.TemporaryDirectory(prefix="satrap-snowluma-") as temporary:
        root = Path(temporary)
        (root / "network.mjs").write_text(module, encoding="utf-8")
        (root / "driver.mjs").write_text(DRIVER, encoding="utf-8")
        group_store = GroupDirectoryStore(root / "platform.db")
        group_store.adopt_legacy("10000", {"group_management_version": 1})
        group_store.patch_group("10000", "20000", "policy", {
            "enabled": {"mode": "value", "value": True},
        }, expected_revision=0)
        adapter.set_group_access_store(group_store)
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

            assert adapter.allows_group("20000") and not adapter.allows_group("20001")
            await command({"type": "event", "event": {"time": 1, "self_id": 10000, "post_type": "message",
                "message_type": "group", "sub_type": "normal", "group_id": 20001, "user_id": 30000,
                "message_id": 3, "sender": {"user_id": 30000, "nickname": "probe"},
                "message": [{"type": "text", "data": {"text": "未启用的群"}}]}})
            try:
                unexpected = await asyncio.wait_for(adapter._event_queue.get(), 0.5)
            except asyncio.TimeoutError:
                unexpected = None
            assert unexpected is None, "未启用的群消息不得进入业务队列"
            group_store.patch_group("10000", "20001", "policy", {
                "enabled": {"mode": "value", "value": True},
            }, expected_revision=0)
            await adapter.refresh_group_access("10000")
            assert adapter.allows_group("20000") and adapter.allows_group("20001")
            await command({"type": "event", "event": {"time": 1, "self_id": 10000, "post_type": "message",
                "message_type": "group", "sub_type": "normal", "group_id": 20001, "user_id": 30000,
                "message_id": 4, "sender": {"user_id": 30000, "nickname": "probe"},
                "message": [{"type": "text", "data": {"text": "第二群"}}]}})
            second_group = await asyncio.wait_for(adapter._event_queue.get(), 5)
            assert second_group.get_group_id() == "20001" and second_group.get_message_str() == "第二群"
            adapter._event_queue.task_done()

            # 入站图片: SnowLuma 上报形态为 {url, file: fileId, sub_type, summary},
            # 两个标识都必须保留, 否则后续无法经 get_image 刷新过期的 url
            await command({"type": "event", "event": {"time": 1, "self_id": 10000, "post_type": "message",
                "message_type": "group", "sub_type": "normal", "group_id": 20000, "user_id": 30000,
                "message_id": 5, "sender": {"user_id": 30000, "nickname": "probe"},
                "message": [{"type": "image", "data": {
                    "url": "https://multimedia.example.invalid/download?rkey=probe",
                    "file": "probe-image.image", "sub_type": 0, "summary": "[图片]"}}]}})
            image_event = await asyncio.wait_for(adapter._event_queue.get(), 5)
            images = [item for item in image_event.get_messages() if isinstance(item, Image)]
            assert len(images) == 1, images
            assert images[0].file == "probe-image.image", images[0].file
            assert images[0].url == "https://multimedia.example.invalid/download?rkey=probe", images[0].url
            assert "[图片]" in image_event.get_message_str()
            adapter._event_queue.task_done()

            # 混合链分流: Plain/File/Plain 按原序经真实网络层到达模拟 QQ 动作
            await command({"type": "dump_actions"})
            assert (await asyncio.wait_for(process.stdout.readline(), 5)).startswith(b"ACTIONS ")
            mixed = MessageChain([
                Plain("段一"), File(name="probe-ok.bin", url="https://example.invalid/probe-ok.bin"), Plain("段二"),
            ])
            receipt = await asyncio.wait_for(adapter.send_message("group%20000", mixed), 15)
            assert receipt.status == "success", receipt
            assert receipt.message_ids == ("102", "F-probe-ok.bin", "102"), receipt

            # 上传被业务拒绝 (1200): 前文已确认, 链在文件处停止, 聚合为部分成功
            partial = await asyncio.wait_for(adapter.send_message("group%20000", MessageChain([
                Plain("段三"), File(name="probe-reject.bin", url="https://example.invalid/probe-reject.bin"), Plain("段四"),
            ])), 15)
            assert partial.status == "partial" and partial.message_ids == ("102",), partial

            # 传输层断开: 动作应答永不到达, 由 aiocqhttp 默认 60 秒 API 超时收敛为 NetworkError,
            # 适配器归一为 unknown 回执, 不假定成功; 客户端自动重连后链路恢复
            unknown = await asyncio.wait_for(adapter.send_message("group%20000", MessageChain([
                File(name="probe-drop.bin", url="https://example.invalid/probe-drop.bin"),
            ])), 75)
            assert unknown.status == "unknown", unknown
            alive = None
            for _ in range(20):
                await asyncio.sleep(0.5)
                candidate = await asyncio.wait_for(adapter.send_message("group%20000", MessageChain.from_text("probe-alive")), 10)
                if candidate.status == "success":
                    alive = candidate
                    break
            assert alive is not None, "断线重连后发送未恢复"

            # 接口缺失 (10002): 记入能力缓存, 回落 file 段发送且保持未确认语义; 同连接代次不重复试错
            for expected_ids in (("301",), ("301",)):
                fallback = await asyncio.wait_for(adapter.send_message("group%20000", MessageChain([
                    File(name="probe-missing.bin", url="https://example.invalid/probe-missing.bin"),
                ])), 15)
                # 普通消息回包只证明消息动作返回, 不构成文件送达的证据
                assert fallback.status == "unknown", fallback
                assert fallback.reason.startswith("file_delivery_unconfirmed"), fallback
                assert fallback.message_ids == expected_ids, fallback

            await command({"type": "dump_actions"})
            actions_line = await asyncio.wait_for(process.stdout.readline(), 5)
            assert actions_line.startswith(b"ACTIONS "), actions_line
            observed = json.loads(actions_line[len(b"ACTIONS "):].decode("utf-8"))
            assert observed == [
                "text:段一", "upload:probe-ok.bin", "text:段二",
                "text:段三", "upload:probe-reject.bin",
                "upload:probe-drop.bin",
                "text:probe-alive",
                "upload:probe-missing.bin", "file_segment:probe-missing.bin",
                "file_segment:probe-missing.bin",
            ], observed
            await command({"type": "close"})
            assert await asyncio.wait_for(process.wait(), 5) == 0
            return {"version": json.loads((installation / "package.json").read_text(encoding="utf-8"))["version"],
                    "bundle_sha256": digest, "event_roundtrips": 3, "group_isolation": "selected_then_enabled", "correlated_actions": 4, "reconnect": "passed", "wrong_token": "rejected",
                    "mixed_chain": "ordered_upload_split", "partial": "confirmed_prefix", "drop": "unknown", "file_fallback": "unknown:file_delivery_unconfirmed",
                    "inbound_image": "file_and_url_preserved",
                    "transport": "installed WsClientAdapter and native websocket", "qq": "simulated"}
        finally:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            if process is not None and process.returncode:
                assert process.stderr
                error = (await process.stderr.read()).decode("utf-8", errors="replace").replace(token, "[redacted]").replace(urllib.parse.quote(token, safe=""), "[redacted]")
                print(error[:3000])
            await adapter.terminate()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installation", type=Path, required=True)
    arguments = parser.parse_args()
    print(json.dumps(asyncio.run(probe(arguments.installation.resolve())), ensure_ascii=False, indent=2))
