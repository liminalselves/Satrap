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
- POST /api/chat/conversations/preload      预分配 ID 并加载 Session 与插件, 不持久化空会话
- GET  /api/chat/conversations              会话列表 (display db)
- GET  /api/chat/history                    历史分页、过滤和统计
- POST /api/chat/history/delete             批量回收历史
- GET  /api/chat/history/trash              历史回收站
- POST /api/chat/history/trash/restore      恢复历史
- POST /api/chat/history/trash/purge        永久删除回收项
- POST /api/chat/turns/variant              切换最后一轮回复版本
- GET  /api/chat/turns?conversation=xxx     对话轮次 (含工具明细)
- POST /api/chat/send                       发送 {conversation, text, think} -> 立即返回, WS 推流
- POST /api/chat/ask-user/answer            回填 ask_user 工具等待的用户回答
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
- /ws/chat?conversation=xxx                 订阅会话实时事件 (thinking/content/tool/ask_user/turn_done)
"""
from __future__ import annotations

from urllib.parse import unquote, urlsplit
import argparse
import asyncio
import base64
import signal
from typing import Any, cast
import json

from satrap.core.framework.BackGroundManager import ModelConfigManager
from satrap.core.config.session_overrides import OverrideConflictError
from satrap.core.config.rag_service import RagOperationError, rag_admin_request, rag_upload_document, require_stored_session
from satrap.core.utils.async_worker import WorkerBusyError, RAG_WORKERS
from satrap.edictum.plugin_settings import model_options
from satrap.core.utils.documents import DEFAULT_MAX_FILE_SIZE
from satrap.core.utils.minihttp import MiniHTTPServer
from satrap.core.config.loader import ConfigLoader
from satrap.display.recorder import list_conversations
from satrap.display.plugins import ChatPluginRegistry
from satrap.display.service import ChatService
from satrap.core.storage import StorageMaintenanceService
from satrap.core.rag import RagService

from satrap.core.log import logger

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
        super().__init__(
            host=host,
            port=port,
            log_errors=True,
            session_namespace="chat",
        )
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
        try:
            snapshot = self.service.runtime_snapshot(conversation_id)
            await asyncio.wait_for(self._ws_send(writer, snapshot), timeout=5.0)
            while True:
                if reader.at_eof():
                    break
                    # 等待广播消息 (带超时以便检测客户端断开)
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                if msg["type"] == "resync_required":
                    await asyncio.wait_for(self._ws_send(writer, msg), timeout=5.0)
                    await asyncio.wait_for(self._ws_close(writer, 1013, "resync required"), timeout=2.0)
                    return
                if msg.get("seq", 0) <= snapshot["seq"]:
                    continue
                await asyncio.wait_for(self._ws_send(writer, msg), timeout=5.0)
        except asyncio.CancelledError:
            pass
        except asyncio.TimeoutError:
            writer.transport.abort()   # 慢客户端不能无限占用发送协程
        except Exception as e:
            logger.warning(f"[聊天服务] WS 推送异常: {e}")
        finally:
            self.service.unsubscribe(conversation_id, queue)

    # ---------- API 路由 ----------

    def _request_body_limit(self, method: str, path: str) -> int:
        """
        为文件上传单独设置 32 MiB 上限

        参数:
        - method: HTTP 方法
        - path: 请求路径

        返回:
        - 当前路由的请求体字节上限
        """
        if method == "POST" and urlsplit(path).path == "/api/chat/rag/upload":
            return DEFAULT_MAX_FILE_SIZE
        return super()._request_body_limit(method, path)

    async def _route(self, method: str, path: str, body: bytes) -> tuple[int, dict[str, Any]]:
        svc = self.service
        clean = path.split("?", 1)[0]

        if method == "GET" and clean == "/api/chat/health":
            persisted = sum(1 for conv in svc._conversations.values() if conv.persisted)
            return 200, {
                "ok": True,
                "conversations": persisted,
                "preloaded": len(svc._conversations) - persisted,
            }

        if method == "GET" and clean == "/api/chat/models":
            return 200, {"models": svc.list_models()}

        if clean == "/api/chat/plugin-model-options" and method == "GET":
            svc._model_cfg.reload()
            return 200, {"options": model_options(svc._model_cfg)}

        if (clean == "/api/chat/rag" and method in {"GET", "POST"}) or (clean == "/api/chat/rag/upload" and method == "POST"):
            try:
                session_id = self._query_param(path, "session_id")
                if session_id and session_id not in svc._conversations:
                    require_stored_session(svc._storage.platform_db(svc._platform_id), session_id)
                payload = {"kb_id": self._query_param(path, "kb_id")} if method == "GET" or clean.endswith("/upload") else json.loads(body or b"{}")
                svc._model_cfg.reload()
                service = RagService(svc._storage, svc._model_cfg, svc._platform_id, session_id)
                if clean.endswith("/upload"):
                    return 200, await RAG_WORKERS.run(
                        rag_upload_document, service, self._query_param(path, "kb_id"),
                        self._query_param(path, "file_name"), body, self._query_param(path, "source"),
                    )
                return 200, await RAG_WORKERS.run(rag_admin_request, service, method, payload)
            except RagOperationError as error:
                return error.status, {"error": str(error), "stage": error.stage}
            except WorkerBusyError as error:
                return 503, {"error": str(error)}
            except (OSError, TypeError, ValueError, TimeoutError) as error:
                return 400, {"error": str(error)}

        if clean == "/api/chat/session-plugin-config" and method in {"GET", "PUT"}:
            try:
                conversation_id = self._query_param(path, "conversation_id")
                name = self._query_param(path, "plugin")
                if method == "GET":
                    return 200, svc.session_plugin_config(conversation_id, name)
                payload = json.loads(body or b"{}")
                if not isinstance(payload, dict):
                    raise ValueError("请求体必须是对象")
                payload = cast(dict[str, Any], payload)
                revision = payload.get("expected_revision")
                if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
                    raise ValueError("expected_revision 必须是非负整数")
                return 200, await svc.save_session_plugin_config(
                    conversation_id, name, payload.get("overrides", {}), revision,
                )
            except OverrideConflictError as error:
                return 409, {"error": str(error)}
            except WorkerBusyError as error:
                return 503, {"error": str(error)}
            except (TypeError, ValueError) as error:
                return 400, {"error": str(error)}

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

        if method == "POST" and clean == "/api/chat/conversations/preload":
            payload = json.loads(body or b"{}")
            temperature = payload.get("temperature")
            try:
                cid = await svc.preload_conversation(
                    model=str(payload.get("model") or "default"),
                    think=str(payload.get("think") or "off"),
                    system_prompt=str(payload.get("system_prompt") or "") or None,
                    project_id=str(payload.get("project_id") or "") or None,
                    temperature=float(temperature) if temperature is not None else None,
                )
            except (TypeError, ValueError) as e:
                return 400, {"ok": False, "error": str(e)}
            return 200, {"ok": True, "conversation_id": cid}

        if method == "GET" and clean == "/api/chat/conversations":
            return 200, {"conversations": list_conversations(db_path=svc._display_db_path)}

        if method == "GET" and clean == "/api/chat/history":
            raw_days = self._query_param(path, "older_than_days")
            try:
                return 200, svc.query_history(
                    search=self._query_param(path, "search"),
                    project_id=self._query_param(path, "project_id") or None,
                    model=self._query_param(path, "model"),
                    turn_count=self._query_param(path, "turn_count") or "all",
                    older_than_days=float(raw_days) if raw_days else None,
                    page=int(self._query_param(path, "page") or 1),
                    page_size=int(self._query_param(path, "page_size") or 50),
                )
            except (TypeError, ValueError) as error:
                return 400, {"error": str(error)}
        if clean == "/api/chat/history/storage" and method in {"GET", "POST"}:
            return 200, await asyncio.to_thread(
                StorageMaintenanceService(svc._storage).session_size_snapshot,
                svc._platform_id, refresh=method == "POST",
            )
        # 接口: GET /api/chat/history

        if method == "POST" and clean == "/api/chat/history/delete":
            try:
                parsed_payload: object = json.loads(body or b"{}")
                if not isinstance(parsed_payload, dict):
                    raise ValueError("请求体必须是 JSON 对象")
                payload = cast(dict[str, object], parsed_payload)
                raw_ids = payload.get("conversation_ids", [])
                raw_filters = payload.get("filters", {})
                if not isinstance(raw_ids, list) or not isinstance(raw_filters, dict):
                    raise ValueError("conversation_ids 必须是数组且 filters 必须是对象")
                return 200, await svc.delete_conversations(
                    mode=str(payload.get("mode") or "selected"),
                    conversation_ids=[str(item) for item in cast(list[object], raw_ids)],
                    filters=dict(cast(dict[str, Any], raw_filters)),
                    force=bool(payload.get("force", False)),
                )
            except (TypeError, ValueError) as error:
                return 400, {"error": str(error)}
        # 接口: POST /api/chat/history/delete

        if method == "GET" and clean == "/api/chat/history/trash":
            return 200, svc.list_history_archives()
        # 接口: GET /api/chat/history/trash

        if method == "POST" and clean == "/api/chat/history/trash/restore":
            try:
                payload = json.loads(body or b"{}")
                archive_id = str(payload.get("archive_id") or "").strip()
                if not archive_id:
                    raise ValueError("archive_id 不能为空")
                return 200, svc.restore_history_archive(archive_id)
            except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
                return 400, {"error": str(error)}
        # 接口: POST /api/chat/history/trash/restore

        if method == "POST" and clean == "/api/chat/history/trash/purge":
            try:
                payload = json.loads(body or b"{}")
                archive_id = str(payload.get("archive_id") or "").strip()
                if not archive_id:
                    raise ValueError("archive_id 不能为空")
                return 200, svc.purge_history_archive(archive_id)
            except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
                return 400, {"error": str(error)}
        # 接口: POST /api/chat/history/trash/purge

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
            try:
                return 200, await svc.delete_conversation(conv_id)
            except ValueError as error:
                return 409, {"error": str(error)}
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
            preload_settings = {
                key: payload[key]
                for key in ("model", "temperature", "system_prompt", "project_id")
                if key in payload
            }
            result = await svc.send(
                conv,
                text,
                think=think,
                attachments=attachments,
                preload_settings=preload_settings,
            )
            return (200 if result.get("ok") else 400), result

        if method == "POST" and clean == "/api/chat/ask-user/answer":
            payload = json.loads(body or b"{}")
            conv = str(payload.get("conversation") or "").strip()
            request_id = str(payload.get("request_id") or "").strip()
            answer = str(payload.get("answer") or "")
            if not conv or not request_id:
                return 400, {"error": "缺少 conversation / request_id 参数"}
            result = svc.answer_user_input(conv, request_id, answer)
            return (200 if result.get("ok") else 400), result
        # ask_user 工具回答回填

        if method == "GET" and clean == "/api/chat/media":
            conversation = self._query_param(path, "conversation")
            source = self._query_param(path, "source")
            if not conversation or not source:
                return 400, {"error": "缺少 conversation / source 参数"}
            try:
                return 200, await asyncio.to_thread(svc.preview_media, conversation, source)
            except (ValueError, OSError) as error:
                return 400, {"error": str(error)}

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

        if method == "GET" and clean == "/api/chat/runs":
            conv = self._query_param(path, "conversation")
            if not conv:
                return 400, {"error": "缺少 conversation 参数"}
            try:
                result = await svc.list_runs(conv, limit=int(self._query_param(path, "limit") or 20), cursor=self._query_param(path, "cursor") or None, unfinished=self._query_param(path, "unfinished") == "true")
            except ValueError as error:
                return 400, {"ok": False, "error": str(error)}
            return (200 if result.get("ok") else 404), result

        if method == "POST" and clean == "/api/chat/runs/action":
            payload = json.loads(body or b"{}")
            conv = str(payload.get("conversation") or "").strip()
            run_id = str(payload.get("run_id") or "").strip()
            if not conv or not run_id:
                return 400, {"error": "缺少 conversation 或 run_id 参数"}
            result = await svc.manage_run(conv, run_id, str(payload.get("action") or ""), str(payload.get("step_id") or ""))
            return (200 if result.get("ok") else 409), result

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

        if method == "POST" and clean == "/api/chat/turns/variant":
            payload = json.loads(body or b"{}")
            conv = str(payload.get("conversation") or "").strip()
            if not conv:
                return 400, {"error": "缺少 conversation 参数"}
            try:
                turn_index = int(payload["turn_index"])
                variant_index = int(payload["variant_index"])
            except (KeyError, TypeError, ValueError):
                return 400, {"error": "turn_index 和 variant_index 必须是整数"}
            result = await svc.select_variant(conv, turn_index, variant_index)
            return (200 if result.get("ok") else 409), result

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
                return 200, await svc.save_plugin_config(
                    name,
                    dict(cast(dict[str, Any], cfg)),
                )
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
            scope = self._query_param(path, "scope")
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
                scope=str(payload.get("scope") or ""),
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
                scope = str(payload.get("scope") or "")
                return 200, svc.update_memory(memory_id, scope=scope, **fields)
            if method == "DELETE":
                scope = self._query_param(path, "scope")
                return 200, svc.delete_memory(memory_id, scope=scope)
            return 405, {"error": f"method not allowed: {method}"}
        # 接口: PUT /api/chat/memories/{id}  /  DELETE /api/chat/memories/{id}?scope=xxx

        return 404, {"error": f"unknown route: {method} {path}"}


# ---------- 入口 ----------

async def _run(host: str, port: int) -> None:
    model_cfg = ModelConfigManager()
    plugins = ChatPluginRegistry()
    backend_config = ConfigLoader.autodetect()
    service = ChatService(
        model_cfg,
        plugins,
        workspace_roots=backend_config.workspace_roots,
    )
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
