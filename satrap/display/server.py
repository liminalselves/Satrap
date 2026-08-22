"""
聊天展示层独立 HTTP + WebSocket 服务

基于共享基类 satrap.core.utils.minihttp.MiniHTTPServer, 只保留本服务特有逻辑:

与平台后端 (BackendManager) 完全隔离:
- 不初始化平台适配器 / 事件分发 / Pipeline / SessionManager / UserManager
- 仅经 ModelConfigManager 读 .satrap/model_config.json (与平台后端同一份配置)
- 独立端口 (默认 19872), 独立进程: python -m satrap.display.server

API (前缀 /api/chat/):
- GET  /api/chat/health                     健康检查
- GET  /api/chat/models                     LLM 配置名列表
- POST /api/chat/conversations              新建会话 {model, think, system_prompt} -> {conversation_id}
- GET  /api/chat/conversations              会话列表 (display db)
- GET  /api/chat/turns?conversation=xxx     对话轮次 (含工具明细)
- POST /api/chat/send                       发送 {conversation, text, think} -> 立即返回, WS 推流
- GET  /api/chat/plugins                    插件清单 (扫描 + 启用状态)
- POST /api/chat/plugins/{name}/enable      启用插件 (对活动会话即时 install)
- POST /api/chat/plugins/{name}/disable     停用插件 (对活动会话即时 uninstall)
- POST /api/chat/plugins/{name}/capability  能力独立启停 {kind, cap, enabled}
- GET  /api/chat/plugins/{name}/config      插件配置 (schema + 当前值)
- PUT  /api/chat/plugins/{name}/config      保存插件配置 {config}
- GET  /api/chat/memories?scope=xxx         列出记忆
- POST /api/chat/memories                   添加记忆 {title, content, tags, importance, scope}
- PUT  /api/chat/memories/{id}              更新记忆 {title, content, tags, importance}
- DELETE /api/chat/memories/{id}?scope=xxx  删除记忆

WebSocket:
- /ws/chat?conversation=xxx                 订阅会话实时事件 (thinking/content/tool/turn_done)
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import signal
from typing import Any
from urllib.parse import unquote

from satrap.core.framework.BackGroundManager import ModelConfigManager
from satrap.core.log import logger
from satrap.core.utils.minihttp import MiniHTTPServer
from satrap.display.plugins import ChatPluginRegistry
from satrap.display.recorder import list_conversations
from satrap.display.service import ChatService

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 19872


class ChatHTTPServer(MiniHTTPServer):
    """聊天展示层 HTTP + WS 服务器 (基于共享基类, 零依赖 asyncio)"""

    def __init__(
        self,
        service: ChatService,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
    ) -> None:
        """
        初始化 ChatHTTPServer

        参数:
        - service: 服务实例
        - host: 监听地址
        - port: 监听端口
        """
        super().__init__(host=host, port=port, log_errors=True)
        self.service = service

    async def start(self) -> None:
        """启动"""
        await super().start()
        logger.info(f"[聊天服务] HTTP API: http://{self.host}:{self.port}")

    # ---------- WebSocket 端点分发 ----------

    async def _ws_dispatch(
        self, path: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """
        按路径分发 WebSocket 端点

        参数:
        - path: 路径
        - reader: 流读取器
        - writer: 流写入器
        """
        if path.split("?", 1)[0] == "/ws/chat":
            await self._ws_chat_handler(reader, writer, path)
        else:
            await self._ws_close(writer, 1008, "unknown endpoint")

    async def _ws_chat_handler(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, path: str
    ) -> None:
        """
        订阅会话广播, 把队列消息经 WS 推给客户端

        参数:
        - reader: 流读取器
        - writer: 流写入器
        - path: 路径
        """
        conversation_id = self._query_param(path, "conversation")
        if not conversation_id:
            await self._ws_send(writer, {"type": "error", "message": "missing conversation"})
            await self._ws_close(writer, 1008, "missing conversation")
            return

        queue = self.service.subscribe(conversation_id)
        await self._ws_send(writer, {"type": "subscribed", "conversation_id": conversation_id})
        try:
            while True:
                if reader.at_eof():
                    break
                    # 等待广播消息 (带超时以便检测客户端断开)
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=1.0)
                    await self._ws_send(writer, msg)
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"[聊天服务] WS 推送异常: {e}")
        finally:
            self.service.unsubscribe(conversation_id, queue)

    # ---------- API 路由 ----------

    async def _route(self, method: str, path: str, body: bytes) -> tuple[int, dict[str, Any]]:
        svc = self.service
        clean = path.split("?", 1)[0]

        if method == "GET" and clean == "/api/chat/health":
            return 200, {"ok": True, "conversations": len(svc._conversations)}

        if method == "GET" and clean == "/api/chat/models":
            return 200, {"models": svc.list_models()}

        if method == "GET" and clean == "/api/chat/models/detail":
            return 200, svc.list_models_detail()
        # 模型配置管理

        if method == "POST" and clean == "/api/chat/models":
            payload = json.loads(body or b"{}")
            return 200, svc.add_model(payload)

        if clean.startswith("/api/chat/models/"):
            model_name = unquote(clean[len("/api/chat/models/"):])
            if method == "PUT":
                payload = json.loads(body or b"{}")
                return 200, svc.update_model(model_name, payload)
            if method == "DELETE":
                return 200, svc.delete_model(model_name)
            return 405, {"error": f"method not allowed: {method}"}

        if method == "POST" and clean == "/api/chat/conversations":
            payload = json.loads(body or b"{}")
            try:
                cid = await svc.create_conversation(
                    model=str(payload.get("model") or "default"),
                    think=str(payload.get("think") or "off"),
                    system_prompt=str(payload.get("system_prompt") or "") or None,
                    project_id=str(payload.get("project_id") or "") or None,
                )
            except ValueError as e:
                return 400, {"ok": False, "error": str(e)}
            return 200, {"ok": True, "conversation_id": cid}

        if method == "GET" and clean == "/api/chat/conversations":
            return 200, {"conversations": list_conversations()}

        if method == "POST" and clean.startswith("/api/chat/conversations/") and clean.endswith("/project"):
            conv_id = unquote(clean[len("/api/chat/conversations/"):-len("/project")])
            if not conv_id:
                return 400, {"error": "缺少 conversation_id"}
            payload = json.loads(body or b"{}")
            project_id = payload.get("project_id")
            result = svc.set_conversation_project(conv_id, str(project_id) if project_id else None)
            return (200 if result.get("ok") else 400), result
        # 会话改绑项目: POST /api/chat/conversations/{id}/project  (project_id 为 null = 移出项目)

        if method == "GET" and clean == "/api/fs/browse":
            dir_path = self._query_param(path, "path") or ""
            result = svc.browse_directories(dir_path)
            return (200 if result.get("ok") else 400), result
        # 目录浏览: GET /api/fs/browse?path=xxx  (前端新建项目选择工作区; 只列子目录, 只读)

        if clean == "/api/projects":
            if method == "GET":
                return 200, {"projects": svc.list_projects()}
            if method == "POST":
                payload = json.loads(body or b"{}")
                result = svc.create_project(
                    str(payload.get("name") or ""),
                    str(payload.get("root_path") or ""),
                )
                return (200 if result.get("ok") else 400), result
        # 项目 (工作区文件夹绑定)

        if method == "DELETE" and clean.startswith("/api/projects/"):
            project_id = unquote(clean[len("/api/projects/"):])
            if not project_id:
                return 400, {"error": "缺少 project_id"}
            return 200, svc.delete_project(project_id)
        # 接口: DELETE /api/projects/{id}  (仅解绑会话, 不动磁盘)

        if method == "DELETE" and clean.startswith("/api/chat/conversations/"):
            conv_id = unquote(clean[len("/api/chat/conversations/"):])
            if not conv_id:
                return 400, {"error": "缺少 conversation_id"}
            return 200, await svc.delete_conversation(conv_id)
        # 接口: DELETE /api/chat/conversations/{id}

        if method == "GET" and clean == "/api/chat/turns":
            conv = self._query_param(path, "conversation")
            if not conv:
                return 400, {"error": "缺少 conversation 参数"}
            return 200, {"turns": svc.list_turns(conv)}

        if method == "POST" and clean == "/api/chat/send":
            payload = json.loads(body or b"{}")
            conv = str(payload.get("conversation") or "").strip()
            text = str(payload.get("text") or "")
            think = payload.get("think")
            attachments = payload.get("attachments")   # 返回类型: list[dict] | None
            if not conv:
                return 400, {"error": "缺少 conversation 参数"}
            # think 缺省 (None) 时由 ChatService 用会话默认
            think = str(think) if think is not None else None
            result = await svc.send(conv, text, think=think, attachments=attachments)
            return (200 if result.get("ok") else 400), result

        if method == "POST" and clean == "/api/chat/upload":
            payload = json.loads(body or b"{}")
            conv = str(payload.get("conversation") or "").strip()
            file_name = str(payload.get("file_name") or "")
            file_data_b64 = str(payload.get("file_data") or "")
            if not conv or not file_name or not file_data_b64:
                return 400, {"error": "缺少 conversation / file_name / file_data"}
            try:
                file_data = base64.b64decode(file_data_b64)
            except Exception:
                return 400, {"error": "file_data base64 解码失败"}
            return 200, svc.save_upload(conv, file_name, file_data)
        # 文件上传

        if method == "POST" and clean == "/api/chat/retry":
            payload = json.loads(body or b"{}")
            conv = str(payload.get("conversation") or "").strip()
            if not conv:
                return 400, {"error": "缺少 conversation 参数"}
            think = payload.get("think")
            think = str(think) if think is not None else None
            result = await svc.retry(conv, think=think)
            return (200 if result.get("ok") else 400), result
        # 重试与分支

        if method == "POST" and clean == "/api/chat/fork":
            payload = json.loads(body or b"{}")
            conv = str(payload.get("conversation") or "").strip()
            turn_index = int(payload.get("turn_index") or 0)
            if not conv:
                return 400, {"error": "缺少 conversation 参数"}
            result = await svc.fork(conv, turn_index)
            return (200 if result.get("ok") else 400), result

        if method == "POST" and clean == "/api/chat/cancel":
            payload = json.loads(body or b"{}")
            conv = str(payload.get("conversation") or "").strip()
            if not conv:
                return 400, {"error": "缺少 conversation 参数"}
            result = await svc.cancel(conv)
            return (200 if result.get("ok") else 400), result
        # 取消生成

        if method == "GET" and clean == "/api/chat/plugins":
            return 200, {"plugins": svc.list_plugins()}

        if clean.startswith("/api/chat/plugins/") and clean.endswith("/config"):
            rest = clean[len("/api/chat/plugins/"):]
            name = unquote(rest[:rest.index("/")])
            if method == "GET":
                return 200, svc.get_plugin_config(name)
            if method == "PUT":
                payload = json.loads(body or b"{}")
                cfg = payload.get("config")
                if not isinstance(cfg, dict):
                    return 400, {"error": "缺少 config 对象"}
                return 200, svc.save_plugin_config(name, cfg)
            return 405, {"error": f"method not allowed: {method}"}
        # 接口: GET/PUT /api/chat/plugins/{name}/config (需在 POST 分支之前匹配)

        if method == "POST" and clean.startswith("/api/chat/plugins/"):
            rest = clean[len("/api/chat/plugins/"):]
            parts = rest.split("/")
            name = unquote(parts[0])
            action = parts[1] if len(parts) > 1 else ""
            if action == "enable":
                return 200, await svc.set_plugin_enabled(name, True)
            if action == "disable":
                return 200, await svc.set_plugin_enabled(name, False)
            if action == "capability":
                payload = json.loads(body or b"{}")
                result = await svc.set_plugin_capability(
                    name,
                    str(payload.get("kind") or ""),
                    str(payload.get("cap") or ""),
                    bool(payload.get("enabled", True)),
                )
                return (200 if result.get("ok") else 400), result
            return 404, {"error": f"unknown plugin action: {action}"}
        # 接口: POST /api/chat/plugins/{name}/enable|disable|capability

        if method == "GET" and clean == "/api/chat/memories":
            scope = self._query_param(path, "scope") or "web_chat"
            return 200, svc.list_memories(scope)
        # 接口: GET /api/chat/memories?scope=xxx

        if method == "POST" and clean == "/api/chat/memories":
            payload = json.loads(body or b"{}")
            title = str(payload.get("title") or "").strip()
            content = str(payload.get("content") or "").strip()
            if not title or not content:
                return 400, {"error": "title 与 content 不能为空"}
            return 200, svc.add_memory(
                title, content,
                tags=str(payload.get("tags") or ""),
                importance=int(payload.get("importance") or 1),
                scope=str(payload.get("scope") or "web_chat"),
            )
        # 接口: POST /api/chat/memories

        if clean.startswith("/api/chat/memories/"):
            memory_id = unquote(clean[len("/api/chat/memories/"):])
            if method == "PUT":
                payload = json.loads(body or b"{}")
                fields: dict[str, Any] = {}
                for key in ("title", "content", "tags", "importance"):
                    if key in payload:
                        fields[key] = payload[key]
                scope = str(payload.get("scope") or "web_chat")
                return 200, svc.update_memory(memory_id, scope=scope, **fields)
            if method == "DELETE":
                scope = self._query_param(path, "scope") or "web_chat"
                return 200, svc.delete_memory(memory_id, scope=scope)
            return 405, {"error": f"method not allowed: {method}"}
        # 接口: PUT /api/chat/memories/{id}  /  DELETE /api/chat/memories/{id}?scope=xxx

        return 404, {"error": f"unknown route: {method} {path}"}


# ---------- 入口 ----------

async def _run(host: str, port: int) -> None:
    model_cfg = ModelConfigManager()
    plugins = ChatPluginRegistry()
    service = ChatService(model_cfg, plugins)
    server = ChatHTTPServer(service, host=host, port=port)
    await server.start()
    print(f"Satrap 聊天服务已启动: http://{host}:{port} (按 Ctrl+C 停止)")

    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in ("SIGINT", "SIGTERM"):
        try:
            loop.add_signal_handler(getattr(signal, sig), stop.set)
        except (NotImplementedError, AttributeError):
            pass
    try:
        await stop.wait()
    finally:
        await service.close()
        await server.stop()
        logger.info("[聊天服务] 已停止")


def main() -> None:
    """执行 `main` 操作"""
    parser = argparse.ArgumentParser(description="Satrap 聊天展示层服务")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    try:
        asyncio.run(_run(args.host, args.port))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
