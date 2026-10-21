"""前端与命令共用的技能导入管理, 不接受模型工具写入"""
from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit, urljoin
from dataclasses import asdict
from typing import cast

from satrap.expend.plugins.skills.store import SkillStore, MAX_MARKDOWN_BYTES
from satrap.core.utils.outbound import safe_sync_get, OutboundHTTPError


def download_markdown(url: str) -> tuple[str, str]:
    """
    下载公开 HTTPS Markdown, 复用 DNS 地址绑定及重定向校验

    参数:
    - url: Markdown 直链或 GitHub 文件链接

    返回:
    - 严格 UTF-8 正文与最终来源地址, 非 Markdown 或超限时拒绝导入
    """
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("技能地址必须是不带凭据的 HTTPS URL")
    parts = parsed.path.split("/")
    if parsed.hostname == "github.com" and len(parts) >= 6 and parts[3] == "blob":
        url = urlunsplit(("https", "raw.githubusercontent.com", "/".join(parts[:3] + parts[4:]), "", ""))
    if not urlsplit(url).path.lower().endswith((".md", ".markdown")):
        raise ValueError("请提供 Markdown 文件地址, 不接受网页或仓库目录")
    response = None
    for attempt in range(4):
        current = urlsplit(url)
        if current.scheme != "https" or current.username or current.password:
            raise ValueError("技能重定向必须使用不带凭据的 HTTPS URL")
        response = safe_sync_get(url, timeout=10, max_response_bytes=MAX_MARKDOWN_BYTES, allow_redirects=False)
        if not response.is_redirect:
            break
        if attempt == 3:
            raise ValueError("技能下载重定向次数超过限制")
        url = urljoin(response.url, response.headers.get("Location", ""))
    if response is None:
        raise OutboundHTTPError("技能下载没有返回响应")
    if response.status_code != 200:
        raise OutboundHTTPError(f"技能下载失败: HTTP {response.status_code}")
    markdown = response.content.decode("utf-8-sig")
    if markdown.lstrip().lower().startswith(("<!doctype html", "<html")):
        raise ValueError("下载结果是网页, 请提供 Markdown 原始文件链接")
    return markdown, response.url


def skill_management(method: str, name: str, payload: dict[str, object] | None = None,
                     store: SkillStore | None = None) -> dict[str, object]:
    """
    执行已认证管理入口的外部技能管理与插件技能只读预览

    参数:
    - method: GET, POST, PUT 或 DELETE
    - name: 更新或删除目标名称, 列表与新建时为空, plugin-catalog 的 GET 返回插件技能预览
    - payload: 管理参数, 不接受任意文件系统路径
    - store: 测试或迁移使用的目录, 默认使用插件管理目录

    返回:
    - 管理结果, 格式错误或版本冲突向调用边界传播
    """
    if method == "GET" and name == "plugin-catalog":
        from satrap.expend.plugins.skills.catalog import plugin_skill_catalog
        return plugin_skill_catalog()
    store = store or SkillStore()
    values = dict(payload or {})
    if method == "GET" and not name:
        return {"items": [asdict(item) for item in store.entries()]}
    if method == "DELETE" and name:
        if set(values) != {"expected_revision"} or type(values["expected_revision"]) is not int:
            raise ValueError("删除需要当前 expected_revision")
        store.delete(name, cast(int, values["expected_revision"]))
        return {"ok": True}
    if method not in {"POST", "PUT"} or bool(name) != (method == "PUT"):
        raise ValueError("不支持的技能管理操作")
    if set(values) - {"markdown", "name", "description", "url", "filename", "expected_revision", "enabled", "scopes"}:
        raise ValueError("技能管理参数包含未知字段")
    for key in ("markdown", "name", "description", "url", "filename"):
        if key in values and not isinstance(values[key], str):
            raise ValueError(f"{key} 必须是文本")
    scopes = values.get("scopes")
    if scopes is not None and (not isinstance(scopes, list) or any(not isinstance(scope, str) for scope in scopes)):
        raise ValueError("scopes 必须是会话类型列表")
    if name and values.get("name", name) != name:
        raise ValueError("更新不能改名, 请另存为新技能")
    if method == "PUT" and "expected_revision" not in values:
        raise ValueError("更新必须提供 expected_revision")
    markdown = cast(str, values.get("markdown", ""))
    source = cast(str, values.get("filename", "upload"))
    if values.get("url"):
        if markdown:
            raise ValueError("Markdown 正文和 URL 只能选择一种导入来源")
        markdown, source = download_markdown(cast(str, values["url"]))
    item = store.save(markdown, name=name or cast(str, values.get("name", "")),
                      description=cast(str, values.get("description", "")), source=source,
                      expected_revision=cast(int | None, values.get("expected_revision")),
                      enabled=cast(bool, values.get("enabled", True)), scopes=cast(list[str] | None, scopes))
    return {"ok": True, "item": asdict(item)}
