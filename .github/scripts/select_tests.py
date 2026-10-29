"""按 PR 差异选择业务回归, 将完整测试保留为手动入口"""
from __future__ import annotations

import argparse
import ast
import json
import os
from pathlib import Path
import re
import subprocess
from typing import TypedDict


ROOT = Path(__file__).resolve().parents[2]


class TestPlan(TypedDict):
    """提供给工作流的固定计划结构"""

    python_tests: list[str]
    compatibility_tests: list[str]
    compatibility_matrix: list[dict[str, str]]
    frontend: bool
    e2e: list[str]
    e2e_scripts: dict[str, str]
    full: bool


CORE_TESTS = ("configuration_models", "workflow_execution_contract")
PYTHON_RULES = (
    (("satrap/core/APICall/", "satrap/core/utils/model_history.py"), ("llm_protocol_flows", "responses_flows")),
    (("satrap/display/",), ("display_service", "chat_stream_flows")),
    (("satrap/core/log/",), ("logging_flows",)),
    (("satrap/expend/plugins/debug_logger/",), ("logging_flows",)),
    (("satrap/core/telemetry/",), ("telemetry_flows",)),
    (("satrap/core/backend/",), ("control_dispatch", "control_api_flows")),
    (("satrap/core/framework/Base/",), ("workflow_execution_contract",)),
    (("satrap/core/framework/",), ("session_management_flows", "session_execution_flows")),
    (("satrap/core/config/rag", "satrap/core/rag.py", "satrap/expend/plugins/rag/"), ("knowledge_flows",)),
    (("satrap/core/config/",), ("configuration_documents", "configuration_models")),
    (("satrap/core/storage/", "satrap/core/state/"), ("conversation_storage_flows", "checkpoint_flows")),
    (("satrap/core/utils/context/",), ("context_policy_flows", "context_storage_flows")),
    (("satrap/core/utils/TCBuilder/",), ("workflow_execution_contract", "permissions_flows")),
    (("satrap/core/utils/sandbox/", "satrap/expend/plugins/satrap_coding/"), ("coding_security_flows",)),
    (("satrap/core/utils/media",), ("media_projection_flows", "visual_input_flows")),
    (("satrap/core/utils/minihttp.py", "satrap/core/server_auth.py"), ("minihttp", "control_api_flows")),
    (("satrap/core/platform/onebot/",), ("onebot_adapter", "platform_reply_flows")),
    (("satrap/core/platform/misskey/",), ("platform_misskey_flows",)),
    (("satrap/core/platform/", "satrap/core/group_chat/"), ("platform_reply_flows", "group_message_flows")),
    (("satrap/core/memory/", "satrap/expend/plugins/memory/"), ("memory_flows",)),
    (("satrap/core/friends/", "satrap/expend/plugins/friend_manager/"), ("request_approval_flows",)),
    (("satrap/core/approval/", "satrap/expend/plugins/account_requests/"), ("request_approval_flows",)),
    (("satrap/core/pipeline/",), ("pipeline_scheduler",)),
    (("satrap/expend/plugins/group_admin/",), ("group_administration_flows",)),
    (("satrap/expend/plugins/group_chat/",), ("group_message_flows", "group_media_flows")),
    (("satrap/expend/plugins/message_forward/",), ("platform_forwarding_flows",)),
    (("satrap/expend/plugins/session_commands/",), ("session_command_flows",)),
    (("satrap/expend/plugins/base_take/",), ("file_access_flows", "coding_security_flows")),
    (("satrap/edictum/", "satrap/core/plugin_interop/", "satrap/expend/plugins/"), ("plugin_interaction_flows", "simple_session")),
    (("satrap/_version.py",), ("version_source",)),
    (("satrap/core/type.py",), ("configuration_models", "responses_flows", "workflow_execution_contract")),
    (("satrap/cli/",), ("cli_flows", "cli_configuration_flows")),
    (("scripts/build_release.py", "scripts/release/"), ("release_flows",)),
    (("satrap/", "scripts/", "pyproject.toml", "requirements.txt"), CORE_TESTS),
)
COMPATIBILITY_TESTS = (
    "tests/unit/test_configuration_models.py::test_maps_all_fields",
    "tests/unit/test_responses_flows.py::test_components_run_without_backend_or_telemetry",
    "tests/unit/test_coding_security_flows.py::test_sandbox_worker_lifecycle_without_approval",
    "tests/unit/test_coding_security_flows.py::test_shell_native_io_exit_and_output_budget",
)
DEFAULT_E2E = (
    "backend-password-recovery", "runtime-cleanup", "attached-service-password-recovery",
    "chat-fork-projects", "chat-sidebar-state", "chat-history-races",
    "runtime-application-progress", "log-stream", "advanced-debug", "chat-model-binding", "chat-reconnect",
)
E2E_RULES = (
    (("satrap-ui/src/pages/Chat/", "satrap/display/"), ("chat-fork-projects", "chat-sidebar-state", "chat-history-races", "chat-model-binding")),
    (("satrap-ui/src/pages/Logs/", "satrap/core/log/"), ("log-stream", "advanced-debug")),
    (("satrap-ui/src/pages/Sessions/", "satrap-ui/src/pages/Plugins/", "satrap/edictum/"), ("runtime-application-progress",)),
    (("satrap-ui/src/pages/Models/",), ("asr-models", "chat-model-binding")),
    (("satrap-ui/src/pages/Groups/",), ("group-chat-plugin",)),
    (("satrap-ui/src/pages/Friends/", "satrap-ui/src/pages/Approvals/"), ("friends", "account-requests")),
    (("satrap-ui/src/pages/Platforms/",), ("backend-platform-status",)),
    (("satrap-ui/src/pages/Rag/", "satrap-ui/src/components/rag/"), ("rag-settings", "rag-upload")),
    (("satrap-ui/src/pages/Conversations/",), ("conversations", "conversation-users")),
    (("satrap-ui/src/pages/Settings/",), ("form-required", "auth-startup")),
    (("satrap-ui/src/pages/Docs/", "docs/user/"), ("user-docs",)),
    (("satrap-ui/src/api/auth", "satrap-ui/src/api/serviceFetch", "satrap-ui/src/components/common/PasswordGate",
      "satrap-ui/src/components/common/LogoutButton", "satrap/core/server_auth.py", "satrap/core/utils/minihttp.py"),
     ("backend-password-recovery", "runtime-cleanup", "attached-service-password-recovery")),
)


def browser_scenarios() -> dict[str, str]:
    """
    从现有浏览器场景表取得入口, 新增场景无需另建命令别名

    返回:
    - 场景名到前端相对脚本路径的映射
    """
    result: dict[str, str] = {}
    for path in sorted((ROOT / "satrap-ui/e2e").glob("*.mjs")):
        source = path.read_text(encoding="utf-8")
        if "export const scenarios =" in source:
            for name in re.findall(r"^\s*\[['\"]([\w-]+)['\"]\s*,", source.split("export const scenarios =", 1)[1], re.MULTILINE):
                if name in result:
                    raise ValueError(f"浏览器场景名重复: {name}")
                result[name] = path.relative_to(ROOT / "satrap-ui").as_posix()
    return result


def git(*args: str) -> str:
    """
    读取 Git 数据, 命令失败时阻止生成不完整计划

    参数:
    - args: 独立传递的 Git 参数

    返回:
    - UTF-8 标准输出
    """
    return subprocess.check_output(["git", *args], cwd=ROOT, encoding="utf-8")


def previous_source(base: str, path: str) -> str:
    """
    读取变更前源码, 新增文件返回空字符串

    参数:
    - base: 基线 Git 修订
    - path: 仓库相对路径

    返回:
    - 旧源码或空字符串, 其它 Git 错误继续阻断计划
    """
    exists = subprocess.run(["git", "cat-file", "-e", f"{base}:{path}"], cwd=ROOT, capture_output=True).returncode == 0
    return git("show", f"{base}:{path}") if exists else ""


def changed_test_targets(path: str, before: str, after: str) -> list[str]:
    """
    比较测试 AST, 仅选择新增或改动用例; 共享夹具变化时回归整份模块

    参数:
    - path: 仓库相对测试路径
    - before: 基线源码, 新文件传空字符串
    - after: 当前源码

    返回:
    - pytest 文件或函数节点, 参数化用例由 pytest 正常展开
    """
    def split(source: str) -> tuple[dict[str, str], list[str]]:
        """
        提取测试定义与共享模块结构

        参数:
        - source: Python 源码

        返回:
        - 测试 AST 与其余模块 AST
        """
        tests: dict[str, str] = {}
        shared: list[str] = []
        for node in ast.parse(source).body:
            dump = ast.dump(node, include_attributes=False)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
                tests[node.name] = dump
            elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                tests[node.name] = dump
            else:
                shared.append(dump)
        return tests, shared

    old, old_shared = split(before)
    new, new_shared = split(after)
    if not before or old_shared != new_shared:
        return [path]
    return [f"{path}::{name}" for name, body in new.items() if old.get(name) != body]


def build_plan(changed: list[str], base: str, full: bool = False) -> TestPlan:
    """
    合并改动测试与必要业务回归, 不在各平台重复运行整套业务

    参数:
    - changed: 相对于基线的路径, 包括删除项
    - base: 读取旧测试定义的 Git 修订
    - full: 手动全量模式

    返回:
    - 工作流消费的测试范围和前端开关
    """
    targets: set[str] = set()
    e2e: set[str] = set()
    backend_changed = any(path.startswith(("satrap/", "tests/", "scripts/")) or path in {"pyproject.toml", "requirements.txt"} for path in changed)
    frontend = full or any(path.startswith(("satrap-ui/", "docs/user/")) or path == "satrap/_version.py" for path in changed)
    if full or "tests/conftest.py" in changed:
        targets.add("tests/unit")
    else:
        for path in changed:
            if path.startswith("tests/unit/test_") and path.endswith(".py") and (ROOT / path).is_file():
                targets.update(changed_test_targets(path, previous_source(base, path), (ROOT / path).read_text(encoding="utf-8-sig")))
            if path.startswith("tests/support/"):
                module = path.removesuffix(".py").replace("/", ".")
                targets.update(test.relative_to(ROOT).as_posix() for test in (ROOT / "tests/unit").glob("test_*.py")
                               if module in test.read_text(encoding="utf-8-sig"))
            for prefixes, modules in PYTHON_RULES:
                if path.startswith(prefixes):
                    targets.update(f"tests/unit/test_{module}.py" for module in modules)
                    break
    for path in changed:
        for prefixes, cases in E2E_RULES:
            if path.startswith(prefixes):
                e2e.update(cases)
        if path.startswith("satrap-ui/e2e/") and (ROOT / path).is_file():
            source = (ROOT / path).read_text(encoding="utf-8")
            if "export const scenarios =" in source:
                names = set(re.findall(r"^\s*\[['\"]([\w-]+)['\"]\s*,", source.split("export const scenarios =", 1)[1], re.MULTILINE))
                old = previous_source(base, path)
                old_names = set(re.findall(r"^\s*\[['\"]([\w-]+)['\"]\s*,", old.split("export const scenarios =", 1)[1], re.MULTILINE)) if "export const scenarios =" in old else set()
                e2e.update((names - old_names) | (names & set(DEFAULT_E2E)))
            else:
                e2e.update(DEFAULT_E2E)
    if any(path.startswith(("satrap-ui/src/api/", "satrap-ui/src/components/", "satrap-ui/src/styles/")) or path in {
        "satrap-ui/src/App.tsx", "satrap-ui/src/router.tsx", "satrap-ui/package.json", "satrap-ui/package-lock.json",
        "satrap/core/backend/control_server.py", ".github/workflows/code-sandbox.yml",
    } for path in changed):
        e2e.update(DEFAULT_E2E)
    scripts = browser_scenarios()
    if full:
        e2e = set(scripts)
    if missing := e2e - scripts.keys():
        raise ValueError(f"未找到浏览器场景入口: {sorted(missing)}")
    frontend = frontend or bool(e2e)
    if backend_changed and not targets:
        targets.update(f"tests/unit/test_{module}.py" for module in CORE_TESTS)
    if "tests/unit" in targets:
        targets = {"tests/unit"}
    else:
        targets = {target for target in targets if "::" not in target or target.split("::", 1)[0] not in targets}
    matrix = [{"os": platform, "python": version}
              for platform in ("windows-latest", "macos-latest", "ubuntu-latest")
              for version in ("3.11", "3.12", "3.13") if (platform, version) != ("ubuntu-latest", "3.13")]
    # Linux / Python 3.13 已承担业务回归, 其余环境只执行兼容小套件, 保留曾出现问题的 Windows 和 macOS 旧版本组合
    return {"python_tests": sorted(targets), "compatibility_tests": ["tests/unit"] if full else list(COMPATIBILITY_TESTS) if backend_changed else [],
            "compatibility_matrix": matrix, "frontend": frontend, "e2e": sorted(e2e), "e2e_scripts": {name: scripts[name] for name in sorted(e2e)}, "full": full}


def main() -> None:
    """生成测试计划, 并将任务开关写入 GitHub 输出"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("ci-test-plan.json"))
    args = parser.parse_args()
    base = git("merge-base", args.base, "HEAD").strip()
    changed = git("diff", "--name-only", "--no-renames", "-z", f"{base}..HEAD").rstrip("\0").split("\0")
    plan = build_plan([path for path in changed if path], base, args.full)
    args.output.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(plan, indent=2))
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a", encoding="utf-8") as handle:
            for key, value in {"python": bool(plan["python_tests"]), "compatibility": bool(plan["compatibility_tests"]),
                               "frontend": plan["frontend"], "browser": bool(plan["e2e"])}.items():
                handle.write(f"{key}={str(value).lower()}\n")
            handle.write(f"matrix={json.dumps(plan['compatibility_matrix'])}\n")


if __name__ == "__main__":
    main()
