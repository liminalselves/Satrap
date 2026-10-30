"""验证测试选择覆盖新增用例, 业务回归和跨平台边界"""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import select_tests as selection


class TestSelection(unittest.TestCase):
    """使用仓库真实场景表检查计划, 不启动业务服务"""

    def test_changed_functions_and_shared_fixtures(self) -> None:
        """参数化定义变化和新增用例被选中, 共享夹具变化扩大到模块"""
        path = "tests/unit/test_example.py"
        before = "import pytest\n\ndef test_old():\n    assert True\n"
        after = before + "\n@pytest.mark.parametrize('value', [1, 2])\ndef test_new(value):\n    assert value\n"
        self.assertEqual(selection.changed_test_targets(path, before, after), [path + "::test_new"])
        self.assertEqual(selection.changed_test_targets(path, before, before.replace("True", "False")), [path + "::test_old"])
        self.assertEqual(selection.changed_test_targets(path, before, after.replace("import pytest", "import pytest\nfixture_value = 1")), [path])
        self.assertEqual(selection.changed_test_targets(path, "", after), [path])
        self.assertEqual(selection.changed_test_targets(path, before, "import pytest\n"), [])

    def test_frontend_backend_and_document_scopes(self) -> None:
        """前端变更不启动 Python 矩阵, 后端诊断变更仍覆盖对应浏览器"""
        plan = selection.build_plan(["satrap-ui/src/pages/Chat/index.tsx"], "HEAD")
        self.assertFalse(plan["python_tests"])
        self.assertFalse(plan["compatibility_tests"])
        self.assertIn("chat-sidebar-state", plan["e2e"])
        self.assertNotIn("advanced-debug", plan["e2e"])
        plan = selection.build_plan(["satrap/core/log/debug/api.py"], "HEAD")
        self.assertEqual(plan["python_tests"], ["tests/unit/test_logging_flows.py"])
        self.assertIn("advanced-debug", plan["e2e"])
        self.assertEqual(len(plan["compatibility_matrix"]), 8)
        plan = selection.build_plan(["docs/README.md"], "HEAD")
        self.assertFalse(plan["frontend"])
        self.assertFalse(plan["python_tests"])

    def test_full_mode_and_shared_test_setup(self) -> None:
        """手动全量恢复九组业务测试, 公共测试配置变化保留全量回归"""
        plan = selection.build_plan([], "HEAD", full=True)
        self.assertEqual(plan["python_tests"], ["tests/unit"])
        self.assertEqual(plan["compatibility_tests"], ["tests/unit"])
        self.assertEqual(len(plan["compatibility_matrix"]), 8)
        self.assertEqual(set(plan["e2e"]), set(selection.browser_scenarios()))
        self.assertEqual(selection.build_plan(["tests/conftest.py"], "HEAD")["python_tests"], ["tests/unit"])

    def test_all_rules_resolve_to_existing_tests_and_scenarios(self) -> None:
        """校验映射不是失效路径, 每个前端领域都有可执行场景"""
        for _, modules in selection.PYTHON_RULES:
            for module in modules:
                self.assertTrue((selection.ROOT / f"tests/unit/test_{module}.py").is_file(), module)
        for prefixes, cases in selection.E2E_RULES:
            plan = selection.build_plan([prefixes[0] + "example.tsx"], "HEAD")
            self.assertTrue(set(cases).issubset(plan["e2e"]))
        for target in selection.COMPATIBILITY_TESTS:
            path, name = target.split("::")
            self.assertIn("def " + name + "(", (selection.ROOT / path).read_text(encoding="utf-8"))

    def test_changed_browser_file_is_selected_and_deleted_test_is_ignored(self) -> None:
        """新场景自动纳入计划, 已删除测试不会传给 pytest"""
        plan = selection.build_plan(["satrap-ui/e2e/chat-recovery-flows.mjs", "tests/unit/test_deleted.py"], "HEAD")
        self.assertIn("chat-reconnect", plan["e2e"])
        self.assertNotIn("tests/unit/test_deleted.py", plan["python_tests"])
        self.assertEqual(plan["e2e_scripts"]["chat-reconnect"], "e2e/chat-recovery-flows.mjs")

    def test_shared_test_helper_selects_its_consumers(self) -> None:
        """公共测试辅助变化时回归实际导入它的业务模块"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tests/unit").mkdir(parents=True)
            (root / "tests/unit/test_consumer.py").write_text("from tests.support.requests import helper", encoding="utf-8")
            with patch.object(selection, "ROOT", root):
                plan = selection.build_plan(["tests/support/requests.py"], "HEAD")
            self.assertEqual(plan["python_tests"], ["tests/unit/test_consumer.py"])


if __name__ == "__main__":
    unittest.main()
